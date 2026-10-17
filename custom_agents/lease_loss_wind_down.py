"""Wind down a spawned worker after it loses its launch lease.

A worker proves it still owns its launch by renewing a fenced lease. Once that
fails with ``LaunchConflict`` another owner may hold the launch, so this worker
must stop taking work. Stopping the scheduler loop alone left the process
consuming its shared queue and running tasks without custody, while the lead
refused to start a replacement for the live worker. The wind-down instead:

1. leaves the shared named-type queue, so no new delegated work arrives;
2. hands every task it has not started, plus the task it is executing, back to
   the TeamLead in one structured message per task (the running turn is
   interrupted without a cancellation record so the task can run elsewhere).
   A running task whose final answer the outbox already sent is finished here
   instead: handing it back made another worker run it again and send a second
   answer (2026-10-17);
3. keeps tasks that already started and now wait on a person or a subtask,
   because their replies are addressed to this worker; they may finish here
   until ``lease_loss_wait_timeout_seconds`` after the lease was lost, after
   which they fail with an explanation to the requester;
4. exits once it holds no unfinished task.

The worker keeps publishing presence while it winds down so a starting worker
of the same type does not adopt tasks this worker is still finishing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Optional, Sequence

from custom_agents.control_runtime import (
    _interrupt_active_execution,
    request_subtask_stop,
)
from custom_agents.task_records import TaskInfo
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.task_v1 import Task
from custom_messages.contracts.redispatch_v1 import DispatchedActionNoteV1
from custom_messages.contracts.worker_handback_v1 import (
    HANDBACK_INTERRUPTED,
    HANDBACK_NOT_STARTED,
    MAX_HANDBACK_ACTION_NOTES,
    MAX_HANDBACK_ERROR_ENTRIES,
    MAX_HANDBACK_TEXT,
    HandedBackTaskV1,
    WorkerLeaseLossEvidenceV1,
    WorkerTaskHandbackV1,
    handback_identity,
)
from custom_messages.enums import MessageMetaType, ProgressState
from custom_transport.nats_adapter import NAMED_WORKER_CONSUMER_ROLE
from custom_agents.structured_llm_messages import TaskResponseAction

WIND_DOWN_POLL_SECONDS = 5.0
HANDBACK_PUBLISH_RETRY_SECONDS = 5.0
# Outbox actions that may already have reached another agent or a user.
_SENT_ACTION_STATUSES = frozenset({"dispatching", "dispatched", "completed"})
# Outbox states of a final answer that went out (or was recorded as delivered
# without a message) once the task is in FINALIZING_PLAN; "dispatching" alone
# does not prove it.
_FINAL_ANSWER_SENT_STATUSES = frozenset({"dispatched", "completed"})
_FINAL_ANSWER_SENT_PHASE = "post_publish_finalize"
_FINAL_ANSWER_ACTION = TaskResponseAction.model_fields["action_type"].default
# Fields a worker adds while running a task; a returned request omits them.
_REQUEST_FIELDS = {"message_class", "task_id", "parent_task_id", "task_type", "problem_statement", "attachments"}


@dataclass
class LeaseLossWindDown:
    """Wind-down state for one worker after its launch lease was lost."""

    evidence: WorkerLeaseLossEvidenceV1
    deadline: datetime
    # Started tasks allowed to continue here: they wait on a reply or resume.
    resumable_task_ids: set[str] = field(default_factory=set)
    # Tasks whose running turn this wind-down cancels; handle_interrupted_turn
    # treats their cancellation as a handback, not a stop.
    interrupting_task_ids: set[str] = field(default_factory=set)
    task: Optional[asyncio.Task[Any]] = None


@dataclass
class _Custody:
    not_started: list[TaskInfo] = field(default_factory=list)
    running: list[TaskInfo] = field(default_factory=list)
    resumable: list[TaskInfo] = field(default_factory=list)


def is_winding_down(agent: Any) -> bool:
    return getattr(agent, "_lease_loss_wind_down", None) is not None


def start_lease_loss_wind_down(
    agent: Any,
    *,
    reason: str,
    recent_scheduler_errors: Sequence[str] = (),
) -> Optional[LeaseLossWindDown]:
    """Begin winding down once; the returned state owns the background task."""

    existing: Optional[LeaseLossWindDown] = getattr(agent, "_lease_loss_wind_down", None)
    if existing is not None:
        return existing
    launch = getattr(agent, "_worker_launch", None)
    now = datetime.now(timezone.utc)
    limits = _worker_limits(agent)
    evidence = WorkerLeaseLossEvidenceV1(
        launch_id=getattr(launch, "launch_id", None),
        lease_lost_at_utc=now,
        lease_expires_at_utc=getattr(launch, "lease_expires_at_utc", None),
        reason=_bounded(reason) or "stale_launch_lease",
        recent_scheduler_errors=tuple(
            _bounded(item) for item in list(recent_scheduler_errors)[-MAX_HANDBACK_ERROR_ENTRIES:]
        ),
    )
    state = LeaseLossWindDown(
        evidence=evidence,
        deadline=now + timedelta(seconds=limits.lease_loss_wait_timeout_seconds),
    )
    agent._lease_loss_wind_down = state
    agent._logger.warning(
        "Worker lost its launch lease and is winding down. "
        "event=worker_wind_down_started agent_id=%s launch_id=%s reason=%s "
        "wait_deadline=%s",
        agent.id,
        evidence.launch_id,
        evidence.reason,
        state.deadline.isoformat(),
    )
    state.task = asyncio.create_task(
        _run_wind_down(agent, state), name=f"{agent.id}:lease_loss_wind_down"
    )
    return state


async def may_start_next_task(agent: Any) -> bool:
    """While winding down, only a started task that is resuming may run here."""

    state: Optional[LeaseLossWindDown] = getattr(agent, "_lease_loss_wind_down", None)
    if state is None:
        return True
    next_task_id = await agent.task_manager.operations_runtime.run(
        partial(_peek_next_task_id, agent.task_manager)
    )
    return next_task_id is None or next_task_id in state.resumable_task_ids


async def hand_back_new_request(
    agent: Any, envelope: MessageEnvelope
) -> Optional[bool]:
    """Return a new task request that reached a winding-down worker to the lead.

    Returns None when the message is not a new request for this worker to
    refuse (normal handling continues), True when it was handed back and the
    delivery may be acknowledged, and False when the handback failed and the
    delivery must be redelivered rather than accepted here.
    """

    state: Optional[LeaseLossWindDown] = getattr(agent, "_lease_loss_wind_down", None)
    content = envelope.msg_content
    if (
        state is None
        or not isinstance(content, Task)
        or content.task_type != MessageMetaType.REQUEST
    ):
        return None
    held = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_task_info, content.task_id
    )
    if held is not None:
        return None
    return await _publish_handback(
        agent,
        state,
        _handed_back(
            content,
            envelope.msg_header,
            envelope.msg_meta or MessageMeta(),
            disposition=HANDBACK_NOT_STARTED,
            prior_state=ProgressState.ACCEPTED.value,
        ),
    )


async def divert_request_during_wind_down(agent: Any, delivery: Any) -> bool:
    """Settle a delivery that a winding-down worker must not accept itself.

    Returns True when the delivery was acknowledged (handed back to the lead)
    or refused for redelivery (the handback failed), so the caller stops.
    """
    if not is_winding_down(agent):
        return False
    handed_back = await hand_back_new_request(agent, delivery.envelope)
    if handed_back is None:
        return False
    if handed_back:
        await delivery.ack()
    else:
        await delivery.nak()
    return True


async def _run_wind_down(agent: Any, state: LeaseLossWindDown) -> None:
    try:
        await _leave_shared_queue(agent)
        # Operation arguments are deep-copied by the task lane; bind the owned
        # manager with partial so the classification reads live state.
        custody = await agent.task_manager.operations_runtime.run(
            partial(_classify_custody, agent.task_manager)
        )
        state.resumable_task_ids.update(info.task.task_id for info in custody.resumable)
        for task_info in custody.running:
            await _finish_or_hand_back_running_task(agent, state, task_info)
        for task_info in custody.not_started:
            await _hand_back_unstarted_task(agent, state, task_info)
        await _hand_back_deferred_requests(agent, state)
        await _wait_for_resumable_tasks(agent, state)
    except asyncio.CancelledError:
        raise
    except Exception:
        agent._logger.exception(
            "Worker wind-down failed; tasks still held here stay in its saved "
            "state for same-role recovery. event=worker_wind_down_failed "
            "agent_id=%s launch_id=%s",
            agent.id,
            state.evidence.launch_id,
        )
    finally:
        agent._logger.info(
            "Worker finished winding down and is exiting. "
            "event=worker_wind_down_exit agent_id=%s launch_id=%s",
            agent.id,
            state.evidence.launch_id,
        )
        await agent.exit_agent()


async def _leave_shared_queue(agent: Any) -> None:
    adapter = agent._ensure_nats_transport_adapter()
    stopped = await adapter.unsubscribe_consumer_role(NAMED_WORKER_CONSUMER_ROLE)
    agent._logger.info(
        "Winding-down worker left its shared work queue. "
        "event=worker_left_shared_queue agent_id=%s subscriptions_stopped=%s",
        agent.id,
        stopped,
    )


def _classify_custody(task_manager: Any) -> _Custody:
    """Sort unfinished local tasks by what the wind-down does with them."""

    custody = _Custody()
    delegated_out = set(task_manager.task_state.subtask_to_parent_map)
    for info in task_manager.list_task_infos():
        if info.task.task_id in delegated_out or task_manager.is_terminal_state(info.state):
            continue
        if task_manager.is_executing_state(info.state):
            custody.running.append(info)
        elif info.state in {ProgressState.WAITING, ProgressState.PAUSED} or _has_started(
            task_manager, info
        ):
            custody.resumable.append(info)
        else:
            custody.not_started.append(info)
    return custody


def _has_started(task_manager: Any, info: TaskInfo) -> bool:
    return any(
        task_manager.is_executing_state(ProgressState(state))
        for state, _timestamp in info.task.task_state or []
    )


def _peek_next_task_id(task_manager: Any) -> Optional[str]:
    queue = task_manager.task_state.task_queue
    return queue[0] if queue else None


def _final_answer_sent(task_manager: Any, task_id: str) -> bool:
    """Whether the outbox shows this task's final answer already went out.

    The outbox marks every task_response action dispatched and then
    completed, also when it held the answer back (a final review, pending
    delegated work or a manager escalation). Only a real final answer moves
    the task to FINALIZING_PLAN before it is sent
    (``_prepare_local_task_finalization_before_dispatch``), and only that path
    reaches the post-publish phase, which may outlast FINALIZING_PLAN when
    finishing up fails and is retried. While its after-action report goes out,
    a sent answer is still at the publish phase, dispatched.
    """

    info = task_manager.get_task_info(task_id)
    finalizing = info is not None and info.state == ProgressState.FINALIZING_PLAN
    return any(
        record.source_task_id == task_id
        and record.action_type == _FINAL_ANSWER_ACTION
        and (
            record.phase == _FINAL_ANSWER_SENT_PHASE
            or (finalizing and record.status in _FINAL_ANSWER_SENT_STATUSES)
        )
        for record in task_manager.execution_state.turn_action_outbox.values()
    )


async def _finish_or_hand_back_running_task(
    agent: Any, state: LeaseLossWindDown, task_info: TaskInfo
) -> None:
    """Finish a running task whose final answer was sent; hand back any other."""

    task_id = task_info.task.task_id
    if await agent.task_manager.operations_runtime.run(
        partial(_final_answer_sent, agent.task_manager), task_id
    ):
        await _finish_answered_task(agent, state, task_id)
    else:
        await _hand_back_running_task(agent, state, task_info)


async def _finish_answered_task(agent: Any, state: LeaseLossWindDown, task_id: str) -> None:
    """Complete a task locally after its final answer went out.

    Its turn was finishing up (summaries, after-action report, completion).
    The turn is stopped as for a handback, without a cancellation record, and
    the task is completed here so nobody runs it again.
    """

    state.interrupting_task_ids.add(task_id)
    await _interrupt_active_execution(agent, root_task_id=task_id, graph_task_ids={task_id})
    state.interrupting_task_ids.discard(task_id)
    finished = await agent._complete_local_task_finalization_after_dispatch(task_id=task_id)
    agent._logger.warning(
        "A running task's final answer was already sent; it was completed here "
        "instead of being handed back. event=worker_wind_down_answered_task_finished "
        "agent_id=%s task_id=%s completed_here=%s",
        agent.id,
        task_id,
        finished,
    )


async def _hand_back_running_task(
    agent: Any, state: LeaseLossWindDown, task_info: TaskInfo
) -> None:
    task_id = task_info.task.task_id
    state.interrupting_task_ids.add(task_id)
    await _interrupt_active_execution(
        agent, root_task_id=task_id, graph_task_ids={task_id}
    )
    current = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_task_info, task_id
    )
    if current is None or agent.task_manager.is_terminal_state(current.state):
        # The turn finished before the interrupt landed; nothing to return.
        state.interrupting_task_ids.discard(task_id)
        agent._logger.info(
            "Running task finished before the wind-down interrupted it. "
            "event=worker_wind_down_task_already_finished agent_id=%s task_id=%s",
            agent.id,
            task_id,
        )
        return
    notes = await agent.task_manager.operations_runtime.run(
        partial(_dispatched_action_notes, agent.task_manager), task_id
    )
    await _stop_pending_subtasks(
        agent,
        task_id,
        reason="The delegating worker lost its launch lease; the parent task is being rerun.",
    )
    handed_back = _handed_back(
        task_info.task,
        task_info.header,
        task_info.meta,
        disposition=HANDBACK_INTERRUPTED,
        prior_state=task_info.state.value,
        dispatched_actions=notes,
    )
    if await _publish_handback(agent, state, handed_back):
        await _release_task(agent, task_id)


async def _hand_back_unstarted_task(
    agent: Any, state: LeaseLossWindDown, task_info: TaskInfo
) -> None:
    handed_back = _handed_back(
        task_info.task,
        task_info.header,
        task_info.meta,
        disposition=HANDBACK_NOT_STARTED,
        prior_state=task_info.state.value,
    )
    if await _publish_handback(agent, state, handed_back):
        await _release_task(agent, task_info.task.task_id)


async def _hand_back_deferred_requests(agent: Any, state: LeaseLossWindDown) -> None:
    """Return new requests still waiting in the incoming-message queue."""

    inbox = getattr(agent, "deferred_inbox", None)
    if inbox is None:
        return
    handed_back_ids: set[str] = set()
    for envelope in list(inbox.messages):
        content = envelope.msg_content
        if not isinstance(content, Task) or content.task_type != MessageMetaType.REQUEST:
            continue
        handed_back = _handed_back(
            content,
            envelope.msg_header,
            envelope.msg_meta or MessageMeta(),
            disposition=HANDBACK_NOT_STARTED,
            prior_state=ProgressState.ACCEPTED.value,
        )
        if await _publish_handback(agent, state, handed_back):
            handed_back_ids.add(envelope.msg_header.message_id)
    if handed_back_ids:
        await inbox.remove_messages(handed_back_ids)


async def _wait_for_resumable_tasks(agent: Any, state: LeaseLossWindDown) -> None:
    """Let started tasks finish here until the deadline, then fail the rest."""

    while not agent._should_exit.is_set():
        remaining = await agent.task_manager.operations_runtime.run(
            partial(_unfinished_resumable, agent.task_manager),
            frozenset(state.resumable_task_ids),
        )
        if not remaining:
            return
        if datetime.now(timezone.utc) >= state.deadline:
            await _end_at_deadline(agent, state, remaining)
            return
        await asyncio.sleep(WIND_DOWN_POLL_SECONDS)


def _unfinished_resumable(task_manager: Any, task_ids: frozenset[str]) -> list[TaskInfo]:
    infos = [task_manager.get_task_info(task_id) for task_id in task_ids]
    return [
        info
        for info in infos
        if info is not None and not task_manager.is_terminal_state(info.state)
    ]


async def _end_at_deadline(
    agent: Any, state: LeaseLossWindDown, remaining: list[TaskInfo]
) -> None:
    for task_info in remaining:
        if agent.task_manager.is_executing_state(task_info.state):
            # A task that resumed and is running at the deadline goes back to
            # the lead like any interrupted run.
            await _finish_or_hand_back_running_task(agent, state, task_info)
        else:
            await _fail_waiting_task(agent, state, task_info)


async def _fail_waiting_task(
    agent: Any, state: LeaseLossWindDown, task_info: TaskInfo
) -> None:
    task_id = task_info.task.task_id
    waited_minutes = round(
        (state.deadline - state.evidence.lease_lost_at_utc).total_seconds() / 60
    )
    message = (
        f"This task was waiting for a reply from a person or a delegated subtask "
        f"when its worker lost its launch lease. No reply arrived within "
        f"{waited_minutes} minutes, so the task ended without a result."
    )
    await _stop_pending_subtasks(agent, task_id, reason=message)
    # Failing the task runs the task-ended callbacks, which withdraw any
    # access request it waited on and end its task-bound grants
    # (access_runtime.register_task_access_cleanup), so the person's Inbox
    # review closes too.
    failed = await agent.task_manager.operations_runtime.run(
        agent.task_manager.fail_task,
        task_id,
        message,
        datetime.now(timezone.utc).isoformat(),
    )
    agent._logger.warning(
        "Waiting task failed after the wind-down deadline. "
        "event=worker_wind_down_wait_expired agent_id=%s task_id=%s prior_state=%s "
        "failed=%s",
        agent.id,
        task_id,
        task_info.state.value,
        failed,
    )
    if failed:
        await agent._send_task_error_response(
            task_info.task, task_info.header, task_info.meta, message
        )


async def _stop_pending_subtasks(agent: Any, task_id: str, *, reason: str) -> None:
    subtasks = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_pending_subtasks, task_id
    )
    for subtask_info in subtasks:
        try:
            await request_subtask_stop(
                agent,
                parent_task_id=task_id,
                subtask_info=subtask_info,
                reason=reason,
            )
        except Exception:
            agent._logger.warning(
                "Could not ask a subtask's worker to stop. "
                "event=worker_wind_down_subtask_stop_failed agent_id=%s "
                "parent_task_id=%s subtask_id=%s",
                agent.id,
                task_id,
                subtask_info.task.task_id,
                exc_info=True,
            )


def _dispatched_action_notes(
    task_manager: Any, task_id: str
) -> tuple[DispatchedActionNoteV1, ...]:
    records = [
        record
        for record in task_manager.execution_state.turn_action_outbox.values()
        if record.source_task_id == task_id and record.status in _SENT_ACTION_STATUSES
    ]
    records.sort(key=lambda record: (record.batch_id, record.action_index))
    return tuple(
        DispatchedActionNoteV1(
            action_id=record.action_id,
            action_type=record.action_type,
            status=record.status,
            summary=_action_summary(record.payload_json),
        )
        for record in records[-MAX_HANDBACK_ACTION_NOTES:]
    )


def _action_summary(payload: dict[str, Any]) -> str:
    for key in ("summary", "problem_statement", "message", "text", "answer"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _bounded(value)
    return ""


def _handed_back(
    task: Task,
    header: MessageHeader,
    meta: MessageMeta,
    *,
    disposition: str,
    prior_state: str,
    dispatched_actions: tuple[DispatchedActionNoteV1, ...] = (),
) -> HandedBackTaskV1:
    gateway_context = getattr(meta, "gateway_context", None)
    return HandedBackTaskV1(
        task_id=task.task_id,
        disposition=disposition,
        prior_state=prior_state,
        task=Task.model_validate(task.model_dump(include=_REQUEST_FIELDS)),
        header=header,
        meta=meta,
        schedule_id=getattr(gateway_context, "scheduled_job_id", None),
        dispatched_actions=dispatched_actions,
    )


async def _publish_handback(
    agent: Any, state: LeaseLossWindDown, handed_back: HandedBackTaskV1
) -> bool:
    """Publish until NATS stores the handback or the wait deadline passes."""

    handback = WorkerTaskHandbackV1(
        handback_id=handback_identity(
            worker_agent_id=agent.id,
            launch_id=state.evidence.launch_id,
            task_id=handed_back.task_id,
        ),
        team_id=agent.team_id,
        worker_agent_id=agent.id,
        worker_named_agent_type=agent.named_agent_type,
        evidence=state.evidence,
        handed_back=handed_back,
    )
    routing_key = _lead_routing_key(agent)
    envelope = MessageEnvelope(
        msg_content=handback,
        msg_meta=MessageMeta(
            from_agent_name=agent.name,
            to_named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
            is_response_requested=False,
        ),
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=routing_key,
            response_routing_key=agent.id,
            idempotency_key=handback.handback_id,
            agent_signature="",
        ),
    )
    while True:
        try:
            await agent.team_comms.send_message(message=envelope, routing_key=routing_key)
        except asyncio.CancelledError:
            raise
        except Exception:
            if datetime.now(timezone.utc) >= state.deadline:
                agent._logger.error(
                    "Task handback could not be delivered to the TeamLead; the "
                    "task stays in this worker's saved state for same-role "
                    "recovery. event=worker_task_handback_undelivered agent_id=%s "
                    "task_id=%s handback_id=%s",
                    agent.id,
                    handed_back.task_id,
                    handback.handback_id,
                    exc_info=True,
                )
                return False
            agent._logger.warning(
                "Task handback publish failed; retrying. "
                "event=worker_task_handback_retry agent_id=%s task_id=%s",
                agent.id,
                handed_back.task_id,
                exc_info=True,
            )
            await asyncio.sleep(HANDBACK_PUBLISH_RETRY_SECONDS)
            continue
        agent._logger.info(
            "Task handed back to the TeamLead. event=worker_task_handed_back "
            "agent_id=%s task_id=%s disposition=%s handback_id=%s",
            agent.id,
            handed_back.task_id,
            handed_back.disposition,
            handback.handback_id,
        )
        return True


async def _release_task(agent: Any, task_id: str) -> None:
    """Drop a handed-back task from local custody without cancelling it."""

    # The marker is consumed by the interrupted turn; clear it if no turn ran.
    state = getattr(agent, "_lease_loss_wind_down", None)
    if state is not None:
        state.interrupting_task_ids.discard(task_id)
    await agent.task_manager.operations_runtime.run(
        partial(agent.task_manager.remove_task, task_id)
    )


def _lead_routing_key(agent: Any) -> str:
    queue_map = getattr(agent, "named_agent_type_queue_map", {}) or {}
    return queue_map.get(TEAM_ARCHITECT_AGENT_TYPE) or (
        f"{agent.team_id}_{TEAM_ARCHITECT_AGENT_TYPE}"
    )


def _worker_limits(agent: Any) -> Any:
    from custom_agents.agent_instantiation_service import current_worker_limits

    return current_worker_limits(agent)


def _bounded(value: Any) -> str:
    text = str(value or "").strip()
    return text if len(text) <= MAX_HANDBACK_TEXT else text[: MAX_HANDBACK_TEXT - 3] + "..."
