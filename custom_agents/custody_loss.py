"""Stop running tasks another agent took over, and keep this agent running.

Why (2026-10-17): an agent takes over a task it received again when it judges
the task's holder gone (``custom_agents.task_custody``). Only the task's
custody row moves; the old holder's saved work still lists the task. If the
judgement was wrong, the old holder keeps running the task, so it ran twice.
A save that still lists a task another agent now holds is refused
(``storage.task_custody_repository.TaskCustodyTakenOver``), and this module
handles what follows.

How a refusal is handled:

1. The task snapshot coordinator (``task_manager_runtime_snapshots``) drops
   the taken-over tasks from memory in the same task-lane step, saves the
   rest, and calls the listener installed here.
2. The listener runs on the storage thread and moves to the agent's event loop.
3. If a turn of a released task is running, it is stopped. The stop is marked
   as a transfer (``control_runtime.consume_handback_interrupt``), so the task
   gets no cancellation record, and ``turn_failure_runtime`` sends no failure
   reply for a released task: its new holder runs it and answers.
4. The agent keeps running and takes new work.

Not done on purpose: no handback to the team lead (the task already has its
new holder), and no exit.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


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
    """Hear about tasks the task snapshot released (call on the agent's loop)."""
    loop = asyncio.get_running_loop()
    agent._custody_release = CustodyRelease(interrupt_turn=interrupt_turn)

    def tasks_lost(task_ids: tuple[str, ...]) -> None:
        loop.call_soon_threadsafe(_release_tasks, agent, task_ids)

    agent.task_manager.set_custody_lost_listener(tasks_lost)


def _release_tasks(agent: Any, task_ids: tuple[str, ...]) -> None:
    state: CustodyRelease = agent._custody_release
    state.released_task_ids.update(task_ids)
    running: Optional[str] = getattr(agent, "_active_execution_task_id", None)
    stopping = running is not None and running in task_ids and (state.interrupt is None or state.interrupt.done())
    agent._logger.warning(
        "Released tasks that another agent took over; this agent keeps running. "
        "event=runtime_custody_released agent_id=%s task_ids=%s stopping_turn_task_id=%s",
        agent.id,
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
    stopped = await state.interrupt_turn(agent, root_task_id=task_id, graph_task_ids=released)
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


def custody_moved(agent: Any, task_id: str, error: BaseException) -> bool:
    """Whether a failed turn's task now belongs to another agent, so this agent must not fail or answer it.

    A task this agent holds again (a redelivered request, a handed-back task
    it picked up) is its own: it leaves the released set and fails as usual.
    """
    state = getattr(agent, "_custody_release", None)
    manager = getattr(agent, "task_manager", None)
    if manager is not None and manager.get_task_info(task_id) is not None:
        if state is not None:
            state.released_task_ids.discard(task_id)
        return False
    return state is not None and task_id in state.released_task_ids


__all__ = ["CustodyRelease", "custody_moved", "install_custody_listeners"]
