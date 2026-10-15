from __future__ import annotations

import asyncio
from builtins import ExceptionGroup

import pytest

from runtime_hosting.controller_loop import supervise_execution_controller


@pytest.mark.asyncio
async def test_controller_failure_cancels_sibling_loops_and_workloads() -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def materialize() -> None:
        await started.wait()
        raise RuntimeError("materialization failed")

    async def reconcile() -> None:
        await asyncio.Event().wait()

    async def run_once() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    with pytest.raises(ExceptionGroup) as caught:
        await asyncio.wait_for(
            supervise_execution_controller(
                materialize=materialize,
                reconcile=reconcile,
                run_once=run_once,
                max_concurrent_jobs=1,
                tick_interval=0.01,
                reconcile_interval=0.02,
            ),
            timeout=2,
        )
    assert str(caught.value.exceptions[0]) == "materialization failed"
    assert cleaned.is_set()
