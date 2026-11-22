"""Interactive SSH PTY broker with exact targets and broker-owned key material."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable
from contextlib import AbstractContextManager
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
import threading
import time
from typing import TypeVar

from runtime_hosting.artifacts import ArtifactStore
from runtime_hosting.authorization import ExecutionAccessGuard
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    InteractiveSessionStatus,
    InteractiveSessionV1,
)
from runtime_hosting.ssh_credentials import (
    CatalogSSHCredentialMaterialProvider,
    CompositeSSHCredentialMaterialProvider,
    ConfiguredSSHCredentialMaterialProvider,
    SSHCredentialMaterial,
    SSHCredentialMaterialProvider,
    SessionSecretScrubber,
    StreamSecretScrubber,
    build_ssh_subprocess_env,
    sweep_stale_ssh_ephemeral_dirs,
)
from runtime_hosting.ssh_network import ValidatedSSHHost, ssh_host_key_alias
from storage.execution_control_repository import ExecutionControlRepository

_T = TypeVar("_T")


async def _await_shielded(
    awaitable: Awaitable[_T],
    *,
    reraise_on_caller_cancel: bool,
) -> _T:
    """Wait for ``awaitable`` despite caller cancellations, stopping if the inner task is cancelled.

    When only the caller task is cancelled, shielding lets cleanup run to
    completion (and optionally re-raises ``CancelledError`` afterwards). When
    the event loop shuts down (such as ``asyncio.run`` cancelling every task),
    the inner task itself ends cancelled; checking ``task.cancelled()`` breaks
    out immediately instead of spinning on an already-cancelled task.
    """

    task = asyncio.ensure_future(awaitable)
    caller_cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            caller_cancelled = True
    if caller_cancelled and reraise_on_caller_cancel:
        raise asyncio.CancelledError
    return result


@dataclass
class _LiveSSHSession:
    session: InteractiveSessionV1
    lease_token: str = field(repr=False)
    process: subprocess.Popen[bytes]
    master_fd: int
    material_context: AbstractContextManager[SSHCredentialMaterial]
    scrubber: SessionSecretScrubber = field(repr=False)
    stream_scrubber: StreamSecretScrubber = field(repr=False)
    raw_transcript: bytearray = field(default_factory=bytearray, repr=False)
    transcript_truncated: bool = False
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
    _sessions: dict[str, _LiveSSHSession] = field(default_factory=dict, init=False, repr=False)

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
        prepare_task: asyncio.Task[
            tuple[AbstractContextManager[SSHCredentialMaterial], SSHCredentialMaterial]
        ] | None = None
        handoff_lock = threading.Lock()
        prepare_abandoned = False
        prepared_ctx_ref: list[AbstractContextManager[SSHCredentialMaterial]] = []
        try:
            def _prepare_and_verify() -> tuple[
                AbstractContextManager[SSHCredentialMaterial],
                SSHCredentialMaterial,
            ]:
                prepared_ctx = self.material_provider.prepare(claimed)
                prepared_material = prepared_ctx.__enter__()
                try:
                    self._verify_material(claimed, prepared_material)
                except Exception:
                    prepared_ctx.__exit__(None, None, None)
                    raise
                with handoff_lock:
                    if prepare_abandoned:
                        prepared_ctx.__exit__(None, None, None)
                        raise asyncio.CancelledError("SSH open was cancelled during prepare.")
                    prepared_ctx_ref.append(prepared_ctx)
                return prepared_ctx, prepared_material

            prepare_task = asyncio.create_task(asyncio.to_thread(_prepare_and_verify))
            prepared_context, material = await asyncio.shield(prepare_task)
            context = prepared_context
            context_entered = True
            scrubber = SessionSecretScrubber(material.touched_secrets, logger=self.logger)
            stream_scrubber = scrubber.new_stream_scrubber()
            master_fd, slave_fd = pty.openpty()
            self._set_terminal_size(
                master_fd,
                rows=claimed.terminal_rows,
                columns=claimed.terminal_columns,
            )
            child_env = self._ssh_env(material)
            process = subprocess.Popen(
                self._ssh_argv(claimed, material),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(material.working_dir) if material.working_dir is not None else None,
                env=child_env,
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
                material_context=prepared_context,
                scrubber=scrubber,
                stream_scrubber=stream_scrubber,
            )
            self._sessions[session_id] = live
            live.expiry_monitor = asyncio.create_task(
                self._monitor_expiry(session_id),
                name=f"ssh-session-expiry:{session_id}",
            )
            return opened
        except BaseException:
            if not context_entered and prepare_task is not None:
                try:
                    prepared_context, _ = await _await_shielded(
                        prepare_task,
                        reraise_on_caller_cancel=False,
                    )
                    context = prepared_context
                    context_entered = True
                except BaseException:
                    with handoff_lock:
                        prepare_abandoned = True
                        if prepared_ctx_ref:
                            context = prepared_ctx_ref[0]
                            context_entered = True
            await _await_shielded(
                self._cleanup_failed_open(
                    claimed=claimed,
                    lease_token=lease_token,
                    process=process,
                    master_fd=master_fd,
                    slave_fd=slave_fd,
                    context=context if context_entered else None,
                ),
                reraise_on_caller_cancel=False,
            )
            raise

    async def _cleanup_failed_open(
        self,
        *,
        claimed: InteractiveSessionV1,
        lease_token: str,
        process: subprocess.Popen[bytes] | None,
        master_fd: int,
        slave_fd: int,
        context: AbstractContextManager[SSHCredentialMaterial] | None,
    ) -> None:
        try:
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
        finally:
            if master_fd >= 0:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            if slave_fd >= 0:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            if context is not None:
                try:
                    await asyncio.to_thread(context.__exit__, None, None, None)
                except Exception:
                    self.logger.exception(
                        "Failed to clean up SSH credential material after aborted open. "
                        "event=ssh_open_cleanup_failed session_id=%s",
                        claimed.session_id,
                    )
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

        def read_ready(wait_seconds: float) -> tuple[bytes, bool]:
            readable, _, _ = select.select([live.master_fd], [], [], max(0.0, wait_seconds))
            if not readable:
                return b"", False
            try:
                data = os.read(live.master_fd, maximum_bytes)
            except OSError:
                return b"", True
            return data, (len(data) == 0)

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        scrubbed_parts: list[bytes] = []
        received_any_payload = False
        fd_eof = False
        while True:
            quiet_wait = live.stream_scrubber.time_until_quiet_release()
            remaining = max(0.0, deadline - time.monotonic())
            if quiet_wait is None:
                wait_for = remaining
            elif quiet_wait == 0.0:
                wait_for = 0.0
            else:
                wait_for = min(remaining, quiet_wait + 0.002)
            payload, fd_eof = await asyncio.to_thread(read_ready, wait_for)
            if payload:
                received_any_payload = True
                remaining_cap = self.transcript_limit_bytes - len(live.raw_transcript)
                if remaining_cap > 0:
                    live.raw_transcript.extend(payload[:remaining_cap])
                if len(payload) > remaining_cap or len(live.raw_transcript) >= self.transcript_limit_bytes:
                    live.transcript_truncated = True
                fed = live.stream_scrubber.feed(payload)
                if fed:
                    scrubbed_parts.append(fed)
                    break
                if time.monotonic() >= deadline:
                    break
                continue
            released = live.stream_scrubber.release_quiet()
            if released:
                scrubbed_parts.append(released)
                break
            if fd_eof or live.process.poll() is not None or time.monotonic() >= deadline:
                break

        scrubbed = b"".join(scrubbed_parts)
        if scrubbed:
            live.session = live.session.model_copy(
                update={"transcript_bytes": live.session.transcript_bytes + len(scrubbed)}
            )
        self._touch(live)
        if live.process.poll() is not None and (not received_any_payload or (fd_eof and not scrubbed_parts)):
            flushed = live.stream_scrubber.flush()
            if flushed:
                live.session = live.session.model_copy(
                    update={"transcript_bytes": live.session.transcript_bytes + len(flushed)}
                )
            await self._close(session_id=session_id, disconnected=True)
            return scrubbed + flushed
        return scrubbed

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
        caller_task = asyncio.current_task()
        close_task = asyncio.create_task(
            self._finish_close(
                live=live,
                disconnected=disconnected,
                cancel_expiry_monitor=(live.expiry_monitor is not caller_task),
            )
        )
        return await _await_shielded(close_task, reraise_on_caller_cancel=True)

    async def _finish_close(
        self,
        *,
        live: _LiveSSHSession,
        disconnected: bool,
        cancel_expiry_monitor: bool = True,
    ) -> InteractiveSessionV1:
        monitor = live.expiry_monitor
        if cancel_expiry_monitor and monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        try:
            if live.process.poll() is None:
                try:
                    os.killpg(live.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(asyncio.to_thread(live.process.wait), timeout=5)
                except TimeoutError:
                    try:
                        os.killpg(live.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await asyncio.to_thread(live.process.wait)
        finally:
            try:
                os.close(live.master_fd)
            except OSError:
                pass
            try:
                await asyncio.to_thread(live.material_context.__exit__, None, None, None)
            except Exception:
                self.logger.exception(
                    "Failed to clean up SSH credential material on close. "
                    "event=ssh_close_material_cleanup_failed session_id=%s",
                    live.session.session_id,
                )
        # Scrub the entire stored transcript as a whole so read chunk boundaries
        # never leave split secrets or held tail bytes behind.
        live.stream_scrubber.flush()
        scrubbed_transcript = (
            live.scrubber.scrub_bytes(bytes(live.raw_transcript))
            if live.raw_transcript
            else b""
        )
        if live.transcript_truncated and scrubbed_transcript:
            scrubbed_transcript = live.scrubber.strip_trailing_secret_prefix(scrubbed_transcript)
        live.raw_transcript.clear()
        transcript_artifact = (
            self.artifact_store.put_bytes(
                kind=ArtifactKind.LOG,
                payload=scrubbed_transcript,
                media_type="application/vnd.ua.ssh-transcript",
            )
            if scrubbed_transcript
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
                "transcript_bytes": len(scrubbed_transcript),
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

    @staticmethod
    def _ssh_env(material: SSHCredentialMaterial) -> dict[str, str]:
        extra = {"SSH_AUTH_SOCK": material.ssh_auth_sock} if material.ssh_auth_sock else None
        return build_ssh_subprocess_env(extra=extra, include_term=True)

    def _ssh_argv(
        self,
        session: InteractiveSessionV1,
        material: SSHCredentialMaterial,
        *,
        validated_host: ValidatedSSHHost | None = None,
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
        if validated_host is not None:
            if validated_host.port != target.port:
                raise ValueError(
                    f"Validated SSH host port {validated_host.port} does not match target port {target.port}."
                )
            # OpenSSH applies the first value of an option. When pinning to a
            # validated IP with HostKeyAlias, disable CheckHostIP so ssh pins
            # strictly to the [host]:port entry without mutating known_hosts or
            # refusing on unrelated historical IP lines.
            host_pinning_args: tuple[str, ...] = (
                "-o",
                "CheckHostIP=no",
                "-o",
                f"HostName={validated_host.resolved_ip}",
                "-o",
                f"HostKeyAlias={validated_host.host_key_alias}",
            )
        else:
            host_pinning_args = (
                "-o",
                "CheckHostIP=yes",
            )
        identity_agent = "SSH_AUTH_SOCK" if material.ssh_auth_sock else "none"
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
            f"UserKnownHostsFile={material.argv_known_hosts_file()}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"IdentityAgent={identity_agent}",
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
            *host_pinning_args,
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-i",
            material.argv_identity_file(),
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
        identity_file = material.resolved_identity_file()
        cls._require_private_regular_file(identity_file)
        known_hosts = material.resolved_known_hosts_file()
        file_stat = known_hosts.lstat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise PermissionError("SSH known-hosts material must be a regular file.")
        expected_host = ssh_host_key_alias(session.target.host, session.target.port)
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
    "CatalogSSHCredentialMaterialProvider",
    "CompositeSSHCredentialMaterialProvider",
    "ConfiguredSSHCredentialMaterialProvider",
    "SSHCredentialMaterial",
    "SSHCredentialMaterialProvider",
    "SSHSessionBroker",
    "SSHSessionUnavailable",
    "SessionSecretScrubber",
    "StreamSecretScrubber",
    "sweep_stale_ssh_ephemeral_dirs",
]
