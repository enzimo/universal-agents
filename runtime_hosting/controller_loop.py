"""Supervise scheduling, reconciliation, and bounded execution independently."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def _periodic(operation: Callable[[], Awaitable[None]], interval: float) -> None:
    """Use monotonic deadlines and skip missed ticks instead of accumulating work."""
    loop = asyncio.get_running_loop()
    deadline = loop.time()
    while True:
        await operation()
        deadline = max(deadline + interval, loop.time())
        await asyncio.sleep(max(0.0, deadline - loop.time()))


async def _run_worker(
    run_once: Callable[[], Awaitable[object | None]], idle_interval: float
) -> None:
    while True:
        completed = await run_once()
        if completed is None:
            await asyncio.sleep(idle_interval)


async def supervise_execution_controller(
    *,
    materialize: Callable[[], Awaitable[None]],
    reconcile: Callable[[], Awaitable[None]],
    run_once: Callable[[], Awaitable[object | None]],
    max_concurrent_jobs: int,
    tick_interval: float,
    reconcile_interval: float,
) -> None:
    """Own every loop and workload through shutdown or an unexpected failure.

    Each execution slot claims again when its job finishes. Long jobs therefore
    cannot hold idle slots or prevent materialization and service reconciliation.
    """
    if max_concurrent_jobs < 1 or min(tick_interval, reconcile_interval) <= 0:
        raise ValueError("Controller concurrency and intervals must be positive.")
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(
            _periodic(materialize, tick_interval), name="execution-materializer"
        )
        tasks.create_task(
            _periodic(reconcile, reconcile_interval), name="service-reconciler"
        )
        for slot in range(max_concurrent_jobs):
            tasks.create_task(
                _run_worker(run_once, tick_interval), name=f"execution-slot-{slot}"
            )
