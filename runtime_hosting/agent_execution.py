"""Exclusive ownership of an agent's mutable model and turn state."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable
from typing import Any


class AgentExecutionGate:
    """Serialize turns while allowing inline handlers to share their owner's scope.

    Ownership belongs to an asyncio task, not a context variable inherited by
    background tasks. Waiting or cancelled contenders cannot release the owner.
    This is a runtime-local resource; durable task custody remains in the stores.

    ``on_release`` is called with the owning task each time an outer hold ends,
    including when the owner is cancelled. The agent uses it to start a queue
    drain after another holder (``pending_work_runtime.wake_after_slot_release``).
    It must not block.
    """

    def __init__(self, on_release: Callable[[asyncio.Task[Any]], None] | None = None) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self.on_release = on_release

    @property
    def busy(self) -> bool:
        return self._owner is not None

    @property
    def owned_by_current_task(self) -> bool:
        return self._owner is asyncio.current_task()

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("Agent execution requires an owning asyncio task.")
        if self._owner is owner:
            yield
            return
        await self._lock.acquire()
        self._owner = owner
        try:
            yield
        finally:
            self._owner = None
            self._lock.release()
            if self.on_release is not None:
                self.on_release(owner)
