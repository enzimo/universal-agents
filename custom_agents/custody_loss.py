"""Release saved work that another agent took over, and keep this agent running.

Why: takeover moves an agent's saved work (``active_task_runtime`` and
``agent_deferred_messages``) to a new owner only when the old one counts as
gone (``custom_agents.agent_liveness``). If that decision is ever wrong, the old
owner is still running the same tasks. Until 2026-10-17 its next save simply
wrote its row again, so two agents held and ran the same task. Saves now expect
the owner's own row (``storage.runtime_custody.RuntimeCustodyLost``),
and this module handles the refusal. An admission takeover of a redelivered
copy (``custom_agents.task_custody``) moves only that one task's custody row;
a later save that still lists the task is refused for it
(``storage.task_custody_repository.TaskCustodyTakenOver``), and the
coordinator drops only those tasks and calls the same listener. Before each
such task is dropped, the task-release callbacks stop the work it delegated
(cancellation records under each child's own root, ``orphaned_subtasks``)
and withdraw the access requests it waited on (``access_runtime``): the new
holder starts the task again, delegates and asks for itself, and the task
itself gets no cancellation record.

How a refusal is handled:

1. The task snapshot coordinator (``task_manager_runtime_snapshots``) or the
   deferred inbox (``deferred_message_runtime``) gets a save refused. In the same
   step, under the task lane or the inbox lock, it drops the taken-over tasks or
   messages from memory, so nothing in this agent can start or save them again,
   and calls the listener installed here. A deferred request leaves its row
   atomically with its own task's admission. Other messages remain until their
   handling completes, with exact-message custody checked on each task save.
2. The listener runs on the storage thread and moves to the agent's event loop.
3. If a turn of released work is running, it is stopped. The stop is marked as
   a transfer (``control_runtime.consume_handback_interrupt``), so the task gets
   no cancellation record, and ``turn_failure_runtime`` sends no failure reply
   for a released task: its new owner runs it and answers.
4. The agent keeps running and takes new work; its next save makes a new row.

Not done on purpose: no handback to the team lead (the work already has its new
owner), no stop requests to subtasks when the whole saved row was taken over
(the replacement adopted them with it; a per-task release stops them, see
above), and no exit (a startup agent's exit stops the whole runtime).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from custom_agents.provider_error_runtime import iter_exception_chain
from custom_agents.sdk.strands.turn_tool_stop import CUSTODY_LOST_REASON
from storage.runtime_custody import CustodyStore, RuntimeCustodyLost


# Stops a running turn: control_runtime._interrupt_active_execution, passed in by
# the agent because control_runtime imports turn_failure_runtime, which imports this.
InterruptTurn = Callable[..., Awaitable[Optional[asyncio.Task[Any]]]]


@dataclass
class CustodyRelease:
    """Work this agent released; kept for its life so late failures of it send no reply."""

    interrupt_turn: InterruptTurn
    released_task_ids: set[str] = field(default_factory=set)
    # Turns this module stopped; handle_interrupted_turn treats them as transfers.
    interrupting_task_ids: set[str] = field(default_factory=set)
    interrupt: Optional[asyncio.Task[Any]] = None


def install_custody_listeners(agent: Any, *, interrupt_turn: InterruptTurn) -> None:
    """Hear about refused saves from the task snapshot and the deferred inbox (call on the agent's loop)."""
    loop = asyncio.get_running_loop()
    agent._custody_release = CustodyRelease(interrupt_turn=interrupt_turn)

    def tasks_lost(task_ids: tuple[str, ...]) -> None:
        loop.call_soon_threadsafe(_release_tasks, agent, task_ids)

    def messages_lost(count: int) -> None:
        loop.call_soon_threadsafe(_report_released_messages, agent, count)

    agent.task_manager.set_custody_lost_listener(tasks_lost)
    agent.deferred_inbox.on_custody_lost = messages_lost


def _release_tasks(agent: Any, task_ids: tuple[str, ...]) -> None:
    state: CustodyRelease = agent._custody_release
    state.released_task_ids.update(task_ids)
    running: Optional[str] = getattr(agent, "_active_execution_task_id", None)
    stopping = running is not None and running in task_ids and (state.interrupt is None or state.interrupt.done())
    agent._logger.warning(
        "Released saved work that another agent took over; this agent keeps running. "
        "event=runtime_custody_released agent_id=%s store=%s task_ids=%s stopping_turn_task_id=%s",
        agent.id,
        CustodyStore.TASK_RUNTIME.value,
        list(task_ids),
        running if stopping else None,
    )
    if stopping and running is not None:
        state.interrupting_task_ids.add(running)
        state.interrupt = asyncio.create_task(
            _stop_released_turn(agent, state, running, set(task_ids)),
            name=f"{agent.id}:custody_release",
        )


async def _stop_released_turn(agent: Any, state: CustodyRelease, task_id: str, released: set[str]) -> None:
    stopped = await state.interrupt_turn(
        agent, root_task_id=task_id, graph_task_ids=released, reason=CUSTODY_LOST_REASON,
    )
    if stopped is None:
        # The turn ended before the stop arrived; nothing will consume the marker.
        state.interrupting_task_ids.discard(task_id)
    agent._logger.info(
        "Stopped the running turn of released work. event=custody_released_turn_stopped "
        "agent_id=%s task_id=%s stopped=%s",
        agent.id,
        task_id,
        stopped is not None,
    )


def _report_released_messages(agent: Any, count: int) -> None:
    agent._logger.warning(
        "Released deferred messages that another agent took over; this agent keeps running. "
        "event=runtime_custody_released agent_id=%s store=%s deferred_count=%s",
        agent.id,
        CustodyStore.DEFERRED_MESSAGES.value,
        count,
    )


def custody_moved(agent: Any, task_id: str, error: BaseException) -> bool:
    """Whether a failed turn's task now belongs to another agent, so this agent must not fail or answer it.

    A task this agent holds again (a redelivered request, a handed-back task
    it picked up) is its own: it leaves the released set and fails as usual.
    Only a refused save of the task snapshot counts; a refused deferred-message
    save says nothing about this task.
    """
    state = getattr(agent, "_custody_release", None)
    manager = getattr(agent, "task_manager", None)
    if manager is not None and manager.get_task_info(task_id) is not None:
        if state is not None:
            state.released_task_ids.discard(task_id)
        return False
    if state is not None and task_id in state.released_task_ids:
        return True
    return any(
        isinstance(cause, RuntimeCustodyLost) and cause.store is CustodyStore.TASK_RUNTIME
        for cause in iter_exception_chain(error)
    )
