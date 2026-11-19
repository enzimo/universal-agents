"""Broker pages inside the real Web Chat page, in headless Chromium (A2, A6, C02, C23).

A real broker (in this process) opens three credential collection forms, and
the preview server shows them as broker cards in the real chat page
(``scripts/run_onboarding_aesthetic_preview.py``). It runs in both deployments:

- **own port**: the broker on its own port, another origin than the chat;
- **path-routed**: a small reverse proxy serves the chat and the broker's page
  paths on one origin, as behind Traefik or nginx. Here only the sandbox keeps
  the chat page out of the form.

``broker_card_browser_runner.cjs`` then checks, in the page:

- the chat's iframe is sandboxed without ``allow-same-origin`` and the chat's
  script cannot read the frame's document, also through a second, unsandboxed
  frame it makes itself of the same card address (the card view's own
  Content-Security-Policy sandbox);
- what is typed into the form survives two chat updates in the same frame
  element, and reaches the broker on submission from the sandboxed frame
  (``Origin: null``); the card then shrinks to "Done.";
- the messages the frames posted hold only the height and an outcome;
- an expired card loses its frame, and a thread switch and sign-out remove the
  others.

Opt-in like the other browser probes (``RUN_CHROMIUM_TESTS=1``).
"""

from __future__ import annotations

import asyncio
import json
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

from scripts.run_onboarding_aesthetic_preview import DEFAULT_ASSET_ROOT, build_handler, preview_broker_page
from secure_tool_broker.service import SecureToolBrokerService
from secure_tools.broker_pages import BROKER_PAGE_PREFIXES
from secure_tools.providers import ToolResult
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, gateway_headers, open_gate
from tests.browser_probe import requires_chromium

RUNNER = Path(__file__).with_name("broker_card_browser_runner.cjs")
TYPED = "ptr_probe_value_7731"
# The card that expires during the run: long enough to load, short enough to wait for.
EXPIRING_SECONDS = 25


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


def _proxy_app(chat: str, broker: str) -> web.Application:
    """One origin for both, like a reverse proxy: broker page paths to the broker, the rest to the chat."""

    async def forward(request: web.Request) -> web.StreamResponse:
        target = broker if request.path.startswith(BROKER_PAGE_PREFIXES) else chat
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
    origin = chat if deployment == "own_port" else f"http://127.0.0.1:{proxy_port}"
    page_base = broker if deployment == "own_port" else origin

    stops: list[Callable[[], None]] = []
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate(), web_chat_origin=origin)
    stops.append(_serve_in_thread(service.build_app(), broker_port))
    preview = ThreadingHTTPServer(("127.0.0.1", chat_port), build_handler(DEFAULT_ASSET_ROOT))
    threading.Thread(target=preview.serve_forever, daemon=True).start()
    try:
        pages = [f"{page_base}/{_open_form(broker)}" for _ in range(3)]
        preview.preview_broker_pages = [  # type: ignore[attr-defined]
            preview_broker_page(pages[0], 0),
            preview_broker_page(pages[1], 1),
            preview_broker_page(pages[2], 2, seconds=EXPIRING_SECONDS),
        ]
        if deployment == "path_routed":
            stops.append(_serve_in_thread(_proxy_app(chat, broker), proxy_port))
        result = subprocess.run(
            ["rtk", "proxy", "node", str(RUNNER), json.dumps({"base": origin, "typed": TYPED})],
            capture_output=True, text=True, timeout=180, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        checked = json.loads(result.stdout.strip().splitlines()[-1])
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
        "submission from the frame reached the broker and the card shrank to Done",
        "frames posted only height and outcome",
        "an expired card lost its frame",
        "a thread switch removed the remaining frame",
        "sign-out removed every frame",
    ]
