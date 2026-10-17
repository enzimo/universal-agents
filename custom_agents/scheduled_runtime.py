"""Scheduled-message and heartbeat runtime helpers for UniversalAgent."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Awaitable, Callable, Optional
from access_control.ownership import PRIVATE_UPDATE, objective_access
from custom_agents.access_runtime import resolve_meta_identity, verified_meta_person_id
from custom_agents.knowledge_curation_runtime import prepare_knowledge_curation_schedule
from custom_agents.schedule_permits import maybe_renew_schedule_permits, sign_system_run
from custom_agents.scheduled_run_contexts import ScheduledRun, sign_scheduled_run
from custom_agents.task_context_runtime import inactive_owner, run_owner, scheduled_owner_roles
from custom_agents.schedule_context_refresh import refresh_schedule_gateway_context
from custom_messages.contracts.schedule_worker_v1 import ScheduleOccurrenceV1

from custom_agents.lease_loss_wind_down import start_lease_loss_wind_down
from custom_agents.worker_handback_runtime import process_pending_handbacks
from custom_agents.task_queue_notifications import check_queue_pressure
from custom_agents.conversation_runtime import (
    ensure_task_conversation_context,
    record_task_conversation_history,
    start_new_conversation,
)
from custom_agents.heartbeat_store import (
    HeartbeatStore,
    is_heartbeat_launcher,
    item_conversation_for_run,
)
from custom_agents.internal_improvement_runtime import (
    enqueue_internal_improvement_job,
    is_internal_improvement_schedule_name,
    prepare_internal_improvement_schedule_for_manager,
)
from custom_agents.memory_review_runtime import (
    COREMEM_AUDIT_REVIEW_KIND,
    USERMEM_AUDIT_REVIEW_KIND,
    enqueue_pinned_memory_audit_job,
    enqueue_stale_conversation_review_jobs,
    memory_review_schedule_kind,
    memory_review_notification_instruction,
)
from custom_agents.schedule_conversation_context import (
    ensure_dedicated_schedule_conversation,
)
from custom_agents.stuck_schedules import report_stuck_schedules
from custom_agents.stuck_tasks import report_stuck_tasks
from custom_agents.liveness_observation import MAX_SCHEDULER_SLEEP_SECONDS, observe_liveness
from custom_agents.dead_letter_watch import report_dead_letters
from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.access_request_runtime import resume_decided_access_waits
from custom_agents.orphaned_work_takeover import take_over_orphaned_work
from custom_agents.turn_failure_runtime import release_orphaned_slot_when_idle
from custom_agents.schedule_store import (
    DEFAULT_SCHEDULE_CLAIM_TTL_SECONDS,
    compute_next_fire_at,
    owner_canonical_user_id_for_schedule,
    prepare_schedule_for_add,
    schedule_claim_scope_for_agent,
)
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    build_schedule_mutation_permit,
    is_system_managed_schedule,
)
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import (
    MessageMetaType,
    PlanActionStatus,
    PlanActionType,
    ProgressState,
    RecoverablePauseTrigger,
    RequesterType,
    RoutingType,
    SystemScheduleKind,
    TaskHistoryImportance,
    TaskHistoryType,
    TaskQueryDecision,
)
from custom_messages.system_schedules import (
    AFTER_ACTION_REPORT_REVIEW_SCHEDULE_NAME_PREFIX,
    DELEGATION_ASSURANCE_SCHEDULE_NAME_PREFIX,
    KNOWLEDGE_CURATION_PREPARER,
    MEMORY_REVIEW_PREPARER,
    system_schedule_spec_for_name,
)
from id_generation import new_id
from storage.agent_launch_repository import LaunchConflict

SKIP_AFTER_ACTION_REPORT_REVIEW = object()
SCHEDULED_TASK_SUPPRESSED_COMPLETION_INSTRUCTION = (
    "Scheduled completion contract: If this scheduled run completes successfully but should not send a "
    "user-visible update because nothing changed or no action is needed, finish with a task_response action, "
    "set suppress_outbound_dispatch=true, and put a concise internal completion note in task.answer. "
    "The gateway will not deliver that note. When schedule_memory is present, 'should not send' means "
    "record_scheduled_run returned should_notify=false or the persisted policy/instruction is explicitly record-only; "
    "do not suppress a successful run that policy.notify=always or the schedule instruction makes user-visible."
)
SCHEDULED_TASK_MEMORY_INSTRUCTION = (
    "Scheduled memory contract: Treat the schedule_memory JSON below as the canonical compact memory for this "
    "recurring scheduled job. Use conversation history as secondary context only. After checking the relevant "
    "source of truth, call record_scheduled_run with the observed outcome and compact state update. Respect "
    "schedule_memory.policy.notify: on_change means notify only for material changes, always means notify after "
    "successful runs, and never means record only. If record_scheduled_run returns should_notify=false, finish "
    "with suppress_outbound_dispatch=true. If it returns should_notify=true, send one user-visible update either "
    "by normal unsuppressed task_response or by notify_user followed by an auto-suppressed terminal response; do not do both."
)


def _append_suppressed_completion_instruction(problem_statement: Optional[str]) -> str:
    base = str(problem_statement or "").strip()
    if "suppress_outbound_dispatch=true" in base and "task.answer" in base:
        return base
    if not base:
        return SCHEDULED_TASK_SUPPRESSED_COMPLETION_INSTRUCTION
    return f"{base}\n\n{SCHEDULED_TASK_SUPPRESSED_COMPLETION_INSTRUCTION}"


async def _objective_work_schedule_is_inactive(
    agent: Any,
    schedule_id: str,
    scheduled_message: ScheduledMessage,
) -> bool:
    if (
        scheduled_message.effective_system_schedule_kind
        != SystemScheduleKind.OBJECTIVE_WORK_SESSION
    ):
        return False
    objective_id = scheduled_message.objective_id or ""
    objective = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_objective, objective_id
    )
    repository = agent.task_manager.objective_work_repository
    policy = (
        await agent.task_manager.operations_runtime.run(
            repository.get_policy,
            objective_id,
            owner_agent_id=await agent.task_manager.operations_runtime.run(
                agent.task_manager.durable_manager_scope_id,
            ),
        )
        if objective_id and repository is not None
        else None
    )
    inactive = (
        objective is None
        or getattr(objective.status, "value", objective.status) != "active"
        or policy is None
        or getattr(policy.status, "value", policy.status) != "active"
    )
    if inactive:
        agent._logger.info(
            "[%s] Skipping Objective work schedule %s because its Objective or policy is inactive.",
            agent.id,
            schedule_id,
        )
    return inactive


def _open_objective_work_session_for_fired_task(
    agent: Any,
    schedule_id: str,
    scheduled_message: ScheduledMessage,
    task: Task,
    internal_meta: MessageMeta,
) -> bool:
    if (
        scheduled_message.effective_system_schedule_kind
        != SystemScheduleKind.OBJECTIVE_WORK_SESSION
    ):
        return True
    if not internal_meta.objective_id:
        agent._logger.error(
            "[%s] Objective work schedule %s fired without an Objective id.",
            agent.id,
            schedule_id,
        )
        return False
    try:
        session = agent.task_manager.objective_work_coordinator.start_session(
            internal_meta.objective_id,
            task_id=task.task_id,
        )
        internal_meta.objective_work_session_id = session.session_id
        agent.task_manager.objective_work_coordinator.set_schedule(
            internal_meta.objective_id,
            schedule_id=schedule_id,
            next_run_at=compute_next_fire_at(
                cron_expression=scheduled_message.cron_expression,
                interval_ms=scheduled_message.interval_ms,
                from_time=datetime.now(timezone.utc),
                cron_timezone=scheduled_message.cron_timezone,
            ),
        )
        agent.task_manager.objective_work_coordinator.resume_existing_plan(session)
        return True
    except Exception as exc:
        agent._logger.error(
            "[%s] Could not open Objective work session for schedule %s: %s",
            agent.id,
            schedule_id,
            exc,
        )
        return False


def _append_schedule_memory_context(
    problem_statement: Optional[str],
    scheduled_message: ScheduledMessage,
) -> str:
    memory = getattr(scheduled_message, "schedule_memory", None)
    if memory is None:
        return str(problem_statement or "").strip()
    base = str(problem_statement or "").strip()
    if "Scheduled memory contract:" in base and "schedule_memory JSON" in base:
        return base
    memory_json = memory.model_dump_json(indent=2)
    context = (
        f"{SCHEDULED_TASK_MEMORY_INSTRUCTION}\n\n"
        f"schedule_id: {scheduled_message.schedule_id}\n"
        f"schedule_memory JSON:\n```json\n{memory_json}\n```"
    )
    if not base:
        return context
    return f"{base}\n\n{context}"


def _task_matches_scheduled_job(task_info: Any, scheduled_job_id: str) -> bool:
    if not scheduled_job_id:
        return False
    meta = getattr(task_info, "meta", None)
    gateway_context = getattr(meta, "gateway_context", None)
    if getattr(gateway_context, "scheduled_job_id", None) == scheduled_job_id:
        return True
    task = getattr(task_info, "task", None)
    for entry in getattr(task, "execution_history", None) or []:
        details = getattr(entry, "details", {}) or {}
        if details.get("schedule_id") == scheduled_job_id:
            return True
        if details.get("heartbeat_item_id") == scheduled_job_id:
            return True
    return False


async def _active_task_for_scheduled_job(
    agent: Any, scheduled_job_id: str
) -> Optional[Any]:
    task_manager = getattr(agent, "task_manager", None)
    tasks = (
        await task_manager.operations_runtime.run(task_manager.list_task_infos)
        if task_manager is not None
        else []
    )
    is_terminal_state = getattr(task_manager, "is_terminal_state", None)
    for task_info in tasks:
        if not _task_matches_scheduled_job(task_info, scheduled_job_id):
            continue
        state = getattr(task_info, "state", None)
        if callable(is_terminal_state) and is_terminal_state(state):
            continue
        return task_info
    return None


def _coalesced_fire_user_notified(task_info: Any, scheduled_job_id: str) -> bool:
    task = getattr(task_info, "task", None)
    for entry in getattr(task, "execution_history", None) or []:
        details = getattr(entry, "details", {}) or {}
        if (
            details.get("event") == "scheduled_fire_coalesced_user_notified"
            and details.get("scheduled_job_id") == scheduled_job_id
        ):
            return True
    return False


async def _notify_user_of_coalesced_scheduled_fire(
    agent: Any,
    *,
    active_task_info: Any,
    scheduled_job_id: str,
    schedule_id: str,
    schedule_name: Optional[str],
    notification_header: Optional[MessageHeader],
    notification_meta: Optional[MessageMeta],
) -> None:
    if notification_meta is None or notification_header is None:
        return
    gateway_context = getattr(notification_meta, "gateway_context", None)
    if gateway_context is None:
        return
    routing_key = getattr(notification_header, "response_routing_key", None)
    if not routing_key:
        return
    if _coalesced_fire_user_notified(active_task_info, scheduled_job_id):
        return

    active_task = active_task_info.task
    active_task_id = getattr(active_task, "task_id", None)
    label = schedule_name or scheduled_job_id
    text = (
        f"Scheduled job '{label}' fired again while its previous run is still active, "
        "so I skipped this occurrence to avoid duplicate queued work. Future scheduled fires will continue normally."
    )
    envelope = MessageEnvelope(
        msg_content=UserNotification(
            text=text,
            urgency="normal",
            reason="scheduled_fire_coalesced",
            source_task_id=active_task_id,
            source_agent_id=getattr(agent, "id", None),
            source_agent_name=getattr(agent, "name", None),
        ),
        msg_meta=MessageMeta(
            from_agent_name=getattr(agent, "name", None),
            msg_correlation_id=new_id("coal_sched"),
            conversation_id=getattr(notification_meta, "conversation_id", None),
            followup_to_task_id=active_task_id,
            request_timezone=getattr(notification_meta, "request_timezone", None),
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
            gateway_context=gateway_context,
        ),
        msg_header=MessageHeader(
            from_agent_id=getattr(agent, "id", ""),
            to_routing_path=routing_key,
            response_routing_key=getattr(agent, "id", None),
            agent_signature=getattr(notification_header, "agent_signature", None),
        ),
    )
    team_comms = getattr(agent, "team_comms", None)
    send_message = getattr(team_comms, "send_message", None)
    if not callable(send_message):
        return
    await send_message(message=envelope, routing_key=routing_key)
    append_history = getattr(
        getattr(agent, "task_manager", None), "append_history_entry", None
    )
    if callable(append_history) and active_task_id:
        append_history(
            active_task_id,
            f"User notified that scheduled job {scheduled_job_id} skipped an overlapping fire.",
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.NORMAL,
            details={
                "event": "scheduled_fire_coalesced_user_notified",
                "schedule_id": schedule_id,
                "scheduled_job_id": scheduled_job_id,
            },
        )


async def _coalesce_into_run_elsewhere(
    agent: Any, *, schedule_id: str, schedule_admission: ScheduleOccurrenceV1
) -> bool:
    """Merge this fire into an unfinished run of the schedule on a live worker.

    The local check above only sees this agent's tasks. A run can also be
    unfinished on another worker, or waiting to be rerun after a handback; a
    new fire must not run beside it. Only runs whose owner is still live count,
    so a crashed worker's leftover receipt cannot block the schedule forever.
    """
    from custom_agents.runtime_recovery import filter_stale_role_takeover_owner_ids

    records = agent._schedule_store.worker_records
    unfinished = [
        record
        for record in await agent.task_manager.operations_runtime.run(
            records.unfinished_occurrences, schedule_id, agent.team_id
        )
        if record.occurrence_id != schedule_admission.occurrence_id
        and record.agent_id != agent.id
    ]
    if not unfinished:
        return False
    stale = set(
        await filter_stale_role_takeover_owner_ids(
            agent, sorted({record.agent_id for record in unfinished})
        )
    )
    live = sorted(
        (record for record in unfinished if record.agent_id not in stale),
        key=lambda record: record.due_at,
    )
    if not live:
        return False
    target = live[0]
    await agent.task_manager.operations_runtime.run(
        agent.task_manager._persist_runtime_state,
        schedule_admission=schedule_admission.model_copy(
            update={"coalesced_into_task_id": target.task_id}
        ),
    )
    agent._logger.warning(
        "Scheduled fire merged into an unfinished run on another worker. "
        "event=scheduled_fire_coalesced_elsewhere agent_id=%s schedule_id=%s "
        "occurrence_id=%s active_task_id=%s active_agent_id=%s",
        agent.id,
        schedule_id,
        schedule_admission.occurrence_id,
        target.task_id,
        target.agent_id,
    )
    return True


async def _coalesce_scheduled_fire_if_active(
    agent: Any,
    scheduled_job_id: str,
    *,
    schedule_id: str,
    schedule_name: Optional[str] = None,
    notification_header: Optional[MessageHeader] = None,
    notification_meta: Optional[MessageMeta] = None,
    schedule_admission: ScheduleOccurrenceV1 | None = None,
) -> bool:
    active_task_info = await _active_task_for_scheduled_job(agent, scheduled_job_id)
    if active_task_info is None:
        return schedule_admission is not None and await _coalesce_into_run_elsewhere(
            agent, schedule_id=schedule_id, schedule_admission=schedule_admission
        )

    active_task = active_task_info.task
    active_task_id = getattr(active_task, "task_id", "unknown")
    active_state = getattr(active_task_info, "state", None)
    state_value = getattr(active_state, "value", active_state)
    if schedule_admission is not None:
        await agent.task_manager.operations_runtime.run(
            agent.task_manager._persist_runtime_state,
            schedule_admission=schedule_admission.model_copy(update={"coalesced_into_task_id": active_task_id}),
        )
    logger = getattr(agent, "_logger", None)
    if logger is not None:
        logger.warning(
            "[%s] Coalescing scheduled fire for %s because prior task %s from the same scheduled job is still %s.",
            getattr(agent, "id", "unknown"),
            scheduled_job_id,
            active_task_id,
            state_value or "active",
        )

    append_history = getattr(
        getattr(agent, "task_manager", None), "append_history_entry", None
    )
    if callable(append_history):
        append_history(
            active_task_id,
            f"Scheduled job {scheduled_job_id} fired again while this prior scheduled task was still active; skipped duplicate fire.",
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details={
                "event": "scheduled_fire_coalesced",
                "schedule_id": schedule_id,
                "scheduled_job_id": scheduled_job_id,
                "active_task_id": active_task_id,
                "active_task_state": str(state_value or ""),
            },
        )
    try:
        await _notify_user_of_coalesced_scheduled_fire(
            agent,
            active_task_info=active_task_info,
            scheduled_job_id=scheduled_job_id,
            schedule_id=schedule_id,
            schedule_name=schedule_name,
            notification_header=notification_header,
            notification_meta=notification_meta,
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[%s] Failed to notify user about coalesced scheduled fire for %s: %s",
                getattr(agent, "id", "unknown"),
                scheduled_job_id,
                exc,
            )
    return True


async def _preserve_inbound_routing(
    agent: Any,
    schedule_msg: ScheduledMessage,
    msg_header: Optional[MessageHeader],
    msg_meta: Optional[MessageMeta],
) -> None:
    """Keep the inbound header and metadata when the schedule omitted its own (recorded as a repair)."""

    repair_recorder = getattr(agent, "_record_runtime_repair_anomaly", None)
    related_task_id = getattr(schedule_msg.payload, "task_id", None) or getattr(
        schedule_msg.payload, "parent_task_id", None
    )
    if schedule_msg.payload_header is None and msg_header is not None:
        agent._logger.warning(
            f"[{agent.id}] ScheduledMessage {schedule_msg.schedule_id} omitted payload_header; "
            "preserving the inbound header at runtime."
        )
        if repair_recorder is not None:
            await repair_recorder(
                "scheduled_message_payload_header_preserved",
                "ScheduledMessage omitted payload_header; runtime preserved the inbound header.",
                related_task_id=related_task_id,
                details={
                    "schedule_id": schedule_msg.schedule_id,
                    "schedule_name": schedule_msg.schedule_name,
                    "payload_type": type(schedule_msg.payload).__name__,
                    "from_agent_id": msg_header.from_agent_id,
                    "response_routing_key": msg_header.response_routing_key,
                },
            )
        schedule_msg.payload_header = msg_header.model_copy(deep=True)
    if schedule_msg.payload_meta is None and msg_meta is not None:
        agent._logger.warning(
            f"[{agent.id}] ScheduledMessage {schedule_msg.schedule_id} omitted payload_meta; "
            "preserving the inbound metadata at runtime."
        )
        if repair_recorder is not None:
            await repair_recorder(
                "scheduled_message_payload_meta_preserved",
                "ScheduledMessage omitted payload_meta; runtime preserved the inbound metadata.",
                related_task_id=related_task_id,
                details={
                    "schedule_id": schedule_msg.schedule_id,
                    "schedule_name": schedule_msg.schedule_name,
                    "payload_type": type(schedule_msg.payload).__name__,
                    "correlation_id": getattr(msg_meta, "msg_correlation_id", None),
                    "conversation_id": getattr(msg_meta, "conversation_id", None),
                },
            )
        normalized_meta = msg_meta.model_copy(deep=True)
        normalized_meta.requester_type = RequesterType.TIMER_REQUESTER
        schedule_msg.payload_meta = normalized_meta


async def _store_ensure(agent: Any, schedule_msg: ScheduledMessage, claim_scope: dict[str, Any]) -> None:
    """Insert the schedule only when absent; a stored one keeps all of its state."""

    ensured = await agent.task_manager.operations_runtime.run(
        agent._schedule_store.add_if_absent,
        schedule_msg,
        **claim_scope,
    )
    if ensured is None:
        return
    if ensured:
        agent._logger.info(
            "[%s] ScheduledMessage %s installed once. Next fire at %s",
            agent.id,
            schedule_msg.schedule_id,
            schedule_msg.next_fire_at,
        )
        return
    persisted = await agent.task_manager.operations_runtime.run(
        agent._schedule_store.get_scheduled_message, schedule_msg.schedule_id
    )
    agent._logger.info(
        "[%s] ScheduledMessage %s already exists; preserved persisted state and next fire at %s.",
        agent.id,
        schedule_msg.schedule_id,
        getattr(persisted, "next_fire_at", None),
    )


async def _store_replace(agent: Any, schedule_msg: ScheduledMessage, claim_scope: dict[str, Any]) -> None:
    """Replace the row; a stored system schedule needs the trusted-runtime permit for this exact change."""

    existing = await agent.task_manager.operations_runtime.run(
        agent._schedule_store.get_scheduled_message, schedule_msg.schedule_id
    )
    runtime_permit = (
        build_schedule_mutation_permit(
            schedule=existing,
            operation=ScheduleMutationOperation.UPDATE,
            authority_mode=ScheduleMutationAuthorityMode.TRUSTED_RUNTIME,
            actor_principal_id=str(getattr(agent, "id", "") or "runtime"),
            owner_canonical_user_id=str(
                owner_canonical_user_id_for_schedule(existing) or ""
            ),
            team_id=str(getattr(agent, "team_id", "") or ""),
            candidate=schedule_msg,
        )
        if existing is not None and is_system_managed_schedule(existing)
        else None
    )
    store_kwargs: dict[str, Any] = dict(claim_scope)
    if runtime_permit is not None:
        store_kwargs["system_mutation_permit"] = runtime_permit
    await agent.task_manager.operations_runtime.run(
        agent._schedule_store.add, schedule_msg, **store_kwargs
    )
    agent._logger.info(
        "[%s] ScheduledMessage %s stored. Next fire at %s",
        agent.id,
        schedule_msg.schedule_id,
        schedule_msg.next_fire_at,
    )


async def handle_scheduled_message(
    agent: Any,
    msg: ScheduledMessage,
    *,
    msg_header: Optional[MessageHeader] = None,
    msg_meta: Optional[MessageMeta] = None,
) -> None:
    if msg.install_mode == "refresh":
        # Only the person and session of an existing runtime-managed schedule
        # change; nothing is installed (custom_agents.schedule_context_refresh).
        await agent.task_manager.operations_runtime.run(
            partial(refresh_schedule_gateway_context, agent), msg.model_copy(deep=True)
        )
        return
    schedule_msg = msg.model_copy(deep=True)
    if is_internal_improvement_schedule_name(schedule_msg.schedule_name):
        schedule_msg = prepare_internal_improvement_schedule_for_manager(
            agent, schedule_msg
        )
        if schedule_msg is None:
            return
    await _preserve_inbound_routing(agent, schedule_msg, msg_header, msg_meta)
    schedule_msg, schedule_conversation = ensure_dedicated_schedule_conversation(
        agent,
        schedule_msg,
        source_header=msg_header,
        source_meta=msg_meta,
    )
    if schedule_conversation:
        agent._logger.info(
            "[%s] Forked recurring schedule %s into dedicated conversation %s seeded by %s.",
            agent.id,
            schedule_msg.schedule_id,
            schedule_conversation.get("conversation_id"),
            schedule_conversation.get("seed_task_id"),
        )
    claim_scope = schedule_claim_scope_for_agent(agent)
    prepare_schedule_for_add(schedule_msg, **claim_scope, default_cron_timezone=getattr(agent, "instance_timezone", None))
    if schedule_msg.install_mode == "ensure":
        await _store_ensure(agent, schedule_msg, claim_scope)
    else:
        await _store_replace(agent, schedule_msg, claim_scope)
    if agent._scheduler_wake:
        agent._scheduler_wake.set()


async def _heartbeat_item_objective_is_inactive(agent: Any, item: Any) -> bool:
    objective_id = str(getattr(item, "objective_id", "") or "").strip()
    if not objective_id:
        return False
    objective = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_objective, objective_id
    )
    return bool(
        objective is None
        or getattr(objective.status, "value", objective.status) != "active"
    )


async def _dispatch_heartbeat_item_to_worker(
    agent: Any,
    *,
    store: HeartbeatStore,
    item: Any,
    task: Task,
    internal_header: MessageHeader,
    internal_meta: MessageMeta,
) -> Optional[bool]:
    if not item.target_agent_type:
        return None
    target_available = await agent._verify_delegation_target(item.target_agent_type)
    if not target_available:
        agent._logger.error(
            "[%s] Heartbeat item %s was not dispatched because target role %s "
            "is not a registered agent type. "
            "event=scheduled_worker_target_unavailable",
            agent.id,
            item.heartbeat_item_id,
            item.target_agent_type,
        )
        await agent.task_manager.operations_runtime.run(
            store.mark_item_run_finished, item.heartbeat_item_id, success=False
        )
        return False
    routing_key = agent.named_agent_type_queue_map.get(
        item.target_agent_type,
        f"{agent.team_id}_{item.target_agent_type}",
    )
    outbound = MessageEnvelope(
        msg_content=task,
        msg_meta=internal_meta,
        msg_header=MessageHeader(
            from_agent_id=internal_header.from_agent_id or agent.id,
            to_routing_path=routing_key,
            response_routing_key=internal_header.response_routing_key,
            agent_signature=internal_header.agent_signature,
        ),
    )
    await agent.team_comms.send_message(message=outbound, routing_key=routing_key)
    agent._logger.info(
        "[%s] Heartbeat item %s dispatched to %s with conversation_id=%s.",
        agent.id,
        item.heartbeat_item_id,
        item.target_agent_type,
        internal_meta.conversation_id,
    )
    return True


@dataclass(frozen=True)
class _HeartbeatRun:
    """One heartbeat item's run, ready to sign and start."""

    item: Any
    task: Task
    header: MessageHeader
    meta: MessageMeta
    owner: Any


def _heartbeat_item_meta(agent: Any, scheduled_message: ScheduledMessage, item: Any) -> MessageMeta:
    """The run's message meta: the launcher's, for this item and its conversation."""

    internal_meta = (
        scheduled_message.payload_meta.model_copy(deep=True)
        if scheduled_message.payload_meta is not None
        else MessageMeta(
            from_agent_name=agent.name,
            requester_type=RequesterType.TIMER_REQUESTER,
            is_response_requested=False,
        )
    )
    internal_meta.requester_type = RequesterType.TIMER_REQUESTER
    internal_meta.conversation_id = item.conversation_id
    internal_meta.followup_to_task_id = None
    internal_meta.to_named_agent_type = item.target_agent_type
    internal_meta.objective_id = item.objective_id
    if internal_meta.gateway_context is not None:
        gateway_copy = internal_meta.gateway_context.model_copy(deep=True)
        gateway_copy.scheduled_job_id = item.heartbeat_item_id
        internal_meta.gateway_context = gateway_copy
    return internal_meta


async def _heartbeat_objective_allowed(
    agent: Any, scheduled_message: ScheduledMessage, item: Any, *, person_id: Optional[str], roles: tuple[str, ...]
) -> bool:
    """A shared launcher never hands one person's Objective to another person.

    A heartbeat joins and changes its Objective and Plan at admission, so it
    needs change authority before any conversation or run state is written.
    Use the same current, vouched roles the scheduler will sign.
    """

    if not item.objective_id:
        return True
    objective = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_objective, item.objective_id
    )
    if objective is not None and objective_access(
        person_id=person_id,
        owner_id=objective.owner_canonical_user_id,
        objective_id=objective.objective_id,
        action=PRIVATE_UPDATE,
        roles=roles,
    ).allowed:
        return True
    agent._logger.warning(
        "Skipped a heartbeat item because its launcher cannot change the linked Objective. "
        "event=heartbeat_objective_access_refused schedule_id=%s heartbeat_item_id=%s objective_id=%s person_id=%s",
        scheduled_message.schedule_id, item.heartbeat_item_id, item.objective_id, person_id,
    )
    return False


async def _prepared_heartbeat_run(
    agent: Any, scheduled_message: ScheduledMessage, item: Any, store: HeartbeatStore
) -> Optional[_HeartbeatRun]:
    """The item's run for the schedule's owner; None (logged) when it must not run now.

    The run continues the item's conversation only when that belongs to the
    run's person; otherwise the item gets a new one first
    (``heartbeat_store.item_conversation_for_run``), before the run is signed.
    """

    if await _heartbeat_item_objective_is_inactive(agent, item):
        agent._logger.info(
            "[%s] Heartbeat item %s was skipped because Objective %s is inactive. "
            "event=inactive_objective_heartbeat_suppressed",
            agent.id,
            item.heartbeat_item_id,
            item.objective_id,
        )
        return None

    task = Task(
        task_id=new_id("task_id"),
        task_type=MessageMetaType.REQUEST,
        problem_statement=item.instruction,
    )
    internal_header = (
        scheduled_message.payload_header.model_copy(deep=True)
        if scheduled_message.payload_header is not None
        else MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=RoutingType.SELF_ROUTE.value,
        )
    )
    internal_meta = _heartbeat_item_meta(agent, scheduled_message, item)
    owner = await asyncio.to_thread(run_owner, agent, internal_meta)
    if inactive_owner(owner, schedule_id=item.heartbeat_item_id):
        return None
    if item.objective_id:
        roles = scheduled_owner_roles(
            internal_meta.gateway_context, owner, schedule_id=item.heartbeat_item_id,
            system_managed=is_system_managed_schedule(scheduled_message),
        ) if owner is not None else ()
        if not await _heartbeat_objective_allowed(
            agent, scheduled_message, item, person_id=_person_id(owner), roles=roles,
        ):
            return None
    conversation_id = await agent.task_manager.operations_runtime.run(
        partial(item_conversation_for_run, store, getattr(agent, "_conversation_store", None)),
        item,
        _person_id(owner),
    )
    if conversation_id is None:
        agent._logger.info(
            "[%s] Heartbeat item %s was removed while its run was prepared; it does not run. "
            "event=heartbeat_item_removed_before_run",
            agent.id,
            item.heartbeat_item_id,
        )
        return None
    internal_meta.conversation_id = conversation_id
    return _HeartbeatRun(item, task, internal_header, internal_meta, owner)


async def _signed_heartbeat_runs(
    agent: Any,
    scheduled_message: ScheduledMessage,
    items: list[Any],
    claim_token: Optional[str],
    store: HeartbeatStore,
) -> Optional[list[_HeartbeatRun]]:
    """Every item's run with its signed context; None when one cannot be signed.

    All are signed before any starts, so a refusal or a manager that does not
    answer starts nothing and the claim is released for a retry, without
    running some items twice.
    """

    runs = []
    for item in items:
        run = await _prepared_heartbeat_run(agent, scheduled_message, item, store)
        if run is None:
            continue
        signing = ScheduledRun(
            scheduled_message,
            claim_token,
            item.heartbeat_item_id,
            run.owner,
            heartbeat_item_id=item.heartbeat_item_id,
        )
        if not await sign_scheduled_run(agent, run.meta, signing):
            return None
        if item.objective_id:
            # A manager reply or live permit check may carry newer roles than
            # preparation saw. Decide once more from the actual signed identity.
            identity = await asyncio.to_thread(resolve_meta_identity, agent, run.meta)
            if identity is None:
                agent._logger.warning(
                    "Skipped a linked heartbeat item without a verified run identity. "
                    "event=heartbeat_objective_identity_unverified heartbeat_item_id=%s objective_id=%s",
                    item.heartbeat_item_id, item.objective_id,
                )
                continue
            if not await _heartbeat_objective_allowed(
                agent, scheduled_message, item, person_id=identity.person_id, roles=identity.person_roles,
            ):
                continue
        runs.append(run)
    return runs


async def _start_heartbeat_run(
    agent: Any,
    *,
    scheduled_message: ScheduledMessage,
    store: HeartbeatStore,
    source_id: str,
    run: _HeartbeatRun,
) -> bool:
    item, task, internal_header, internal_meta = run.item, run.task, run.header, run.meta
    await agent.task_manager.operations_runtime.run(
        store.mark_item_run_started, item.heartbeat_item_id
    )
    worker_dispatched = await _dispatch_heartbeat_item_to_worker(
        agent,
        store=store,
        item=item,
        task=task,
        internal_header=internal_header,
        internal_meta=internal_meta,
    )
    if worker_dispatched is not None:
        return worker_dispatched
    if await _coalesce_scheduled_fire_if_active(
        agent,
        item.heartbeat_item_id,
        schedule_id=scheduled_message.schedule_id,
    ):
        await agent.task_manager.operations_runtime.run(
            store.mark_item_run_finished, item.heartbeat_item_id, success=False
        )
        return False
    # The item's run is signed before it starts (_signed_heartbeat_runs), so
    # its meta proves whom the task works for.
    owner_person_id = await asyncio.to_thread(verified_meta_person_id, agent, internal_meta)
    if not await agent.task_manager.operations_runtime.run(
        agent.task_manager.add_task, task, internal_header, internal_meta,
        owner_person_id=owner_person_id,
    ):
        agent._logger.warning(
            "[%s] Could not admit heartbeat item %s as task %s; leaving it "
            "for retry on next tick.",
            agent.id,
            item.heartbeat_item_id,
            task.task_id,
        )
        await agent.task_manager.operations_runtime.run(
            store.mark_item_run_finished, item.heartbeat_item_id, success=False
        )
        return False

    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task.task_id,
        f"Heartbeat item {item.heartbeat_item_id} launched from source {source_id}.",
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={
            "heartbeat_item_id": item.heartbeat_item_id,
            "heartbeat_source_id": source_id,
        },
    )
    return True


async def enqueue_heartbeat_items(
    agent: Any,
    scheduled_message: ScheduledMessage,
    *,
    claim_token: Optional[str] = None,
) -> bool:
    """Start every enabled item of a heartbeat schedule; False starts none and leaves it to retry."""

    gateway_context = getattr(scheduled_message.payload_meta, "gateway_context", None)
    source_id = getattr(gateway_context, "heartbeat_source_id", None)
    if not source_id:
        agent._logger.warning(
            f"[{agent.id}] Heartbeat launcher schedule {scheduled_message.schedule_id} omitted heartbeat_source_id."
        )
        return False

    store = HeartbeatStore(
        db_path=agent.task_manager._db_path,
        logger_=agent._logger,
        storage_settings=agent.task_manager.storage_settings,
    )
    items = await agent.task_manager.operations_runtime.run(
        store.list_items, source_id, enabled_only=True
    )
    if not items:
        agent._logger.info(
            f"[{agent.id}] Heartbeat launcher {scheduled_message.schedule_id} found no enabled items for source {source_id}."
        )
        return True

    runs = await _signed_heartbeat_runs(agent, scheduled_message, items, claim_token, store)
    if runs is None:
        return False
    accepted_any = False
    for run in runs:
        accepted = await _start_heartbeat_run(
            agent,
            scheduled_message=scheduled_message,
            store=store,
            source_id=source_id,
            run=run,
        )
        accepted_any = accepted or accepted_any

    if accepted_any:
        schedule_pending_work(agent)
    return True


async def enqueue_provider_overload_retry(
    agent: Any,
    schedule_id: str,
    scheduled_message: ScheduledMessage,
) -> bool:
    task_id = getattr(scheduled_message.payload, "task_id", None)
    if not task_id:
        agent._logger.warning(
            "[%s] Provider-overload retry schedule %s omitted payload task_id.",
            agent.id,
            schedule_id,
        )
        return True

    task_info = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_task_info, task_id
    )
    if task_info is None:
        agent._logger.info(
            "[%s] Skipping provider-overload retry schedule %s because task %s is no longer active.",
            agent.id,
            schedule_id,
            task_id,
        )
        return True
    if await agent.task_manager.operations_runtime.run(
        agent.task_manager.is_terminal_state, task_info.state
    ):
        agent._logger.info(
            "[%s] Skipping provider-overload retry schedule %s because task %s is already %s.",
            agent.id,
            schedule_id,
            task_id,
            task_info.state.value,
        )
        return True

    now_ts = datetime.now(timezone.utc).isoformat()
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_runtime_resume_directive,
        task_id,
        (
            PROVIDER_COOLDOWN_RETRY_DIRECTIVE
            if str(scheduled_message.schedule_name or "").endswith(f":{PROVIDER_COOLDOWN_RETRY_MARKER}")
            else agent._runtime_resume_directive(
                TaskQueryDecision.RETRY.value,
                RecoverablePauseTrigger.PROVIDER_OVERLOADED.value,
            )
        ),
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task_id,
        "Provider-overload retry timer fired; task re-queued for another model attempt.",
        entry_type=TaskHistoryType.QUERY_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=now_ts,
        details={
            "reason": "provider_overload_retry_timer_fired",
            "schedule_id": schedule_id,
            "schedule_name": scheduled_message.schedule_name,
        },
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.update_main_task_state,
        task_id,
        ProgressState.ACCEPTED,
        now_ts,
    )
    if not await agent.task_manager.operations_runtime.run(
        agent.task_manager.enqueue_task, task_id, front=True
    ):
        agent._logger.warning(
            "[%s] Could not enqueue provider-overload retry task %s for schedule %s.",
            agent.id,
            task_id,
            schedule_id,
        )
        return False
    schedule_pending_work(agent)
    return True


def prepare_memory_review_schedule(
    agent: Any,
    schedule_id: str,
    scheduled_message: ScheduledMessage,
) -> ScheduledMessage:
    """Prepare review data on the local specialist before ordinary task admission.

    The ``memory_review`` preparer of the system schedule table; it runs only
    on MemReviewAgent (``_prepare_system_timer``).
    """
    from custom_messages.contracts.runtime_v1 import MEM_REVIEW_AGENT_TYPE

    review_kind = memory_review_schedule_kind(scheduled_message.schedule_name)
    if review_kind is None:
        agent._logger.warning(
            "[%s] Memory review schedule %s has unknown schedule_name=%s.",
            agent.id,
            schedule_id,
            scheduled_message.schedule_name,
        )
        raise ValueError("unknown_memory_review_schedule_kind")

    jobs: list[Any] = []
    owner = owner_canonical_user_id_for_schedule(scheduled_message)
    if not owner:
        raise ValueError("memory_review_schedule_owner_missing")
    if review_kind == COREMEM_AUDIT_REVIEW_KIND:
        job = enqueue_pinned_memory_audit_job(
            memory_review_store=getattr(agent, "_memory_review_store", None),
            pinned_memory_repository=getattr(agent, "_pinned_memory_repository", None),
            team_id=getattr(agent, "team_id", ""),
            named_agent_type=getattr(agent, "named_agent_type", None),
            agent_id=getattr(agent, "id", None),
            memory_config=getattr(agent, "memory_config", None),
            bank="coremem",
            canonical_user_id=owner,
            logger=getattr(agent, "_logger", None),
        )
        if job is not None:
            jobs.append(job)
    elif review_kind == USERMEM_AUDIT_REVIEW_KIND:
        job = enqueue_pinned_memory_audit_job(
            memory_review_store=getattr(agent, "_memory_review_store", None),
            pinned_memory_repository=getattr(agent, "_pinned_memory_repository", None),
            team_id=getattr(agent, "team_id", ""),
            named_agent_type=getattr(agent, "named_agent_type", None),
            agent_id=getattr(agent, "id", None),
            memory_config=getattr(agent, "memory_config", None),
            bank="usermem",
            canonical_user_id=owner,
            logger=getattr(agent, "_logger", None),
        )
        if job is not None:
            jobs.append(job)
    else:
        jobs.extend(
            enqueue_stale_conversation_review_jobs(
                memory_review_store=getattr(agent, "_memory_review_store", None),
                conversation_store=getattr(agent, "_conversation_store", None),
                team_id=getattr(agent, "team_id", ""),
                named_agent_type=getattr(agent, "named_agent_type", None),
                agent_id=getattr(agent, "id", None),
                memory_config=getattr(agent, "memory_config", None),
                canonical_user_id=owner,
                logger=getattr(agent, "_logger", None),
            )
        )

    # A crash after creating review jobs but before admitting the occurrence
    # must reuse the saved jobs. Claimed jobs are retained for outcome review.
    known_ids = {job.job_id for job in jobs}
    for job in agent._memory_review_store.list_review_jobs(limit=1000, owner_team_id=agent.team_id, owner_named_agent_type=MEM_REVIEW_AGENT_TYPE, pending_only=True):
        if (
            job.owner_team_id == agent.team_id
            and job.owner_named_agent_type == MEM_REVIEW_AGENT_TYPE
            and job.status in {"queued", "claimed"}
            and job.source_payload.get("review_kind") == review_kind
            and (review_kind == COREMEM_AUDIT_REVIEW_KIND or job.source_payload.get("canonical_user_id") == owner)
        ):
            known_ids.add(job.review_job_id)
    prepared = scheduled_message.model_copy(deep=True)
    prepared.payload.problem_statement = (
        "Perform this scheduled memory review locally. Use get_memory_review_job "
        "with each exact saved job ID below to inspect its current status and source. "
        "Read needed source pages through detail.next_offset with an unchanged sha256; "
        "complete, skip, or fail each through "
        "resolve_memory_review_job. Preserve jobs already claimed by another worker. "
        "Use the existing memory tools and your current access. Do not delegate "
        "the timer back to TeamArchitect or create another schedule. "
        "If there are no review jobs, finish quietly without changing memory.\n"
        f"{memory_review_notification_instruction(agent.memory_config)}\n"
        f"Review kind: {review_kind}\nSaved review job IDs: {sorted(known_ids)}\n"
    )
    agent._logger.info(
        "[%s] Memory review schedule %s enqueued %d MemReview job(s).",
        agent.id,
        schedule_id,
        len(jobs),
    )
    return prepared


# The preparers the system schedule table names (custom_messages.system_schedules,
# ``preparer``). Each prepares a fired run of its kinds on the agent type that
# runs them, before the run is admitted for the schedule's owner.
# A preparer returns None when the run has nothing to do: the run is skipped
# and no worker is started for it.
SystemSchedulePreparer = Callable[[Any, str, ScheduledMessage], Optional[ScheduledMessage]]
SYSTEM_SCHEDULE_PREPARERS: dict[str, SystemSchedulePreparer] = {
    MEMORY_REVIEW_PREPARER: prepare_memory_review_schedule,
    KNOWLEDGE_CURATION_PREPARER: prepare_knowledge_curation_schedule,
}


async def _prepare_system_timer(
    agent: Any, schedule_id: str, scheduled_message: ScheduledMessage
) -> Optional[ScheduledMessage]:
    """Run the table's preparer of a system timer an on-demand worker runs; any other schedule is unchanged.

    Only the timer's own agent type prepares it: an old memory timer still
    claimed by TeamArchitect fails with ``memory_review_schedule_upgrade_required``
    until the one-time upgrade (``custom_agents.local_schedule_upgrade``).
    """

    from custom_agents.local_schedule_policy import local_system_timer_spec

    spec = local_system_timer_spec(scheduled_message)
    if spec is None or spec.preparer is None:
        return scheduled_message
    if agent.named_agent_type != spec.agent_type:
        raise ValueError(f"{spec.name_family}_schedule_upgrade_required")
    preparer = SYSTEM_SCHEDULE_PREPARERS[spec.preparer]
    prepared: Optional[ScheduledMessage] = await agent.task_manager.operations_runtime.run(
        partial(preparer, agent), schedule_id, scheduled_message
    )
    return prepared


# A retry a worker scheduled itself after its model provider refused calls
# (custom_agents.provider_limits); nobody chose it, so the directive says so.
PROVIDER_COOLDOWN_RETRY_MARKER = "provider_limit_cooldown"
PROVIDER_COOLDOWN_RETRY_DIRECTIVE = (
    "Runtime retry instruction: the model provider refused calls earlier (rate limit, quota "
    "or overload), so this task waited and now continues. Retry the interrupted model work "
    "from the current task state. Do not restart completed work or repeat completed research "
    "unless the current task history shows it is necessary."
)

# Delegation pickup checks were one-shot internal schedules named with this
# prefix until worker pools replaced them (2026-10-04). Remove this handling in
# the release after the one that ships worker pools.
OBSOLETE_DELEGATION_ASSURANCE_PREFIX = DELEGATION_ASSURANCE_SCHEDULE_NAME_PREFIX


def _schedule_event_kind(schedule_name: str) -> tuple[str, str]:
    """How the runtime coordinator classifies a fired schedule: (event type, event class).

    From the system schedule table (``coordinator_event``) for the kind the
    name belongs to; anything else is an ordinary scheduled message.
    """

    spec = system_schedule_spec_for_name(schedule_name)
    if spec is not None and spec.coordinator_event is not None:
        return spec.coordinator_event
    return "scheduled_message", "deferrable"


async def _deferred_while_blocked(
    agent: Any, schedule_id: str, scheduled_message: ScheduledMessage
) -> bool:
    """Whether the runtime coordinator deferred this fire; a failed check is logged and defers nothing."""

    try:
        from custom_agents.task_runtime_coordinator import (
            scheduled_message_root_task_id,
        )

        event_type, event_class = _schedule_event_kind(
            str(scheduled_message.schedule_name or "")
        )
        root_task_id = scheduled_message_root_task_id(
            agent.task_manager.resolve_root_task_id, scheduled_message
        )
        return bool(
            await agent.task_manager.operations_runtime.run(
                agent._runtime_coordinator().defer_envelope_if_blocked,
                root_task_id=root_task_id,
                event_type=event_type,
                event_class=event_class,
                content=scheduled_message,
                header=scheduled_message.payload_header,
                meta=scheduled_message.payload_meta,
                related_task_id=getattr(scheduled_message.payload, "task_id", None),
                schedule_id=schedule_id,
            )
        )
    except Exception as exc:
        agent._logger.warning(
            "[%s] Runtime coordinator schedule deferral check failed for schedule %s: %s",
            agent.id,
            schedule_id,
            exc,
        )
        return False


async def _enqueue_runtime_schedule(
    agent: Any,
    schedule_id: str,
    scheduled_message: ScheduledMessage,
) -> Optional[bool]:
    """Fires of the runtime's own schedule kinds, which have their own paths; None for any other schedule.

    The kind comes from the name (``custom_messages.system_schedules``).
    """

    spec = system_schedule_spec_for_name(scheduled_message.schedule_name)
    kind = spec.kind if spec is not None else None
    if kind is SystemScheduleKind.INTERNAL_IMPROVEMENT:
        return await enqueue_internal_improvement_job(agent, schedule_id, scheduled_message)
    if kind is SystemScheduleKind.ISOLATED_RECOVERY_CHECKPOINT:
        return await enqueue_isolated_recovery_checkpoint(agent, schedule_id, scheduled_message)
    if kind is SystemScheduleKind.PROVIDER_OVERLOAD_RETRY:
        return await enqueue_provider_overload_retry(agent, schedule_id, scheduled_message)
    return None


def _is_recurring(scheduled_message: ScheduledMessage) -> bool:
    return (
        scheduled_message.repeat_count is None
        or (scheduled_message.repeat_count or 0) > 1
        or scheduled_message.interval_ms is not None
        or scheduled_message.cron_expression is not None
    )


def _fired_task(
    scheduled_message: ScheduledMessage, schedule_admission: ScheduleOccurrenceV1 | None
) -> Task:
    task = scheduled_message.payload.model_copy(deep=True)
    task.problem_statement = _append_suppressed_completion_instruction(
        task.problem_statement
    )
    task.problem_statement = _append_schedule_memory_context(
        task.problem_statement, scheduled_message
    )
    if _is_recurring(scheduled_message) and task.parent_task_id is None:
        task.task_id = new_id("task_id")
    if schedule_admission is not None:
        task.task_id = schedule_admission.task_id
    return task


def _fired_header_and_meta(
    agent: Any, scheduled_message: ScheduledMessage, task: Task
) -> tuple[MessageHeader, MessageMeta]:
    internal_header = (
        scheduled_message.payload_header.model_copy(deep=True)
        if scheduled_message.payload_header is not None
        else MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=RoutingType.SELF_ROUTE.value,
        )
    )
    internal_meta = (
        scheduled_message.payload_meta.model_copy(deep=True)
        if scheduled_message.payload_meta is not None
        else MessageMeta(
            from_agent_name=agent.name,
            requester_type=RequesterType.TIMER_REQUESTER,
            is_response_requested=False,
        )
    )
    internal_meta.requester_type = RequesterType.TIMER_REQUESTER
    internal_meta.objective_id = scheduled_message.objective_id or getattr(
        internal_meta, "objective_id", None
    )
    if _is_recurring(scheduled_message) and task.parent_task_id is None:
        internal_meta.followup_to_task_id = None
    internal_meta = agent._ensure_request_timezone(
        internal_meta,
        top_level_request=task.parent_task_id is None,
    )
    return internal_header, internal_meta


def _with_conversation(
    agent: Any,
    schedule_id: str,
    task: Task,
    header: MessageHeader,
    meta: MessageMeta,
    *,
    owner_person_id: Optional[str],
    is_aar_review_schedule: bool,
) -> Optional[MessageMeta]:
    """The run's meta with its conversation; None (logged) when the run cannot be admitted.

    The run is not admitted either when the schedule names a conversation of
    someone other than ``owner_person_id`` (``custom_agents.conversation_runtime``);
    the scheduler then releases its claim and retries later.

    ``owner_person_id`` is the schedule's owner the run is then signed for
    (None for a run for no person):
    the conversation is recorded before the run is signed (the signed context
    names the conversation), so the meta cannot prove the person yet.
    """

    if task.parent_task_id is not None or is_aar_review_schedule:
        return meta
    try:
        return ensure_task_conversation_context(
            conversation_store=agent._conversation_store,
            team_id=agent.team_id,
            owner_agent_id=agent.id,
            owner_agent_name=agent.name,
            task_request=task,
            msg_header=header,
            msg_meta=meta,
            owner_person_id=owner_person_id,
            owner_named_agent_type=getattr(agent, "named_agent_type", None),
        )
    except ValueError as exc:
        agent._logger.warning(
            f"[{agent.id}] Could not admit fired schedule {schedule_id} as task {task.task_id}: {exc}"
        )
        return None


async def _queue_fired_task(
    agent: Any,
    schedule_id: str,
    scheduled_message: ScheduledMessage,
    task: Task,
    header: MessageHeader,
    meta: MessageMeta,
    *,
    schedule_admission: ScheduleOccurrenceV1 | None,
) -> bool:
    """Store the run as a task and start pending work; False leaves the schedule pending to retry.

    The run is signed by now (``_enqueue_owner_run``), so the task's owner
    comes from its verified meta like any admitted task: the schedule's owner
    the run was signed for, or nobody for system work.
    """

    owner_person_id = await asyncio.to_thread(verified_meta_person_id, agent, meta)
    if not await agent.task_manager.operations_runtime.run(
        agent.task_manager.add_task, task, header, meta,
        owner_person_id=owner_person_id,
        **({"schedule_admission": schedule_admission} if schedule_admission is not None else {}),
    ):
        agent._logger.warning(
            f"[{agent.id}] Could not admit fired schedule {schedule_id} as task {task.task_id}. "
            "Leaving the schedule pending so it can retry."
        )
        return False

    if not _open_objective_work_session_for_fired_task(
        agent,
        schedule_id,
        scheduled_message,
        task,
        meta,
    ):
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.remove_task, task.task_id
        )
        return False

    if task.parent_task_id is None and getattr(meta, "conversation_id", None):
        record_task_conversation_history(
            task_manager=agent.task_manager,
            task_id=task.task_id,
            conversation_id=meta.conversation_id,
            followup_to_task_id=getattr(meta, "followup_to_task_id", None),
        )

    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task.task_id,
        f"ScheduledMessage {schedule_id} fired and queued this task for execution.",
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={
            "schedule_id": schedule_id,
            "objective_id": scheduled_message.objective_id,
        },
    )
    schedule_pending_work(agent)
    return True


def _person_id(owner: Any) -> Optional[str]:
    """The person a run works for (``run_owner``), or None when it names nobody."""

    return owner.person_id if owner is not None else None


def _runs_for_nobody(scheduled_message: ScheduledMessage) -> bool:
    from custom_agents.local_schedule_policy import local_system_timer_spec

    spec = local_system_timer_spec(scheduled_message)
    return spec is not None and spec.system_work


def _sign_for_nobody(agent: Any, meta: Any, schedule_id: str) -> bool:
    """Sign a run as system work for no person; False (logged) when nothing could sign it."""

    sign_system_run(agent, meta, schedule_id=schedule_id)
    gateway_context = getattr(meta, "gateway_context", None)
    if getattr(gateway_context, "task_context", None) or getattr(gateway_context, "schedule_run", None):
        return True
    agent._logger.warning(
        "A scheduled run for no person could not be signed; it waits for the next try. "
        "event=system_work_run_unsigned agent_id=%s schedule_id=%s",
        getattr(agent, "id", None),
        schedule_id,
    )
    return False


async def _enqueue_owner_run(
    agent: Any,
    schedule_id: str,
    scheduled_message: ScheduledMessage,
    *,
    owner: Any,
    is_aar_review_schedule: bool,
    schedule_admission: ScheduleOccurrenceV1 | None,
    claim_token: Optional[str] = None,
) -> bool:
    """A schedule's run as a task that works for its owner.

    False, without starting the run, when its signed context cannot be had
    (``custom_agents.scheduled_run_contexts``); the caller then releases the
    claim so the schedule is retried.
    """

    task = _fired_task(scheduled_message, schedule_admission)
    internal_header, internal_meta = _fired_header_and_meta(agent, scheduled_message, task)
    for_nobody = _runs_for_nobody(scheduled_message)
    if for_nobody:
        # The schedule names the conversation of the session it was installed
        # with, which is a person's (the team's proactive session); a run for
        # no person must not continue it, so it starts its own, owned by nobody
        # (custom_agents.conversation_runtime).
        start_new_conversation(internal_meta)
    internal_meta = _with_conversation(
        agent,
        schedule_id,
        task,
        internal_header,
        internal_meta,
        owner_person_id=None if for_nobody else _person_id(owner),
        is_aar_review_schedule=is_aar_review_schedule,
    )
    if internal_meta is None:
        return False
    scheduled_job_id = schedule_id
    gateway_context = getattr(internal_meta, "gateway_context", None)
    if getattr(gateway_context, "scheduled_job_id", None):
        scheduled_job_id = gateway_context.scheduled_job_id
    # Each firing gets a fresh context signed by the scheduler for the owner,
    # in this process or by the manager for a worker process without the key.
    # A kind whose runs work for no person (system schedule table,
    # ``system_work``) is signed as system work instead.
    if for_nobody:
        if not await asyncio.to_thread(_sign_for_nobody, agent, internal_meta, scheduled_job_id):
            return False
    elif not await sign_scheduled_run(
        agent,
        internal_meta,
        ScheduledRun(scheduled_message, claim_token, scheduled_job_id, owner),
    ):
        return False
    if await _coalesce_scheduled_fire_if_active(
        agent,
        scheduled_job_id,
        schedule_id=schedule_id,
        schedule_name=getattr(scheduled_message, "schedule_name", None),
        notification_header=internal_header,
        notification_meta=internal_meta,
        schedule_admission=schedule_admission,
    ):
        return True
    return await _queue_fired_task(
        agent,
        schedule_id,
        scheduled_message,
        task,
        internal_header,
        internal_meta,
        schedule_admission=schedule_admission,
    )


async def enqueue_fired_scheduled_task(
    agent: Any,
    schedule_id: str,
    scheduled_message: ScheduledMessage,
    *,
    from_runtime_mailbox: bool = False,
    schedule_admission: ScheduleOccurrenceV1 | None = None,
    claim_token: Optional[str] = None,
) -> bool:
    """Handle one fire: the runtime's own kinds by their paths, anything else as a run for its owner.

    ``claim_token`` is this agent's claim on the schedule row; a worker
    without the scheduler key shows it to the manager to get the run signed.
    """

    if str(scheduled_message.schedule_name or "").startswith(OBSOLETE_DELEGATION_ASSURANCE_PREFIX):
        # Pickup checks of the release before worker pools (ADR-019). One left
        # by an upgrade is dropped here; it would otherwise run as a task.
        agent._logger.warning(
            "Dropped a pickup check left by an older release. "
            "event=obsolete_delegation_assurance_schedule_dropped agent_id=%s schedule_id=%s",
            agent.id,
            schedule_id,
        )
        return True
    if (
        not from_runtime_mailbox
        and schedule_admission is None
        and await _deferred_while_blocked(agent, schedule_id, scheduled_message)
    ):
        return True
    if is_heartbeat_launcher(scheduled_message):
        return await enqueue_heartbeat_items(agent, scheduled_message, claim_token=claim_token)
    prepared = await _prepare_system_timer(agent, schedule_id, scheduled_message)
    if prepared is None:
        # The table's preparer found nothing to do (knowledge curation); the
        # occurrence is done without a run.
        return True
    scheduled_message = prepared
    handled = await _enqueue_runtime_schedule(agent, schedule_id, scheduled_message)
    if handled is not None:
        return handled
    is_aar_review_schedule = str(scheduled_message.schedule_name or "").startswith(
        AFTER_ACTION_REPORT_REVIEW_SCHEDULE_NAME_PREFIX
    )
    if is_aar_review_schedule:
        refreshed = await prepare_after_action_report_batch_review(
            agent, schedule_id, scheduled_message
        )
        if refreshed is SKIP_AFTER_ACTION_REPORT_REVIEW:
            return True
        if refreshed is None:
            return False
        scheduled_message = refreshed

    if await _objective_work_schedule_is_inactive(
        agent, schedule_id, scheduled_message
    ):
        return True
    # A disabled or removed owner's schedule does not run; the run is signed
    # for this owner with their current roles.
    owner = await asyncio.to_thread(run_owner, agent, scheduled_message.payload_meta)
    if inactive_owner(owner, schedule_id=schedule_id):
        return True
    return await _enqueue_owner_run(
        agent,
        schedule_id,
        scheduled_message,
        owner=owner,
        is_aar_review_schedule=is_aar_review_schedule,
        schedule_admission=schedule_admission,
        claim_token=claim_token,
    )


async def prepare_after_action_report_batch_review(
    agent: Any,
    schedule_id: str,
    scheduled_message: ScheduledMessage,
) -> Optional[ScheduledMessage] | object:
    """Close the batch's collection and fill the review task with its reports.

    The review then runs as a run of its schedule, signed for the schedule's
    owner: the person the reports are about
    (``custom_agents.after_action_report_identity``). A batch saved before the
    person was recorded is closed unreviewed, and a schedule whose owner is not
    the batch's person does not run; both are skipped (logged).
    """
    from custom_agents.after_action_report_identity import review_runs_for_batch_person

    raw_name = str(scheduled_message.schedule_name or "")
    parts = raw_name.split(":", 1)
    if len(parts) != 2 or not parts[1]:
        agent._logger.warning(
            "[%s] Invalid AAR batch review schedule name: %s", agent.id, raw_name
        )
        return None
    batch_id = parts[1]
    task_id = scheduled_message.payload.task_id
    closed = await agent.task_manager.operations_runtime.run(
        agent.task_manager.close_after_action_report_batch_collection_for_review,
        batch_id,
        scheduled_review_task_id=task_id,
    )
    if closed is None:
        agent._logger.warning(
            "[%s] Could not close AAR batch collection for schedule %s batch %s.",
            agent.id,
            schedule_id,
            batch_id,
        )
        return None
    if not closed.identity_verified:
        # Closed unreviewed by the task manager (logged there): nobody can say whose work it describes.
        return SKIP_AFTER_ACTION_REPORT_REVIEW
    if not review_runs_for_batch_person(closed, scheduled_message, schedule_id=schedule_id):
        return SKIP_AFTER_ACTION_REPORT_REVIEW
    pending_count = await agent.task_manager.operations_runtime.run(
        agent.task_manager.count_pending_after_action_report_reviews_for_batch, batch_id
    )
    if pending_count <= 0:
        agent._logger.info(
            "[%s] Skipping scheduled AAR review task %s for batch %s because no accepted reports remain pending after the collection window.",
            agent.id,
            task_id,
            batch_id,
        )
        return SKIP_AFTER_ACTION_REPORT_REVIEW
    review_task = await agent.task_manager.operations_runtime.run(
        agent.task_manager.build_after_action_report_batch_review_task,
        batch_id,
        task_id=task_id,
    )
    if review_task is None:
        agent._logger.warning(
            "[%s] Could not hydrate AAR batch review task for schedule %s batch %s.",
            agent.id,
            schedule_id,
            batch_id,
        )
        return None
    refreshed = scheduled_message.model_copy(deep=True)
    refreshed.payload = review_task
    agent._logger.info(
        "[%s] Hydrated scheduled AAR review task %s for batch %s.",
        agent.id,
        review_task.task_id,
        batch_id,
    )
    return refreshed


async def enqueue_isolated_recovery_checkpoint(
    agent: Any,
    schedule_id: str,
    scheduled_message: ScheduledMessage,
) -> bool:
    raw_name = str(scheduled_message.schedule_name or "")
    parts = raw_name.split(":", 2)
    if len(parts) != 3:
        agent._logger.warning(
            "[%s] Invalid isolated recovery checkpoint schedule name: %s",
            agent.id,
            raw_name,
        )
        return True
    _, original_task_id, recovery_task_id = parts

    original_info = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_task_info, original_task_id
    )
    if original_info is None:
        agent._logger.info(
            "[%s] Skipping recovery checkpoint %s because original task %s is no longer active.",
            agent.id,
            schedule_id,
            original_task_id,
        )
        return True

    recovery_info = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_task_info, recovery_task_id
    )
    if recovery_info is None or await agent.task_manager.operations_runtime.run(
        agent.task_manager.is_terminal_state, recovery_info.state
    ):
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_history_entry,
            original_task_id,
            "Isolated recovery checkpoint skipped because recovery is no longer active.",
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.NORMAL,
            related_task_id=recovery_task_id,
            details={"schedule_id": schedule_id, "recovery_task_id": recovery_task_id},
        )
        return True

    query = TaskQuery(
        query_type=MessageMetaType.REQUEST,
        parent_task_id=original_task_id,
        message=(
            "Troubleshooting has been running for about 5 minutes. "
            "Should I continue investigating, stop and return partial findings, or abort the task?"
        ),
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.add_task_query,
        original_task_id,
        query,
        agent_name=agent.name,
        agent_id=agent.id,
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.record_plan_action,
        original_task_id,
        PlanActionType.ASK_USER,
        "Asked the user whether to continue isolated recovery, return partial findings, or abort.",
        status=PlanActionStatus.PENDING,
        reason="isolated_recovery_checkpoint_elapsed",
        related_task_id=recovery_task_id,
        recovery_task_id=recovery_task_id,
        user_visible=True,
        details={"schedule_id": schedule_id, "task_query_id": query.task_query_id},
        agent_name=agent.name,
        agent_id=agent.id,
    )

    requester_header = original_info.header
    requester_meta = original_info.meta
    routing_key = (
        requester_header.response_routing_key or requester_header.from_agent_id
    )
    if not routing_key:
        agent._logger.warning(
            "[%s] Recovery checkpoint %s could not route TaskQuery %s; no requester route.",
            agent.id,
            schedule_id,
            query.task_query_id,
        )
        return True

    outgoing_meta = requester_meta.model_copy(deep=True)
    outgoing_meta.from_agent_name = agent.name
    outgoing_meta.requester_type = RequesterType.AGENT_REQUESTER
    outgoing_meta.is_response_requested = True
    envelope = MessageEnvelope(
        msg_content=query,
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=routing_key,
            response_routing_key=agent.id,
            agent_signature=getattr(requester_header, "agent_signature", None),
        ),
        msg_meta=outgoing_meta,
    )
    await agent.team_comms.send_message(message=envelope, routing_key=routing_key)
    return True


_SCHEDULER_MAX_SLEEP_SECONDS = MAX_SCHEDULER_SLEEP_SECONDS
_SCHEDULER_FAILURE_BACKOFF_SECONDS = 1.0
_SCHEDULER_RECENT_ERROR_LIMIT = 10


async def scheduler_loop(
    agent: Any,
    *,
    sleep_fn: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Run scheduler passes until the agent exits.

    Each pass renews the worker launch lease, refreshes agent state, recovers
    stalled subtasks, supervises schedules, lets the team lead issue schedules'
    run permits (``custom_agents.schedule_permits``), and fires at most one
    due schedule.
    One failed pass must not stop later passes: a transient storage error used
    to end the loop, which stopped lease renewal and schedule firing for the
    rest of the process lifetime. Unexpected errors are logged with a traceback
    and retried with capped exponential backoff. A stale launch lease is the
    exception: another owner holds this launch, so the loop stops claiming work
    and the worker winds down (custom_agents.lease_loss_wind_down).
    """
    claim_retry_deadlines: dict[str, datetime] = {}
    consecutive_failures = 0
    # Recent pass errors explain a later lease loss in the task handback.
    recent_errors: deque[str] = deque(maxlen=_SCHEDULER_RECENT_ERROR_LIMIT)
    await _fill_missing_cron_timezones(agent)
    try:
        while not agent._should_exit.is_set():
            try:
                sleep_seconds = await _scheduler_pass(agent, claim_retry_deadlines, sleep_fn)
                consecutive_failures = 0
            except LaunchConflict as exc:
                agent._logger.error(
                    "Scheduler stopped: this runtime no longer holds its launch lease. "
                    "event=scheduler_launch_lease_lost agent_id=%s reason=%s",
                    agent.id,
                    exc,
                )
                start_lease_loss_wind_down(
                    agent, reason=str(exc), recent_scheduler_errors=tuple(recent_errors)
                )
                return
            except Exception as exc:
                recent_errors.append(
                    f"{datetime.now(timezone.utc).isoformat()} {type(exc).__name__}: {exc}"
                )
                consecutive_failures += 1
                sleep_seconds = min(
                    _SCHEDULER_MAX_SLEEP_SECONDS,
                    _SCHEDULER_FAILURE_BACKOFF_SECONDS * 2 ** (consecutive_failures - 1),
                )
                agent._logger.exception(
                    "Scheduler pass failed; retrying. event=scheduler_pass_failed "
                    "agent_id=%s consecutive_failures=%s retry_in_seconds=%s",
                    agent.id,
                    consecutive_failures,
                    sleep_seconds,
                )
            if sleep_seconds is not None:
                await _wait_for_scheduler_wake(agent, sleep_seconds)
    except asyncio.CancelledError:
        agent._logger.info(f"[{agent.id}] Scheduler loop cancelled.")


async def _fill_missing_cron_timezones(agent: Any) -> None:
    """Give cron schedules saved before time zones their owner's (``ScheduleStore.fill_missing_cron_timezones``).

    Idempotent: every scheduler start runs it, and only schedules still
    without a time zone change. A failure is logged and leaves those
    schedules without one; they fail to compute their next run (and say so)
    until the next start fills them.
    """

    try:
        await asyncio.to_thread(agent._ensure_schedule_store().fill_missing_cron_timezones)
    except Exception:
        agent._logger.exception(
            "Could not give old cron schedules a time zone. event=schedule_cron_timezone_fill_failed agent_id=%s",
            agent.id,
        )


async def _wait_for_scheduler_wake(agent: Any, timeout_seconds: float) -> None:
    agent._scheduler_wake.clear()
    try:
        await asyncio.wait_for(agent._scheduler_wake.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        pass


async def _scheduler_pass(
    agent: Any,
    claim_retry_deadlines: dict[str, datetime],
    sleep_fn: Callable[[float], Awaitable[Any]],
) -> float | None:
    """Run one pass; return the idle wait, or None to start the next pass now."""
    from custom_agents.agent_instantiation_service import renew_runtime_launch

    observe_liveness(agent)
    await agent.task_manager.operations_runtime.run(partial(renew_runtime_launch, agent))
    await agent._refresh_self_agent_state()
    await check_queue_pressure(agent)
    await agent._recover_stalled_subtasks()
    # A task taken over after a restart may wait on a decision whose notice
    # went to its previous agent; find such decisions in the access store.
    try:
        await resume_decided_access_waits(agent)
    except Exception:
        agent._logger.exception(
            "Could not check for decided access requests. event=access_decision_check_failed agent_id=%s",
            agent.id,
        )
    # The lead sends out again any task a worker returned that is not settled.
    await process_pending_handbacks(agent)
    # ...and starts a replacement for a dead agent still holding unfinished work.
    try:
        observe_liveness(agent)
        await take_over_orphaned_work(agent)
    except Exception:
        agent._logger.exception(
            "Could not check for work held by dead agents. event=orphaned_work_check_failed agent_id=%s",
            agent.id,
        )

    # Counts on running delegated work for the people waiting on it.
    try:
        from custom_agents.delegation_wake import send_progress_notes

        await send_progress_notes(agent)
    except Exception:
        agent._logger.exception(
            "Could not send progress notes on delegated work. event=delegation_progress_failed agent_id=%s",
            agent.id,
        )
    # Tell people about stuck schedules and tasks, and when they work again.
    await report_stuck_schedules(agent)
    observe_liveness(agent)
    await report_stuck_tasks(agent)
    # Tell administrators about messages set aside since the last pass.
    await report_dead_letters(agent)
    # A task left holding the work slot with no turn must not block the queue
    # until new work happens to arrive.
    await release_orphaned_slot_when_idle(agent)
    # The team lead in the manager process issues and renews schedules' run
    # permits, so workers without the key need not ask it for each run.
    await maybe_renew_schedule_permits(agent)

    task_manager = getattr(agent, "task_manager", None)
    can_claim_scheduled_work = (
        not getattr(agent, "paused", False)
        and not getattr(agent, "is_executing", False)
        and (
            task_manager is None
            or not hasattr(task_manager, "can_start_new_task")
            or await task_manager.operations_runtime.run(
                task_manager.can_start_new_task,
            )
        )
    )
    now = datetime.now(timezone.utc)
    # Deadlines persist across passes; drop only the expired retry holds.
    for schedule_id in [key for key, deadline in claim_retry_deadlines.items() if deadline <= now]:
        del claim_retry_deadlines[schedule_id]
    claimed_schedule = None
    if can_claim_scheduled_work:
        claimed_schedule = await agent.task_manager.operations_runtime.run(
            agent._schedule_store.claim_next_due,
            claimer_agent_id=agent.id,
            claimer_named_agent_type=getattr(agent, "named_agent_type", None),
            claimer_team_id=getattr(agent, "team_id", None),
            claim_ttl_seconds=DEFAULT_SCHEDULE_CLAIM_TTL_SECONDS,
            allow_unscoped=bool(getattr(agent, "is_team_lead", False)),
            exclude_schedule_ids=tuple(claim_retry_deadlines),
        )

    if claimed_schedule:
        await _fire_claimed_schedule(agent, claimed_schedule, claim_retry_deadlines, sleep_fn)
        return None
    return await _idle_sleep_seconds(agent, can_claim_scheduled_work)


async def _fire_claimed_schedule(
    agent: Any,
    claimed_schedule: dict[str, Any],
    claim_retry_deadlines: dict[str, datetime],
    sleep_fn: Callable[[float], Awaitable[Any]],
) -> None:
    """Admit one claimed schedule, then advance it or release the claim."""
    schedule_id = claimed_schedule["schedule_id"]
    claim_token = claimed_schedule.get("claim_token")
    agent._logger.debug(
        f"[{agent.id}] Claimed schedule {schedule_id}; checking task admission"
    )

    try:
        scheduled_message = await agent.task_manager.operations_runtime.run(
            agent._schedule_store.get_scheduled_message, schedule_id
        )
        if scheduled_message is None:
            raise ValueError(
                f"ScheduledMessage {schedule_id} could not be reconstructed."
            )
        from custom_agents.schedule_occurrences import prepare_occurrence

        admission = prepare_occurrence(agent, scheduled_message, claim_token)
        receipt = None
        if admission is not None:
            receipt = await agent.task_manager.operations_runtime.run(
                agent._schedule_store.worker_records.get_occurrence,
                admission.occurrence_id, agent.team_id,
            )
        if receipt is not None:
            # Admission committed before the prior worker lost its
            # claim. Runtime recovery owns the task; advance once.
            accepted = True
        else:
            from custom_agents.schedule_worker_controller import schedule_ready_for_admission

            if await agent.task_manager.operations_runtime.run(partial(schedule_ready_for_admission, agent), scheduled_message):
                accepted = await agent._enqueue_fired_scheduled_task(
                    schedule_id, scheduled_message,
                    **({"schedule_admission": admission} if admission is not None else {}),
                    claim_token=claim_token,
                )
            else:
                accepted = False
    except Exception as exc:
        agent._logger.error(
            f"[{agent.id}] Error firing schedule {schedule_id}: {exc}"
        )
        accepted = False

    if accepted:
        await agent.task_manager.operations_runtime.run(
            agent._schedule_store.update_after_fire,
            schedule_id,
            claim_token=claim_token,
            claimer_agent_id=agent.id,
        )
    else:
        # A blocked earliest job must not starve later eligible jobs.
        from custom_agents.agent_instantiation_service import current_worker_limits

        claim_retry_deadlines[schedule_id] = datetime.now(timezone.utc) + timedelta(seconds=current_worker_limits(agent).defaults.retry_backoff_seconds)
        if claim_token:
            await agent.task_manager.operations_runtime.run(
                agent._schedule_store.release_claim,
                schedule_id,
                claim_token=claim_token,
                claimer_agent_id=agent.id,
            )
        await sleep_fn(1)


async def _idle_sleep_seconds(agent: Any, can_claim_scheduled_work: bool) -> float:
    """Wait until the next due schedule, capped by watchdog and lease cadence."""
    earliest = await agent.task_manager.operations_runtime.run(
        agent._schedule_store.get_earliest_fire_time
    )
    watchdog_sleep = max(1.0, float(agent.subtask_watchdog_interval_seconds))
    if earliest and can_claim_scheduled_work:
        now = datetime.now(timezone.utc)
        try:
            fire_time = datetime.fromisoformat(earliest)
            if fire_time.tzinfo is None:
                fire_time = fire_time.replace(tzinfo=timezone.utc)
            sleep_seconds = max(0.0, (fire_time - now).total_seconds())
            if sleep_seconds <= 0.0:
                sleep_seconds = 1.0
            sleep_seconds = min(sleep_seconds, _SCHEDULER_MAX_SLEEP_SECONDS)
        except (ValueError, TypeError):
            sleep_seconds = _SCHEDULER_MAX_SLEEP_SECONDS
    else:
        sleep_seconds = _SCHEDULER_MAX_SLEEP_SECONDS
    sleep_seconds = min(sleep_seconds, watchdog_sleep)
    if getattr(agent, "_worker_launch", None) is not None:
        from custom_agents.agent_instantiation_service import current_worker_limits

        sleep_seconds = min(sleep_seconds, current_worker_limits(agent).lease_seconds / 3)
    if getattr(agent, "is_team_lead", False):
        from custom_agents.agent_instantiation_service import current_worker_limits

        sleep_seconds = min(sleep_seconds, current_worker_limits(agent).reconcile_interval_seconds)
    return sleep_seconds
