"""Own an agent's background work: the queue drain, delayed wake-ups and short sends.

Why one owner: work started with a bare ``asyncio.create_task`` is not joined
when the agent stops, so it could write task state or publish after shutdown
had released the transport and stores (found 2026-10-17: a ``resume`` control
message, scheduler enqueues and outbox retries each started their own drain).

- Every wake-up of the queue drain goes through ``schedule_pending_work``. The
  drain runs in one retained task, never inside a delivery callback or the
  scheduler loop, so neither waits for a model turn.
- A wake-up is never lost (found 2026-10-17: a wake-up merged into a pass that
  had already made its checks was dropped, and the drain did nothing while
  another holder had the execution slot, so a quiet worker left a saved
  message waiting for unrelated traffic). ``schedule_pending_work`` records a
  request and the drain runs passes while one is recorded. While another
  holder has the slot the request stays recorded, and that holder's release
  starts the next pass (``wake_after_slot_release``, the slot's release
  callback).
- ``wake_pending_work_after`` wakes the drain at a later time with a timer.
- ``start_owned_task`` runs short work that is safe to stop (its durable state
  is saved before it starts; see each caller).
- ``close_pending_work`` runs first in shutdown cleanup: it sets the exit flag,
  so nothing new starts, then cancels and joins all of the above.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


def schedule_pending_work(agent: Any) -> None:
    """Record a wake-up and make sure the retained drain runs.

    A paused agent records nothing: ``resume`` wakes the drain. Once the agent
    is stopping nothing starts.
    """
    if agent.paused or agent._should_exit.is_set():
        return
    agent._pending_work_requested = True
    pending = agent._pending_work_task
    if pending is None or pending.done():
        agent._pending_work_task = asyncio.create_task(
            _drain_pending_work(agent), name=f"pending-work:{agent.id}"
        )


async def _drain_pending_work(agent: Any) -> None:
    """Run passes while a wake-up is recorded.

    A failed pass is logged and ends the drain; a recorded request then waits
    for the next wake-up instead of repeating the failure in a tight loop.
    """
    while agent._pending_work_requested and not (agent.paused or agent._should_exit.is_set()):
        if agent.is_executing:
            return  # the holder's release starts the next pass
        agent._pending_work_requested = False
        try:
            await agent._continue_pending_work()
        except Exception:
            agent._logger.exception(
                "Pending task processing failed. event=pending_work_failed agent_id=%s",
                agent.id,
            )
            return


def wake_after_slot_release(agent: Any, owner: asyncio.Task[Any]) -> None:
    """The execution slot's release callback: run a pass after another holder.

    A delivery, a parent resume or the scheduler's orphaned-slot check may hold
    the slot while work is saved or freed; the drain waits for this release.
    The drain's own releases start nothing: it continues by itself, and a pass
    started from them could repeat a failing step without end.
    """
    if owner is agent._pending_work_task:
        return
    schedule_pending_work(agent)


def wake_pending_work_after(agent: Any, delay_seconds: float) -> None:
    """Wake the drain after ``delay_seconds``; shutdown cancels the timer."""
    if agent._should_exit.is_set():
        return
    timers: set[asyncio.TimerHandle] = agent._pending_work_timers

    def wake() -> None:
        timers.discard(handle)
        schedule_pending_work(agent)

    handle = asyncio.get_running_loop().call_later(max(0.0, delay_seconds), wake)
    timers.add(handle)


def start_owned_task(
    agent: Any,
    make_coroutine: Callable[[], Coroutine[Any, Any, Any]],
    *,
    name: str,
) -> asyncio.Task[None] | None:
    """Run short work in the background until it ends or the agent stops.

    Only work that may be stopped at shutdown belongs here. Once the agent is
    stopping, nothing new starts: the refusal is logged with the task name and
    the coroutine is never created.
    """
    if agent._should_exit.is_set():
        agent._logger.warning(
            "The agent is stopping, so this background work was not started. "
            "event=owned_task_refused agent_id=%s task_name=%s",
            agent.id,
            name,
        )
        return None
    task = asyncio.create_task(_run_owned(agent, make_coroutine(), name), name=name)
    owned: set[asyncio.Task[None]] = agent._owned_tasks
    owned.add(task)
    task.add_done_callback(owned.discard)
    return task


async def _run_owned(agent: Any, coroutine: Coroutine[Any, Any, Any], name: str) -> None:
    try:
        await coroutine
    except Exception:
        agent._logger.exception(
            "Background work failed. event=owned_task_failed agent_id=%s task_name=%s",
            agent.id,
            name,
        )


async def close_pending_work(agent: Any) -> None:
    """Stop new background work, then cancel and join what is running."""
    agent._should_exit.set()
    timers: set[asyncio.TimerHandle] = agent._pending_work_timers
    for handle in timers:
        handle.cancel()
    timers.clear()
    current = asyncio.current_task()
    running = [
        task
        for task in (agent._pending_work_task, *agent._owned_tasks)
        if task is not None and task is not current
    ]
    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)
    agent._pending_work_task = None
