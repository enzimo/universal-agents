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
- A pass that fails, or ends on a waiting message that failed and was kept,
  is retried after a delay that starts at ``FAILURE_RETRY_FIRST_SECONDS`` and
  doubles up to ``FAILURE_RETRY_MAX_SECONDS``, resetting once a pass finishes.
  The rule is the same whether or not the failing handler recorded a wake-up:
  a failure never repeats in a tight loop (found 2026-10-17: a handler that
  recorded a wake-up and then failed was retried thousands of times a second)
  and never waits forever (a message that failed once on a short-lived error,
  such as a locked database, stayed unhandled on an idle worker, which then
  stopped asking for work and never retired).
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


FAILURE_RETRY_FIRST_SECONDS = 5.0
FAILURE_RETRY_MAX_SECONDS = 300.0


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
    """Run one pass; False ends the drain.

    A pass that fails, or ends on a waiting message that failed and was kept,
    ends the drain and is retried after a backoff delay (module docstring).
    """
    try:
        finished = await agent._continue_pending_work()
    except Exception:
        delay = _retry_after_failure(agent)
        agent._logger.exception(
            "Pending task processing failed; the queue drain tries again in %.0f s. "
            "event=pending_work_failed agent_id=%s failures=%s",
            delay,
            agent.id,
            agent._pending_work_failures,
        )
        return False
    if not finished:
        delay = _retry_after_failure(agent)
        message_id, task_id = _first_waiting_message(agent)
        agent._logger.warning(
            "A waiting message failed and was kept; the queue drain tries again in %.0f s. "
            "event=pending_work_retry_scheduled agent_id=%s message_id=%s task_id=%s failures=%s",
            delay,
            agent.id,
            message_id,
            task_id,
            agent._pending_work_failures,
        )
        return False
    agent._pending_work_failures = 0
    if agent.is_executing:
        # Another holder took the slot before this pass made all its checks;
        # its release starts the next pass.
        agent._pending_work_requested = True
        return False
    return True


def _retry_after_failure(agent: Any) -> float:
    """Arm the one backoff retry for a failed pass and return its delay.

    Only the retry, or a new wake-up, runs the next pass: a wake-up the failing
    handler recorded is dropped, so a slot release cannot retry it early.
    """
    failures: int = agent._pending_work_failures + 1
    agent._pending_work_failures = failures
    delay = min(FAILURE_RETRY_MAX_SECONDS, FAILURE_RETRY_FIRST_SECONDS * 2.0 ** min(failures - 1, 16))
    agent._pending_work_requested = False
    previous = agent._pending_work_retry
    if previous is not None:
        previous.cancel()
        agent._pending_work_timers.discard(previous)
    agent._pending_work_retry = wake_pending_work_after(agent, delay)
    return delay


def _first_waiting_message(agent: Any) -> tuple[str | None, str | None]:
    """Message and task id of the first waiting message: the one a failed pass kept."""
    waiting = agent.deferred_inbox.messages
    if not waiting:
        return None, None
    envelope = waiting[0]
    return envelope.msg_header.message_id, getattr(envelope.msg_content, "task_id", None)


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


def wake_scheduler(agent: Any) -> None:
    """Wake the agent's scheduler loop from a tool's worker thread or from the agent loop.

    ``asyncio.Event`` is not thread-safe, so from another thread the event is
    set on the agent loop. Without a running agent loop no scheduler waits on
    the event and nothing can race with setting it directly.
    """
    wake = getattr(agent, "_scheduler_wake", None)
    if wake is None:
        return
    runtime_loop = getattr(agent, "_runtime_loop", None)
    if runtime_loop is None or not runtime_loop.is_running() or _on_event_loop_thread():
        wake.set()
        return
    runtime_loop.call_soon_threadsafe(wake.set)


def _on_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def wake_pending_work_after(agent: Any, delay_seconds: float) -> asyncio.TimerHandle | None:
    """Wake the drain after ``delay_seconds``; shutdown cancels the timer."""
    if agent._should_exit.is_set():
        return None
    timers: set[asyncio.TimerHandle] = agent._pending_work_timers

    def wake() -> None:
        timers.discard(handle)
        schedule_pending_work(agent)

    handle = asyncio.get_running_loop().call_later(max(0.0, delay_seconds), wake)
    timers.add(handle)
    return handle


def start_owned_task(
    agent: Any,
    make_coroutine: Callable[[], Coroutine[Any, Any, Any]],
    *,
    name: str,
) -> asyncio.Task[None] | None:
    """Run short work in the background until it ends or the agent stops.

    Only work that may be stopped at shutdown belongs here. Once the agent is
    stopping, nothing new starts: the refusal is logged with the task name and
    the coroutine is never created. ``make_coroutine`` is called inside the
    new task, so work cancelled before it starts leaves no unawaited coroutine.
    """
    if agent._should_exit.is_set():
        agent._logger.warning(
            "The agent is stopping, so this background work was not started. "
            "event=owned_task_refused agent_id=%s task_name=%s",
            agent.id,
            name,
        )
        return None
    task = asyncio.create_task(_run_owned(agent, make_coroutine, name), name=name)
    owned: set[asyncio.Task[None]] = agent._owned_tasks
    owned.add(task)
    task.add_done_callback(owned.discard)
    return task


async def _run_owned(
    agent: Any, make_coroutine: Callable[[], Coroutine[Any, Any, Any]], name: str
) -> None:
    # The coroutine is created only once the task runs. A task cancelled
    # before its first step (shutdown right after the start) never runs this
    # body, so a coroutine created earlier would be left unawaited and Python
    # would warn "coroutine ... was never awaited".
    try:
        await make_coroutine()
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

    A cancelled drain may stop a model turn halfway. Its task stays saved as in
    progress. The note the turn read stays saved unless its decision was
    already recorded with the outbox batch (``TurnStartNotes``); results saved
    outside the outbox release it when the turn returns. Restart recovery queues
    the task again only if the agent restarts within
    ``restart_executing_task_grace_seconds`` (900 s by default) of the task's
    last saved state; after that, recovery fails the task and tells its
    requester (``runtime_recovery.should_fail_stale_executing_recovery``).
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
