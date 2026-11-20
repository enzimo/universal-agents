"""The bundled NATS server's room as it runs, read from its monitoring endpoint (docker.nats_service).

The server takes its room once, when it starts, so the Status page reads this figure to tell
whether the server refuses new streams now (assistant_gateway.storage_capacity). These tests
serve a ``/varz`` reply from a local HTTP server: the figure read, directly although the
environment names an HTTP proxy; a reply without the figure, or not JSON, refused as
``ValueError``; an endpoint that fails, does not answer in time, cuts its reply off or answers
with something that is not HTTP raising ``OSError``. The
real server's figure is read in test_local_nats_auth_live.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from docker.nats_service import bundled_running_room


@contextmanager
def _monitoring(body: bytes, *, status: int = 200) -> Iterator[int]:
    """A local endpoint answering every request with ``body``; yields its port."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the http.server name
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _varz(max_storage: object) -> bytes:
    return json.dumps({"server_id": "N1", "jetstream": {"config": {"max_storage": max_storage, "store_dir": "/x"}}}).encode()


def test_the_room_is_read_directly_although_the_environment_names_a_proxy(monkeypatch) -> None:
    # A proxy nothing listens on: a read through it would fail.
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    with _monitoring(_varz(3_000_000_000)) as port:
        assert bundled_running_room(port) == 3_000_000_000


@pytest.mark.parametrize(
    "body",
    [_varz(True), _varz(-1), _varz("3000000000"), json.dumps({"server_id": "N1"}).encode(), b"<html>", b"[]"],
    ids=["true", "negative", "string", "no-jetstream", "not-json", "not-an-object"],
)
def test_a_reply_without_a_whole_figure_is_refused(body: bytes) -> None:
    with _monitoring(body) as port, pytest.raises(ValueError):
        bundled_running_room(port)


def test_an_endpoint_that_fails_raises_os_error() -> None:
    with _monitoring(b"{}", status=500) as port, pytest.raises(OSError):
        bundled_running_room(port)


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
    # urllib passes http.client's errors on unchanged; the reader raises OSError, as for no answer.
    with _raw_reply(reply) as port, pytest.raises(OSError, match="not valid HTTP"):
        bundled_running_room(port)


def test_an_endpoint_that_does_not_answer_in_time_raises_os_error() -> None:
    # Listening but never answering: the connection is queued, the reply never comes.
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        with pytest.raises(OSError):
            bundled_running_room(silent.getsockname()[1], timeout=0.2)
