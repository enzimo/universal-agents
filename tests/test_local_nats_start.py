"""The managed local NATS server is ready only when the server this start launched says so.

The server runs in its own session, so one left over from an earlier start (a launcher that
crashed, or a team still running) can keep the port. The start used to wait until anything
answered on the port: the new server stopped with "address already in use" and the start went
on with the old one, which may hold another store or other credentials.

A start that fails after launching its server stops and reaps that server: the caller never
gets the process, so a server left running would keep the ports and refuse the next start.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import docker.nats_service as nats_service
import scripts.reconcile_nats_consumers as reconciler
from tests.own_nats_server import own_nats_server_process
from tests.owned_processes import record_started_processes, stop_owned_process, stopped_at_exit

_LOGGER = logging.getLogger("test_local_nats_start")
_LISTENING = re.compile(r"Listening for client connections on 127\.0\.0\.1:(\d+)")
_MONITORING = re.compile(r"Starting http monitor on 127\.0\.0\.1:(\d+)")
needs_nats_server = pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server binary is not installed")


def _start(tmp_path: Path) -> subprocess.Popen[bytes]:
    # Like the launcher, pass a full environment without NATS credentials.
    env = {key: value for key, value in os.environ.items() if not key.startswith("NATS_")}
    return nats_service.start_local_nats(env=env, logger=_LOGGER, cwd=tmp_path, service_root=tmp_path / "nats")


def _refusal(tmp_path: Path) -> str:
    """Start, expecting a refusal; a server that starts anyway is stopped before the test fails."""

    with pytest.raises(RuntimeError) as refused:
        stop_owned_process(_start(tmp_path), name="nats-server the start should have refused")
    return str(refused.value)


def _launched_server(started: list[subprocess.Popen]) -> subprocess.Popen:
    """The nats-server the start launched, not its ``nats-server --version`` check."""

    [server] = [process for process in started if "-sd" in process.args]
    return server


@pytest.fixture
def leftover(tmp_path: Path) -> Iterator[tuple[int, subprocess.Popen[bytes]]]:
    """A nats-server from an earlier start, still holding its port."""

    folder = tmp_path / "leftover"
    folder.mkdir()
    with own_nats_server_process(shutil.which("nats-server") or "nats-server", folder) as (url, process):
        yield int(url.rsplit(":", 1)[1]), process


@needs_nats_server
def test_a_leftover_server_on_the_port_stops_the_start_and_says_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leftover: tuple[int, subprocess.Popen[bytes]]
) -> None:
    port, old_server = leftover
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_PORT", port)
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_MONITOR_PORT", -1)

    message = _refusal(tmp_path)

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

        assert f"127.0.0.1:{port} is already in use" in _refusal(tmp_path)


@needs_nats_server
def test_a_start_that_times_out_stops_its_server_and_a_retry_on_its_ports_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER.name)
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_PORT", -1)
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_MONITOR_PORT", -1)
    with monkeypatch.context() as patched, record_started_processes() as started:
        # The server says it is ready, but not in the words this start waits for.
        patched.setattr(nats_service, "_READY", "a ready line nats-server never writes")
        patched.setattr(
            nats_service,
            "_wait_for_nats_ready",
            functools.partial(nats_service._wait_for_nats_ready, timeout_seconds=1.0),
        )
        with pytest.raises(RuntimeError, match="did not say it was ready"):
            _start(tmp_path)
        server = _launched_server(started)
        # Checked inside the block: when it ends, the recording kills whatever still runs.
        # Only the start's own poll() or wait() sets returncode, and nats-server exits with 0
        # after SIGTERM, so 0 here means the start stopped it gracefully.
        assert server.returncode == 0, f"the start left its server running or killed it: {server.returncode}"
    assert f"event=local_nats_start_cleanup pid={server.pid}" in caplog.text
    assert "event=local_nats_stop_killed" not in caplog.text

    # The ports the timed-out server held are free again: a retry on exactly them starts.
    output = (tmp_path / "nats" / "log" / "nats-server-startup.log").read_text()
    client, monitoring = _LISTENING.search(output), _MONITORING.search(output)
    assert client is not None and monitoring is not None, output
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_PORT", int(client.group(1)))
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_MONITOR_PORT", int(monitoring.group(1)))
    with stopped_at_exit(_start(tmp_path), name="nats-server of the retry") as retry:
        assert retry.poll() is None


@needs_nats_server
def test_an_interrupted_start_stops_its_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_PORT", -1)
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_MONITOR_PORT", -1)
    wait_until_ready = nats_service._wait_for_nats_ready

    def interrupted_once_ready(process: subprocess.Popen[bytes], **kwargs: object) -> None:
        wait_until_ready(process, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt  # Ctrl-C in a maintenance run, after the server came up

    monkeypatch.setattr(nats_service, "_wait_for_nats_ready", interrupted_once_ready)
    with record_started_processes() as started:
        with pytest.raises(KeyboardInterrupt):
            _start(tmp_path)
        server = _launched_server(started)
        assert server.returncode == 0, f"the start left its server running or killed it: {server.returncode}"


def test_a_server_that_ignores_sigterm_is_killed_and_reaped(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER.name)
    ignores_sigterm = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ignoring', flush=True); "
        "time.sleep(60)"
    )
    with (
        subprocess.Popen([sys.executable, "-c", ignores_sigterm], stdout=subprocess.PIPE) as child,
        stopped_at_exit(child, name="child that ignores SIGTERM"),
    ):
        assert child.stdout is not None and child.stdout.readline() == b"ignoring\n"  # its handler is set

        nats_service.stop_local_nats(child, logger=_LOGGER, terminate_seconds=0.2)

        assert child.returncode == -signal.SIGKILL
    [killed] = [record for record in caplog.records if "event=local_nats_stop_killed" in record.getMessage()]
    assert killed.levelno == logging.WARNING and f"pid={child.pid}" in killed.getMessage()


class _Unreapable:
    pid = 4242

    def __init__(self) -> None:
        self.signals: list[str] = []

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.signals.append("SIGTERM")

    def kill(self) -> None:
        self.signals.append("SIGKILL")

    def wait(self, timeout: float) -> int:
        raise subprocess.TimeoutExpired("nats-server", timeout)


def test_a_server_that_cannot_be_reaped_is_logged_and_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER.name)
    server = _Unreapable()

    nats_service.stop_local_nats(server, logger=_LOGGER, terminate_seconds=0, kill_seconds=0)  # type: ignore[arg-type]

    assert server.signals == ["SIGTERM", "SIGKILL"]
    [failed] = [record for record in caplog.records if "event=local_nats_stop_failed" in record.getMessage()]
    assert failed.levelno == logging.ERROR and "pid=4242" in failed.getMessage()


def test_the_consumer_check_stops_its_local_server_when_the_check_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=reconciler.LOGGER.name)
    # main() writes these into os.environ; setting them first makes the test's end restore them.
    for key in ("NATS_URL", "NATS_SERVERS", "NATS_JETSTREAM_ENABLED", "NATS_USER", "NATS_PASSWORD"):
        monkeypatch.setenv(key, "")
    with (
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as server,
        stopped_at_exit(server, name="stand-in for the check's local nats-server"),
    ):

        def start_local_nats(*, env: dict[str, str], **_kwargs: object) -> subprocess.Popen[bytes]:
            env.update({"NATS_USER": "check", "NATS_PASSWORD": "secret"})
            return server

        async def failing_check(**_kwargs: object) -> None:
            raise RuntimeError("the consumer check failed")

        monkeypatch.setattr(reconciler, "start_local_nats", start_local_nats)
        monkeypatch.setattr(reconciler, "reconcile_nats_consumers", failing_check)

        assert reconciler.main(["--start-local-nats"]) == 1
        # Stopped and reaped by main(); this test never polls it.
        assert server.returncode == -signal.SIGTERM
    assert "event=nats_consumer_reconciliation_failed" in caplog.text
    assert f"event=local_nats_stop pid={server.pid}" in caplog.text


class _Running:
    def poll(self) -> int | None:
        return None


def test_an_earlier_starts_ready_line_in_the_startup_log_does_not_count(tmp_path: Path) -> None:
    log_path = tmp_path / "nats-server-startup.log"
    log_path.write_text("[INF] Listening for client connections on 127.0.0.1:4222\n[INF] Server is ready\n")

    with pytest.raises(RuntimeError, match="did not say it was ready"):
        nats_service._wait_for_nats_ready(
            _Running(),  # type: ignore[arg-type]
            logger=_LOGGER,
            service_root=tmp_path,
            startup_log_path=log_path,
            offset=log_path.stat().st_size,
            timeout_seconds=0.5,
        )
