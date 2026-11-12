"""Each protocol 1 live test talks to a nats-server of its own (tests/own_nats_server.py).

The fixtures used to choose a free port and start the server on it afterwards; two tests
starting servers at once could choose the same one, and the test whose server could not
listen then ran against the other test's server. Found while a live test failed once under
load in about 17 runs.
"""

from __future__ import annotations

import json
import logging
import re
import signal
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.own_nats_server import own_nats_server, own_nats_server_process
from tests.test_live_message_contract import nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]


def _server_id(url: str) -> str:
    """The id in the INFO line the server at ``url`` sends first."""

    host, port = url.removeprefix("nats://").split(":")
    with socket.create_connection((host, int(port)), timeout=2) as connection:
        line = connection.makefile("rb").readline().decode()
    return json.loads(line.removeprefix("INFO "))["server_id"]


def test_two_tests_starting_at_once_each_talk_to_their_own_server(tmp_path: Path) -> None:
    """Both servers start at the same moment, as two tests' fixtures do in parallel runs."""

    folders = [tmp_path / "a", tmp_path / "b"]
    for folder in folders:
        folder.mkdir()
    together = threading.Barrier(2)
    found: dict[Path, tuple[str, str]] = {}

    def start(folder: Path) -> None:
        # The waits are bounded: when one server fails to start, its thread never reaches
        # the second wait, and the other thread must still leave its block and stop its
        # server instead of waiting forever (the pool's exit would then never return).
        together.wait(timeout=30)
        with own_nats_server(nats_server(), folder) as url:
            found[folder] = (url, _server_id(url))
            together.wait(timeout=30)  # both servers run at once while each is asked who it is

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(start, folder) for folder in folders]:
            future.result(timeout=30)
    (first, first_id), (second, second_id) = found[folders[0]], found[folders[1]]
    assert first != second and first_id != second_id
    for folder in folders:
        logged = re.search(r"\[INF\]\s+ID:\s+(\S+)", (folder / "nats.log").read_text())
        assert logged is not None and found[folder][1] == logged.group(1)


def test_a_server_that_cannot_start_fails_the_test_instead_of_lending_it_another(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="NATS stopped while starting"):
        with own_nats_server(nats_server(), tmp_path, "--no_such_option"):
            pass


@pytest.mark.parametrize("body_fails", [True, False], ids=["the test failed", "the test passed"])
def test_a_server_that_ignores_the_stop_request_is_killed_and_the_test_reports_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, body_fails: bool
) -> None:
    """A server that hangs while stopping no longer stays running, hides the test's own error,
    or lets a test that passed hide the hang."""

    stand_in = tmp_path / "hanging-nats-server"
    stand_in.write_text(
        f"#!{sys.executable}\n"
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('[INF] Listening for client connections on 127.0.0.1:4222', flush=True)\n"
        "print('[INF] Server is ready', flush=True)\n"
        "time.sleep(120)\n"
    )
    stand_in.chmod(0o755)
    caplog.set_level(logging.WARNING, logger="tests.owned_processes")
    if body_fails:
        with pytest.raises(RuntimeError, match="the test's own error"):
            with own_nats_server_process(str(stand_in), tmp_path) as (_url, process):
                raise RuntimeError("the test's own error")
    else:
        with pytest.raises(pytest.fail.Exception, match="did not stop within 5.0 s of SIGTERM and was killed"):
            with own_nats_server_process(str(stand_in), tmp_path) as (_url, process):
                pass
    assert process.returncode == -signal.SIGKILL
    assert f"event=owned_process_killed name=nats-server in {tmp_path} pid={process.pid}" in caplog.text


def test_a_paused_server_is_resumed_and_stops_when_its_block_ends(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A test that paused its server (an outage) still leaves no server behind."""

    caplog.set_level(logging.WARNING, logger="tests.owned_processes")
    with own_nats_server_process(nats_server(), tmp_path) as (_url, process):
        process.send_signal(signal.SIGSTOP)
    # Stopped by its SIGTERM once resumed, not killed after the wait ran out.
    assert process.returncode is not None and process.returncode != -signal.SIGKILL
    assert "owned_process_killed" not in caplog.text
