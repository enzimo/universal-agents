"""Trusted provenance and exact permits for schedule control-state mutations.

Schedule execution bookkeeping (claims, next-fire calculation, run ledgers) is
runtime data plane activity.  Disabling, pausing, resuming, replacing control
fields, and deleting a system-managed schedule are control-plane mutations and
must carry one exact authority proof into :class:`ScheduleStore`.

Who may issue a permit, by authority mode:

- ``person_approval``: a person approved the exact staged change
  (``system_schedule`` change kind, ``custom_agents.system_schedule_changes``);
- ``agreement``: authorization is off and the person agreed in conversation;
- ``held_access``: the per-call access hook already allowed an agent tool for
  the agent and its person (Objective work-session schedules);
- ``authenticated_gateway_user``: a signed-in person's gateway command;
- ``trusted_runtime``: runtime installation, repair and the operator CLI.

User-owned schedules need no permit; their tools are decided per call and by
the owner check. A permit is evidence for one exact transition, never a grant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Optional

from custom_messages.contracts.access_v2 import ChangeApprovalMode
from custom_messages.contracts.common_v1 import contract_sha256
from custom_messages.enums import ScheduleManagementClass, SystemScheduleKind
from id_generation import new_id

logger = logging.getLogger(__name__)


class ScheduleMutationOperation(StrEnum):
    UPDATE = "update"
    DISABLE = "disable"
    ENABLE = "enable"
    PAUSE = "pause"
    RESUME = "resume"
    DELETE = "delete"


class ScheduleMutationAuthorityMode(StrEnum):
    """Why trusted code may change protected schedule state (module docstring)."""

    PERSON_APPROVAL = "person_approval"
    AGREEMENT = "agreement"
    HELD_ACCESS = "held_access"
    AUTHENTICATED_GATEWAY_USER = "authenticated_gateway_user"
    TRUSTED_RUNTIME = "trusted_runtime"


_APPROVAL_AUTHORITY = {
    ChangeApprovalMode.PERSON_APPROVAL: ScheduleMutationAuthorityMode.PERSON_APPROVAL,
    ChangeApprovalMode.AGREEMENT: ScheduleMutationAuthorityMode.AGREEMENT,
    ChangeApprovalMode.HELD_ACCESS: ScheduleMutationAuthorityMode.HELD_ACCESS,
}


def authority_mode_for_approval(mode: ChangeApprovalMode) -> ScheduleMutationAuthorityMode:
    """The permit mode for an applied change's approval (``access_control.changes``)."""

    try:
        return _APPROVAL_AUTHORITY[mode]
    except KeyError as exc:
        raise PermissionError(f"A {mode.value} approval cannot change a schedule.") from exc


def agent_tool_authority_mode(agent: Any) -> ScheduleMutationAuthorityMode:
    """The permit mode for an agent tool the access hook already allowed.

    With authorization on the hook decided the call for the agent and its
    person (``held_access``); with it off the person's conversational
    agreement stands in (``agreement``, decision D8).
    """

    from custom_agents.access_runtime import runtime_access

    return (
        ScheduleMutationAuthorityMode.HELD_ACCESS
        if runtime_access(agent).enabled
        else ScheduleMutationAuthorityMode.AGREEMENT
    )


class SystemScheduleMutationDenied(PermissionError):
    """Raised when protected schedule control state lacks an exact permit."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = str(reason_code or "system_schedule_mutation_denied")


def is_system_managed_schedule(schedule: Any) -> bool:
    return bool(
        schedule is not None
        and getattr(schedule, "management_class", None)
        == ScheduleManagementClass.SYSTEM
    )


def effective_system_schedule_kind(schedule: Any) -> Optional[str]:
    if not is_system_managed_schedule(schedule):
        return None
    value = getattr(schedule, "system_schedule_kind", None)
    return str(getattr(value, "value", value) or SystemScheduleKind.UNCLASSIFIED.value)


def schedule_control_state_payload(schedule: Any) -> dict[str, Any]:
    """Return only mutation-relevant state, excluding scheduler data-plane churn."""

    payload = getattr(schedule, "payload", None)
    payload_header = getattr(schedule, "payload_header", None)
    payload_meta = getattr(schedule, "payload_meta", None)
    gateway_context = getattr(payload_meta, "gateway_context", None)
    management_class = getattr(schedule, "management_class", None)
    payload_contract = (
        payload.model_dump(mode="json", exclude_none=True)
        if payload is not None and hasattr(payload, "model_dump")
        else payload
    )
    payload_header_contract = (
        payload_header.model_dump(mode="json", exclude_none=True)
        if payload_header is not None and hasattr(payload_header, "model_dump")
        else payload_header
    )
    payload_meta_contract = (
        payload_meta.model_dump(mode="json", exclude_none=True)
        if payload_meta is not None and hasattr(payload_meta, "model_dump")
        else payload_meta
    )
    return {
        "schedule_id": str(getattr(schedule, "schedule_id", "") or ""),
        "schedule_name": getattr(schedule, "schedule_name", None),
        "install_mode": getattr(schedule, "install_mode", None),
        "management_class": str(
            getattr(management_class, "value", management_class) or ""
        ),
        "system_schedule_kind": effective_system_schedule_kind(schedule),
        "objective_id": getattr(schedule, "objective_id", None),
        "claim_team_id": getattr(schedule, "claim_team_id", None),
        "claim_named_agent_type": getattr(schedule, "claim_named_agent_type", None),
        "claim_agent_id": getattr(schedule, "claim_agent_id", None),
        # Preserve the complete execution-bearing contract.  schedule_memory is
        # intentionally separate runtime ledger state and is not included.
        "payload_contract": payload_contract,
        "payload_header_contract": payload_header_contract,
        "payload_meta_contract": payload_meta_contract,
        "instruction": getattr(payload, "problem_statement", None),
        "payload_task_type": str(
            getattr(getattr(payload, "task_type", None), "value", None) or ""
        ),
        "payload_parent_task_id": getattr(payload, "parent_task_id", None),
        "payload_header_from_agent_id": getattr(payload_header, "from_agent_id", None),
        "target_routing_path": str(
            getattr(
                getattr(payload_header, "to_routing_path", None),
                "value",
                getattr(payload_header, "to_routing_path", None),
            )
            or ""
        ),
        "response_routing_key": getattr(payload_header, "response_routing_key", None),
        "from_agent_name": getattr(payload_meta, "from_agent_name", None),
        "to_agent_name": getattr(payload_meta, "to_agent_name", None),
        "target_named_agent_type": getattr(payload_meta, "to_named_agent_type", None),
        "requester_type": str(
            getattr(
                getattr(payload_meta, "requester_type", None),
                "value",
                getattr(payload_meta, "requester_type", None),
            )
            or ""
        ),
        "owner_canonical_user_id": getattr(gateway_context, "canonical_user_id", None),
        "gateway_instance_name": getattr(
            gateway_context, "gateway_instance_name", None
        ),
        "gateway_session_key": getattr(gateway_context, "session_key", None),
        "gateway_channel_name": getattr(gateway_context, "channel_name", None),
        "gateway_user_id": getattr(gateway_context, "user_id", None),
        "gateway_chat_id": getattr(gateway_context, "chat_id", None),
        "gateway_thread_id": getattr(gateway_context, "thread_id", None),
        "delivery_mode": getattr(gateway_context, "delivery_mode", None),
        "schedule_owner_agent_id": getattr(
            gateway_context, "schedule_owner_agent_id", None
        ),
        "heartbeat_source_id": getattr(gateway_context, "heartbeat_source_id", None),
        "interval_ms": getattr(schedule, "interval_ms", None),
        "cron_expression": getattr(schedule, "cron_expression", None),
        "cron_timezone": getattr(schedule, "cron_timezone", None),
        "repeat_count": getattr(schedule, "repeat_count", None),
        "next_fire_at": getattr(schedule, "next_fire_at", None),
        "paused_until": getattr(schedule, "paused_until", None),
        "expires_at_utc": getattr(schedule, "expires_at_utc", None),
        "enabled": bool(getattr(schedule, "enabled", False)),
    }


def schedule_control_state_digest(schedule: Any) -> str:
    return contract_sha256(schedule_control_state_payload(schedule))


# Moved by every run of the schedule; not part of what a person reviews.
_RUN_BOOKKEEPING_FIELDS = frozenset({"next_fire_at"})


def schedule_reviewed_state_digest(schedule: Any) -> str:
    """The schedule as a person reviews it: the control state without its next run time.

    A person's approval of a system-schedule change binds this, so a run
    between staging and approval does not make the approval stale. The store
    permit still binds the exact current state (``schedule_control_state_digest``).
    """

    payload = schedule_control_state_payload(schedule)
    return contract_sha256({key: value for key, value in payload.items() if key not in _RUN_BOOKKEEPING_FIELDS})


def schedule_reviewed_change_digest(
    *,
    schedule: Any,
    operation: "ScheduleMutationOperation | str",
    candidate: Any = None,
    request_binding: Optional[dict[str, Any]] = None,
) -> str:
    """One reviewed change: the reviewed states before and after, the operation and the exact request."""

    return contract_sha256(
        {
            "operation": ScheduleMutationOperation(operation).value,
            "before": schedule_reviewed_state_digest(schedule),
            "after": schedule_reviewed_state_digest(candidate) if candidate is not None else None,
            "request_binding": dict(request_binding or {}),
        }
    )


def schedule_mutation_digest(
    *,
    schedule: Any,
    operation: ScheduleMutationOperation | str,
    candidate: Any = None,
    request_binding: Optional[dict[str, Any]] = None,
) -> str:
    normalized_operation = ScheduleMutationOperation(operation)
    normalized_request_binding = dict(request_binding or {})
    return contract_sha256(
        {
            "schedule_id": str(getattr(schedule, "schedule_id", "") or ""),
            "operation": normalized_operation.value,
            "before_state_digest": schedule_control_state_digest(schedule),
            "after_state_digest": (
                # Derived fields such as next_fire_at can change between
                # staging a change and applying it after approval.  When the
                # caller supplies a stable semantic binding (the exact
                # request), use it for the approval identity.  The store
                # permit still binds the exact concrete candidate through
                # after_state_digest.
                None
                if normalized_request_binding
                else schedule_control_state_digest(candidate)
                if candidate is not None
                else None
            ),
            "request_binding": normalized_request_binding,
        }
    )


@dataclass(frozen=True)
class ScheduleMutationPermit:
    permit_id: str
    authority_mode: ScheduleMutationAuthorityMode
    actor_principal_id: str
    owner_canonical_user_id: str
    team_id: str
    schedule_id: str
    operation: ScheduleMutationOperation
    before_state_digest: str
    after_state_digest: Optional[str]
    mutation_digest: str


def build_schedule_mutation_permit(
    *,
    schedule: Any,
    operation: ScheduleMutationOperation | str,
    authority_mode: ScheduleMutationAuthorityMode | str,
    actor_principal_id: str,
    owner_canonical_user_id: str,
    team_id: str,
    candidate: Any = None,
    request_binding: Optional[dict[str, Any]] = None,
) -> Optional[ScheduleMutationPermit]:
    if not is_system_managed_schedule(schedule):
        return None
    normalized_operation = ScheduleMutationOperation(operation)
    before_digest = schedule_control_state_digest(schedule)
    after_digest = (
        schedule_control_state_digest(candidate) if candidate is not None else None
    )
    return ScheduleMutationPermit(
        permit_id=new_id("sched_permit"),
        authority_mode=ScheduleMutationAuthorityMode(authority_mode),
        actor_principal_id=str(actor_principal_id or "").strip(),
        owner_canonical_user_id=str(owner_canonical_user_id or "").strip(),
        team_id=str(team_id or "").strip(),
        schedule_id=str(getattr(schedule, "schedule_id", "") or "").strip(),
        operation=normalized_operation,
        before_state_digest=before_digest,
        after_state_digest=after_digest,
        mutation_digest=schedule_mutation_digest(
            schedule=schedule,
            operation=normalized_operation,
            candidate=candidate,
            request_binding=request_binding,
        ),
    )


def require_schedule_mutation_permit(
    schedule: Any,
    *,
    operation: ScheduleMutationOperation | str,
    permit: Optional[ScheduleMutationPermit],
    candidate: Any = None,
    request_binding: Optional[dict[str, Any]] = None,
) -> None:
    """Fail closed unless a system mutation permit matches the exact transition."""

    if not is_system_managed_schedule(schedule):
        return
    normalized_operation = ScheduleMutationOperation(operation)
    if permit is None:
        raise SystemScheduleMutationDenied(
            "system_schedule_permit_required",
            "System-managed schedule mutation requires authenticated authority.",
        )
    expected_mutation_digest = schedule_mutation_digest(
        schedule=schedule,
        operation=normalized_operation,
        candidate=candidate,
        request_binding=request_binding,
    )
    payload_meta = getattr(schedule, "payload_meta", None)
    gateway_context = getattr(payload_meta, "gateway_context", None)
    schedule_owner_id = str(
        getattr(gateway_context, "canonical_user_id", None) or ""
    ).strip()
    schedule_team_id = str(getattr(schedule, "claim_team_id", None) or "").strip()
    if (
        permit.schedule_id != str(getattr(schedule, "schedule_id", "") or "")
        or permit.operation != normalized_operation
        or permit.before_state_digest != schedule_control_state_digest(schedule)
        or permit.after_state_digest
        != (schedule_control_state_digest(candidate) if candidate is not None else None)
        or permit.mutation_digest != expected_mutation_digest
        or not permit.actor_principal_id
        or not permit.team_id
        or (schedule_owner_id and permit.owner_canonical_user_id != schedule_owner_id)
        or (schedule_team_id and permit.team_id != schedule_team_id)
    ):
        raise SystemScheduleMutationDenied(
            "system_schedule_permit_mismatch",
            "System-managed schedule mutation permit does not match current state and requested operation.",
        )
    logger.info(
        "System schedule change permitted. event=system_schedule_change_permitted schedule_id=%s "
        "operation=%s authority_mode=%s actor=%s owner=%s permit_id=%s",
        permit.schedule_id,
        permit.operation.value,
        permit.authority_mode.value,
        permit.actor_principal_id,
        permit.owner_canonical_user_id or None,
        permit.permit_id,
    )


__all__ = [
    "ScheduleMutationAuthorityMode",
    "ScheduleMutationOperation",
    "ScheduleMutationPermit",
    "SystemScheduleMutationDenied",
    "agent_tool_authority_mode",
    "authority_mode_for_approval",
    "build_schedule_mutation_permit",
    "effective_system_schedule_kind",
    "is_system_managed_schedule",
    "require_schedule_mutation_permit",
    "schedule_control_state_digest",
    "schedule_mutation_digest",
    "schedule_reviewed_change_digest",
    "schedule_reviewed_state_digest",
]
