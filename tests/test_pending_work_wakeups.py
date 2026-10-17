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
    """The runtime state store accepts the admission, then fails every later commit."""

    def __init__(self) -> None:
        self.saves = 0

    def save_deferred_messages(self, **_kwargs):
        self.saves += 1
        if self.saves > 1:
            raise RuntimeError("runtime state store unavailable")

    def clear_deferred_messages(self, *_args):
        raise RuntimeError("runtime state store unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["handler_fails", "store_fails_after_dispatch"])
async def test_a_failing_waiting_message_does_not_repeat_even_if_it_records_a_wake_up(failure, caplog):
    """A handler may queue work and wake the drain before it fails; the drain must still stop.

    Before, the recorded wake-up started another pass at once, which retried
    the kept message thousands of times a second.
    """
    agent = DrainAgent()
    attempts = []

    async def dispatch(**_kwargs):
        attempts.append(True)
        schedule_pending_work(agent)  # e.g. a resume that queues work and wakes the drain
        if failure == "handler_fails":
            raise RuntimeError("handler failed after waking the drain")

    agent._dispatch_message = dispatch
    if failure == "store_fails_after_dispatch":
        agent.deferred_inbox.repository = _StoreFailsAfterAdmission()
    await agent.deferred_inbox.admit(subtask_result(agent, "task_child_result").envelope)
    with caplog.at_level("WARNING", logger="tests.pending_work_support"):
        schedule_pending_work(agent)
        await asyncio.sleep(0.3)
    assert attempts == [True]
    assert len(agent.deferred_inbox) == 1  # kept for the next wake-up
    assert agent._pending_work_task.done()
    assert agent._pending_work_requested is True  # the recorded wake-up waits for a real one
    assert "event=pending_work_stopped_after_failure" in caplog.text
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
