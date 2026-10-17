"""Queue wakeups and background work have one owner, and shutdown waits for all of it."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.pending_work_runtime import (
    close_pending_work,
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
