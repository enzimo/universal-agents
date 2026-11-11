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
has not stopped in time, logging a warning that names it. A test body's error then stays the
error the test reports.

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

logger = logging.getLogger(__name__)


def stop_owned_process(process: subprocess.Popen, *, name: str, timeout: float = 5.0) -> int:
    """Stop ``process`` and reap it; returns its exit code.

    ``name`` says which server it is in the warning logged when SIGTERM was not enough.
    ``timeout`` bounds each wait: for the SIGTERM, and for the SIGKILL after it. A process
    that has not exited even after SIGKILL is a fault worth seeing, so that ``TimeoutExpired``
    is raised (chained to any error the test body raised).
    """

    if process.poll() is not None:
        return process.returncode
    # Popen.send_signal skips a process that has exited meanwhile, so this cannot signal a
    # reused pid.
    process.send_signal(signal.SIGCONT)  # a test may have paused it
    process.terminate()
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning(
            "event=owned_process_killed name=%s pid=%s timeout_seconds=%s "
            "reason=still_running_after_sigterm",
            name, process.pid, timeout,
        )
        process.kill()
        return process.wait(timeout=timeout)


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
