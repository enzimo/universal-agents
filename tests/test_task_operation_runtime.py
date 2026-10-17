"""The task operation lane refuses an operation submitted from inside another one."""

from __future__ import annotations

import asyncio

import pytest

from custom_agents.task_operation_runtime import NestedTaskOperation, TaskOperationRuntime


def test_run_sync_inside_an_operation_raises_before_the_loop_starts() -> None:
    runtime = TaskOperationRuntime()

    def outer() -> int:
        return runtime.run_sync(lambda: 1)

    with pytest.raises(NestedTaskOperation):
        runtime.run_sync(outer)
    # The lane still serves operations afterwards.
    assert runtime.run_sync(lambda: 2) == 2


def test_run_sync_inside_a_running_operation_raises_instead_of_waiting_forever() -> None:
    runtime = TaskOperationRuntime()

    def outer() -> int:
        # The lane runs one operation at a time and this one holds it, so a
        # nested submission used to wait for itself and hang the manager.
        return runtime.run_sync(lambda: 1)

    async def main() -> None:
        with pytest.raises(NestedTaskOperation):
            await asyncio.wait_for(runtime.run(outer), timeout=5)
        assert await runtime.run(lambda: 3) == 3

    asyncio.run(main())


def test_a_tool_thread_still_joins_the_running_lane() -> None:
    runtime = TaskOperationRuntime()

    async def main() -> int:
        await runtime.run(lambda: None)  # the lane now belongs to this loop
        return await asyncio.to_thread(runtime.run_sync, lambda: 4)

    assert asyncio.run(main()) == 4
