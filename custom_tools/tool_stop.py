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
  ``invocation_state``, where tools find it with ``tool_stop_for``. A real
  turn's invocation_state reaches tools one level down
  (``runtime_context.caller_invocation_state``), which ``tool_stop_for``
  handles.
- Tools that run commands wrap each call in ``tracked_tool_call``: the call is
  counted while it runs, its task is fixed for its whole run (so a call that
  outlives its turn never picks up the next task's person), and the process
  supervisor (``custom_tools.shell_execution``) gets the stop event, refuses to
  start a command once it is set and stops a running one. A call that starts
  after the stop does nothing.
- Each command's process group is recorded. A command can leave processes
  running in its group after it returns (``server &``); when the turn stops,
  ``end_process_groups`` ends those too, after checking that the group number
  still belongs to that command. A process that moves itself into a new
  session or group (``setsid``, daemonizing) is not reached.
- The turn waits with ``wait_until_idle``, at most ``TOOL_STOP_GRACE_SECONDS``,
  until its tool calls have ended, so a stopped turn finishes only after its
  commands did (``custom_agents.sdk.strands.turn_tool_stop``).

A turn that ends normally leaves its background processes alone. Process
groups are read from ``/proc``, so ending leftover processes works on Linux
only, the only system the code sandbox supports.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from custom_tools.runtime_context import bind_tool_call_task, caller_invocation_state, get_agent_ref

logger = logging.getLogger(__name__)

# The key under which a turn's ToolStop travels in Strands' invocation_state.
TOOL_STOP_STATE_KEY = "ua_tool_stop"

# How long a stopped turn waits for its running tool calls to end. A stopped
# command gets SIGTERM, then SIGKILL after one second; reaping it and joining
# its two output readers takes up to about three more seconds
# (custom_tools.shell_execution). The rest is margin, for example for the
# OpenShell client to end its remote command.
TOOL_STOP_GRACE_SECONDS = 10.0

# How long processes a stopped turn's commands left behind get after SIGTERM
# before SIGKILL, the same as the supervisor gives a running command.
BACKGROUND_TERM_GRACE_SECONDS = 1.0

# How often the waits look again. They poll on the event loop rather than
# waiting in a thread, so a wait that is given up never leaves a thread behind.
_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class RunningToolCall:
    """One tool call that is still running, for the wait and its log lines."""

    tool_name: str
    tool_use_id: str
    thread_name: str
    started_monotonic: float

    def running_seconds(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 3)


@dataclass(frozen=True)
class _StartedGroup:
    """A process group one of the turn's commands started.

    ``leader_start`` is the kernel start time of the group's first process
    (its id is the group id), used to tell this group from a later process
    that got the same number.
    """

    pgid: int
    leader_start: int
    tool_name: str


@dataclass(frozen=True)
class EndedGroup:
    """Processes of one recorded group that were signalled, for the log."""

    pgid: int
    tool_name: str
    pids: tuple[int, ...] = field(default=())


def _process_stat(pid: int) -> tuple[int, int] | None:
    """(process group, kernel start time) of a live process; None when gone or a zombie."""

    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = text.rsplit(")", 1)[-1].split()
    if len(fields) < 20 or fields[0] == "Z":
        return None
    return int(fields[2]), int(fields[19])


def _group_members(pgid: int) -> list[tuple[int, int]]:
    """(pid, start time) of every live process in a process group."""

    members = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        stat = _process_stat(int(entry.name))
        if stat is not None and stat[0] == pgid:
            members.append((int(entry.name), stat[1]))
    return members


def _owned_members(group: _StartedGroup) -> tuple[int, ...]:
    """The live processes still in ``group`` that belong to it; () when the number was reused.

    A group id stays taken while any member lives, so it can be reused only
    after the whole group ended. A process whose id is the group id but whose
    start time differs is such a reuse, and nothing is signalled. Members
    started before the recorded command cannot belong to it either.
    """

    leader = _process_stat(group.pgid)
    if leader is not None and leader[1] != group.leader_start:
        return ()
    return tuple(pid for pid, started in _group_members(group.pgid) if started >= group.leader_start)


class ToolStop:
    """The stop signal, running command tool calls and process groups of one model turn.

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
        self._groups: list[_StartedGroup] = []

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
            await asyncio.sleep(_POLL_SECONDS)

    def process_group_recorder(self, tool_name: str) -> Callable[[int], None]:
        """A callback for the process supervisor: records the group of each command it starts."""

        def record(pid: int) -> None:
            stat = _process_stat(pid)
            if stat is None:
                # Gone within microseconds of starting: whatever it left
                # behind cannot be told from a later process, so it is not
                # recorded (and is logged, so the gap is visible).
                logger.warning(
                    "A command ended before its process group could be recorded; processes it left "
                    "behind are not ended with its task. event=tool_process_group_unrecorded "
                    "agent_id=%s task_id=%s tool_name=%s pid=%s",
                    self.agent_id,
                    self.task_id,
                    tool_name,
                    pid,
                )
                return
            with self._lock:
                self._groups.append(_StartedGroup(pgid=pid, leader_start=stat[1], tool_name=tool_name))

        return record

    def live_process_groups(self) -> tuple[EndedGroup, ...]:
        """Recorded groups that still have processes of their own."""

        with self._lock:
            groups = list(self._groups)
        if not groups:
            return ()
        if not Path("/proc").is_dir():
            logger.warning(
                "Cannot see process groups here (no /proc), so processes a stopped turn's commands "
                "left running are not ended. event=tool_process_groups_unavailable agent_id=%s task_id=%s",
                self.agent_id,
                self.task_id,
            )
            return ()
        live = (EndedGroup(group.pgid, group.tool_name, _owned_members(group)) for group in groups)
        return tuple(group for group in live if group.pids)

    def signal_process_groups(self, sig: signal.Signals) -> tuple[EndedGroup, ...]:
        """Send ``sig`` to every recorded group that still has processes of its own."""

        signalled = []
        for group in self.live_process_groups():
            try:
                os.killpg(group.pgid, sig)
            except (ProcessLookupError, PermissionError):
                continue
            signalled.append(group)
        return tuple(signalled)


def tool_stop_for(tool_context: Any) -> ToolStop | None:
    """The ToolStop of the turn a tool call belongs to, from its Strands tool context."""

    state = caller_invocation_state(getattr(tool_context, "invocation_state", None))
    stop = state.get(TOOL_STOP_STATE_KEY)
    return stop if isinstance(stop, ToolStop) else None


def _tool_use_id(tool_context: Any) -> str:
    tool_use = getattr(tool_context, "tool_use", None) or {}
    return str(tool_use.get("toolUseId") or "")


@contextmanager
def tracked_tool_call(tool_context: Any, tool_name: str) -> Iterator[ToolStop | None]:
    """Run one command tool call under its turn's ToolStop; yields it.

    For the whole call the active task is the turn's task, so a call still
    running after the agent moved on keeps its own person and folders. The
    caller must do nothing when the yielded stop is already requested. Every
    model turn runs with a ToolStop (custom_agents.sdk.strands.turn_tool_stop);
    a call without one cannot be stopped with its task, so it is logged
    rather than quietly run unstoppable.
    """

    stop = tool_stop_for(tool_context)
    if stop is None:
        logger.warning(
            "A command tool ran outside a turn that can stop it; stopping its task "
            "will not end its commands. event=tool_stop_missing agent_id=%s tool_name=%s tool_use_id=%s",
            getattr(get_agent_ref(), "id", None),
            tool_name,
            _tool_use_id(tool_context),
        )
        yield None
        return
    with stop.tool_call(tool_name, _tool_use_id(tool_context)), bind_tool_call_task(stop.task_id):
        yield stop


__all__ = [
    "BACKGROUND_TERM_GRACE_SECONDS",
    "TOOL_STOP_GRACE_SECONDS",
    "TOOL_STOP_STATE_KEY",
    "EndedGroup",
    "RunningToolCall",
    "ToolStop",
    "tool_stop_for",
    "tracked_tool_call",
]
