"""Runtime-neutral view models and errors for Settings > Tasks > Brokered SSH.

Administrators review enrolled SSH targets and delegated SSH sessions here and
can revoke a target or terminate a session immediately
(``assistant_gateway.execution_ssh``). Every record carries only non-secret
metadata (host, port, username, host-key fingerprint, Sealbox credential key
name, delegation bounds, and status); raw secrets never appear here.
"""

from __future__ import annotations

from dataclasses import dataclass

ADMINISTRATORS_ONLY_TEXT = (
    "Only team administrators can view or revoke brokered SSH targets and delegated sessions."
)
TARGET_NOT_FOUND_TEXT = "That enrolled SSH target was not found in this team."
SESSION_NOT_FOUND_TEXT = "That SSH session was not found in this team."


class ExecutionSSHRefused(Exception):
    """The person is not permitted to inspect or revoke brokered SSH access."""


class ExecutionSSHNotFound(Exception):
    """The requested enrolled SSH target or delegated session does not exist."""


@dataclass(frozen=True)
class EnrolledSSHTargetSummary:
    """Non-secret summary of one enrolled SSH target for the admin Settings UI."""

    target_id: str
    host: str
    port: int
    username: str
    platform: str
    host_key_algorithm: str
    host_key_fingerprint: str
    credential_key: str
    bootstrap_credential_key: str | None
    lifecycle: str
    status: str
    can_revoke: bool
    expires_at_utc: str | None
    revoked_at_utc: str | None
    revoked_by: str | None
    password_bootstrap_completed_at_utc: str | None
    reason: str
    owner_scope_kind: str
    owner_id: str
    created_at_utc: str


@dataclass(frozen=True)
class DelegatedSSHSessionSummary:
    """Non-secret summary of one brokered SSH session and its delegation bounds."""

    session_id: str
    target_id: str | None
    target_label: str
    host: str
    port: int
    username: str
    platform: str
    status: str
    is_active: bool
    can_revoke: bool
    owner_agent_type: str | None
    delegated_agent_type: str | None
    delegated_task_id: str | None
    capabilities: tuple[str, ...]
    expires_at_utc: str | None
    revoked_at_utc: str | None
    revoked_by: str | None
    owner_scope_kind: str
    owner_id: str
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True)
class ExecutionSSHPage:
    """What Settings > Tasks > Brokered SSH shows to the signed-in person."""

    can_manage: bool
    ssh_enabled: bool
    targets: tuple[EnrolledSSHTargetSummary, ...]
    sessions: tuple[DelegatedSSHSessionSummary, ...]


@dataclass(frozen=True)
class ExecutionSSHRevokeResult:
    """Outcome of one administrator Revoke / Terminate action plus fresh page state."""

    kind: str
    resource_id: str
    revoked: bool
    broker_notified: bool
    terminated_live: bool
    message: str
    page: ExecutionSSHPage


__all__ = [
    "ADMINISTRATORS_ONLY_TEXT",
    "SESSION_NOT_FOUND_TEXT",
    "TARGET_NOT_FOUND_TEXT",
    "DelegatedSSHSessionSummary",
    "EnrolledSSHTargetSummary",
    "ExecutionSSHNotFound",
    "ExecutionSSHPage",
    "ExecutionSSHRefused",
    "ExecutionSSHRevokeResult",
]
