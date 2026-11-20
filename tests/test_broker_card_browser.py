"""Broker pages inside the real Web Chat page, in headless Chromium (A2, A6, C02, C23; review fixes F2, F8, A3).

A real broker (in this process) opens credential collection forms, and the
preview server shows them as broker cards in the real chat page
(``scripts/run_onboarding_aesthetic_preview.py``). The chat is always reached
through a small proxy that adds the gateway's own Content-Security-Policy
(``web_chat_csp``) to ``/webchat``, so the cards load under the real policy
(review finding F8a). It runs in both deployments:

- **own port**: the broker on its own port, another origin than the chat;
- **path-routed**: the proxy also serves the broker's page paths on the chat's
  origin, as behind Traefik or nginx. Here only the sandbox keeps the chat
  page out of the form.

``broker_card_browser_runner.cjs`` then checks, in the page:

- the chat's iframe is sandboxed without ``allow-same-origin`` and the chat's
  script cannot read the frame's document, also through a second, unsandboxed
  frame it makes itself of the same card address (the card view's own
  Content-Security-Policy sandbox);
- what is typed into the form survives two chat updates in the same frame
  element; the keyboard reaches the card (Shift+Tab from the message box to
  "Open in its own tab", into the frame to Decline and the form's button, and
  Tab back out), and the form is sent with Enter from the sandboxed frame
  (``Origin: null``); the card then shrinks to "Done." and focus is on the
  message box, where it stays through a later chat update (A3, C02);
- the messages the frames posted hold only the height and an outcome;
- an expired card loses its frame, and a card that expires while its frame
  has focus hands focus to the message box;
- approving a proposal in the Inbox panel puts its credential form into the
  conversation as a broker card, and "Show in the conversation" closes
  Settings and brings it into view (F2, C14);
- a thread switch and sign-out remove the frames.

A second test opens a GitHub browser sign-in (a fake ``gh``) as a card and
checks that the page's own status polling, from inside the sandboxed frame,
shrinks the card to "Done." when the sign-in ends (F8b).

Opt-in like the other browser probes (``RUN_CHROMIUM_TESTS=1``).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import urllib.request
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import unused_port

from assistant_gateway.interactive_apps.website_support import web_chat_csp
from scripts.run_onboarding_aesthetic_preview import DEFAULT_ASSET_ROOT, build_handler, preview_broker_page
from secure_tool_broker.service import SecureToolBrokerService
from secure_tools.broker_pages import BROKER_PAGE_PREFIXES
from secure_tools.providers import ToolResult
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, gateway_headers, open_gate
from tests.browser_probe import requires_chromium

RUNNER = Path(__file__).with_name("broker_card_browser_runner.cjs")
TYPED = "ptr_probe_value_7731"
# The cards that expire during the run: long enough to load and to go through the
# keyboard checks, short enough to wait for. The second expires while its frame has focus.
EXPIRING_SECONDS = 25
FOCUSED_EXPIRING_SECONDS = 40


def _serve_in_thread(app: web.Application, port: int) -> Callable[[], None]:
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", port).start())
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    def stop() -> None:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()

    return stop


def _proxy_app(chat: str, broker: str | None, web_chat_policy: str) -> web.Application:
    """The chat's front door, with the gateway's real policy on /webchat (F8a).

    With ``broker`` it also serves the broker's page paths on the same origin,
    as a reverse proxy does (the path-routed deployment).
    """

    async def forward(request: web.Request) -> web.StreamResponse:
        to_broker = broker is not None and request.path.startswith(BROKER_PAGE_PREFIXES)
        target = broker if to_broker and broker else chat
        headers = {name: value for name, value in request.headers.items() if name.lower() not in {"host"}}
        async with ClientSession(auto_decompress=False) as session:
            async with session.request(
                request.method, f"{target}{request.path_qs}", headers=headers, data=await request.read(),
                allow_redirects=False,
            ) as upstream:
                # Streamed, so the chat's live updates (server-sent events) pass through as they come.
                skipped = {"content-length", "transfer-encoding", "connection"}
                reply = web.StreamResponse(status=upstream.status)
                for name, value in upstream.headers.items():
                    if name.lower() not in skipped:
                        reply.headers.add(name, value)
                if not to_broker and request.path.rstrip("/") == "/webchat":
                    reply.headers["Content-Security-Policy"] = web_chat_policy
                await reply.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await reply.write(chunk)
                await reply.write_eof()
                return reply

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", forward)
    return app


def _open_form(broker: str) -> str:
    body = json.dumps(
        {
            "request": {
                "credential_key": "portainer_api_token",
                "service_name": "Portainer",
                "fields": [{"name": "api_token", "label": "API token", "kind": "api_key", "visibility": "secret"}],
            },
            "context": {"canonical_user_id": "user-1", "principal_type": "user", "team_id": TEST_TEAM},
        }
    ).encode()
    request = urllib.request.Request(
        f"{broker}/v1/credential-collection/sessions",
        data=body,
        headers={**gateway_headers(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as answer:  # noqa: S310 - local test broker
        return json.loads(answer.read())["form_url"].split("/", 3)[3]


@requires_chromium
@pytest.mark.parametrize("deployment", ["own_port", "path_routed"])
def test_broker_cards_in_the_real_chat_page(deployment: str, monkeypatch: pytest.MonkeyPatch) -> None:
    stored: list[dict[str, str]] = []

    def collect(_payload: Any, fields: dict[str, str], **_kwargs: Any) -> ToolResult:
        stored.append(dict(fields))
        return ToolResult(status="success", content={"status": "credential_stored"})

    monkeypatch.setattr("secure_tool_broker.service.collect_credential", collect)
    chat_port, broker_port, proxy_port = unused_port(), unused_port(), unused_port()
    chat = f"http://127.0.0.1:{chat_port}"
    broker = f"http://127.0.0.1:{broker_port}"
    origin = f"http://127.0.0.1:{proxy_port}"
    page_base = broker if deployment == "own_port" else origin

    stops: list[Callable[[], None]] = []
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate(), web_chat_origin=origin)
    stops.append(_serve_in_thread(service.build_app(), broker_port))
    preview = ThreadingHTTPServer(("127.0.0.1", chat_port), build_handler(DEFAULT_ASSET_ROOT))
    threading.Thread(target=preview.serve_forever, daemon=True).start()
    try:
        pages = [f"{page_base}/{_open_form(broker)}" for _ in range(5)]
        preview.preview_broker_pages = [  # type: ignore[attr-defined]
            preview_broker_page(pages[0], 0),
            preview_broker_page(pages[1], 1),
            preview_broker_page(pages[2], 2, seconds=EXPIRING_SECONDS),
            preview_broker_page(pages[3], 3, seconds=FOCUSED_EXPIRING_SECONDS),
        ]
        # pages[4] stands in for the account link form an approved proposal opens (F2).
        stops.append(_serve_in_thread(
            _proxy_app(chat, broker if deployment == "path_routed" else None, web_chat_csp(page_base)), proxy_port
        ))
        checked = _run(
            {"mode": "cards", "base": origin, "typed": TYPED, "follow_up": pages[4]}, timeout=240
        )
    finally:
        preview.shutdown()
        preview.server_close()
        for stop in reversed(stops):
            stop()

    assert stored == [{"api_token": TYPED}], "the typed value reached the broker, once, from the sandboxed frame"
    assert checked == [
        "sandboxed without allow-same-origin",
        "chat page cannot read the card's document",
        "an unsandboxed frame of the card address cannot be read either",
        "typed value kept across two chat updates in the same frame",
        "chat page still cannot read the card or the typed value after typing and two updates",
        "Shift+Tab from the message box reaches the card's Open in its own tab",
        "one more Shift+Tab goes into the frame, to Decline",
        "Shift+Tab reaches the form's own button",
        "Tab leaves the frame to Open in its own tab",
        "submission with Enter from the frame reached the broker and the card shrank to Done",
        "focus moved to the message box when the card shrank",
        "frames posted only height and outcome",
        "an expired card lost its frame",
        "focus stayed on the message box through that chat update",
        "a card that expired while its frame had focus handed focus to the message box",
        "approving a proposal in the Inbox panel says its forms are in the conversation",
        "Show in the conversation closed Settings and showed the form as a broker card",
        "a thread switch removed the remaining frames",
        "sign-out removed every frame",
    ]


# Stands in for ``gh auth login --web``: prints the device code and waits until stopped.
FAKE_GH = """#!/bin/sh
echo "! First copy your one-time code: ABCD-1234"
echo "Open this URL to continue in your web browser: https://github.com/login/device"
exec sleep 120
"""


def _open_sign_in(broker: str) -> str:
    body = json.dumps(
        {"context": {"canonical_user_id": "user-1", "principal_type": "user", "team_id": TEST_TEAM}, "method": "web"}
    ).encode()
    request = urllib.request.Request(
        f"{broker}/v1/auth/github/sessions", data=body, headers={**gateway_headers(), "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as answer:  # noqa: S310 - local test broker
        return json.loads(answer.read())["login_url"].split("/", 3)[3]


@requires_chromium
@pytest.mark.parametrize("deployment", ["own_port", "path_routed"])
def test_a_sign_in_card_polls_its_status_and_shrinks_when_done(
    deployment: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The browser sign-in page's status polling works from inside the sandboxed frame (F8b, A7)."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    chat_port, broker_port, proxy_port = unused_port(), unused_port(), unused_port()
    chat = f"http://127.0.0.1:{chat_port}"
    broker = f"http://127.0.0.1:{broker_port}"
    origin = f"http://127.0.0.1:{proxy_port}"
    page_base = broker if deployment == "own_port" else origin

    stops: list[Callable[[], None]] = []
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate(), web_chat_origin=origin)
    stops.append(_serve_in_thread(service.build_app(), broker_port))
    preview = ThreadingHTTPServer(("127.0.0.1", chat_port), build_handler(DEFAULT_ASSET_ROOT))
    threading.Thread(target=preview.serve_forever, daemon=True).start()
    try:
        preview.preview_broker_pages = [preview_broker_page(f"{page_base}/{_open_sign_in(broker)}", 0)]  # type: ignore[attr-defined]
        stops.append(_serve_in_thread(
            _proxy_app(chat, broker if deployment == "path_routed" else None, web_chat_csp(page_base)), proxy_port
        ))
        runner = subprocess.Popen(
            ["rtk", "proxy", "node", str(RUNNER), json.dumps({"mode": "sign_in", "base": origin})],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert runner.stdout is not None
        loaded = runner.stdout.readline()
        assert "framed sign-in page loaded" in loaded, loaded + (runner.stderr.read() if runner.stderr else "")
        # The person finished signing in on GitHub: the broker's status turns to success.
        session = next(iter(service._auth_sessions.values()))
        session.state["status"] = "success"
        session.state["message"] = "Signed in."
        out, err = runner.communicate(timeout=120)
    finally:
        for session in list(service._auth_sessions.values()):
            if session.process is not None:
                session.process.kill()
        preview.shutdown()
        preview.server_close()
        for stop in reversed(stops):
            stop()

    assert runner.returncode == 0, out + err
    assert json.loads(out.strip().splitlines()[-1]) == [
        "framed sign-in page loaded",
        "the page polled its status from inside the sandboxed frame",
        "the card shrank to Done when the sign-in ended",
    ]


def _run(arguments: dict[str, Any], *, timeout: int) -> list[str]:
    result = subprocess.run(
        ["rtk", "proxy", "node", str(RUNNER), json.dumps(arguments)],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])
