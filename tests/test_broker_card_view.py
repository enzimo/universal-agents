"""Broker pages in their two views: the full page and the card Web Chat frames (A1).

Every page a chat can send a person to is opened through the broker's real
routes, then read once as the full page and once with ``?view=card``. The card
view may be framed only by the Web Chat's own address and is always sandboxed;
the full page may be framed by nobody. Both deployments are covered: the broker
on its own port (another origin than the chat) and the broker path-routed on
the chat's own address behind a reverse proxy (the same origin).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer, unused_port

from secure_tool_broker.defaults import CARD_FRAME_SANDBOX, card_view_url
from secure_tool_broker.model_key_page import model_key_reports, team_model_key_store
from secure_tool_broker.page_view import web_chat_origin
from secure_tool_broker.service import SecureToolBrokerService
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


@pytest.fixture(autouse=True)
def _page_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    "GitHub token": lambda client: _sign_in_page(client, "github", "token"),
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
    app = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate, web_chat_origin=origin).build_app()
    client = TestClient(TestServer(app, host="127.0.0.1", port=port))
    await client.start_server()
    yield client, origin
    await client.close()


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
    assert web_chat_origin("") is None
    assert web_chat_origin("ftp://example.com") is None
    assert web_chat_origin("https://user:pass@example.com") is None


def test_the_card_view_address_adds_only_the_view() -> None:
    assert card_view_url("https://b.example/auth/github/v1.a.b") == "https://b.example/auth/github/v1.a.b?view=card"
    assert card_view_url("https://b.example/x?view=full&a=1") == "https://b.example/x?a=1&view=card"
