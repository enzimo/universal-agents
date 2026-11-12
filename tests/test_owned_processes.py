"""The stops in tests/owned_processes.py leave no server a test started behind.

A server that needed a kill fails a test that otherwise passed, and leaves a failing test's
own error the one reported.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator

import pytest

from tests.owned_processes import stop_owned_process, stopped_at_exit


@pytest.fixture
def start_child() -> Iterator[Callable[[str], subprocess.Popen[str]]]:
    """Start Python children that run some code, say they are ready and sleep; kill any left."""

    started: list[subprocess.Popen[str]] = []

    def start(code: str) -> subprocess.Popen[str]:
        child = subprocess.Popen(
            [sys.executable, "-c", f"{code}\nprint('ready', flush=True)\nimport time\ntime.sleep(120)"],
            stdout=subprocess.PIPE, text=True,
        )
        started.append(child)
        assert child.stdout is not None and child.stdout.readline() == "ready\n"
        return child

    yield start
    for child in started:
        if child.poll() is None:
            child.kill()  # only when the test failed before stopping it
        child.wait()
        assert child.stdout is not None
        child.stdout.close()


def test_a_process_that_ignores_sigterm_is_killed_reaped_and_named_in_a_warning(
    start_child: Callable[[str], subprocess.Popen[str]], caplog: pytest.LogCaptureFixture,
) -> None:
    child = start_child("import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)")
    caplog.set_level(logging.WARNING, logger="tests.owned_processes")

    assert stop_owned_process(child, name="stubborn server", timeout=0.5) is True
    assert child.returncode == -signal.SIGKILL
    assert f"event=owned_process_killed name=stubborn server pid={child.pid}" in caplog.text


def test_a_paused_process_is_resumed_and_stops_on_sigterm(
    start_child: Callable[[str], subprocess.Popen[str]], caplog: pytest.LogCaptureFixture,
) -> None:
    child = start_child("")
    child.send_signal(signal.SIGSTOP)
    caplog.set_level(logging.WARNING, logger="tests.owned_processes")

    # Without the SIGCONT the SIGTERM would wait until the timeout and end in a kill.
    assert stop_owned_process(child, name="paused server", timeout=5) is False
    assert child.returncode == -signal.SIGTERM
    assert "owned_process_killed" not in caplog.text


def test_a_block_that_passed_fails_when_its_server_had_to_be_killed(
    start_child: Callable[[str], subprocess.Popen[str]],
) -> None:
    """A hung shutdown (a thread or task that never ends) is a defect, not a quiet pass."""

    child = start_child("import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)")

    with pytest.raises(pytest.fail.Exception,
                       match="stubborn server did not stop within 0.5 s of SIGTERM and was killed"):
        with stopped_at_exit(child, name="stubborn server", timeout=0.5):
            pass
    assert child.returncode == -signal.SIGKILL


def test_a_block_that_failed_keeps_its_error_when_its_server_had_to_be_killed(
    start_child: Callable[[str], subprocess.Popen[str]],
) -> None:
    child = start_child("import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)")

    with pytest.raises(RuntimeError, match="the test's own error"):
        with stopped_at_exit(child, name="stubborn server", timeout=0.5):
            raise RuntimeError("the test's own error")
    assert child.returncode == -signal.SIGKILL


def test_a_block_that_passed_stops_its_server_with_sigterm(
    start_child: Callable[[str], subprocess.Popen[str]],
) -> None:
    child = start_child("")

    with stopped_at_exit(child, name="server", timeout=5):
        pass
    assert child.returncode == -signal.SIGTERM
