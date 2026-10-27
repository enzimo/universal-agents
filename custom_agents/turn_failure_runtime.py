"""End a task whose model turn failed, and never leave the execution slot held.

The task manager has one main execution slot (``current_in_progress_task_id``);
a request sent in a parallel thread runs in a lane of its own instead
(``custom_agents.execution_lanes``). A task claims its slot by moving to
IN_PROGRESS just before its model turn runs,
and gives it back when the turn's result moves it on (completed, waiting,
failed). Every path that runs a turn must therefore also handle a failed
turn; otherwise the task stays IN_PROGRESS with no turn running, and the
queue never starts another task, silently.

That happened on 2026-09-28: a turn resumed after a ``set_up_tools`` sign-in
(``task_steering_runtime.resume_interrupted_task_turn``) raised, the task kept
the slot, and every later message was "added and queued" and never started
until the person stopped the task by hand.

- ``fail_task_after_turn_error`` is the one handling for a failed turn: log
  it, fail the task, close its conversation turn, and tell the requester. The
  queue loop, the resumed-turn path and the resume-with-directive paths all
  use it.
- ``release_orphaned_execution_slot`` is the safety net for any path that
  still misses it. It runs where the next task is chosen, with the execution
  gate freshly held, so no turn can be running in this process: an
  IN_PROGRESS task there has lost its turn. It is logged
  (``event=execution_slot_orphaned``) and failed with a plain explanation,
  like a task restart recovery cannot retry
  (``runtime_recovery.fail_recovered_executing_task``). Other executing states
  are left alone: RETRYING keeps the slot on purpose while the turn-action
  outbox waits to retry an action.
- ``release_orphaned_parallel_tasks`` does the same for parallel requests: a
  task IN_PROGRESS in a parallel lane that nobody holds has lost its turn.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from typing import Any

from strands.types.exceptions import MaxTokensReachedException

from custom_agents.strands_parts import turn_strands_agent
from custom_agents.custody_loss import custody_moved
from storage.runtime_custody import CustodyStore, RuntimeCustodyLost
from custom_agents.conversation_runtime import finalize_conversation_turn
from custom_agents.model_failover_runtime import ProviderFailoverRequeued
from custom_agents.model_keys import ModelKeyError, tell_administrators_about_key_problem
from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.objective_work_completion import ObjectiveWorkCompletionError
from custom_agents.provider_error_runtime import ProviderTimeoutError, iter_exception_chain
from custom_agents.provider_limits import ProviderLimitExhausted
from custom_agents.runtime_recovery import fail_recovered_executing_task
from custom_agents.task_wait_limit import TaskWaitLimitReached, tell_person_about_wait_limit
from custom_agents.turn_action_outbox_runtime import TurnActionRecoveryScheduled
from custom_messages.enums import ProgressState
from custom_agents.execution_lanes import executing_parallel_task_ids, lane_for_task
from runtime_hosting.agent_execution import MAIN_LANE

MODEL_OUTPUT_LIMIT_MESSAGE = (
    "The model reached its output limit before completing this request. "
    "Please retry with a smaller request or split it into steps."
)
class UnroutableTaskInterruptError(RuntimeError):
    """Input cannot wake this task because there is no external requester route."""


# A turn that raises one of these handed its task on (outbox recovery paused
# it, or model failover queued it again); the task is not failed.
TURN_HANDED_OFF = (TurnActionRecoveryScheduled, ProviderFailoverRequeued)
ORPHANED_SLOT_MESSAGE = (
    "This task stopped unexpectedly before it finished, so it was ended to let "
    "your other messages run. Send it again if you still need it."
)


def _model_key_error(error: BaseException) -> ModelKeyError | None:
    """The model key problem behind a failed turn, also when the SDK wrapped it."""

    return next((item for item in iter_exception_chain(error) if isinstance(item, ModelKeyError)), None)


def task_runtime_error_message(error: Exception) -> str:
    """The error text the requester sees for a failed turn."""

    key_error = _model_key_error(error)
    if key_error is not None:
        return str(key_error)
    if isinstance(error, (ProviderTimeoutError, ObjectiveWorkCompletionError, ProviderLimitExhausted, TaskWaitLimitReached)):
        return str(error)
    if isinstance(error, MaxTokensReachedException):
        return f"Runtime error: {MODEL_OUTPUT_LIMIT_MESSAGE}"
    return f"Runtime error: {error}"


def _log_turn_failure(agent: Any, task_id: str, error: Exception) -> None:
    logger = agent._logger
    key_error = _model_key_error(error)
    if key_error is not None:
        logger.warning(
            "[%s] event=model_key_problem task_id=%s error_type=%s outcome=failed",
            agent.id, task_id, type(key_error).__name__,
        )
        tell_administrators_about_key_problem(agent, key_error)
    elif isinstance(error, ObjectiveWorkCompletionError):
        logger.warning(
            "event=objective_work_completion_rejected task_id=%s session_id=%s "
            "objective_id=%s schedule_id=%s reason=%s outcome=failed message=%s",
            task_id, error.session_id, error.objective_id, error.schedule_id, error.reason, error,
        )
    elif isinstance(error, TaskWaitLimitReached):
        logger.warning(
            "[%s] event=task_wait_limit_failed task_id=%s waits=%s limit=%s outcome=failed",
            agent.id, task_id, error.waits, error.limit,
        )
    elif isinstance(error, UnroutableTaskInterruptError):
        logger.warning("event=task_input_unroutable task_id=%s outcome=failed message=%s", task_id, error)
    elif isinstance(error, ProviderTimeoutError):
        logger.warning(
            "[%s] event=model_provider_timeout task_id=%s outcome=failed message=%s",
            agent.id, task_id, error,
        )
    elif isinstance(error, ProviderLimitExhausted):
        logger.warning(
            "[%s] event=model_provider_limited task_id=%s outcome=failed attempts=%s",
            agent.id, task_id, error.failure.attempts,
        )
    elif isinstance(error, MaxTokensReachedException):
        logger.exception(
            "[%s] event=model_output_token_limit_reached task_id=%s user_error_sanitized=true",
            agent.id, task_id,
        )
    else:
        logger.exception(f"[{agent.id}] Error processing task {task_id}: {error}")


def _log_custody_moved(agent: Any, task_id: str, error: BaseException) -> None:
    agent._logger.warning(
        "A turn ended after its task moved to another agent; nothing is recorded or sent. "
        "event=turn_ended_after_custody_moved agent_id=%s task_id=%s error_type=%s",
        agent.id,
        task_id,
        type(error).__name__,
    )


async def fail_task_after_turn_error(agent: Any, task_id: str, task_info: Any, error: Exception) -> None:
    """Fail the task whose turn raised, and tell the requester (module docstring).

    A task another agent took over (``custom_agents.custody_loss``) is not
    failed or answered here: its new owner runs it and answers.
    """

    if custody_moved(agent, task_id, error):
        _log_custody_moved(agent, task_id, error)
        return
    _log_turn_failure(agent, task_id, error)
    now_ts = datetime.now(timezone.utc).isoformat()
    runtime_error = task_runtime_error_message(error)
    try:
        await agent.task_manager.operations_runtime.run(agent.task_manager.fail_task, task_id, runtime_error, now_ts)
    except RuntimeCustodyLost as lost:
        if lost.store is not CustodyStore.TASK_RUNTIME:
            raise
        # Recording the failure was the first save since another agent took the
        # task over: that agent runs it and answers.
        _log_custody_moved(agent, task_id, error)
        return
    await finalize_conversation_turn(
        conversation_store=agent._conversation_store,
        conversation_compactor=agent._conversation_compactor,
        conversation_artifact_offloader=agent._conversation_artifact_offloader,
        strands_messages=list(getattr(turn_strands_agent(agent), "messages", []) or []),
        task_manager=agent.task_manager,
        task_info=task_info,
        task_status=ProgressState.FAILED.value,
        logger=agent._logger,
        agent_id=agent.id,
        team_id=agent.team_id,
        named_agent_type=agent.named_agent_type,
        summary_generator=agent._conversation_summary_generator,
    )
    await agent._notify_context_after_turn(task_info=task_info, task_status=ProgressState.FAILED.value)
    failed_task = task_info.task
    if isinstance(error, ProviderLimitExhausted):
        # The typed reason travels with the failure, for the requester's trusted code.
        failed_task = failed_task.model_copy(update={"failure": error.failure})
    await agent._send_task_error_response(failed_task, task_info.header, task_info.meta, runtime_error)
    if isinstance(error, TaskWaitLimitReached):
        # A scheduled run's failure goes to the scheduler; tell the person too.
        await tell_person_about_wait_limit(agent, task_info, error)


async def settle_failed_turn(agent: Any, task_id: str, task_info: Any, error: Exception) -> None:
    """End a resumed turn that raised: a hand-off is logged, anything else fails the task."""

    if isinstance(error, TURN_HANDED_OFF):
        agent._logger.warning(
            "A resumed turn handed its task on. event=resumed_turn_handed_off task_id=%s reason=%s",
            task_id,
            type(error).__name__,
        )
        return
    await fail_task_after_turn_error(agent, task_id, task_info, error)


def _slot_holder(manager: Any) -> Any:
    task_id = manager.current_task_id
    return manager.get_task_info(task_id) if task_id else None


async def release_orphaned_execution_slot(agent: Any) -> bool:
    """Fail an IN_PROGRESS task that holds the slot with no turn running (module docstring).

    Call only with the execution gate freshly held, never nested inside a turn.
    """

    # Bind the manager: the operations lane deep-copies call arguments.
    holder = await agent.task_manager.operations_runtime.run(partial(_slot_holder, agent.task_manager))
    if holder is None or holder.state != ProgressState.IN_PROGRESS:
        return False
    task_id = holder.task.task_id
    agent._logger.error(
        "A task held the execution slot with no turn running; ending it so queued work can start. "
        "event=execution_slot_orphaned agent_id=%s task_id=%s state=%s",
        agent.id,
        task_id,
        holder.state.value,
    )
    await fail_recovered_executing_task(
        agent,
        holder,
        now_ts=datetime.now(timezone.utc).isoformat(),
        reason=ORPHANED_SLOT_MESSAGE,
        snapshot_age_seconds=None,
    )
    return True


async def release_orphaned_slot_when_idle(agent: Any) -> bool:
    """Run the orphaned-slot check from the scheduler pass when no turn holds the gate.

    Before 2026-10-04 the check ran only when the next task was picked, so an
    orphaned slot blocked the queue until new work arrived. The gate is taken
    only when it is free: in asyncio, acquiring a free lock does not suspend, so
    no turn can start in between, and a busy gate means a turn is running.
    """

    lanes = getattr(agent, "_execution_lanes", None)
    if lanes is None or lanes.main_busy or getattr(agent, "paused", False):
        return False
    async with lanes.hold(MAIN_LANE):
        released = await release_orphaned_execution_slot(agent)
    released = await release_orphaned_parallel_tasks(agent) or released
    if released:
        # Queued work starts now, not when unrelated traffic next wakes the drain.
        schedule_pending_work(agent)
    return released


async def release_orphaned_parallel_tasks(agent: Any) -> bool:
    """Fail parallel requests' tasks left IN_PROGRESS in a lane nobody holds (module docstring).

    A lane's holder claims a task and runs its turn while holding the lane, so
    a free lane means no turn runs for it. The lane is taken for the check;
    acquiring a free lock does not suspend, so no turn can start in between.
    """

    lanes = getattr(agent, "_execution_lanes", None)
    manager = agent.task_manager
    # Only the team lead opens parallel lanes (custom_agents.execution_lanes).
    if lanes is None or getattr(agent, "paused", False) or not getattr(manager, "parallel_lanes_enabled", False):
        return False
    released = False
    for task_id in await manager.operations_runtime.run(partial(executing_parallel_task_ids, manager)):
        lane = await manager.operations_runtime.run(partial(lane_for_task, manager), task_id)
        if lanes.lane_busy(lane):
            continue
        async with lanes.hold(lane):
            holder = await manager.operations_runtime.run(manager.get_task_info, task_id)
            if holder is None or holder.state != ProgressState.IN_PROGRESS:
                continue
            agent._logger.error(
                "A parallel request's task was in progress with no turn running; ending it. "
                "event=execution_slot_orphaned agent_id=%s task_id=%s lane=%s",
                agent.id,
                task_id,
                lane,
            )
            await fail_recovered_executing_task(
                agent,
                holder,
                now_ts=datetime.now(timezone.utc).isoformat(),
                reason=ORPHANED_SLOT_MESSAGE,
                snapshot_age_seconds=None,
            )
            released = True
    return released


__all__ = [
    "MODEL_OUTPUT_LIMIT_MESSAGE",
    "ORPHANED_SLOT_MESSAGE",
    "TURN_HANDED_OFF",
    "TaskWaitLimitReached",
    "UnroutableTaskInterruptError",
    "fail_task_after_turn_error",
    "release_orphaned_execution_slot",
    "release_orphaned_parallel_tasks",
    "release_orphaned_slot_when_idle",
    "settle_failed_turn",
    "task_runtime_error_message",
]
