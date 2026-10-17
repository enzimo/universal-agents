"""Stop the commands a model turn runs when its task stops.

Why this exists: Strands runs a plain (synchronous) tool in a worker thread
(``asyncio.to_thread``). Cancelling the turn's asyncio task ends the turn but
not that thread, so a ``shell`` or ``python_env`` command kept running after a
person stopped the task, after a lease-loss handback while another worker
reran the task, and through shutdown. Strands' own ``agent.cancel_signal``
cannot carry the stop: Strands clears it as soon as an invocation ends, and a
stopped turn ends within a few event-loop steps, before a tool thread that
checks every 50 ms sees it.

So each turn gets a ``ToolStop`` that Universal Agents owns and Strands never
clears:

- The turn passes its event to Strands as the invocation's ``cancel_signal``
  (Strands checks it at its own checkpoints) and puts the ``ToolStop`` in
  ``invocation_state``, where tools read it with ``tool_stop_for``.
- Tools that run commands wrap each call in ``tracked_tool_call`` and hand the
  event to the process supervisor (``custom_tools.shell_execution``), which
  refuses to start a command once the stop is set and stops a running one.
- The turn waits with ``wait_until_idle``, at most ``TOOL_STOP_GRACE_SECONDS``,
  until those calls have ended, so a stopped turn finishes only after its
  commands did (``custom_agents.sdk.strands.turn_tool_stop``).

Standard library only, so tool modules can use it without the agent runtime.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The key under which a turn's ToolStop travels in Strands' invocation_state.
TOOL_STOP_STATE_KEY = "ua_tool_stop"

# How long a stopped turn waits for its running tool calls to end. A stopped
# command gets SIGTERM, then SIGKILL after one second; reaping it and joining
# its two output readers takes up to about three more seconds
# (custom_tools.shell_execution). The rest is margin, for example for the
# OpenShell client to end its remote command.
TOOL_STOP_GRACE_SECONDS = 10.0

# How often wait_until_idle looks again. It polls on the event loop rather than
# waiting in a thread, so a wait that is given up never leaves a thread behind.
_IDLE_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class RunningToolCall:
    """One tool call that is still running, for the wait and its log lines."""

    tool_name: str
    tool_use_id: str
    thread_name: str
    started_monotonic: float

    def running_seconds(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 3)


class ToolStop:
    """The stop signal and the running command tool calls of one model turn.

    ``request`` and ``tool_call`` take the same lock, so a tool call either is
    counted before the stop (and the turn waits for it) or starts after it
    (and sees the stop before it starts anything).
    """

    def __init__(self, *, agent_id: str | None, task_id: str | None) -> None:
        self.agent_id = agent_id
        self.task_id = task_id
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._keys = itertools.count()
        self._running: dict[int, RunningToolCall] = {}

    @property
    def event(self) -> threading.Event:
        """Set once the turn must stop; never cleared."""

        return self._event

    @property
    def reason(self) -> str | None:
        """Why the stop was first requested."""

        return self._reason

    def is_requested(self) -> bool:
        return self._event.is_set()

    def request(self, reason: str) -> bool:
        """Ask every command of this turn to stop. Returns False if already asked."""

        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._event.set()
            return True

    @contextmanager
    def tool_call(self, tool_name: str, tool_use_id: str) -> Iterator[threading.Event]:
        """Count one running tool call until it returns; yields the stop event."""

        with self._lock:
            key = next(self._keys)
            self._running[key] = RunningToolCall(
                tool_name=tool_name,
                tool_use_id=tool_use_id,
                thread_name=threading.current_thread().name,
                started_monotonic=time.monotonic(),
            )
        try:
            yield self._event
        finally:
            with self._lock:
                self._running.pop(key, None)

    def running(self) -> tuple[RunningToolCall, ...]:
        with self._lock:
            return tuple(self._running.values())

    async def wait_until_idle(self, timeout_seconds: float) -> tuple[RunningToolCall, ...]:
        """Wait until no tool call runs; returns the calls still running at the timeout."""

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            still_running = self.running()
            if not still_running or time.monotonic() >= deadline:
                return still_running
            await asyncio.sleep(_IDLE_POLL_SECONDS)


def tool_stop_for(tool_context: Any) -> ToolStop | None:
    """The ToolStop of the turn a tool call belongs to, from its Strands tool context."""

    state = getattr(tool_context, "invocation_state", None) or {}
    stop = state.get(TOOL_STOP_STATE_KEY)
    return stop if isinstance(stop, ToolStop) else None


def _tool_use_id(tool_context: Any) -> str:
    tool_use = getattr(tool_context, "tool_use", None) or {}
    return str(tool_use.get("toolUseId") or "")


@contextmanager
def tracked_tool_call(tool_context: Any, tool_name: str) -> Iterator[threading.Event | None]:
    """Run one command tool call under its turn's stop; yields the stop event.

    Every model turn runs with a ToolStop (custom_agents.sdk.strands.turn_tool_stop).
    A call without one cannot be stopped with its task, so it is logged rather
    than quietly run unstoppable.
    """

    stop = tool_stop_for(tool_context)
    if stop is None:
        logger.warning(
            "A command tool ran outside a turn that can stop it; stopping its task "
            "will not end its commands. event=tool_stop_missing tool_name=%s tool_use_id=%s",
            tool_name,
            _tool_use_id(tool_context),
        )
        yield None
        return
    with stop.tool_call(tool_name, _tool_use_id(tool_context)) as event:
        yield event


__all__ = [
    "TOOL_STOP_GRACE_SECONDS",
    "TOOL_STOP_STATE_KEY",
    "RunningToolCall",
    "ToolStop",
    "tool_stop_for",
    "tracked_tool_call",
]
