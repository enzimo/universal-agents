"""A sync tool's asynchronous work runs on its turn's own loop and stops with the turn.

``custom_agents.turn_registry.run_on_turn_loop`` exists because a sync Strands
call made in a tool's worker thread runs on a fresh event loop in another
thread, where model clients bound to the turn's loop fail or retry (PR #114
review, finding 114-loop). These tests run the real ``start_turn`` and
``end_turn`` and call the tool in a worker thread, as Strands does.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import threading
from typing import Any

import pytest

from custom_agents import turn_registry
from custom_agents.pending_work_runtime import AgentStopping
from custom_agents.provider_error_runtime import ModelStreamTimeoutError
from custom_agents.turn_registry import TurnEnded, end_turn, run_on_turn_loop, start_turn, turn_has_ended
from custom_tools.runtime_context import TaskWork, current_task_work, task_work
from custom_tools.tool_stop import ToolStop
from tests.task_work_support import run_in_turn, turn_agent

_SEEN_BY_TOOL: contextvars.ContextVar[str | None] = contextvars.ContextVar("seen_by_tool", default=None)


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turn_registry, "TURN_POLL_SECONDS", 0.01)


class _Probe:
    """A coroutine that records whether it ran, to check refused work never started and was closed."""

    def __init__(self) -> None:
        self.ran = False
        self.coroutine = self._body()

    async def _body(self) -> str:
        self.ran = True
        return "unused"

    def closed_unrun(self) -> bool:
        return not self.ran and inspect.getcoroutinestate(self.coroutine) == inspect.CORO_CLOSED


class _OwnerOnStoppedLoop:
    """A turn's owner whose event loop is not running."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def done(self) -> bool:
        return False

    def get_loop(self) -> asyncio.AbstractEventLoop:
        return self._loop


@pytest.mark.asyncio
async def test_the_work_runs_on_the_turns_loop_with_the_tools_context_as_owned_work():
    agent = turn_agent()
    turn_loop = asyncio.get_running_loop()
    seen: dict[str, Any] = {}

    async def work() -> str:
        seen["loop"] = asyncio.get_running_loop()
        seen["thread"] = threading.get_ident()
        seen["work"] = current_task_work()
        seen["context_value"] = _SEEN_BY_TOOL.get()
        seen["owned"] = asyncio.current_task() in agent._owned_tasks
        return "answer"

    def tool() -> tuple[str, int, TaskWork | None]:
        _SEEN_BY_TOOL.set("set in the tool thread")
        return run_on_turn_loop(agent, work(), name="test-work"), threading.get_ident(), current_task_work()

    result, tool_thread, tool_work = await run_in_turn(agent, "task-a", tool)

    assert result == "answer"
    assert seen["loop"] is turn_loop
    assert seen["thread"] == threading.get_ident() != tool_thread
    assert seen["work"] is tool_work and tool_work is not None and tool_work.task_id == "task-a"
    assert seen["context_value"] == "set in the tool thread"
    assert seen["owned"] is True
    assert agent._owned_tasks == set()


def _work_states() -> dict[str, Any]:
    """Each way a work record has no running turn, applied to a record of a running turn."""

    def handling_finished(agent, work):
        work.handling_finished = True

    def no_turn(agent, work):
        agent._running_turns.pop(work.turn_owner)
        work.turn_owner = None

    def removed_by_end_turn(agent, work):
        # end_turn removes the turn and puts the team model back before the runner finishes.
        agent._running_turns.pop(work.turn_owner)

    def replaced_by_a_later_turn(agent, work):
        agent._running_turns[work.turn_owner] = TaskWork(task_id=work.task_id, turn_owner=work.turn_owner)

    def stop_requested(agent, work):
        work.tool_stop.request("stopped by the test")

    return {
        "handling_finished": handling_finished,
        "no_turn": no_turn,
        "removed_by_end_turn": removed_by_end_turn,
        "replaced_by_a_later_turn": replaced_by_a_later_turn,
        "stop_requested": stop_requested,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("state", sorted(_work_states()))
async def test_turn_has_ended_for_each_state_without_a_running_turn(state):
    agent = turn_agent()
    checks: dict[str, bool] = {}

    async def turn() -> None:
        start_turn(agent, "task-a")
        work = current_task_work()
        checks["running"] = turn_has_ended(agent, work)
        _work_states()[state](agent, work)
        checks["after"] = turn_has_ended(agent, work)
        agent._running_turns.clear()

    with task_work("task-a"):
        await asyncio.create_task(turn())

    assert checks == {"running": False, "after": True}
    assert turn_has_ended(agent, None) is True


@pytest.mark.asyncio
async def test_turn_has_ended_once_the_turns_task_is_done():
    agent = turn_agent()
    with task_work("task-a") as work:
        owner = asyncio.create_task(asyncio.sleep(0))
        work.turn_owner = owner
        work.tool_stop = ToolStop(agent_id=agent.id, task_id="task-a")
        agent._running_turns[owner] = work
        assert turn_has_ended(agent, work) is False
        await owner
        assert turn_has_ended(agent, work) is True


@pytest.mark.asyncio
async def test_work_outside_a_turn_is_refused_and_never_started():
    agent = turn_agent()
    probe = _Probe()

    with pytest.raises(TurnEnded, match="test-work was not started"):
        await asyncio.to_thread(run_on_turn_loop, agent, probe.coroutine, name="test-work")

    assert probe.closed_unrun()


@pytest.mark.asyncio
async def test_a_tool_thread_that_outlives_its_stopped_turn_is_refused():
    """The stopped-thread case of finding 114-failclosed: the turn ended while the tool still ran."""

    agent = turn_agent()
    turn_cancelled = threading.Event()
    outcome: dict[str, Any] = {}

    def lingering_tool() -> None:
        turn_cancelled.wait(5)
        probe = _Probe()
        try:
            run_on_turn_loop(agent, probe.coroutine, name="test-work")
        except TurnEnded as exc:
            outcome["error"] = exc
        outcome["closed_unrun"] = probe.closed_unrun()

    async def turn() -> None:
        start_turn(agent, "task-a")
        try:
            await asyncio.to_thread(lingering_tool)
        finally:
            end_turn(agent)

    # The task's handling is still open (handling_finished is False): only the turn ended.
    with task_work("task-a") as work:
        owner = asyncio.create_task(turn())
        await asyncio.sleep(0.05)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        turn_cancelled.set()
        for _ in range(500):
            if outcome:
                break
            await asyncio.sleep(0.01)
        assert work.handling_finished is False

    assert isinstance(outcome.get("error"), TurnEnded)
    assert outcome["closed_unrun"] is True


@pytest.mark.asyncio
async def test_work_is_refused_on_an_event_loop_thread():
    agent = turn_agent()
    probe = _Probe()

    with task_work("task-a"), pytest.raises(RuntimeError, match="worker thread"):
        run_on_turn_loop(agent, probe.coroutine, name="test-work")

    assert probe.closed_unrun()


@pytest.mark.asyncio
async def test_work_is_refused_when_the_turns_loop_is_not_running():
    agent = turn_agent()
    stopped_loop = asyncio.new_event_loop()
    owner = _OwnerOnStoppedLoop(stopped_loop)
    probe = _Probe()
    try:
        with task_work("task-a") as work:
            work.turn_owner = owner  # type: ignore[assignment]
            agent._running_turns[owner] = work

            with pytest.raises(TurnEnded, match="not started"):
                await asyncio.to_thread(run_on_turn_loop, agent, probe.coroutine, name="test-work")
    finally:
        stopped_loop.close()

    assert probe.closed_unrun()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["tool_stop", "owner_cancelled"])
async def test_a_stop_while_the_work_runs_cancels_it_on_the_turns_loop(stop):
    agent = turn_agent()
    started = asyncio.Event()
    seen: dict[str, Any] = {}

    async def slow_work() -> str:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            seen["cancelled_on_loop"] = True
            raise
        return "unused"

    def tool() -> None:
        try:
            run_on_turn_loop(agent, slow_work(), name="test-work")
        except TurnEnded as exc:
            seen["error"] = exc
            seen["owned_after"] = set(agent._owned_tasks)

    async def turn() -> None:
        tool_stop = start_turn(agent, "task-a")
        thread = asyncio.create_task(asyncio.to_thread(tool))
        try:
            await started.wait()
            if stop == "tool_stop":
                tool_stop.request("stopped by the test")
            else:
                asyncio.current_task().cancel()
            await asyncio.shield(thread)
        finally:
            end_turn(agent)
            await thread

    with task_work("task-a"):
        owner = asyncio.create_task(turn())
        await asyncio.gather(owner, return_exceptions=True)

    assert isinstance(seen.get("error"), TurnEnded)
    assert seen["cancelled_on_loop"] is True
    assert seen["owned_after"] == set()


@pytest.mark.asyncio
async def test_the_works_own_timeout_reaches_the_caller_unchanged():
    agent = turn_agent()

    raised = ModelStreamTimeoutError(
        limit="idle", limit_seconds=1.0, elapsed_seconds=1.5, events_received=0, model_id="test-model"
    )

    async def timing_out() -> None:
        raise raised

    def tool() -> None:
        run_on_turn_loop(agent, timing_out(), name="test-work")

    with pytest.raises(ModelStreamTimeoutError) as caught:
        await run_in_turn(agent, "task-a", tool)

    assert caught.value is raised


@pytest.mark.asyncio
async def test_a_stopping_agent_refuses_the_work():
    agent = turn_agent()
    agent._should_exit.set()
    probe = _Probe()

    def tool() -> None:
        run_on_turn_loop(agent, probe.coroutine, name="test-work")

    with pytest.raises(TurnEnded, match="agent is stopping") as caught:
        await run_in_turn(agent, "task-a", tool)

    assert isinstance(caught.value.__cause__, AgentStopping)
    assert probe.closed_unrun()


@pytest.mark.asyncio
async def test_shutdown_cancelling_the_owned_work_ends_with_turn_ended():
    agent = turn_agent()
    started = asyncio.Event()

    async def slow_work() -> None:
        started.set()
        await asyncio.sleep(30)

    async def cancel_owned_work_like_shutdown() -> None:
        await started.wait()
        for task in list(agent._owned_tasks):
            task.cancel()

    def tool() -> None:
        run_on_turn_loop(agent, slow_work(), name="test-work")

    shutdown = asyncio.create_task(cancel_owned_work_like_shutdown())
    with pytest.raises(TurnEnded, match="cancelled"):
        await run_in_turn(agent, "task-a", tool)
    await shutdown


@pytest.mark.asyncio
async def test_cancelled_work_that_does_not_end_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(turn_registry, "TURN_WORK_CANCEL_GRACE_SECONDS", 0.05)
    agent = turn_agent()
    started = asyncio.Event()
    release = asyncio.Event()

    async def ignores_cancellation() -> None:
        started.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue

    def tool() -> None:
        run_on_turn_loop(agent, ignores_cancellation(), name="stubborn-work")

    async def turn() -> None:
        tool_stop = start_turn(agent, "task-a")
        try:
            thread = asyncio.create_task(asyncio.to_thread(tool))
            await started.wait()
            tool_stop.request("stopped by the test")
            await thread
        finally:
            end_turn(agent)

    caplog.set_level(logging.WARNING, logger="custom_agents.turn_registry")
    with task_work("task-a"), pytest.raises(TurnEnded):
        await asyncio.create_task(turn())
    release.set()
    while agent._owned_tasks:
        await asyncio.sleep(0.01)

    record = next(r for r in caplog.records if "event=turn_work_cancel_unconfirmed" in r.getMessage())
    assert "task_id=task-a" in record.getMessage() and "work_name=stubborn-work" in record.getMessage()


@pytest.mark.asyncio
async def test_work_cancelled_before_its_first_step_still_reports_its_end():
    """Why run_on_turn_loop starts its own task: the done callback runs even when nothing else does."""

    agent = turn_agent()
    probe = _Probe()
    call = turn_registry._TurnLoopCall(agent, probe.coroutine, "test-work")

    call.start()
    call.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert call.ended.is_set() and call.cancelled is True
    assert probe.closed_unrun()
    assert agent._owned_tasks == set()
