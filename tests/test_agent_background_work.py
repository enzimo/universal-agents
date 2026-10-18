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
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import scheduled_runtime
from custom_agents.pending_work_runtime import (
    AgentStopping,
    close_pending_work,
    schedule_pending_work,
    wake_scheduler,
)
from custom_agents.strands_agent import UniversalAgent
from custom_tools.providers._runtime import _run_agent_coroutine
from tests.broker_access_support import forget_task_context_keys
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
    agent._cancel_scheduler_pass = lambda: UniversalAgent._cancel_scheduler_pass(agent)
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


@pytest.fixture
def scheduled_world(tmp_path, monkeypatch):
    """A manager and a worker sharing a schedule store; the cached signing keys are forgotten after."""
    from tests import scheduled_run_support as runs

    yield runs.build_world(tmp_path, monkeypatch)
    forget_task_context_keys()


def test_a_fired_schedule_wakes_the_retained_drain(scheduled_world):
    from tests import scheduled_run_support as runs
    from custom_agents.schedule_permits import renew_schedule_permits
    from custom_messages.contracts.access_v2 import PersonStanding

    world = scheduled_world
    runs.record_people(world, (runs.ANN, PersonStanding.ACTIVE, ("member",)))
    runs.save_schedule(world)
    renew_schedule_permits(world.manager, signer=world.signer)
    world.worker.is_executing = False  # no turn holds the slot, so the drain runs a pass

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


def _tool_agent(loop):
    return SimpleNamespace(
        id="agent_tools",
        _runtime_loop=loop,
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _pending_work_timers=set(),
        _owned_tasks=set(),
        _logger=logging.getLogger("test_agent_background_work"),
    )


async def _send_from_tool_thread(agent, coroutine, **kwargs):
    """Call the send helper from a worker thread, as Strands runs a sync tool."""
    return await asyncio.to_thread(_run_agent_coroutine, agent, coroutine, **kwargs)


@pytest.mark.asyncio
async def test_a_tool_send_runs_on_the_agent_loop_and_finishes_first():
    agent_loop = asyncio.get_running_loop()
    ran_on = []

    async def send():
        ran_on.append(asyncio.get_running_loop())

    await _send_from_tool_thread(_tool_agent(agent_loop), send())
    assert ran_on == [agent_loop]  # the agent's loop and connection, never a private loop


@pytest.mark.asyncio
async def test_a_tool_send_failure_reaches_the_tool():
    async def send():
        raise ConnectionError("broker unavailable")

    with pytest.raises(ConnectionError, match="broker unavailable"):
        await _send_from_tool_thread(_tool_agent(asyncio.get_running_loop()), send())


@pytest.mark.asyncio
async def test_a_tool_send_is_refused_from_the_event_loop_or_without_a_running_agent(caplog):
    async def send():
        raise AssertionError("must not run")

    agent = _tool_agent(asyncio.get_running_loop())
    coroutine = send()
    with caplog.at_level(logging.ERROR, logger="custom_tools.providers._runtime"):
        with pytest.raises(RuntimeError, match="the tool ran on the agent's event loop instead of a worker thread"):
            _run_agent_coroutine(agent, coroutine)
        assert coroutine.cr_frame is None  # closed, never left unawaited
        stopped = _tool_agent(None)  # no running agent loop: no private loop either
        with pytest.raises(RuntimeError, match="the agent is not running"):
            await _send_from_tool_thread(stopped, send())
    assert caplog.text.count("event=agent_tool_send_refused") == 2


@pytest.mark.asyncio
async def test_a_tool_send_after_shutdown_started_is_refused(caplog):
    """A sync tool keeps running after its turn is cancelled; its send must not publish."""
    agent = _tool_agent(asyncio.get_running_loop())
    await close_pending_work(agent)
    sent = []

    async def send():
        sent.append("published after exit")

    with caplog.at_level(logging.WARNING, logger="test_agent_background_work"):
        with pytest.raises(AgentStopping, match="^the agent is shutting down, so the message was not sent$"):
            await _send_from_tool_thread(agent, send())
    assert sent == [] and agent._owned_tasks == set()
    assert "event=owned_task_refused" in caplog.text and "tool-send:agent_tools" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_cancels_and_joins_a_tool_send_in_progress():
    agent = _tool_agent(asyncio.get_running_loop())
    sending, stopped = asyncio.Event(), []

    async def slow_send():
        sending.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(True)
            raise

    def tool() -> Exception | None:
        try:
            _run_agent_coroutine(agent, slow_send())
        except Exception as exc:  # what the tool's own error handling receives
            return exc
        return None

    seen = asyncio.create_task(asyncio.to_thread(tool))
    await asyncio.wait_for(sending.wait(), 5)
    assert len(agent._owned_tasks) == 1
    await close_pending_work(agent)
    assert stopped == [True] and agent._owned_tasks == set()
    error = await seen  # the tool reports the send as not done, and says why
    assert isinstance(error, AgentStopping)
    assert str(error) == "the agent is shutting down, so the message was not sent"


@pytest.mark.asyncio
async def test_a_tool_send_that_times_out_is_cancelled(caplog):
    agent = _tool_agent(asyncio.get_running_loop())
    stopped = asyncio.Event()

    async def stuck_send():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.set()
            raise

    with caplog.at_level(logging.WARNING, logger="custom_tools.providers._runtime"):
        with pytest.raises(TimeoutError, match="^the send did not finish within 0.05 s and was cancelled$"):
            await _send_from_tool_thread(agent, stuck_send(), timeout=0.05)
    await asyncio.wait_for(stopped.wait(), 5)  # not left running after the tool gave up
    assert "event=agent_tool_send_timed_out" in caplog.text
    await asyncio.sleep(0)
    assert agent._owned_tasks == set()


@pytest.mark.asyncio
async def test_a_tool_wakes_the_scheduler_on_the_agent_loop():
    loop_thread = threading.get_ident()
    set_on = []

    class Wake:
        def set(self):
            set_on.append(threading.get_ident())

    agent = SimpleNamespace(_scheduler_wake=Wake(), _runtime_loop=asyncio.get_running_loop())
    await asyncio.to_thread(wake_scheduler, agent)  # from a tool's worker thread
    await asyncio.sleep(0)
    wake_scheduler(agent)  # from the agent loop itself
    assert set_on == [loop_thread, loop_thread]

    idle = SimpleNamespace(_scheduler_wake=Wake(), _runtime_loop=None)
    await asyncio.to_thread(wake_scheduler, idle)  # no loop runs a scheduler yet
    assert len(set_on) == 3


@pytest.mark.asyncio
async def test_a_signal_exit_stops_the_scheduler_pass_while_the_drain_is_joined():
    events: list[str] = []
    drain_stopping, let_drain_finish = asyncio.Event(), asyncio.Event()

    async def turn_that_unwinds_slowly():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            drain_stopping.set()
            await let_drain_finish.wait()
            events.append("drain_stopped")
            raise

    async def scheduler_pass():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("scheduler_stopped")
            raise

    agent = _stopping_agent(_continue_pending_work=turn_that_unwinds_slowly)
    agent._pending_work_requested = False
    agent._scheduler_task = asyncio.create_task(scheduler_pass())
    schedule_pending_work(agent)
    await asyncio.sleep(0)

    async def close_transport():
        events.append("transport_closed")

    cleanup = asyncio.create_task(
        UniversalAgent._cleanup_nats_runtime_on_exit(agent, SimpleNamespace(close=close_transport))
    )
    await asyncio.wait_for(drain_stopping.wait(), 5)
    await asyncio.sleep(0)
    assert events == ["scheduler_stopped"]  # stopped while the drain is still being joined
    let_drain_finish.set()
    await asyncio.wait_for(cleanup, 5)
    assert events == ["scheduler_stopped", "drain_stopped", "transport_closed"]


def test_the_agent_runs_a_drain_pass_when_another_holder_releases_the_slot(tmp_path):
    from tests.test_turn_action_outbox import _make_agent

    agent = _make_agent(tmp_path)
    object.__setattr__(agent, "_continue_pending_work", AsyncMock())

    async def scenario():
        async def other_holder(wake_up: bool):
            async with agent._execution_gate.hold():
                if wake_up:
                    schedule_pending_work(agent)  # work saved while the slot is held
                await asyncio.sleep(0)

        await asyncio.create_task(other_holder(wake_up=False))
        assert agent._pending_work_task is None  # a release with nothing recorded starts no pass
        await asyncio.create_task(other_holder(wake_up=True))
        assert agent._pending_work_task is not None
        await asyncio.wait_for(agent._pending_work_task, 5)

    asyncio.run(scenario())
    agent._continue_pending_work.assert_awaited_once()


# -- owned work and a deferred message's hand-off (main's DeferredHandoff rule) --


@pytest.mark.asyncio
@pytest.mark.parametrize("owned_by", ["start_owned_task", "tool_send"])
async def test_a_save_by_owned_work_started_during_a_dispatch_does_not_take_the_message(tmp_path, owned_by):
    """Only the dispatching task's own save may take a waiting message (``DeferredHandoff``).

    Owned work runs as its own asyncio task: ``start_owned_task`` creates one,
    and a tool's send runs in a task the tool thread starts on the agent loop.
    Both copy the dispatch's context, so only the task check
    (``submitting_task``) keeps their saves from the hand-off. Here the owned
    work admits the message's own task, the one save that would take the
    message out of the deferred row, and then the dispatch fails: the message
    must stay in the deferred row for the retry.
    """
    from custom_agents.pending_work_runtime import start_owned_task
    from tests.test_custody_loss import admit, deferred_ids, held_and_deferred, OLD

    task_manager, inbox = await held_and_deferred(tmp_path)
    agent = _tool_agent(asyncio.get_running_loop())

    async def admit_the_message_task():
        # The lane copies an operation's arguments, so the manager goes in the operation itself.
        await task_manager.operations_runtime.run(partial(admit, task_manager, "m1"))

    async def dispatch(_envelope):
        if owned_by == "start_owned_task":
            await start_owned_task(agent, admit_the_message_task, name="owned:admit")
        else:
            await _send_from_tool_thread(agent, admit_the_message_task())
        raise RuntimeError("handling failed after the owned work saved")

    assert await inbox.drain(dispatch=dispatch, available=lambda: True) is False
    assert deferred_ids(task_manager.runtime_state_store, OLD) == ["m1"]
    assert [item.msg_content.task_id for item in inbox.messages] == ["m1"]
    assert agent._owned_tasks == set()
