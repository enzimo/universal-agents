"""The bundled NATS server's room as it runs and what its streams store, read from its monitoring endpoint (docker.nats_service).

The server takes its room once, when it starts, so the Status page reads these figures to tell
whether the server refuses new streams or new messages now (assistant_gateway.storage_capacity).
These tests serve a ``/varz`` reply from a local HTTP server: both figures read from one reply,
directly although the environment names an HTTP proxy; a reply without either figure, or not
JSON, refused as ``ValueError``; an endpoint that fails, redirects (not followed), answers with
more than the reply limit, does not answer in time, sends its reply too slowly to finish within
the one deadline, cuts its reply off or answers with something that is not HTTP raising
``OSError``. The real server's figures are read in test_local_nats_auth_live and
test_storage_capacity.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from docker.nats_service import MONITOR_REPLY_LIMIT_BYTES, RunningRoom, bundled_running_room


@contextmanager
def _serving(answer: Callable[[BaseHTTPRequestHandler], None]) -> Iterator[tuple[int, list[str]]]:
    """A local endpoint answering every request with ``answer``; yields its port and the paths asked for."""

    asked: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the http.server name
            asked.append(self.path)
            answer(self)

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], asked
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@contextmanager
def _monitoring(body: bytes, *, status: int = 200) -> Iterator[int]:
    """A local endpoint answering every request with ``body``; yields its port."""

    def answer(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        try:
            handler.wfile.write(body)
        except OSError:  # the reader stopped reading (a reply over the limit) and closed the connection
            pass

    with _serving(answer) as (port, _):
        yield port


def _varz(max_storage: object, storage: object = 1_200_000) -> bytes:
    return json.dumps({
        "server_id": "N1",
        "jetstream": {"config": {"max_storage": max_storage, "store_dir": "/x"}, "stats": {"storage": storage}},
    }).encode()


def test_both_figures_are_read_from_one_reply_directly_although_the_environment_names_a_proxy(monkeypatch) -> None:
    # A proxy nothing listens on: a read through it would fail.
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    with _monitoring(_varz(3_000_000_000, 2_500_000_000)) as port:
        assert bundled_running_room(port) == RunningRoom(room=3_000_000_000, stored=2_500_000_000)


@pytest.mark.parametrize(
    "body",
    [_varz(True), _varz(-1), _varz("3000000000"), _varz(3_000_000_000, storage=None),
     _varz(3_000_000_000, storage=True), json.dumps({"server_id": "N1"}).encode(), b"<html>", b"[]"],
    ids=["true", "negative", "string", "no-stored-figure", "stored-true", "no-jetstream", "not-json", "not-an-object"],
)
def test_a_reply_without_both_whole_figures_is_refused(body: bytes) -> None:
    with _monitoring(body) as port, pytest.raises(ValueError):
        bundled_running_room(port)


def test_an_endpoint_that_fails_raises_os_error() -> None:
    with _monitoring(b"{}", status=500) as port, pytest.raises(OSError, match="answered 500"):
        bundled_running_room(port)


def test_a_redirect_is_not_followed() -> None:
    def varz(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.end_headers()
        handler.wfile.write(_varz(1))

    with _serving(varz) as (elsewhere, asked_elsewhere):
        def redirect(handler: BaseHTTPRequestHandler) -> None:
            handler.send_response(302)
            handler.send_header("Location", f"http://127.0.0.1:{elsewhere}/varz")
            handler.end_headers()

        with _serving(redirect) as (port, asked), pytest.raises(OSError, match="answered 302"):
            bundled_running_room(port)

    assert asked == ["/varz"] and asked_elsewhere == []


def test_a_reply_longer_than_the_limit_is_refused_without_reading_it_all() -> None:
    # Valid JSON with both figures, padded past the limit: refused, however it would parse.
    padded = b'{"jetstream":{"config":{"max_storage":7},"stats":{"storage":1}},"pad":"' + b" " * MONITOR_REPLY_LIMIT_BYTES + b'"}'
    with _monitoring(padded) as port, pytest.raises(OSError, match="longer than"):
        bundled_running_room(port)


def test_a_reply_sent_too_slowly_ends_at_the_deadline_for_the_whole_read() -> None:
    # Each piece arrives well within the time left, so only a deadline for the whole read ends it.
    def trickle(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.end_headers()
        try:
            for _ in range(40):
                handler.wfile.write(b" ")
                handler.wfile.flush()
                time.sleep(0.05)
            handler.wfile.write(_varz(9))
        except OSError:  # the reader gave up and closed the connection
            pass

    with _serving(trickle) as (port, _):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            bundled_running_room(port, timeout=0.5)
        elapsed = time.monotonic() - started

    assert elapsed < 1.0  # the trickle would take two seconds


@contextmanager
def _raw_reply(reply: bytes) -> Iterator[int]:
    """A local endpoint that answers one request with exactly ``reply`` and closes; yields its port."""

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def answer() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(reply)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        thread.join(5)
        listener.close()


@pytest.mark.parametrize(
    "reply",
    [b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"jetstream":',
     b"not an HTTP reply\r\n\r\n"],
    ids=["cut-off-body", "not-http"],
)
def test_a_reply_that_is_not_valid_http_raises_os_error(reply: bytes) -> None:
    # http.client's parsing errors are raised as OSError, as for no answer.
    with _raw_reply(reply) as port, pytest.raises(OSError, match="not valid HTTP"):
        bundled_running_room(port)


def test_an_endpoint_that_does_not_answer_in_time_raises_os_error() -> None:
    # Listening but never answering: the connection is queued, the reply never comes.
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        with pytest.raises(OSError):
            bundled_running_room(silent.getsockname()[1], timeout=0.2)
