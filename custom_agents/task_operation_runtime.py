"""Execute synchronous task operations in order without sharing their results."""

from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
import asyncio
from typing import Any, Callable, ParamSpec, TypeVar

from runtime_hosting.blocking_io import BlockingIOLane

_P = ParamSpec("_P")
_T = TypeVar("_T")

# The lane whose operation is running, if any. An operation that asks its own
# lane for another operation would wait for itself forever: the lane runs one
# operation at a time and the outer one holds it. ``run_sync`` turns that hang
# into an error. Another manager's lane in the same process is not affected.
_RUNNING_LANE: ContextVar["TaskOperationRuntime | None"] = ContextVar(
    "task_operation_lane", default=None
)


class NestedTaskOperation(RuntimeError):
    """A task operation asked its own lane for another operation while it ran."""


def _detached_operation(
    lane: "TaskOperationRuntime",
    operation: Callable[..., _T],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> _T:
    # Requests and returned records are data. A caller must use another operation
    # to change owned task state, rather than retaining a live TaskInfo reference.
    token = _RUNNING_LANE.set(lane)
    try:
        result = operation(*deepcopy(args), **deepcopy(kwargs))
    finally:
        _RUNNING_LANE.reset(token)
    return deepcopy(result)


class TaskOperationRuntime:
    """One submission lane for a manager and its state-owning collaborators."""

    def __init__(self) -> None:
        self._io = BlockingIOLane()
        self.loop: asyncio.AbstractEventLoop | None = None

    def run_sync(
        self, operation: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
    ) -> _T:
        """Join the owner lane from a synchronous SDK tool worker.

        Before runtime start, synchronous initialization has exclusive custody.
        Once a loop owns the manager, tools must wait for its operation result;
        a timeout must never report failure while a mutation continues unseen.
        Code that already runs inside an operation calls the manager directly:
        asking the lane again raises ``NestedTaskOperation``, before the loop
        starts too, so tests find the call that would hang a running manager.
        """
        if self.inside_operation():
            raise NestedTaskOperation(
                f"{getattr(operation, '__qualname__', operation)!s} was submitted from "
                "inside a running task operation; call it directly instead."
            )
        loop = self.loop
        if loop is None or not loop.is_running():
            return _detached_operation(self, operation, args, kwargs)
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            raise RuntimeError("Use await task operations on the owning event loop.")
        future = asyncio.run_coroutine_threadsafe(
            self.run(operation, *args, **kwargs), loop
        )
        return future.result()

    async def run(
        self, operation: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
    ) -> _T:
        loop = asyncio.get_running_loop()
        if self.loop is not None and self.loop is not loop and self.loop.is_running():
            raise RuntimeError("Task operations must use one owning event loop.")
        self.loop = loop
        return await self._io.run(_detached_operation, self, operation, args, kwargs)

    def inside_operation(self) -> bool:
        """Whether the calling code runs inside one of this lane's operations."""

        return _RUNNING_LANE.get() is self
