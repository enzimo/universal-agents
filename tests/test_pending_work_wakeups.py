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


@pytest.mark.asyncio
async def test_a_failing_deferred_dispatch_does_not_repeat_by_itself():
    agent = DrainAgent()
    attempts = []

    async def failing_dispatch(**_kwargs):
        attempts.append(True)
        raise RuntimeError("store unavailable")

    agent._dispatch_message = failing_dispatch
    await agent.deferred_inbox.admit(subtask_result(agent, "task_child_result").envelope)
    schedule_pending_work(agent)
    await settled(agent)
    assert attempts == [True]  # the drain's own slot releases start no new pass
    assert len(agent.deferred_inbox) == 1  # kept for the next wake-up
    assert agent._pending_work_task.done()
    await close_pending_work(agent)


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
