"""Broker pages in their two views: the full page and the card Web Chat frames (A1).

Every page a chat can send a person to is opened through the broker's real
routes, then read once as the full page and once with ``?view=card``. The card
view may be framed only by the Web Chat's own address and is always sandboxed;
the full page may be framed by nobody. Both deployments are covered: the broker
on its own port (another origin than the chat) and the broker path-routed on
the chat's own address behind a reverse proxy (the same origin).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer, unused_port

from secure_tool_broker.auth_flows import get_auth_flow
from secure_tool_broker.defaults import CARD_FRAME_SANDBOX, card_view_url
from secure_tool_broker.model_key_page import model_key_reports, team_model_key_store
from secure_tool_broker.page_view import web_chat_origin
from secure_tool_broker.service import SecureToolBrokerService
from secure_tools.providers import ToolResult
from team_transfer.export_requests import EXPORT_REQUESTS_BY_SUPERVISOR, EXPORT_REQUESTS_ENV
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, gateway_headers
from tests.test_account_links import (  # noqa: F401 - pytest fixtures
    _isolated_credentials,
    link_request,
    world,
)

OWN_PORT_CHAT = "http://chat.example.test:8080"
SAME_ORIGIN = "same-origin"


def _context(roles: list[str] | None = None) -> dict:
    context: dict = {"canonical_user_id": "user-1", "principal_type": "user", "team_id": TEST_TEAM}
    if roles is not None:
        context["person_roles"] = roles
    return context


# A stand-in for ``gh auth login --web``: it prints the device code and waits,
# like the real CLI, so the GitHub browser sign-in page shows its waiting state.
FAKE_GH = """#!/bin/sh
echo "! First copy your one-time code: ABCD-1234"
echo "Open this URL to continue in your web browser: https://github.com/login/device"
exec sleep 30
"""


@pytest.fixture(autouse=True)
def _page_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setenv(EXPORT_REQUESTS_ENV, EXPORT_REQUESTS_BY_SUPERVISOR)
    team_model_key_store.cache_clear()
    model_key_reports.cache_clear()


async def _sign_in_page(client: TestClient, provider: str, method: str, roles: list[str] | None = None) -> str:
    created = await client.post(
        f"/v1/auth/{provider}/sessions",
        headers=gateway_headers(),
        json={"context": _context(roles), "method": method},
    )
    assert created.status == 200, await created.text()
    return "/" + (await created.json())["login_url"].split("/", 3)[3]


async def _collection_page(client: TestClient, flow: str = "") -> str:
    request: dict = {"credential_key": "portainer_token", "service_name": "Portainer"}
    if flow:
        request["flow"] = flow
    created = await client.post(
        "/v1/credential-collection/sessions",
        headers=gateway_headers(),
        json={"request": request, "context": _context()},
    )
    assert created.status == 200, await created.text()
    return "/" + (await created.json())["form_url"].split("/", 3)[3]


async def _account_link_page(client: TestClient) -> str:
    created = await client.post("/v1/account-link/sessions", headers=gateway_headers(), json=link_request())
    assert created.status == 200, await created.text()
    return "/" + (await created.json())["form_url"].split("/", 3)[3]


# Every page family the chat sends people to, by how the gateway opens it.
PAGE_FAMILIES = {
    "credential collection": lambda client: _collection_page(client),
    "credential deletion": lambda client: _collection_page(client, "credential_deletion"),
    "account link": _account_link_page,
    "GitHub browser sign-in": lambda client: _sign_in_page(client, "github", "web"),
    "GitHub token": lambda client: _sign_in_page(client, "github", "token"),
    "Google browser sign-in": lambda client: _sign_in_page(client, "google", "web"),
    "Google token import": lambda client: _sign_in_page(client, "google", "json_token"),
    "Google team sign-in app": lambda client: _sign_in_page(client, "google", "team_client", ["admin"]),
    "Bitwarden": lambda client: _sign_in_page(client, "bitwarden", "form"),
    "X sign-in": lambda client: _sign_in_page(client, "x", "web"),
    "X team sign-in app": lambda client: _sign_in_page(client, "x", "team_client", ["admin"]),
    "team model keys": lambda client: _sign_in_page(client, "model", "team", ["admin"]),
    "own model keys": lambda client: _sign_in_page(client, "model", "personal", ["member"]),
    "team export": lambda client: _sign_in_page(client, "team", "export", ["owner"]),
}


@pytest_asyncio.fixture(params=[OWN_PORT_CHAT, SAME_ORIGIN])
async def deployment(request, world):  # noqa: F811 - the fixture
    """A broker client and the Web Chat origin it was started with, for each deployment."""

    gate, _store = world
    port = unused_port()
    origin = f"http://127.0.0.1:{port}" if request.param == SAME_ORIGIN else request.param
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate, web_chat_origin=origin)
    client = TestClient(TestServer(service.build_app(), host="127.0.0.1", port=port))
    await client.start_server()
    yield client, origin
    await client.close()
    _stop_sign_in_processes(service)


def _stop_sign_in_processes(service: SecureToolBrokerService) -> None:
    """End the fake CLI processes the browser sign-in pages started."""

    for session in list(service._auth_sessions.values()):
        flow = get_auth_flow(session.provider, session.method)
        if flow is not None:
            flow.cleanup(session)


def _assert_full_page(response) -> None:
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def _assert_card_view(response, origin: str) -> None:
    assert response.headers["Content-Security-Policy"] == f"frame-ancestors {origin}; sandbox {CARD_FRAME_SANDBOX}"
    assert "allow-same-origin" not in response.headers["Content-Security-Policy"]
    assert "X-Frame-Options" not in response.headers
    assert response.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(PAGE_FAMILIES))
async def test_each_page_has_a_card_view_only_web_chat_may_frame(deployment, family) -> None:
    client, origin = deployment
    path = await PAGE_FAMILIES[family](client)

    full = await client.get(path)
    assert full.status < 500, await full.text()
    assert full.content_type == "text/html"
    _assert_full_page(full)

    card = await client.get(card_view_url(path))
    assert card.status == full.status, await card.text()
    assert card.content_type == "text/html"
    _assert_card_view(card, origin)


@pytest.mark.asyncio
async def test_without_a_web_chat_address_every_view_is_the_full_page(world) -> None:  # noqa: F811
    gate, _store = world
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as client:
        path = await _collection_page(client)
        _assert_full_page(await client.get(card_view_url(path)))


@pytest.mark.asyncio
async def test_a_page_error_is_a_styled_page_with_the_views_headers(deployment) -> None:
    client, origin = deployment
    for path in ("/credential-collection/unknown", "/account-link/unknown", "/auth/github/unknown"):
        full = await client.get(path)
        assert full.status == 404
        assert full.content_type == "text/html"
        assert "This page was not found or has expired." in await full.text()
        _assert_full_page(full)

        card = await client.get(card_view_url(path))
        assert card.status == 404
        _assert_card_view(card, origin)


@pytest.mark.asyncio
async def test_the_status_route_stays_plain_and_answers_the_card_with_its_errors(deployment) -> None:
    client, _origin = deployment
    path = await _sign_in_page(client, "github", "token")

    from_card = await client.get(f"{path}/status", headers={"Origin": "null"})
    assert from_card.status == 200
    assert from_card.headers["Access-Control-Allow-Origin"] == "null"
    assert "Access-Control-Allow-Credentials" not in from_card.headers
    assert (await from_card.json())["status"] == "pending"

    missing = await client.get("/auth/github/v1.unknown.link/status", headers={"Origin": "null"})
    assert missing.status == 404
    assert missing.headers["Access-Control-Allow-Origin"] == "null"
    assert missing.content_type == "text/plain"
    assert "<html" not in (await missing.text()).lower()

    other_site = await client.get(f"{path}/status", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in other_site.headers


def test_the_web_chat_origin_is_the_scheme_host_and_port_of_its_address() -> None:
    assert web_chat_origin("https://Assistant.Example.com/") == "https://assistant.example.com"
    assert web_chat_origin("http://127.0.0.1:18093") == "http://127.0.0.1:18093"
    assert web_chat_origin("http://[::1]:8080/webchat") == "http://[::1]:8080"
    assert web_chat_origin("") is None
    assert web_chat_origin("ftp://example.com") is None
    assert web_chat_origin("https://user:pass@example.com") is None


@pytest.mark.parametrize(
    "address",
    [
        "https://chat.example; sandbox allow-scripts allow-same-origin",
        "https://chat.example sandbox",
        "https://chat.example,https://evil.example",
        "https://chat.example:notaport",
        "https://chat.example:99999",
        "https://chat_example.test",
    ],
)
def test_an_address_that_could_change_the_policy_turns_the_card_view_off(address, caplog) -> None:
    """The origin goes into the Content-Security-Policy, so it is rebuilt from checked parts."""

    assert web_chat_origin(address) is None
    assert "event=broker_card_view_origin_unusable" in caplog.text


def test_the_card_view_address_adds_only_the_view() -> None:
    assert card_view_url("https://b.example/auth/github/v1.a.b") == "https://b.example/auth/github/v1.a.b?view=card"
    assert card_view_url("https://b.example/x?view=full&a=1") == "https://b.example/x?a=1&view=card"


# -- the card view's page ------------------------------------------------------------

_FORM_TAG = re.compile(r"<form\b[^>]*>", re.I)
_LINK_TAG = re.compile(r"<a\b[^>]*\bhref=\"(https?://[^\"]+)\"[^>]*>", re.I)


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(PAGE_FAMILIES))
async def test_a_card_is_the_bare_page_with_the_bridge_and_stays_a_card(deployment, family) -> None:
    """No header, mesh, countdown or Close; the bridge; every form and reload keeps ?view=card;
    links leave the frame for a new tab (vendor sites refuse to be framed)."""

    client, origin = deployment
    path = await PAGE_FAMILIES[family](client)
    card = await (await client.get(card_view_url(path))).text()

    assert f'data-card-parent-origin="{origin}"' in card
    assert '"ua-broker-card"' in card, "the bridge script is inlined"
    for absent in ('class="site-header"', "<canvas", "data-session-countdown", "window.close()"):
        assert absent not in card, absent
    assert _outcome(card) is None, "an open form has no outcome yet"
    for form in _FORM_TAG.findall(card):
        action = re.search(r'\baction="([^"]*)"', form)
        assert action is None or action.group(1).startswith("#"), f"{family}: {form} drops the query"
    assert not re.search(r"location\.replace\(window\.location\.pathname\)", card)
    broker = str(client.make_url("/")).rstrip("/")
    for match in _LINK_TAG.finditer(card):
        if not match.group(1).startswith(broker):
            assert 'target="_blank"' in match.group(0), f"{family}: {match.group(0)} would navigate the frame"

    full = await (await client.get(path)).text()
    assert 'class="site-header"' in full
    assert '"ua-broker-card"' not in full and "data-card-parent-origin" not in full


def _outcome(page: str) -> str | None:
    found = re.search(r'data-card-outcome="([a-z]+)"', page)
    return found.group(1) if found else None


@pytest.mark.asyncio
async def test_a_stored_credential_reports_finished_and_a_failure_reports_nothing(deployment, monkeypatch) -> None:
    client, _origin = deployment
    results = iter(
        [
            ToolResult(status="error", error="The store refused it.", content={"status": "credential_unavailable"}),
            ToolResult(status="success", content={"status": "credential_stored"}),
        ]
    )
    monkeypatch.setattr("secure_tool_broker.service.collect_credential", lambda *args, **kwargs: next(results))

    failed_path = await _collection_page(client)
    failed = await client.post(card_view_url(failed_path), data={"field__username": "me", "field__password": "pw"})
    assert failed.status == 400
    assert _outcome(await failed.text()) is None

    path = await _collection_page(client)
    stored = await client.post(card_view_url(path), data={"field__username": "me", "field__password": "pw"})
    assert _outcome(await stored.text()) == "finished"
    assert _outcome(await (await client.get(card_view_url(path))).text()) == "finished"
    assert _outcome(await (await client.get(path)).text()) is None, "the full page has no outcome"


@pytest.mark.asyncio
async def test_an_account_link_refusal_stays_readable_and_an_unconfirmed_form_reports_declined(deployment) -> None:
    client, _origin = deployment
    refused_path = await _account_link_page(client)
    refused = await client.post(card_view_url(refused_path), data={"credential_key": "not_stored", "confirm": "yes"})
    assert "Account link unavailable" in await refused.text()
    assert _outcome(await refused.text()) is None
    assert _outcome(await (await client.get(card_view_url(refused_path))).text()) is None

    second = link_request(request_id="req-2")
    created = await client.post("/v1/account-link/sessions", headers=gateway_headers(), json=second)
    path = "/" + (await created.json())["form_url"].split("/", 3)[3]
    declined = await client.post(card_view_url(path), data={"credential_key": "portainer_token"})
    assert _outcome(await declined.text()) == "declined"
    assert _outcome(await (await client.get(card_view_url(path))).text()) == "declined"


@pytest.mark.asyncio
async def test_a_sign_in_reports_finished_only_when_it_succeeded(deployment, monkeypatch) -> None:
    client, _origin = deployment
    results = iter([ToolResult(status="error", error="Bad token."), ToolResult(status="success", content={})])
    monkeypatch.setattr("secure_tool_broker.service.login_github_with_token", lambda *args, **kwargs: next(results))

    failed = await client.post(card_view_url(await _sign_in_page(client, "github", "token")), data={"token": "x"})
    assert failed.status == 400
    assert _outcome(await failed.text()) is None

    signed_in = await client.post(card_view_url(await _sign_in_page(client, "github", "token")), data={"token": "x"})
    assert _outcome(await signed_in.text()) == "finished"


@pytest.mark.asyncio
async def test_closing_the_long_lived_pages_reports_their_end(deployment) -> None:
    client, _origin = deployment
    keys = card_view_url(await _sign_in_page(client, "model", "team", ["admin"]))
    closed_keys = await (await client.post(keys, data={"form_id": "page-top", "action": "close"})).text()
    assert _outcome(closed_keys) == "finished"
    assert "window.close()" not in closed_keys

    export = card_view_url(await _sign_in_page(client, "team", "export", ["owner"]))
    closed_export = await (await client.post(export, data={"action": "close"})).text()
    assert _outcome(closed_export) == "declined", "closed before any export ran"


@pytest.mark.asyncio
async def test_an_error_page_in_a_card_has_no_outcome(deployment) -> None:
    client, _origin = deployment
    missing = await client.get(card_view_url("/credential-collection/unknown"))
    assert missing.status == 404
    assert _outcome(await missing.text()) is None
