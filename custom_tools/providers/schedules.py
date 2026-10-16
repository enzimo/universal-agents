"""Schedule tools for the trusted Strands tool surface.

Every call is decided by the per-call access hook (catalog ``fn:schedules``
and ``fn:schedule_runs``). The tools work only on the schedules of the person
the task works for, taken from the task's signed context. System work (a
built-in system agent's run for no person, such as InternalImprovementAgent's
maintenance) sees and records only the schedule its signed context names and
changes nothing (``access_control.ownership.system_work_schedule_access``).
Changes go through
one path (``custom_agents.system_schedule_changes``): a person's own schedule
changes at once; a schedule the runtime manages is staged as an exact change
that a person approves (``apply_change``), and the tool returns
``approval_required`` with the ``change_id``.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from pydantic import ValidationError
from strands import tool

from custom_agents.schedule_changes import (
    REPORT_KEY,
    RESULT_FLAGS,
    SCHEDULE_NOT_FOUND,
    ScheduleChangeRequestV1,
    ScheduleNotFound,
    ScheduleUpdateV1,
    owned_schedule,
)
from custom_agents.schedule_conversation_context import (
    ensure_dedicated_schedule_conversation,
)
from custom_agents.schedule_fields import (
    apply_schedule_policy_update,
    coerce_schedule_memory,
    normalize_schedule_utc_iso,
    parse_schedule_bool,
    parse_schedule_json_object,
    schedule_memory_now,
    trim_schedule_memory_ledger,
)
from access_control.ownership import PRIVATE_READ, PRIVATE_UPDATE, system_work_schedule_access
from custom_agents.access_runtime import agent_principal, verified_task_context
from custom_agents.schedule_management import SystemScheduleMutationDenied, is_system_managed_schedule
from custom_agents.schedule_observations import (
    SCHEDULE_FAILURE_OUTCOMES,
    record_run_observation,
)
from custom_agents.schedule_store import ScheduleStore
from custom_agents.system_schedule_changes import change_schedule
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import (
    ManagerEscalationContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.schedule_v1 import (
    ScheduledMessage,
    ScheduleMemoryState,
    ScheduleTaskMemory,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    ScheduleManagementClass,
    TaskHistoryImportance,
    TaskHistoryType,
)
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers._runtime import (
    _get_current_task_info,
    _link_to_objective,
    _objective_to_link,
    _run_agent_coroutine,
    _schedule_store,
)
from custom_tools.providers._user_scope import _private_user_scope
from custom_tools.providers.schedule_workers import (
    authorize_scheduled_job_access,
    configure_scheduled_worker,
    inspect_scheduled_worker,
    install_scheduled_worker_draft,
    prepare_scheduled_job_access,
)
from custom_tools.result_pages import page_size, text_page
from custom_tools.runtime_context import active_task_context as _active_task_context
from custom_tools.runtime_context import get_agent_ref
from custom_tools.schedule_inspection import ScheduleView, compact_schedule, list_schedule_payloads
from id_generation import new_id

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent

logger = logging.getLogger(__name__)
_SCHEDULED_JOBS = "Scheduled jobs"


_OWN_SCHEDULE_ONLY = {
    "error": "system_work_own_schedule_only",
    "message": (
        "This task works for no person. Only a built-in system agent may use the schedule its run "
        "belongs to, and no other schedule is available."
    ),
}


def _system_work_schedule_id(agent: "UniversalAgent", schedule_id: str, *, action: str) -> tuple[bool, Optional[str]]:
    """Whether the task is system work, and the schedule id it may touch (None if refused).

    System work is a task whose verified signed context names no person. It
    may touch only the schedule that context names, when the runtime manages
    it (``access_control.ownership.system_work_schedule_access``); a blank
    ``schedule_id`` means that schedule.
    """

    store = getattr(agent, "_schedule_store", None)
    try:
        context = verified_task_context(agent)
    except RuntimeError:  # no task storage, so nothing is verified
        context = None
    if store is None or context is None or context.person_id is not None:
        return False, None
    wanted = str(schedule_id or "").strip() or str(context.schedule_id or "")
    schedule = store.get_scheduled_message(wanted) if wanted else None
    decision = system_work_schedule_access(
        agent_role=agent_principal(agent).role,
        signed_schedule_id=context.schedule_id,
        schedule_id=wanted,
        system_managed=schedule is not None and is_system_managed_schedule(schedule),
        action=action,
    )
    return True, wanted if decision.allowed else None


def _schedule_view(
    agent: "UniversalAgent", *, resource_name: str, schedule_id: str = "", action: str = PRIVATE_READ
) -> tuple[Optional[ScheduleView], Optional[dict[str, Any]]]:
    """The schedules this task may see or record, or the result refusing it.

    Work for a person: that person's schedules. System work: only the schedule
    its signed context names (see ``_system_work_schedule_id``).
    """

    owner, denial = _private_user_scope(agent, resource_name=resource_name)
    if denial is None:
        return ScheduleView(owner=owner), None
    system_work, allowed = _system_work_schedule_id(agent, schedule_id, action=action)
    if allowed is not None:
        return ScheduleView(schedule_id=allowed), None
    return None, (_OWN_SCHEDULE_ONLY if system_work else denial)


def _owned_or_denial(
    agent: "UniversalAgent", store: ScheduleStore, schedule_id: str, *, flag: str
) -> tuple[Optional[ScheduledMessage], Optional[dict[str, Any]]]:
    """The schedule whose run this task records, or the result refusing it.

    The person's own schedule; in system work only the schedule its signed
    context names (``_schedule_view``).
    """

    view, denial = _schedule_view(agent, resource_name=_SCHEDULED_JOBS, schedule_id=schedule_id, action=PRIVATE_UPDATE)
    if view is None:
        return None, denial
    if view.schedule_id is not None:
        return store.get_scheduled_message(view.schedule_id), None
    try:
        return owned_schedule(store, schedule_id, view.owner), None
    except ScheduleNotFound:
        return None, {"schedule_id": schedule_id, flag: False, "error": SCHEDULE_NOT_FOUND}


def _validation_message(exc: ValidationError) -> str:
    errors = exc.errors()
    message = str(errors[0].get("msg") if errors else exc)
    return message.removeprefix("Value error, ")


def _run_change(agent: "UniversalAgent", request: ScheduleChangeRequestV1, *, action: str) -> str:
    """Apply (or stage) one change and tell TeamArchitect about an applied one."""

    flag = RESULT_FLAGS[request.action]
    try:
        result = change_schedule(agent, request)
    except SystemScheduleMutationDenied as exc:
        return json.dumps(
            {"schedule_id": request.schedule_id, flag: False, "error": str(exc), "reason_code": exc.reason_code}
        )
    report = result.pop(REPORT_KEY, None)
    if report is not None:
        if result.get(flag) and getattr(agent, "_scheduler_wake", None):
            agent._scheduler_wake.set()
        result["manager_report"] = (
            _report_schedule_mutation_to_manager(agent, action=action, schedule_id=request.schedule_id, **report)
            if result.get(flag)
            else {"reported": False, "reason": "schedule_not_updated"}
        )
    return json.dumps(result, default=str)


def _apply_schedule_state_update(
    memory: ScheduleTaskMemory,
    *,
    state_json: str = "",
    domain_state_json: str = "",
    state_summary: str = "",
    state_changed: bool = False,
    observed_at: Optional[str] = None,
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    raw_state = parse_schedule_json_object(state_json, field_name="state_json")
    if raw_state:
        known_state_keys = {
            "summary",
            "confidence",
            "last_observed_at",
            "last_changed_at",
            "domain",
        }
        if any(key in raw_state for key in known_state_keys):
            merged = memory.state.model_dump(mode="json")
            merged.update(raw_state)
            memory.state = ScheduleMemoryState.model_validate(merged)
        else:
            memory.state.domain.update(raw_state)
        changes["state"] = "updated"
    raw_domain = parse_schedule_json_object(
        domain_state_json, field_name="domain_state_json"
    )
    if raw_domain:
        memory.state.domain.update(raw_domain)
        changes["state.domain"] = "updated"
    if state_summary.strip():
        memory.state.summary = state_summary.strip()
        changes["state.summary"] = "updated"
    if observed_at:
        memory.state.last_observed_at = observed_at
        changes["state.last_observed_at"] = observed_at
    if state_changed:
        memory.state.last_changed_at = observed_at or schedule_memory_now()
        changes["state.last_changed_at"] = memory.state.last_changed_at
    return changes


def _remaining_count_for_schedule(store: ScheduleStore, schedule_id: str) -> Any:
    return next(
        (
            row.get("remaining_count")
            for row in store.get_all()
            if row.get("schedule_id") == schedule_id
        ),
        None,
    )


def _persist_schedule_memory(
    store: ScheduleStore,
    scheduled_message: ScheduledMessage,
) -> None:
    if scheduled_message.schedule_memory is not None:
        scheduled_message.schedule_memory.updated_at = schedule_memory_now()
        trim_schedule_memory_ledger(scheduled_message.schedule_memory)
    store.add(
        scheduled_message,
        remaining_count_override=_remaining_count_for_schedule(
            store, scheduled_message.schedule_id
        ),
    )


def _resolve_active_schedule_id(agent: "UniversalAgent", schedule_id: str = "") -> str:
    resolved = str(schedule_id or "").strip()
    if resolved:
        return resolved
    _, source_meta = _active_task_context(agent)
    gateway_context = (
        getattr(source_meta, "gateway_context", None) if source_meta else None
    )
    resolved = str(getattr(gateway_context, "scheduled_job_id", None) or "").strip()
    if not resolved:
        raise ValueError(
            "schedule_id is required when no active scheduled task context is available."
        )
    return resolved


def _report_schedule_mutation_to_manager(
    agent: "UniversalAgent",
    *,
    action: str,
    schedule_id: str,
    before: Optional[dict[str, Any]],
    after: Optional[dict[str, Any]],
    details: dict[str, Any],
) -> dict[str, Any]:
    """Report subagent schedule mutations to TeamArchitect for notification decisions."""
    if getattr(agent, "named_agent_type", None) == TEAM_ARCHITECT_AGENT_TYPE:
        return {"reported": False, "reason": "mutating_agent_is_team_architect"}
    team_comms = getattr(agent, "team_comms", None)
    if team_comms is None or not hasattr(team_comms, "send_message"):
        return {"reported": False, "reason": "team_comms_unavailable"}

    routing_key = (getattr(agent, "named_agent_type_queue_map", {}) or {}).get(
        TEAM_ARCHITECT_AGENT_TYPE
    )
    if not routing_key:
        team_id = getattr(agent, "team_id", None)
        routing_key = (
            f"{team_id}_{TEAM_ARCHITECT_AGENT_TYPE}"
            if team_id
            else TEAM_ARCHITECT_AGENT_TYPE
        )

    source_task_id, source_meta = _active_task_context(agent)
    gateway_context = (
        getattr(source_meta, "gateway_context", None) if source_meta else None
    )
    source_schedule_id = (
        getattr(gateway_context, "scheduled_job_id", None) if gateway_context else None
    )
    original_requester = (
        "worker_schedule"
        if getattr(source_meta, "requester_type", None) == RequesterType.TIMER_REQUESTER
        else "worker_task"
    )
    mutation_summary = {
        "action": action,
        "schedule_id": schedule_id,
        "mutating_agent": {
            "agent_id": getattr(agent, "id", None),
            "agent_name": getattr(agent, "name", None),
            "named_agent_type": getattr(agent, "named_agent_type", None),
            "team_id": getattr(agent, "team_id", None),
        },
        "source_task_id": source_task_id,
        "source_schedule_id": source_schedule_id,
        "details": details,
        "before": before,
        "after": after,
    }
    report_task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement=(
            "A non-manager agent modified a scheduled job. Review whether the user should be notified.\n\n"
            f"{json.dumps(mutation_summary, indent=2, sort_keys=True, default=str)}"
        ),
    )
    envelope = MessageEnvelope(
        msg_content=report_task,
        msg_meta=MessageMeta(
            from_agent_name=getattr(agent, "name", None),
            to_named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
            msg_correlation_id=new_id("sched_mut"),
            manager_escalation=ManagerEscalationContext(
                escalation_type="manager_review",
                source_task_id=source_task_id,
                source_schedule_id=source_schedule_id or schedule_id,
                source_agent_id=getattr(agent, "id", None),
                source_agent_name=getattr(agent, "name", None),
                source_named_agent_type=getattr(agent, "named_agent_type", None),
                original_requester=original_requester,
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id=getattr(agent, "id", ""),
            to_routing_path=routing_key,
            response_routing_key=getattr(agent, "id", None),
            agent_signature="",
        ),
    )
    try:
        _run_agent_coroutine(
            agent,
            team_comms.send_message(message=envelope, routing_key=routing_key),
        )
    except Exception as exc:
        return {"reported": False, "reason": f"send_failed: {exc}"}

    task_manager = getattr(agent, "task_manager", None)
    if source_task_id and hasattr(task_manager, "append_history_entry"):
        try:
            task_manager.append_history_entry(
                source_task_id,
                f"Reported scheduled-job mutation {action} for {schedule_id} to TeamArchitect.",
                entry_type=TaskHistoryType.TASK_EVENT,
                importance=TaskHistoryImportance.NORMAL,
                agent_name=getattr(agent, "name", None),
                agent_id=getattr(agent, "id", None),
                details={
                    "event": "schedule_mutation_reported_to_manager",
                    "action": action,
                    "schedule_id": schedule_id,
                    "routing_key": routing_key,
                },
            )
        except Exception:
            pass
    return {
        "reported": True,
        "routing_key": routing_key,
        "task_id": report_task.task_id,
    }


def _build_schedule_payload_meta(
    agent: "UniversalAgent",
    schedule_id: str,
    objective_id: Optional[str] = None,
    task_info: Any = None,
):
    task_info = task_info or _get_current_task_info(agent)
    header = task_info.header.model_copy(deep=True)
    meta = task_info.meta.model_copy(deep=True)
    header.to_routing_path = agent.id
    meta.requester_type = RequesterType.TIMER_REQUESTER
    meta.is_response_requested = True
    meta.msg_correlation_id = f"schedule_corr_{schedule_id}"
    meta.followup_to_task_id = task_info.task.task_id
    meta.objective_id = objective_id or getattr(meta, "objective_id", None)
    if meta.gateway_context is not None:
        gateway_context = meta.gateway_context.model_copy(deep=True)
        gateway_context.scheduled_job_id = schedule_id
        # A scheduled run's identity belongs to that run, never to a saved
        # schedule; each run of this schedule gets its own.
        gateway_context.schedule_run = None
        meta.gateway_context = gateway_context
    return header, meta


@tool
def create_scheduled_job(
    instruction: str,
    schedule_name: str = "",
    interval_seconds: int = 0,
    cron_expression: str = "",
    cron_timezone: str = "",
    delay_seconds: int = 0,
    repeat_count: int = 0,
    objective_id: str = "",
    expires_at_utc: str = "",
    schedule_kind: str = "",
    schedule_memory_json: str = "",
    notify: str = "on_change",
    evidence_required: str = "",
    notify_on_failure: bool = False,
    target_agent_type: str = "",
    worker_policy_json: str = "",
    requirements_json: str = "",
) -> str:
    """
    Create a new manager-owned ScheduledMessage tied to the current requester/session context.

    Provide either `interval_seconds`, `cron_expression`, or `delay_seconds`.
    A cron expression is read in the person's own time zone ("0 9 * * *" is 9:00
    where they live, across daylight saving changes); pass `cron_timezone`
    (an IANA name such as "Europe/Berlin") only when they ask for another zone.
    `repeat_count=0` means infinite repeats for interval/cron schedules and a one-shot delay for delay-only schedules.
    `expires_at_utc` is optional and must be an explicit ISO 8601 UTC expiration timestamp when provided.
    For recurring jobs, provide compact schedule memory when useful. `notify` must be
    "on_change", "always", or "never"; flexible task-specific policy belongs under
    schedule_memory_json.policy.custom and compact task state under schedule_memory_json.state.domain.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if not instruction.strip():
        return json.dumps({"error": "instruction is required"})
    current_owner, denial = _private_user_scope(agent, resource_name=_SCHEDULED_JOBS)
    if denial is not None:
        return json.dumps(denial)
    if (
        sum(
            1
            for value in [
                interval_seconds > 0,
                bool(cron_expression.strip()),
                delay_seconds > 0,
            ]
            if value
        )
        != 1
    ):
        return json.dumps(
            {
                "error": "Provide exactly one of interval_seconds, cron_expression, or delay_seconds."
            }
        )
    try:
        schedule_id = new_id("sched")
        current_task_id = agent.task_manager.current_task_id
        task_info = _get_current_task_info(agent)
        resolved_objective_id, objective_refusal = _objective_to_link(
            agent, objective_id, current_task_id or ""
        )
        if objective_refusal:
            return json.dumps({"error": objective_refusal})
        payload_header, payload_meta = _build_schedule_payload_meta(
            agent,
            schedule_id,
            objective_id=resolved_objective_id,
            task_info=task_info,
        )
        if (
            getattr(
                getattr(payload_meta, "gateway_context", None),
                "canonical_user_id",
                None,
            )
            != current_owner
        ):
            return json.dumps(
                {"error": "Current task is not linked to the active canonical user."}
            )
        is_recurring_job = interval_seconds > 0 or bool(cron_expression.strip())
        schedule_memory = (
            coerce_schedule_memory(
                schedule_memory_json=schedule_memory_json,
                schedule_kind=schedule_kind,
                notify=notify,
                evidence_required=evidence_required,
                notify_on_failure=notify_on_failure,
            )
            if is_recurring_job or schedule_memory_json.strip() or schedule_kind.strip()
            else None
        )
        scheduled_message = ScheduledMessage(
            schedule_id=schedule_id,
            schedule_name=schedule_name.strip() or None,
            management_class=ScheduleManagementClass.USER,
            objective_id=resolved_objective_id,
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement=instruction.strip(),
            ),
            payload_header=payload_header,
            payload_meta=payload_meta,
            interval_ms=interval_seconds * 1000 if interval_seconds > 0 else None,
            cron_expression=cron_expression.strip() or None,
            cron_timezone=cron_timezone.strip() or None,
            repeat_count=(repeat_count if repeat_count > 0 else None)
            if interval_seconds > 0 or cron_expression.strip()
            else 1,
            expires_at_utc=normalize_schedule_utc_iso(
                expires_at_utc, field_name="expires_at_utc"
            )
            if expires_at_utc.strip()
            else None,
            schedule_memory=schedule_memory,
            next_fire_at=(
                datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
            ).isoformat()
            if delay_seconds > 0
            else None,
        )
        scheduled_message, schedule_conversation = (
            ensure_dedicated_schedule_conversation(
                agent,
                scheduled_message,
                source_header=task_info.header,
                source_meta=task_info.meta,
                source_task=task_info.task,
            )
        )
        worker_result = install_scheduled_worker_draft(
            agent, _schedule_store(agent), scheduled_message,
            target_agent_type=target_agent_type, worker_policy_json=worker_policy_json,
            requirements_json=requirements_json,
        )
        if worker_result and worker_result.get("status") == "draft":
            return json.dumps(worker_result)
        if schedule_conversation and hasattr(
            agent.task_manager, "append_history_entry"
        ):
            agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.append_history_entry,
                task_info.task.task_id,
                (
                    "Created dedicated conversation "
                    f"{schedule_conversation.get('conversation_id')} for recurring scheduled job {schedule_id}."
                ),
                entry_type=TaskHistoryType.TASK_EVENT,
                importance=TaskHistoryImportance.NORMAL,
                agent_name=getattr(agent, "name", None),
                agent_id=getattr(agent, "id", None),
                details={
                    "event": "schedule_conversation_forked",
                    "schedule_id": schedule_id,
                    "schedule_conversation_id": schedule_conversation.get(
                        "conversation_id"
                    ),
                    "schedule_seed_task_id": schedule_conversation.get("seed_task_id"),
                    "source_conversation_id": schedule_conversation.get(
                        "source_conversation_id"
                    ),
                },
            )
        objective_link_fields = _link_to_objective(
            agent,
            "schedule",
            resolved_objective_id,
            schedule_id,
            managed_by_objective=True,
            user_visible=True,
            reason="Scheduled job created from objective-linked task context.",
        )
        if getattr(agent, "_scheduler_wake", None):
            agent._scheduler_wake.set()
        return json.dumps(
            {
                **objective_link_fields,
                "schedule_id": scheduled_message.schedule_id,
                "schedule_name": scheduled_message.schedule_name,
                "worker": worker_result,
                "objective_id": scheduled_message.objective_id,
                "next_fire_at": scheduled_message.next_fire_at,
                "paused_until": scheduled_message.paused_until,
                "expires_at_utc": scheduled_message.expires_at_utc,
                "schedule_memory": scheduled_message.schedule_memory.model_dump(
                    mode="json"
                )
                if scheduled_message.schedule_memory is not None
                else None,
                "conversation_id": getattr(
                    scheduled_message.payload_meta, "conversation_id", None
                )
                if scheduled_message.payload_meta
                else None,
            }
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def list_scheduled_jobs(
    limit: int = 20,
    offset: int = 0,
    schedule_id: str = "",
    details_offset: int = 0,
    details_max_chars: int = 4000,
) -> str:
    """Page schedule summaries belonging to the current user.

    Follow next_offset for more summaries. Supply schedule_id to inspect one
    schedule, including a bounded JSON detail excerpt. Follow detail.next_offset
    using details_offset while detail.sha256 stays unchanged. Inspection never
    changes a schedule. Full instructions, run history and gateway context are
    available only through these exact-schedule detail pages.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    view, denial = _schedule_view(agent, resource_name=_SCHEDULED_JOBS, schedule_id=schedule_id)
    if view is None:
        return json.dumps(denial)
    try:
        size = page_size(limit, offset)
        schedules = sorted(
            list_schedule_payloads(
                _schedule_store(agent),
                view=view,
                schedule_id=schedule_id,
            ),
            key=lambda item: item["schedule_id"],
        )
        selected = schedules[offset : offset + size]
        result = [compact_schedule(item) for item in selected]
        if schedule_id and selected:
            result[0]["detail"] = text_page(
                json.dumps(
                    selected[0], default=str, ensure_ascii=False, sort_keys=True
                ),
                offset=details_offset,
                max_chars=details_max_chars,
            )
        return json.dumps(
            {
                "total": len(schedules),
                "returned": len(result),
                "offset": offset,
                "next_offset": offset + size
                if offset + size < len(schedules)
                else None,
                "schedules": result,
            },
            default=str,
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _visible_telemetry_items(
    store: ScheduleStore, raw_snapshot: dict[str, Any], view: ScheduleView, schedule_id: str
) -> list[dict[str, Any]]:
    """The snapshot rows of the schedules this task may see, sorted by id."""

    visible: list[dict[str, Any]] = []
    for item in raw_snapshot.get("schedules") or []:
        item_id = item.get("schedule_id")
        if not item_id or (schedule_id and item_id != schedule_id):
            continue
        if view.shows(store.get_scheduled_message(str(item_id))):
            visible.append(item)
    return sorted(visible, key=lambda item: item["schedule_id"])


def _is_recurring(item: dict[str, Any]) -> bool:
    return item.get("repeat_count") is None or item.get("interval_ms") is not None or bool(item.get("cron_expression"))


def _telemetry_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and the next fire time over the visible schedules."""

    def count(predicate: Any) -> int:
        return sum(1 for item in items if predicate(item))

    next_fire_values = [
        item.get("next_fire_at")
        for item in items
        if item.get("enabled") and not item.get("paused") and item.get("next_fire_at")
    ]
    return {
        "total": len(items),
        "enabled": count(lambda item: item.get("enabled")),
        "disabled": count(lambda item: not item.get("enabled")),
        "paused": count(lambda item: item.get("paused")),
        "due": count(lambda item: item.get("due")),
        "recurring": count(_is_recurring),
        "one_shot": count(lambda item: not _is_recurring(item)),
        "with_interval": count(lambda item: item.get("interval_ms") is not None),
        "with_cron": count(lambda item: item.get("cron_expression")),
        "load_errors": count(lambda item: item.get("load_error")),
        "next_fire_at": min(next_fire_values) if next_fire_values else None,
    }


@tool
def get_schedule_telemetry(
    include_schedules: bool = False,
    limit: int = 20,
    offset: int = 0,
    schedule_id: str = "",
) -> str:
    """
    Return schedule telemetry for this agent's runtime.

    Default output contains counts only. Set include_schedules to receive a
    bounded metadata page; follow next_offset. Supply schedule_id to narrow both
    counts and rows to one schedule. Use list_scheduled_jobs(schedule_id=...)
    for bounded full-detail pages rather than reading tool-result artifacts.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    view, denial = _schedule_view(agent, resource_name="Schedule telemetry", schedule_id=schedule_id)
    if view is None:
        return json.dumps(denial)
    try:
        size = page_size(limit, offset)
        store = _schedule_store(agent)
        raw_snapshot = store.build_telemetry_snapshot(include_schedules=True)
        visible = _visible_telemetry_items(store, raw_snapshot, view, schedule_id)
        page = [compact_schedule(item) for item in visible[offset : offset + size]]
        snapshot = {
            "generated_at": raw_snapshot.get("generated_at"),
            "summary": _telemetry_summary(visible),
            "schedules": page if include_schedules else None,
            "offset": offset,
            "next_offset": offset + size if include_schedules and offset + size < len(visible) else None,
            "agent": {
                "agent_id": getattr(agent, "id", None),
                "agent_name": getattr(agent, "name", None),
                "team_id": getattr(agent, "team_id", None),
                "named_agent_type": getattr(agent, "named_agent_type", None),
            },
        }
        return json.dumps(snapshot, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def update_scheduled_job(
    schedule_id: str,
    schedule_name: str = "",
    clear_schedule_name: bool = False,
    instruction: str = "",
    interval_seconds: int = -1,
    cron_expression: str = "",
    cron_timezone: str = "",
    next_fire_at: str = "",
    repeat_count: int = -1,
    enabled: str = "",
    paused_until_iso: str = "",
    clear_pause: bool = False,
    expires_at_utc: str = "",
    clear_expiration: bool = False,
    schedule_kind: str = "",
    schedule_memory_json: str = "",
    clear_schedule_memory: bool = False,
    notify: str = "",
    evidence_required: str = "",
    notify_on_failure: str = "",
) -> str:
    """
    Modify an existing scheduled job.

    Empty string arguments leave the current value unchanged. `interval_seconds > 0`
    switches the schedule to a fixed interval, while `cron_expression` switches it to
    cron, read in the schedule's time zone (the person's own unless `cron_timezone`,
    an IANA name, sets another). `repeat_count=-1` leaves repeat count unchanged, `0` makes recurring jobs
    infinite, and positive values set a finite remaining policy. Expiration is never
    inferred; provide `expires_at_utc` explicitly or `clear_expiration=True`.
    Schedule memory is a compact recurring-task policy/state/ledger envelope.
    Use `notify` with one of "on_change", "always", or "never" to persist
    notification policy changes. A schedule the runtime manages is not changed
    at once: the result is approval_required with a change_id for apply_change.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if not str(schedule_id or "").strip():
        return json.dumps({"error": "schedule_id is required"})
    try:
        update = ScheduleUpdateV1(
            schedule_name=schedule_name,
            clear_schedule_name=clear_schedule_name,
            instruction=instruction,
            interval_seconds=interval_seconds,
            cron_expression=cron_expression,
            cron_timezone=cron_timezone.strip(),
            next_fire_at=normalize_schedule_utc_iso(next_fire_at, field_name="next_fire_at")
            if next_fire_at.strip()
            else "",
            repeat_count=repeat_count,
            enabled=parse_schedule_bool(enabled, field_name="enabled"),
            paused_until=normalize_schedule_utc_iso(paused_until_iso, field_name="paused_until_iso")
            if paused_until_iso.strip()
            else "",
            clear_pause=clear_pause,
            expires_at_utc=normalize_schedule_utc_iso(expires_at_utc, field_name="expires_at_utc")
            if expires_at_utc.strip()
            else "",
            clear_expiration=clear_expiration,
            schedule_kind=schedule_kind,
            schedule_memory_json=schedule_memory_json,
            clear_schedule_memory=clear_schedule_memory,
            notify=notify,
            evidence_required=evidence_required,
            notify_on_failure=parse_schedule_bool(notify_on_failure, field_name="notify_on_failure"),
        )
        request = ScheduleChangeRequestV1(schedule_id=schedule_id.strip(), action="update", update=update)
        return _run_change(agent, request, action="update_scheduled_job")
    except ValidationError as exc:
        return json.dumps({"schedule_id": schedule_id, "updated": False, "error": _validation_message(exc)})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def record_scheduled_run(
    schedule_id: str = "",
    outcome: str = "success",
    state_changed: bool = False,
    state_summary: str = "",
    state_json: str = "",
    domain_state_json: str = "",
    policy_json: str = "",
    notify: str = "",
    evidence_required: str = "",
    notify_on_failure: str = "",
    evidence_json: str = "",
    error_text: str = "",
) -> str:
    """
    Record the result of a fired scheduled task in its compact persistent memory.

    Use this from recurring scheduled tasks after checking the source of truth.
    `schedule_id` may be omitted when the current task came from a schedule.
    `outcome` should be one of success, changed, no_change, failure, conflict,
    inconclusive, or skipped. The tool updates the bounded run ledger, merges
    compact state into schedule_memory.state, persists optional policy changes,
    and returns whether the persisted notification policy says this run should
    notify the user. Do not use this as a substitute for `notify_user`; call
    `notify_user` only when `should_notify` is true and a user-facing update is
    still appropriate.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        resolved_schedule_id = _resolve_active_schedule_id(agent, schedule_id)
        store = _schedule_store(agent)
        scheduled_message, denial = _owned_or_denial(agent, store, resolved_schedule_id, flag="recorded")
        if denial is not None:
            return json.dumps(denial)
        assert scheduled_message is not None

        if scheduled_message.schedule_memory is None:
            scheduled_message.schedule_memory = ScheduleTaskMemory()
        memory = scheduled_message.schedule_memory
        now = schedule_memory_now()
        normalized_outcome = str(outcome or "success").strip().lower() or "success"
        if normalized_outcome == "failed":
            normalized_outcome = "failure"
        material_change = bool(state_changed or normalized_outcome == "changed")
        failure = normalized_outcome in SCHEDULE_FAILURE_OUTCOMES

        parsed_notify_on_failure = parse_schedule_bool(
            notify_on_failure, field_name="notify_on_failure"
        )
        policy_changes = apply_schedule_policy_update(
            memory,
            policy_json=policy_json,
            notify=notify,
            evidence_required=evidence_required,
            notify_on_failure=parsed_notify_on_failure,
        )
        state_changes = {}
        if (
            not failure
            or state_json.strip()
            or domain_state_json.strip()
            or state_summary.strip()
        ):
            state_changes = _apply_schedule_state_update(
                memory,
                state_json=state_json,
                domain_state_json=domain_state_json,
                state_summary=state_summary,
                state_changed=material_change,
                observed_at=now,
            )

        evidence = parse_schedule_json_object(
            evidence_json, field_name="evidence_json"
        )
        should_notify = record_run_observation(
            memory, outcome=normalized_outcome, state_changed=material_change,
            summary=state_summary, evidence=evidence, error=error_text, at=now,
        )

        _persist_schedule_memory(store, scheduled_message)
        return json.dumps(
            {
                "schedule_id": resolved_schedule_id,
                "recorded": True,
                "outcome": normalized_outcome,
                "state_changed": material_change,
                "should_notify": should_notify,
                "notify": memory.policy.notify,
                "notify_on_failure": memory.policy.notify_on_failure,
                "policy_changes": policy_changes,
                "state_changes": state_changes,
                "schedule_memory": memory.model_dump(mode="json"),
                "next_step": (
                    "Call notify_user if a user-facing notification is still appropriate."
                    if should_notify
                    else "Finish with suppress_outbound_dispatch=true unless the original instruction explicitly requires a heartbeat response."
                ),
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def remove_scheduled_job(schedule_id: str) -> str:
    """Remove a scheduled job permanently.

    A schedule the runtime manages is not removed at once: the result is
    approval_required with a change_id for apply_change.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        request = ScheduleChangeRequestV1(schedule_id=str(schedule_id or "").strip(), action="remove")
        return _run_change(agent, request, action="remove_scheduled_job")
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def disable_scheduled_job(schedule_id: str) -> str:
    """Disable a scheduled job indefinitely until it is resumed/enabled again.

    A schedule the runtime manages is not changed at once: the result is
    approval_required with a change_id for apply_change.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        request = ScheduleChangeRequestV1(schedule_id=str(schedule_id or "").strip(), action="disable")
        return _run_change(agent, request, action="disable_scheduled_job")
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def resume_scheduled_job(schedule_id: str) -> str:
    """Resume a scheduled job by clearing temporary pause and enabling it.

    A schedule the runtime manages is not changed at once: the result is
    approval_required with a change_id for apply_change.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        request = ScheduleChangeRequestV1(schedule_id=str(schedule_id or "").strip(), action="resume")
        return _run_change(agent, request, action="resume_scheduled_job")
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def pause_scheduled_job(
    schedule_id: str,
    duration_minutes: int = 0,
    until_iso: str = "",
) -> str:
    """Temporarily pause a scheduled job until a specific UTC ISO timestamp or for a number of minutes.

    A schedule the runtime manages is not paused at once: the result is
    approval_required with a change_id for apply_change.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if duration_minutes <= 0 and not until_iso.strip():
        return json.dumps({"error": "Provide duration_minutes or until_iso."})
    try:
        paused_until = (
            normalize_schedule_utc_iso(until_iso, field_name="until_iso")
            if until_iso.strip()
            else (datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)).isoformat()
        )
        request = ScheduleChangeRequestV1(
            schedule_id=str(schedule_id or "").strip(), action="pause", paused_until_utc=paused_until
        )
        return _run_change(agent, request, action="pause_scheduled_job")
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# Seeing the person's schedules and recording one's own scheduled run is every
# agent's own work (catalog fn:schedule_runs, agent_runtime); changing
# schedules needs a grant (fn:schedules), and changing a schedule the runtime
# manages needs a person's approval of the exact change (fn:system_schedules).
SCHEDULE_RUNS_PROVIDER = ToolProvider(
    name="schedule_runs",
    tools=(
        inspect_scheduled_worker,
        list_scheduled_jobs,
        get_schedule_telemetry,
        record_scheduled_run,
    ),
)
PROVIDER = ToolProvider(
    name="schedules",
    tools=(
        authorize_scheduled_job_access,
        configure_scheduled_worker,
        prepare_scheduled_job_access,
        create_scheduled_job,
        update_scheduled_job,
        remove_scheduled_job,
        disable_scheduled_job,
        resume_scheduled_job,
        pause_scheduled_job,
    ),
)
