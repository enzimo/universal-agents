"""Settings > Tasks > Brokered SSH: admin review and immediate revocation.

Administrators inspect enrolled SSH targets and active/recent delegated SSH
sessions for their team and can revoke a target or terminate a session with
one click. Authorization is decided exclusively through ``PersonChangeCheck``
against the ``execution_ssh_target_enroll`` and ``execution_ssh_interaction``
change kinds (real Cedar when authorization is enabled; active team members in
trusted single-person mode). Non-administrators receive an empty
``can_manage=False`` view and 403 on revocation attempts, so target hostnames,
fingerprints, and session metadata are never exposed to non-administrators.

Revocation is fail-closed: the revocation is first persisted in
``ExecutionControlRepository`` (so every broker check and the broker's expiry
monitor observe it even if the broker is temporarily unreachable), and then
forwarded to the secure tool broker's gateway-only revocation route so any live
PTY and running remote commands are killed immediately.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from assistant_gateway.application.execution_ssh_page import (
    ADMINISTRATORS_ONLY_TEXT,
    SESSION_NOT_FOUND_TEXT,
    TARGET_NOT_FOUND_TEXT,
    DelegatedSSHSessionSummary,
    EnrolledSSHTargetSummary,
    ExecutionSSHNotFound,
    ExecutionSSHPage,
    ExecutionSSHRefused,
    ExecutionSSHRevokeResult,
)
from assistant_gateway.application.ports import GatewayPersonChangeCheck
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.execution_changes import (
    SSH_INTERACTION_CHANGE_KIND,
    SSH_TARGET_ENROLL_CHANGE_KIND,
)
from runtime_hosting.execution_contracts import (
    EnrolledSSHTargetV1,
    InteractiveSessionStatus,
    InteractiveSessionV1,
    SSHInteractionRecordV1,
    SSHRevocationStateV1,
    SSHTargetStatus,
)
from storage.execution_control_repository import (
    ExecutionControlRepository,
    create_execution_control_repository,
)

logger = logging.getLogger(__name__)

# Always display every active target and active session; bound historical
# inactive rows so the Settings view stays fast as team history grows.
_MAX_INACTIVE_TARGETS = 50
_MAX_INACTIVE_SESSIONS = 50
_ACTIVE_SESSION_STATUSES = frozenset(
    {
        InteractiveSessionStatus.PENDING,
        InteractiveSessionStatus.CONNECTING,
        InteractiveSessionStatus.OPEN,
    }
)


@runtime_checkable
class ExecutionSSHRevokeBroker(Protocol):
    """Minimal broker client port used by ``GatewayExecutionSSH`` for live termination."""

    @property
    def available(self) -> bool: ...

    async def revoke_execution_ssh_target(
        self, *, target_id: str, revoked_by: str
    ) -> dict[str, Any]: ...

    async def revoke_execution_ssh_session(
        self, *, session_id: str, revoked_by: str
    ) -> dict[str, Any]: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat()


def _capabilities_from_interaction(
    interaction: SSHInteractionRecordV1 | None,
) -> tuple[str, ...]:
    if interaction is None:
        return ()
    bounds = interaction.bounds
    if bounds is None:
        # Legacy 0.9 approvals without delegation bounds authorize interactive typing only.
        return ("typing",)
    enabled: list[str] = []
    if bounds.allow_commands:
        enabled.append("commands")
    if bounds.allow_typing:
        enabled.append("typing")
    if bounds.allow_password_bootstrap:
        enabled.append("password_bootstrap")
    if bounds.allow_broker_sudo:
        enabled.append("broker_sudo")
    if bounds.allow_workspace_transfer:
        enabled.append("workspace_transfer")
    return tuple(enabled)


def _summarize_target(
    target: EnrolledSSHTargetV1, *, now: datetime
) -> EnrolledSSHTargetSummary:
    effective_status = target.effective_status(now=now).value
    return EnrolledSSHTargetSummary(
        target_id=target.target_id,
        host=target.host,
        port=target.port,
        username=target.username,
        platform=target.platform.value,
        host_key_algorithm=target.host_key_algorithm,
        host_key_fingerprint=target.host_key_fingerprint,
        credential_key=target.credential_key,
        bootstrap_credential_key=target.bootstrap_credential_key,
        lifecycle=target.lifecycle.value,
        status=effective_status,
        can_revoke=effective_status == SSHTargetStatus.ACTIVE.value,
        expires_at_utc=_iso_or_none(target.expires_at_utc),
        revoked_at_utc=_iso_or_none(target.revoked_at_utc),
        revoked_by=target.revoked_by,
        password_bootstrap_completed_at_utc=_iso_or_none(
            target.password_bootstrap_completed_at_utc
        ),
        reason=target.reason,
        owner_scope_kind=target.owner.scope_kind.value,
        owner_id=target.owner.owner_id,
        created_at_utc=target.created_at_utc.astimezone(timezone.utc).isoformat(),
    )


def _summarize_session(
    session: InteractiveSessionV1,
    *,
    interaction: SSHInteractionRecordV1 | None,
    revocation: SSHRevocationStateV1 | None,
    now: datetime,
) -> DelegatedSSHSessionSummary:
    now_utc = _as_utc(now)
    revoked_at = (
        revocation.revoked_at_utc
        if revocation is not None
        else (interaction.revoked_at_utc if interaction is not None else None)
    )
    revoked_by = (
        revocation.revoked_by
        if revocation is not None
        else (interaction.revoked_by if interaction is not None else None)
    )
    connect_activation = session.connect_activation
    expiry_candidates = [
        _as_utc(candidate)
        for candidate in (
            connect_activation.expires_at_utc if connect_activation is not None else None,
            interaction.activation.expires_at_utc if interaction is not None else None,
        )
        if candidate is not None
    ]
    expires_at = min(expiry_candidates) if expiry_candidates else None
    is_revoked = revoked_at is not None
    is_expired = (
        not is_revoked
        and expires_at is not None
        and expires_at <= now_utc
        and session.status in _ACTIVE_SESSION_STATUSES
    )
    if is_revoked:
        effective_status = "revoked"
    elif is_expired:
        effective_status = "expired"
    else:
        effective_status = session.status.value
    is_active = (
        not is_revoked
        and not is_expired
        and session.status in _ACTIVE_SESSION_STATUSES
    )
    interaction_unrevoked = (
        interaction is not None
        and interaction.revoked_at_utc is None
        and (expires_at is None or expires_at > now_utc)
    )
    can_revoke = not is_revoked and (is_active or interaction_unrevoked)
    bounds = interaction.bounds if interaction is not None else None
    owner_agent_type = (
        (
            (connect_activation.holder.agent_type or "").strip()
            if connect_activation is not None
            else ""
        )
        or (
            (interaction.activation.holder.agent_type or "").strip()
            if interaction is not None
            else ""
        )
        or None
    )
    target = session.target
    return DelegatedSSHSessionSummary(
        session_id=session.session_id,
        target_id=target.target_id,
        target_label=f"{target.username}@{target.host}:{target.port}",
        host=target.host,
        port=target.port,
        username=target.username,
        platform=target.platform.value if target.platform is not None else "unknown",
        status=effective_status,
        is_active=is_active,
        can_revoke=can_revoke,
        owner_agent_type=owner_agent_type,
        delegated_agent_type=bounds.delegated_agent_type if bounds is not None else None,
        delegated_task_id=bounds.delegated_task_id if bounds is not None else None,
        capabilities=_capabilities_from_interaction(interaction),
        expires_at_utc=_iso_or_none(expires_at),
        revoked_at_utc=_iso_or_none(revoked_at),
        revoked_by=revoked_by,
        owner_scope_kind=session.owner.scope_kind.value,
        owner_id=session.owner.owner_id,
        created_at_utc=session.created_at_utc.astimezone(timezone.utc).isoformat(),
        updated_at_utc=session.updated_at_utc.astimezone(timezone.utc).isoformat(),
    )


def _bound_targets(
    targets: list[EnrolledSSHTargetSummary],
) -> tuple[EnrolledSSHTargetSummary, ...]:
    active = sorted(
        (item for item in targets if item.status == SSHTargetStatus.ACTIVE.value),
        key=lambda item: (item.created_at_utc, item.target_id),
        reverse=True,
    )
    inactive = sorted(
        (item for item in targets if item.status != SSHTargetStatus.ACTIVE.value),
        key=lambda item: (item.created_at_utc, item.target_id),
        reverse=True,
    )
    return tuple(active + inactive[:_MAX_INACTIVE_TARGETS])


def _bound_sessions(
    sessions: list[DelegatedSSHSessionSummary],
) -> tuple[DelegatedSSHSessionSummary, ...]:
    active = sorted(
        (item for item in sessions if item.is_active),
        key=lambda item: (item.updated_at_utc, item.session_id),
        reverse=True,
    )
    inactive = sorted(
        (item for item in sessions if not item.is_active),
        key=lambda item: (item.updated_at_utc, item.session_id),
        reverse=True,
    )
    return tuple(active + inactive[:_MAX_INACTIVE_SESSIONS])


class GatewayExecutionSSH:
    """Admin-only view and immediate revocation service for brokered SSH."""

    def __init__(
        self,
        *,
        team_id: str,
        config_path: Path,
        repository: ExecutionControlRepository,
        person_changes: GatewayPersonChangeCheck,
        broker: ExecutionSSHRevokeBroker | None = None,
        default_ssh_enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._team_id = team_id
        self._config_path = config_path
        self._repository = repository
        self._person_changes = person_changes
        self._broker = broker
        self._default_ssh_enabled = bool(default_ssh_enabled)
        self._clock = clock or _utc_now

    @classmethod
    def from_gateway_config(
        cls,
        config: GatewayInstanceConfig,
        *,
        person_changes: GatewayPersonChangeCheck,
        broker: ExecutionSSHRevokeBroker | None = None,
    ) -> "GatewayExecutionSSH":
        return cls(
            team_id=config.manager.team_id,
            config_path=config.config_path,
            repository=create_execution_control_repository(
                config.manager_storage_settings
            ),
            person_changes=person_changes,
            broker=broker,
            default_ssh_enabled=bool(
                config.execution_control.enabled and config.execution_control.ssh.enabled
            ),
        )

    def _can_manage(self, person_id: str) -> bool:
        return bool(
            self._person_changes.may_change(person_id, SSH_TARGET_ENROLL_CHANGE_KIND)
            and self._person_changes.may_change(person_id, SSH_INTERACTION_CHANGE_KIND)
        )

    def _read_ssh_enabled(self) -> bool:
        try:
            saved = GatewayInstanceConfig.load_saved(self._config_path)
            return bool(
                saved.execution_control.enabled and saved.execution_control.ssh.enabled
            )
        except Exception:  # noqa: BLE001 - fall back to startup setting if config unreadable
            return self._default_ssh_enabled

    def page(self, *, person_id: str) -> ExecutionSSHPage:
        """Return enrolled SSH targets and delegated sessions for administrators only."""

        if not self._can_manage(person_id):
            return ExecutionSSHPage(
                can_manage=False,
                ssh_enabled=False,
                targets=(),
                sessions=(),
            )
        now = self._clock()
        raw_targets = self._repository.list_ssh_targets(
            team_id=self._team_id, include_inactive=True
        )
        raw_sessions = self._repository.list_interactive_sessions(team_id=self._team_id)
        interactions = {
            record.activation.resource_id: record
            for record in self._repository.list_ssh_interactions(team_id=self._team_id)
        }
        revocations = self._repository.list_interactive_session_revocations(
            team_id=self._team_id
        )
        bounded_targets = _bound_targets(
            [_summarize_target(target, now=now) for target in raw_targets]
        )
        bounded_sessions = _bound_sessions(
            [
                _summarize_session(
                    session,
                    interaction=interactions.get(session.session_id),
                    revocation=revocations.get(session.session_id),
                    now=now,
                )
                for session in raw_sessions
            ]
        )
        return ExecutionSSHPage(
            can_manage=True,
            ssh_enabled=self._read_ssh_enabled(),
            targets=bounded_targets,
            sessions=bounded_sessions,
        )

    async def revoke_target(
        self, *, person_id: str, target_id: str
    ) -> ExecutionSSHRevokeResult:
        """Revoke one enrolled SSH target and terminate every live session using it."""

        allowed = await asyncio.to_thread(
            self._person_changes.may_change, person_id, SSH_TARGET_ENROLL_CHANGE_KIND
        )
        if not allowed:
            logger.warning(
                "Unauthorized attempt to revoke an enrolled SSH target. "
                "event=gateway_ssh_target_revoke_refused team_id=%s person_id=%s target_id=%s",
                self._team_id,
                person_id,
                target_id,
            )
            raise ExecutionSSHRefused(ADMINISTRATORS_ONLY_TEXT)
        normalized_id = str(target_id or "").strip()
        if not normalized_id:
            raise ExecutionSSHNotFound(TARGET_NOT_FOUND_TEXT)
        revoked = await asyncio.to_thread(
            self._repository.revoke_ssh_target,
            team_id=self._team_id,
            target_id=normalized_id,
            revoked_by=person_id,
        )
        if revoked is None:
            raise ExecutionSSHNotFound(TARGET_NOT_FOUND_TEXT)
        broker_notified = False
        terminated_live = False
        if self._broker is not None and self._broker.available:
            try:
                broker_result = await self._broker.revoke_execution_ssh_target(
                    target_id=normalized_id, revoked_by=person_id
                )
                broker_notified = True
                terminated_live = bool(broker_result.get("terminated_live"))
            except Exception as exc:  # noqa: BLE001 - DB revocation is already durable
                logger.warning(
                    "Could not notify the secure tool broker of an SSH target revocation; "
                    "storage revocation is active. event=gateway_ssh_broker_revoke_notify_failed "
                    "kind=target team_id=%s person_id=%s target_id=%s error=%s",
                    self._team_id,
                    person_id,
                    normalized_id,
                    exc,
                )
        logger.info(
            "Enrolled SSH target revoked by administrator. "
            "event=gateway_ssh_target_revoked team_id=%s person_id=%s target_id=%s "
            "host=%s port=%d username=%s broker_notified=%s terminated_live=%s",
            self._team_id,
            person_id,
            normalized_id,
            revoked.host,
            revoked.port,
            revoked.username,
            broker_notified,
            terminated_live,
        )
        message = (
            f"Revoked enrolled host {revoked.username}@{revoked.host}:{revoked.port}. "
            "Any live sessions and remote commands using it were stopped."
            if broker_notified
            else (
                f"Revoked enrolled host {revoked.username}@{revoked.host}:{revoked.port} in storage. "
                "The broker could not be reached right now; any live session will stop on its next check."
            )
        )
        page = await asyncio.to_thread(self.page, person_id=person_id)
        return ExecutionSSHRevokeResult(
            kind="target",
            resource_id=normalized_id,
            revoked=True,
            broker_notified=broker_notified,
            terminated_live=terminated_live,
            message=message,
            page=page,
        )

    async def revoke_session(
        self, *, person_id: str, session_id: str
    ) -> ExecutionSSHRevokeResult:
        """Revoke and immediately terminate one delegated SSH session."""

        allowed = await asyncio.to_thread(
            self._person_changes.may_change, person_id, SSH_INTERACTION_CHANGE_KIND
        )
        if not allowed:
            logger.warning(
                "Unauthorized attempt to revoke a delegated SSH session. "
                "event=gateway_ssh_session_revoke_refused team_id=%s person_id=%s session_id=%s",
                self._team_id,
                person_id,
                session_id,
            )
            raise ExecutionSSHRefused(ADMINISTRATORS_ONLY_TEXT)
        normalized_id = str(session_id or "").strip()
        if not normalized_id:
            raise ExecutionSSHNotFound(SESSION_NOT_FOUND_TEXT)
        revocation = await asyncio.to_thread(
            self._repository.revoke_interactive_session,
            team_id=self._team_id,
            session_id=normalized_id,
            revoked_by=person_id,
        )
        if revocation is None:
            raise ExecutionSSHNotFound(SESSION_NOT_FOUND_TEXT)
        broker_notified = False
        terminated_live = False
        if self._broker is not None and self._broker.available:
            try:
                broker_result = await self._broker.revoke_execution_ssh_session(
                    session_id=normalized_id, revoked_by=person_id
                )
                broker_notified = True
                terminated_live = bool(broker_result.get("terminated_live"))
            except Exception as exc:  # noqa: BLE001 - DB revocation is already durable
                logger.warning(
                    "Could not notify the secure tool broker of an SSH session revocation; "
                    "storage revocation is active. event=gateway_ssh_broker_revoke_notify_failed "
                    "kind=session team_id=%s person_id=%s session_id=%s error=%s",
                    self._team_id,
                    person_id,
                    normalized_id,
                    exc,
                )
        logger.info(
            "Delegated SSH session revoked by administrator. "
            "event=gateway_ssh_session_revoked team_id=%s person_id=%s session_id=%s "
            "broker_notified=%s terminated_live=%s",
            self._team_id,
            person_id,
            normalized_id,
            broker_notified,
            terminated_live,
        )
        if broker_notified:
            message = (
                f"Terminated live SSH session {normalized_id} and stopped any active remote commands."
                if terminated_live
                else f"Revoked SSH session {normalized_id}."
            )
        else:
            message = (
                f"Revoked SSH session {normalized_id} in storage. "
                "The broker could not be reached right now; any live connection will stop on its next check."
            )
        page = await asyncio.to_thread(self.page, person_id=person_id)
        return ExecutionSSHRevokeResult(
            kind="session",
            resource_id=normalized_id,
            revoked=True,
            broker_notified=broker_notified,
            terminated_live=terminated_live,
            message=message,
            page=page,
        )


__all__ = ["ExecutionSSHRevokeBroker", "GatewayExecutionSSH"]
