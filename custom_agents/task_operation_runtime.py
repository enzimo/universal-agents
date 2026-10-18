"""Execute synchronous task operations in order without sharing their results."""

from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
import asyncio
from typing import Any, Callable, Optional, ParamSpec, TypeVar

from runtime_hosting.blocking_io import BlockingIOLane

_P = ParamSpec("_P")
_T = TypeVar("_T")

# The asyncio task that submitted the operation now running on the lane. The
# lane runs each operation in a copy of the submitter's context, so code inside
# the operation can tell which task asked for it (submitting_task).
_SUBMITTING_TASK: ContextVar[Optional["asyncio.Task[Any]"]] = ContextVar("submitting_task", default=None)


def submitting_task() -> Optional["asyncio.Task[Any]"]:
    """The asyncio task a task operation runs for: its lane submitter, else the current task.

    None in a thread that is neither the lane nor the event loop.
    """
    task = _SUBMITTING_TASK.get()
    if task is not None:
        return task
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _detached_operation(
    operation: Callable[..., _T], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> _T:
    # Requests and returned records are data. A caller must use another operation
    # to change owned task state, rather than retaining a live TaskInfo reference.
    result = operation(*deepcopy(args), **deepcopy(kwargs))
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
        """
        loop = self.loop
        if loop is None or not loop.is_running():
            return _detached_operation(operation, args, kwargs)
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
        token = _SUBMITTING_TASK.set(asyncio.current_task())
        try:
            return await self._io.run(_detached_operation, operation, args, kwargs)
        finally:
            _SUBMITTING_TASK.reset(token)
