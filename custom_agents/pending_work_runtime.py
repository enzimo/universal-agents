"""Keep queue draining outside transport subscription callbacks."""

import asyncio
from typing import Any


def schedule_pending_work(agent: Any) -> None:
    """Coalesce wakeups and retain the task so shutdown can join its cleanup."""
    if agent.paused or agent._should_exit.is_set():
        return
    pending = agent._pending_work_task
    if pending is None or pending.done():
        agent._pending_work_task = asyncio.create_task(
            _drain_pending_work(agent), name=f"pending-work:{agent.id}"
        )


async def _drain_pending_work(agent: Any) -> None:
    try:
        await agent._continue_pending_work()
    except Exception:
        agent._logger.exception(
            "Pending task processing failed. event=pending_work_failed agent_id=%s",
            agent.id,
        )


async def close_pending_work(agent: Any) -> None:
    agent._should_exit.set()
    pending = agent._pending_work_task
    if pending is not None:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        agent._pending_work_task = None
