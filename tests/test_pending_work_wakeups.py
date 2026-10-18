"""A wake-up of the queue drain is never lost (found 2026-10-17).

Before, a wake-up merged into a drain pass that had already made its checks was
dropped, and the drain did nothing when another holder had the execution slot.
On a quiet worker a delegated result saved in the deferred inbox then waited
for the next unrelated message. These tests run the real drain, delivery
handling, deferred inbox and execution slot (``tests.pending_work_support``).
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from custom_agents import turn_failure_runtime
from custom_agents.pending_work_runtime import close_pending_work, schedule_pending_work
from tests.pending_work_support import (
    DrainAgent,
    FakeTaskManager,
    settled,
    subtask_result,
    use_simple_queue_checks,
)


@pytest.fixture(autouse=True)
def simple_queue_checks(monkeypatch):
    use_simple_queue_checks(monkeypatch)


@pytest.mark.asyncio
async def test_message_saved_during_the_final_empty_check_is_dispatched():
    agent = DrainAgent()
    agent.task_manager.hold.armed = True
    schedule_pending_work(agent)
    draining = agent._pending_work_task
    await agent.task_manager.hold.until_entered()
    assert agent.is_executing  # the drain holds the slot for its empty check

    delivery = subtask_result(agent, "task_child_result")
    await agent.on_nats_delivery(delivery)
    assert delivery.acked and len(agent.deferred_inbox) == 1
    assert agent._pending_work_task is draining  # merged into the running drain

    agent.task_manager.hold.released.set()
    await settled(agent)
    assert agent.dispatched == ["task_child_result"]
    assert len(agent.deferred_inbox) == 0
    await close_pending_work(agent)


@pytest.mark.asyncio
async def test_message_saved_while_the_scheduler_holds_the_slot_is_dispatched():
    entered, release = threading.Event(), threading.Event()

    class SlowSlotCheck(FakeTaskManager):
        @property
        def current_task_id(self):
            entered.set()
            release.wait(5)
            return None

    agent = DrainAgent()
    agent.task_manager = SlowSlotCheck()
    holder = asyncio.create_task(turn_failure_runtime.release_orphaned_slot_when_idle(agent))
    await asyncio.to_thread(entered.wait, 5)
    assert agent.is_executing

    await agent.on_nats_delivery(subtask_result(agent, "task_child_result"))
    await settled(agent)
    assert agent.dispatched == []  # the drain waits for the holder

    release.set()
    await holder
    await settled(agent)
    assert agent.dispatched == ["task_child_result"]
    await close_pending_work(agent)


@pytest.mark.asyncio
async def test_a_task_freed_by_the_orphaned_slot_check_starts_without_new_traffic(monkeypatch):
    agent = DrainAgent()

    async def free_the_slot(target):
        target.task_manager.enqueue_task("task_was_blocked")
        return True

    monkeypatch.setattr(turn_failure_runtime, "release_orphaned_execution_slot", free_the_slot)
    assert await turn_failure_runtime.release_orphaned_slot_when_idle(agent)
    await settled(agent)
    assert agent.task_manager.would_start == ["task_was_blocked"]
    await close_pending_work(agent)


@pytest.mark.asyncio
async def test_drain_keeps_the_request_while_another_holder_runs():
    agent = DrainAgent()
    holding, release = asyncio.Event(), asyncio.Event()

    async def other_holder():
        async with agent._execution_scope():
            holding.set()
            await release.wait()

    holder = asyncio.create_task(other_holder())
    await holding.wait()
    agent.task_manager.enqueue_task("task_queued")
    schedule_pending_work(agent)
    await settled(agent)
    assert agent._pending_work_requested is True
    assert agent.task_manager.queue_checks == 0

    release.set()
    await holder
    await settled(agent)
    assert agent.task_manager.would_start == ["task_queued"]
    assert agent._pending_work_requested is False
    await close_pending_work(agent)


class _StoreFailsAfterAdmission:
    """The runtime state store accepts the admission, then fails to remove the handled message."""

    def add_deferred_message(self, **_kwargs):
        pass

    def remove_deferred_messages(self, **_kwargs):
        raise RuntimeError("runtime state store unavailable")


async def _observe(seconds: float) -> None:
    """Let the drain run for a while; fails fast instead of hanging if the loop is starved."""
    await asyncio.wait_for(asyncio.sleep(seconds), timeout=seconds + 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["handler_fails", "store_fails_after_dispatch"])
async def test_a_failing_waiting_message_does_not_repeat_even_if_it_records_a_wake_up(failure, caplog):
    """A handler may queue work and wake the drain before it fails; the drain must not spin.

    Before, the recorded wake-up started another pass at once, which retried
    the kept message thousands of times a second.
    """
    agent = DrainAgent()
    attempts = []

    async def dispatch(**_kwargs):
        attempts.append(True)
        await asyncio.sleep(0)  # a real handler yields, so a spin cannot starve the loop
        schedule_pending_work(agent)  # e.g. a resume that queues work and wakes the drain
        if failure == "handler_fails":
            raise RuntimeError("handler failed after waking the drain")

    agent._dispatch_message = dispatch
    if failure == "store_fails_after_dispatch":
        agent.deferred_inbox.repository = _StoreFailsAfterAdmission()
    envelope = subtask_result(agent, "task_child_result").envelope
    await agent.deferred_inbox.admit(envelope)
    try:
        with caplog.at_level("WARNING", logger="tests.pending_work_support"):
            schedule_pending_work(agent)
            await _observe(0.3)
        assert attempts == [True]
        assert len(agent.deferred_inbox) == 1  # kept for the retry
        assert agent._pending_work_task.done()
        assert agent._pending_work_requested is False  # only the backoff retry runs the next pass
        assert agent._pending_work_retry in agent._pending_work_timers
    finally:
        # The exit flag ends even a spinning drain, whose cancellation a failing
        # store write can swallow, so a regression fails here instead of hanging.
        await asyncio.wait_for(close_pending_work(agent), timeout=5)
    if failure == "handler_fails":
        # The inbox kept the message and the pass names it.
        retry = [r.getMessage() for r in caplog.records if "event=pending_work_retry_scheduled" in r.getMessage()]
        assert retry and envelope.msg_header.message_id in retry[0] and "task_child_result" in retry[0]
    else:
        # A failed removal of a handled message reaches the pass as an error
        # (DeferredMessageInbox._settle_dispatched); the same backoff applies.
        retry = [r.getMessage() for r in caplog.records if "event=pending_work_failed" in r.getMessage()]
        assert retry
    assert "tries again in 5 s" in retry[0]


@pytest.mark.asyncio
async def test_a_waiting_message_that_failed_once_is_retried_on_an_idle_agent(monkeypatch, caplog):
    """A short-lived error (a locked database) must not leave a message unhandled.

    Before, a handler that recorded no wake-up left the message waiting for
    unrelated traffic. On a pool worker that never comes: it stops asking for
    work while its inbox holds a message, and does not retire while it holds work.
    """
    from custom_agents import pending_work_runtime

    monkeypatch.setattr(pending_work_runtime, "FAILURE_RETRY_FIRST_SECONDS", 0.5)
    monkeypatch.setattr(turn_failure_runtime, "release_orphaned_execution_slot", _no_orphan)
    agent = DrainAgent()
    attempts = []

    async def dispatch(*, msg_content, **_kwargs):
        attempts.append(msg_content.task_id)
        if len(attempts) == 1:
            raise RuntimeError("database is locked")
        agent.dispatched.append(msg_content.task_id)

    agent._dispatch_message = dispatch
    envelope = subtask_result(agent, "task_child_result").envelope
    await agent.deferred_inbox.admit(envelope)
    with caplog.at_level("WARNING", logger="tests.pending_work_support"):
        schedule_pending_work(agent)
        await settled(agent)
        assert attempts == ["task_child_result"]
        await turn_failure_runtime.release_orphaned_slot_when_idle(agent)  # an idle scheduler pass
        await settled(agent)
        assert attempts == ["task_child_result"]  # no early retry from the scheduler
        await _observe(0.4)  # the backoff retry
        await settled(agent)
    assert agent.dispatched == ["task_child_result"] and len(agent.deferred_inbox) == 0
    assert agent._pending_work_failures == 0  # reset once a pass finished
    retained = [r.getMessage() for r in caplog.records if "event=deferred_dispatch_retained" in r.getMessage()]
    assert retained and envelope.msg_header.message_id in retained[0] and "task_child_result" in retained[0]
    await close_pending_work(agent)


async def _no_orphan(_agent):
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("records_wake_up", [False, True])
async def test_failure_retries_back_off_the_same_way_with_or_without_a_recorded_wake_up(
    records_wake_up, monkeypatch
):
    """Before, a handler that recorded a wake-up was retried on every idle scheduler pass."""
    from custom_agents import pending_work_runtime

    monkeypatch.setattr(turn_failure_runtime, "release_orphaned_execution_slot", _no_orphan)
    real_wake_after = pending_work_runtime.wake_pending_work_after
    delays: list[float] = []

    def scaled_wake_after(agent, delay_seconds):
        delays.append(delay_seconds)
        return real_wake_after(agent, delay_seconds / 1000)  # run the schedule a thousand times faster

    monkeypatch.setattr(pending_work_runtime, "wake_pending_work_after", scaled_wake_after)
    agent = DrainAgent()
    attempts = []

    async def dispatch(**_kwargs):
        attempts.append(True)
        await asyncio.sleep(0)
        if records_wake_up:
            schedule_pending_work(agent)
        raise RuntimeError("handler keeps failing")

    agent._dispatch_message = dispatch
    await agent.deferred_inbox.admit(subtask_result(agent, "task_child_result").envelope)
    schedule_pending_work(agent)
    await settled(agent)
    for _ in range(3):  # idle scheduler passes retry nothing early
        await turn_failure_runtime.release_orphaned_slot_when_idle(agent)
        await settled(agent)
    while len(attempts) < 5:
        await _observe(0.01)
    await close_pending_work(agent)
    assert delays[:5] == [5.0, 10.0, 20.0, 40.0, 80.0]
    assert len(attempts) == len(delays)  # one attempt per retry, nothing in between


def test_the_failure_backoff_is_capped(monkeypatch):
    from custom_agents import pending_work_runtime

    armed = []
    monkeypatch.setattr(pending_work_runtime, "wake_pending_work_after", lambda agent, delay: armed.append(delay))
    agent = DrainAgent()
    for _ in range(12):
        pending_work_runtime._retry_after_failure(agent)
    assert armed[-1] == pending_work_runtime.FAILURE_RETRY_MAX_SECONDS == 300.0


@pytest.mark.asyncio
async def test_a_task_queued_before_an_interrupted_pass_starts_after_the_holder():
    """A pass that another holder interrupts before its queue check records its wake-up again."""
    agent = DrainAgent()
    agent.task_manager.enqueue_task("task_queued")
    in_outbox, outbox_go = asyncio.Event(), asyncio.Event()

    async def outbox_check():
        in_outbox.set()
        await outbox_go.wait()  # storage I/O; the slot is free meanwhile
        return False

    agent._drain_due_turn_action_outbox = outbox_check
    schedule_pending_work(agent)
    await asyncio.wait_for(in_outbox.wait(), 5)
    holder_in, holder_go = asyncio.Event(), asyncio.Event()

    async def holder():  # e.g. a delivery that needs the slot and saves nothing
        async with agent._execution_scope():
            holder_in.set()
            await holder_go.wait()

    held = asyncio.create_task(holder())
    await holder_in.wait()
    outbox_go.set()  # the pass resumes, finds the slot busy and ends
    await _observe(0.05)
    assert agent.task_manager.would_start == []
    holder_go.set()
    await held
    await settled(agent)
    assert agent.task_manager.would_start == ["task_queued"]
    await close_pending_work(agent)


@pytest.mark.asyncio
async def test_a_release_with_no_recorded_wake_up_starts_no_pass():
    agent = DrainAgent()
    agent.task_manager.enqueue_task("task_queued")

    async def holder():
        async with agent._execution_scope():
            await asyncio.sleep(0)

    await asyncio.create_task(holder())
    assert await turn_failure_runtime.release_orphaned_slot_when_idle(agent) is False
    await settled(agent)
    assert agent._pending_work_task is None  # an idle scheduler pass costs no drain pass
    assert agent.task_manager.queue_checks == 0


@pytest.mark.asyncio
async def test_a_paused_or_stopping_agent_records_no_wake_up():
    agent = DrainAgent()
    agent._pending_work_requested = False
    agent.paused = True
    schedule_pending_work(agent)
    assert agent._pending_work_task is None and agent._pending_work_requested is False

    agent.paused = False
    agent._should_exit.set()
    schedule_pending_work(agent)
    assert agent._pending_work_task is None and agent._pending_work_requested is False
