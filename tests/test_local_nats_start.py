"""The managed local NATS server is ready only when the server this start launched says so.

The server runs in its own session, so one left over from an earlier start (a launcher that
crashed, or a team still running) can keep the port. The start used to wait until anything
answered on the port: the new server stopped with "address already in use" and the start went
on with the old one, which may hold another store or other credentials.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import docker.nats_service as nats_service

_LISTENING = re.compile(r"Listening for client connections on 127\.0\.0\.1:(\d+)")
needs_nats_server = pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server binary is not installed")


def _start(tmp_path: Path) -> subprocess.Popen[bytes]:
    # Like the launcher, pass a full environment without NATS credentials.
    env = {key: value for key, value in os.environ.items() if not key.startswith("NATS_")}
    return nats_service.start_local_nats(
        env=env, logger=logging.getLogger("test_local_nats_start"), cwd=tmp_path, service_root=tmp_path / "nats"
    )


@pytest.fixture
def leftover(tmp_path: Path) -> Iterator[tuple[int, subprocess.Popen[bytes]]]:
    """A nats-server from an earlier start, still holding its port."""

    log_path = tmp_path / "leftover.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(["nats-server", "-a", "127.0.0.1", "-p", "-1"], stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 10
        while True:
            text = log_path.read_text()
            listening = _LISTENING.search(text)
            if listening is not None and "Server is ready" in text:
                break
            assert process.poll() is None and time.monotonic() < deadline, text
            time.sleep(0.02)
        yield int(listening.group(1)), process
    finally:
        process.terminate()
        process.wait(timeout=5)


@needs_nats_server
def test_a_leftover_server_on_the_port_stops_the_start_and_says_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leftover: tuple[int, subprocess.Popen[bytes]]
) -> None:
    port, old_server = leftover
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_PORT", port)
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_MONITOR_PORT", -1)

    with pytest.raises(RuntimeError) as refused:
        started = _start(tmp_path)
        started.terminate()
        started.wait(timeout=5)

    message = str(refused.value)
    assert f"127.0.0.1:{port} is already in use" in message, message
    assert "Stop that nats-server" in message, message
    assert old_server.poll() is None, "the leftover server is left alone"


@needs_nats_server
def test_a_taken_monitoring_port_is_named_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        monkeypatch.setattr(nats_service, "INTERNAL_NATS_PORT", -1)
        monkeypatch.setattr(nats_service, "INTERNAL_NATS_MONITOR_PORT", port)

        with pytest.raises(RuntimeError, match=f"127.0.0.1:{port} is already in use"):
            _start(tmp_path)


class _Running:
    def poll(self) -> int | None:
        return None


def test_an_earlier_starts_ready_line_in_the_startup_log_does_not_count(tmp_path: Path) -> None:
    log_path = tmp_path / "nats-server-startup.log"
    log_path.write_text("[INF] Listening for client connections on 127.0.0.1:4222\n[INF] Server is ready\n")

    with pytest.raises(RuntimeError, match="did not say it was ready"):
        nats_service._wait_for_nats_ready(
            _Running(),  # type: ignore[arg-type]
            logger=logging.getLogger("test_local_nats_start"),
            service_root=tmp_path,
            startup_log_path=log_path,
            offset=log_path.stat().st_size,
            timeout_seconds=0.5,
        )
