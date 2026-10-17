"""TeamLead handling of tasks returned by workers that lost their launch lease.

The lead accepts a handback only from the worker that owns the stated launch:
the envelope sender must be the handback's worker, and the stored launch
record must name that worker for this team. Accepted handbacks are saved
before any further action so redelivery and restarts are safe.

The lead then sends each returned task out again, unchanged, on the shared
queue for the worker's agent type so a free worker of the type takes it
(ADR-019); the worker pool controller starts a worker if none is free. The
original reply route is kept, so the result still reaches the original
requester. For a task the lead delegated itself, its subtask record goes back
to "waiting in the queue" before the task is sent again, so it is not probed
until a worker claims it and that claim is never overwritten.

Processing runs right after receipt and on every lead scheduler pass, so a
publish failure or a lead restart only delays the re-dispatch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

from custom_agents.worker_interruption_escalation import (
    escalate_interruptions,
    interruption_limit,
    interruption_streak,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.redispatch_v1 import (
    TaskRedispatchNoticeV1,
    render_redispatch_directive,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.worker_handback_v1 import WorkerTaskHandbackV1
from custom_messages.enums import ProgressState
from id_generation import new_id
from storage.agent_launch_repository import AgentLaunchRepository
from storage.worker_handback_repository import (
    HANDBACK_STATUS_CLOSED_CANCELLED,
    HANDBACK_STATUS_CLOSED_FINISHED,
    HANDBACK_STATUS_CLOSED_SCHEDULE_INACTIVE,
    HANDBACK_STATUS_ESCALATED,
    HANDBACK_STATUS_RECEIVED,
    HANDBACK_STATUS_REDISPATCHED,
    HANDBACK_STATUS_SUPERSEDED,
    WorkerHandbackRepository,
)

# Pending handbacks retried by the scheduler pass wait this long between
# attempts, so one handback that keeps failing cannot flood the log.
HANDBACK_RETRY_INTERVAL_SECONDS = 30.0


async def handle_worker_task_handback(
    agent: Any,
    *,
    handback: WorkerTaskHandbackV1,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> bool:
    """Validate, save, and process one handback; True when newly saved."""

    _ = msg_meta
    if not getattr(agent, "is_team_lead", False):
        agent._logger.warning(
            "A task handback reached an agent that is not the TeamLead; ignored. "
            "event=worker_handback_misrouted agent_id=%s handback_id=%s",
            agent.id,
            handback.handback_id,
        )
        return False
    rejection = await agent.task_manager.operations_runtime.run(
        partial(_rejection_reason, agent), handback, msg_header
    )
    if rejection:
        agent._logger.warning(
            "Task handback rejected. event=worker_handback_rejected agent_id=%s "
            "handback_id=%s worker_agent_id=%s task_id=%s reason=%s",
            agent.id,
            handback.handback_id,
            handback.worker_agent_id,
            handback.handed_back.task_id,
            rejection,
        )
        return False
    repository = _handback_repository(agent)
    saved: bool = await agent.task_manager.operations_runtime.run(
        repository.record, handback
    )
    agent._logger.warning(
        "Worker returned a task after losing its launch lease. "
        "event=worker_task_handback_received agent_id=%s handback_id=%s "
        "worker_agent_id=%s launch_id=%s task_id=%s disposition=%s schedule_id=%s "
        "duplicate=%s",
        agent.id,
        handback.handback_id,
        handback.worker_agent_id,
        handback.evidence.launch_id,
        handback.handed_back.task_id,
        handback.handed_back.disposition,
        handback.handed_back.schedule_id,
        not saved,
    )
    status = await agent.task_manager.operations_runtime.run(
        repository.status, handback.handback_id, agent.team_id
    )
    if status == HANDBACK_STATUS_RECEIVED:
        await _process_safely(agent, repository, handback)
    return saved


async def process_pending_handbacks(agent: Any) -> None:
    """Retry handbacks not yet settled; called on each lead scheduler pass."""

    if not getattr(agent, "is_team_lead", False):
        return
    # Contained here: the scheduler pass also fires schedules, and a handback
    # store problem must not stop that. Each pending handback is retried later.
    try:
        repository = _handback_repository(agent)
        retry_before = datetime.now(timezone.utc) - timedelta(
            seconds=HANDBACK_RETRY_INTERVAL_SECONDS
        )
        pending = await agent.task_manager.operations_runtime.run(
            partial(repository.list_pending, agent.team_id), updated_before=retry_before
        )
    except Exception:
        agent._logger.exception(
            "Pending task handbacks could not be read; the lead will retry on its "
            "next scheduler pass. event=worker_handback_sweep_failed agent_id=%s",
            agent.id,
        )
        return
    for handback in pending:
        await _process_safely(agent, repository, handback)


async def _process_safely(
    agent: Any, repository: WorkerHandbackRepository, handback: WorkerTaskHandbackV1
) -> None:
    try:
        await _process_handback(agent, repository, handback)
    except Exception:
        await agent.task_manager.operations_runtime.run(
            repository.touch, handback.handback_id, agent.team_id
        )
        agent._logger.exception(
            "Returned task could not be sent out again; the lead will retry. "
            "event=worker_handback_redispatch_failed agent_id=%s handback_id=%s "
            "task_id=%s",
            agent.id,
            handback.handback_id,
            handback.handed_back.task_id,
        )


async def _process_handback(
    agent: Any, repository: WorkerHandbackRepository, handback: WorkerTaskHandbackV1
) -> None:
    task_id = handback.handed_back.task_id
    skip_status = await agent.task_manager.operations_runtime.run(
        partial(_settled_status, agent.task_manager), task_id
    )
    if (
        skip_status is None
        and handback.handed_back.disposition == "interrupted"
        # A delegated task's moves between workers are counted by its requester
        # from claim reports (delegated_work_runtime), which also sees moves
        # after a worker crash; counting here too would stop it twice.
        and not handback.handed_back.task.parent_task_id
    ):
        # Counted before the schedule rules: a run that is not rerun still
        # belongs to the streak of interruptions.
        streak = await agent.task_manager.operations_runtime.run(
            partial(interruption_streak, agent, repository),
            handback,
            interruption_limit(agent),
        )
        if streak.should_escalate:
            await escalate_interruptions(agent, handback, streak)
            skip_status = HANDBACK_STATUS_ESCALATED
    if skip_status is None:
        skip_status = await _scheduled_run_status(agent, task_id)
    if skip_status is not None:
        await _settle(agent, repository, handback, skip_status)
        return
    interruptions = await agent.task_manager.operations_runtime.run(
        repository.interruption_count, agent.team_id, task_id
    )
    # Requeue before publishing, as delegated tasks are recorded before they
    # are published: the worker that takes the re-sent task may claim it
    # before the publish call returns, and that claim must stay recorded.
    requeued = await agent.task_manager.operations_runtime.run(
        partial(_requeue_lead_subtask, agent, handback.worker_agent_id), task_id
    )
    sequence = await _publish_redispatch(agent, handback, interruptions)
    if requeued:
        from custom_agents.delegated_work_runtime import record_publish_sequence

        await agent.task_manager.operations_runtime.run(
            partial(record_publish_sequence, agent, subtask_id=task_id, publish_sequence=sequence)
        )
    await _settle(agent, repository, handback, HANDBACK_STATUS_REDISPATCHED)


def _settled_status(task_manager: Any, task_id: str) -> str | None:
    """Why a returned task must not run again, or None if it should."""

    if task_manager.is_execution_cancelled(task_id):
        return HANDBACK_STATUS_CLOSED_CANCELLED
    info = task_manager.get_task_info(task_id)
    if info is not None and task_manager.is_terminal_state(info.state):
        return HANDBACK_STATUS_CLOSED_FINISHED
    return None


async def _scheduled_run_status(agent: Any, task_id: str) -> str | None:
    """For a scheduled run, why it must not run again; None to rerun it.

    Guards the race between a rerun and the schedule firing again: a run is not
    rerun once the schedule has fired again, while the schedule is paused or
    disabled, or when its next run is due within the skip window. A run that
    will not rerun is closed, so it cannot hold up later runs. The overlap guard
    at firing time (scheduled_runtime) covers a rerun that is still in flight.
    """

    store = getattr(agent, "_schedule_store", None)
    records = getattr(store, "worker_records", None)
    if records is None:
        return None
    occurrence = await agent.task_manager.operations_runtime.run(
        records.for_task, task_id, agent.team_id
    )
    if occurrence is None:
        return None
    if occurrence.finished_at_utc is not None:
        return HANDBACK_STATUS_CLOSED_FINISHED
    blocked = await agent.task_manager.operations_runtime.run(
        partial(_rerun_blocker, agent, store), occurrence
    )
    if blocked is None:
        return None
    status: str = blocked[0]
    reason: str = blocked[1]
    await agent.task_manager.operations_runtime.run(
        partial(records.supersede_run, task_id, agent.team_id), reason=reason
    )
    agent._logger.warning(
        "Interrupted scheduled run will not run again. "
        "event=scheduled_rerun_skipped agent_id=%s task_id=%s schedule_id=%s "
        "due_at=%s reason=%s",
        agent.id,
        task_id,
        occurrence.schedule_id,
        occurrence.due_at,
        reason,
    )
    return status


def _rerun_blocker(
    agent: Any, store: Any, occurrence: Any
) -> tuple[str, str] | None:
    from custom_agents.agent_instantiation_service import effective_worker_rules

    records = store.worker_records
    if records.ran_again_since(occurrence.schedule_id, agent.team_id, occurrence.due_at):
        return HANDBACK_STATUS_SUPERSEDED, "schedule_already_ran_again"
    row = store.get_row(occurrence.schedule_id)
    if row is None:
        # A one-time or exhausted schedule has no next run to wait for.
        return None
    now = datetime.now(timezone.utc)
    paused_until = _as_time(row.get("paused_until"))
    if not row.get("enabled"):
        return HANDBACK_STATUS_CLOSED_SCHEDULE_INACTIVE, "schedule_disabled"
    if paused_until is not None and paused_until > now:
        return HANDBACK_STATUS_CLOSED_SCHEDULE_INACTIVE, "schedule_paused"
    next_run = _as_time(row.get("next_fire_at"))
    binding = records.get_binding(occurrence.schedule_id, team_id=agent.team_id)
    rules = effective_worker_rules(
        agent,
        occurrence.named_agent_type,
        dict(binding.policy.rules) if binding is not None else {},
    )
    if (
        next_run is not None
        and (next_run - now).total_seconds() <= rules.interrupted_run_skip_window_seconds
    ):
        return HANDBACK_STATUS_SUPERSEDED, "next_run_due_soon"
    return None


def _as_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        parsed = datetime.fromisoformat(str(value))
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


async def _publish_redispatch(
    agent: Any, handback: WorkerTaskHandbackV1, interruptions: int
) -> int | None:
    handed_back = handback.handed_back
    agent_type = handback.worker_named_agent_type
    route = (getattr(agent, "named_agent_type_queue_map", {}) or {}).get(
        agent_type
    ) or f"{agent.team_id}_{agent_type}"
    notice = TaskRedispatchNoticeV1(
        handback_id=handback.handback_id,
        prior_disposition=handed_back.disposition,
        prior_worker_agent_id=handback.worker_agent_id,
        interruption_count=interruptions,
        interrupted_at_utc=handback.evidence.lease_lost_at_utc,
        dispatched_actions=handed_back.dispatched_actions,
    )
    envelope = MessageEnvelope(
        msg_content=handed_back.task,
        # Keep the original sender and reply route so the result reaches the
        # original requester; a direct-id route becomes the shared type queue.
        msg_header=handed_back.header.model_copy(
            update={
                "message_id": new_id("msg_id"),
                "to_routing_path": route,
                "idempotency_key": f"redispatch:{handback.handback_id}",
                "outbox_action_id": None,
            }
        ),
        msg_meta=handed_back.meta.model_copy(
            update={
                "to_named_agent_type": agent_type,
                "to_agent_name": None,
                "redispatch_notice": notice,
            }
        ),
    )
    sequence: int | None = await agent.team_comms.send_message(
        message=envelope, routing_key=route
    )
    agent._logger.warning(
        "Returned task sent out again for another worker. "
        "event=worker_task_redispatched agent_id=%s handback_id=%s task_id=%s "
        "agent_type=%s prior_disposition=%s interruption_count=%s",
        agent.id,
        handback.handback_id,
        handed_back.task_id,
        agent_type,
        handed_back.disposition,
        interruptions,
    )
    return sequence


def _requeue_lead_subtask(agent: Any, from_worker_agent_id: str, task_id: str) -> bool:
    """Put a lead-delegated subtask back to "waiting in the queue"; True if it did.

    Its delegated record returns to queued (the new message's sequence follows
    after publishing), so it is not probed until a worker claims it and a stop
    withdraws the new message, and a fresh ACCEPTED timestamp restarts its
    timing. Nothing changes once another worker claimed the re-sent task: a
    retried handback (its first attempt published but could not settle) must
    not turn that claim back into "queued", which lost its stops (2026-10-17).
    """

    task_manager = agent.task_manager
    if task_manager.get_parent_task_id(task_id) is None:
        return False
    info = task_manager.get_task_info(task_id)
    if info is None or task_manager.is_terminal_state(info.state):
        return False
    from custom_agents.delegated_work_runtime import delegated_tasks

    requeued = delegated_tasks(agent).mark_requeued(
        team_id=str(agent.team_id),
        task_id=task_id,
        publish_sequence=None,
        from_holder_agent_id=from_worker_agent_id,
    )
    if requeued is None:
        agent._logger.info(
            "A returned task already has a new worker; its record stays as it is. "
            "event=worker_handback_requeue_skipped agent_id=%s task_id=%s from_worker_agent_id=%s",
            agent.id,
            task_id,
            from_worker_agent_id,
        )
        return False
    now = datetime.now(timezone.utc)
    task_manager.update_main_task_state(task_id, ProgressState.ACCEPTED, now.isoformat())
    return True


async def _settle(
    agent: Any,
    repository: WorkerHandbackRepository,
    handback: WorkerTaskHandbackV1,
    status: str,
) -> None:
    await agent.task_manager.operations_runtime.run(
        repository.set_status, handback.handback_id, agent.team_id, status
    )
    if status != HANDBACK_STATUS_REDISPATCHED:
        agent._logger.warning(
            "Returned task was not sent out again. "
            "event=worker_handback_settled agent_id=%s handback_id=%s task_id=%s "
            "status=%s",
            agent.id,
            handback.handback_id,
            handback.handed_back.task_id,
            status,
        )


async def apply_redispatch_notice(agent: Any, task_id: str, meta: MessageMeta) -> bool:
    """On the receiving worker: take over a re-sent task before it runs.

    Returns False when the task was dropped because the schedule already ran
    again while this rerun was in flight; the caller must not start it.
    """

    notice = getattr(meta, "redispatch_notice", None)
    if notice is None:
        return True
    if not await _take_over_scheduled_run(agent, task_id, notice):
        return False
    directive = render_redispatch_directive(notice)
    if directive:
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_runtime_resume_directive, task_id, directive
        )
    agent._logger.info(
        "Accepted a task the TeamLead sent out again. "
        "event=redispatched_task_admitted agent_id=%s task_id=%s handback_id=%s "
        "prior_disposition=%s interruption_count=%s",
        agent.id,
        task_id,
        notice.handback_id,
        notice.prior_disposition,
        notice.interruption_count,
    )
    return True


async def _take_over_scheduled_run(
    agent: Any, task_id: str, notice: TaskRedispatchNoticeV1
) -> bool:
    """Move a rerun's occurrence receipt here, or drop an overtaken rerun.

    The receipt carries the schedule execution scope and closes when the run
    finishes, so the worker running it must own it. If the schedule ran again
    on its own while this rerun was in flight, the rerun would overlap that run
    and is closed instead.
    """

    records = getattr(getattr(agent, "_schedule_store", None), "worker_records", None)
    if records is None:
        return True
    occurrence = await agent.task_manager.operations_runtime.run(
        records.for_task, task_id, agent.team_id
    )
    if occurrence is None:
        return True
    overtaken = await agent.task_manager.operations_runtime.run(
        records.ran_again_since, occurrence.schedule_id, agent.team_id, occurrence.due_at
    )
    if overtaken:
        await agent.task_manager.operations_runtime.run(
            partial(records.supersede_run, task_id, agent.team_id),
            reason="schedule_ran_again_before_rerun",
        )
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.remove_task, task_id
        )
        agent._logger.warning(
            "Dropped a rerun because its schedule already ran again. "
            "event=scheduled_rerun_overtaken agent_id=%s task_id=%s schedule_id=%s",
            agent.id,
            task_id,
            occurrence.schedule_id,
        )
        return False
    moved = await agent.task_manager.operations_runtime.run(
        partial(records.reassign_run, task_id, agent.team_id),
        from_agent_id=notice.prior_worker_agent_id,
        to_agent_id=agent.id,
        launch_id=getattr(getattr(agent, "_worker_launch", None), "launch_id", None),
    )
    log = agent._logger.info if moved else agent._logger.warning
    log(
        "Rerun of a scheduled run taken over. event=scheduled_run_custody_moved "
        "agent_id=%s task_id=%s schedule_id=%s from_agent_id=%s receipts_moved=%s",
        agent.id,
        task_id,
        occurrence.schedule_id,
        notice.prior_worker_agent_id,
        moved,
    )
    return True


def _rejection_reason(
    agent: Any, handback: WorkerTaskHandbackV1, msg_header: MessageHeader
) -> str | None:
    if handback.team_id != agent.team_id:
        return "team_mismatch"
    if msg_header.from_agent_id != handback.worker_agent_id:
        return "sender_is_not_the_worker"
    if not handback.evidence.launch_id:
        return "missing_launch_id"
    launch = AgentLaunchRepository(agent.task_manager.storage_settings).get(
        handback.evidence.launch_id, agent.team_id
    )
    if launch is None or launch.agent_id != handback.worker_agent_id:
        return "launch_not_owned_by_worker"
    return None


def _handback_repository(agent: Any) -> WorkerHandbackRepository:
    return WorkerHandbackRepository(agent.task_manager.storage_settings)

