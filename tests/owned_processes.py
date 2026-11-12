"""Stop a server process a test started, however the test ends.

A test that starts nats-server, the secure tool broker or sealbox-server owns that process
and must stop it on every exit path. ``terminate()`` followed by ``wait(timeout=...)`` is not
enough on its own:

- a test may have paused the process with SIGSTOP (an outage); a paused process does not act
  on SIGTERM until it is resumed;
- a process that hangs while shutting down makes ``wait`` raise ``TimeoutExpired``. Raised
  from a ``finally`` block, that error replaces the test body's own error and leaves the
  process running, holding its port and store until the test session ends.

``stop_owned_process`` resumes the process, asks it to stop, and kills and reaps it when it
has not stopped in time, logging a warning that names it. It is the stop for error paths:
the error already raised stays the one the test reports.

A server that needs a kill to stop is still a defect (a shutdown that waits on a thread or
task that never finishes), and only a failing test makes it visible: pytest shows logged
warnings for failing tests alone. ``stopped_at_exit`` therefore fails the test when the
block itself succeeded but the server had to be killed, and keeps the block's own error
otherwise; ``require_graceful_stop`` is the same check for a stop outside a block (a
restart, a fixture's teardown).

``record_started_processes`` lets a test check that a start helper stopped the process it
began when its wait for that process failed.
"""

from __future__ import annotations

import logging
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock

import pytest

logger = logging.getLogger(__name__)


def stop_owned_process(process: subprocess.Popen, *, name: str, timeout: float = 5.0) -> bool:
    """Stop ``process`` and reap it; returns True when SIGTERM was not enough and it was killed.

    ``name`` says which server it is in the warning logged when it had to be killed.
    ``timeout`` bounds each wait: for the SIGTERM, and for the SIGKILL after it. A process
    that has not exited even after SIGKILL is a fault worth seeing, so that ``TimeoutExpired``
    is raised (chained to any error the test body raised).
    """

    if process.poll() is not None:
        return False
    # Popen.send_signal skips a process that has exited meanwhile, so this cannot signal a
    # reused pid.
    process.send_signal(signal.SIGCONT)  # a test may have paused it
    process.terminate()
    try:
        process.wait(timeout=timeout)
        return False
    except subprocess.TimeoutExpired:
        logger.warning(
            "event=owned_process_killed name=%s pid=%s timeout_seconds=%s "
            "reason=still_running_after_sigterm",
            name, process.pid, timeout,
        )
        process.kill()
        process.wait(timeout=timeout)
        return True


def require_graceful_stop(process: subprocess.Popen, *, name: str, timeout: float = 5.0) -> None:
    """Stop ``process`` like ``stop_owned_process``; fail the test when it had to be killed."""

    if stop_owned_process(process, name=name, timeout=timeout):
        pytest.fail(f"{name} did not stop within {timeout} s of SIGTERM and was killed")


@contextmanager
def stopped_at_exit(process: subprocess.Popen, *, name: str,
                    timeout: float = 5.0) -> Iterator[subprocess.Popen]:
    """Stop ``process`` when the block ends, and kill and reap it if SIGTERM is not enough.

    When the block raised, that error is the one raised. When it succeeded, a server that
    had to be killed fails the test.
    """

    try:
        yield process
    except BaseException:
        stop_owned_process(process, name=name, timeout=timeout)
        raise
    require_graceful_stop(process, name=name, timeout=timeout)


@contextmanager
def record_started_processes() -> Iterator[list[subprocess.Popen]]:
    """Record each ``subprocess.Popen`` started inside the block.

    When the block ends, any recorded process still running is killed and every one is
    reaped and its pipes closed, so a failed check leaves no process behind.
    """

    started: list[subprocess.Popen] = []

    class RecordingPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            started.append(self)

    try:
        with mock.patch.object(subprocess, "Popen", RecordingPopen):
            yield started
    finally:
        for process in started:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
