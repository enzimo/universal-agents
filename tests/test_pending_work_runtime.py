"""Queue wakeups have one owner and shutdown waits for its cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.pending_work_runtime import close_pending_work, schedule_pending_work


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

    agent = SimpleNamespace(id="lead", paused=False, _should_exit=asyncio.Event(),
                            _pending_work_task=None, _continue_pending_work=AsyncMock(side_effect=drain))
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
