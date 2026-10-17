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
  callback). A pass that ends on a failed waiting message stops the drain; the
  request stays recorded for the next real wake-up, so a message whose
  handler records a wake-up before failing cannot repeat in a tight loop.
- ``wake_pending_work_after`` wakes the drain at a later time with a timer.
- ``start_owned_task`` runs short work that is safe to stop (its durable state
  is saved before it starts; see each caller). ``run_as_owned_task`` does the
  same for work submitted from another thread, such as a sync tool's send,
  and lets its failure reach the caller.
- ``close_pending_work`` runs first in shutdown cleanup: it sets the exit flag,
  so nothing new starts, then cancels and joins all of the above.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


class AgentStopping(RuntimeError):
    """Work was refused because the agent is stopping."""


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
    """Run passes while a wake-up is recorded."""
    while agent._pending_work_requested and not (agent.paused or agent._should_exit.is_set()):
        if agent.is_executing:
            return  # the holder's release starts the next pass
        agent._pending_work_requested = False
        if not await _run_pass(agent):
            return


async def _run_pass(agent: Any) -> bool:
    """Run one pass; False ends the drain until the next wake-up.

    A pass that fails, or ends on a waiting message that failed and was kept,
    stops the drain instead of repeating the failure in a tight loop: a
    recorded request waits for the next real wake-up.
    """
    try:
        finished = await agent._continue_pending_work()
    except Exception:
        agent._logger.exception(
            "Pending task processing failed. event=pending_work_failed agent_id=%s",
            agent.id,
        )
        return False
    if not finished:
        agent._logger.warning(
            "A waiting message failed and was kept; the queue drain waits for the next wake-up. "
            "event=pending_work_stopped_after_failure agent_id=%s wake_up_recorded=%s",
            agent.id,
            agent._pending_work_requested,
        )
        return False
    if agent.is_executing:
        # Another holder took the slot before this pass made all its checks;
        # its release starts the next pass.
        agent._pending_work_requested = True
        return False
    return True


def wake_after_slot_release(agent: Any, owner: asyncio.Task[Any]) -> None:
    """The execution slot's release callback: run a recorded wake-up after another holder.

    A delivery, a parent resume or the scheduler's orphaned-slot check may hold
    the slot while work is saved; its wake-up is recorded and the drain waits
    for this release. A release with no recorded wake-up starts nothing, so an
    idle scheduler pass costs no drain pass. The drain's own releases start
    nothing either: it continues by itself.
    """
    if owner is agent._pending_work_task or not agent._pending_work_requested:
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


async def run_as_owned_task(agent: Any, coroutine: Coroutine[Any, Any, Any], *, name: str) -> Any:
    """Run ``coroutine`` in the current task as owned work and return its result.

    For work submitted from outside the agent's own tasks, such as a sync
    tool's send from a worker thread. It is refused once the agent is
    stopping, and shutdown cancels and joins it like any owned task. Unlike
    ``start_owned_task``, its failure reaches the caller.
    """
    if agent._should_exit.is_set():
        coroutine.close()
        agent._logger.warning(
            "The agent is stopping, so this work was not started. "
            "event=owned_task_refused agent_id=%s task_name=%s",
            agent.id,
            name,
        )
        raise AgentStopping(f"The agent is stopping, so {name} was not started.")
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Owned work must run in an asyncio task.")
    owned: set[asyncio.Task[Any]] = agent._owned_tasks
    owned.add(task)
    try:
        return await coroutine
    finally:
        owned.discard(task)


async def close_pending_work(agent: Any) -> None:
    """Stop new background work, then cancel and join what is running.

    A cancelled drain may stop a model turn halfway; its task stays saved and
    restart recovery queues it again.
    """
    agent._should_exit.set()
    timers: set[asyncio.TimerHandle] = agent._pending_work_timers
    timer_count = len(timers)
    for handle in timers:
        handle.cancel()
    timers.clear()
    current = asyncio.current_task()
    running = [
        task
        for task in (agent._pending_work_task, *agent._owned_tasks)
        if task is not None and task is not current and not task.done()
    ]
    agent._logger.info(
        "Stopping the agent's background work. event=pending_work_closed agent_id=%s "
        "cancelled_tasks=%s cancelled_timers=%s",
        agent.id,
        len(running),
        timer_count,
    )
    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)
    agent._pending_work_task = None
