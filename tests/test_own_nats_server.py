"""Each protocol 1 live test talks to a nats-server of its own (tests/own_nats_server.py).

The fixtures used to choose a free port and start the server on it afterwards; two tests
starting servers at once could choose the same one, and the test whose server could not
listen then ran against the other test's server. Found while a live test failed once under
load in about 17 runs.
"""

from __future__ import annotations

import json
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.own_nats_server import own_nats_server
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
        together.wait()
        with own_nats_server(nats_server(), folder) as url:
            found[folder] = (url, _server_id(url))
            together.wait()  # both servers run at once while each is asked who it is

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
