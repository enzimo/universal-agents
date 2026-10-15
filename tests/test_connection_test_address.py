"""The live test behind /auth model and endpoint switching, against local fake endpoints.

Owner's report (2026-10-02): switching to a working Bifrost endpoint failed with
"no response within 20 seconds". Bifrost answers HTTP 405 at the host's root and
serves the API under /v1; a reasoning model then thinks in silence before its
first output. Covered:

* a model that is silent for longer than the old 20 seconds passes when the
  test waits as long as agents' own requests do, and a too-short wait fails
  with a message saying what to check;
* a bare host is tried with /v1 first, so the root (a web interface or a
  proxy) is asked only when /v1 is not the API.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from model_providers.connection_test import ConnectionTestRequest, check_endpoint_address
from model_providers.endpoints import EndpointKind


class _Bifrost(BaseHTTPRequestHandler):
    """/v1/responses streams after ``silence`` seconds; anything else answers 405."""

    silence = 0.0
    paths: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).paths.append(self.path)
        if self.path != "/v1/responses":
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'data: {"type":"response.created","response":{}}\n\n')
        self.wfile.flush()
        time.sleep(self.silence)  # the model thinks; nothing is sent
        done = {"type": "response.completed", "response": {"output_text": "I'm connected and ready to go!"}}
        self.wfile.write(f"data: {json.dumps(done)}\n\n".encode())
        self.wfile.flush()

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def bifrost():
    _Bifrost.paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Bifrost)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _test(base_url: str, *, timeout_seconds: float):
    return check_endpoint_address(
        ConnectionTestRequest(
            kind=EndpointKind.OPENAI_COMPATIBLE,
            base_url=base_url,
            key="sk-test",
            model="openai/gpt-6-luna",
            api_mode="responses",
            timeout_seconds=timeout_seconds,
        )
    )


def test_a_model_that_thinks_in_silence_passes_when_the_test_waits_like_agents_do(bifrost) -> None:
    _Bifrost.silence = 1.5

    patient = _test(bifrost, timeout_seconds=5)
    hasty = _test(bifrost, timeout_seconds=0.5)

    assert patient.ok and patient.base_url == f"{bifrost}/v1" and patient.moved
    assert not hasty.ok
    assert "sent nothing for 0.5 seconds" in hasty.result.message
    assert "llm.request_timeout_seconds" in hasty.result.message


def test_a_bare_host_is_tried_with_v1_first(bifrost) -> None:
    _Bifrost.silence = 0.0

    outcome = _test(f"{bifrost}/", timeout_seconds=5)

    assert outcome.ok and outcome.tried == (f"{bifrost}/v1",)
    assert _Bifrost.paths == ["/v1/responses"]


def test_an_address_with_a_path_is_tried_as_typed_first(bifrost) -> None:
    _Bifrost.silence = 0.0

    outcome = _test(f"{bifrost}/api", timeout_seconds=5)

    assert not outcome.ok and outcome.tried == (f"{bifrost}/api", f"{bifrost}/api/v1")


class _SlowGateway(BaseHTTPRequestHandler):
    """A gateway that gives up on the model: HTTP 504 after a short wait."""

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(0.3)
        body = b"Gateway Timeout"
        self.send_response(504)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


def test_a_gateway_timeout_names_the_address_how_long_it_took_and_what_it_means() -> None:
    from model_providers.connection_test import failure_explanation

    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        outcome = _test(f"{base}/v1", timeout_seconds=5)
    finally:
        server.shutdown()

    assert outcome.result.status_code == 504 and outcome.result.url == f"{base}/v1/responses"
    assert outcome.result.message.startswith(f"{base}/v1/responses answered HTTP 504 after 0.")
    assert "A gateway in front of the model gave up waiting for the model's answer after 0 seconds" in failure_explanation(outcome)
