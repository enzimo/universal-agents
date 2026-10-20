"""Agent changes to schedules, and approved changes to schedules the runtime manages.

Every agent change to a schedule goes through ``change_schedule`` (update,
pause, resume, disable, remove) or ``configure_worker`` (which worker type
runs a schedule). The access hook has already decided the tool call
(``fn:schedules``), and the change is built for the person the task works for
(``custom_agents.schedule_changes``). Then:

- a person's own schedule changes at once; the store needs no permit;
- a schedule the runtime manages (memory review, heartbeat, Objective work
  sessions, ...) is never changed on the agent's say-so. Without an approval
  the change is staged as an exact plan, change kind ``system_schedule``
  (``fn:system_schedules``, a platform tool, so a person always approves;
  ``access_control.changes``), and the tool returns ``approval_required``.
  ``apply_change`` hands the approval to ``SystemScheduleChangeService.apply``,
  which calls the same function again with it: the change is rebuilt from the
  stored request, its before-state and change digests must equal the plan's,
  and trusted code issues the store's exact permit in the approval's mode
  (``person_approval``, or ``agreement`` with authorization off).

A plan binds the schedule, the operation, the digests, the exact request
(with the version of the schedule the model read, ``expected_record_digest``)
and candidate, and the agent, person and task that staged it; it expires after 24
hours and applies once (a receipt records it). A change based on an older
version than the stored one is refused before anything is staged. A schedule whose reviewed state changed
after staging is refused before review and again when applying, and the agent
stages the change again. A run that only moved the next run time does not
count: the approval binds what the person reviewed, and the store permit is
issued for the exact current state.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from access_control.changes import ChangeApproval
from custom_agents.access_runtime import acting_agent, runtime_access, verified_person_id
from custom_agents.governance.authorized_operations import (
    FilePlanStore,
    GovernanceAuditEvent,
    atomic_write_json,
    model_digest,
)
from custom_agents.governance.runtime_adapters import LoggingGovernanceAuditSink
from custom_agents.runtime_paths import resolve_runtime_instance_root
from custom_agents.schedule_changes import (
    CONFIGURE_WORKER_ACTION,
    REPORT_KEY,
    RESULT_FLAGS,
    SCHEDULE_NOT_FOUND,
    NoScheduleChange,
    PreparedScheduleChange,
    ScheduleChangeRequestV1,
    ScheduleNotFound,
    ScheduleRequest,
    WorkerConfigurationRequestV1,
    prepare_schedule_change,
)
from custom_agents.schedule_management import (
    ScheduleMutationOperation,
    authority_mode_for_approval,
    build_schedule_mutation_permit,
    effective_system_schedule_kind,
)
from custom_agents.schedule_store import ScheduleWriteConflict
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeProposalV1, summary_line
from custom_messages.contracts.common_v1 import utc_now
from custom_messages.contracts.runtime_v1 import (
    AUTHORITY_MODE_AUTHORIZATION_DISABLED,
    AUTHORITY_MODE_PERSON_APPROVAL,
)
from custom_messages.contracts.schedule_v1 import SYSTEM_SCHEDULE_CHANGE_KIND, ScheduledMessage
from custom_messages.enums import ScheduleManagementClass
from id_generation import new_id

logger = logging.getLogger(__name__)

SYSTEM_SCHEDULE_PLAN_SCHEMA_VERSION: Literal[1] = 1
SYSTEM_SCHEDULE_PLAN_TTL_SECONDS = 24 * 60 * 60
SYSTEM_SCHEDULE_CHANGE_ID_PREFIX = "sys_sched_chg"
_CHANGE_ID_PATTERN = re.compile(r"sys_sched_chg_[0-9a-f]{12}")
STATUS_APPROVAL_REQUIRED = "approval_required"
_STALE = (
    "The schedule changed after this change was staged (for example it ran, or someone changed it). "
    "Stage the change again."
)
_VERBS = {
    "update": "Change",
    "pause": "Pause",
    "resume": "Resume",
    "disable": "Turn off",
    "remove": "Remove",
    CONFIGURE_WORKER_ACTION: "Set the worker for",
}


class SystemScheduleChangePlan(BaseModel):
    """One staged change to a schedule the runtime manages."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SYSTEM_SCHEDULE_PLAN_SCHEMA_VERSION
    change_id: str
    plan_digest: str = ""
    created_at_utc: datetime
    expires_at_utc: datetime
    actor_principal_id: str
    actor_named_agent_type: str
    team_id: str
    owner_canonical_user_id: str
    source_task_id: str
    authority_mode: Literal["person_approval", "authorization_disabled_agreement"]
    schedule_id: str
    schedule_name: Optional[str] = None
    system_schedule_kind: Optional[str] = None
    operation: ScheduleMutationOperation
    request: Annotated[ScheduleRequest, Field(discriminator="kind")]
    # What the person reviewed (schedule_reviewed_*): runs moving the next run
    # time do not make an approval stale; the store permit binds exact state.
    reviewed_state_digest: str
    reviewed_change_digest: str
    candidate: Optional[ScheduledMessage] = None
    title: str
    effect: tuple[str, ...]

    @field_validator("change_id")
    @classmethod
    def _validate_change_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid system schedule change id.")
        return normalized


# -- the one path for agent schedule changes ----------------------------------------


def change_schedule(
    agent: Any,
    request: ScheduleChangeRequestV1,
    *,
    approval: Optional[ChangeApproval] = None,
    plan: Optional[SystemScheduleChangePlan] = None,
) -> dict[str, Any]:
    """Update, pause, resume, disable or remove one of the person's schedules (module docstring)."""

    person_id = verified_person_id(agent)
    if not person_id:
        return _no_person()
    flag = RESULT_FLAGS[request.action]
    try:
        prepared = prepare_schedule_change(_store(agent), request, person_id=person_id)
    except ScheduleNotFound:
        return {"schedule_id": request.schedule_id, flag: False, "error": SCHEDULE_NOT_FOUND}
    except NoScheduleChange:
        return {"schedule_id": request.schedule_id, flag: False, "message": "No changes provided."}
    return _authorized_commit(agent, prepared, request, approval=approval, plan=plan)


def configure_worker(
    agent: Any,
    request: WorkerConfigurationRequestV1,
    *,
    approval: Optional[ChangeApproval] = None,
    plan: Optional[SystemScheduleChangePlan] = None,
) -> dict[str, Any]:
    """Save which worker type runs one of the person's schedules, and its startup policy."""

    from custom_agents.schedule_worker_service import ScheduleWorkerService

    prepared = ScheduleWorkerService(agent).prepare_configuration(request)
    return _authorized_commit(agent, prepared, request, approval=approval, plan=plan)


def _store(agent: Any) -> Any:
    store = getattr(agent, "_schedule_store", None)
    if store is None:
        raise ValueError("ScheduleStore not initialized on this agent.")
    return store


def _no_person() -> dict[str, Any]:
    return {
        "error": "canonical_user_required",
        "message": "Scheduled jobs are available only for registered users.",
    }


def _authorized_commit(
    agent: Any,
    prepared: PreparedScheduleChange,
    request: ScheduleRequest,
    *,
    approval: Optional[ChangeApproval],
    plan: Optional[SystemScheduleChangePlan],
) -> dict[str, Any]:
    if not prepared.system_managed:
        if approval is not None:
            raise PermissionError("Only changes to schedules the runtime manages are staged for approval.")
        return prepared.commit(None)
    if approval is None or plan is None:
        return SystemScheduleChangeService(agent).stage(prepared, request)
    require_plan_matches(prepared, plan)
    permit = build_schedule_mutation_permit(
        schedule=prepared.schedule,
        operation=prepared.operation,
        authority_mode=authority_mode_for_approval(approval.mode),
        actor_principal_id=plan.actor_principal_id,
        owner_canonical_user_id=prepared.owner_id,
        team_id=plan.team_id,
        candidate=prepared.candidate,
        request_binding=prepared.binding,
    )
    return prepared.commit(permit)


def require_plan_matches(prepared: PreparedScheduleChange, plan: SystemScheduleChangePlan) -> None:
    """The rebuilt change is exactly the staged one, against an unchanged schedule."""

    if (
        prepared.schedule.schedule_id != plan.schedule_id
        or prepared.operation != plan.operation
        or prepared.owner_id != plan.owner_canonical_user_id
    ):
        raise PermissionError("This staged change is for another schedule, operation or person.")
    if (
        prepared.reviewed_state_digest != plan.reviewed_state_digest
        or prepared.reviewed_change_digest != plan.reviewed_change_digest
    ):
        raise PermissionError(_STALE)


# -- describing a change for review ------------------------------------------------------


def _schedule_label(schedule: ScheduledMessage) -> str:
    return f"{schedule.schedule_name or schedule.schedule_id} ({schedule.schedule_id})"


def _change_lines(prepared: PreparedScheduleChange, request: ScheduleRequest) -> list[str]:
    if isinstance(request, WorkerConfigurationRequestV1):
        starts = "on" if (request.policy is None or request.policy.enabled) else "off"
        return [
            f"Runs it with {request.target_agent_type}; automatic worker start stays {starts}.",
            "Turns the schedule on once its known needs are met." if request.activate_schedule else "Does not turn the schedule on or off.",
        ]
    if request.action == "pause":
        return [f"Pauses it until {request.paused_until_utc} (UTC); it runs again after that."]
    if request.action == "resume":
        return ["Clears its pause and turns it back on."]
    if request.action == "disable":
        return ["Turns it off until someone resumes it."]
    if request.action == "remove":
        return ["Deletes it permanently."]
    changed = ", ".join(f"{name}: {value}" for name, value in sorted(prepared.changes.items(), key=lambda item: item[0]))
    return [f"Changes: {changed}"[:1000]]


def _title(prepared: PreparedScheduleChange) -> str:
    return f"{_VERBS[prepared.action]} the system schedule {_schedule_label(prepared.schedule)}"[:400]


def _effect(prepared: PreparedScheduleChange, request: ScheduleRequest) -> tuple[str, ...]:
    kind = effective_system_schedule_kind(prepared.schedule) or "unclassified"
    lines = [
        f"Schedule: {_schedule_label(prepared.schedule)}, which the runtime manages ({kind.replace('_', ' ')}).",
        *_change_lines(prepared, request),
        "If the schedule's settings change before approval, this change is refused and must be "
        "staged again (its regular runs do not count).",
    ]
    return tuple(line[:1000] for line in lines)


class SystemScheduleChangeService:
    """Stage exact changes to schedules the runtime manages and apply approved ones."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.audit = LoggingGovernanceAuditSink()

    def stage(self, prepared: PreparedScheduleChange, request: ScheduleRequest) -> dict[str, Any]:
        actor = acting_agent(self.agent)
        if not actor.person_id or not actor.task_id:
            raise PermissionError("A change to a system schedule is staged from a task for a person.")
        now = utc_now()
        plan = SystemScheduleChangePlan(
            change_id=new_id(SYSTEM_SCHEDULE_CHANGE_ID_PREFIX),
            created_at_utc=now,
            expires_at_utc=now + timedelta(seconds=SYSTEM_SCHEDULE_PLAN_TTL_SECONDS),
            actor_principal_id=actor.agent.agent_id,
            actor_named_agent_type=actor.agent.agent_type,
            team_id=actor.agent.team_id,
            owner_canonical_user_id=actor.person_id,
            source_task_id=actor.task_id,
            authority_mode=self._authority_mode(),
            schedule_id=prepared.schedule.schedule_id,
            schedule_name=prepared.schedule.schedule_name,
            system_schedule_kind=effective_system_schedule_kind(prepared.schedule),
            operation=prepared.operation,
            request=request,
            reviewed_state_digest=prepared.reviewed_state_digest,
            reviewed_change_digest=prepared.reviewed_change_digest,
            candidate=prepared.candidate,
            title=_title(prepared),
            effect=_effect(prepared, request),
        )
        plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
        self._plan_store().save(plan)
        self._audit(plan, "staged")
        person_review = plan.authority_mode == AUTHORITY_MODE_PERSON_APPROVAL
        return {
            "status": STATUS_APPROVAL_REQUIRED,
            "change_id": plan.change_id,
            "schedule_id": plan.schedule_id,
            "management_class": ScheduleManagementClass.SYSTEM.value,
            "system_schedule_kind": plan.system_schedule_kind,
            RESULT_FLAGS[prepared.action]: False,
            "title": plan.title,
            "effect": list(plan.effect),
            "next_step": (
                "Call apply_change(change_id, reason); a person approves this exact change in the Inbox, "
                "then it is applied."
                if person_review
                else "Explain the change, get the person's agreement, then call "
                "apply_change(change_id, reason, confirmed=true)."
            ),
        }

    def load_plan(self, change_id: str) -> SystemScheduleChangePlan:
        plan = self._plan_store().load(str(change_id or "").strip())
        actor = acting_agent(self.agent)
        if plan.plan_digest != model_digest(plan):
            raise PermissionError("System schedule change plan digest mismatch.")
        if plan.expires_at_utc <= utc_now():
            raise PermissionError("This system schedule change has expired. Stage it again.")
        if plan.authority_mode != self._authority_mode():
            raise PermissionError("Authorization was turned on or off after staging. Stage the change again.")
        if (
            plan.actor_principal_id != actor.agent.agent_id
            or plan.team_id != actor.agent.team_id
            or plan.owner_canonical_user_id != (actor.person_id or "")
            or plan.source_task_id != (actor.task_id or "")
        ):
            raise PermissionError("This system schedule change belongs to another agent, person or task.")
        return plan

    def _prepare(self, plan: SystemScheduleChangePlan) -> PreparedScheduleChange:
        request = plan.request
        if isinstance(request, WorkerConfigurationRequestV1):
            from custom_agents.schedule_worker_service import ScheduleWorkerService

            return ScheduleWorkerService(self.agent).prepare_configuration(request)
        try:
            return prepare_schedule_change(
                _store(self.agent), request, person_id=verified_person_id(self.agent)
            )
        except ScheduleNotFound as exc:
            raise PermissionError("The schedule no longer exists. Nothing to change.") from exc
        except (NoScheduleChange, ScheduleWriteConflict) as exc:
            # A schedule that is no longer the version the model read when it
            # staged the change is a stale plan, as the digest check below says.
            raise PermissionError(_STALE) from exc

    def _verify_current(self, plan: SystemScheduleChangePlan) -> None:
        """Not applied yet and the schedule unchanged (before review and again when applying)."""

        if self._receipt_path(plan.change_id).is_file():
            raise FileExistsError(f"System schedule change {plan.change_id} was already applied.")
        require_plan_matches(self._prepare(plan), plan)

    def describe(self, change_id: str) -> ChangeProposalV1:
        plan = self.load_plan(change_id)
        self._verify_current(plan)
        return ChangeProposalV1(
            kind=SYSTEM_SCHEDULE_CHANGE_KIND,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=plan.title,
            # The title is trusted words ("Pause the system schedule ...").
            summary=summary_line(f"{plan.title}. Takes effect when approved."),
            effect=plan.effect,
        )

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Apply an approved change (only the change mechanism calls this)."""

        plan = self.load_plan(change_id)
        agreement = approval.mode is ChangeApprovalMode.AGREEMENT
        if agreement != (plan.authority_mode == AUTHORITY_MODE_AUTHORIZATION_DISABLED):
            raise PermissionError("This change needs a person's approval, not agreement (or the reverse).")
        self._verify_current(plan)
        request = plan.request
        if isinstance(request, WorkerConfigurationRequestV1):
            result = configure_worker(self.agent, request, approval=approval, plan=plan)
        else:
            result = change_schedule(self.agent, request, approval=approval, plan=plan)
        result.pop(REPORT_KEY, None)
        receipt = {
            "schema_version": SYSTEM_SCHEDULE_PLAN_SCHEMA_VERSION,
            "receipt_id": new_id("sys_sched_rcpt"),
            "change_id": plan.change_id,
            "plan_digest": plan.plan_digest,
            "applied_at_utc": utc_now().isoformat(),
            "schedule_id": plan.schedule_id,
            "operation": plan.operation.value,
            "reviewed_change_digest": plan.reviewed_change_digest,
            "approval": approval.record(),
        }
        atomic_write_json(self._receipt_path(plan.change_id), receipt, immutable=True)
        self._audit(plan, "applied")
        return {**result, "authority": {"mode": approval.mode.value, "approval": approval.record()}}

    # -- helpers -----------------------------------------------------------------

    def _audit(self, plan: SystemScheduleChangePlan, phase: str) -> None:
        self.audit.record(
            GovernanceAuditEvent(
                operation=SYSTEM_SCHEDULE_CHANGE_KIND,
                phase=phase,
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=plan.source_task_id,
                authority_mode=plan.authority_mode,
            )
        )

    def _authority_mode(self) -> str:
        return (
            AUTHORITY_MODE_PERSON_APPROVAL
            if runtime_access(self.agent).enabled
            else AUTHORITY_MODE_AUTHORIZATION_DISABLED
        )

    def _root(self) -> Path:
        root = resolve_runtime_instance_root(
            bootstrap_config=dict(getattr(self.agent, "bootstrap_config", {}) or {}),
            instance_config_path=getattr(self.agent, "instance_config_path", None),
            instance_root=getattr(self.agent, "instance_root", None),
            workspace_dir=getattr(self.agent, "workspace_dir", None),
        )
        if not root:
            raise RuntimeError("System schedule changes need a resolvable instance root.")
        return Path(root).expanduser().resolve() / "runtime"

    def _plan_store(self) -> FilePlanStore[SystemScheduleChangePlan]:
        path = self._root() / "system_schedule_change_plans"
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        return FilePlanStore(
            path,
            SystemScheduleChangePlan,
            change_id_pattern=_CHANGE_ID_PATTERN,
            invalid_change_id_message="Invalid system schedule change id.",
            missing_plan_message="System schedule change plan not found: {change_id}",
        )

    def _receipt_path(self, change_id: str) -> Path:
        normalized = str(change_id or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid system schedule change id.")
        path = self._root() / "system_schedule_change_receipts"
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        return path / f"{normalized}.json"


__all__ = [
    "STATUS_APPROVAL_REQUIRED",
    "SYSTEM_SCHEDULE_CHANGE_ID_PREFIX",
    "SystemScheduleChangePlan",
    "SystemScheduleChangeService",
    "change_schedule",
    "configure_worker",
    "require_plan_matches",
]
