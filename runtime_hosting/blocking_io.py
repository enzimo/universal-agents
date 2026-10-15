"""Bound synchronous I/O while keeping commit disposition on the owning loop."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
import logging
from typing import Callable, TypeVar


_T = TypeVar("_T")
# At most one submitted operation per lane and four running operations per
# process. The executor owns its threads until interpreter shutdown. Callers
# retain the lane for their resource lifetime, rather than creating one per call.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ua-storage")
_LOGGER = logging.getLogger(__name__)


class BlockingIOLane:
    """Serialize one resource's submitted I/O, including cancelled callers.

    Pass detached records to the worker. Mutable runtime state belongs to the
    caller's event loop; ``committed`` publishes the result there after success,
    even if cancellation arrived while an irreversible commit was in progress.
    Cancellation never releases the lane while its worker is still running.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def run(
        self,
        operation: Callable[..., _T],
        /,
        *args: object,
        committed: Callable[[_T], None] | None = None,
        **kwargs: object,
    ) -> _T:
        async with self._lock:
            context = copy_context()
            work = partial(operation, *args, **kwargs)
            future = asyncio.get_running_loop().run_in_executor(
                _EXECUTOR, context.run, work
            )
            cancellation: asyncio.CancelledError | None = None
            while True:
                try:
                    result = await asyncio.shield(future)
                    break
                except asyncio.CancelledError as exc:
                    cancellation = exc
                except Exception:
                    if cancellation is not None:
                        _LOGGER.exception(
                            "Blocking I/O failed while its caller was cancelling",
                            extra={"event": "cancelled_blocking_io_failed"},
                        )
                    raise
            if committed is not None:
                committed(result)
            if cancellation is not None:
                raise cancellation
            return result
