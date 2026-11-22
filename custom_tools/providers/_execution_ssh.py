"""Agent-facing SSH tools for ``fn:execution_control``.

Every brokered SSH operation (probing, opening sessions, reading/writing/
resizing/closing PTYs, remote commands, password key bootstrap, broker sudo,
and workspace bundle transfer — for both Sealbox-backed enrolled targets and
operator-pinned disk-key targets) executes inside the ``secure_tool_broker``
process via ``custom_tools.broker_execution_ssh`` using the active task's
signed context. Agent processes never hold SSH key material, never resolve
Sealbox secrets, and never run an in-agent SSH broker.

Manager-only tools (``TeamArchitect``):
- ``probe_execution_ssh_host_key`` (read)
- ``plan_execution_ssh_target`` (write; applies as ``change:execution_ssh_target_enroll`` at full)
- ``create_execution_ssh_session`` (write)
- ``plan_execution_ssh_interaction`` (write; applies as ``change:execution_ssh_interaction`` at full)

Operational SSH tools (callable by ``TeamArchitect`` and by any worker type
holding task access to ``fn:execution_control`` AND satisfying
``ExecutionAccessGuard``'s verified caller / delegation-bounds checks on every
call):
- ``list_execution_ssh_targets`` (read)
- ``list_execution_ssh_sessions`` (read)
- ``open_execution_ssh_session`` (write)
- ``read_execution_ssh_session`` (read)
- ``write_execution_ssh_session`` (write)
- ``resize_execution_ssh_session`` (write)
- ``close_execution_ssh_session`` (write)
- ``run_execution_ssh_command`` (write)
- ``get_execution_ssh_command`` (read)
- ``enroll_execution_ssh_key`` (write)
- ``run_execution_ssh_sudo`` (write)
- ``upload_execution_ssh_bundle`` (write)
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Sequence
from datetime import datetime, timezone
import json
import logging
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Literal

from strands import tool

from access_control.sensitive_paths import is_sensitive_path
from custom_agents.execution_changes import (
    SSHInteractionChangeService,
    SSHTargetEnrollChangeService,
)
from custom_agents.execution_control_service import ExecutionControlService
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_tools import file_access
from custom_tools.broker_execution_ssh import (
    ExecutionSSHBrokerError,
    call_execution_ssh_broker,
)
from custom_tools.file_access import (
    FILE_TOO_LARGE,
    NOT_A_FOLDER,
    NOT_A_REGULAR_FILE,
    PathPolicyError,
)
from custom_tools.providers._runtime import _require_manager_tool
from custom_tools.runtime_context import get_agent_ref
from runtime_hosting.authorization import (
    ExecutionAuthorizationDenied,
    SSH_DISABLED_NEXT_STEP,
    SSHCallerContext,
    SSHCapability,
    SSHDisabledError,
    SSHSettingsUnusableError,
)
from runtime_hosting.execution_contracts import InteractiveSessionV1
from runtime_hosting.ssh import MAX_PTY_WRITE_BYTES, SSHSessionUnavailable
from runtime_hosting.ssh_credentials import InvalidSSHCredentialValueError
from runtime_hosting.ssh_operations import (
    MAX_BUNDLE_FILES,
    MAX_BUNDLE_SINGLE_FILE_BYTES,
    MAX_BUNDLE_TOTAL_BYTES,
)
from storage.execution_control_repository import StaleExecutionFence

logger = logging.getLogger(__name__)
_MAX_BUNDLE_WALK_DEPTH = 32
_PATH_POLICY_INVALID_REASONS = frozenset({FILE_TOO_LARGE, NOT_A_REGULAR_FILE, NOT_A_FOLDER})


def _error_payload(exc: Exception) -> str:
    if isinstance(exc, ExecutionSSHBrokerError):
        return json.dumps(exc.to_payload())
    payload: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, SSHDisabledError):
        payload["error_code"] = "ssh_disabled"
        payload["next_step"] = SSH_DISABLED_NEXT_STEP
    elif isinstance(exc, SSHSettingsUnusableError):
        payload["error_code"] = "ssh_settings_unusable"
        payload["next_step"] = exc.next_step
    elif isinstance(exc, InvalidSSHCredentialValueError):
        payload["error_code"] = "invalid_credential_value"
        payload["credential_key"] = exc.credential_key
        payload["field_name"] = exc.field_name
        payload["next_step"] = (
            f"Ask an administrator to update field {exc.field_name!r} in credential "
            f"{exc.credential_key!r} as a single line without line breaks and save it again, then retry."
        )
    elif isinstance(exc, PathPolicyError):
        reason = getattr(exc, "reason_code", None)
        payload["error_code"] = (
            "invalid_argument" if reason in _PATH_POLICY_INVALID_REASONS else "execution_access_refused"
        )
        if reason:
            payload["reason_code"] = reason
    elif isinstance(exc, StaleExecutionFence):
        payload["error_code"] = "session_lease_lost"
        payload["next_step"] = (
            "The SSH session lease was lost or superseded; inspect session status with "
            "list_execution_ssh_sessions and open a new session if needed."
        )
    elif isinstance(exc, (ExecutionAuthorizationDenied, PermissionError)):
        payload["error_code"] = "execution_access_refused"
    elif isinstance(exc, TimeoutError):
        payload["error_code"] = "execution_timeout"
    elif isinstance(exc, (FileNotFoundError, LookupError)):
        payload["error_code"] = "not_found"
    elif isinstance(exc, SSHSessionUnavailable):
        lowered = str(exc).lower()
        payload["error_code"] = "not_found" if "does not exist" in lowered else "session_unavailable"
    elif isinstance(exc, ValueError):
        payload["error_code"] = "invalid_argument"
    return json.dumps(payload)


def _manager_agent(tool_name: str) -> tuple[Any, str | None]:
    agent = get_agent_ref()
    if agent is None:
        return None, "Agent reference not set"
    manager_error = _require_manager_tool(agent, tool_name)
    if manager_error:
        return None, f"{manager_error} Escalate to {TEAM_ARCHITECT_AGENT_TYPE}."
    return agent, None


def _build_execution_service(agent: Any) -> ExecutionControlService:
    """Build ``ExecutionControlService``, classifying unreadable instance config as unusable SSH settings."""

    from assistant_gateway.config import GatewayInstanceConfig

    configured_path = str(getattr(agent, "instance_config_path", "") or "").strip()
    if not configured_path:
        raise ValueError("Execution control requires an instance config path.")
    try:
        loaded_config = GatewayInstanceConfig.load_for_process(configured_path)
    except Exception as exc:
        config_name = Path(configured_path).name or "instance_config.yaml"
        raise SSHSettingsUnusableError(
            f"Broker SSH settings in {config_name} could not be loaded: {exc}",
            config_path=configured_path,
        ) from exc
    return ExecutionControlService(agent, config=loaded_config)


def _manager_service(tool_name: str) -> tuple[ExecutionControlService | None, str | None]:
    agent, error = _manager_agent(tool_name)
    if error:
        return None, error
    return _build_execution_service(agent), None


def _ssh_service(tool_name: str) -> tuple[ExecutionControlService | None, str | None]:
    """Return ``ExecutionControlService`` for any verified agent (manager or delegated worker)."""

    del tool_name
    agent = get_agent_ref()
    if agent is None:
        return None, "Agent reference not set"
    return _build_execution_service(agent), None


def _preflight_broker_ssh(
    tool_name: str,
    *,
    manager_only: bool = False,
    session_id: str | None = None,
) -> str | None:
    """Run local preflight for a broker-dispatched SSH tool.

    When the instance configuration file cannot be parsed on the agent side
    (for example during an in-place edit or after an operator mistake), let
    the broker's ``ExecutionSSHSettingsSource`` decide under its unified
    grace-period and fail-closed rule.
    """

    if manager_only:
        agent, error = _manager_agent(tool_name)
    else:
        agent = get_agent_ref()
        error = None if agent is not None else "Agent reference not set"
    if error:
        return error
    assert agent is not None
    try:
        service = _build_execution_service(agent)
    except SSHSettingsUnusableError:
        return None
    _require_ssh_enabled(service)
    if session_id is not None:
        service.owned_session(session_id)
    return None


def _require_ssh_enabled(service: ExecutionControlService) -> None:
    """Fail fast on the agent side when operator policy has turned SSH off."""

    if not service.config.execution_control.ssh.enabled:
        raise SSHDisabledError()


def _json_strings(value: str, *, field_name: str) -> tuple[str, ...]:
    if not str(value or "").strip():
        return ()
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{field_name} must be a JSON list of strings.")
    return tuple(parsed)


def _json_dict(value: str, *, field_name: str) -> dict[str, Any] | None:
    if not str(value or "").strip():
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    return dict(parsed)


def _utc(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _parse_owner_scope_literal(
    value: str, *, field_name: str
) -> Literal["user", "team"] | None:
    cleaned = str(value or "").strip().lower()
    if not cleaned:
        return None
    if cleaned == "user":
        return "user"
    if cleaned == "team":
        return "team"
    raise ValueError(f"{field_name} must be 'user' or 'team'.")


def _ssh_caller(service: ExecutionControlService) -> SSHCallerContext:
    return SSHCallerContext(
        agent_type=service.actor.agent.agent_type,
        task_id=service.actor.agent.work_id,
        person_id=service.actor.person_id,
    )


def _enabled_delegation_capabilities(interaction: Any) -> tuple[SSHCapability, ...]:
    if interaction is None:
        return ()
    bounds = interaction.bounds
    if bounds is None:
        return (SSHCapability.TYPING,)
    capabilities: list[SSHCapability] = []
    if bounds.allow_commands:
        capabilities.append(SSHCapability.COMMAND)
    if bounds.allow_typing:
        capabilities.append(SSHCapability.TYPING)
    if bounds.allow_password_bootstrap:
        capabilities.append(SSHCapability.PASSWORD_BOOTSTRAP)
    if bounds.allow_broker_sudo:
        capabilities.append(SSHCapability.BROKER_SUDO)
    if bounds.allow_workspace_transfer:
        capabilities.append(SSHCapability.WORKSPACE_TRANSFER)
    return tuple(capabilities)


def _session_capability_view(
    service: ExecutionControlService,
    session: Any,
    interaction: Any,
    revocation: Any,
    caller: SSHCallerContext,
) -> tuple[bool, bool, str | None]:
    """Derive ``usable_in_this_request`` and ``typing_approved`` directly from ``ExecutionAccessGuard``."""

    guard = service.access_guard()
    enrolled_target = (
        service.repository.get_ssh_target(
            team_id=service.owner.team_id, target_id=session.target.target_id
        )
        if session.target.target_id is not None
        else None
    )
    allow_pw = service.config.execution_control.ssh.allow_password_bootstrap
    enabled = _enabled_delegation_capabilities(interaction)
    usable = False
    first_refusal: str | None = None
    typing_approved = False
    for capability in enabled:
        try:
            guard.require_ssh_capability(
                session,
                interaction,
                capability=capability,
                caller=caller,
                enrolled_target=enrolled_target,
                session_revocation=revocation,
                allow_password_bootstrap_setting=allow_pw,
            )
        except ExecutionAuthorizationDenied as exc:
            if first_refusal is None:
                first_refusal = str(exc)
        else:
            usable = True
            if capability is SSHCapability.TYPING:
                typing_approved = True
    return usable, typing_approved, (None if usable else first_refusal)


def _require_local_capability(
    service: ExecutionControlService,
    session: InteractiveSessionV1,
    *,
    capability: SSHCapability,
    caller: SSHCallerContext,
) -> None:
    """Pre-check delegation capability before scanning workspace files on disk."""

    interaction = service.ssh_interaction(session.session_id)
    revocation = service.repository.get_interactive_session_revocation(
        team_id=service.owner.team_id,
        session_id=session.session_id,
    )
    enrolled_target = (
        service.repository.get_ssh_target(
            team_id=service.owner.team_id,
            target_id=session.target.target_id,
        )
        if session.target.target_id is not None
        else None
    )
    service.access_guard().require_ssh_capability(
        session,
        interaction,
        capability=capability,
        caller=caller,
        enrolled_target=enrolled_target,
        session_revocation=revocation,
        allow_password_bootstrap_setting=service.config.execution_control.ssh.allow_password_bootstrap,
    )


def _archive_prefix_for_request(raw_entry: str, location: file_access.FileLocation) -> PurePosixPath:
    posix = PurePosixPath(raw_entry.replace("\\", "/"))
    if posix.is_absolute() or any(part == ".." for part in posix.parts):
        return PurePosixPath(*location.parts) if location.parts else PurePosixPath()
    cleaned_parts = tuple(part for part in posix.parts if part not in {"", "."})
    return PurePosixPath(*cleaned_parts) if cleaned_parts else PurePosixPath()


def _collect_workspace_bundle_entries(
    agent: Any,
    raw_paths: Sequence[str],
) -> list[tuple[str, bytes]]:
    """Read verified non-sensitive workspace files via ``custom_tools.file_access`` only.

    Enforces ``access_control.sensitive_paths`` (refusing ``.env``, ``.ssh``,
    private keys, etc.), rejects symlinks and non-regular files without
    following links, enforces Cedar folder read access, and stops immediately if
    file count, single-file size, total byte, or depth ceilings are exceeded.
    """

    if not raw_paths:
        raise ValueError("paths_json must contain at least one workspace file or directory path.")

    entries: list[tuple[str, bytes]] = []
    seen_archive_paths: set[str] = set()
    total_bytes = 0

    def _add_file(disk_path: Path, archive_rel: PurePosixPath, file_size: int) -> None:
        nonlocal total_bytes
        if not archive_rel.parts:
            raise ValueError("Bundle archive entry path must not be empty.")
        if is_sensitive_path(archive_rel):
            raise PermissionError(
                f"Refused to bundle sensitive credential or key file: {archive_rel.as_posix()!r}."
            )
        rel_str = archive_rel.as_posix()
        if rel_str in seen_archive_paths:
            raise ValueError(f"Duplicate workspace bundle entry path: {rel_str!r}.")
        if len(entries) >= MAX_BUNDLE_FILES:
            raise ValueError(
                f"Workspace bundle exceeds the maximum file count ({MAX_BUNDLE_FILES})."
            )
        if file_size > MAX_BUNDLE_SINGLE_FILE_BYTES:
            raise ValueError(
                f"File {rel_str!r} ({file_size} bytes) exceeds maximum single file size "
                f"({MAX_BUNDLE_SINGLE_FILE_BYTES} bytes)."
            )
        remaining_total = MAX_BUNDLE_TOTAL_BYTES - total_bytes
        if file_size > remaining_total:
            raise ValueError(
                f"Total workspace bundle size exceeds the maximum limit ({MAX_BUNDLE_TOTAL_BYTES} bytes)."
            )
        max_read = min(MAX_BUNDLE_SINGLE_FILE_BYTES, remaining_total)
        _loc, data = file_access.read_bytes(disk_path, max_bytes=max_read, agent=agent)
        if len(data) > MAX_BUNDLE_SINGLE_FILE_BYTES:
            raise ValueError(
                f"File {rel_str!r} exceeds maximum single file size ({MAX_BUNDLE_SINGLE_FILE_BYTES} bytes)."
            )
        if total_bytes + len(data) > MAX_BUNDLE_TOTAL_BYTES:
            raise ValueError(
                f"Total workspace bundle size exceeds the maximum limit ({MAX_BUNDLE_TOTAL_BYTES} bytes)."
            )
        seen_archive_paths.add(rel_str)
        total_bytes += len(data)
        entries.append((rel_str, data))

    def _walk_dir(dir_path: Path, prefix: PurePosixPath, depth: int) -> None:
        if depth > _MAX_BUNDLE_WALK_DEPTH:
            raise ValueError(
                f"Workspace bundle directory nesting exceeds limit ({_MAX_BUNDLE_WALK_DEPTH})."
            )
        _dir_loc, children = file_access.list_directory(dir_path, agent=agent)
        for child_name, child_stat in sorted(children, key=lambda item: item[0]):
            child_rel = (prefix / child_name) if prefix.parts else PurePosixPath(child_name)
            if is_sensitive_path(child_rel):
                raise PermissionError(
                    f"Refused to bundle sensitive credential or key path: {child_rel.as_posix()!r}."
                )
            if stat.S_ISLNK(child_stat.st_mode):
                raise PermissionError(
                    f"Symbolic links cannot be uploaded in an SSH bundle: {child_rel.as_posix()!r}."
                )
            child_disk = dir_path / child_name
            if stat.S_ISDIR(child_stat.st_mode):
                _walk_dir(child_disk, child_rel, depth + 1)
            elif stat.S_ISREG(child_stat.st_mode):
                _add_file(child_disk, child_rel, child_stat.st_size)
            else:
                raise PermissionError(
                    f"Non-regular file cannot be uploaded in an SSH bundle: {child_rel.as_posix()!r}."
                )

    for raw_item in raw_paths:
        cleaned = str(raw_item or "").strip()
        if not cleaned:
            raise ValueError("paths_json entries must be non-empty workspace paths.")
        raw_posix = PurePosixPath(cleaned.replace("\\", "/"))
        if is_sensitive_path(raw_posix):
            raise PermissionError(
                f"Refused to bundle sensitive credential or key path: {cleaned!r}."
            )
        location, entry_stat = file_access.status(cleaned, agent=agent)
        if entry_stat is None:
            raise FileNotFoundError(f"Workspace path not found: {cleaned!r}.")
        if stat.S_ISLNK(entry_stat.st_mode):
            raise PermissionError(
                f"Symbolic links cannot be uploaded in an SSH bundle: {cleaned!r}."
            )
        prefix = _archive_prefix_for_request(cleaned, location)
        if stat.S_ISREG(entry_stat.st_mode):
            archive_rel = prefix if prefix.parts else PurePosixPath(location.path.name)
            _add_file(location.path, archive_rel, entry_stat.st_size)
        elif stat.S_ISDIR(entry_stat.st_mode):
            if prefix.parts and is_sensitive_path(prefix):
                raise PermissionError(
                    f"Refused to bundle sensitive credential or key path: {prefix.as_posix()!r}."
                )
            _walk_dir(location.path, prefix, 0)
        else:
            raise PermissionError(
                f"Non-regular file cannot be uploaded in an SSH bundle: {cleaned!r}."
            )

    if not entries:
        raise ValueError("No files were found in the requested workspace paths.")
    return entries


# -- Manager-only discovery, enrollment, session creation, and delegation -----


@tool
async def probe_execution_ssh_host_key(
    host: str,
    port: int = 22,
    timeout_seconds: int = 10,
) -> str:
    """Probe a remote SSH host's ed25519 host key from the broker without authenticating.

    Enforces operator network guardrails (allowed_cidrs, allowed_domains,
    denied_cidrs) before contacting the host and returns the algorithm,
    SHA256 fingerprint, public key, and commands an administrator can run on
    the target host to confirm the key out-of-band before enrollment.
    """
    try:
        error = _preflight_broker_ssh("probe_execution_ssh_host_key", manager_only=True)
        if error:
            return json.dumps({"error": error})
        result = await call_execution_ssh_broker(
            "probe_execution_ssh_host_key",
            {"host": host, "port": port, "timeout_seconds": timeout_seconds},
            reason=f"Probe SSH host key for {host}:{port}",
            timeout_seconds=float(timeout_seconds),
        )
        return json.dumps(result)
    except Exception as exc:
        return _error_payload(exc)


@tool
async def plan_execution_ssh_target(
    host: str,
    username: str,
    platform: str,
    credential_key: str,
    reason: str,
    port: int = 22,
    target_id: str = "",
    expected_host_key_fingerprint: str = "",
    credential_owner_scope: str = "user",
    credential_public_key_fingerprint: str = "",
    bootstrap_credential_key: str = "",
    bootstrap_credential_owner_scope: str = "",
    lifecycle: str = "temporary",
    expires_at_utc: str = "",
) -> str:
    """Stage enrolling a pinned Sealbox-backed SSH target; apply with apply_change.

    Validates operator network guardrails, runs a trusted host-key probe in the
    secure tool broker (never trusting an agent-typed host key), pins the
    observed ed25519 host key and non-secret Sealbox credential metadata, and
    stages an execution_ssh_target_enroll change for an administrator to review
    in the Admin Inbox. Optional expected_host_key_fingerprint is verified
    against the live broker probe and refused if it differs. platform must be
    'linux', 'macos', or 'windows'; lifecycle must be 'temporary' (requires
    expires_at_utc) or 'persistent'.
    """
    try:
        agent, error = _manager_agent("plan_execution_ssh_target")
        if error:
            return json.dumps({"error": error})
        staged = await asyncio.to_thread(
            SSHTargetEnrollChangeService(agent).stage,
            host=host,
            port=port,
            username=username,
            platform=platform,
            credential_key=credential_key,
            reason=reason,
            target_id=target_id or None,
            expected_host_key_fingerprint=expected_host_key_fingerprint or None,
            credential_owner_scope=_parse_owner_scope_literal(
                credential_owner_scope, field_name="credential_owner_scope"
            ),
            credential_public_key_fingerprint=credential_public_key_fingerprint or None,
            bootstrap_credential_key=bootstrap_credential_key or None,
            bootstrap_credential_owner_scope=_parse_owner_scope_literal(
                bootstrap_credential_owner_scope,
                field_name="bootstrap_credential_owner_scope",
            ),
            lifecycle=lifecycle,
            expires_at_utc=(
                _utc(expires_at_utc, field_name="expires_at_utc")
                if str(expires_at_utc or "").strip()
                else None
            ),
        )
        return json.dumps(staged)
    except Exception as exc:
        return _error_payload(exc)


@tool
def create_execution_ssh_session(
    target_id: str,
    terminal_rows: int = 24,
    terminal_columns: int = 80,
    confirmed: bool = False,
) -> str:
    """Create a session for one installed or enrolled SSH target that you can open, read and resize.

    Remote commands, typing, password bootstrap, broker sudo, and workspace
    bundle upload each need an administrator's bounded approval for the session:
    call plan_execution_ssh_interaction, then apply_change. With authorization
    off, get the person's agreement first and pass confirmed=true.
    """
    try:
        service, error = _manager_service("create_execution_ssh_session")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        session = service.create_ssh_session(
            target_id=target_id,
            terminal_rows=terminal_rows,
            terminal_columns=terminal_columns,
            agreement_confirmed=confirmed,
        )
        return session.model_dump_json()
    except Exception as exc:
        return _error_payload(exc)


@tool
def plan_execution_ssh_interaction(
    session_id: str,
    reason: str,
    ttl_seconds: int = 0,
    expires_at_utc: str = "",
    delegated_agent_type: str = "",
    this_request_only: bool | None = None,
    allow_commands: bool = False,
    allow_typing: bool = False,
    allow_password_bootstrap: bool = False,
    allow_broker_sudo: bool = False,
    allow_workspace_transfer: bool = False,
    allowed_secret_recipes_json: str = "",
) -> str:
    """Stage time-bounded SSH capabilities and optional single-request worker delegation; apply with apply_change.

    Requires ttl_seconds (duration from administrator approval, capped by
    execution_control.ssh.max_delegation_ttl_seconds) and/or expires_at_utc
    (ISO 8601 UTC hard deadline), plus at least one explicit capability flag.
    When this_request_only is omitted, it defaults to true when
    delegated_agent_type is set (single-request worker delegation) and false
    otherwise (usable across follow-up messages from the same person on this
    session until it expires or is revoked). Pass this_request_only=true or
    this_request_only=false to override that default. An administrator approves
    the exact bounds in the Inbox.
    """
    try:
        agent, error = _manager_agent("plan_execution_ssh_interaction")
        if error:
            return json.dumps({"error": error})
        staged = SSHInteractionChangeService(agent).stage(
            session_id=session_id,
            reason=reason,
            expires_at_utc=(
                _utc(expires_at_utc, field_name="expires_at_utc")
                if str(expires_at_utc or "").strip()
                else None
            ),
            ttl_seconds=ttl_seconds if ttl_seconds > 0 else None,
            delegated_agent_type=delegated_agent_type or None,
            this_request_only=this_request_only,
            allow_commands=allow_commands,
            allow_typing=allow_typing,
            allow_password_bootstrap=allow_password_bootstrap,
            allow_broker_sudo=allow_broker_sudo,
            allow_workspace_transfer=allow_workspace_transfer,
            allowed_secret_recipes=_json_strings(
                allowed_secret_recipes_json,
                field_name="allowed_secret_recipes_json",
            ),
        )
        return json.dumps(staged)
    except Exception as exc:
        return _error_payload(exc)


# -- Operational SSH tools (TeamArchitect and authorized delegated workers) ---


@tool
def list_execution_ssh_targets() -> str:
    """List installed and enrolled SSH targets without exposing key-file paths or secrets."""
    try:
        service, error = _ssh_service("list_execution_ssh_targets")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        targets = service.list_ssh_targets()
        return json.dumps({"returned": len(targets), "targets": targets})
    except Exception as exc:
        return _error_payload(exc)


@tool
def list_execution_ssh_sessions() -> str:
    """List owner-scoped SSH sessions, active delegation status, and transcript references."""
    try:
        service, error = _ssh_service("list_execution_ssh_sessions")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        sessions = service.list_ssh_sessions()
        caller = _ssh_caller(service)
        payloads: list[dict[str, Any]] = []
        for item in sessions:
            interaction = service.ssh_interaction(item.session_id)
            revocation = service.repository.get_interactive_session_revocation(
                team_id=service.owner.team_id, session_id=item.session_id
            )
            delegation_active = bool(
                revocation is None and interaction is not None and interaction.is_active()
            )
            usable_in_this_request, typing_approved, unusable_reason = _session_capability_view(
                service, item, interaction, revocation, caller
            )
            entry: dict[str, Any] = {
                **item.model_dump(mode="json"),
                "revoked": revocation is not None,
                "delegation_active": delegation_active,
                "usable_in_this_request": usable_in_this_request,
                "typing_approved": typing_approved,
                "delegation": (
                    interaction.bounds.model_dump(mode="json")
                    if interaction is not None and interaction.bounds is not None
                    else None
                ),
            }
            if delegation_active and not usable_in_this_request and unusable_reason:
                entry["unusable_reason"] = unusable_reason
            payloads.append(entry)
        return json.dumps({"returned": len(payloads), "sessions": payloads})
    except Exception as exc:
        return _error_payload(exc)


@tool
async def open_execution_ssh_session(session_id: str) -> str:
    """Open a created SSH session's PTY using broker-owned identity material."""
    try:
        error = _preflight_broker_ssh("open_execution_ssh_session", session_id=session_id)
        if error:
            return json.dumps({"error": error})
        opened = await call_execution_ssh_broker(
            "open_execution_ssh_session",
            {"session_id": session_id},
            reason=f"Open SSH session {session_id}",
            timeout_seconds=60.0,
        )
        return json.dumps(opened)
    except Exception as exc:
        return _error_payload(exc)


@tool
async def read_execution_ssh_session(
    session_id: str,
    maximum_bytes: int = 65536,
    timeout_seconds: float = 1.0,
) -> str:
    """Read bounded, secret-scrubbed output from a live SSH PTY and renew its fenced lease."""
    try:
        error = _preflight_broker_ssh("read_execution_ssh_session", session_id=session_id)
        if error:
            return json.dumps({"error": error})
        bounded_bytes = max(1, min(int(maximum_bytes), 1_048_576))
        bounded_timeout = max(0.0, min(float(timeout_seconds), 30.0))
        result = await call_execution_ssh_broker(
            "read_execution_ssh_session",
            {
                "session_id": session_id,
                "maximum_bytes": bounded_bytes,
                "timeout_seconds": bounded_timeout,
            },
            reason=f"Read SSH session {session_id}",
            timeout_seconds=bounded_timeout + 15.0,
        )
        return json.dumps(result)
    except Exception as exc:
        return _error_payload(exc)


@tool
async def write_execution_ssh_session(session_id: str, text: str) -> str:
    """Type bounded UTF-8 input (up to 64 KiB per call) into a live SSH PTY; requires an approved delegation with allow_typing.

    Larger files or scripts belong in ``upload_execution_ssh_bundle``.
    """
    try:
        error = _preflight_broker_ssh("write_execution_ssh_session", session_id=session_id)
        if error:
            return json.dumps({"error": error})
        if len(str(text).encode("utf-8")) > MAX_PTY_WRITE_BYTES:
            raise ValueError(
                f"SSH terminal write exceeds the maximum size ({MAX_PTY_WRITE_BYTES} bytes); "
                "use upload_execution_ssh_bundle for larger content."
            )
        result = await call_execution_ssh_broker(
            "write_execution_ssh_session",
            {"session_id": session_id, "text": text},
            reason=f"Write to SSH session {session_id}",
            timeout_seconds=30.0,
        )
        return json.dumps(result)
    except Exception as exc:
        return _error_payload(exc)


@tool
async def resize_execution_ssh_session(
    session_id: str,
    terminal_rows: int,
    terminal_columns: int,
) -> str:
    """Resize a live SSH PTY and renew its fenced lease."""
    try:
        error = _preflight_broker_ssh("resize_execution_ssh_session", session_id=session_id)
        if error:
            return json.dumps({"error": error})
        result = await call_execution_ssh_broker(
            "resize_execution_ssh_session",
            {
                "session_id": session_id,
                "terminal_rows": terminal_rows,
                "terminal_columns": terminal_columns,
            },
            reason=f"Resize SSH session {session_id}",
            timeout_seconds=30.0,
        )
        return json.dumps(result)
    except Exception as exc:
        return _error_payload(exc)


@tool
async def close_execution_ssh_session(session_id: str) -> str:
    """Close a live SSH PTY, stop its running commands, and persist its redacted transcript."""
    try:
        error = _preflight_broker_ssh("close_execution_ssh_session", session_id=session_id)
        if error:
            return json.dumps({"error": error})
        result = await call_execution_ssh_broker(
            "close_execution_ssh_session",
            {"session_id": session_id},
            reason=f"Close SSH session {session_id}",
            timeout_seconds=30.0,
        )
        return json.dumps(result)
    except Exception as exc:
        return _error_payload(exc)


@tool
async def run_execution_ssh_command(
    session_id: str,
    command: str,
    timeout_seconds: float = 120.0,
    wait_seconds: float = 5.0,
) -> str:
    """Run a non-interactive remote command on an approved SSH session (requires allow_commands).

    Waits up to wait_seconds (capped at 60s) and returns command_id, status,
    exit_code, and secret-scrubbed stdout/stderr. If status is still 'running',
    poll with get_execution_ssh_command.

    Process lifecycle notes:
    - On Linux/macOS, timeouts, revocations, or disconnects signal the remote
      command's process group (including nohup/subshell jobs in that group).
      Anything still running 2 seconds after the command's script ends is also
      stopped, including a slow output helper pipeline (so trailing output can
      be missing). A script that logs through a helper (for example
      ``exec > >(while read ... | tee ...)``) should close its output
      descriptors and wait for the helper before exiting
      (``helper=$!; ...; exec >&- 2>&-; wait "$helper"; exit $rc``). Start
      intentional long-lived daemons in a new session (``setsid`` on Linux, or
      ``launchctl`` / a system service on macOS).
    - On Windows, stopping closes the SSH client connection; whether OpenSSH
      for Windows terminates the remote PowerShell child depends on the host's
      Windows OpenSSH configuration, so verify remote state after a timeout or
      revocation.
    """
    try:
        error = _preflight_broker_ssh("run_execution_ssh_command", session_id=session_id)
        if error:
            return json.dumps({"error": error})
        bounded_wait = max(0.0, min(float(wait_seconds), 60.0))
        result = await call_execution_ssh_broker(
            "run_execution_ssh_command",
            {
                "session_id": session_id,
                "command": command,
                "timeout_seconds": timeout_seconds,
                "wait_seconds": bounded_wait,
            },
            reason=f"Run SSH command on session {session_id}",
            timeout_seconds=bounded_wait + 30.0,
        )
        return json.dumps(result)
    except Exception as exc:
        return _error_payload(exc)


@tool
async def get_execution_ssh_command(
    session_id: str,
    command_id: str,
    wait_seconds: float = 0.0,
) -> str:
    """Poll the status and secret-scrubbed output of a running or completed SSH command."""
    try:
        error = _preflight_broker_ssh("get_execution_ssh_command", session_id=session_id)
        if error:
            return json.dumps({"error": error})
        bounded_wait = max(0.0, min(float(wait_seconds), 60.0))
        result = await call_execution_ssh_broker(
            "get_execution_ssh_command",
            {
                "session_id": session_id,
                "command_id": command_id,
                "wait_seconds": bounded_wait,
            },
            reason=f"Poll SSH command {command_id} on session {session_id}",
            timeout_seconds=bounded_wait + 20.0,
        )
        return json.dumps(result)
    except Exception as exc:
        return _error_payload(exc)


@tool
async def enroll_execution_ssh_key(
    session_id: str,
    timeout_seconds: float = 60.0,
) -> str:
    """One-time broker password bootstrap: install the Sealbox ssh_public_key, verify key login, and permanently latch off password bootstrap for this target.

    Requires allow_password_bootstrap on both operator policy and the session's
    approved delegation, plus an enrolled target with a bootstrap credential.
    The broker never exposes the admin password or private key to any agent.
    """
    try:
        error = _preflight_broker_ssh("enroll_execution_ssh_key", session_id=session_id)
        if error:
            return json.dumps({"error": error})
        result = await call_execution_ssh_broker(
            "enroll_execution_ssh_key",
            {"session_id": session_id, "timeout_seconds": timeout_seconds},
            reason=f"Enroll SSH key for session {session_id}",
            timeout_seconds=max(30.0, float(timeout_seconds)),
        )
        return json.dumps(result)
    except Exception as exc:
        return _error_payload(exc)


@tool
async def run_execution_ssh_sudo(
    session_id: str,
    command: str = "",
    secret_recipe: str = "",
    recipe_params_json: str = "",
    extra_secret_field: str = "",
    timeout_seconds: float = 120.0,
    wait_seconds: float = 5.0,
) -> str:
    """Run a root command or admin-approved broker secret recipe over SSH (requires allow_broker_sudo).

    Pass either free ``command`` text (runs under sudo with stdin /dev/null and
    Sealbox admin_password handled only by the broker) OR an admin-approved
    ``secret_recipe`` (for example 'tailscale_up' with optional JSON flags in
    ``recipe_params_json``). Sealbox secrets are never piped to arbitrary
    agent-written commands. Supports async polling via ``get_execution_ssh_command``.

    Process lifecycle notes: on Linux/macOS, stopping (timeout, revoke, or
    disconnect) signals the remote root process group directly; anything still
    running 2 seconds after the script ends is also stopped, including a slow
    output helper pipeline (so trailing output can be missing). Scripts that
    log through a helper should close their output descriptors and wait for the
    helper before exiting (``helper=$!; ...; exec >&- 2>&-; wait "$helper";
    exit $rc``). Start intentional daemons with ``setsid`` (Linux) or
    ``launchctl`` / a system service (macOS).
    """
    try:
        error = _preflight_broker_ssh("run_execution_ssh_sudo", session_id=session_id)
        if error:
            return json.dumps({"error": error})
        recipe_params = _json_dict(recipe_params_json, field_name="recipe_params_json")
        bounded_wait = max(0.0, min(float(wait_seconds), 60.0))
        result = await call_execution_ssh_broker(
            "run_execution_ssh_sudo",
            {
                "session_id": session_id,
                "command": command or None,
                "secret_recipe": secret_recipe or None,
                "recipe_params": recipe_params,
                "extra_secret_field": extra_secret_field or None,
                "timeout_seconds": timeout_seconds,
                "wait_seconds": bounded_wait,
            },
            reason=f"Run broker sudo on SSH session {session_id}",
            timeout_seconds=bounded_wait + 30.0,
        )
        return json.dumps(result)
    except Exception as exc:
        return _error_payload(exc)


@tool
async def upload_execution_ssh_bundle(
    session_id: str,
    paths_json: str,
    remote_destination: str,
    timeout_seconds: float = 120.0,
) -> str:
    """Upload non-sensitive workspace files as a bounded tar.gz archive to a remote directory (requires allow_workspace_transfer).

    ``paths_json`` is a JSON list of workspace file or directory paths. Files
    are read through ``file_access`` without following symlinks, filtered
    against ``access_control.sensitive_paths`` (refusing .env, .ssh, private
    keys, and certificates), and capped at 500 files / 8 MiB per file / 16 MiB
    total.
    """
    try:
        service, error = _ssh_service("upload_execution_ssh_bundle")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        _require_ssh_enabled(service)
        session = service.owned_session(session_id)
        caller = _ssh_caller(service)
        # Verify delegation capability before reading any files from disk.
        _require_local_capability(
            service,
            session,
            capability=SSHCapability.WORKSPACE_TRANSFER,
            caller=caller,
        )
        raw_paths = _json_strings(paths_json, field_name="paths_json")
        entries = await asyncio.to_thread(
            _collect_workspace_bundle_entries,
            service.agent,
            raw_paths,
        )
        encoded_entries = [
            {"path": rel_path, "content_base64": base64.b64encode(data).decode("ascii")}
            for rel_path, data in entries
        ]
        result = await call_execution_ssh_broker(
            "upload_execution_ssh_bundle",
            {
                "session_id": session_id,
                "entries": encoded_entries,
                "remote_destination": remote_destination,
                "timeout_seconds": timeout_seconds,
            },
            reason=f"Upload workspace bundle to SSH session {session_id}",
            timeout_seconds=max(30.0, float(timeout_seconds)),
        )
        return json.dumps(result)
    except Exception as exc:
        return _error_payload(exc)


SSH_TOOLS = (
    probe_execution_ssh_host_key,
    plan_execution_ssh_target,
    list_execution_ssh_targets,
    create_execution_ssh_session,
    open_execution_ssh_session,
    read_execution_ssh_session,
    plan_execution_ssh_interaction,
    write_execution_ssh_session,
    resize_execution_ssh_session,
    close_execution_ssh_session,
    list_execution_ssh_sessions,
    run_execution_ssh_command,
    get_execution_ssh_command,
    enroll_execution_ssh_key,
    run_execution_ssh_sudo,
    upload_execution_ssh_bundle,
)

__all__ = [
    "SSH_TOOLS",
    "_error_payload",
    "_json_strings",
    "_manager_agent",
    "_manager_service",
    "_ssh_caller",
    "_utc",
    "close_execution_ssh_session",
    "create_execution_ssh_session",
    "enroll_execution_ssh_key",
    "get_execution_ssh_command",
    "list_execution_ssh_sessions",
    "list_execution_ssh_targets",
    "open_execution_ssh_session",
    "plan_execution_ssh_interaction",
    "plan_execution_ssh_target",
    "probe_execution_ssh_host_key",
    "read_execution_ssh_session",
    "resize_execution_ssh_session",
    "run_execution_ssh_command",
    "run_execution_ssh_sudo",
    "upload_execution_ssh_bundle",
    "write_execution_ssh_session",
]
