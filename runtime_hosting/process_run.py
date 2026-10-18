"""Which run of the runtime a process belongs to, and proof that earlier runs are gone.

Why this exists: a scheduled or delegated worker is a thread inside the process
that started it (``custom_agents.strands_team.instantiate_agent_type``), and its
start record (``AgentLaunchV1.host_id``) names that process. Before the team lead
frees a start's slot or starts a replacement, it must know whether the recorded
process is still alive. Hostnames change when a container is recreated and
process numbers repeat after a restart, so neither proves anything on its own.
On 2026-10-03 a recreated container left one start "unknown" for good, and that
start blocked three schedules.

How the proof works:

- The supervisor (``docker.launch_headless``) takes an exclusive ``flock`` on
  ``InstanceLayout.process_run_lock_path`` before it starts anything and keeps
  it for its whole life. It hands the locked file to every agent process it
  starts (``subprocess.Popen(pass_fds=...)``). A ``flock`` belongs to the open
  file, so the lock stays held while any process of the run is alive, even if
  the supervisor itself is killed.
- A new supervisor starts only when it gets that lock. Getting it proves that no
  agent process of an earlier run is alive; failing to get it means one still
  is, and the supervisor refuses to start (two runtimes must never share one
  instance folder).
- Each run gets a fresh id, passed to agent processes in ``UA_PROCESS_RUN_ID``.
  Every start record carries it (``process_identity``). A record from another
  run is therefore from a process that is certainly gone.
- Within one run (for example the manager restarted on its own), the record's
  process number and kernel start time identify the process exactly, so a
  reused process number is not mistaken for the old process.

When the proof is not available the answer stays "unknown", as before: a
process started by hand without the supervisor (tests, development), a file
system without ``flock`` support, or a system without ``/proc``. Every such
case is logged so an operator can see why a start was not cleared.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

# Passed by the supervisor to every agent process it starts.
PROCESS_RUN_ID_ENV = "UA_PROCESS_RUN_ID"
# "1" when the supervisor holds the run lock, so other runs are provably gone.
PROCESS_RUN_PROVEN_ENV = "UA_PROCESS_RUN_PROVEN"
# The inherited descriptor of the locked file; the agent process keeps it open
# for its whole life and marks it not inheritable so its own children do not
# keep the lock after it exits.
PROCESS_RUN_LOCK_FD_ENV = "UA_PROCESS_RUN_LOCK_FD"
# The prefix all three share. Only agent processes themselves read them; the
# commands agents run never get them (custom_tools.non_interactive_shell), so
# a command never looks like a supervised agent process
# (custom_agents.code_access refuses commands without an agent there).
PROCESS_RUN_ENV_PREFIX = "UA_PROCESS_RUN_"

# Version tag of the process identity text stored in start records.
_IDENTITY_TAG = "run1"
_FIELD_SEPARATOR = "|"
# Stands in for a missing run id or start time.
_NONE = "-"


class ProcessRunBusy(RuntimeError):
    """A process of an earlier run still holds the instance's run lock."""


class RecordedProcessState(StrEnum):
    """What is known about the process a start record names."""

    GONE = "gone"
    ALIVE = "alive"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcessIdentity:
    """One process: its run, host, process number and kernel start time."""

    run_id: str | None
    hostname: str
    pid: int
    start_ticks: int | None

    def text(self) -> str:
        return _FIELD_SEPARATOR.join(
            (
                _IDENTITY_TAG,
                self.run_id or _NONE,
                self.hostname,
                str(self.pid),
                _NONE if self.start_ticks is None else str(self.start_ticks),
            )
        )


@dataclass(frozen=True)
class ProcessRunLock:
    """The supervisor's hold on the instance's run lock."""

    run_id: str
    fd: int | None
    proven: bool
    path: Path

    def environment(self) -> dict[str, str]:
        """Variables every agent process of this run receives."""

        values = {
            PROCESS_RUN_ID_ENV: self.run_id,
            PROCESS_RUN_PROVEN_ENV: "1" if self.proven else "0",
        }
        if self.fd is not None:
            values[PROCESS_RUN_LOCK_FD_ENV] = str(self.fd)
        return values

    def pass_fds(self) -> tuple[int, ...]:
        """Descriptors an agent process must inherit to keep the run locked."""

        return (self.fd,) if self.fd is not None else ()


def acquire_process_run(path: Path) -> ProcessRunLock:
    """Take the instance's run lock and start a new run; raise ProcessRunBusy if held.

    Called once by the supervisor before it starts any agent process. When the
    file system does not support ``flock`` the run still gets an id but is not
    proven, so earlier runs' starts are not cleared automatically (logged).
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    run_id = uuid4().hex
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise ProcessRunBusy(
            "Another run of Universal Agents is still using this instance folder: "
            f"a process from an earlier start still holds {path}. Stop every "
            "Universal Agents process for this instance, then start again."
        ) from None
    except OSError as exc:
        if exc.errno not in (errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL):
            os.close(fd)
            raise
        logger.warning(
            "This file system cannot lock the instance's run file, so worker starts "
            "left by an earlier run cannot be proven gone and stay unknown. "
            "event=process_run_lock_unsupported path=%s errno=%s",
            path,
            exc.errno,
        )
        os.close(fd)
        return ProcessRunLock(run_id=run_id, fd=None, proven=False, path=path)
    record = {
        "run_id": run_id,
        "supervisor_pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    # The content is for operators only; the lock itself is the proof.
    os.ftruncate(fd, 0)
    os.write(fd, (json.dumps(record) + "\n").encode("utf-8"))
    logger.info(
        "Started a new runtime run. event=process_run_started run_id=%s path=%s",
        run_id,
        path,
    )
    return ProcessRunLock(run_id=run_id, fd=fd, proven=True, path=path)


def adopt_process_run(environ: dict[str, str] | None = None) -> None:
    """Keep the inherited run lock to this process only.

    The descriptor arrives inheritable (``pass_fds``). Marking it close-on-exec
    stops programs this process starts from holding the run lock after it
    exits, which would make the next start wrongly refuse.
    """

    env = os.environ if environ is None else environ
    raw = str(env.get(PROCESS_RUN_LOCK_FD_ENV) or "").strip()
    if not raw:
        return
    try:
        os.set_inheritable(int(raw), False)
    except (ValueError, OSError) as exc:
        logger.warning(
            "The inherited run lock is not usable. event=process_run_lock_fd_invalid fd=%s error=%s",
            raw,
            exc,
        )


def process_start_ticks(pid: int) -> int | None:
    """The kernel start time of ``pid`` (Linux ``/proc``), or None when unavailable.

    Together with the process number it identifies one process: a reused number
    has a different start time.
    """

    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    # Field 2 (the command name) is in parentheses and may contain spaces;
    # field 22 (start time) is the 20th field after its closing parenthesis.
    fields = stat.rsplit(")", 1)[-1].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _process_exists(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


@lru_cache(maxsize=1)
def current_process_identity() -> ProcessIdentity:
    """This process's identity, read once (the run id is fixed at process start)."""

    run_id = str(os.environ.get(PROCESS_RUN_ID_ENV) or "").strip() or None
    pid = os.getpid()
    return ProcessIdentity(
        run_id=run_id,
        hostname=socket.gethostname(),
        pid=pid,
        start_ticks=process_start_ticks(pid),
    )


def current_run_proven() -> bool:
    """True when this process's run holds the instance's run lock."""

    return str(os.environ.get(PROCESS_RUN_PROVEN_ENV) or "").strip() == "1"


def parse_process_identity(text: str) -> ProcessIdentity | None:
    """The identity in a start record, or None for text in another format."""

    parts = str(text or "").split(_FIELD_SEPARATOR)
    if len(parts) != 5 or parts[0] != _IDENTITY_TAG:
        return None
    _tag, run_id, hostname, pid, start = parts
    try:
        return ProcessIdentity(
            run_id=None if run_id == _NONE else run_id,
            hostname=hostname,
            pid=int(pid),
            start_ticks=None if start == _NONE else int(start),
        )
    except ValueError:
        return None


def recorded_process_state(
    recorded_text: str,
    *,
    current: ProcessIdentity,
    run_proven: bool,
) -> RecordedProcessState:
    """Whether the process a start record names is gone, alive, or unknown.

    ``current`` is the deciding process and ``run_proven`` says whether its run
    holds the instance's run lock. A record from another run (including a
    record written before run ids existed) is gone only under a proven run;
    within one run the process number and start time decide.
    """

    recorded = parse_process_identity(recorded_text)
    if recorded is None or recorded.run_id != current.run_id:
        if run_proven and current.run_id is not None and (
            recorded is None or recorded.run_id is not None
        ):
            return RecordedProcessState.GONE
        if recorded is None:
            return RecordedProcessState.UNKNOWN
    # The same run, or processes started without the supervisor: only a
    # process on this host can be inspected.
    if recorded.hostname != current.hostname:
        return RecordedProcessState.UNKNOWN
    exists = _process_exists(recorded.pid)
    if exists is False:
        return RecordedProcessState.GONE
    if exists is None or recorded.start_ticks is None:
        return RecordedProcessState.UNKNOWN
    actual = process_start_ticks(recorded.pid)
    if actual is None:
        return RecordedProcessState.UNKNOWN
    return (
        RecordedProcessState.ALIVE
        if actual == recorded.start_ticks
        else RecordedProcessState.GONE
    )
