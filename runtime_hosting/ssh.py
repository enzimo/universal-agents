"""Interactive SSH PTY broker with exact targets and broker-owned key material."""

from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import timedelta
import fcntl
import hashlib
import logging
import os
from pathlib import Path
import pty
import select
import signal
import stat
import struct
import subprocess
import termios
from typing import Protocol

from runtime_hosting.artifacts import ArtifactStore
from runtime_hosting.authorization import ExecutionAccessGuard
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    InteractiveSessionStatus,
    InteractiveSessionV1,
)
from storage.execution_control_repository import ExecutionControlRepository


@dataclass(frozen=True)
class SSHCredentialMaterial:
    identity_file: Path
    known_hosts_file: Path


class SSHCredentialMaterialProvider(Protocol):
    def prepare(
        self, session: InteractiveSessionV1
    ) -> AbstractContextManager[SSHCredentialMaterial]: ...


@dataclass(frozen=True)
class ConfiguredSSHCredentialMaterialProvider:
    """Resolve only operator-configured binding ids to broker-owned key paths."""

    materials: dict[str, SSHCredentialMaterial]

    def prepare(
        self, session: InteractiveSessionV1
    ) -> AbstractContextManager[SSHCredentialMaterial]:
        material = self.materials.get(session.target.credential_binding_id)
        if material is None:
            raise PermissionError("SSH credential binding is not installed in this broker.")
        return nullcontext(material)


@dataclass
class _LiveSSHSession:
    session: InteractiveSessionV1
    lease_token: str
    process: subprocess.Popen[bytes]
    master_fd: int
    material_context: AbstractContextManager[SSHCredentialMaterial]
    transcript: bytearray = field(default_factory=bytearray)
    expiry_monitor: asyncio.Task[None] | None = None


class SSHSessionUnavailable(RuntimeError):
    pass


@dataclass
class SSHSessionBroker:
    repository: ExecutionControlRepository
    authorization_guard: ExecutionAccessGuard
    material_provider: SSHCredentialMaterialProvider
    artifact_store: ArtifactStore
    team_id: str
    broker_id: str
    ssh_executable: str = "ssh"
    lease_ttl_seconds: int = 300
    transcript_limit_bytes: int = 4_194_304
    logger: logging.Logger = logging.getLogger(__name__)
    _sessions: dict[str, _LiveSSHSession] = field(default_factory=dict, init=False)

    async def open(self, *, session_id: str, lease_token: str) -> InteractiveSessionV1:
        claimed = self.repository.claim_interactive_session(
            team_id=self.team_id,
            session_id=session_id,
            broker_id=self.broker_id,
            lease_token=lease_token,
            lease_ttl_seconds=self.lease_ttl_seconds,
        )
        try:
            self.authorization_guard.require_ssh_connect(claimed)
        except Exception:
            self.repository.update_claimed_interactive_session(
                claimed.model_copy(update={"status": InteractiveSessionStatus.FAILED}),
                broker_id=self.broker_id,
                lease_token=lease_token,
                fence=claimed.fence,
                release=True,
            )
            raise
        claimed = claimed.model_copy(
            update={
                "status": InteractiveSessionStatus.CONNECTING,
                "updated_at_utc": self.repository.current_time_utc(),
            }
        )
        claimed = self.repository.update_claimed_interactive_session(
            claimed,
            broker_id=self.broker_id,
            lease_token=lease_token,
            fence=claimed.fence,
        )
        context: AbstractContextManager[SSHCredentialMaterial] | None = None
        master_fd = -1
        slave_fd = -1
        process: subprocess.Popen[bytes] | None = None
        context_entered = False
        try:
            context = self.material_provider.prepare(claimed)
            material = context.__enter__()
            context_entered = True
            self._verify_material(claimed, material)
            master_fd, slave_fd = pty.openpty()
            self._set_terminal_size(
                master_fd,
                rows=claimed.terminal_rows,
                columns=claimed.terminal_columns,
            )
            process = subprocess.Popen(
                self._ssh_argv(claimed, material),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
            )
            os.close(slave_fd)
            slave_fd = -1
            opened = claimed.model_copy(
                update={
                    "status": InteractiveSessionStatus.OPEN,
                    "updated_at_utc": self.repository.current_time_utc(),
                }
            )
            opened = self.repository.update_claimed_interactive_session(
                opened,
                broker_id=self.broker_id,
                lease_token=lease_token,
                fence=claimed.fence,
            )
            live = _LiveSSHSession(
                session=opened,
                lease_token=lease_token,
                process=process,
                master_fd=master_fd,
                material_context=context,
            )
            self._sessions[session_id] = live
            live.expiry_monitor = asyncio.create_task(
                self._monitor_expiry(session_id),
                name=f"ssh-session-expiry:{session_id}",
            )
            return opened
        except Exception:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(process.wait),
                        timeout=5,
                    )
                except TimeoutError:
                    process.kill()
                    await asyncio.to_thread(process.wait)
            if master_fd >= 0:
                os.close(master_fd)
            if slave_fd >= 0:
                os.close(slave_fd)
            if context_entered and context is not None:
                context.__exit__(None, None, None)
            failed = claimed.model_copy(
                update={
                    "status": InteractiveSessionStatus.FAILED,
                    "updated_at_utc": self.repository.current_time_utc(),
                }
            )
            self.repository.update_claimed_interactive_session(
                failed,
                broker_id=self.broker_id,
                lease_token=lease_token,
                fence=claimed.fence,
                release=True,
            )
            raise

    async def write(self, *, session_id: str, payload: bytes) -> int:
        """Type into the session: needs a person's approval for this session (trusted record)."""

        live = self._require_live(session_id)
        interaction = self.repository.get_ssh_interaction(team_id=self.team_id, session_id=session_id)
        self.authorization_guard.require_ssh_interact(live.session, interaction)
        written = os.write(live.master_fd, bytes(payload))
        self._touch(live)
        return written

    async def read(
        self,
        *,
        session_id: str,
        maximum_bytes: int = 65_536,
        timeout_seconds: float = 1.0,
    ) -> bytes:
        live = self._require_live(session_id)
        self.authorization_guard.require_ssh_connect(live.session)

        def read_ready() -> bytes:
            readable, _, _ = select.select([live.master_fd], [], [], timeout_seconds)
            if not readable:
                return b""
            try:
                return os.read(live.master_fd, maximum_bytes)
            except OSError:
                return b""

        payload = await asyncio.to_thread(read_ready)
        if payload and len(live.transcript) < self.transcript_limit_bytes:
            live.transcript.extend(
                payload[: self.transcript_limit_bytes - len(live.transcript)]
            )
            live.session = live.session.model_copy(
                update={"transcript_bytes": live.session.transcript_bytes + len(payload)}
            )
        self._touch(live)
        if live.process.poll() is not None and not payload:
            await self._close(session_id=session_id, disconnected=True)
        return payload

    async def resize(self, *, session_id: str, rows: int, columns: int) -> None:
        live = self._require_live(session_id)
        self.authorization_guard.require_ssh_connect(live.session)
        self._set_terminal_size(live.master_fd, rows=rows, columns=columns)
        live.session = live.session.model_copy(
            update={"terminal_rows": rows, "terminal_columns": columns}
        )
        self._touch(live)

    async def close(
        self,
        *,
        session_id: str,
        disconnected: bool = False,
    ) -> InteractiveSessionV1:
        """Close the PTY. Closing only ends access, so it needs no approval here;
        the agent's call itself is decided by the access decision hook."""

        return await self._close(session_id=session_id, disconnected=disconnected)

    async def _close(
        self,
        *,
        session_id: str,
        disconnected: bool,
    ) -> InteractiveSessionV1:
        live = self._sessions.get(session_id)
        if live is None:
            existing = self.repository.get_interactive_session(
                team_id=self.team_id,
                session_id=session_id,
            )
            if existing is None:
                raise SSHSessionUnavailable(f"SSH session {session_id!r} does not exist.")
            return existing
        self._sessions.pop(session_id)
        monitor = live.expiry_monitor
        current_task = asyncio.current_task()
        if monitor is not None and monitor is not current_task:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if live.process.poll() is None:
            os.killpg(live.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.to_thread(live.process.wait), timeout=5)
            except TimeoutError:
                os.killpg(live.process.pid, signal.SIGKILL)
                await asyncio.to_thread(live.process.wait)
        os.close(live.master_fd)
        live.material_context.__exit__(None, None, None)
        transcript_artifact = (
            self.artifact_store.put_bytes(
                kind=ArtifactKind.LOG,
                payload=bytes(live.transcript),
                media_type="application/vnd.ua.ssh-transcript",
            )
            if live.transcript
            else None
        )
        closed = live.session.model_copy(
            update={
                "status": (
                    InteractiveSessionStatus.DISCONNECTED
                    if disconnected
                    else InteractiveSessionStatus.CLOSED
                ),
                "transcript_artifact": transcript_artifact,
                "updated_at_utc": self.repository.current_time_utc(),
            }
        )
        return self.repository.update_claimed_interactive_session(
            closed,
            broker_id=self.broker_id,
            lease_token=live.lease_token,
            fence=closed.fence,
            release=True,
        )

    async def _monitor_expiry(self, session_id: str) -> None:
        interval = max(1.0, min(float(self.lease_ttl_seconds) / 3, 30.0))
        try:
            while True:
                await asyncio.sleep(interval)
                live = self._sessions.get(session_id)
                if live is None:
                    return
                expires_at = live.session.lease_expires_at_utc
                if expires_at is None or self.repository.current_time_utc() < expires_at:
                    continue
                self.logger.info(
                    "Closing idle SSH session after its broker lease expired",
                    extra={"session_id": session_id, "team_id": self.team_id},
                )
                await self._close(
                    session_id=session_id,
                    disconnected=True,
                )
                return
        except asyncio.CancelledError:
            return
        except Exception:
            self.logger.exception(
                "SSH session expiry monitor failed",
                extra={"session_id": session_id, "team_id": self.team_id},
            )
            if session_id in self._sessions:
                try:
                    await self._close(
                        session_id=session_id,
                        disconnected=True,
                    )
                except Exception:
                    self.logger.exception(
                        "Failed to close SSH session after expiry monitor failure",
                        extra={"session_id": session_id, "team_id": self.team_id},
                    )

    def recover_after_restart(self) -> tuple[InteractiveSessionV1, ...]:
        # Live PTYs cannot be reattached after broker restart. A separately
        # authorized remote persistent session can be opened as a new PTY.
        return self.repository.mark_interactive_sessions_lost(
            team_id=self.team_id,
            updated_at_utc=self.repository.current_time_utc(),
        )

    def _touch(self, live: _LiveSSHSession) -> None:
        database_now = self.repository.current_time_utc()
        live.session = live.session.model_copy(
            update={
                "lease_expires_at_utc": database_now
                + timedelta(seconds=self.lease_ttl_seconds),
                "updated_at_utc": database_now,
            }
        )
        live.session = self.repository.update_claimed_interactive_session(
            live.session,
            broker_id=self.broker_id,
            lease_token=live.lease_token,
            fence=live.session.fence,
        )

    def _require_live(self, session_id: str) -> _LiveSSHSession:
        live = self._sessions.get(session_id)
        if live is None:
            raise SSHSessionUnavailable(f"SSH session {session_id!r} is not live in this broker.")
        return live

    def _ssh_argv(
        self,
        session: InteractiveSessionV1,
        material: SSHCredentialMaterial,
    ) -> tuple[str, ...]:
        target = session.target
        remote_command = (
            (
                "tmux",
                "new-session",
                "-A",
                "-s",
                target.remote_persistent_session_name,
            )
            if target.remote_persistent_session_name
            else ()
        )
        return (
            self.ssh_executable,
            "-tt",
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={material.known_hosts_file}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "ProxyCommand=none",
            "-o",
            "CanonicalizeHostname=no",
            "-o",
            "CheckHostIP=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-i",
            str(material.identity_file),
            "-p",
            str(target.port),
            f"{target.username}@{target.host}",
            *remote_command,
        )

    @classmethod
    def _verify_material(
        cls,
        session: InteractiveSessionV1,
        material: SSHCredentialMaterial,
    ) -> None:
        cls._require_private_regular_file(material.identity_file)
        known_hosts = material.known_hosts_file
        file_stat = known_hosts.lstat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise PermissionError("SSH known-hosts material must be a regular file.")
        expected_host = (
            session.target.host
            if session.target.port == 22
            else f"[{session.target.host}]:{session.target.port}"
        )
        for raw_line in known_hosts.read_text(encoding="utf-8").splitlines():
            fields = raw_line.strip().split()
            if len(fields) < 3 or raw_line.lstrip().startswith("#"):
                continue
            hosts, algorithm, encoded_key = fields[:3]
            if expected_host not in hosts.split(","):
                continue
            try:
                key_bytes = base64.b64decode(encoded_key, validate=True)
            except (ValueError, binascii.Error):
                continue
            fingerprint = "SHA256:" + base64.b64encode(
                hashlib.sha256(key_bytes).digest()
            ).decode("ascii").rstrip("=")
            if (
                algorithm == session.target.host_key_algorithm
                and fingerprint == session.target.host_key_fingerprint
            ):
                return
        raise PermissionError("SSH known-hosts material does not match the pinned host key.")

    @staticmethod
    def _require_private_regular_file(path: Path) -> None:
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise PermissionError("SSH identity material must be a regular file, not a symlink.")
        if file_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError("SSH identity material must not be accessible by group or other.")

    @staticmethod
    def _set_terminal_size(fd: int, *, rows: int, columns: int) -> None:
        if rows < 1 or columns < 1:
            raise ValueError("Terminal rows and columns must be positive.")
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


__all__ = [
    "SSHCredentialMaterial",
    "SSHCredentialMaterialProvider",
    "ConfiguredSSHCredentialMaterialProvider",
    "SSHSessionBroker",
    "SSHSessionUnavailable",
]
