"""Proving that a worker start's process is gone (runtime_hosting.process_run).

Decision rules use this test process's real identity; the lock tests start
real processes, because ``flock`` semantics (a lock shared by an inherited
descriptor, released only when every holder exits) are what the proof rests on.

Regression: on 2026-10-03 a recreated container (new hostname) left a dead
worker start "unknown" for good, and after a plain restart the old process
number belonged to a live process, so the old check also answered "alive".
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from runtime_hosting.process_run import (
    PROCESS_RUN_ID_ENV,
    PROCESS_RUN_LOCK_FD_ENV,
    PROCESS_RUN_PROVEN_ENV,
    ProcessIdentity,
    ProcessRunBusy,
    RecordedProcessState,
    acquire_process_run,
    current_process_identity,
    parse_process_identity,
    process_start_ticks,
    recorded_process_state,
)

ROOT = Path(__file__).resolve().parents[1]
GONE, ALIVE, UNKNOWN = (
    RecordedProcessState.GONE,
    RecordedProcessState.ALIVE,
    RecordedProcessState.UNKNOWN,
)
needs_proc = pytest.mark.skipif(
    process_start_ticks(os.getpid()) is None, reason="needs Linux /proc start times"
)


def me(run_id: str | None = "run-now") -> ProcessIdentity:
    real = current_process_identity()
    return ProcessIdentity(run_id, real.hostname, real.pid, real.start_ticks)


def test_identity_text_round_trips():
    identity = ProcessIdentity("abc", "host-1", 42, 1234)
    assert parse_process_identity(identity.text()) == identity
    assert parse_process_identity(ProcessIdentity(None, "h", 1, None).text()) == ProcessIdentity(None, "h", 1, None)
    assert parse_process_identity("3b1d714a967e:71:2756b2e986ae485abb2493584e306cc1") is None


@pytest.mark.parametrize(
    "recorded",
    [
        # The record from the live incident: written before run ids existed,
        # by a container whose hostname no longer exists.
        "3b1d714a967e:71:2756b2e986ae485abb2493584e306cc1",
        # Same hostname and this very process number, as after a plain restart.
        f"{current_process_identity().hostname}:{os.getpid()}:0123456789abcdef",
    ],
)
def test_start_written_before_run_ids_is_gone_under_a_proven_run(recorded):
    assert recorded_process_state(recorded, current=me(), run_proven=True) is GONE
    # Without the lock nothing proves it, so the cautious answer stays.
    assert recorded_process_state(recorded, current=me(), run_proven=False) is UNKNOWN


def test_start_from_an_earlier_run_is_gone_even_when_its_process_number_is_alive():
    earlier = me("run-before")  # same host, same (live) process number and start time
    assert recorded_process_state(earlier.text(), current=me(), run_proven=True) is GONE
    assert recorded_process_state(earlier.text(), current=me(), run_proven=False) is ALIVE


@needs_proc
def test_within_one_run_the_process_number_and_start_time_decide():
    alive = me().text()
    assert recorded_process_state(alive, current=me(), run_proven=True) is ALIVE
    reused = ProcessIdentity("run-now", me().hostname, os.getpid(), (me().start_ticks or 0) + 1)
    assert recorded_process_state(reused.text(), current=me(), run_proven=True) is GONE
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True)
    dead = ProcessIdentity("run-now", me().hostname, int(finished.stdout), 1)
    assert recorded_process_state(dead.text(), current=me(), run_proven=True) is GONE


def test_a_process_on_another_host_in_the_same_run_cannot_be_checked():
    other = ProcessIdentity("run-now", "another-host", 1, 1)
    assert recorded_process_state(other.text(), current=me(), run_proven=True) is UNKNOWN


@needs_proc
def test_a_hand_started_process_is_checked_by_process_number_only():
    """A process started without the supervisor carries no run, so another
    run's lock proves nothing about it."""
    hand_started = me(None)
    assert recorded_process_state(hand_started.text(), current=me(), run_proven=True) is ALIVE
    assert recorded_process_state(me().text(), current=me(None), run_proven=False) is ALIVE


def _start(code: str, env: dict[str, str] | None = None, pass_fds=()) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})},
        stdout=subprocess.PIPE,
        text=True,
        pass_fds=pass_fds,
    )


def _wait_free(path: Path, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return acquire_process_run(path)
        except ProcessRunBusy:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


SUPERVISOR = """
    import subprocess, sys, time
    from pathlib import Path
    from runtime_hosting.process_run import acquire_process_run
    run = acquire_process_run(Path(sys.argv[1] if len(sys.argv) > 1 else {path!r}))
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        env=run.environment(), pass_fds=run.pass_fds(),
    )
    print(child.pid, flush=True)
    time.sleep(120)
"""


def test_the_run_stays_locked_while_any_agent_process_lives(tmp_path):
    path = tmp_path / "runtime" / "process_run.lock"
    supervisor = _start(SUPERVISOR.format(path=str(path)))
    child_pid = int(supervisor.stdout.readline())
    try:
        with pytest.raises(ProcessRunBusy, match="still using this instance folder"):
            acquire_process_run(path)
        supervisor.kill()  # the supervisor dies without cleaning up
        supervisor.wait()
        with pytest.raises(ProcessRunBusy):
            acquire_process_run(path)
    finally:
        os.kill(child_pid, 9)
    run = _wait_free(path)
    assert run.proven and run.fd is not None and len(run.run_id) == 32
    assert run.environment()[PROCESS_RUN_ID_ENV] == run.run_id
    assert run.environment()[PROCESS_RUN_PROVEN_ENV] == "1"
    os.close(run.fd)


def test_programs_an_agent_process_starts_do_not_keep_the_run_locked(tmp_path):
    path = tmp_path / "process_run.lock"
    run = acquire_process_run(path)
    agent = _start(
        """
        import subprocess, sys, time
        from runtime_hosting.process_run import adopt_process_run
        adopt_process_run()
        tool = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], close_fds=False)
        print(tool.pid, flush=True)
        time.sleep(120)
        """,
        env=run.environment(),
        pass_fds=run.pass_fds(),
    )
    os.close(run.fd)  # the "supervisor" is gone; the agent process holds the run
    tool_pid = int(agent.stdout.readline())
    try:
        with pytest.raises(ProcessRunBusy):
            acquire_process_run(path)
        agent.kill()
        agent.wait()
        # The tool the agent started is still running but holds no lock.
        os.kill(tool_pid, 0)
        next_run = _wait_free(path)
        assert next_run.run_id != run.run_id
        os.close(next_run.fd)
    finally:
        os.kill(tool_pid, 9)


def test_environment_names_the_inherited_descriptor(tmp_path):
    run = acquire_process_run(tmp_path / "process_run.lock")
    try:
        assert run.environment()[PROCESS_RUN_LOCK_FD_ENV] == str(run.fd)
        assert run.pass_fds() == (run.fd,)
    finally:
        os.close(run.fd)
