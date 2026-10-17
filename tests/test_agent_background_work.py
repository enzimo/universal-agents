"""An agent's background work is owned, joined at shutdown, and stops once exit is requested.

Before 2026-10-17 a ``resume`` control message, scheduler enqueues and outbox
retries each started their own drain with a bare ``asyncio.create_task``.
Shutdown did not wait for them, so a continuation could write task state or
publish after the transport had closed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import scheduled_runtime
from custom_agents.pending_work_runtime import close_pending_work
from custom_agents.strands_agent import UniversalAgent
from custom_tools.providers._runtime import _run_agent_coroutine
from tests.pending_work_support import DrainAgent, use_simple_queue_checks


def _stopping_agent(**extra):
    agent = SimpleNamespace(
        id="agent_stopping",
        team_id=None,
        paused=False,
        is_executing=False,
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _pending_work_timers=set(),
        _owned_tasks=set(),
        _local_schedule_task=None,
        _presence_task=None,
        _scheduler_task=None,
        _agent_state_store=None,
        _logger=logging.getLogger("test_agent_background_work"),
        _refresh_self_agent_state=AsyncMock(),
    )
    for key, value in extra.items():
        setattr(agent, key, value)
    return agent


@pytest.mark.asyncio
async def test_resume_continuation_is_joined_before_transport_close():
    events: list[str] = []
    started = asyncio.Event()

    async def resumed_turn():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("turn_stopped")
            raise
        events.append("turn_wrote_state")

    agent = _stopping_agent(paused=True, _continue_pending_work=resumed_turn)
    await UniversalAgent.resume(agent)
    await started.wait()

    async def close_transport():
        events.append("transport_closed")

    await UniversalAgent._cleanup_nats_runtime_on_exit(agent, SimpleNamespace(close=close_transport))

    assert events == ["turn_stopped", "transport_closed"]
    leftovers = [task for task in asyncio.all_tasks() if task.get_name().startswith("pending-work:")]
    assert all(task.done() for task in leftovers)


@pytest.mark.asyncio
async def test_drain_starts_nothing_once_the_agent_is_stopping(monkeypatch):
    use_simple_queue_checks(monkeypatch)
    agent = DrainAgent()
    agent.task_manager.enqueue_task("task_waiting")
    agent._drain_due_turn_action_outbox = AsyncMock(return_value=False)
    agent._should_exit.set()

    await UniversalAgent._continue_pending_work(agent)
    assert await UniversalAgent._check_and_process_next_task(agent) is False

    agent._drain_due_turn_action_outbox.assert_not_awaited()
    assert agent.task_manager.queue_checks == 0 and agent.task_manager.would_start == []


@pytest.mark.asyncio
async def test_provider_retry_wakes_the_retained_drain(monkeypatch):
    use_simple_queue_checks(monkeypatch)
    agent = DrainAgent()
    agent.task_manager.known.add("task_retry")
    fire = SimpleNamespace(payload=SimpleNamespace(task_id="task_retry"), schedule_name="retry")

    assert await scheduled_runtime.enqueue_provider_overload_retry(agent, "schedule_retry", fire)

    assert agent._pending_work_task is not None
    await asyncio.wait_for(agent._pending_work_task, 5)
    assert agent.task_manager.would_start == ["task_retry"]
    await close_pending_work(agent)


def test_a_fired_schedule_wakes_the_retained_drain(tmp_path, monkeypatch):
    from tests import scheduled_run_support as runs
    from custom_agents.schedule_permits import renew_schedule_permits
    from custom_messages.contracts.access_v2 import PersonStanding

    world = runs.build_world(tmp_path, monkeypatch)
    runs.record_people(world, (runs.ANN, PersonStanding.ACTIVE, ("member",)))
    runs.save_schedule(world)
    renew_schedule_permits(world.manager, signer=world.signer)

    async def fire_and_wait():
        await runs.subscribe(world)
        result = await runs.fire(world, runs.claim(world))
        await asyncio.wait_for(world.worker._pending_work_task, 5)
        return result

    assert asyncio.run(fire_and_wait()) == {}, "the run was accepted"
    world.worker._continue_pending_work.assert_awaited_once()


@pytest.mark.asyncio
async def test_memory_review_dispatch_is_owned_and_stopped_at_shutdown(caplog):
    sending = asyncio.Event()
    stopped = []

    async def publish(_job, _payload, _routing_key):
        sending.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(True)
            raise

    agent = _stopping_agent(
        name="lead",
        named_agent_type="TeamArchitect",
        named_agent_type_queue_map={},
        team_id="team_review",
        _publish_memory_review_task=publish,
    )
    job = SimpleNamespace(plugin_id="pinned_memory_review", job_id="review_1", payload={"persisted": True})

    UniversalAgent._dispatch_memory_review_job(agent, job)
    await sending.wait()
    assert len(agent._owned_tasks) == 1
    await close_pending_work(agent)
    assert stopped == [True] and agent._owned_tasks == set()

    # The job stays saved as queued; a dispatch after exit is refused and logged.
    with caplog.at_level(logging.WARNING, logger="test_agent_background_work"):
        UniversalAgent._dispatch_memory_review_job(agent, job)
    assert agent._owned_tasks == set()
    assert "event=owned_task_refused" in caplog.text and "memory-review-dispatch:review_1" in caplog.text


@pytest.mark.asyncio
async def test_agent_tools_refuse_to_leave_sends_running_from_the_event_loop(caplog):
    sent = []

    async def send():
        sent.append(True)

    agent = SimpleNamespace(id="agent_tools", _runtime_loop=asyncio.get_running_loop())
    coroutine = send()
    with caplog.at_level(logging.ERROR, logger="custom_tools.providers._runtime"):
        with pytest.raises(RuntimeError, match="worker thread"):
            _run_agent_coroutine(agent, coroutine)
    assert coroutine.cr_frame is None  # closed, never left unawaited
    assert "event=agent_tool_loop_thread_refused" in caplog.text

    # From a tool's worker thread the send runs on the agent loop and finishes first.
    worker = threading.Thread(target=_run_agent_coroutine, args=(agent, send()))
    worker.start()
    await asyncio.to_thread(worker.join, 5)
    assert sent == [True]
