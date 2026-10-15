"""One agent change to a schedule, built exactly from its request.

Schedule tools describe what to change as a request: ``ScheduleChangeRequestV1``
(update, pause, resume, disable, remove) or ``WorkerConfigurationRequestV1``
(which worker type runs a schedule; ``custom_agents.schedule_worker_service``
builds that one). Relative times are already exact UTC times in a request, so
building a change from the schedule and the request is deterministic apart
from derived values such as the next run time.

This module finds the schedule, checks that it belongs to the person the task
works for (``access_control.ownership``, with the person from the task's signed
context), builds the exact candidate and returns a ``PreparedScheduleChange``
whose ``commit`` writes it to the schedule store. It never decides authority:
``custom_agents.system_schedule_changes`` decides whether the change needs a
person's approval (schedules the runtime manages) and issues the store's
permit; the access hook already decided the tool call itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from access_control.ownership import (
    PRIVATE_DELETE,
    PRIVATE_UPDATE,
    RESOURCE_SCHEDULE,
    private_access,
)
from custom_agents.schedule_execution_identity import follow_authorized_change
from custom_agents.schedule_fields import (
    apply_schedule_policy_update,
    coerce_schedule_memory,
    schedule_memory_now,
)
from custom_agents.schedule_management import (
    ScheduleMutationOperation,
    ScheduleMutationPermit,
    effective_system_schedule_kind,
    is_system_managed_schedule,
    schedule_control_state_digest,
    schedule_mutation_digest,
    schedule_reviewed_change_digest,
    schedule_reviewed_state_digest,
)
from custom_agents.schedule_store import (
    ScheduleStore,
    compute_next_fire_at,
    fill_cron_timezone,
    owner_canonical_user_id_for_schedule,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage, ScheduleTaskMemory
from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleCapabilityRequirementsV1,
    ScheduleWorkerPolicySpecV1,
)

logger = logging.getLogger(__name__)

ScheduleAction = Literal["update", "pause", "resume", "disable", "remove"]
ConfigureWorkerAction = Literal["configure_worker"]
CONFIGURE_WORKER_ACTION: ConfigureWorkerAction = "configure_worker"
_OPERATIONS: dict[str, ScheduleMutationOperation] = {
    "update": ScheduleMutationOperation.UPDATE,
    "pause": ScheduleMutationOperation.PAUSE,
    "resume": ScheduleMutationOperation.RESUME,
    "disable": ScheduleMutationOperation.DISABLE,
    "remove": ScheduleMutationOperation.DELETE,
    CONFIGURE_WORKER_ACTION: ScheduleMutationOperation.UPDATE,
}
# The result field that says whether each action took effect (the tools'
# long-standing result shape).
RESULT_FLAGS: dict[str, str] = {
    "update": "updated",
    "pause": "paused",
    "resume": "resumed",
    "disable": "disabled",
    "remove": "removed",
    CONFIGURE_WORKER_ACTION: "configured",
}
SCHEDULE_NOT_FOUND = "schedule not found"
REPORT_KEY = "_report"  # before/after snapshots for the tools' manager report; never shown


class ScheduleNotFound(LookupError):
    """No such schedule, or it belongs to someone else (the same answer for both)."""


class NoScheduleChange(ValueError):
    """The update names nothing to change."""


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScheduleUpdateV1(_Request):
    """``update_scheduled_job`` inputs, with timestamps already in UTC.

    Empty strings and negative numbers leave a value unchanged, as in the tool.
    """

    schedule_name: str = ""
    clear_schedule_name: bool = False
    instruction: str = ""
    interval_seconds: int = -1
    cron_expression: str = ""
    # IANA time zone the cron expression is read in; empty keeps the current one
    # (a new cron schedule gets its owner's).
    cron_timezone: str = ""
    next_fire_at: str = ""
    repeat_count: int = -1
    enabled: Optional[bool] = None
    paused_until: str = ""
    clear_pause: bool = False
    expires_at_utc: str = ""
    clear_expiration: bool = False
    schedule_kind: str = ""
    schedule_memory_json: str = ""
    clear_schedule_memory: bool = False
    notify: str = ""
    evidence_required: str = ""
    notify_on_failure: Optional[bool] = None

    @model_validator(mode="after")
    def _one_choice_per_field(self) -> "ScheduleUpdateV1":
        pairs = (
            (self.interval_seconds > 0, bool(self.cron_expression.strip()), "interval_seconds or cron_expression"),
            (bool(self.expires_at_utc), self.clear_expiration, "expires_at_utc or clear_expiration"),
            (bool(self.paused_until), self.clear_pause, "paused_until_iso or clear_pause"),
            (bool(self.schedule_memory_json.strip()), self.clear_schedule_memory, "schedule_memory_json or clear_schedule_memory"),
        )
        for first, second, names in pairs:
            if first and second:
                raise ValueError(f"Provide only one of {names}.")
        if self.cron_timezone and self.interval_seconds > 0:
            raise ValueError("cron_timezone applies to cron schedules, not interval ones.")
        if self.cron_timezone:
            try:
                ZoneInfo(self.cron_timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(
                    f"cron_timezone must be an IANA time zone such as America/Chicago, not {self.cron_timezone!r}."
                ) from exc
        return self


class ScheduleChangeRequestV1(_Request):
    """One schedule tool call: which schedule, what to do, and its exact inputs."""

    kind: Literal["schedule"] = "schedule"
    schedule_id: str = Field(min_length=1, max_length=240)
    action: ScheduleAction
    update: Optional[ScheduleUpdateV1] = None
    paused_until_utc: Optional[str] = None

    @model_validator(mode="after")
    def _inputs_match_action(self) -> "ScheduleChangeRequestV1":
        if (self.action == "update") != (self.update is not None):
            raise ValueError("Only an update carries update fields.")
        if (self.action == "pause") != bool(self.paused_until_utc):
            raise ValueError("A pause needs paused_until_utc, and only a pause has one.")
        return self


class WorkerConfigurationRequestV1(_Request):
    """``configure_scheduled_worker`` inputs; None keeps the saved policy or requirements."""

    kind: Literal["worker_configuration"] = "worker_configuration"
    schedule_id: str = Field(min_length=1, max_length=240)
    action: ConfigureWorkerAction = CONFIGURE_WORKER_ACTION
    target_agent_type: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=0)
    policy: Optional[ScheduleWorkerPolicySpecV1] = None
    capabilities: Optional[ScheduleCapabilityRequirementsV1] = None
    activate_schedule: bool = False
    convert_exact_target: bool = False


ScheduleRequest = ScheduleChangeRequestV1 | WorkerConfigurationRequestV1


def request_binding(request: ScheduleRequest) -> dict[str, Any]:
    """The request as the stable identity of an approved change (schedule_mutation_digest)."""

    return request.model_dump(mode="json")


@dataclass
class PreparedScheduleChange:
    """A change built against the schedule as it is now; ``commit`` performs it.

    ``commit`` takes the store permit (None for a person's own schedule) and
    returns the result shown to the agent.
    """

    action: str
    schedule: ScheduledMessage
    candidate: Optional[ScheduledMessage]
    owner_id: str
    binding: dict[str, Any]
    changes: dict[str, Any]
    commit: Callable[[Optional[ScheduleMutationPermit]], dict[str, Any]]

    @property
    def operation(self) -> ScheduleMutationOperation:
        return _OPERATIONS[self.action]

    @property
    def system_managed(self) -> bool:
        return is_system_managed_schedule(self.schedule)

    @property
    def before_state_digest(self) -> str:
        return schedule_control_state_digest(self.schedule)

    @property
    def mutation_digest(self) -> str:
        return schedule_mutation_digest(
            schedule=self.schedule,
            operation=self.operation,
            candidate=self.candidate,
            request_binding=self.binding,
        )

    @property
    def reviewed_state_digest(self) -> str:
        return schedule_reviewed_state_digest(self.schedule)

    @property
    def reviewed_change_digest(self) -> str:
        return schedule_reviewed_change_digest(
            schedule=self.schedule,
            operation=self.operation,
            candidate=self.candidate,
            request_binding=self.binding,
        )


def owned_schedule(
    store: ScheduleStore, schedule_id: str, person_id: Optional[str], *, action: str = PRIVATE_UPDATE
) -> ScheduledMessage:
    """The person's schedule; ``ScheduleNotFound`` when missing or someone else's."""

    schedule = store.get_scheduled_message(schedule_id)
    decision = private_access(
        person_id=person_id,
        owner_id=owner_canonical_user_id_for_schedule(schedule) if schedule is not None else None,
        resource_type=RESOURCE_SCHEDULE,
        resource_id=schedule_id,
        action=action,
    )
    if schedule is None or not decision.allowed:
        raise ScheduleNotFound(schedule_id)
    return schedule


def schedule_snapshot(schedule: Optional[ScheduledMessage]) -> Optional[dict[str, Any]]:
    """What a person reviewing a schedule change needs to see about the schedule."""

    if schedule is None:
        return None
    gateway_context = schedule.payload_meta.gateway_context if schedule.payload_meta is not None else None
    problem_statement = getattr(schedule.payload, "problem_statement", None)
    kind = effective_system_schedule_kind(schedule)
    return {
        "schedule_id": schedule.schedule_id,
        "schedule_name": schedule.schedule_name,
        "management_class": schedule.management_class.value,
        "system_schedule_kind": kind,
        "enabled": schedule.enabled,
        "paused_until": schedule.paused_until,
        "expires_at_utc": schedule.expires_at_utc,
        "next_fire_at": schedule.next_fire_at,
        "interval_ms": schedule.interval_ms,
        "cron_expression": schedule.cron_expression,
        "cron_timezone": schedule.cron_timezone,
        "repeat_count": schedule.repeat_count,
        "objective_id": schedule.objective_id,
        "claim_team_id": schedule.claim_team_id,
        "claim_named_agent_type": schedule.claim_named_agent_type,
        "claim_agent_id": schedule.claim_agent_id,
        "payload_task_id": getattr(schedule.payload, "task_id", None),
        "problem_statement": problem_statement[:500] if isinstance(problem_statement, str) else problem_statement,
        "gateway_context": gateway_context.model_dump(mode="json") if gateway_context is not None else None,
        "schedule_memory": schedule.schedule_memory.model_dump(mode="json")
        if schedule.schedule_memory is not None
        else None,
    }


# -- building candidates --------------------------------------------------------


def _update_identity(candidate: ScheduledMessage, update: ScheduleUpdateV1, changes: dict[str, Any]) -> None:
    if update.clear_schedule_name:
        candidate.schedule_name = None
        changes["schedule_name"] = None
    elif update.schedule_name.strip():
        candidate.schedule_name = update.schedule_name.strip()
        changes["schedule_name"] = candidate.schedule_name
    if update.instruction.strip():
        candidate.payload.problem_statement = update.instruction.strip()
        changes["instruction"] = "updated"


def _update_timing(
    candidate: ScheduledMessage, update: ScheduleUpdateV1, changes: dict[str, Any], default_zone: Optional[str]
) -> None:
    if update.interval_seconds > 0:
        candidate.interval_ms = update.interval_seconds * 1000
        candidate.cron_expression = None
        candidate.cron_timezone = None
        candidate.next_fire_at = compute_next_fire_at(interval_ms=candidate.interval_ms)
        changes.update(interval_seconds=update.interval_seconds, cron_expression=None, next_fire_at=candidate.next_fire_at)
    elif update.cron_expression.strip() or update.cron_timezone:
        if update.cron_expression.strip():
            candidate.interval_ms = None
            candidate.cron_expression = update.cron_expression.strip()
            changes.update(interval_seconds=None, cron_expression=candidate.cron_expression)
        if not candidate.cron_expression:
            raise ValueError("cron_timezone applies to cron schedules; this schedule has no cron expression.")
        if update.cron_timezone:
            candidate.cron_timezone = update.cron_timezone
        fill_cron_timezone(candidate, default_zone)
        candidate.next_fire_at = compute_next_fire_at(
            cron_expression=candidate.cron_expression, cron_timezone=candidate.cron_timezone
        )
        changes.update(cron_timezone=candidate.cron_timezone, next_fire_at=candidate.next_fire_at)
    if update.next_fire_at:
        candidate.next_fire_at = update.next_fire_at
        changes["next_fire_at"] = candidate.next_fire_at


def _update_controls(candidate: ScheduledMessage, update: ScheduleUpdateV1, changes: dict[str, Any]) -> None:
    if update.enabled is not None:
        candidate.enabled = update.enabled
        changes["enabled"] = candidate.enabled
    if update.clear_pause:
        candidate.paused_until = None
        changes["paused_until"] = None
    elif update.paused_until:
        candidate.paused_until = update.paused_until
        changes["paused_until"] = candidate.paused_until
    if update.clear_expiration:
        candidate.expires_at_utc = None
        changes["expires_at_utc"] = None
    elif update.expires_at_utc:
        candidate.expires_at_utc = update.expires_at_utc
        changes["expires_at_utc"] = candidate.expires_at_utc


def _update_memory(candidate: ScheduledMessage, update: ScheduleUpdateV1, changes: dict[str, Any]) -> None:
    if update.clear_schedule_memory:
        candidate.schedule_memory = None
        changes["schedule_memory"] = None
        return
    if update.schedule_memory_json.strip():
        candidate.schedule_memory = coerce_schedule_memory(
            schedule_memory_json=update.schedule_memory_json,
            schedule_kind=update.schedule_kind,
            notify=update.notify,
            evidence_required=update.evidence_required,
            notify_on_failure=update.notify_on_failure,
        )
        changes["schedule_memory"] = "replaced"
        return
    if not (
        update.schedule_kind.strip()
        or update.notify.strip()
        or update.evidence_required.strip()
        or update.notify_on_failure is not None
    ):
        return
    memory = candidate.schedule_memory = candidate.schedule_memory or ScheduleTaskMemory()
    memory_changes = apply_schedule_policy_update(
        memory,
        notify=update.notify,
        evidence_required=update.evidence_required,
        notify_on_failure=update.notify_on_failure,
    )
    if update.schedule_kind.strip():
        memory.schedule_kind = update.schedule_kind.strip()
        memory_changes["schedule_kind"] = memory.schedule_kind
    if memory_changes:
        memory.updated_at = schedule_memory_now()
        changes["schedule_memory"] = memory_changes


def _updated_candidate(
    schedule: ScheduledMessage, update: ScheduleUpdateV1, default_zone: Optional[str]
) -> tuple[ScheduledMessage, dict[str, Any]]:
    candidate = schedule.model_copy(deep=True)
    changes: dict[str, Any] = {}
    _update_identity(candidate, update, changes)
    _update_timing(candidate, update, changes, default_zone)
    if update.repeat_count >= 0:
        candidate.repeat_count = update.repeat_count if update.repeat_count > 0 else None
        changes["repeat_count"] = candidate.repeat_count
    _update_controls(candidate, update, changes)
    _update_memory(candidate, update, changes)
    if not changes:
        raise NoScheduleChange(schedule.schedule_id)
    return candidate, changes


def _candidate(
    schedule: ScheduledMessage, request: ScheduleChangeRequestV1, default_zone: Optional[str]
) -> tuple[Optional[ScheduledMessage], dict[str, Any]]:
    if request.action == "update":
        assert request.update is not None
        return _updated_candidate(schedule, request.update, default_zone)
    if request.action == "remove":
        return None, {"removed": True}
    candidate = schedule.model_copy(deep=True)
    if request.action == "pause":
        candidate.paused_until = request.paused_until_utc
        return candidate, {"paused_until": request.paused_until_utc}
    if request.action == "resume":
        candidate.paused_until = None
        candidate.enabled = True
        return candidate, {"enabled": True, "paused_until": None}
    candidate.enabled = False
    return candidate, {"enabled": False}


# -- committing -------------------------------------------------------------------


def _remaining_count(store: ScheduleStore, schedule_id: str) -> Any:
    row = store.get_row(schedule_id) or {}
    return row.get("remaining_count")


class _Commit:
    """Writes one prepared change to the store (the store checks any permit)."""

    def __init__(
        self,
        store: ScheduleStore,
        request: ScheduleChangeRequestV1,
        schedule: ScheduledMessage,
        candidate: Optional[ScheduledMessage],
        changes: dict[str, Any],
    ) -> None:
        self.store = store
        self.request = request
        self.schedule = schedule
        self.candidate = candidate
        self.changes = changes
        self.binding = request_binding(request)

    def __call__(self, permit: Optional[ScheduleMutationPermit]) -> dict[str, Any]:
        schedule_id = self.request.schedule_id
        before = schedule_snapshot(self.schedule)
        result = getattr(self, f"_{self.request.action}")(permit)
        after = None if self.request.action == "remove" else schedule_snapshot(self.store.get_scheduled_message(schedule_id))
        flag = RESULT_FLAGS[self.request.action]
        logger.info(
            "Schedule changed by an agent tool. event=schedule_changed_by_agent schedule_id=%s action=%s "
            "management_class=%s applied=%s authority_mode=%s",
            schedule_id,
            self.request.action,
            self.schedule.management_class.value,
            bool(result.get(flag)),
            permit.authority_mode.value if permit is not None else "owner",
        )
        return {"schedule_id": schedule_id, **result, REPORT_KEY: {"before": before, "after": after, "details": self.changes}}

    def _update(self, permit: Optional[ScheduleMutationPermit]) -> dict[str, Any]:
        assert self.candidate is not None and self.request.update is not None
        remaining = (
            self.candidate.repeat_count
            if self.request.update.repeat_count >= 0
            else _remaining_count(self.store, self.request.schedule_id)
        )
        self.store.add(
            self.candidate,
            remaining_count_override=remaining,
            system_mutation_permit=permit,
            system_mutation_operation=ScheduleMutationOperation.UPDATE,
            system_mutation_request_binding=self.binding,
        )
        pruned = self.store.prune_expired()
        after = self.store.get_scheduled_message(self.request.schedule_id)
        # The update ran with the owner's own request or a person's approval,
        # the authority a worker-policy re-save needs, so the saved policy
        # follows; otherwise the next run would wait for the same confirmation.
        follow_authorized_change(
            self.store.worker_records,
            str(self.schedule.claim_team_id or ""),
            self.schedule,
            after,
            source="schedule_update",
        )
        return {
            "updated": after is not None,
            "removed_by_expiration": after is None,
            "changes": self.changes,
            "schedule": schedule_snapshot(after),
            "pruned_expired_rows": pruned,
        }

    def _pause(self, permit: Optional[ScheduleMutationPermit]) -> dict[str, Any]:
        paused = self.store.set_paused_until(
            self.request.schedule_id,
            self.request.paused_until_utc,
            operation=ScheduleMutationOperation.PAUSE,
            system_mutation_permit=permit,
            system_mutation_request_binding=self.binding,
        )
        return {"paused_until": self.request.paused_until_utc, "paused": bool(paused)}

    def _disable(self, permit: Optional[ScheduleMutationPermit]) -> dict[str, Any]:
        disabled = self.store.set_enabled(
            self.request.schedule_id,
            False,
            operation=ScheduleMutationOperation.DISABLE,
            system_mutation_permit=permit,
            system_mutation_request_binding=self.binding,
        )
        return {"disabled": bool(disabled)}

    def _resume(self, permit: Optional[ScheduleMutationPermit]) -> dict[str, Any]:
        if permit is None:
            # A person's own schedule: two targeted updates keep the claim state.
            cleared = self.store.set_paused_until(self.request.schedule_id, None)
            enabled = self.store.set_enabled(self.request.schedule_id, True)
            return {"resumed": bool(cleared or enabled)}
        assert self.candidate is not None
        # One exact permit covers one transition, so a protected schedule is
        # resumed with a single replacement of its control state.
        self.store.add(
            self.candidate,
            remaining_count_override=_remaining_count(self.store, self.request.schedule_id),
            system_mutation_permit=permit,
            system_mutation_operation=ScheduleMutationOperation.RESUME,
            system_mutation_request_binding=self.binding,
        )
        return {"resumed": self.store.get_scheduled_message(self.request.schedule_id) is not None}

    def _remove(self, permit: Optional[ScheduleMutationPermit]) -> dict[str, Any]:
        removed = self.store.remove(
            self.request.schedule_id,
            system_mutation_permit=permit,
            system_mutation_request_binding=self.binding,
        )
        return {"removed": bool(removed)}


def prepare_schedule_change(
    store: ScheduleStore, request: ScheduleChangeRequestV1, *, person_id: Optional[str]
) -> PreparedScheduleChange:
    """Build the change against the person's schedule as it is now."""

    schedule = owned_schedule(
        store,
        request.schedule_id,
        person_id,
        action=PRIVATE_DELETE if request.action == "remove" else PRIVATE_UPDATE,
    )
    candidate, changes = _candidate(schedule, request, store.default_cron_timezone)
    return PreparedScheduleChange(
        action=request.action,
        schedule=schedule,
        candidate=candidate,
        owner_id=str(owner_canonical_user_id_for_schedule(schedule) or ""),
        binding=request_binding(request),
        changes=changes,
        commit=_Commit(store, request, schedule, candidate, changes),
    )


__all__ = [
    "CONFIGURE_WORKER_ACTION",
    "NoScheduleChange",
    "PreparedScheduleChange",
    "REPORT_KEY",
    "RESULT_FLAGS",
    "SCHEDULE_NOT_FOUND",
    "ScheduleAction",
    "ScheduleChangeRequestV1",
    "ScheduleNotFound",
    "ScheduleRequest",
    "ScheduleUpdateV1",
    "WorkerConfigurationRequestV1",
    "owned_schedule",
    "prepare_schedule_change",
    "request_binding",
    "schedule_snapshot",
]
