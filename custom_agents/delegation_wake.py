"""When a requester's model wakes for its delegated tasks, and progress notes meanwhile.

A task that delegated work waits (WAITING) and its model is not called while
the delegated tasks run. It wakes (owner's decision of 2026-10-04: only when
something needs a decision):

1. when every delegated task has ended (``subtask_watchdog_runtime._resume_parent_task_if_ready``);
2. at once when a delegated task failed for a reason trusted code reports
   (``TaskFailureV1``: the model provider kept refusing calls, or the task kept
   losing its workers), even while other delegated tasks still run, so the
   requester can stop the rest, retry differently or answer with what it has;
3. when a check-back time it set on a delegation passed
   (``delegate_task.check_back_after_minutes``), with the counts so far.

After such an early wake the task waits again if delegated tasks are still
pending and the model did not finish it.

While delegated tasks run, trusted code sends the person short counts ("9 of
24 done, 4 running, 11 waiting for a free worker") for requests with at least
``progress_notices.min_tasks`` delegated tasks, at most every
``progress_notices.interval_minutes`` and only when the counts changed. No
model call is needed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Optional

from custom_agents.delegated_work_runtime import delegated_tasks
from custom_agents.execution_lanes_runtime import lane_of
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import DELIVERY_MODE_INTERACTIVE, MessageHeader, MessageMeta
from custom_messages.enums import ProgressState, RequesterType, TaskHistoryImportance, TaskHistoryType
from storage.delegated_task_repository import status_counts

logger = logging.getLogger(__name__)

PROGRESS_NOTICE_REASON = "delegated_work_progress"
_FAILURE_WORDS = {
    "provider_limited": "the model provider kept refusing calls (rate limit, quota or overload)",
    "worker_lost": "its workers kept being lost, so automatic retries stopped",
}


def counts_text(counts: dict[str, int]) -> str:
    """'9 of 24 done, 4 running, 11 waiting for a free worker' (zero parts left out)."""

    total = sum(counts.values())
    parts = [f"{counts.get('finished', 0)} of {total} done"]
    for key, words in (
        ("running", "running"),
        ("queued", "waiting for a free worker"),
        ("failed", "failed"),
        ("cancelled", "stopped"),
    ):
        if counts.get(key):
            parts.append(f"{counts[key]} {words}")
    return ", ".join(parts)


async def wake_parent_for_decision(agent: Any, *, parent_task_id: str, directive: str, reason: str) -> bool:
    """Run the parent's model now although delegated tasks may still be pending.

    Returns False when the parent is gone or no longer waiting.

    The decision is taken while holding the parent's execution lane: the main
    lane, or its request's own lane for a request from a parallel thread
    (``custom_agents.execution_lanes``). The turn it runs asks for that same
    lane, and an owner holding one lane may not ask for another. The callers
    (the scheduler pass and the claim handler) hold no lane, or already hold
    the parent's lane when a worker's result is handled in it, so a delegated
    task's result handled meanwhile may already have resumed, or finished,
    the parent. Before 2026-10-17 the parent was read before waiting for the
    lane, and a second model turn then ran for a parent another path had
    already completed. Moving WAITING to IN_PROGRESS with a compare-and-set
    makes exactly one path resume it.
    """

    manager = agent.task_manager
    run = manager.operations_runtime.run
    async with agent._execution_scope(await lane_of(agent, parent_task_id)):
        resumed = await run(
            partial(
                manager.try_transition_task_state,
                parent_task_id,
                allowed_from={ProgressState.WAITING},
                to_state=ProgressState.IN_PROGRESS,
                timestamp=datetime.now(timezone.utc).isoformat(),
                reason=f"Woken for a decision: {reason}.",
                source="delegation_wake",
            )
        )
        if not resumed:
            logger.info(
                "A delegated-work wake-up found the task no longer waiting; nothing to do. "
                "event=delegation_decision_wake_skipped agent_id=%s task_id=%s reason=%s",
                agent.id,
                parent_task_id,
                reason,
            )
            return False
        parent = await run(manager.get_task_info, parent_task_id)
        if parent is None:
            return False
        await _resume_for_decision(agent, parent, directive=directive, reason=reason)
    return True


async def _resume_for_decision(agent: Any, parent: Any, *, directive: str, reason: str) -> None:
    """Run the parent's turn; it waits again if work is still pending (caller holds its lane)."""

    manager = agent.task_manager
    run = manager.operations_runtime.run
    parent_task_id = parent.task.task_id
    await run(
        manager.append_history_entry,
        parent_task_id,
        f"Woken before all delegated tasks ended: {reason}.",
        entry_type=TaskHistoryType.SUBTASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        agent_name=agent.name,
        agent_id=agent.id,
        details={"event": "delegation_decision_wake", "reason": reason},
    )
    logger.warning(
        "Woke a task for a decision about its delegated work. event=delegation_decision_wake "
        "agent_id=%s task_id=%s reason=%s",
        agent.id,
        parent_task_id,
        reason,
    )
    from custom_agents.task_resume_runtime import resume_task_with_directive

    await resume_task_with_directive(agent, parent, directive)
    # The model may have answered, stopped the rest, or delegated more; if work
    # is still pending and the task did not finish, it waits again.
    current = await run(manager.get_task_info, parent_task_id)
    if (
        current is not None
        and not manager.is_terminal_state(current.state)
        and current.state == ProgressState.IN_PROGRESS
        and await run(manager.has_pending_subtasks, parent_task_id)
    ):
        await run(
            manager.update_main_task_state,
            parent_task_id,
            ProgressState.WAITING,
            datetime.now(timezone.utc).isoformat(),
        )


def _decision_directive(agent: Any, parent_task_id: str, *, headline: str) -> str:
    records = delegated_tasks(agent).list_for_parent(str(agent.team_id), parent_task_id)
    counts = status_counts(records)
    lines = [
        f"Runtime instruction: {headline}",
        f"Delegated tasks of this request so far: {counts_text(counts)}.",
        "Inspect them with inspect_delegated_work. Then decide: keep waiting (end this turn "
        "without a final answer; you are woken again when the rest ends), stop tasks you no "
        "longer need with cancel_delegated_task, delegate a task again differently, or answer "
        "with what you have after stopping the rest. Do not repeat delegated tasks that are "
        "still running or waiting.",
    ]
    return "\n".join(lines)


async def wake_for_failed_delegation(agent: Any, *, parent_task_id: str, subtask_info: Any) -> bool:
    """Wake the parent at once for a delegated task that failed with a typed reason."""

    failure = getattr(subtask_info.task, "failure", None)
    category = getattr(getattr(failure, "category", None), "value", None)
    if category is None:
        return False
    manager = agent.task_manager
    if not await manager.operations_runtime.run(manager.has_pending_subtasks, parent_task_id):
        return False  # The ordinary resume handles a parent with nothing pending.
    headline = (
        f"delegated task {subtask_info.task.task_id} failed because "
        f"{_FAILURE_WORDS.get(category, category)}."
    )
    directive = await manager.operations_runtime.run(
        partial(_decision_directive, agent, parent_task_id, headline=headline)
    )
    return await wake_parent_for_decision(
        agent, parent_task_id=parent_task_id, directive=directive, reason=f"delegated task failed ({category})"
    )


async def wake_due_check_backs(agent: Any, *, now: Optional[datetime] = None) -> int:
    """Wake parents whose check-back time passed; returns how many woke.

    Runs in the scheduler pass, which must not wait for a turn: a parent whose
    execution lane runs a turn now keeps its check-back time, and a later pass
    wakes it. Before the parallel lanes, the whole pass skipped this while any
    turn ran.
    """

    moment = now or datetime.now(timezone.utc)
    repository = delegated_tasks(agent)
    run = agent.task_manager.operations_runtime.run
    woken = 0
    for wait in await run(repository.waits_for_requester, str(agent.team_id), str(agent.id)):
        if wait.check_back_at_utc is None or wait.check_back_at_utc > moment:
            continue
        if agent._execution_lanes.lane_busy(await lane_of(agent, wait.parent_task_id)):
            continue
        await run(
            partial(
                repository.set_check_back,
                team_id=str(agent.team_id),
                parent_task_id=wait.parent_task_id,
                requester_agent_id=str(agent.id),
                at=None,
            )
        )
        directive = await run(
            partial(
                _decision_directive,
                agent,
                wait.parent_task_id,
                headline="the check-back time you set on this request's delegated work has come.",
            )
        )
        if await wake_parent_for_decision(
            agent, parent_task_id=wait.parent_task_id, directive=directive, reason="check-back time"
        ):
            woken += 1
    return woken


# -- progress notes ------------------------------------------------------------------


def _route_for(root: Any) -> tuple[Optional[str], Any]:
    """The person's chat for a top-level, interactive request the person made."""

    if root is None or root.task.parent_task_id is not None:
        return None, None
    if getattr(root.meta, "requester_type", None) != RequesterType.USER_REQUESTER:
        return None, None
    context = getattr(root.meta, "gateway_context", None)
    if str(getattr(context, "delivery_mode", "") or "").strip().lower() != DELIVERY_MODE_INTERACTIVE:
        return None, None
    route = str(getattr(root.header, "response_routing_key", "") or "").strip()
    return (route or None), context


def _pool_limit_text(agent: Any, records: list[Any]) -> str:
    """When tasks wait because their pool is full, say so (from the controller's view)."""

    snapshot = getattr(agent, "_worker_pool_snapshot", None) or {}
    pools = snapshot.get("pools") or {}
    for agent_type in sorted({r.named_agent_type for r in records if r.status == "queued" and r.named_agent_type}):
        pool = pools.get(agent_type) or {}
        decision = pool.get("decision") or {}
        if decision.get("reason") in {"the pool is at its size", "the team is at its worker limit"}:
            size = (pool.get("pool_size") or {}).get("effective")
            return f" The team is at its limit of {size} {agent_type} workers, so the rest wait their turn."
    return ""


async def _progress_note_for(
    agent: Any, root_id: str, open_records: list[Any], *, settings: Any, moment: datetime
) -> Optional[tuple[str, dict[str, int]]]:
    """The note text and counts for one request when a note is due, else None.

    Due when the request has at least ``min_tasks`` delegated tasks, the last
    note is at least ``interval_minutes`` old, and the counts changed.
    """

    repository = delegated_tasks(agent)
    run = agent.task_manager.operations_runtime.run
    parent_ids = sorted({r.parent_task_id for r in open_records if r.root_task_id == root_id})
    records = [
        record
        for parent_id in parent_ids
        for record in await run(repository.list_for_parent, str(agent.team_id), parent_id)
    ]
    if len(records) < settings.min_tasks:
        return None
    counts = status_counts(records)
    wait = await run(repository.get_wait, str(agent.team_id), root_id)
    if wait is not None and wait.last_progress_at_utc is not None:
        too_soon = moment - wait.last_progress_at_utc < timedelta(minutes=settings.interval_minutes)
        if too_soon or wait.last_progress_counts == counts:
            return None
    text = f"Progress on your request: {counts_text(counts)}." + _pool_limit_text(agent, records)
    return text, counts


def _progress_envelope(agent: Any, root: Any, route: str, context: Any, text: str, moment: datetime) -> MessageEnvelope:
    root_id = root.task.task_id
    notification_id = f"delegation_progress:{root_id}:{moment.strftime('%Y%m%dT%H%M%S')}"
    return MessageEnvelope(
        msg_content=UserNotification(
            notification_id=notification_id,
            text=text,
            urgency="normal",
            reason=PROGRESS_NOTICE_REASON,
            source_task_id=root_id,
            source_agent_id=agent.id,
            source_agent_name=agent.name,
        ),
        msg_meta=MessageMeta(
            from_agent_name=agent.name,
            msg_correlation_id=f"delegation_progress_{root_id}",
            conversation_id=getattr(root.meta, "conversation_id", None),
            followup_to_task_id=root_id,
            request_timezone=getattr(root.meta, "request_timezone", None),
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
            gateway_context=context.model_copy(deep=True),
        ),
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=route,
            response_routing_key=agent.id,
            idempotency_key=notification_id,
            agent_signature="",
        ),
    )


async def send_progress_notes(agent: Any, *, now: Optional[datetime] = None) -> int:
    """Send counts for running requests with enough delegated tasks; returns how many sent."""

    from custom_agents.worker_pool_settings import current_worker_pool_settings

    moment = now or datetime.now(timezone.utc)
    try:
        settings = current_worker_pool_settings(agent).progress_notices
    except ValueError:
        return 0
    repository = delegated_tasks(agent)
    manager = agent.task_manager
    run = manager.operations_runtime.run
    open_records = await run(repository.list_open, str(agent.team_id), str(agent.id))
    sent = 0
    for root_id in sorted({record.root_task_id for record in open_records}):
        root = await run(manager.get_task_info, root_id)
        route, context = _route_for(root)
        if route is None:
            continue
        note = await _progress_note_for(agent, root_id, list(open_records), settings=settings, moment=moment)
        if note is None:
            continue
        text, counts = note
        try:
            await agent.team_comms.send_message(
                message=_progress_envelope(agent, root, route, context, text, moment), routing_key=route
            )
        except Exception:
            logger.warning(
                "Could not send a progress note; retrying later. event=delegation_progress_send_failed "
                "agent_id=%s task_id=%s",
                agent.id,
                root_id,
                exc_info=True,
            )
            continue
        await run(
            partial(
                repository.record_progress,
                team_id=str(agent.team_id),
                parent_task_id=root_id,
                requester_agent_id=str(agent.id),
                counts=counts,
                at=moment,
            )
        )
        sent += 1
        logger.info(
            "Sent a progress note on delegated work. event=delegation_progress_sent agent_id=%s "
            "task_id=%s counts=%s",
            agent.id,
            root_id,
            counts,
        )
    return sent


__all__ = [
    "PROGRESS_NOTICE_REASON",
    "counts_text",
    "send_progress_notes",
    "wake_due_check_backs",
    "wake_for_failed_delegation",
    "wake_parent_for_decision",
]
