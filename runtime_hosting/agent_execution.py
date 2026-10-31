"""Exclusive ownership of an agent's model turns, one execution lane at a time.

An agent runs at most one model turn at a time in each lane. The main lane
serves all ordinary work, one turn after another, as before. A request a person
sent in a thread set to Web Chat's Run concurrently gets a
lane of its own (``custom_agents.execution_lanes``), so its turns run beside the
main lane's and other parallel lanes', while the turns of one request still run
one at a time. A lane owns the turn's mutable state for the whole turn,
including context preparation and result application; each turn calls its own
Strands agent (``custom_agents.strands_parts``).

Ownership belongs to an asyncio task, not a context variable inherited by
background tasks. Waiting or cancelled contenders cannot release the owner.
An owner holds one lane: a nested hold of the same lane shares the owner's
scope, and asking for another lane while holding one is a defect (two owners
waiting for each other's lanes would never finish), so it raises. This is a
runtime-local resource; durable task custody remains in the stores.

``on_release`` is called with the owning task and the lane each time an outer
hold ends, including when the owner is cancelled. The agent uses it to start
a queue drain after another holder
(``pending_work_runtime.wake_after_slot_release``). It must not block. A
failure in it is logged and never replaces the holder's own result or
exception.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from collections.abc import AsyncIterator, Callable
from typing import Any

from runtime_hosting.turn_context import turn_scoped

_LOGGER = logging.getLogger(__name__)

# Every agent has it; it serves all work that is not a parallel request.
MAIN_LANE = "main"

# The lane the current asyncio task (or the turn it started) holds, so code a
# turn runs, such as choosing its Strands agent, knows its lane.
_CURRENT_LANE: ContextVar[str | None] = turn_scoped(ContextVar("execution_lane", default=None), None)


def current_lane() -> str | None:
    """The lane held in this context, or None outside any lane."""

    return _CURRENT_LANE.get()


class AgentExecutionLanes:
    """Serialize turns per lane while allowing inline handlers to share their owner's scope."""

    def __init__(self, on_release: Callable[[asyncio.Task[Any], str], None] | None = None) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        # Holders and waiters per lane; a lane's lock goes once nobody uses it.
        self._users: dict[str, int] = {}
        self._owners: dict[str, asyncio.Task[Any]] = {}
        self.on_release = on_release

    @property
    def busy(self) -> bool:
        """Whether any lane runs a turn now."""

        return bool(self._owners)

    def lane_busy(self, lane: str) -> bool:
        return lane in self._owners

    @property
    def main_busy(self) -> bool:
        return self.lane_busy(MAIN_LANE)

    def held_lanes(self) -> list[str]:
        return sorted(self._owners)

    def lane_of_current_task(self) -> str | None:
        owner = asyncio.current_task()
        return next((lane for lane, holder in self._owners.items() if holder is owner), None)

    def owned_by_current_task(self, lane: str = MAIN_LANE) -> bool:
        return self._owners.get(lane) is asyncio.current_task()

    @asynccontextmanager
    async def hold(self, lane: str = MAIN_LANE) -> AsyncIterator[None]:
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("Agent execution requires an owning asyncio task.")
        held = self.lane_of_current_task()
        if held == lane:
            yield
            return
        if held is not None:
            raise RuntimeError(
                f"An execution owner holding lane {held} asked for lane {lane}; "
                "an owner holds one lane at a time."
            )
        lock = self._locks.setdefault(lane, asyncio.Lock())
        self._users[lane] = self._users.get(lane, 0) + 1
        try:
            await lock.acquire()
        except BaseException:
            self._leave(lane)
            raise
        self._owners[lane] = owner
        token = _CURRENT_LANE.set(lane)
        try:
            yield
        finally:
            _CURRENT_LANE.reset(token)
            del self._owners[lane]
            lock.release()
            self._leave(lane)
            self._run_release_callback(owner, lane)

    def _leave(self, lane: str) -> None:
        remaining = self._users.get(lane, 1) - 1
        if remaining > 0:
            self._users[lane] = remaining
            return
        self._users.pop(lane, None)
        if lane != MAIN_LANE:
            self._locks.pop(lane, None)

    def _run_release_callback(self, owner: asyncio.Task[Any], lane: str) -> None:
        if self.on_release is None:
            return
        try:
            self.on_release(owner, lane)
        except Exception:
            _LOGGER.exception(
                "The execution lane's release callback failed. "
                "event=execution_lane_release_callback_failed owner_task=%s lane=%s",
                owner.get_name(),
                lane,
            )


__all__ = ["AgentExecutionLanes", "MAIN_LANE", "current_lane"]
