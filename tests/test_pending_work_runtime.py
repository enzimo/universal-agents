"""Queue wakeups and background work have one owner, and shutdown waits for all of it."""

import asyncio
import gc
import logging
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.pending_work_runtime import (
    AgentStopping,
    close_pending_work,
    run_as_owned_task,
    schedule_pending_work,
    start_owned_task,
    wake_pending_work_after,
)


def _agent(**extra):
    agent = SimpleNamespace(
        id="lead",
        paused=False,
        is_executing=False,
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _pending_work_timers=set(),
        _owned_tasks=set(),
        _continue_pending_work=AsyncMock(),
        _logger=logging.getLogger("test_pending_work_runtime"),
    )
    for key, value in extra.items():
        setattr(agent, key, value)
    return agent


@pytest.mark.asyncio
async def test_pending_work_coalesces_wakeups_and_joins_shutdown():
    entered, cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def drain():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release_cleanup.wait()

    agent = _agent(_continue_pending_work=AsyncMock(side_effect=drain))
    schedule_pending_work(agent)
    original = agent._pending_work_task
    await entered.wait()
    schedule_pending_work(agent)
    assert agent._pending_work_task is original
    shutdown = asyncio.create_task(close_pending_work(agent))
    await cleaning.wait()
    try:
        assert not shutdown.done()
        assert agent._should_exit.is_set()
        schedule_pending_work(agent)
        assert agent._pending_work_task is original
    finally:
        release_cleanup.set()
        await asyncio.wait_for(shutdown, 2)
    assert agent._pending_work_task is None
    agent._continue_pending_work.assert_awaited_once()


@pytest.mark.asyncio
async def test_delayed_wake_runs_the_drain_at_its_time():
    agent = _agent()
    wake_pending_work_after(agent, 0.01)
    assert len(agent._pending_work_timers) == 1
    agent._continue_pending_work.assert_not_awaited()
    await asyncio.sleep(0.05)
    await asyncio.wait_for(agent._pending_work_task, 2)
    agent._continue_pending_work.assert_awaited_once()
    assert agent._pending_work_timers == set()


@pytest.mark.asyncio
async def test_shutdown_cancels_delayed_wakes():
    agent = _agent()
    wake_pending_work_after(agent, 0.01)
    await close_pending_work(agent)
    assert agent._pending_work_timers == set()
    await asyncio.sleep(0.05)
    assert agent._pending_work_task is None
    agent._continue_pending_work.assert_not_awaited()
    wake_pending_work_after(agent, 0.0)  # after exit nothing is armed
    assert agent._pending_work_timers == set()


@pytest.mark.asyncio
async def test_shutdown_cancels_and_joins_owned_tasks():
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def send():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)  # cleanup that itself awaits is still joined
            cleaned.set()

    agent = _agent()
    task = start_owned_task(agent, send, name="send:one")
    assert task in agent._owned_tasks
    await started.wait()
    await close_pending_work(agent)
    assert task.done() and cleaned.is_set()
    assert agent._owned_tasks == set()


@pytest.mark.asyncio
async def test_owned_task_failure_is_logged_and_released(caplog):
    async def send():
        raise RuntimeError("broker down")

    agent = _agent()
    with caplog.at_level(logging.ERROR, logger="test_pending_work_runtime"):
        task = start_owned_task(agent, send, name="send:fails")
        await task
    assert "event=owned_task_failed" in caplog.text and "send:fails" in caplog.text
    assert agent._owned_tasks == set()


@pytest.mark.asyncio
async def test_no_owned_task_starts_once_the_agent_is_stopping(caplog):
    created = []

    def make():
        created.append(True)
        return asyncio.sleep(0)

    agent = _agent()
    agent._should_exit.set()
    with caplog.at_level(logging.WARNING, logger="test_pending_work_runtime"):
        assert start_owned_task(agent, make, name="send:late") is None
    assert created == []  # the coroutine is never created, so nothing is left unawaited
    assert "event=owned_task_refused" in caplog.text and "send:late" in caplog.text


def test_owned_work_stopped_before_it_starts_leaves_no_unawaited_coroutine():
    """Review F1: the coroutine was created before its task ran, so a shutdown
    right after the start cancelled the task first and Python warned
    "coroutine ... was never awaited".

    The loop runs to its end inside the warning capture: the warning comes when
    the last reference to the coroutine goes, which can be after the test's own
    step returns.
    """
    ran = []

    async def send():
        ran.append(True)

    async def start_then_stop() -> bool:
        agent = _agent()
        task = start_owned_task(agent, send, name="send:early")
        await close_pending_work(agent)  # cancels the task before its first step
        return task.cancelled()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert asyncio.run(start_then_stop())
        gc.collect()
    assert ran == []
    assert [str(w.message) for w in caught if "was never awaited" in str(w.message)] == []


@pytest.mark.asyncio
async def test_shutdown_logs_what_it_cancelled(caplog):
    agent = _agent()

    async def send():
        await asyncio.Event().wait()

    start_owned_task(agent, send, name="send:open")
    wake_pending_work_after(agent, 60)
    await asyncio.sleep(0)
    with caplog.at_level(logging.INFO, logger="test_pending_work_runtime"):
        await close_pending_work(agent)
    assert "event=pending_work_closed agent_id=lead cancelled_tasks=1 cancelled_timers=1" in caplog.text


@pytest.mark.asyncio
async def test_work_run_as_owned_returns_its_result_and_failure():
    agent = _agent()

    async def answer():
        assert asyncio.current_task() in agent._owned_tasks  # shutdown would cancel it
        return 42

    async def fails():
        raise ConnectionError("broker unavailable")

    assert await run_as_owned_task(agent, answer(), name="answer") == 42
    with pytest.raises(ConnectionError):
        await run_as_owned_task(agent, fails(), name="fails")
    assert agent._owned_tasks == set()


@pytest.mark.asyncio
async def test_work_run_as_owned_is_refused_once_the_agent_is_stopping(caplog):
    agent = _agent()
    agent._should_exit.set()

    async def send():
        raise AssertionError("must not run")

    coroutine = send()
    with caplog.at_level(logging.WARNING, logger="test_pending_work_runtime"):
        with pytest.raises(AgentStopping):
            await run_as_owned_task(agent, coroutine, name="send:late")
    assert coroutine.cr_frame is None  # closed, never left unawaited
    assert "event=owned_task_refused" in caplog.text and "send:late" in caplog.text
