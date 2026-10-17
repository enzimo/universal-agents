"""The requester's view of the tasks it delegated: published, taken, ended.

Delegated tasks wait in their worker type's shared queue until a free worker
takes one (ADR-019). Nothing probes a task while it waits: the queue holds it.
When a worker admits a task it sends ``DelegatedTaskClaimedV1`` to the
requester, which records the holder (``storage.delegated_task_repository``) and
from then on checks only that worker. Before 2026-10-04 the requester probed the
worker *type*: a task waiting behind another one was "unknown" to its worker and
was marked failed, and an idle worker's "unknown" reply could fail a task
another worker was running.

The same record drives:

- stops: a stop for a delegated task goes to the worker holding it, and a task
  no worker has taken yet is withdrawn from the queue. Stops used to be
  published into the shared queue, where whichever worker took the message
  was usually not the one holding the task;
- automatic retry: a task taken over by another worker (its worker died or lost
  its launch lease) counts one move; after
  ``agent.worker_pools.max_retries_after_worker_loss`` moves the task fails as
  ``worker_lost`` and its requester decides what to do;
- inspection, progress notes and the worker pool controller.

All functions are trusted runtime code; none needs a model turn or the
execution slot.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Optional

from custom_messages.contracts.control_v1 import ControlInstruction, Instruction
from custom_messages.contracts.delegated_work_v1 import (
    DelegatedTaskClaimedV1,
    TaskFailureCategory,
    TaskFailureV1,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import (
    ControlType,
    ProgressState,
    RequesterType,
    TaskHistoryImportance,
    TaskHistoryType,
)
from storage.delegated_task_repository import (
    DELEGATED_CANCELLED,
    DELEGATED_FAILED,
    DELEGATED_FINISHED,
    DELEGATED_QUEUED,
    DelegatedTaskRecordV1,
    DelegatedTaskRepository,
)

logger = logging.getLogger(__name__)

TASK_ALREADY_ENDED_REASON = (
    "The request that delegated this task no longer needs it, so this work was stopped."
)
NEVER_PUBLISHED_REASON = (
    "This delegated task could not be sent to a worker, so it was stopped. "
    "The request continues without it."
)
_OPEN_SUBTASK_STATES = frozenset({
    ProgressState.ACCEPTED,
    ProgressState.IN_PROGRESS,
    ProgressState.WAITING,
    ProgressState.PAUSED,
    ProgressState.PARTIAL,
    ProgressState.RETRYING,
    ProgressState.UNDEFINED,
    ProgressState.NOT_STARTED,
})
_TERMINAL_TO_STATUS = {
    ProgressState.COMPLETED: DELEGATED_FINISHED,
    ProgressState.FAILED: DELEGATED_FAILED,
    ProgressState.CANCELLED: DELEGATED_CANCELLED,
    ProgressState.ABANDONED: DELEGATED_CANCELLED,
}


def delegated_tasks(agent: Any) -> DelegatedTaskRepository:
    """This agent's repository for delegated-task records (created once)."""

    repository = getattr(agent, "_delegated_task_repository", None)
    if repository is None:
        repository = DelegatedTaskRepository(agent.task_manager.storage_settings)
        agent._delegated_task_repository = repository
    return repository


# -- publication (requester) ------------------------------------------------------


def record_publication(
    agent: Any,
    *,
    subtask_id: str,
    parent_task_id: str,
    named_agent_type: Optional[str],
    target_agent_name: Optional[str],
    publish_sequence: Optional[int],
) -> DelegatedTaskRecordV1:
    """Record a delegated task before it is published. Runs on the task lane.

    The publish call returns after the stream stored the task, and a free
    worker may already have taken it and sent its claim by then; the record
    must exist first so that claim is never overwritten (see
    ``DelegatedTaskRepository.record_published``). ``record_publish_sequence``
    adds the stream sequence afterwards.
    """

    manager = agent.task_manager
    root = manager.resolve_root_task_id(parent_task_id) or parent_task_id
    return delegated_tasks(agent).record_published(
        team_id=str(agent.team_id),
        task_id=subtask_id,
        parent_task_id=parent_task_id,
        root_task_id=root,
        requester_agent_id=str(agent.id),
        named_agent_type=named_agent_type,
        target_agent_name=target_agent_name,
        publish_sequence=publish_sequence,
    )


def record_publish_sequence(agent: Any, *, subtask_id: str, publish_sequence: Optional[int]) -> None:
    """Store the stream sequence of a published delegated task (to withdraw it). Task lane."""

    delegated_tasks(agent).set_publish_sequence(
        team_id=str(agent.team_id), task_id=subtask_id, publish_sequence=publish_sequence
    )


def record_check_back(agent: Any, *, parent_task_id: str, minutes: float) -> None:
    """Remember when the requester's model wants to look at this request again.

    Runs on the task lane. The requester's runtime wakes the parent task at
    that time with the counts so far (``delegation_wake``), even when delegated
    tasks are still running.
    """

    at = datetime.now(timezone.utc) + timedelta(minutes=float(minutes))
    delegated_tasks(agent).set_check_back(
        team_id=str(agent.team_id),
        parent_task_id=parent_task_id,
        requester_agent_id=str(agent.id),
        at=at,
    )


def register_delegated_task_endings(agent: Any) -> None:
    """Close a delegated task's record when its local subtask record ends.

    A task-terminal callback (``TaskManager.add_task_terminal_callback``): it
    runs after the state change is saved, on the task lane, for every path that
    ends a subtask (a worker's result, a timeout, a stop, a parent ending).
    """

    callback = getattr(agent, "_delegated_task_end_callback", None)
    if callback is None:

        def callback(task_id: str, state: Any, timestamp: str) -> None:
            manager = agent.task_manager
            if task_id not in manager.task_state.subtask_to_parent_map:
                return
            status = _TERMINAL_TO_STATUS.get(state)
            if status is None:
                return
            info = manager.get_task_info(task_id)
            failure = getattr(getattr(info, "task", None), "failure", None)
            delegated_tasks(agent).mark_closed(
                team_id=str(agent.team_id),
                task_id=task_id,
                status=status,
                failure_category=getattr(getattr(failure, "category", None), "value", None),
            )

        agent._delegated_task_end_callback = callback
    agent.task_manager.add_task_terminal_callback(callback)


def end_unpublished_delegation(agent: Any, *, subtask_id: str, parent_task_id: str, error: str) -> bool:
    """Fail a delegated task whose publication was given up; True if it ended here.

    Runs on the task lane when the delegation's outbox action leaves automatic
    retry (model recovery, isolated recovery or a terminal failure) without
    having published. Its record was written before publishing, so without
    this it stayed "queued" for ever: the watchdog does not check queued
    tasks, and the parent's answer waited on it (2026-10-17). The cancellation
    record makes a worker refuse the task if the publish did reach NATS after
    all, and the delegated record is closed as failed. The parent is not
    resumed here: the outbox recovery that follows runs its next turn.
    """

    manager = agent.task_manager
    now = datetime.now(timezone.utc).isoformat()
    if manager.identity.team_id:
        manager.record_execution_graph_cancelled(
            root_task_id=subtask_id,
            task_ids=[subtask_id],
            canonical_user_id=None,
            command_id=f"delegation_not_published:{subtask_id}",
            reason=NEVER_PUBLISHED_REASON,
            cancelled_at=now,
        )
    if not manager.try_transition_task_state(
        subtask_id,
        allowed_from=set(_OPEN_SUBTASK_STATES),
        to_state=ProgressState.FAILED,
        timestamp=now,
        reason=NEVER_PUBLISHED_REASON,
        source="delegation_not_published",
    ):
        return False
    from custom_messages.records.task_results import TaskResultUpdate

    info = manager.get_task_info(subtask_id)
    errors = list(getattr(getattr(info, "task", None), "errors", None) or [])
    manager.update_task_result(subtask_id, TaskResultUpdate(errors=[*errors, NEVER_PUBLISHED_REASON]))
    manager.bubble_subtask_result(parent_task_id, subtask_id)
    delegated_tasks(agent).mark_closed(
        team_id=str(agent.team_id), task_id=subtask_id, status=DELEGATED_FAILED
    )
    manager.append_history_entry(
        parent_task_id,
        f"Delegated task {subtask_id} could not be sent to a worker and was stopped.",
        entry_type=TaskHistoryType.SUBTASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=now,
        related_task_id=subtask_id,
        details={"event": "delegated_task_never_published", "error": error},
    )
    return True


# -- claims (worker) ---------------------------------------------------------------


def _claim_id(task_id: str, worker_agent_id: str, recovered: bool) -> str:
    return f"claim:{task_id}:{worker_agent_id}:{'recovered' if recovered else 'admitted'}"


async def send_task_claim(
    agent: Any,
    *,
    task_id: str,
    parent_task_id: Optional[str],
    header: Any,
    meta: Any,
    recovered: bool = False,
) -> bool:
    """Tell a delegated task's requester that this worker holds it.

    Only for delegated work whose requester is another agent and expects a
    response; the claim goes to the task's reply route.
    """

    route = getattr(header, "response_routing_key", None)
    if (
        not parent_task_id
        or not route
        or route == agent.id
        or getattr(meta, "requester_type", None) != RequesterType.AGENT_REQUESTER
        or not getattr(meta, "is_response_requested", True)
    ):
        return False
    claim = DelegatedTaskClaimedV1(
        claim_id=_claim_id(task_id, str(agent.id), recovered),
        team_id=str(agent.team_id),
        task_id=task_id,
        parent_task_id=parent_task_id,
        worker_agent_id=str(agent.id),
        worker_agent_name=getattr(agent, "name", None),
        worker_named_agent_type=getattr(agent, "named_agent_type", None),
        claimed_at_utc=datetime.now(timezone.utc),
        recovered=recovered,
    )
    envelope = MessageEnvelope(
        msg_content=claim,
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=route,
            response_routing_key=agent.id,
            idempotency_key=claim.claim_id,
            agent_signature="",
        ),
        msg_meta=MessageMeta(
            from_agent_name=getattr(agent, "name", None),
            msg_correlation_id=getattr(meta, "msg_correlation_id", None),
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
        ),
    )
    try:
        await agent.team_comms.send_message(message=envelope, routing_key=route)
    except Exception:
        # Advisory: the result still reaches the requester. Without the claim
        # the requester cannot check this worker or count a move.
        agent._logger.warning(
            "Could not tell the requester which worker holds its task. "
            "event=delegated_task_claim_send_failed agent_id=%s task_id=%s route=%s",
            agent.id,
            task_id,
            route,
            exc_info=True,
        )
        return False
    agent._logger.info(
        "Told the requester this worker holds its task. event=delegated_task_claim_sent "
        "agent_id=%s task_id=%s parent_task_id=%s recovered=%s",
        agent.id,
        task_id,
        parent_task_id,
        recovered,
    )
    return True


async def report_held_delegated_tasks(agent: Any) -> int:
    """After a restart or takeover, claim every delegated task this worker holds."""

    manager = agent.task_manager
    infos = await manager.operations_runtime.run(manager.get_locally_executable_task_infos)
    sent = 0
    for info in infos:
        if manager.is_terminal_state(info.state) or not info.task.parent_task_id:
            continue
        if await send_task_claim(
            agent,
            task_id=info.task.task_id,
            parent_task_id=info.task.parent_task_id,
            header=info.header,
            meta=info.meta,
            recovered=True,
        ):
            sent += 1
    return sent


# -- claims (requester) ------------------------------------------------------------


async def send_worker_stop(agent: Any, *, worker_agent_id: str, task_id: str, reason: str) -> None:
    """Send the stop control for one task straight to the worker holding it."""

    stop = MessageEnvelope(
        msg_content=ControlInstruction(
            type=ControlType.CONTROL_MSG,
            instruction=Instruction(instruction="stop"),
            target_task_id=task_id,
            reason=reason,
            cascade_to_subtasks=True,
        ),
        msg_meta=MessageMeta(
            from_agent_name=getattr(agent, "name", None),
            followup_to_task_id=task_id,
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
        ),
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=worker_agent_id,
            response_routing_key=agent.id,
            agent_signature="",
        ),
    )
    await agent.team_comms.send_message(message=stop, routing_key=worker_agent_id)


def max_retries_after_worker_loss(agent: Any) -> int:
    from custom_agents.worker_pool_settings import current_worker_pool_settings

    return current_worker_pool_settings(agent).max_retries_after_worker_loss


async def handle_delegated_task_claimed(
    agent: Any,
    *,
    claim: DelegatedTaskClaimedV1,
    msg_header: Any,
    msg_meta: Any,
) -> None:
    """Record which worker holds a task this agent delegated."""

    _ = msg_meta
    if claim.team_id != agent.team_id or msg_header.from_agent_id != claim.worker_agent_id:
        agent._logger.warning(
            "Ignored a task claim whose sender is not the claiming worker. "
            "event=delegated_task_claim_rejected agent_id=%s task_id=%s sender=%s worker=%s",
            agent.id,
            claim.task_id,
            msg_header.from_agent_id,
            claim.worker_agent_id,
        )
        return
    manager = agent.task_manager
    run = manager.operations_runtime.run
    subtask = await run(manager.get_task_info, claim.task_id)
    parent_id = await run(manager.get_parent_task_id, claim.task_id) if subtask else None
    if subtask is None or parent_id != claim.parent_task_id:
        from custom_agents.orphaned_subtasks import stop_report_for_ended_parent

        await stop_report_for_ended_parent(
            agent,
            parent_task_id=claim.parent_task_id,
            child_task_id=claim.task_id,
            msg_header=msg_header,
        )
        return
    if manager.is_terminal_state(subtask.state) or await run(
        manager.is_execution_cancelled, claim.task_id
    ):
        await send_worker_stop(
            agent,
            worker_agent_id=claim.worker_agent_id,
            task_id=claim.task_id,
            reason=TASK_ALREADY_ENDED_REASON,
        )
        agent._logger.warning(
            "A worker took a delegated task that already ended; told it to stop. "
            "event=delegated_task_claim_after_end agent_id=%s task_id=%s worker_agent_id=%s state=%s",
            agent.id,
            claim.task_id,
            claim.worker_agent_id,
            subtask.state.value,
        )
        return
    repository = delegated_tasks(agent)
    outcome = await run(
        partial(
            repository.record_claim,
            team_id=str(agent.team_id),
            task_id=claim.task_id,
            worker_agent_id=claim.worker_agent_id,
            worker_agent_name=claim.worker_agent_name,
            claimed_at=claim.claimed_at_utc,
        )
    )
    if outcome.record is None:
        agent._logger.warning(
            "A delegated task had no publication record; recording it now. "
            "event=delegated_task_record_missing agent_id=%s task_id=%s",
            agent.id,
            claim.task_id,
        )
        await run(
            partial(
                record_publication,
                agent,
                subtask_id=claim.task_id,
                parent_task_id=claim.parent_task_id,
                named_agent_type=claim.worker_named_agent_type,
                target_agent_name=None,
                publish_sequence=None,
            )
        )
        outcome = await run(
            partial(
                repository.record_claim,
                team_id=str(agent.team_id),
                task_id=claim.task_id,
                worker_agent_id=claim.worker_agent_id,
                worker_agent_name=claim.worker_agent_name,
                claimed_at=claim.claimed_at_utc,
            )
        )
    record = outcome.record
    if record is None:
        return
    await _record_claim_history(agent, claim, record, moved=outcome.moved)
    if outcome.moved and record.interruptions > max_retries_after_worker_loss(agent):
        await fail_after_worker_losses(agent, subtask_id=claim.task_id, record=record)


async def _record_claim_history(
    agent: Any, claim: DelegatedTaskClaimedV1, record: DelegatedTaskRecordV1, *, moved: bool
) -> None:
    worker = claim.worker_agent_name or claim.worker_agent_id
    if moved:
        summary = (
            f"Delegated task {claim.task_id} moved to worker {worker} after its previous "
            f"worker was lost ({record.interruptions} time(s))."
        )
        agent._logger.warning(
            "A delegated task moved to another worker. event=delegated_task_moved agent_id=%s "
            "task_id=%s worker_agent_id=%s moves=%s limit=%s",
            agent.id,
            claim.task_id,
            claim.worker_agent_id,
            record.interruptions,
            max_retries_after_worker_loss(agent),
        )
    else:
        summary = f"Worker {worker} took delegated task {claim.task_id}."
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        claim.parent_task_id,
        summary,
        entry_type=TaskHistoryType.SUBTASK_EVENT,
        importance=TaskHistoryImportance.HIGH if moved else TaskHistoryImportance.NORMAL,
        agent_name=claim.worker_agent_name,
        agent_id=claim.worker_agent_id,
        related_task_id=claim.task_id,
        details={
            "event": "delegated_task_moved" if moved else "delegated_task_claimed",
            "worker_agent_id": claim.worker_agent_id,
            "recovered": claim.recovered,
            "moves": record.interruptions,
        },
    )


async def fail_after_worker_losses(
    agent: Any, *, subtask_id: str, record: DelegatedTaskRecordV1
) -> None:
    """Stop a task that kept losing its workers and let its requester decide."""

    from custom_messages.records.task_results import TaskResultUpdate

    manager = agent.task_manager
    run = manager.operations_runtime.run
    subtask = await run(manager.get_task_info, subtask_id)
    if subtask is None or manager.is_terminal_state(subtask.state):
        return
    limit = max_retries_after_worker_loss(agent)
    message = (
        f"Delegated task {subtask_id} was moved to another worker {record.interruptions} "
        f"times because its worker was lost (automatic retries allow {limit}); it was "
        "stopped so the requester can decide what to do."
    )
    if record.holder_agent_id:
        try:
            await send_worker_stop(
                agent,
                worker_agent_id=record.holder_agent_id,
                task_id=subtask_id,
                reason=message,
            )
        except Exception:
            agent._logger.warning(
                "Could not stop the worker holding a task that kept losing workers. "
                "event=delegated_task_stop_failed agent_id=%s task_id=%s worker_agent_id=%s",
                agent.id,
                subtask_id,
                record.holder_agent_id,
                exc_info=True,
            )
    await run(
        manager.update_task_result,
        subtask_id,
        TaskResultUpdate(
            failure=TaskFailureV1(
                category=TaskFailureCategory.WORKER_LOST, attempts=record.interruptions
            )
        ),
    )
    agent._logger.error(
        "A delegated task kept losing its workers and was stopped. "
        "event=delegated_task_worker_loss_limit agent_id=%s task_id=%s parent_task_id=%s "
        "moves=%s limit=%s holders=%s",
        agent.id,
        subtask_id,
        record.parent_task_id,
        record.interruptions,
        limit,
        ",".join(record.holders),
    )
    await agent._fail_subtask_and_reconcile_parent(
        parent_task_id=record.parent_task_id,
        subtask_info=subtask,
        error_message=message,
        resume_parent=True,
        synthetic_context=message,
    )


# -- stops (requester) -------------------------------------------------------------


async def stop_delegated_task(
    agent: Any, *, parent_task_id: str, subtask_info: Any, reason: str
) -> bool:
    """Stop one delegated task wherever it is.

    Held by a worker (it sent a claim): the stop goes to that worker. Not taken
    yet: the message is withdrawn from the stream. A task sent to one named
    worker's own mailbox that worker may already have taken into its inbox, so
    it also gets the stop. Callers record the cancellation barrier first, and a
    worker that admits an ended task later is told to stop when it claims it
    (``handle_delegated_task_claimed``). Nothing is published into a shared
    queue, where a random free worker would take it.
    """

    task_id = subtask_info.task.task_id
    record = await agent.task_manager.operations_runtime.run(
        delegated_tasks(agent).get, str(agent.team_id), task_id
    )
    if record is not None and record.holder_agent_id:
        await send_worker_stop(
            agent, worker_agent_id=record.holder_agent_id, task_id=task_id, reason=reason
        )
        return True
    named_worker = (getattr(record, "target_agent_name", None) if record else None) or getattr(
        subtask_info.meta, "to_agent_name", None
    )
    withdrawn = False
    if record is not None and record.status == DELEGATED_QUEUED and record.publish_sequence:
        withdrawn = bool(await agent.team_comms.delete_work_message(record.publish_sequence))
        agent._logger.info(
            "Withdrew a delegated task no worker had taken yet. "
            "event=delegated_task_withdrawn agent_id=%s task_id=%s parent_task_id=%s "
            "sequence=%s withdrawn=%s",
            agent.id,
            task_id,
            parent_task_id,
            record.publish_sequence,
            withdrawn,
        )
        if withdrawn or not named_worker:
            return True
    mailbox = getattr(subtask_info.header, "to_routing_path", None)
    if named_worker and mailbox:
        await send_worker_stop(agent, worker_agent_id=str(mailbox), task_id=task_id, reason=reason)
        return True
    agent._logger.warning(
        "Could not reach the worker of a delegated task to stop it; its cancellation "
        "record still makes a worker refuse it. event=delegated_task_stop_unrouted "
        "agent_id=%s task_id=%s parent_task_id=%s",
        agent.id,
        task_id,
        parent_task_id,
    )
    return False


__all__ = [
    "NEVER_PUBLISHED_REASON",
    "TASK_ALREADY_ENDED_REASON",
    "delegated_tasks",
    "end_unpublished_delegation",
    "fail_after_worker_losses",
    "handle_delegated_task_claimed",
    "max_retries_after_worker_loss",
    "record_check_back",
    "record_publication",
    "record_publish_sequence",
    "register_delegated_task_endings",
    "report_held_delegated_tasks",
    "send_task_claim",
    "send_worker_stop",
    "stop_delegated_task",
]
