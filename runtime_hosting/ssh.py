"""Interactive and command-capable SSH broker with pinned host keys, live revocation, and scrubbing."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import fcntl
import hashlib
import inspect
import logging
import os
from pathlib import Path
import pty
import shlex
import signal
import stat
import struct
import subprocess
import termios
import threading
import time
from typing import Any, Literal, TypeVar

from id_generation import new_id
from runtime_hosting.artifacts import ArtifactStore
from runtime_hosting.authorization import (
    ExecutionAccessGuard,
    ExecutionAuthorizationDenied,
    SSH_DISABLED_MESSAGE,
    SSHCallerContext,
    SSHCapability,
    SSHDisabledError,
    SSHSettingsUnusableError,
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
    record_ssh_child_pid,
    sweep_stale_ssh_ephemeral_dirs,
)
from runtime_hosting.ssh_network import (
    LocalAddressChecker,
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
PinnedSSHTargetKey = tuple[str, int, str, str, str, str]

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
_DEFAULT_MAX_CONCURRENT_COMMANDS_PER_SESSION = 4
_DEFAULT_MAX_CONCURRENT_COMMANDS_PER_BROKER = 16
_MONITOR_MAX_CONSECUTIVE_FAILURES = 3
_STDIN_WRITE_CHUNK_BYTES = 65_536
MAX_PTY_WRITE_BYTES = 65_536
_PTY_WRITE_CHUNK_BYTES = 8_192
_DEFAULT_PTY_WRITE_TIMEOUT_SECONDS = 10.0
_REMOTE_SIGNALLED_TERMINATION_NOTE = (
    "remote process group signalled; any process that detached into its own session "
    "may still require host-side cleanup"
)
_LOCAL_CLIENT_TERMINATION_NOTE = (
    "local SSH client terminated; no remote signal watcher was active, so remote "
    "cleanup depends on the target SSH server"
)
_RECIPE_REQUIRED_FIELD: dict[SSHSecretRecipe, str] = {
    SSHSecretRecipe.TAILSCALE_UP: "tailscale_auth_key",
}


def _termination_note(*, watched_remote: bool) -> str:
    return (
        _REMOTE_SIGNALLED_TERMINATION_NOTE
        if watched_remote
        else _LOCAL_CLIENT_TERMINATION_NOTE
    )


async def _write_stdin_payload(
    process: asyncio.subprocess.Process,
    payload: bytes,
    *,
    keep_open: bool,
) -> None:
    """Stream ``payload`` into ``process.stdin`` in chunks so slow hosts stay cancellable."""

    stdin = process.stdin
    if stdin is None:
        return
    try:
        view = memoryview(payload)
        for offset in range(0, len(view), _STDIN_WRITE_CHUNK_BYTES):
            stdin.write(view[offset : offset + _STDIN_WRITE_CHUNK_BYTES])
            await stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        if not keep_open:
            _close_async_stdin(process)


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


async def _enter_context_safely(
    factory: Callable[[], AbstractContextManager[_T]],
    *,
    verify: Callable[[_T], None] | None = None,
) -> tuple[AbstractContextManager[_T], _T]:
    """Enter a blocking context manager in a worker thread without leaking on cancellation."""

    handoff_lock = threading.Lock()
    abandoned = False
    entered_ref: list[AbstractContextManager[_T]] = []

    def _enter() -> tuple[AbstractContextManager[_T], _T]:
        ctx = factory()
        value = ctx.__enter__()
        try:
            if verify is not None:
                verify(value)
        except Exception:
            ctx.__exit__(None, None, None)
            raise
        with handoff_lock:
            if abandoned:
                ctx.__exit__(None, None, None)
                raise asyncio.CancelledError("Context entry was cancelled during prepare.")
            entered_ref.append(ctx)
        return ctx, value

    task = asyncio.create_task(asyncio.to_thread(_enter))
    try:
        return await asyncio.shield(task)
    except BaseException:
        ctx_to_close: AbstractContextManager[_T] | None = None
        try:
            ctx_to_close, _ = await _await_shielded(task, reraise_on_caller_cancel=False)
        except BaseException:
            with handoff_lock:
                abandoned = True
                if entered_ref:
                    ctx_to_close = entered_ref[0]
        if ctx_to_close is not None:
            try:
                await _await_shielded(
                    asyncio.to_thread(ctx_to_close.__exit__, None, None, None),
                    reraise_on_caller_cancel=False,
                )
            except BaseException:
                pass
        raise


def _close_async_stdin(process: asyncio.subprocess.Process) -> None:
    stdin = process.stdin
    if stdin is None:
        return
    try:
        if stdin.can_write_eof():
            stdin.write_eof()
    except (OSError, RuntimeError):
        pass
    try:
        stdin.close()
    except (OSError, RuntimeError):
        pass


async def _kill_async_process_group(
    process: asyncio.subprocess.Process,
    *,
    close_stdin_first: bool = False,
    stdin_grace_seconds: float = 1.5,
    grace_seconds: float = 1.0,
) -> None:
    """Stop an asyncio subprocess and its process group without holding worker threads.

    When ``close_stdin_first`` is True, closes SSH stdin first so the remote
    stdin-EOF watcher terminates the remote process group (including ``sudo``
    commands) cleanly before falling back to local process-group signals.
    """

    if process.returncode is not None:
        return
    if close_stdin_first:
        _close_async_stdin(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=max(0.1, stdin_grace_seconds))
            return
        except TimeoutError:
            pass
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=max(0.1, grace_seconds))
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            process.kill()
        await process.wait()


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
    caller: SSHCallerContext
    lease_token: str = field(repr=False)
    process: subprocess.Popen[bytes]
    master_fd: int
    material_context: AbstractContextManager[SSHCredentialMaterial]
    scrubber: SessionSecretScrubber = field(repr=False)
    stream_scrubber: StreamSecretScrubber = field(repr=False)
    raw_transcript: bytearray = field(default_factory=bytearray, repr=False)
    transcript_truncated: bool = False
    monitor_failures: int = 0
    closed: bool = False
    read_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    active_waiters: set[asyncio.Future[bool]] = field(default_factory=set, repr=False)


@dataclass(eq=False)
class _CancellableStep:
    """In-flight broker SSH step (for example key bootstrap or sudo probe) watched by the monitor."""

    session: InteractiveSessionV1
    caller: SSHCallerContext
    capability: SSHCapability
    secret_recipe: SSHSecretRecipe | None = None
    revoked_event: asyncio.Event = field(default_factory=asyncio.Event)
    invalidation_reason: str | None = None
    monitor_failures: int = 0


@dataclass
class _LiveSSHCommand:
    command_id: str
    session_id: str
    session: InteractiveSessionV1
    caller: SSHCallerContext
    capability: SSHCapability
    target_id: str | None
    owner_person_id: str | None
    started_at_utc: datetime
    scrubber: SessionSecretScrubber = field(repr=False)
    stdout_stream: StreamSecretScrubber = field(repr=False)
    stderr_stream: StreamSecretScrubber = field(repr=False)
    secret_recipe: SSHSecretRecipe | None = None
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
    invalidation_reason: str | None = None
    process: asyncio.subprocess.Process | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    revoked_event: asyncio.Event = field(default_factory=asyncio.Event)
    runner_task: asyncio.Task[None] | None = None
    monitor_failures: int = 0

    def snapshot(self) -> SSHCommandResult:
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


@dataclass(frozen=True)
class SSHSettingsSyncOutcome:
    """Result of syncing live operator SSH settings before a monitor tick or spawn."""

    enabled: bool = True
    stop_reason: str | None = None
    stop_event: str = "ssh_broker_disabled_killswitch"
    unusable: bool = False
    config_path: str | None = None


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
    max_concurrent_commands_per_session: int = _DEFAULT_MAX_CONCURRENT_COMMANDS_PER_SESSION
    max_concurrent_commands_per_broker: int = _DEFAULT_MAX_CONCURRENT_COMMANDS_PER_BROKER
    network_policy: SSHNetworkPolicy | None = None
    allowed_pinned_targets: frozenset[PinnedSSHTargetKey] | None = None
    resolver: SSHHostResolver | None = None
    local_address_checker: LocalAddressChecker | None = None
    allow_password_bootstrap: bool = False
    sudo_search_paths: tuple[str, ...] = DEFAULT_SUDO_SEARCH_PATHS
    tailscale_search_paths: tuple[str, ...] = DEFAULT_TAILSCALE_SEARCH_PATHS
    revocation_poll_interval_seconds: float = 1.0
    settings_sync: Callable[[], Awaitable[SSHSettingsSyncOutcome] | SSHSettingsSyncOutcome] | None = None
    logger: logging.Logger = logging.getLogger(__name__)
    _sessions: dict[str, _LiveSSHSession] = field(default_factory=dict, init=False, repr=False)
    _closing_tasks: dict[str, asyncio.Task[InteractiveSessionV1]] = field(
        default_factory=dict, init=False, repr=False
    )
    _commands: dict[str, _LiveSSHCommand] = field(default_factory=dict, init=False, repr=False)
    _session_commands: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False)
    _active_steps: set[_CancellableStep] = field(default_factory=set, init=False, repr=False)
    _monitor_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _shutting_down: bool = field(default=False, init=False, repr=False)
    _checked_network_policy: SSHNetworkPolicy | None = field(default=None, init=False, repr=False)
    _checked_material_provider: SSHCredentialMaterialProvider | None = field(
        default=None, init=False, repr=False
    )
    _checked_allowed_pinned_targets: frozenset[PinnedSSHTargetKey] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._checked_network_policy = self.network_policy
        self._checked_material_provider = self.material_provider
        self._checked_allowed_pinned_targets = self.allowed_pinned_targets

    def _require_not_shutting_down(self) -> None:
        if self._shutting_down:
            raise SSHSessionUnavailable("SSH session broker is shutting down.")

    async def _sync_settings_before_spawn(self) -> None:
        """Run a lightweight operator settings sync before spawning or touching secrets."""

        if self.settings_sync is None:
            return
        sync_result = self.settings_sync()
        if inspect.isawaitable(sync_result):
            sync_result = await sync_result
        if not sync_result.enabled:
            if sync_result.unusable:
                raise SSHSettingsUnusableError(
                    sync_result.stop_reason or "execution_control.ssh settings cannot be used.",
                    config_path=sync_result.config_path,
                )
            raise SSHDisabledError(sync_result.stop_reason or SSH_DISABLED_MESSAGE)

    async def _recheck_before_spawn(
        self,
        *,
        session: InteractiveSessionV1,
        caller: SSHCallerContext,
        capability: SSHCapability,
        validated_host: ValidatedSSHHost | None,
        policy_snapshot: SSHNetworkPolicy | None,
        provider_snapshot: SSHCredentialMaterialProvider | None,
        secret_recipe: SSHSecretRecipe | None = None,
        revoked_event: asyncio.Event | None = None,
        invalidation_reason: Callable[[], str | None] | None = None,
        default_revoked_reason: str = (
            "SSH command was terminated because the session or target was revoked."
        ),
    ) -> tuple[
        ValidatedSSHHost | None,
        SSHNetworkPolicy | None,
        SSHCredentialMaterialProvider,
        EnrolledSSHTargetV1 | None,
        SSHInteractionRecordV1 | None,
    ]:
        """Re-sync settings, re-authorize, and re-validate target policy right before spawning ssh."""

        while True:
            await self._sync_settings_before_spawn()
            self._require_not_shutting_down()
            if revoked_event is not None and revoked_event.is_set():
                reason = invalidation_reason() if invalidation_reason is not None else None
                raise ExecutionAuthorizationDenied(reason or default_revoked_reason)
            enrolled_target, interaction = self._authorize_session(
                session,
                capability=capability,
                caller=caller,
                secret_recipe=secret_recipe,
            )
            if session.target.target_id is None:
                self._require_pinned_target_allowed(session)
                current_provider = self.material_provider
                if provider_snapshot is not None and current_provider is not provider_snapshot:
                    configured_bindings = self._configured_binding_ids(current_provider)
                    if (
                        configured_bindings is not None
                        and session.target.credential_binding_id not in configured_bindings
                    ):
                        raise ExecutionAuthorizationDenied(
                            "SSH target credential binding was removed from execution_control.ssh.targets."
                        )
                return (
                    validated_host,
                    self.network_policy,
                    current_provider,
                    enrolled_target,
                    interaction,
                )

            current_policy = self.network_policy
            if current_policy == policy_snapshot:
                return (
                    validated_host,
                    current_policy,
                    self.material_provider,
                    enrolled_target,
                    interaction,
                )

            policy_snapshot = current_policy
            try:
                validated_host = await asyncio.to_thread(
                    validate_ssh_host,
                    session.target.host,
                    session.target.port,
                    current_policy or SSHNetworkPolicy.default(),
                    resolver=self.resolver,
                    local_address_checker=self.local_address_checker,
                )
            except PermissionError as exc:
                raise ExecutionAuthorizationDenied(str(exc)) from exc

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

    def authorize_capability(
        self,
        *,
        session_id: str,
        capability: SSHCapability,
        caller: SSHCallerContext,
        secret_recipe: SSHSecretRecipe | None = None,
    ) -> tuple[EnrolledSSHTargetV1 | None, SSHInteractionRecordV1 | None]:
        """Verify session state and ``ExecutionAccessGuard`` before running side-effecting tool preflight."""

        self._require_not_shutting_down()
        session = self._session_for_operation(session_id)
        return self._authorize_session(
            session,
            capability=capability,
            caller=caller,
            secret_recipe=secret_recipe,
        )

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

    @staticmethod
    def _pinned_target_key(session: InteractiveSessionV1) -> PinnedSSHTargetKey:
        target = session.target
        return (
            target.host,
            target.port,
            target.username,
            target.host_key_algorithm,
            target.host_key_fingerprint,
            target.credential_binding_id,
        )

    def _require_pinned_target_allowed(self, session: InteractiveSessionV1) -> None:
        if session.target.target_id is not None:
            return
        if (
            self.allowed_pinned_targets is not None
            and self._pinned_target_key(session) not in self.allowed_pinned_targets
        ):
            raise ExecutionAuthorizationDenied(
                "SSH target was removed or changed in execution_control.ssh.targets."
            )

    def _validate_target_network(self, session: InteractiveSessionV1) -> ValidatedSSHHost | None:
        if session.target.target_id is None:
            # Operator-pinned static targets from execution_control.ssh.targets connect
            # directly as in 0.9 without dynamic network-policy checks or IP pinning,
            # while still requiring the exact pinned target entry to remain active.
            self._require_pinned_target_allowed(session)
            return None
        policy = self.network_policy or SSHNetworkPolicy.default()
        return validate_ssh_host(
            session.target.host,
            session.target.port,
            policy,
            resolver=self.resolver,
            local_address_checker=self.local_address_checker,
        )

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

    # -- interactive PTY lifecycle -----------------------------------------------

    async def open(
        self,
        *,
        session_id: str,
        lease_token: str,
        caller: SSHCallerContext,
    ) -> InteractiveSessionV1:
        await self._sync_settings_before_spawn()
        self._require_not_shutting_down()
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
            policy_snapshot = self.network_policy
            provider_snapshot = self.material_provider
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
        try:
            context, material = await _enter_context_safely(
                lambda: self._prepare_material_context(
                    claimed, key_lifetime_seconds=open_key_lifetime
                ),
                verify=lambda mat: self._verify_material(claimed, mat),
            )
            validated_host, policy_snapshot, provider_snapshot, _, _ = (
                await self._recheck_before_spawn(
                    session=claimed,
                    caller=caller,
                    capability=SSHCapability.CONNECT,
                    validated_host=validated_host,
                    policy_snapshot=policy_snapshot,
                    provider_snapshot=provider_snapshot,
                )
            )
            scrubber = SessionSecretScrubber(material.touched_secrets, logger=self.logger)
            stream_scrubber = scrubber.new_stream_scrubber()
            master_fd, slave_fd = pty.openpty()
            os.set_blocking(master_fd, False)
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
            record_ssh_child_pid(material.working_dir, process.pid)
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
                caller=caller,
                lease_token=lease_token,
                process=process,
                master_fd=master_fd,
                material_context=context,
                scrubber=scrubber,
                stream_scrubber=stream_scrubber,
            )
            self._sessions[session_id] = live
            self._ensure_monitor_started()
            return opened
        except BaseException:
            await _await_shielded(
                self._cleanup_failed_open(
                    claimed=claimed,
                    lease_token=lease_token,
                    process=process,
                    master_fd=master_fd,
                    slave_fd=slave_fd,
                    context=context,
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

    def _require_live_authorized(
        self,
        session_id: str,
        *,
        capability: SSHCapability,
        caller: SSHCallerContext,
    ) -> _LiveSSHSession:
        live = self._sessions.get(session_id)
        if live is not None:
            self._authorize_session(live.session, capability=capability, caller=caller)
            return live
        # If the background monitor already terminated this session after a grant or
        # session revocation, surface the real authorization denial rather than a
        # generic "not live" error.
        existing = self.repository.get_interactive_session(
            team_id=self.team_id,
            session_id=session_id,
        )
        if existing is not None:
            self._authorize_session(existing, capability=capability, caller=caller)
        raise SSHSessionUnavailable(f"SSH session {session_id!r} is not live in this broker.")

    @staticmethod
    def _detach_live_fd(live: _LiveSSHSession) -> None:
        """Mark a live PTY closed, remove event-loop fd callbacks, and wake pending readers/writers."""

        live.closed = True
        fd = live.master_fd
        if fd >= 0:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                try:
                    loop.remove_reader(fd)
                except Exception:
                    pass
                try:
                    loop.remove_writer(fd)
                except Exception:
                    pass
        for waiter in list(live.active_waiters):
            if not waiter.done():
                waiter.set_result(False)

    @staticmethod
    async def _wait_fd_ready(
        live: _LiveSSHSession,
        *,
        mode: Literal["read", "write"],
        timeout: float,
    ) -> bool:
        """Wait for non-blocking PTY readiness on the event loop without using thread-pool workers."""

        if live.closed or live.master_fd < 0 or timeout <= 0.0:
            return False
        loop = asyncio.get_running_loop()
        fd = live.master_fd
        waiter: asyncio.Future[bool] = loop.create_future()
        live.active_waiters.add(waiter)

        def _on_ready() -> None:
            if not waiter.done():
                waiter.set_result(True)

        add = loop.add_reader if mode == "read" else loop.add_writer
        remove = loop.remove_reader if mode == "read" else loop.remove_writer
        add(fd, _on_ready)
        try:
            return await asyncio.wait_for(waiter, timeout=timeout)
        except TimeoutError:
            return False
        finally:
            live.active_waiters.discard(waiter)
            if live.master_fd == fd and fd >= 0:
                try:
                    remove(fd)
                except Exception:
                    pass

    async def write(
        self,
        *,
        session_id: str,
        payload: bytes,
        caller: SSHCallerContext,
        timeout_seconds: float = _DEFAULT_PTY_WRITE_TIMEOUT_SECONDS,
    ) -> int:
        """Type bounded UTF-8 input into a non-blocking PTY without stalling the event loop."""

        self._require_not_shutting_down()
        raw_bytes = bytes(payload)
        if len(raw_bytes) > MAX_PTY_WRITE_BYTES:
            raise ValueError(
                f"SSH terminal write exceeds the maximum size ({MAX_PTY_WRITE_BYTES} bytes); "
                "use upload_execution_ssh_bundle for larger content."
            )
        live = self._require_live_authorized(
            session_id,
            capability=SSHCapability.TYPING,
            caller=caller,
        )
        if not raw_bytes:
            await self._touch(live)
            return 0
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        view = memoryview(raw_bytes)
        written = 0
        async with live.write_lock:
            while written < len(view):
                if live.closed or live.master_fd < 0:
                    raise SSHSessionUnavailable(
                        f"SSH session {session_id!r} closed during write."
                    )
                chunk = view[written : written + _PTY_WRITE_CHUNK_BYTES]
                try:
                    count = os.write(live.master_fd, chunk)
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        raise TimeoutError(
                            f"Timed out writing to SSH session {session_id!r} after "
                            f"{written} of {len(view)} bytes."
                        ) from None
                    ready = await self._wait_fd_ready(live, mode="write", timeout=remaining)
                    if live.closed or live.master_fd < 0:
                        raise SSHSessionUnavailable(
                            f"SSH session {session_id!r} closed during write."
                        ) from None
                    if not ready:
                        raise TimeoutError(
                            f"Timed out writing to SSH session {session_id!r} after "
                            f"{written} of {len(view)} bytes."
                        ) from None
                    continue
                except OSError as exc:
                    raise SSHSessionUnavailable(
                        f"SSH session {session_id!r} write failed: {exc}"
                    ) from exc
                if count <= 0:
                    raise SSHSessionUnavailable(
                        f"SSH session {session_id!r} closed during write."
                    )
                written += count
        if not live.closed:
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
        self._require_not_shutting_down()
        live = self._require_live_authorized(
            session_id,
            capability=SSHCapability.CONNECT,
            caller=caller,
        )

        async def _read_chunk(wait_seconds: float) -> tuple[bytes, bool]:
            if live.closed or live.master_fd < 0:
                return b"", True
            try:
                data = os.read(live.master_fd, maximum_bytes)
                return data, (len(data) == 0)
            except BlockingIOError:
                pass
            except OSError:
                return b"", True
            if wait_seconds <= 0.0:
                return b"", False
            ready = await self._wait_fd_ready(live, mode="read", timeout=wait_seconds)
            if live.closed or live.master_fd < 0:
                return b"", True
            if not ready:
                return b"", False
            try:
                data = os.read(live.master_fd, maximum_bytes)
                return data, (len(data) == 0)
            except BlockingIOError:
                return b"", False
            except OSError:
                return b"", True

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        scrubbed_parts: list[bytes] = []
        received_any_payload = False
        fd_eof = False
        async with live.read_lock:
            while True:
                if live.closed or live.master_fd < 0:
                    fd_eof = True
                    break
                quiet_wait = live.stream_scrubber.time_until_quiet_release()
                remaining = max(0.0, deadline - time.monotonic())
                if quiet_wait is None:
                    wait_for = remaining
                elif quiet_wait == 0.0:
                    wait_for = 0.0
                else:
                    wait_for = min(remaining, quiet_wait + 0.002)
                payload, fd_eof = await _read_chunk(wait_for)
                if live.closed:
                    break
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
        if live.closed:
            return scrubbed
        if scrubbed:
            live.session = live.session.model_copy(
                update={"transcript_bytes": live.session.transcript_bytes + len(scrubbed)}
            )
        await self._touch(live)
        if not live.closed and live.process.poll() is not None and (
            not received_any_payload or (fd_eof and not scrubbed_parts)
        ):
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
        self._require_not_shutting_down()
        live = self._require_live_authorized(
            session_id,
            capability=SSHCapability.CONNECT,
            caller=caller,
        )
        if live.closed or live.master_fd < 0:
            raise SSHSessionUnavailable(f"SSH session {session_id!r} is not live in this broker.")
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

        await self._sync_settings_before_spawn()
        self._require_not_shutting_down()
        if not command or not command.strip():
            raise ValueError("SSH command must not be empty.")
        session = self._session_for_operation(session_id)
        enrolled, interaction = self._authorize_session(
            session, capability=SSHCapability.COMMAND, caller=caller
        )
        policy_snapshot = self.network_policy
        provider_snapshot = self.material_provider
        validated_host = self._validate_target_network(session)
        effective_timeout = self._effective_timeout(timeout_seconds, interaction)
        platform = enrolled.platform if enrolled is not None else (session.target.platform or SSHTargetPlatform.LINUX)
        remote_argv = build_remote_shell_command(command, platform)
        cmd = await self._start_tracked_command(
            session=session,
            caller=caller,
            capability=SSHCapability.COMMAND,
            remote_argv=remote_argv,
            validated_host=validated_host,
            policy_snapshot=policy_snapshot,
            provider_snapshot=provider_snapshot,
            stdin_payload=None,
            keep_stdin_open=(platform != SSHTargetPlatform.WINDOWS),
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
        session = self.repository.get_interactive_session(
            team_id=self.team_id, session_id=session_id
        )
        if session is None:
            raise SSHSessionUnavailable(f"SSH session {session_id!r} does not exist.")
        if cmd is None or cmd.session_id != session_id:
            self._authorize_session(session, capability=SSHCapability.CONNECT, caller=caller)
            raise LookupError(
                f"SSH command {command_id!r} is no longer tracked by this broker for session "
                f"{session_id!r}; the broker may have restarted or the command record expired, "
                "so its outcome is unknown."
            )
        if cmd.status is SSHCommandStatus.RUNNING:
            self._authorize_session(session, capability=SSHCapability.CONNECT, caller=caller)
        else:
            if cmd.owner_person_id is not None and caller.person_id != cmd.owner_person_id:
                raise ExecutionAuthorizationDenied("SSH command belongs to another person.")
            owner_agent_type = (
                session.connect_activation.holder.agent_type
                if session.connect_activation is not None
                else None
            )
            allowed_agent_types = {cmd.caller.agent_type}
            if owner_agent_type:
                allowed_agent_types.add(owner_agent_type)
            if caller.agent_type not in allowed_agent_types:
                raise ExecutionAuthorizationDenied(
                    "Only the session's owning manager or the delegated agent that started this SSH command may read its result."
                )
            if (
                caller.agent_type != owner_agent_type
                and cmd.caller.task_id is not None
                and caller.task_id != cmd.caller.task_id
            ):
                raise ExecutionAuthorizationDenied("SSH command belongs to another request.")
        return await self._wait_for_command(cmd, wait_seconds=wait_seconds)

    async def enroll_ssh_key(
        self,
        *,
        session_id: str,
        caller: SSHCallerContext,
        timeout_seconds: float = 60.0,
    ) -> SSHKeyEnrollResult:
        """One-time password bootstrap: install Sealbox ``ssh_public_key``, verify key login, latch target."""

        await self._sync_settings_before_spawn()
        self._require_not_shutting_down()
        bounded_requested_timeout = max(1.0, min(float(timeout_seconds), 3600.0))
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
                lease_ttl_seconds=max(self.lease_ttl_seconds, int(bounded_requested_timeout) + 30),
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
            policy_snapshot = self.network_policy
            provider_snapshot = self.material_provider
            validated_host = self._validate_target_network(session)
            effective_timeout = self._effective_timeout(bounded_requested_timeout, interaction)
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
                caller=caller,
                enrolled_target=enrolled_target,
                validated_host=validated_host,
                policy_snapshot=policy_snapshot,
                provider_snapshot=provider_snapshot,
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
        caller: SSHCallerContext,
        capability: SSHCapability,
        argv: tuple[str, ...],
        env: dict[str, str],
        cwd: Path | None,
        stdin_payload: bytes | None,
        timeout_seconds: float,
        scrubber: SessionSecretScrubber,
        secret_recipe: SSHSecretRecipe | None = None,
    ) -> tuple[int, str, str]:
        """Run one async broker SSH subprocess watched by the shared broker monitor."""

        if timeout_seconds <= 0:
            raise TimeoutError("SSH operation timed out before the step could start.")
        step = _CancellableStep(
            session=session,
            caller=caller,
            capability=capability,
            secret_recipe=secret_recipe,
        )
        self._active_steps.add(step)
        self._ensure_monitor_started()
        process: asyncio.subprocess.Process | None = None
        comm_task: asyncio.Task[tuple[bytes, bytes]] | None = None
        revoke_waiter: asyncio.Task[bool] | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin_payload is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                start_new_session=True,
            )
            record_ssh_child_pid(cwd, process.pid)
            if step.revoked_event.is_set():
                await _kill_async_process_group(process, grace_seconds=1.0)
                raise ExecutionAuthorizationDenied(
                    step.invalidation_reason
                    or "SSH session or delegation became inactive during operation."
                )
            if self._shutting_down:
                await _kill_async_process_group(process, grace_seconds=1.0)
                raise SSHSessionUnavailable("SSH session broker is shutting down.")
            comm_task = asyncio.create_task(process.communicate(stdin_payload))
            revoke_waiter = asyncio.create_task(step.revoked_event.wait())
            done, _ = await asyncio.wait(
                {comm_task, revoke_waiter},
                timeout=max(0.1, timeout_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if step.revoked_event.is_set():
                await _kill_async_process_group(process, grace_seconds=1.0)
                await asyncio.gather(comm_task, return_exceptions=True)
                raise ExecutionAuthorizationDenied(
                    step.invalidation_reason
                    or "SSH session or delegation became inactive during operation."
                )
            if comm_task not in done:
                await _kill_async_process_group(process, grace_seconds=1.0)
                await asyncio.gather(comm_task, return_exceptions=True)
                raise TimeoutError(f"SSH step timed out after {timeout_seconds:.1f}s.")
            raw_out, raw_err = comm_task.result()
            out_text = scrubber.scrub_bytes(raw_out or b"").decode("utf-8", errors="replace")
            err_text = scrubber.scrub_bytes(raw_err or b"").decode("utf-8", errors="replace")
            return int(process.returncode or 0), out_text, err_text
        except BaseException:
            if process is not None and process.returncode is None:
                await _await_shielded(
                    _kill_async_process_group(process, grace_seconds=1.0),
                    reraise_on_caller_cancel=False,
                )
            if comm_task is not None and not comm_task.done():
                comm_task.cancel()
                await asyncio.gather(comm_task, return_exceptions=True)
            raise
        finally:
            if revoke_waiter is not None and not revoke_waiter.done():
                revoke_waiter.cancel()
                await asyncio.gather(revoke_waiter, return_exceptions=True)
            self._active_steps.discard(step)

    async def _execute_key_bootstrap(
        self,
        *,
        session: InteractiveSessionV1,
        caller: SSHCallerContext,
        enrolled_target: EnrolledSSHTargetV1,
        validated_host: ValidatedSSHHost | None,
        policy_snapshot: SSHNetworkPolicy | None,
        provider_snapshot: SSHCredentialMaterialProvider | None,
        catalog_provider: CatalogSSHCredentialMaterialProvider,
        scrubber: SessionSecretScrubber,
        timeout_seconds: float,
    ) -> tuple[str, EnrolledSSHTargetV1]:
        assert enrolled_target.bootstrap_credential_key is not None
        assert enrolled_target.bootstrap_credential_owner_scope is not None
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        mat_ctx, material = await _enter_context_safely(
            lambda: catalog_provider.prepare(
                session,
                key_lifetime_seconds=max(15, int(timeout_seconds) + 10),
                scrubber=scrubber,
            ),
            verify=lambda mat: self._verify_material(session, mat),
        )
        try:
            if not material.public_key:
                raise PermissionError("Resolved SSH key credential has no public key to enroll.")
            _algo, key_fp = ssh_public_key_sha256_fingerprint(material.public_key)
            validated_host, policy_snapshot, provider_snapshot, _, _ = (
                await self._recheck_before_spawn(
                    session=session,
                    caller=caller,
                    capability=SSHCapability.PASSWORD_BOOTSTRAP,
                    validated_host=validated_host,
                    policy_snapshot=policy_snapshot,
                    provider_snapshot=provider_snapshot,
                )
            )
            install_cmd = build_key_install_command(enrolled_target.platform)
            boot_owner = enrolled_target.bootstrap_credential_owner_scope
            boot_key = enrolled_target.bootstrap_credential_key
            askpass_ctx, askpass = await _enter_context_safely(
                lambda: catalog_provider.prepare_password_askpass(
                    owner=boot_owner,
                    credential_key=boot_key,
                    scrubber=scrubber,
                )
            )
            try:
                validated_host, policy_snapshot, provider_snapshot, _, _ = (
                    await self._recheck_before_spawn(
                        session=session,
                        caller=caller,
                        capability=SSHCapability.PASSWORD_BOOTSTRAP,
                        validated_host=validated_host,
                        policy_snapshot=policy_snapshot,
                        provider_snapshot=provider_snapshot,
                    )
                )
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
                remaining_install = deadline - time.monotonic()
                rc, out_text, err_text = await self._run_cancellable_ssh_step(
                    session=session,
                    caller=caller,
                    capability=SSHCapability.PASSWORD_BOOTSTRAP,
                    argv=install_argv,
                    env=install_env,
                    cwd=askpass.working_dir,
                    stdin_payload=f"{material.public_key}\n".encode("utf-8"),
                    timeout_seconds=remaining_install,
                    scrubber=scrubber,
                )
                if rc != 0:
                    detail = (err_text or out_text).strip()
                    raise RuntimeError(
                        f"SSH password key installation failed (exit {rc})"
                        + (f": {detail}" if detail else ".")
                    )
            finally:
                await _await_shielded(
                    asyncio.to_thread(askpass_ctx.__exit__, None, None, None),
                    reraise_on_caller_cancel=False,
                )

            validated_host, policy_snapshot, provider_snapshot, _, _ = (
                await self._recheck_before_spawn(
                    session=session,
                    caller=caller,
                    capability=SSHCapability.PASSWORD_BOOTSTRAP,
                    validated_host=validated_host,
                    policy_snapshot=policy_snapshot,
                    provider_snapshot=provider_snapshot,
                )
            )
            remaining_verify = deadline - time.monotonic()
            if remaining_verify <= 0:
                raise TimeoutError(
                    f"SSH key bootstrap timed out after {timeout_seconds:.1f}s before key verification."
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
                caller=caller,
                capability=SSHCapability.PASSWORD_BOOTSTRAP,
                argv=verify_argv,
                env=self._ssh_env(material),
                cwd=material.working_dir,
                stdin_payload=None,
                timeout_seconds=remaining_verify,
                scrubber=scrubber,
            )
            if verify_rc != 0 or KEY_AUTH_VERIFY_MARKER not in verify_stdout:
                detail = (verify_stderr or verify_stdout).strip()
                raise RuntimeError(
                    f"Key-only SSH verification failed after key install (exit {verify_rc})"
                    + (f": {detail}" if detail else ".")
                )
        finally:
            await _await_shielded(
                asyncio.to_thread(mat_ctx.__exit__, None, None, None),
                reraise_on_caller_cancel=False,
            )

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

        await self._sync_settings_before_spawn()
        self._require_not_shutting_down()
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
        policy_snapshot = self.network_policy
        provider_snapshot = self.material_provider
        validated_host = self._validate_target_network(session)
        effective_timeout = self._effective_timeout(timeout_seconds, interaction)
        self._catalog_provider()

        # Preflight: locate trusted absolute sudo binary and check NOPASSWD without touching secrets.
        sudo_path, nopasswd, validated_host, policy_snapshot, provider_snapshot = (
            await self._probe_remote_sudo(
                session=session,
                caller=caller,
                validated_host=validated_host,
                policy_snapshot=policy_snapshot,
                provider_snapshot=provider_snapshot,
                timeout_seconds=min(30.0, effective_timeout),
                secret_recipe=parsed_recipe,
            )
        )

        # Re-sync settings, re-authorize, and re-validate network/pinned policy right before
        # resolving secrets or starting the sudo command so a revocation or policy change
        # during preflight never reads or transmits secrets.
        validated_host, policy_snapshot, provider_snapshot, enrolled_target, interaction = (
            await self._recheck_before_spawn(
                session=session,
                caller=caller,
                capability=SSHCapability.BROKER_SUDO,
                secret_recipe=parsed_recipe,
                validated_host=validated_host,
                policy_snapshot=policy_snapshot,
                provider_snapshot=provider_snapshot,
            )
        )
        assert enrolled_target is not None
        assert enrolled_target.bootstrap_credential_key is not None
        assert enrolled_target.bootstrap_credential_owner_scope is not None
        effective_timeout = self._effective_timeout(timeout_seconds, interaction)
        catalog_provider = self._catalog_provider()

        stdin_lines: list[tuple[str, str]] = []
        extra_secrets: list[str] = []
        boot_owner: OwnerScopeV1 = enrolled_target.bootstrap_credential_owner_scope
        boot_key: str = enrolled_target.bootstrap_credential_key
        needs_password = not nopasswd
        if needs_password:
            password = catalog_provider.resolve_admin_password(
                owner=boot_owner,
                credential_key=boot_key,
            )
            stdin_lines.append(("admin_password", password))
            extra_secrets.append(password)

        if parsed_recipe is not None:
            secret_field = _RECIPE_REQUIRED_FIELD[parsed_recipe]
            recipe_secret = catalog_provider.resolve_named_secret(
                owner=boot_owner,
                credential_key=boot_key,
                field_name=secret_field,
            )
            stdin_lines.append((secret_field, recipe_secret))
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

        stdin_payload = (
            ("\n".join(secret_line for _, secret_line in stdin_lines) + "\n").encode("utf-8")
            if stdin_lines
            else None
        )
        cmd = await self._start_tracked_command(
            session=session,
            caller=caller,
            capability=SSHCapability.BROKER_SUDO,
            secret_recipe=parsed_recipe,
            remote_argv=remote_argv,
            validated_host=validated_host,
            policy_snapshot=policy_snapshot,
            provider_snapshot=provider_snapshot,
            stdin_payload=stdin_payload,
            keep_stdin_open=True,
            timeout_seconds=effective_timeout,
            extra_secrets=tuple(extra_secrets),
        )
        return await self._wait_for_command(cmd, wait_seconds=wait_seconds)

    async def _probe_remote_sudo(
        self,
        *,
        session: InteractiveSessionV1,
        caller: SSHCallerContext,
        validated_host: ValidatedSSHHost | None,
        policy_snapshot: SSHNetworkPolicy | None,
        provider_snapshot: SSHCredentialMaterialProvider | None,
        timeout_seconds: float,
        secret_recipe: SSHSecretRecipe | None = None,
    ) -> tuple[
        str,
        bool,
        ValidatedSSHHost | None,
        SSHNetworkPolicy | None,
        SSHCredentialMaterialProvider,
    ]:
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
        scrubber = SessionSecretScrubber(logger=self.logger)
        mat_ctx, material = await _enter_context_safely(
            lambda: self._prepare_material_context(
                session,
                key_lifetime_seconds=max(15, int(timeout_seconds) + 10),
                scrubber=scrubber,
            ),
            verify=lambda mat: self._verify_material(session, mat),
        )
        try:
            validated_host, policy_snapshot, current_provider, _, _ = (
                await self._recheck_before_spawn(
                    session=session,
                    caller=caller,
                    capability=SSHCapability.BROKER_SUDO,
                    secret_recipe=secret_recipe,
                    validated_host=validated_host,
                    policy_snapshot=policy_snapshot,
                    provider_snapshot=provider_snapshot,
                )
            )
            scrubber.register_many(material.touched_secrets)
            argv = self._ssh_command_argv(
                session,
                material,
                validated_host=validated_host,
                remote_command=(f"sh -c {shlex.quote(probe_script)}",),
            )
            rc, stdout, stderr = await self._run_cancellable_ssh_step(
                session=session,
                caller=caller,
                capability=SSHCapability.BROKER_SUDO,
                secret_recipe=secret_recipe,
                argv=argv,
                env=self._ssh_env(material),
                cwd=material.working_dir,
                stdin_payload=None,
                timeout_seconds=timeout_seconds,
                scrubber=scrubber,
            )
            if rc != 0:
                detail = stderr.strip()
                raise RuntimeError(
                    "Could not find sudo at a trusted path on the remote host"
                    + (f": {detail}" if detail else ".")
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
            return sudo_path, nopasswd, validated_host, policy_snapshot, current_provider
        finally:
            await _await_shielded(
                asyncio.to_thread(mat_ctx.__exit__, None, None, None),
                reraise_on_caller_cancel=False,
            )

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

        await self._sync_settings_before_spawn()
        self._require_not_shutting_down()
        session = self._session_for_operation(session_id)
        enrolled, interaction = self._authorize_session(
            session, capability=SSHCapability.WORKSPACE_TRANSFER, caller=caller
        )
        policy_snapshot = self.network_policy
        provider_snapshot = self.material_provider
        validated_host = self._validate_target_network(session)
        cleaned_dest = validate_remote_destination(remote_destination)
        archive_bytes, total_file_bytes = build_tar_gz_bundle(entries)
        effective_timeout = self._effective_timeout(timeout_seconds, interaction)
        platform = enrolled.platform if enrolled is not None else (session.target.platform or SSHTargetPlatform.LINUX)
        extract_argv = build_bundle_extract_command(cleaned_dest, platform)
        cmd = await self._start_tracked_command(
            session=session,
            caller=caller,
            capability=SSHCapability.WORKSPACE_TRANSFER,
            remote_argv=extract_argv,
            validated_host=validated_host,
            policy_snapshot=policy_snapshot,
            provider_snapshot=provider_snapshot,
            stdin_payload=archive_bytes,
            keep_stdin_open=False,
            timeout_seconds=effective_timeout,
            extra_secrets=(),
        )
        try:
            result = await self._wait_for_command(cmd, wait_seconds=effective_timeout + 5.0)
        except asyncio.CancelledError:
            if cmd.status is SSHCommandStatus.RUNNING:
                cmd.invalidation_reason = "Workspace bundle upload was cancelled."
                cmd.revoked_event.set()
                if cmd.runner_task is not None and not cmd.runner_task.done():
                    await _await_shielded(
                        asyncio.gather(cmd.runner_task, return_exceptions=True),
                        reraise_on_caller_cancel=False,
                    )
            raise
        if result.status == SSHCommandStatus.RUNNING:
            timeout_msg = (
                f"Workspace bundle upload timed out after {effective_timeout:.1f}s "
                f"({_termination_note(watched_remote=False)})."
            )
            cmd.invalidation_reason = timeout_msg
            cmd.revoked_event.set()
            if cmd.runner_task is not None and not cmd.runner_task.done():
                await asyncio.gather(cmd.runner_task, return_exceptions=True)
            if cmd.status is SSHCommandStatus.REVOKED:
                cmd.status = SSHCommandStatus.TIMED_OUT
                cmd.error = timeout_msg
            raise TimeoutError(timeout_msg)
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
        capability: SSHCapability,
        remote_argv: tuple[str, ...],
        validated_host: ValidatedSSHHost | None,
        policy_snapshot: SSHNetworkPolicy | None,
        provider_snapshot: SSHCredentialMaterialProvider | None,
        stdin_payload: bytes | None,
        keep_stdin_open: bool,
        timeout_seconds: float,
        extra_secrets: tuple[str, ...],
        secret_recipe: SSHSecretRecipe | None = None,
    ) -> _LiveSSHCommand:
        session_running = sum(
            1
            for existing_id in self._session_commands.get(session.session_id, ())
            if (existing := self._commands.get(existing_id)) is not None
            and existing.status is SSHCommandStatus.RUNNING
        )
        if session_running >= max(1, int(self.max_concurrent_commands_per_session)):
            raise RuntimeError(
                f"SSH session {session.session_id!r} already has {session_running} running "
                f"commands (limit {self.max_concurrent_commands_per_session}); "
                "wait for one to finish before starting another."
            )
        broker_running = sum(
            1 for existing in self._commands.values() if existing.status is SSHCommandStatus.RUNNING
        )
        if broker_running >= max(1, int(self.max_concurrent_commands_per_broker)):
            raise RuntimeError(
                f"SSH broker already has {broker_running} running commands "
                f"(limit {self.max_concurrent_commands_per_broker}); "
                "wait for one to finish before starting another."
            )

        scrubber = SessionSecretScrubber(extra_secrets, logger=self.logger)
        command_id = new_id("ssh_cmd")
        cmd = _LiveSSHCommand(
            command_id=command_id,
            session_id=session.session_id,
            session=session,
            caller=caller,
            capability=capability,
            secret_recipe=secret_recipe,
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
                policy_snapshot=policy_snapshot,
                provider_snapshot=provider_snapshot,
                stdin_payload=stdin_payload,
                keep_stdin_open=keep_stdin_open,
                timeout_seconds=timeout_seconds,
            ),
            name=f"ssh-command:{command_id}",
        )
        self._ensure_monitor_started()
        self.logger.info(
            "Started SSH command. event=ssh_command_started session_id=%s command_id=%s agent_type=%s",
            session.session_id,
            command_id,
            caller.agent_type,
        )
        return cmd

    def _release_command_quiet_output(self, cmd: _LiveSSHCommand) -> None:
        if cmd.status is not SSHCommandStatus.RUNNING:
            return
        for stream_scrubber, scrubbed_buf in (
            (cmd.stdout_stream, cmd.scrubbed_stdout),
            (cmd.stderr_stream, cmd.scrubbed_stderr),
        ):
            released = stream_scrubber.release_quiet()
            if released:
                scrub_cap = self.command_output_limit_bytes - len(scrubbed_buf)
                if scrub_cap > 0:
                    scrubbed_buf.extend(released[:scrub_cap])

    async def _wait_for_command(
        self, cmd: _LiveSSHCommand, *, wait_seconds: float
    ) -> SSHCommandResult:
        wait = max(0.0, float(wait_seconds))
        if wait > 0 and not cmd.done_event.is_set():
            try:
                await asyncio.wait_for(cmd.done_event.wait(), timeout=wait)
            except TimeoutError:
                pass
        self._release_command_quiet_output(cmd)
        return cmd.snapshot()

    async def _run_tracked_command(
        self,
        *,
        cmd: _LiveSSHCommand,
        session: InteractiveSessionV1,
        remote_argv: tuple[str, ...],
        validated_host: ValidatedSSHHost | None,
        policy_snapshot: SSHNetworkPolicy | None,
        provider_snapshot: SSHCredentialMaterialProvider | None,
        stdin_payload: bytes | None,
        keep_stdin_open: bool,
        timeout_seconds: float,
    ) -> None:
        material_context: AbstractContextManager[SSHCredentialMaterial] | None = None
        process: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[None] | None = None
        stderr_task: asyncio.Task[None] | None = None
        stdin_task: asyncio.Task[None] | None = None
        wait_task: asyncio.Task[int] | None = None
        revoke_waiter: asyncio.Task[bool] | None = None
        termination_note = _termination_note(watched_remote=keep_stdin_open)
        try:
            material_context, material = await _enter_context_safely(
                lambda: self._prepare_material_context(
                    session,
                    key_lifetime_seconds=max(15, int(timeout_seconds) + 10),
                    scrubber=cmd.scrubber,
                ),
                verify=lambda mat: self._verify_material(session, mat),
            )
            cmd.scrubber.register_many(material.touched_secrets)

            # Re-verify authorization and active settings immediately before starting ssh or sending stdin secrets.
            validated_host, policy_snapshot, provider_snapshot, _, _ = (
                await self._recheck_before_spawn(
                    session=session,
                    caller=cmd.caller,
                    capability=cmd.capability,
                    secret_recipe=cmd.secret_recipe,
                    validated_host=validated_host,
                    policy_snapshot=policy_snapshot,
                    provider_snapshot=provider_snapshot,
                    revoked_event=cmd.revoked_event,
                    invalidation_reason=lambda: cmd.invalidation_reason,
                )
            )

            argv = self._ssh_command_argv(
                session,
                material,
                validated_host=validated_host,
                remote_command=remote_argv,
            )
            use_stdin_pipe = stdin_payload is not None or keep_stdin_open
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if use_stdin_pipe else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(material.working_dir) if material.working_dir is not None else None,
                env=self._ssh_env(material),
                start_new_session=True,
            )
            record_ssh_child_pid(material.working_dir, process.pid)
            cmd.process = process
            if cmd.revoked_event.is_set():
                raise ExecutionAuthorizationDenied(
                    cmd.invalidation_reason
                    or "SSH command was terminated because the session or target was revoked."
                )
            if self._shutting_down:
                raise SSHSessionUnavailable("SSH session broker is shutting down.")

            assert process.stdout is not None and process.stderr is not None
            stdout_task = asyncio.create_task(
                self._pump_command_stream_async(process.stdout, cmd, True)
            )
            stderr_task = asyncio.create_task(
                self._pump_command_stream_async(process.stderr, cmd, False)
            )
            wait_task = asyncio.create_task(process.wait())
            if stdin_payload is not None and process.stdin is not None:
                stdin_task = asyncio.create_task(
                    _write_stdin_payload(process, stdin_payload, keep_open=keep_stdin_open)
                )

            revoke_waiter = asyncio.create_task(cmd.revoked_event.wait())
            done, _ = await asyncio.wait(
                {wait_task, revoke_waiter},
                timeout=max(0.1, timeout_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stdin_task is not None and not stdin_task.done():
                stdin_task.cancel()
                await asyncio.gather(stdin_task, return_exceptions=True)

            if cmd.revoked_event.is_set():
                await _kill_async_process_group(
                    process,
                    close_stdin_first=keep_stdin_open,
                    stdin_grace_seconds=3.0,
                    grace_seconds=1.0,
                )
                cmd.status = SSHCommandStatus.REVOKED
                reason = (
                    cmd.invalidation_reason
                    or "SSH command was terminated because the session or target was revoked."
                )
                cmd.error = f"{reason} ({termination_note})"
            elif wait_task not in done:
                await _kill_async_process_group(
                    process,
                    close_stdin_first=keep_stdin_open,
                    stdin_grace_seconds=3.0,
                    grace_seconds=1.0,
                )
                cmd.status = SSHCommandStatus.TIMED_OUT
                cmd.error = (
                    f"SSH command timed out after {timeout_seconds:.1f}s "
                    f"({termination_note})."
                )
            elif keep_stdin_open:
                _close_async_stdin(process)

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
        except asyncio.CancelledError:
            if stdin_task is not None and not stdin_task.done():
                stdin_task.cancel()
                await asyncio.gather(stdin_task, return_exceptions=True)
            if process is not None and process.returncode is None:
                await _await_shielded(
                    _kill_async_process_group(
                        process,
                        close_stdin_first=keep_stdin_open,
                        stdin_grace_seconds=2.5,
                        grace_seconds=0.5,
                    ),
                    reraise_on_caller_cancel=False,
                )
            for task in (stdout_task, stderr_task, wait_task):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self._finalize_command_buffers(cmd)
            cmd.exit_code = process.returncode if process is not None else None
            cmd.status = (
                SSHCommandStatus.REVOKED
                if cmd.revoked_event.is_set()
                else SSHCommandStatus.FAILED
            )
            cmd.error = (
                cmd.invalidation_reason
                or f"SSH command was interrupted because the broker stopped ({termination_note})."
            )
            cmd.finished_at_utc = self.repository.current_time_utc()
            raise
        except (ExecutionAuthorizationDenied, SSHDisabledError, SSHSettingsUnusableError) as exc:
            if stdin_task is not None and not stdin_task.done():
                stdin_task.cancel()
                await asyncio.gather(stdin_task, return_exceptions=True)
            if process is not None and process.returncode is None:
                await _kill_async_process_group(
                    process,
                    close_stdin_first=keep_stdin_open,
                    stdin_grace_seconds=2.5,
                    grace_seconds=0.5,
                )
            self._finalize_command_buffers(cmd)
            cmd.status = SSHCommandStatus.REVOKED
            cmd.error = cmd.scrubber.scrub_text(str(exc))
            cmd.finished_at_utc = self.repository.current_time_utc()
        except Exception as exc:
            if stdin_task is not None and not stdin_task.done():
                stdin_task.cancel()
                await asyncio.gather(stdin_task, return_exceptions=True)
            if process is not None and process.returncode is None:
                await _kill_async_process_group(
                    process,
                    close_stdin_first=keep_stdin_open,
                    stdin_grace_seconds=2.5,
                    grace_seconds=0.5,
                )
            self._finalize_command_buffers(cmd)
            cmd.status = SSHCommandStatus.FAILED
            cmd.error = cmd.scrubber.scrub_text(str(exc))
            cmd.finished_at_utc = self.repository.current_time_utc()
        finally:
            if stdin_task is not None and not stdin_task.done():
                stdin_task.cancel()
                await asyncio.gather(stdin_task, return_exceptions=True)
            if revoke_waiter is not None and not revoke_waiter.done():
                revoke_waiter.cancel()
                await asyncio.gather(revoke_waiter, return_exceptions=True)
            if process is not None:
                _close_async_stdin(process)
            cmd.process = None
            if material_context is not None:
                try:
                    await _await_shielded(
                        asyncio.to_thread(material_context.__exit__, None, None, None),
                        reraise_on_caller_cancel=False,
                    )
                except BaseException:
                    self.logger.exception(
                        "Failed to clean up SSH credential material after command. "
                        "event=ssh_command_cleanup_failed session_id=%s command_id=%s",
                        cmd.session_id,
                        cmd.command_id,
                    )
            cmd.done_event.set()

    async def _pump_command_stream_async(
        self,
        stream: asyncio.StreamReader,
        cmd: _LiveSSHCommand,
        is_stdout: bool,
    ) -> None:
        raw_buf = cmd.raw_stdout if is_stdout else cmd.raw_stderr
        scrubbed_buf = cmd.scrubbed_stdout if is_stdout else cmd.scrubbed_stderr
        stream_scrubber = cmd.stdout_stream if is_stdout else cmd.stderr_stream

        def _append_scrubbed(emitted: bytes) -> None:
            if not emitted:
                return
            scrub_cap = self.command_output_limit_bytes - len(scrubbed_buf)
            if scrub_cap > 0:
                scrubbed_buf.extend(emitted[:scrub_cap])

        read_task: asyncio.Task[bytes] | None = None
        try:
            while True:
                if read_task is None:
                    read_task = asyncio.create_task(stream.read(4096))
                quiet_wait = stream_scrubber.time_until_quiet_release()
                if quiet_wait is None:
                    chunk = await read_task
                    read_task = None
                else:
                    done, _ = await asyncio.wait(
                        {read_task},
                        timeout=max(0.0, quiet_wait) + 0.002,
                    )
                    if not done:
                        _append_scrubbed(stream_scrubber.release_quiet())
                        continue
                    chunk = read_task.result()
                    read_task = None

                if not chunk:
                    _append_scrubbed(stream_scrubber.flush())
                    break

                remaining_cap = self.command_output_limit_bytes - len(raw_buf)
                if remaining_cap > 0:
                    raw_buf.extend(chunk[:remaining_cap])
                if len(chunk) > remaining_cap or len(raw_buf) >= self.command_output_limit_bytes:
                    if is_stdout:
                        cmd.stdout_truncated = True
                    else:
                        cmd.stderr_truncated = True
                _append_scrubbed(stream_scrubber.feed(chunk))
        finally:
            if read_task is not None and not read_task.done():
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)

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

    async def _terminate_session_commands(
        self,
        session_id: str,
        *,
        reason: str | None = None,
    ) -> int:
        for step in list(self._active_steps):
            if step.session.session_id == session_id:
                if reason:
                    step.invalidation_reason = reason
                step.revoked_event.set()
        cmd_ids = tuple(self._session_commands.get(session_id, ()))
        tasks: list[asyncio.Task[None]] = []
        terminated = 0
        for cmd_id in cmd_ids:
            cmd = self._commands.get(cmd_id)
            if cmd is None or cmd.status is not SSHCommandStatus.RUNNING:
                continue
            terminated += 1
            if reason:
                cmd.invalidation_reason = reason
            cmd.revoked_event.set()
            if cmd.runner_task is not None and not cmd.runner_task.done():
                tasks.append(cmd.runner_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return terminated

    # -- close, shutdown, and live revocation ------------------------------------

    async def shutdown(self) -> None:
        """Stop all running commands, cancel active steps, and close every live PTY session."""

        self._shutting_down = True
        monitor = self._monitor_task
        self._monitor_task = None
        if monitor is not None and not monitor.done() and monitor is not asyncio.current_task():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

        shutdown_reason = "SSH operation was interrupted because the broker shut down."
        for step in list(self._active_steps):
            step.invalidation_reason = shutdown_reason
            step.revoked_event.set()

        cmd_tasks: list[asyncio.Task[None]] = []
        for cmd in list(self._commands.values()):
            if cmd.status is SSHCommandStatus.RUNNING:
                cmd.invalidation_reason = shutdown_reason
                cmd.revoked_event.set()
                if cmd.runner_task is not None and not cmd.runner_task.done():
                    cmd_tasks.append(cmd.runner_task)
        if cmd_tasks:
            await asyncio.gather(*cmd_tasks, return_exceptions=True)

        session_ids = tuple(self._sessions.keys())
        for sid in session_ids:
            try:
                await self._close(session_id=sid, disconnected=True)
            except Exception:
                self.logger.exception(
                    "Failed to close live SSH session during broker shutdown. "
                    "event=ssh_broker_shutdown_close_failed session_id=%s",
                    sid,
                )
        in_flight_closes = tuple(self._closing_tasks.values())
        if in_flight_closes:
            await asyncio.gather(*in_flight_closes, return_exceptions=True)
        self.logger.info(
            "Shut down SSH session broker. event=ssh_broker_shutdown closed_sessions=%d stopped_commands=%d",
            len(session_ids),
            len(cmd_tasks),
        )

    async def close(
        self,
        *,
        session_id: str,
        disconnected: bool = False,
        caller: SSHCallerContext | None = None,
    ) -> InteractiveSessionV1:
        """Close the PTY and stop any running commands for this session."""

        if caller is not None:
            live = self._sessions.get(session_id)
            session = (
                live.session
                if live is not None
                else self.repository.get_interactive_session(
                    team_id=self.team_id,
                    session_id=session_id,
                )
            )
            if session is None:
                raise SSHSessionUnavailable(f"SSH session {session_id!r} does not exist.")
            interaction = self.repository.get_ssh_interaction(
                team_id=self.team_id,
                session_id=session_id,
            )
            self.authorization_guard.require_ssh_close(
                session,
                caller=caller,
                interaction=interaction,
            )
        return await self._close(session_id=session_id, disconnected=disconnected)

    async def revoke_session(
        self,
        *,
        session_id: str,
        revoked_by: str = "administrator",
    ) -> InteractiveSessionV1 | None:
        """Revoke one SSH session immediately: persist revocation, kill PTY and commands, save scrubbed transcript."""

        revocation = self.repository.revoke_interactive_session(
            team_id=self.team_id,
            session_id=session_id,
            revoked_by=revoked_by,
        )
        terminated_cmds = await self._terminate_session_commands(
            session_id,
            reason="SSH command was terminated because the session was revoked.",
        )
        closed: InteractiveSessionV1 | None = None
        if session_id in self._sessions:
            closed = await self._close(session_id=session_id, disconnected=True)
        elif revocation is not None:
            closed = self.repository.get_interactive_session(
                team_id=self.team_id, session_id=session_id
            )
        if revocation is None and closed is None and terminated_cmds == 0:
            return None
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
        for step in list(self._active_steps):
            if step.session.target.target_id == target_id:
                affected_sessions.add(step.session.session_id)
        terminated_cmds = 0
        for sid in sorted(affected_sessions):
            terminated_cmds += await self._terminate_session_commands(
                sid,
                reason="SSH command was terminated because the enrolled target was revoked.",
            )
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
        in_flight = self._closing_tasks.get(session_id)
        if in_flight is not None:
            return await _await_shielded(in_flight, reraise_on_caller_cancel=True)
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
        close_task = asyncio.create_task(
            self._finish_close(
                live=live,
                disconnected=disconnected,
            )
        )
        self._closing_tasks[session_id] = close_task

        def _clear_closing_task(done_task: asyncio.Task[InteractiveSessionV1]) -> None:
            if self._closing_tasks.get(session_id) is done_task:
                self._closing_tasks.pop(session_id, None)
            if not done_task.cancelled():
                exc = done_task.exception()
                if exc is not None:
                    self.logger.warning(
                        "SSH session close task finished with error. "
                        "event=ssh_close_task_failed session_id=%s error=%s",
                        session_id,
                        exc,
                    )

        close_task.add_done_callback(_clear_closing_task)
        return await _await_shielded(close_task, reraise_on_caller_cancel=True)

    async def _finish_close(
        self,
        *,
        live: _LiveSSHSession,
        disconnected: bool,
    ) -> InteractiveSessionV1:
        self._detach_live_fd(live)
        try:
            await _kill_process_group(live.process, grace_seconds=5.0)
        finally:
            self._detach_live_fd(live)
            fd = live.master_fd
            if fd >= 0:
                live.master_fd = -1
                try:
                    os.close(fd)
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

    def _has_monitored_work(self) -> bool:
        if self._sessions or self._active_steps:
            return True
        return any(cmd.status is SSHCommandStatus.RUNNING for cmd in self._commands.values())

    def _ensure_monitor_started(self) -> None:
        if self._shutting_down:
            return
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(),
            name=f"ssh-broker-monitor:{self.broker_id}",
        )

    def _record_monitor_failure(
        self,
        *,
        current_failures: int,
        kind: str,
        session_id: str,
        exc: Exception,
        command_id: str | None = None,
    ) -> tuple[int, bool]:
        failures = current_failures + 1
        if failures == 1:
            self.logger.error(
                "SSH broker monitor authorization check failed. "
                "event=ssh_monitor_check_failed kind=%s session_id=%s command_id=%s error=%s",
                kind,
                session_id,
                command_id or "",
                exc,
            )
        should_fail_closed = failures >= _MONITOR_MAX_CONSECUTIVE_FAILURES
        if should_fail_closed:
            self.logger.error(
                "Stopping SSH %s fail-closed after %d consecutive monitor check failures. "
                "event=ssh_monitor_fail_closed kind=%s session_id=%s command_id=%s",
                kind,
                failures,
                kind,
                session_id,
                command_id or "",
            )
        return failures, should_fail_closed

    async def _terminate_session_work(
        self,
        *,
        session_id: str,
        reason: str,
        event: str,
    ) -> None:
        self.logger.info(
            "Stopping live SSH work after operator settings change. "
            "event=%s session_id=%s reason=%s",
            event,
            session_id,
            reason,
        )
        for step in list(self._active_steps):
            if step.session.session_id == session_id:
                step.invalidation_reason = reason
                step.revoked_event.set()
        cmd_tasks: list[asyncio.Task[None]] = []
        for cmd in list(self._commands.values()):
            if cmd.session_id == session_id and cmd.status is SSHCommandStatus.RUNNING:
                cmd.invalidation_reason = reason
                cmd.revoked_event.set()
                if cmd.runner_task is not None and not cmd.runner_task.done():
                    cmd_tasks.append(cmd.runner_task)
        if cmd_tasks:
            await asyncio.gather(*cmd_tasks, return_exceptions=True)
        if session_id in self._sessions:
            try:
                await self._close(session_id=session_id, disconnected=True)
            except Exception:
                self.logger.exception(
                    "Failed to close SSH session after operator settings change. "
                    "event=ssh_policy_reload_close_failed session_id=%s",
                    session_id,
                )

    def _configured_binding_ids(
        self,
        provider: SSHCredentialMaterialProvider,
    ) -> frozenset[str] | None:
        if isinstance(provider, ConfiguredSSHCredentialMaterialProvider):
            return frozenset(provider.materials.keys())
        if isinstance(provider, CompositeSSHCredentialMaterialProvider) and isinstance(
            provider.configured_provider, ConfiguredSSHCredentialMaterialProvider
        ):
            return frozenset(provider.configured_provider.materials.keys())
        return None

    async def enforce_settings_on_live_work(
        self,
        *,
        check_network: bool = False,
        check_pinned_targets: bool = False,
        allowed_pinned_targets: frozenset[PinnedSSHTargetKey] | None = None,
    ) -> None:
        """Re-check live sessions, running commands, and steps once after a settings change."""

        policy_snapshot = self.network_policy
        provider_snapshot = self.material_provider
        broker_allowed_pinned = self.allowed_pinned_targets
        effective_allowed_pinned = (
            allowed_pinned_targets
            if allowed_pinned_targets is not None
            else broker_allowed_pinned
        )
        if check_network:
            self._checked_network_policy = policy_snapshot
        if check_pinned_targets:
            self._checked_material_provider = provider_snapshot
            if allowed_pinned_targets is None or allowed_pinned_targets == broker_allowed_pinned:
                self._checked_allowed_pinned_targets = broker_allowed_pinned
        if (not check_network and not check_pinned_targets) or not self._has_monitored_work():
            return

        sessions_by_id: dict[str, InteractiveSessionV1] = {}
        for sid, live in list(self._sessions.items()):
            sessions_by_id[sid] = live.session
        for cmd in list(self._commands.values()):
            if cmd.status is SSHCommandStatus.RUNNING and cmd.session_id not in sessions_by_id:
                sessions_by_id[cmd.session_id] = cmd.session
        for step in list(self._active_steps):
            if step.session.session_id not in sessions_by_id:
                sessions_by_id[step.session.session_id] = step.session

        configured_bindings = (
            self._configured_binding_ids(provider_snapshot) if check_pinned_targets else None
        )
        effective_policy = policy_snapshot or SSHNetworkPolicy.default()
        network_cache: dict[tuple[str, int], str | None] = {}

        for sid, session in sessions_by_id.items():
            invalidation_reason: str | None = None
            if session.target.target_id is not None:
                if check_network:
                    host_key = (session.target.host, session.target.port)
                    if host_key not in network_cache:
                        try:
                            await asyncio.to_thread(
                                validate_ssh_host,
                                session.target.host,
                                session.target.port,
                                effective_policy,
                                resolver=self.resolver,
                                local_address_checker=self.local_address_checker,
                            )
                        except Exception as exc:
                            network_cache[host_key] = str(exc)
                        else:
                            network_cache[host_key] = None
                    invalidation_reason = network_cache[host_key]
            elif check_pinned_targets:
                pinned_key = self._pinned_target_key(session)
                if (
                    effective_allowed_pinned is not None
                    and pinned_key not in effective_allowed_pinned
                ):
                    invalidation_reason = (
                        "SSH target was removed or changed in execution_control.ssh.targets."
                    )
                elif (
                    configured_bindings is not None
                    and session.target.credential_binding_id not in configured_bindings
                ):
                    invalidation_reason = (
                        "SSH target credential binding was removed from execution_control.ssh.targets."
                    )
            if invalidation_reason is not None:
                await self._terminate_session_work(
                    session_id=sid,
                    reason=invalidation_reason,
                    event="ssh_work_terminated_by_policy_reload",
                )

    async def _fail_closed_all_monitored_work(self, reason: str) -> None:
        for step in list(self._active_steps):
            step.invalidation_reason = reason
            step.revoked_event.set()
        cmd_tasks: list[asyncio.Task[None]] = []
        for cmd in list(self._commands.values()):
            if cmd.status is SSHCommandStatus.RUNNING:
                cmd.invalidation_reason = reason
                cmd.revoked_event.set()
                if cmd.runner_task is not None and not cmd.runner_task.done():
                    cmd_tasks.append(cmd.runner_task)
        if cmd_tasks:
            await asyncio.gather(*cmd_tasks, return_exceptions=True)
        for sid in list(self._sessions.keys()):
            try:
                await self._close(session_id=sid, disconnected=True)
            except Exception:
                self.logger.exception(
                    "Failed to close SSH session after monitor failure. "
                    "event=ssh_monitor_close_failed session_id=%s",
                    sid,
                )

    async def _monitor_loop(self) -> None:
        interval = max(
            0.05,
            min(
                float(self.revocation_poll_interval_seconds),
                max(0.05, float(self.lease_ttl_seconds) / 3),
            ),
        )
        cancelled = False
        consecutive_loop_failures = 0
        try:
            while self._has_monitored_work() and not self._shutting_down:
                await asyncio.sleep(interval)
                if not self._has_monitored_work() or self._shutting_down:
                    break
                try:
                    await self._monitor_tick()
                    consecutive_loop_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_loop_failures += 1
                    if consecutive_loop_failures == 1:
                        self.logger.error(
                            "SSH broker monitor tick failed. "
                            "event=ssh_broker_monitor_failed broker_id=%s error=%s",
                            self.broker_id,
                            exc,
                        )
                    if consecutive_loop_failures >= _MONITOR_MAX_CONSECUTIVE_FAILURES:
                        self.logger.error(
                            "Closing all active SSH work fail-closed after %d consecutive monitor tick failures. "
                            "event=ssh_broker_monitor_fail_closed broker_id=%s",
                            consecutive_loop_failures,
                            self.broker_id,
                        )
                        await self._fail_closed_all_monitored_work(
                            "SSH operation was stopped because authorization monitoring failed repeatedly."
                        )
                        consecutive_loop_failures = 0
        except asyncio.CancelledError:
            cancelled = True
            return
        finally:
            if self._monitor_task is asyncio.current_task():
                self._monitor_task = None
            if not cancelled and not self._shutting_down and self._has_monitored_work():
                self._ensure_monitor_started()

    async def _monitor_tick(self) -> None:
        if self.settings_sync is not None:
            sync_result = self.settings_sync()
            if inspect.isawaitable(sync_result):
                sync_result = await sync_result
            if not sync_result.enabled:
                stop_reason = sync_result.stop_reason or SSH_DISABLED_MESSAGE
                stop_event = sync_result.stop_event or "ssh_broker_disabled_killswitch"
                self.logger.warning(
                    "Stopping all active SSH work. event=%s broker_id=%s reason=%s",
                    stop_event,
                    self.broker_id,
                    stop_reason,
                )
                await self._fail_closed_all_monitored_work(stop_reason)
                return
        network_changed = self.network_policy != self._checked_network_policy
        provider_changed = self.material_provider is not self._checked_material_provider
        pinned_changed = self.allowed_pinned_targets != self._checked_allowed_pinned_targets
        if network_changed or provider_changed or pinned_changed:
            await self.enforce_settings_on_live_work(
                check_network=network_changed,
                check_pinned_targets=provider_changed or pinned_changed,
            )
            if not self._has_monitored_work():
                return
        now = self.repository.current_time_utc()
        revocation_cache: dict[str, tuple[bool, Any]] = {}
        interaction_cache: dict[str, tuple[bool, SSHInteractionRecordV1 | None | Exception]] = {}
        target_cache: dict[str, tuple[bool, EnrolledSSHTargetV1 | None | Exception]] = {}

        def _cached_lookup(
            cache: dict[str, tuple[bool, _T | Exception]],
            key: str,
            loader: Callable[[], _T],
        ) -> _T:
            if key not in cache:
                try:
                    cache[key] = (True, loader())
                except Exception as exc:
                    cache[key] = (False, exc)
            ok, value = cache[key]
            if not ok:
                assert isinstance(value, Exception)
                raise value
            return value  # type: ignore[return-value]

        def _get_revocation(sid: str) -> Any:
            return _cached_lookup(
                revocation_cache,
                sid,
                lambda: self.repository.get_interactive_session_revocation(
                    team_id=self.team_id, session_id=sid
                ),
            )

        def _get_interaction(sid: str) -> SSHInteractionRecordV1 | None:
            return _cached_lookup(
                interaction_cache,
                sid,
                lambda: self.repository.get_ssh_interaction(
                    team_id=self.team_id, session_id=sid
                ),
            )

        def _get_target(target_id: str | None) -> EnrolledSSHTargetV1 | None:
            if target_id is None:
                return None
            return _cached_lookup(
                target_cache,
                target_id,
                lambda: self.repository.get_ssh_target(
                    team_id=self.team_id, target_id=target_id
                ),
            )

        # 1. Check active cancellable steps (bootstrap, sudo probe) via ExecutionAccessGuard.
        for step in list(self._active_steps):
            sid = step.session.session_id
            try:
                self.authorization_guard.require_ssh_capability(
                    step.session,
                    _get_interaction(sid),
                    capability=step.capability,
                    caller=step.caller,
                    enrolled_target=_get_target(step.session.target.target_id),
                    session_revocation=_get_revocation(sid),
                    allow_password_bootstrap_setting=self.allow_password_bootstrap,
                    secret_recipe=step.secret_recipe,
                    now=now,
                )
                step.monitor_failures = 0
            except ExecutionAuthorizationDenied as exc:
                step.monitor_failures = 0
                step.invalidation_reason = str(exc)
                step.revoked_event.set()
            except Exception as exc:
                step.monitor_failures, fail_closed = self._record_monitor_failure(
                    current_failures=step.monitor_failures,
                    kind="step",
                    session_id=sid,
                    exc=exc,
                )
                if fail_closed:
                    step.invalidation_reason = (
                        "SSH operation was stopped because authorization checks could not run."
                    )
                    step.revoked_event.set()

        # 2. Check running tracked commands via ExecutionAccessGuard.
        for cmd in list(self._commands.values()):
            if cmd.status is not SSHCommandStatus.RUNNING:
                continue
            sid = cmd.session_id
            try:
                self.authorization_guard.require_ssh_capability(
                    cmd.session,
                    _get_interaction(sid),
                    capability=cmd.capability,
                    caller=cmd.caller,
                    enrolled_target=_get_target(cmd.target_id),
                    session_revocation=_get_revocation(sid),
                    allow_password_bootstrap_setting=self.allow_password_bootstrap,
                    secret_recipe=cmd.secret_recipe,
                    now=now,
                )
                cmd.monitor_failures = 0
            except ExecutionAuthorizationDenied as exc:
                cmd.monitor_failures = 0
                cmd.invalidation_reason = str(exc)
                cmd.revoked_event.set()
            except Exception as exc:
                cmd.monitor_failures, fail_closed = self._record_monitor_failure(
                    current_failures=cmd.monitor_failures,
                    kind="command",
                    session_id=sid,
                    command_id=cmd.command_id,
                    exc=exc,
                )
                if fail_closed:
                    cmd.invalidation_reason = (
                        "SSH command was stopped because authorization checks could not run."
                    )
                    cmd.revoked_event.set()

        # 3. Check live PTY sessions:
        #    - Static 0.9 operator-pinned targets without delegation bounds close on
        #      explicit session revocation, connect-activation expiry, or lease expiry,
        #      keeping 0.9 behavior where an expired typing approval leaves read open.
        #    - Enrolled targets and bounded delegations close immediately on target/
        #      delegation/session revocation or expiry via ExecutionAccessGuard.
        for sid, live in list(self._sessions.items()):
            session = live.session
            try:
                revocation = _get_revocation(sid)
                interaction = _get_interaction(sid)
                enrolled_target = _get_target(session.target.target_id)
                reason = self._check_live_session_validity(
                    live,
                    enrolled_target=enrolled_target,
                    interaction=interaction,
                    session_revocation=revocation,
                    now=now,
                )
                live.monitor_failures = 0
            except Exception as exc:
                live.monitor_failures, fail_closed = self._record_monitor_failure(
                    current_failures=live.monitor_failures,
                    kind="session",
                    session_id=sid,
                    exc=exc,
                )
                if not fail_closed:
                    continue
                reason = "authorization_check_failed"
            if reason is not None:
                self.logger.info(
                    "Terminating live SSH session after revocation or expiry. "
                    "event=ssh_session_terminated_by_monitor session_id=%s reason=%s",
                    sid,
                    reason,
                )
                try:
                    await self._close(session_id=sid, disconnected=True)
                except Exception:
                    self.logger.exception(
                        "Failed to close invalidated SSH session from broker monitor. "
                        "event=ssh_monitor_close_failed session_id=%s",
                        sid,
                    )

    def _check_live_session_validity(
        self,
        live: _LiveSSHSession,
        *,
        enrolled_target: EnrolledSSHTargetV1 | None,
        interaction: SSHInteractionRecordV1 | None,
        session_revocation: Any,
        now: datetime,
    ) -> str | None:
        session = live.session
        if session_revocation is not None:
            return "session_revoked"
        is_bounded_or_enrolled = (
            session.target.target_id is not None
            or (interaction is not None and interaction.bounds is not None)
        )
        if is_bounded_or_enrolled:
            try:
                self.authorization_guard.require_ssh_connect(
                    session,
                    caller=live.caller,
                    enrolled_target=enrolled_target,
                    session_revocation=session_revocation,
                    interaction=interaction,
                    now=now,
                )
            except ExecutionAuthorizationDenied as exc:
                return str(exc)
            if interaction is not None:
                if interaction.revoked_at_utc is not None:
                    return "delegation_revoked"
                if not interaction.is_active(now=now):
                    return "delegation_expired"
        else:
            # Static 0.9 operator-pinned target without delegation bounds:
            # keep PTY readable even after a legacy typing approval expires.
            if (
                session.connect_activation is not None
                and session.connect_activation.expires_at_utc is not None
                and now >= session.connect_activation.expires_at_utc
            ):
                return "connect_activation_expired"
        expires_at = session.lease_expires_at_utc
        if expires_at is not None and now >= expires_at:
            return "lease_expired"
        return None

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
    "MAX_PTY_WRITE_BYTES",
    "PinnedSSHTargetKey",
    "SSHBundleUploadResult",
    "SSHCommandResult",
    "SSHCommandStatus",
    "SSHCredentialMaterial",
    "SSHCredentialMaterialProvider",
    "SSHHostKeyProbeResult",
    "SSHKeyEnrollResult",
    "SSHSessionBroker",
    "SSHSessionUnavailable",
    "SSHSettingsSyncOutcome",
    "SessionSecretScrubber",
    "StreamSecretScrubber",
    "sweep_stale_ssh_ephemeral_dirs",
]
