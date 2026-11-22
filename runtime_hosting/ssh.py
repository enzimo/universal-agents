"""Interactive and command-capable SSH broker with pinned host keys, live revocation, and scrubbing."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import fcntl
import hashlib
import logging
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import stat
import struct
import subprocess
import termios
import threading
import time
from typing import Any, TypeVar

from id_generation import new_id
from runtime_hosting.artifacts import ArtifactStore
from runtime_hosting.authorization import (
    ExecutionAccessGuard,
    ExecutionAuthorizationDenied,
    SSHCallerContext,
    SSHCapability,
)
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    EnrolledSSHTargetV1,
    InteractiveSessionStatus,
    InteractiveSessionV1,
    OwnerScopeV1,
    SSHInteractionRecordV1,
    SSHSecretRecipe,
    SSHTargetPlatform,
    ssh_public_key_sha256_fingerprint,
)
from runtime_hosting.ssh_credentials import (
    FORBIDDEN_EXTRA_SECRET_FIELDS,
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
from runtime_hosting.ssh_network import (
    LocalAddressChecker,
    SSHHostResolutionError,
    SSHHostResolver,
    SSHNetworkPolicy,
    ValidatedSSHHost,
    ssh_host_key_alias,
    validate_ssh_host,
)
from runtime_hosting.ssh_operations import (
    DEFAULT_COMMAND_OUTPUT_LIMIT_BYTES,
    DEFAULT_SUDO_SEARCH_PATHS,
    DEFAULT_TAILSCALE_SEARCH_PATHS,
    KEY_AUTH_VERIFY_MARKER,
    SUDO_AUTH_FAILED_MARKER,
    SUDO_NO_TIMESTAMP_MARKER,
    TAILSCALE_BIN_MISSING_MARKER,
    SSHBundleUploadResult,
    SSHCommandResult,
    SSHCommandStatus,
    SSHHostKeyProbeResult,
    SSHKeyEnrollResult,
    build_bundle_extract_command,
    build_key_install_command,
    build_key_verify_command,
    build_remote_shell_command,
    build_sudo_free_command,
    build_sudo_secret_recipe_command,
    build_tar_gz_bundle,
    probe_ssh_host_key,
    validate_remote_destination,
    validate_trusted_bin_paths,
)
from storage.execution_control_repository import (
    ExecutionControlRepository,
    StaleExecutionFence,
)

_T = TypeVar("_T")

_TERMINAL_SESSION_STATUSES = frozenset(
    {
        InteractiveSessionStatus.DISCONNECTED,
        InteractiveSessionStatus.CLOSED,
        InteractiveSessionStatus.LOST,
        InteractiveSessionStatus.FAILED,
        InteractiveSessionStatus.EXPIRED,
    }
)
_MAX_COMPLETED_COMMANDS = 200
_RECIPE_REQUIRED_FIELD: dict[SSHSecretRecipe, str] = {
    SSHSecretRecipe.TAILSCALE_UP: "tailscale_auth_key",
}


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


async def _kill_process_group(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = 2.0,
) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=max(0.1, grace_seconds))
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            process.kill()
        await asyncio.to_thread(process.wait)


def _write_private_copy(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


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


@dataclass
class _LiveSSHCommand:
    command_id: str
    session_id: str
    target_id: str | None
    owner_person_id: str | None
    started_at_utc: datetime
    scrubber: SessionSecretScrubber = field(repr=False)
    stdout_stream: StreamSecretScrubber = field(repr=False)
    stderr_stream: StreamSecretScrubber = field(repr=False)
    raw_stdout: bytearray = field(default_factory=bytearray, repr=False)
    raw_stderr: bytearray = field(default_factory=bytearray, repr=False)
    scrubbed_stdout: bytearray = field(default_factory=bytearray, repr=False)
    scrubbed_stderr: bytearray = field(default_factory=bytearray, repr=False)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    status: SSHCommandStatus = SSHCommandStatus.RUNNING
    exit_code: int | None = None
    finished_at_utc: datetime | None = None
    error: str | None = None
    process: subprocess.Popen[bytes] | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    revoked_event: asyncio.Event = field(default_factory=asyncio.Event)
    runner_task: asyncio.Task[None] | None = None

    def snapshot(self) -> SSHCommandResult:
        if self.status is SSHCommandStatus.RUNNING:
            stdout_bytes = bytes(self.scrubbed_stdout)
            stderr_bytes = bytes(self.scrubbed_stderr)
        else:
            stdout_bytes = bytes(self.scrubbed_stdout)
            stderr_bytes = bytes(self.scrubbed_stderr)
        return SSHCommandResult(
            command_id=self.command_id,
            session_id=self.session_id,
            status=self.status,
            exit_code=self.exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            stdout_truncated=self.stdout_truncated,
            stderr_truncated=self.stderr_truncated,
            started_at_utc=self.started_at_utc,
            finished_at_utc=self.finished_at_utc,
            error=self.error,
        )


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
    ssh_keyscan_executable: str = "ssh-keyscan"
    lease_ttl_seconds: int = 300
    transcript_limit_bytes: int = 4_194_304
    command_output_limit_bytes: int = DEFAULT_COMMAND_OUTPUT_LIMIT_BYTES
    network_policy: SSHNetworkPolicy | None = None
    resolver: SSHHostResolver | None = None
    local_address_checker: LocalAddressChecker | None = None
    allow_password_bootstrap: bool = False
    sudo_search_paths: tuple[str, ...] = DEFAULT_SUDO_SEARCH_PATHS
    tailscale_search_paths: tuple[str, ...] = DEFAULT_TAILSCALE_SEARCH_PATHS
    revocation_poll_interval_seconds: float = 1.0
    logger: logging.Logger = logging.getLogger(__name__)
    _sessions: dict[str, _LiveSSHSession] = field(default_factory=dict, init=False, repr=False)
    _commands: dict[str, _LiveSSHCommand] = field(default_factory=dict, init=False, repr=False)
    _session_commands: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False)

    # -- host key probing ---------------------------------------------------------

    def probe_host_key(
        self,
        *,
        host: str,
        port: int = 22,
        timeout_seconds: int = 10,
    ) -> SSHHostKeyProbeResult:
        """Probe an SSH host's ed25519 public key after enforcing operator network policy."""

        result = probe_ssh_host_key(
            host,
            port,
            network_policy=self.network_policy or SSHNetworkPolicy.default(),
            ssh_keyscan_executable=self.ssh_keyscan_executable,
            resolver=self.resolver,
            local_address_checker=self.local_address_checker,
            timeout_seconds=timeout_seconds,
        )
        self.logger.info(
            "Probed SSH host key. event=ssh_host_key_probed host=%s port=%s fingerprint=%s",
            result.host,
            result.port,
            result.host_key_fingerprint,
        )
        return result

    # -- authorization and network validation ------------------------------------

    def _authorize_session(
        self,
        session: InteractiveSessionV1,
        *,
        capability: SSHCapability,
        caller: SSHCallerContext,
        secret_recipe: SSHSecretRecipe | None = None,
    ) -> tuple[EnrolledSSHTargetV1 | None, SSHInteractionRecordV1 | None]:
        enrolled_target = (
            self.repository.get_ssh_target(
                team_id=self.team_id, target_id=session.target.target_id
            )
            if session.target.target_id is not None
            else None
        )
        session_revocation = self.repository.get_interactive_session_revocation(
            team_id=self.team_id, session_id=session.session_id
        )
        interaction = self.repository.get_ssh_interaction(
            team_id=self.team_id, session_id=session.session_id
        )
        self.authorization_guard.require_ssh_capability(
            session,
            interaction,
            capability=capability,
            caller=caller,
            enrolled_target=enrolled_target,
            session_revocation=session_revocation,
            allow_password_bootstrap_setting=self.allow_password_bootstrap,
            secret_recipe=secret_recipe,
        )
        return enrolled_target, interaction

    def _validate_target_network(self, session: InteractiveSessionV1) -> ValidatedSSHHost | None:
        if session.target.target_id is not None:
            policy = self.network_policy or SSHNetworkPolicy.default()
            return validate_ssh_host(
                session.target.host,
                session.target.port,
                policy,
                resolver=self.resolver,
                local_address_checker=self.local_address_checker,
            )
        if self.network_policy is None:
            return None
        try:
            return validate_ssh_host(
                session.target.host,
                session.target.port,
                self.network_policy,
                resolver=self.resolver,
                local_address_checker=self.local_address_checker,
            )
        except SSHHostResolutionError:
            if not self.network_policy.allowed_cidrs and not self.network_policy.allowed_domains:
                return None
            raise

    def _session_for_operation(self, session_id: str) -> InteractiveSessionV1:
        live = self._sessions.get(session_id)
        if live is not None:
            return live.session
        session = self.repository.get_interactive_session(
            team_id=self.team_id, session_id=session_id
        )
        if session is None:
            raise SSHSessionUnavailable(f"SSH session {session_id!r} does not exist.")
        if session.status in _TERMINAL_SESSION_STATUSES:
            raise SSHSessionUnavailable(
                f"SSH session {session_id!r} is {session.status.value} and cannot be used; create a new session."
            )
        return session

    def _remaining_delegation_seconds(
        self, interaction: SSHInteractionRecordV1 | None
    ) -> float | None:
        if interaction is None or interaction.activation.expires_at_utc is None:
            return None
        remaining = (
            interaction.activation.expires_at_utc - self.repository.current_time_utc()
        ).total_seconds()
        if remaining <= 0:
            raise ExecutionAuthorizationDenied("SSH session delegation has expired.")
        return remaining

    def _effective_timeout(
        self, requested_seconds: float, interaction: SSHInteractionRecordV1 | None
    ) -> float:
        bounded = max(1.0, min(float(requested_seconds), 3600.0))
        remaining = self._remaining_delegation_seconds(interaction)
        if remaining is not None:
            return max(0.1, min(bounded, remaining))
        return bounded

    def _prepare_material_context(
        self,
        session: InteractiveSessionV1,
        *,
        key_lifetime_seconds: int | None = None,
        scrubber: SessionSecretScrubber | None = None,
    ) -> AbstractContextManager[SSHCredentialMaterial]:
        if isinstance(
            self.material_provider,
            (CatalogSSHCredentialMaterialProvider, CompositeSSHCredentialMaterialProvider),
        ):
            return self.material_provider.prepare(
                session,
                key_lifetime_seconds=key_lifetime_seconds,
                scrubber=scrubber,
            )
        return self.material_provider.prepare(session)

    def _catalog_provider(self) -> CatalogSSHCredentialMaterialProvider:
        if isinstance(self.material_provider, CatalogSSHCredentialMaterialProvider):
            return self.material_provider
        if (
            isinstance(self.material_provider, CompositeSSHCredentialMaterialProvider)
            and self.material_provider.catalog_provider is not None
        ):
            return self.material_provider.catalog_provider
        raise PermissionError("Sealbox SSH credential provider is not enabled in this broker.")

    def _is_session_invalidated(
        self,
        session: InteractiveSessionV1,
        *,
        require_active_delegation: bool,
    ) -> str | None:
        if (
            self.repository.get_interactive_session_revocation(
                team_id=self.team_id, session_id=session.session_id
            )
            is not None
        ):
            return "session_revoked"
        if session.target.target_id is not None:
            enrolled = self.repository.get_ssh_target(
                team_id=self.team_id, target_id=session.target.target_id
            )
            if enrolled is None or not enrolled.is_usable(now=self.repository.current_time_utc()):
                return "target_revoked_or_expired"
        interaction = self.repository.get_ssh_interaction(
            team_id=self.team_id, session_id=session.session_id
        )
        if interaction is not None:
            if interaction.revoked_at_utc is not None:
                return "delegation_revoked"
            if not interaction.is_active(now=self.repository.current_time_utc()):
                return "delegation_expired"
        elif require_active_delegation:
            return "delegation_missing"
        return None

    # -- interactive PTY lifecycle -----------------------------------------------

    async def open(
        self,
        *,
        session_id: str,
        lease_token: str,
        caller: SSHCallerContext,
    ) -> InteractiveSessionV1:
        claimed = self.repository.claim_interactive_session(
            team_id=self.team_id,
            session_id=session_id,
            broker_id=self.broker_id,
            lease_token=lease_token,
            lease_ttl_seconds=self.lease_ttl_seconds,
        )
        try:
            _enrolled, interaction = self._authorize_session(
                claimed, capability=SSHCapability.CONNECT, caller=caller
            )
            validated_host = self._validate_target_network(claimed)
            open_key_lifetime: int | None = None
            if interaction is not None and interaction.activation.expires_at_utc is not None:
                remaining = self._remaining_delegation_seconds(interaction)
                if remaining is not None:
                    open_key_lifetime = max(15, int(remaining) + 10)
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
                prepared_ctx = self._prepare_material_context(
                    claimed, key_lifetime_seconds=open_key_lifetime
                )
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
                self._ssh_argv(claimed, material, validated_host=validated_host),
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
            if process is not None:
                await _kill_process_group(process, grace_seconds=2.0)
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
            try:
                self.repository.update_claimed_interactive_session(
                    failed,
                    broker_id=self.broker_id,
                    lease_token=lease_token,
                    fence=claimed.fence,
                    release=True,
                )
            except StaleExecutionFence:
                pass

    async def write(
        self,
        *,
        session_id: str,
        payload: bytes,
        caller: SSHCallerContext,
    ) -> int:
        """Type into the session: needs a person's approval for this session (trusted record)."""

        live = self._require_live(session_id)
        self._authorize_session(live.session, capability=SSHCapability.TYPING, caller=caller)
        written = os.write(live.master_fd, bytes(payload))
        await self._touch(live)
        return written

    async def read(
        self,
        *,
        session_id: str,
        maximum_bytes: int = 65_536,
        timeout_seconds: float = 1.0,
        caller: SSHCallerContext,
    ) -> bytes:
        live = self._require_live(session_id)
        self._authorize_session(live.session, capability=SSHCapability.CONNECT, caller=caller)

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
        await self._touch(live)
        if live.process.poll() is not None and (not received_any_payload or (fd_eof and not scrubbed_parts)):
            flushed = live.stream_scrubber.flush()
            if flushed:
                live.session = live.session.model_copy(
                    update={"transcript_bytes": live.session.transcript_bytes + len(flushed)}
                )
            await self._close(session_id=session_id, disconnected=True)
            return scrubbed + flushed
        return scrubbed

    async def resize(
        self,
        *,
        session_id: str,
        rows: int,
        columns: int,
        caller: SSHCallerContext,
    ) -> None:
        live = self._require_live(session_id)
        self._authorize_session(live.session, capability=SSHCapability.CONNECT, caller=caller)
        self._set_terminal_size(live.master_fd, rows=rows, columns=columns)
        live.session = live.session.model_copy(
            update={"terminal_rows": rows, "terminal_columns": columns}
        )
        await self._touch(live)

    # -- async commands, key bootstrap, sudo recipes, and bundles ----------------

    async def run_command(
        self,
        *,
        session_id: str,
        command: str,
        caller: SSHCallerContext,
        timeout_seconds: float = 120.0,
        wait_seconds: float = 5.0,
    ) -> SSHCommandResult:
        """Run a non-interactive remote command under ``allow_commands`` with async polling."""

        if not command or not command.strip():
            raise ValueError("SSH command must not be empty.")
        session = self._session_for_operation(session_id)
        enrolled, interaction = self._authorize_session(
            session, capability=SSHCapability.COMMAND, caller=caller
        )
        validated_host = self._validate_target_network(session)
        effective_timeout = self._effective_timeout(timeout_seconds, interaction)
        platform = enrolled.platform if enrolled is not None else (session.target.platform or SSHTargetPlatform.LINUX)
        remote_argv = build_remote_shell_command(command, platform)
        cmd = await self._start_tracked_command(
            session=session,
            caller=caller,
            remote_argv=remote_argv,
            validated_host=validated_host,
            stdin_payload=None,
            timeout_seconds=effective_timeout,
            extra_secrets=(),
        )
        return await self._wait_for_command(cmd, wait_seconds=wait_seconds)

    async def get_command(
        self,
        *,
        session_id: str,
        command_id: str,
        caller: SSHCallerContext,
        wait_seconds: float = 0.0,
    ) -> SSHCommandResult:
        """Poll a tracked remote command after re-checking caller and session authorization."""

        cmd = self._commands.get(command_id)
        if cmd is None or cmd.session_id != session_id:
            raise LookupError(f"SSH command {command_id!r} was not found for session {session_id!r}.")
        session = self.repository.get_interactive_session(
            team_id=self.team_id, session_id=session_id
        )
        if session is None:
            raise SSHSessionUnavailable(f"SSH session {session_id!r} does not exist.")
        if cmd.status is SSHCommandStatus.RUNNING:
            self._authorize_session(session, capability=SSHCapability.CONNECT, caller=caller)
        else:
            if cmd.owner_person_id is not None and caller.person_id != cmd.owner_person_id:
                raise ExecutionAuthorizationDenied("SSH command belongs to another person.")
        return await self._wait_for_command(cmd, wait_seconds=wait_seconds)

    async def enroll_ssh_key(
        self,
        *,
        session_id: str,
        caller: SSHCallerContext,
        timeout_seconds: float = 60.0,
    ) -> SSHKeyEnrollResult:
        """One-time password bootstrap: install Sealbox ``ssh_public_key``, verify key login, latch target."""

        live = self._sessions.get(session_id)
        temp_lease_token: str | None = None
        claimed: InteractiveSessionV1 | None = None
        if live is not None:
            session = live.session
        else:
            existing = self._session_for_operation(session_id)
            temp_lease_token = new_id("ssh_boot")
            claimed = self.repository.claim_interactive_session(
                team_id=self.team_id,
                session_id=existing.session_id,
                broker_id=self.broker_id,
                lease_token=temp_lease_token,
                lease_ttl_seconds=max(self.lease_ttl_seconds, int(timeout_seconds) + 30),
            )
            session = claimed

        enrolled_target: EnrolledSSHTargetV1 | None = None
        bootstrap_claimed = False
        latched = False
        try:
            enrolled_target, interaction = self._authorize_session(
                session, capability=SSHCapability.PASSWORD_BOOTSTRAP, caller=caller
            )
            assert enrolled_target is not None
            assert enrolled_target.bootstrap_credential_key is not None
            assert enrolled_target.bootstrap_credential_owner_scope is not None
            validated_host = self._validate_target_network(session)
            effective_timeout = self._effective_timeout(timeout_seconds, interaction)
            catalog_provider = self._catalog_provider()

            bootstrap_claimed = self.repository.claim_ssh_target_password_bootstrap(
                team_id=self.team_id,
                target_id=enrolled_target.target_id,
                session_id=session.session_id,
            )
            if not bootstrap_claimed:
                raise PermissionError(
                    "Password bootstrap is already in progress or has completed for this target."
                )

            scrubber = SessionSecretScrubber(logger=self.logger)
            key_fp, latched_record = await self._execute_key_bootstrap(
                session=session,
                enrolled_target=enrolled_target,
                validated_host=validated_host,
                catalog_provider=catalog_provider,
                scrubber=scrubber,
                timeout_seconds=effective_timeout,
            )
            latched = True
            self.logger.info(
                "Completed one-time SSH key bootstrap and permanently latched target. "
                "event=ssh_key_bootstrap_completed session_id=%s target_id=%s platform=%s",
                session.session_id,
                enrolled_target.target_id,
                enrolled_target.platform.value,
            )
            assert latched_record.password_bootstrap_completed_at_utc is not None
            return SSHKeyEnrollResult(
                session_id=session.session_id,
                target_id=enrolled_target.target_id,
                host=enrolled_target.host,
                port=enrolled_target.port,
                username=enrolled_target.username,
                platform=enrolled_target.platform,
                public_key_fingerprint=key_fp,
                verified_key_login=True,
                password_bootstrap_latched_at_utc=latched_record.password_bootstrap_completed_at_utc,
            )
        finally:
            if bootstrap_claimed and not latched and enrolled_target is not None:
                self.repository.release_ssh_target_password_bootstrap_claim(
                    team_id=self.team_id,
                    target_id=enrolled_target.target_id,
                    session_id=session.session_id,
                )
            if claimed is not None and temp_lease_token is not None:
                try:
                    self.repository.update_claimed_interactive_session(
                        claimed,
                        broker_id=self.broker_id,
                        lease_token=temp_lease_token,
                        fence=claimed.fence,
                        release=True,
                    )
                except StaleExecutionFence:
                    pass

    async def _run_cancellable_ssh_step(
        self,
        *,
        session: InteractiveSessionV1,
        argv: tuple[str, ...],
        env: dict[str, str],
        cwd: Path | None,
        stdin_payload: bytes | None,
        timeout_seconds: float,
        scrubber: SessionSecretScrubber,
    ) -> tuple[int, str, str]:
        """Run one broker SSH subprocess that can be terminated immediately on revocation."""

        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        comm_task = asyncio.create_task(asyncio.to_thread(process.communicate, stdin_payload))
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        poll_interval = max(0.05, float(self.revocation_poll_interval_seconds))
        try:
            while not comm_task.done():
                invalidated = self._is_session_invalidated(
                    session, require_active_delegation=True
                )
                if invalidated is not None:
                    await _kill_process_group(process, grace_seconds=1.0)
                    await asyncio.gather(comm_task, return_exceptions=True)
                    raise ExecutionAuthorizationDenied(
                        f"SSH session or delegation became inactive during bootstrap ({invalidated})."
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await _kill_process_group(process, grace_seconds=1.0)
                    await asyncio.gather(comm_task, return_exceptions=True)
                    raise TimeoutError(f"SSH bootstrap step timed out after {timeout_seconds:.1f}s.")
                await asyncio.wait({comm_task}, timeout=min(poll_interval, remaining))
            raw_out, raw_err = await comm_task
            out_text = scrubber.scrub_bytes(raw_out or b"").decode("utf-8", errors="replace")
            err_text = scrubber.scrub_bytes(raw_err or b"").decode("utf-8", errors="replace")
            return int(process.returncode or 0), out_text, err_text
        except BaseException:
            if process.poll() is None:
                await _kill_process_group(process, grace_seconds=1.0)
            if not comm_task.done():
                await asyncio.gather(comm_task, return_exceptions=True)
            raise

    async def _execute_key_bootstrap(
        self,
        *,
        session: InteractiveSessionV1,
        enrolled_target: EnrolledSSHTargetV1,
        validated_host: ValidatedSSHHost | None,
        catalog_provider: CatalogSSHCredentialMaterialProvider,
        scrubber: SessionSecretScrubber,
        timeout_seconds: float,
    ) -> tuple[str, EnrolledSSHTargetV1]:
        assert enrolled_target.bootstrap_credential_key is not None
        assert enrolled_target.bootstrap_credential_owner_scope is not None
        mat_ctx = catalog_provider.prepare(
            session,
            key_lifetime_seconds=max(15, int(timeout_seconds) + 10),
            scrubber=scrubber,
        )
        material = await asyncio.to_thread(mat_ctx.__enter__)
        try:
            self._verify_material(session, material)
            if not material.public_key:
                raise PermissionError("Resolved SSH key credential has no public key to enroll.")
            _algo, key_fp = ssh_public_key_sha256_fingerprint(material.public_key)
            install_cmd = build_key_install_command(enrolled_target.platform)
            askpass_ctx = catalog_provider.prepare_password_askpass(
                owner=enrolled_target.bootstrap_credential_owner_scope,
                credential_key=enrolled_target.bootstrap_credential_key,
                scrubber=scrubber,
            )
            askpass = await asyncio.to_thread(askpass_ctx.__enter__)
            try:
                assert askpass.working_dir is not None
                known_hosts_copy = askpass.working_dir / "known_hosts"
                _write_private_copy(
                    known_hosts_copy,
                    material.resolved_known_hosts_file().read_bytes(),
                )
                install_argv = self._ssh_password_bootstrap_argv(
                    session=session,
                    known_hosts_argv="./known_hosts",
                    validated_host=validated_host,
                    remote_command=install_cmd,
                )
                install_env = build_ssh_subprocess_env(extra=askpass.env)
                rc, out_text, err_text = await self._run_cancellable_ssh_step(
                    session=session,
                    argv=install_argv,
                    env=install_env,
                    cwd=askpass.working_dir,
                    stdin_payload=f"{material.public_key}\n".encode("utf-8"),
                    timeout_seconds=timeout_seconds,
                    scrubber=scrubber,
                )
                if rc != 0:
                    detail = (err_text or out_text).strip()
                    raise RuntimeError(
                        f"SSH password key installation failed (exit {rc})"
                        + (f": {detail}" if detail else ".")
                    )
            finally:
                await asyncio.to_thread(askpass_ctx.__exit__, None, None, None)

            invalidated = self._is_session_invalidated(session, require_active_delegation=True)
            if invalidated is not None:
                raise ExecutionAuthorizationDenied(
                    f"SSH session or delegation became inactive during bootstrap ({invalidated})."
                )

            verify_cmd = build_key_verify_command(enrolled_target.platform)
            verify_argv = self._ssh_command_argv(
                session,
                material,
                validated_host=validated_host,
                remote_command=verify_cmd,
            )
            verify_rc, verify_stdout, verify_stderr = await self._run_cancellable_ssh_step(
                session=session,
                argv=verify_argv,
                env=self._ssh_env(material),
                cwd=material.working_dir,
                stdin_payload=None,
                timeout_seconds=timeout_seconds,
                scrubber=scrubber,
            )
            if verify_rc != 0 or KEY_AUTH_VERIFY_MARKER not in verify_stdout:
                detail = (verify_stderr or verify_stdout).strip()
                raise RuntimeError(
                    f"Key-only SSH verification failed after key install (exit {verify_rc})"
                    + (f": {detail}" if detail else ".")
                )
        finally:
            await asyncio.to_thread(mat_ctx.__exit__, None, None, None)

        latched_record = self.repository.mark_ssh_target_password_bootstrap_completed(
            team_id=self.team_id,
            target_id=enrolled_target.target_id,
        )
        if latched_record is None:
            raise RuntimeError("Enrolled SSH target disappeared before bootstrap latch could be saved.")
        return key_fp, latched_record

    async def run_sudo(
        self,
        *,
        session_id: str,
        caller: SSHCallerContext,
        command: str | None = None,
        secret_recipe: SSHSecretRecipe | str | None = None,
        recipe_params: Mapping[str, Any] | None = None,
        extra_secret_field: str | None = None,
        timeout_seconds: float = 120.0,
        wait_seconds: float = 5.0,
    ) -> SSHCommandResult:
        """Run a sudo command or approved broker secret recipe with separated password handling."""

        normalized_cmd = str(command or "").strip() or None
        normalized_extra = str(extra_secret_field or "").strip() or None
        parsed_recipe = SSHSecretRecipe(secret_recipe) if secret_recipe is not None else None

        if normalized_extra is not None:
            if normalized_extra in FORBIDDEN_EXTRA_SECRET_FIELDS:
                raise PermissionError(
                    f"Credential field {normalized_extra!r} is permanently forbidden as extra_secret_field."
                )
            if parsed_recipe is None:
                raise PermissionError(
                    "Sealbox secrets are never piped to arbitrary agent-written commands. "
                    "Use an admin-approved broker secret_recipe (for example 'tailscale_up') instead."
                )
            expected_field = _RECIPE_REQUIRED_FIELD[parsed_recipe]
            if normalized_extra != expected_field:
                raise ValueError(
                    f"Secret recipe {parsed_recipe.value!r} requires field {expected_field!r}, not {normalized_extra!r}."
                )

        if parsed_recipe is not None:
            if normalized_cmd is not None:
                raise ValueError(
                    "Do not pass free command text together with a broker secret_recipe."
                )
        elif normalized_cmd is None:
            raise ValueError("run_sudo requires either a command or an approved secret_recipe.")

        session = self._session_for_operation(session_id)
        enrolled_target, interaction = self._authorize_session(
            session,
            capability=SSHCapability.BROKER_SUDO,
            caller=caller,
            secret_recipe=parsed_recipe,
        )
        assert enrolled_target is not None
        assert enrolled_target.bootstrap_credential_key is not None
        assert enrolled_target.bootstrap_credential_owner_scope is not None
        validated_host = self._validate_target_network(session)
        effective_timeout = self._effective_timeout(timeout_seconds, interaction)
        catalog_provider = self._catalog_provider()

        # Preflight: locate trusted absolute sudo binary and check NOPASSWD without touching secrets.
        sudo_path, nopasswd = await asyncio.to_thread(
            self._probe_remote_sudo_sync,
            session=session,
            validated_host=validated_host,
            timeout_seconds=min(30.0, effective_timeout),
        )

        stdin_lines: list[str] = []
        extra_secrets: list[str] = []
        boot_owner: OwnerScopeV1 = enrolled_target.bootstrap_credential_owner_scope
        boot_key: str = enrolled_target.bootstrap_credential_key
        needs_password = not nopasswd
        if needs_password:
            password = catalog_provider.resolve_admin_password(
                owner=boot_owner,
                credential_key=boot_key,
            )
            stdin_lines.append(password)
            extra_secrets.append(password)

        if parsed_recipe is not None:
            secret_field = _RECIPE_REQUIRED_FIELD[parsed_recipe]
            recipe_secret = catalog_provider.resolve_named_secret(
                owner=boot_owner,
                credential_key=boot_key,
                field_name=secret_field,
            )
            stdin_lines.append(recipe_secret)
            extra_secrets.append(recipe_secret)
            remote_argv = build_sudo_secret_recipe_command(
                recipe=parsed_recipe,
                sudo_path=sudo_path,
                needs_password=needs_password,
                recipe_params=recipe_params,
                tailscale_search_paths=self.tailscale_search_paths,
            )
        else:
            assert normalized_cmd is not None
            remote_argv = build_sudo_free_command(
                sudo_path,
                normalized_cmd,
                needs_password=needs_password,
            )

        stdin_payload = ("\n".join(stdin_lines) + "\n").encode("utf-8") if stdin_lines else None
        cmd = await self._start_tracked_command(
            session=session,
            caller=caller,
            remote_argv=remote_argv,
            validated_host=validated_host,
            stdin_payload=stdin_payload,
            timeout_seconds=effective_timeout,
            extra_secrets=tuple(extra_secrets),
        )
        return await self._wait_for_command(cmd, wait_seconds=wait_seconds)

    def _probe_remote_sudo_sync(
        self,
        *,
        session: InteractiveSessionV1,
        validated_host: ValidatedSSHHost | None,
        timeout_seconds: float,
    ) -> tuple[str, bool]:
        paths = validate_trusted_bin_paths(self.sudo_search_paths, label="sudo")
        quoted = " ".join(shlex.quote(path) for path in paths)
        probe_script = (
            f"for _ua_p in {quoted}; do "
            "if [ -x \"$_ua_p\" ]; then "
            "printf 'UA_SUDO_BIN:%s\\n' \"$_ua_p\"; "
            "if \"$_ua_p\" -n true </dev/null >/dev/null 2>&1; then "
            "printf 'UA_SUDO_NOPASSWD:1\\n'; else printf 'UA_SUDO_NOPASSWD:0\\n'; fi; "
            "exit 0; fi; done; exit 127"
        )
        with self._prepare_material_context(
            session,
            key_lifetime_seconds=max(15, int(timeout_seconds) + 10),
        ) as material:
            self._verify_material(session, material)
            scrubber = SessionSecretScrubber(material.touched_secrets, logger=self.logger)
            argv = self._ssh_command_argv(
                session,
                material,
                validated_host=validated_host,
                remote_command=(f"sh -c {shlex.quote(probe_script)}",),
            )
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                cwd=str(material.working_dir) if material.working_dir is not None else None,
                env=self._ssh_env(material),
                timeout=timeout_seconds,
                check=False,
            )
            stdout = scrubber.scrub_bytes(completed.stdout).decode("utf-8", errors="replace")
            stderr = scrubber.scrub_bytes(completed.stderr).decode("utf-8", errors="replace").strip()
            if completed.returncode != 0:
                raise RuntimeError(
                    "Could not find sudo at a trusted path on the remote host"
                    + (f": {stderr}" if stderr else ".")
                )
            sudo_path = ""
            nopasswd = False
            for line in stdout.splitlines():
                if line.startswith("UA_SUDO_BIN:"):
                    sudo_path = line.split(":", 1)[1].strip()
                elif line.startswith("UA_SUDO_NOPASSWD:"):
                    nopasswd = line.split(":", 1)[1].strip() == "1"
            if sudo_path not in paths:
                raise RuntimeError("Remote sudo probe returned an untrusted binary path.")
            return sudo_path, nopasswd

    async def upload_bundle(
        self,
        *,
        session_id: str,
        entries: Sequence[tuple[str, bytes]],
        remote_destination: str,
        caller: SSHCallerContext,
        timeout_seconds: float = 120.0,
    ) -> SSHBundleUploadResult:
        """Stream a verified non-secret workspace tar.gz archive over ``ssh -T``."""

        session = self._session_for_operation(session_id)
        enrolled, interaction = self._authorize_session(
            session, capability=SSHCapability.WORKSPACE_TRANSFER, caller=caller
        )
        validated_host = self._validate_target_network(session)
        cleaned_dest = validate_remote_destination(remote_destination)
        archive_bytes, total_file_bytes = build_tar_gz_bundle(entries)
        effective_timeout = self._effective_timeout(timeout_seconds, interaction)
        platform = enrolled.platform if enrolled is not None else (session.target.platform or SSHTargetPlatform.LINUX)
        extract_argv = build_bundle_extract_command(cleaned_dest, platform)
        cmd = await self._start_tracked_command(
            session=session,
            caller=caller,
            remote_argv=extract_argv,
            validated_host=validated_host,
            stdin_payload=archive_bytes,
            timeout_seconds=effective_timeout,
            extra_secrets=(),
        )
        result = await self._wait_for_command(cmd, wait_seconds=effective_timeout + 5.0)
        if result.status == SSHCommandStatus.REVOKED:
            raise ExecutionAuthorizationDenied(
                result.error or "Workspace bundle upload was stopped because the session was revoked."
            )
        if result.status == SSHCommandStatus.TIMED_OUT:
            raise TimeoutError(result.error or "Workspace bundle upload timed out.")
        if result.status != SSHCommandStatus.COMPLETED:
            detail = result.error or result.stderr.strip() or f"exit {result.exit_code}"
            raise RuntimeError(f"Workspace bundle upload failed: {detail}")
        self.logger.info(
            "Uploaded workspace bundle over SSH. event=ssh_bundle_uploaded "
            "session_id=%s files=%d total_bytes=%d archive_bytes=%d",
            session_id,
            len(entries),
            total_file_bytes,
            len(archive_bytes),
        )
        return SSHBundleUploadResult(
            session_id=session_id,
            remote_destination=cleaned_dest,
            file_count=len(entries),
            archive_bytes=len(archive_bytes),
            total_file_bytes=total_file_bytes,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    # -- tracked remote command execution ----------------------------------------

    async def _start_tracked_command(
        self,
        *,
        session: InteractiveSessionV1,
        caller: SSHCallerContext,
        remote_argv: tuple[str, ...],
        validated_host: ValidatedSSHHost | None,
        stdin_payload: bytes | None,
        timeout_seconds: float,
        extra_secrets: tuple[str, ...],
    ) -> _LiveSSHCommand:
        scrubber = SessionSecretScrubber(extra_secrets, logger=self.logger)
        command_id = new_id("ssh_cmd")
        cmd = _LiveSSHCommand(
            command_id=command_id,
            session_id=session.session_id,
            target_id=session.target.target_id,
            owner_person_id=caller.person_id,
            started_at_utc=self.repository.current_time_utc(),
            scrubber=scrubber,
            stdout_stream=scrubber.new_stream_scrubber(),
            stderr_stream=scrubber.new_stream_scrubber(),
        )
        self._prune_completed_commands()
        self._commands[command_id] = cmd
        self._session_commands.setdefault(session.session_id, set()).add(command_id)
        cmd.runner_task = asyncio.create_task(
            self._run_tracked_command(
                cmd=cmd,
                session=session,
                remote_argv=remote_argv,
                validated_host=validated_host,
                stdin_payload=stdin_payload,
                timeout_seconds=timeout_seconds,
            ),
            name=f"ssh-command:{command_id}",
        )
        self.logger.info(
            "Started SSH command. event=ssh_command_started session_id=%s command_id=%s agent_type=%s",
            session.session_id,
            command_id,
            caller.agent_type,
        )
        return cmd

    async def _wait_for_command(
        self, cmd: _LiveSSHCommand, *, wait_seconds: float
    ) -> SSHCommandResult:
        wait = max(0.0, float(wait_seconds))
        if wait > 0 and not cmd.done_event.is_set():
            try:
                await asyncio.wait_for(cmd.done_event.wait(), timeout=wait)
            except TimeoutError:
                pass
        return cmd.snapshot()

    async def _run_tracked_command(
        self,
        *,
        cmd: _LiveSSHCommand,
        session: InteractiveSessionV1,
        remote_argv: tuple[str, ...],
        validated_host: ValidatedSSHHost | None,
        stdin_payload: bytes | None,
        timeout_seconds: float,
    ) -> None:
        material_context: AbstractContextManager[SSHCredentialMaterial] | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            def _prepare() -> tuple[
                AbstractContextManager[SSHCredentialMaterial], SSHCredentialMaterial
            ]:
                ctx = self._prepare_material_context(
                    session,
                    key_lifetime_seconds=max(15, int(timeout_seconds) + 10),
                    scrubber=cmd.scrubber,
                )
                mat = ctx.__enter__()
                try:
                    self._verify_material(session, mat)
                except Exception:
                    ctx.__exit__(None, None, None)
                    raise
                return ctx, mat

            material_context, material = await asyncio.to_thread(_prepare)
            cmd.scrubber.register_many(material.touched_secrets)
            argv = self._ssh_command_argv(
                session,
                material,
                validated_host=validated_host,
                remote_command=remote_argv,
            )
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(material.working_dir) if material.working_dir is not None else None,
                env=self._ssh_env(material),
                close_fds=True,
                start_new_session=True,
            )
            cmd.process = process
            if stdin_payload is not None and process.stdin is not None:
                def _write_stdin(stream: Any, data: bytes) -> None:
                    try:
                        stream.write(data)
                        stream.flush()
                    except BrokenPipeError:
                        pass
                    except OSError:
                        pass
                    finally:
                        try:
                            stream.close()
                        except OSError:
                            pass

                stdin_task: asyncio.Task[None] | None = asyncio.create_task(
                    asyncio.to_thread(_write_stdin, process.stdin, stdin_payload)
                )
            else:
                stdin_task = None

            assert process.stdout is not None and process.stderr is not None
            stdout_task = asyncio.create_task(
                asyncio.to_thread(self._pump_command_stream, process.stdout, cmd, True)
            )
            stderr_task = asyncio.create_task(
                asyncio.to_thread(self._pump_command_stream, process.stderr, cmd, False)
            )
            wait_task = asyncio.create_task(asyncio.to_thread(process.wait))

            deadline = time.monotonic() + max(0.1, timeout_seconds)
            poll_interval = max(0.05, float(self.revocation_poll_interval_seconds))
            while not wait_task.done():
                if cmd.revoked_event.is_set():
                    await _kill_process_group(process, grace_seconds=1.0)
                    cmd.status = SSHCommandStatus.REVOKED
                    cmd.error = "SSH command was terminated because the session or target was revoked."
                    break
                invalidated = self._is_session_invalidated(
                    session, require_active_delegation=True
                )
                if invalidated is not None:
                    await _kill_process_group(process, grace_seconds=1.0)
                    cmd.status = SSHCommandStatus.REVOKED
                    cmd.error = (
                        f"SSH command was terminated because the session or delegation became inactive ({invalidated})."
                    )
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await _kill_process_group(process, grace_seconds=1.0)
                    cmd.status = SSHCommandStatus.TIMED_OUT
                    cmd.error = f"SSH command timed out after {timeout_seconds:.1f}s."
                    break
                revoke_waiter = asyncio.create_task(cmd.revoked_event.wait())
                try:
                    await asyncio.wait(
                        {wait_task, revoke_waiter},
                        timeout=min(poll_interval, remaining),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not revoke_waiter.done():
                        revoke_waiter.cancel()
                        await asyncio.gather(revoke_waiter, return_exceptions=True)

            if stdin_task is not None:
                await asyncio.gather(stdin_task, return_exceptions=True)
            await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)
            cmd.exit_code = process.returncode
            self._finalize_command_buffers(cmd)
            if cmd.status is SSHCommandStatus.RUNNING:
                self._classify_command_outcome(cmd)
            cmd.finished_at_utc = self.repository.current_time_utc()
            self.logger.info(
                "Finished SSH command. event=%s session_id=%s command_id=%s status=%s exit_code=%s",
                "ssh_command_revoked" if cmd.status is SSHCommandStatus.REVOKED else "ssh_command_finished",
                cmd.session_id,
                cmd.command_id,
                cmd.status.value,
                cmd.exit_code,
            )
        except Exception as exc:
            if process is not None and process.poll() is None:
                await _kill_process_group(process, grace_seconds=1.0)
            self._finalize_command_buffers(cmd)
            cmd.status = SSHCommandStatus.FAILED
            cmd.error = cmd.scrubber.scrub_text(str(exc))
            cmd.finished_at_utc = self.repository.current_time_utc()
        finally:
            cmd.process = None
            if material_context is not None:
                try:
                    await asyncio.to_thread(material_context.__exit__, None, None, None)
                except Exception:
                    self.logger.exception(
                        "Failed to clean up SSH credential material after command. "
                        "event=ssh_command_cleanup_failed session_id=%s command_id=%s",
                        cmd.session_id,
                        cmd.command_id,
                    )
            cmd.done_event.set()

    def _pump_command_stream(
        self, stream: Any, cmd: _LiveSSHCommand, is_stdout: bool
    ) -> None:
        raw_buf = cmd.raw_stdout if is_stdout else cmd.raw_stderr
        scrubbed_buf = cmd.scrubbed_stdout if is_stdout else cmd.scrubbed_stderr
        stream_scrubber = cmd.stdout_stream if is_stdout else cmd.stderr_stream
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                remaining_cap = self.command_output_limit_bytes - len(raw_buf)
                if remaining_cap > 0:
                    raw_buf.extend(chunk[:remaining_cap])
                if len(chunk) > remaining_cap or len(raw_buf) >= self.command_output_limit_bytes:
                    if is_stdout:
                        cmd.stdout_truncated = True
                    else:
                        cmd.stderr_truncated = True
                emitted = stream_scrubber.feed(chunk)
                if emitted:
                    scrub_cap = self.command_output_limit_bytes - len(scrubbed_buf)
                    if scrub_cap > 0:
                        scrubbed_buf.extend(emitted[:scrub_cap])
        finally:
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _finalize_command_buffers(cmd: _LiveSSHCommand) -> None:
        cmd.stdout_stream.flush()
        cmd.stderr_stream.flush()
        final_out = cmd.scrubber.scrub_bytes(bytes(cmd.raw_stdout)) if cmd.raw_stdout else b""
        if cmd.stdout_truncated and final_out:
            final_out = cmd.scrubber.strip_trailing_secret_prefix(final_out)
        final_err = cmd.scrubber.scrub_bytes(bytes(cmd.raw_stderr)) if cmd.raw_stderr else b""
        if cmd.stderr_truncated and final_err:
            final_err = cmd.scrubber.strip_trailing_secret_prefix(final_err)
        cmd.scrubbed_stdout[:] = final_out
        cmd.scrubbed_stderr[:] = final_err
        cmd.raw_stdout.clear()
        cmd.raw_stderr.clear()

    @staticmethod
    def _classify_command_outcome(cmd: _LiveSSHCommand) -> None:
        stderr_text = bytes(cmd.scrubbed_stderr).decode("utf-8", errors="replace")
        if SUDO_AUTH_FAILED_MARKER in stderr_text:
            cmd.status = SSHCommandStatus.FAILED
            cmd.error = (
                "Sudo authentication failed for the stored admin_password; "
                "verify or rotate the bootstrap credential."
            )
            return
        if SUDO_NO_TIMESTAMP_MARKER in stderr_text:
            cmd.status = SSHCommandStatus.FAILED
            cmd.error = (
                "Remote host does not keep a sudo authentication timestamp across commands "
                "(for example timestamp_timeout=0); broker sudo refuses to share stdin between sudo and the command."
            )
            return
        if TAILSCALE_BIN_MISSING_MARKER in stderr_text:
            cmd.status = SSHCommandStatus.FAILED
            cmd.error = "Could not find the tailscale binary in trusted paths on the remote host."
            return
        if cmd.exit_code == 0:
            cmd.status = SSHCommandStatus.COMPLETED
        else:
            cmd.status = SSHCommandStatus.FAILED

    def _prune_completed_commands(self) -> None:
        completed = [
            cmd_id
            for cmd_id, item in self._commands.items()
            if item.status is not SSHCommandStatus.RUNNING
        ]
        if len(completed) <= _MAX_COMPLETED_COMMANDS:
            return
        to_remove = completed[: len(completed) - _MAX_COMPLETED_COMMANDS]
        for cmd_id in to_remove:
            removed = self._commands.pop(cmd_id, None)
            if removed is not None:
                bucket = self._session_commands.get(removed.session_id)
                if bucket is not None:
                    bucket.discard(cmd_id)
                    if not bucket:
                        self._session_commands.pop(removed.session_id, None)

    async def _terminate_session_commands(self, session_id: str) -> int:
        cmd_ids = tuple(self._session_commands.get(session_id, ()))
        tasks: list[asyncio.Task[None]] = []
        terminated = 0
        for cmd_id in cmd_ids:
            cmd = self._commands.get(cmd_id)
            if cmd is None or cmd.status is not SSHCommandStatus.RUNNING:
                continue
            terminated += 1
            cmd.revoked_event.set()
            if cmd.runner_task is not None and not cmd.runner_task.done():
                tasks.append(cmd.runner_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return terminated

    # -- close and live revocation -----------------------------------------------

    async def close(
        self,
        *,
        session_id: str,
        disconnected: bool = False,
    ) -> InteractiveSessionV1:
        """Close the PTY and stop any running commands for this session."""

        return await self._close(session_id=session_id, disconnected=disconnected)

    async def revoke_session(
        self,
        *,
        session_id: str,
        revoked_by: str = "administrator",
    ) -> InteractiveSessionV1 | None:
        """Revoke one SSH session immediately: persist revocation, kill PTY and commands, save scrubbed transcript."""

        self.repository.revoke_interactive_session(
            team_id=self.team_id,
            session_id=session_id,
            revoked_by=revoked_by,
        )
        terminated_cmds = await self._terminate_session_commands(session_id)
        closed: InteractiveSessionV1 | None = None
        if session_id in self._sessions:
            closed = await self._close(session_id=session_id, disconnected=True)
        else:
            closed = self.repository.get_interactive_session(
                team_id=self.team_id, session_id=session_id
            )
        self.logger.info(
            "Revoked SSH session. event=ssh_session_revoked session_id=%s revoked_by=%s terminated_commands=%d",
            session_id,
            revoked_by,
            terminated_cmds,
        )
        return closed

    async def revoke_target(
        self,
        *,
        target_id: str,
        revoked_by: str = "administrator",
    ) -> EnrolledSSHTargetV1 | None:
        """Revoke an enrolled SSH target and immediately terminate every live session and command using it."""

        revoked = self.repository.revoke_ssh_target(
            team_id=self.team_id,
            target_id=target_id,
            revoked_by=revoked_by,
        )
        affected_sessions: set[str] = {
            sid
            for sid, live in self._sessions.items()
            if live.session.target.target_id == target_id
        }
        for cmd in list(self._commands.values()):
            if cmd.target_id == target_id and cmd.status is SSHCommandStatus.RUNNING:
                affected_sessions.add(cmd.session_id)
        terminated_cmds = 0
        for sid in sorted(affected_sessions):
            terminated_cmds += await self._terminate_session_commands(sid)
            if sid in self._sessions:
                await self._close(session_id=sid, disconnected=True)
        self.logger.info(
            "Revoked SSH target. event=ssh_target_revoked target_id=%s revoked_by=%s "
            "terminated_sessions=%d terminated_commands=%d",
            target_id,
            revoked_by,
            len(affected_sessions),
            terminated_cmds,
        )
        return revoked

    async def _close(
        self,
        *,
        session_id: str,
        disconnected: bool,
    ) -> InteractiveSessionV1:
        await self._terminate_session_commands(session_id)
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
            await _kill_process_group(live.process, grace_seconds=5.0)
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
        interval = max(
            0.05,
            min(float(self.revocation_poll_interval_seconds), max(0.05, float(self.lease_ttl_seconds) / 3)),
        )
        try:
            while True:
                await asyncio.sleep(interval)
                live = self._sessions.get(session_id)
                if live is None:
                    return
                invalidated = self._is_session_invalidated(
                    live.session, require_active_delegation=False
                )
                if invalidated is not None:
                    self.logger.info(
                        "Terminating live SSH session after revocation or expiry. "
                        "event=ssh_session_terminated_by_monitor session_id=%s reason=%s",
                        session_id,
                        invalidated,
                    )
                    await self._close(session_id=session_id, disconnected=True)
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
        # Live PTYs cannot be reattached after broker restart. Sweep any leftover
        # ephemeral credential directories and orphaned ssh-agents at the same time.
        sweep_fn = getattr(self.material_provider, "sweep_stale", None)
        if callable(sweep_fn):
            try:
                swept = int(sweep_fn())
                if swept > 0:
                    self.logger.info(
                        "Swept stale SSH ephemeral directories on broker recovery. "
                        "event=ssh_broker_startup_sweep_completed swept=%d",
                        swept,
                    )
            except Exception:
                self.logger.exception(
                    "Failed to sweep stale SSH ephemeral directories on recovery. "
                    "event=ssh_broker_startup_sweep_failed"
                )
        return self.repository.mark_interactive_sessions_lost(
            team_id=self.team_id,
            updated_at_utc=self.repository.current_time_utc(),
        )

    async def _touch(self, live: _LiveSSHSession) -> None:
        database_now = self.repository.current_time_utc()
        candidate = live.session.model_copy(
            update={
                "lease_expires_at_utc": database_now
                + timedelta(seconds=self.lease_ttl_seconds),
                "updated_at_utc": database_now,
            }
        )
        try:
            live.session = self.repository.update_claimed_interactive_session(
                candidate,
                broker_id=self.broker_id,
                lease_token=live.lease_token,
                fence=live.session.fence,
            )
        except StaleExecutionFence as exc:
            await self._close(session_id=live.session.session_id, disconnected=True)
            raise ExecutionAuthorizationDenied(
                "SSH session lease was lost or session was revoked."
            ) from exc

    def _require_live(self, session_id: str) -> _LiveSSHSession:
        live = self._sessions.get(session_id)
        if live is None:
            raise SSHSessionUnavailable(f"SSH session {session_id!r} is not live in this broker.")
        return live

    @staticmethod
    def _ssh_env(material: SSHCredentialMaterial) -> dict[str, str]:
        extra = {"SSH_AUTH_SOCK": material.ssh_auth_sock} if material.ssh_auth_sock else None
        return build_ssh_subprocess_env(extra=extra, include_term=True)

    @staticmethod
    def _host_pinning_args(
        session: InteractiveSessionV1,
        validated_host: ValidatedSSHHost | None,
    ) -> tuple[str, ...]:
        if validated_host is None:
            return ("-o", "CheckHostIP=yes")
        if validated_host.port != session.target.port:
            raise ValueError(
                f"Validated SSH host port {validated_host.port} does not match target port {session.target.port}."
            )
        # OpenSSH applies the first value of an option. When pinning to a
        # validated IP with HostKeyAlias, disable CheckHostIP so ssh pins
        # strictly to the [host]:port entry without mutating known_hosts or
        # refusing on unrelated historical IP lines.
        return (
            "-o",
            "CheckHostIP=no",
            "-o",
            f"HostName={validated_host.resolved_ip}",
            "-o",
            f"HostKeyAlias={validated_host.host_key_alias}",
        )

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
        host_pinning_args = self._host_pinning_args(session, validated_host)
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
            "--",
            f"{target.username}@{target.host}",
            *remote_command,
        )

    def _ssh_command_argv(
        self,
        session: InteractiveSessionV1,
        material: SSHCredentialMaterial,
        *,
        validated_host: ValidatedSSHHost | None = None,
        remote_command: tuple[str, ...],
    ) -> tuple[str, ...]:
        target = session.target
        host_pinning_args = self._host_pinning_args(session, validated_host)
        identity_agent = "SSH_AUTH_SOCK" if material.ssh_auth_sock else "none"
        return (
            self.ssh_executable,
            "-T",
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "PubkeyAuthentication=yes",
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
            "--",
            f"{target.username}@{target.host}",
            *remote_command,
        )

    def _ssh_password_bootstrap_argv(
        self,
        *,
        session: InteractiveSessionV1,
        known_hosts_argv: str,
        validated_host: ValidatedSSHHost | None,
        remote_command: tuple[str, ...],
    ) -> tuple[str, ...]:
        target = session.target
        host_pinning_args = self._host_pinning_args(session, validated_host)
        return (
            self.ssh_executable,
            "-T",
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=no",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "PasswordAuthentication=yes",
            "-o",
            "KbdInteractiveAuthentication=yes",
            "-o",
            "PreferredAuthentications=password,keyboard-interactive",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts_argv}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
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
            *host_pinning_args,
            "-o",
            "ConnectTimeout=15",
            "-p",
            str(target.port),
            "--",
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
    "SSHBundleUploadResult",
    "SSHCommandResult",
    "SSHCommandStatus",
    "SSHCredentialMaterial",
    "SSHCredentialMaterialProvider",
    "SSHHostKeyProbeResult",
    "SSHKeyEnrollResult",
    "SSHSessionBroker",
    "SSHSessionUnavailable",
    "SessionSecretScrubber",
    "StreamSecretScrubber",
    "sweep_stale_ssh_ephemeral_dirs",
]
