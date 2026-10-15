"""A person's own X sign-in, end to end, against a fake X OAuth 2.0 server.

The fake server speaks X's authorization code flow with PKCE: it checks the
authorize request (client id, callback address, scopes, S256 challenge),
redirects the browser back with a code, checks the code verifier and the app's
authentication at the token endpoint, and answers ``/2/users/me``. The broker
is the real HTTP service with the real file-backed credential store, and a
small fake ``xurl`` stands in for the CLI. Covered:

* only an administrator stores the team's X app, checked when the page opens
  and again when it is submitted;
* the full browser flow for a person, finished by the broker's callback page
  or by pasting the address, stores the sign-in under that person only;
* the next X call for that person uses the sign-in and saves the refreshed
  tokens; a person without a sign-in falls back to the team's account link,
  and with neither is told how to sign in;
* a callback that does not belong to the sign-in changes nothing;
* the broker's gate gives X only the team's link, and person links for X are
  refused in favour of the sign-in.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from access_catalog.catalog import CatalogSource
from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller
from custom_messages.contracts.access_v2 import AccountLinkRequestV1, AccountSharing, LinkOwnerKind
from secure_tool_broker import hub_cli_executor, x_sign_in
from secure_tool_broker.account_links import (
    REASON_TEAM_ACCOUNT_PRIVATE,
    REASON_USES_SIGN_IN,
    AccountLinkRefused,
    linkable_tool,
)
from secure_tool_broker.auth_flows import BrokerAuthSession
from secure_tool_broker.credential_state import broker_credential_file, credential_owner_context
from secure_tool_broker.executors import execute_validated_action, get_auth_status
from secure_tool_broker.service import SecureToolBrokerService
from secure_tools.basic_providers import register_builtin_providers
from secure_tools.providers import ToolContext
from secure_tools.registry import get_provider
from tests.broker_access_support import (
    BROKER_ROOT,
    TEST_TEAM,
    access_store,
    account_link,
    agent_context,
    choose_sharing,
    gateway_headers,
    linked_gate,
    open_gate,
    store_account_link,
    team_sharing,
)

ALICE = "alice"
BOB = "bob"
CLIENT_ID = "x-app-client"
CLIENT_SECRET = "x-app-secret-canary"
ACCESS_TOKEN = "x-access-canary"
REFRESH_TOKEN = "x-refresh-canary"

FAKE_XURL = r'''
import json, os, sys
from pathlib import Path
import yaml

home = Path(os.environ["HOME"])
path = home / ".xurl/auth.yml"
data = yaml.safe_load(path.read_text())
app = data["apps"]["ua"]
user = app["default_user"]
token = app["oauth2_tokens"][user]["oauth2"]
with Path(__file__).with_name("xurl-calls.jsonl").open("a") as log:
    log.write(json.dumps({"args": sys.argv[1:], "client_id": app["client_id"],
                          "confidential": bool(app["client_secret"]), "user": user}) + "\n")
if "token" in sys.argv:
    # A refresh: X rotates both tokens.
    token["access_token"] += "-refreshed"
    token["refresh_token"] += "-refreshed"
    path.write_text(yaml.safe_dump(data))
    print(token["access_token"])
elif "/2/users/me" in sys.argv:
    print(json.dumps({"data": {"id": "4242", "username": user}}))
else:
    print(json.dumps({"data": {"id": "777"}}))
'''


class FakeX:
    """X's authorize, token and users/me endpoints for one app."""

    def __init__(self, *, client_secret: str = "") -> None:
        self.client_secret = client_secret
        self.codes: dict[str, dict[str, str]] = {}
        self.token_requests: list[dict[str, str]] = []

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/i/oauth2/authorize", self._authorize)
        app.router.add_post("/2/oauth2/token", self._token)
        app.router.add_get("/2/users/me", self._me)
        return app

    async def _authorize(self, request: web.Request) -> web.Response:
        query = request.query
        assert query["response_type"] == "code"
        assert query["client_id"] == CLIENT_ID
        assert query["code_challenge_method"] == "S256"
        assert set(query["scope"].split()) == {"tweet.read", "tweet.write", "users.read", "offline.access"}
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = {"challenge": query["code_challenge"], "redirect_uri": query["redirect_uri"]}
        # The person approved: X sends the browser to the app's callback address.
        target = f"{query['redirect_uri']}?{urlencode({'state': query['state'], 'code': code})}"
        raise web.HTTPFound(target)

    async def _token(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        self.token_requests.append(form)
        if self.client_secret:
            expected = base64.b64encode(f"{CLIENT_ID}:{self.client_secret}".encode()).decode()
            if request.headers.get("Authorization") != f"Basic {expected}":
                return web.json_response({"error": "unauthorized_client"}, status=401)
        issued = self.codes.pop(str(form.get("code")), None)
        verifier = str(form.get("code_verifier") or "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if (
            form.get("grant_type") != "authorization_code"
            or issued is None
            or issued["challenge"] != challenge
            or issued["redirect_uri"] != form.get("redirect_uri")
            or form.get("client_id") != CLIENT_ID
        ):
            return web.json_response({"error": "invalid_request", "error_description": "Value passed was invalid"}, status=400)
        return web.json_response(
            {"token_type": "bearer", "expires_in": 7200, "access_token": ACCESS_TOKEN,
             "refresh_token": REFRESH_TOKEN, "scope": "tweet.read tweet.write users.read offline.access"}
        )

    async def _me(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
            return web.json_response({"title": "Unauthorized"}, status=401)
        return web.json_response({"data": {"id": "4242", "name": "Alice", "username": "alice_x"}})


@pytest.fixture
def broker_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """File-backed credential store and a fake xurl; returns xurl's call log."""

    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path / "broker_credentials"))
    program = tmp_path / "xurl"
    program.write_text(f"#!{sys.executable}\n{FAKE_XURL}", encoding="utf-8")
    program.chmod(0o755)
    monkeypatch.setattr(hub_cli_executor.shutil, "which", lambda name: str(program))
    register_builtin_providers()
    return tmp_path / "xurl-calls.jsonl"


async def _fake_x(monkeypatch: pytest.MonkeyPatch, fake: FakeX) -> TestServer:
    server = TestServer(fake.build_app())
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    monkeypatch.setattr(x_sign_in, "AUTHORIZE_URL", f"{base}/i/oauth2/authorize")
    monkeypatch.setattr(x_sign_in, "TOKEN_URL", f"{base}/2/oauth2/token")
    monkeypatch.setattr(x_sign_in, "API_BASE_URL", base)
    return server


def _context(person: str, roles: list[str] | None = None) -> dict:
    context = {"canonical_user_id": person, "principal_type": "user", "team_id": TEST_TEAM}
    if roles is not None:
        context["person_roles"] = roles
    return context


def _page_path(body: dict) -> str:
    return "/" + body["login_url"].split("/", 3)[3]


async def _store_team_app(client: TestClient, *, redirect_uri: str, secret: str = "") -> None:
    created = await client.post(
        "/v1/auth/x/sessions",
        headers=gateway_headers(),
        json={"context": _context("admin-1", ["admin"]), "method": "team_client"},
    )
    assert created.status == 200, await created.text()
    page = _page_path(await created.json())
    form = await client.get(page)
    assert "Set the team's X sign-in app." in await form.text()
    stored = await client.post(page, data={"client_id": CLIENT_ID, "client_secret": secret, "redirect_uri": redirect_uri})
    assert "The team's X sign-in app is stored." in await stored.text()


async def _open_sign_in(client: TestClient, person: str) -> tuple[str, str]:
    """Open the person's sign-in page; return its path and the X authorize address on it."""

    created = await client.post(
        "/v1/auth/x/sessions", headers=gateway_headers(), json={"context": _context(person), "method": "web"}
    )
    assert created.status == 200, await created.text()
    page = _page_path(await created.json())
    html_text = await (await client.get(page)).text()
    assert "Sign in to X" in html_text
    authorize = html_text.split('data-auth-url="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    return page, authorize


async def _approve_on_x(authorize_url: str) -> str:
    """The person approves on X; return the address X sends the browser to."""

    async with aiohttp.ClientSession() as browser:
        async with browser.get(authorize_url, allow_redirects=False) as response:
            assert response.status == 302
            return response.headers["Location"]


def _stored_sign_in(person: str) -> dict | None:
    stored = x_sign_in.person_sign_in(person)
    assert not hasattr(stored, "status"), stored
    return json.loads(stored) if stored else None


def _x_call(person: str, action: str = "auth.status", link=None, **params):
    validated = get_provider("xurl").validate(action, params)
    return execute_validated_action(
        validated, ToolContext(canonical_user_id=person, principal_type="user", account_link=link)
    )


def _saved_sessions(person: str, *, scope_kind: str = "user") -> dict:
    with credential_owner_context(person, scope_kind=scope_kind):
        return json.loads(broker_credential_file("xurl", "sessions.json").read_text())


# -- the team's X app -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("roles", "allowed"), [(["member"], False), (["admin"], True), (["owner"], True)])
async def test_only_an_administrator_opens_the_team_app_page(broker_state: Path, roles: list[str], allowed: bool) -> None:
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        created = await client.post(
            "/v1/auth/x/sessions",
            headers=gateway_headers(),
            json={"context": _context(ALICE, roles), "method": "team_client"},
        )
        body = json.loads(await created.text())
    if allowed:
        assert created.status == 200
        return
    assert created.status == 403
    assert body["reason_code"] == "x_team_client_admin_required"
    assert body["error"] == "Only a team administrator or owner can change the team's X sign-in app."
    assert x_sign_in.team_client(TEST_TEAM) is None


def test_the_team_app_is_refused_at_submission_without_an_administrator(broker_state: Path, caplog) -> None:
    from datetime import datetime, timedelta, timezone

    flow = x_sign_in.XTeamClientFlow()
    now = datetime.now(timezone.utc)
    session = BrokerAuthSession(
        provider="x", method="team_client", created_at=now, expires_at=now + timedelta(minutes=5),
        owner_canonical_user_id=ALICE, owner_team_id=TEST_TEAM, owner_roles=("member",),
    )
    flow.submit(session, {"client_id": CLIENT_ID, "redirect_uri": "http://localhost:8080/callback"}, x_sign_in.LOGGER)

    assert session.state["status"] == "error"
    assert x_sign_in.team_client(TEST_TEAM) is None
    assert "event=team_credential_admin_required" in caplog.text


@pytest.mark.parametrize(
    ("redirect_uri", "problem"),
    [
        ("http://broker.example.com/auth/x/callback", "must use https"),
        ("broker.example.com/callback", "full web address"),
        ("https://broker.example.com/cb#part", "# part"),
    ],
)
def test_an_unusable_team_app_is_not_stored(broker_state: Path, redirect_uri: str, problem: str) -> None:
    client = x_sign_in.XClient(client_id=CLIENT_ID, redirect_uri=redirect_uri)
    result = x_sign_in.store_team_client(TEST_TEAM, client, stored_by="admin-1")
    assert result.status == "error" and problem in str(result.error)
    assert x_sign_in.team_client(TEST_TEAM) is None


# -- a person's sign-in --------------------------------------------------------------


@pytest_asyncio.fixture
async def running(broker_state: Path, monkeypatch: pytest.MonkeyPatch):
    """The broker and a fake X with a public app (no client secret)."""

    fake = FakeX()
    x_server = await _fake_x(monkeypatch, fake)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        yield SimpleNamespace(client=client, fake=fake, calls=broker_state)
    await x_server.close()


@pytest.mark.asyncio
async def test_a_person_signs_in_through_the_callback_page_and_the_next_x_call_uses_it(running, caplog) -> None:
    caplog.set_level(logging.INFO)
    client: TestClient = running.client
    await _store_team_app(client, redirect_uri=str(client.make_url(x_sign_in.CALLBACK_PATH)))
    page, authorize = await _open_sign_in(client, ALICE)
    assert parse_qs(urlparse(authorize).query)["redirect_uri"] == [str(client.make_url(x_sign_in.CALLBACK_PATH))]

    back = await _approve_on_x(authorize)
    finished = await client.get("/" + back.split("/", 3)[3])
    text = await finished.text()

    assert finished.status == 200, text
    assert "You are signed in to X." in text and "@alice_x" in text
    record = _stored_sign_in(ALICE)
    assert record == {
        "auth_type": "oauth2",
        "client_id": CLIENT_ID,
        "client_secret": "",
        "username": "alice_x",
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "expiration_time": record["expiration_time"],
    }
    assert record["expiration_time"] > 0
    assert _stored_sign_in(BOB) is None
    # A public app: the client id goes in the body, never HTTP Basic.
    assert running.fake.token_requests[0]["client_id"] == CLIENT_ID
    # The page's status now says the sign-in finished.
    status = await (await client.get(f"{page}/status")).json()
    assert status["status"] == "success"
    with credential_owner_context(ALICE):
        assert get_auth_status("x", team_id=TEST_TEAM).status == "signed_in"

    # The next X call for Alice uses her sign-in and saves X's refreshed tokens.
    first = _x_call(ALICE)
    assert first.status == "success", first
    assert first.content["username"] == "alice_x"
    saved = _saved_sessions(ALICE)[x_sign_in.X_SIGN_IN_SESSION_KEY]["store"]["apps"]["ua"]
    tokens = saved["oauth2_tokens"]["alice_x"]["oauth2"]
    assert tokens["access_token"] == ACCESS_TOKEN + "-refreshed"
    assert tokens["refresh_token"] == REFRESH_TOKEN + "-refreshed"
    # The following call starts from the saved refresh, not the original sign-in.
    assert _x_call(ALICE, "post.create", text="hello").content["post_id"] == "777"
    tokens = _saved_sessions(ALICE)[x_sign_in.X_SIGN_IN_SESSION_KEY]["store"]["apps"]["ua"]["oauth2_tokens"]
    assert tokens["alice_x"]["oauth2"]["refresh_token"] == REFRESH_TOKEN + "-refreshed-refreshed"
    calls = [json.loads(line) for line in running.calls.read_text().splitlines()]
    assert {call["client_id"] for call in calls} == {CLIENT_ID}
    assert not any(call["confidential"] for call in calls)
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in caplog.text
    assert "event=x_sign_in_stored" in caplog.text


@pytest.mark.asyncio
async def test_a_person_signs_in_by_pasting_the_address_with_a_confidential_app(
    broker_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeX(client_secret=CLIENT_SECRET)
    x_server = await _fake_x(monkeypatch, fake)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    try:
        async with TestClient(TestServer(service.build_app())) as client:
            await _store_team_app(client, redirect_uri="http://localhost:8080/callback", secret=CLIENT_SECRET)
            page, authorize = await _open_sign_in(client, BOB)
            address = await _approve_on_x(authorize)
            assert address.startswith("http://localhost:8080/callback?")

            pasted = await client.post(page, data={"redirect_url": address})
            text = await pasted.text()
    finally:
        await x_server.close()

    assert "You are signed in to X." in text
    record = _stored_sign_in(BOB)
    assert record["client_secret"] == CLIENT_SECRET and record["username"] == "alice_x"
    assert _stored_sign_in(ALICE) is None
    assert CLIENT_SECRET not in text


@pytest.mark.asyncio
async def test_a_callback_that_is_not_this_sign_in_changes_nothing(running) -> None:
    client: TestClient = running.client
    await _store_team_app(client, redirect_uri="http://localhost:8080/callback")
    page, authorize = await _open_sign_in(client, ALICE)
    address = await _approve_on_x(authorize)
    forged = address.replace("state=", "state=forged")

    unmatched = await client.get(x_sign_in.CALLBACK_PATH + "?" + urlparse(forged).query)
    pasted = await client.post(page, data={"redirect_url": forged})

    assert unmatched.status == 404
    assert "This X sign-in is not open." in await unmatched.text()
    assert "does not belong to this sign-in" in await pasted.text()
    assert running.fake.token_requests == []
    assert _stored_sign_in(ALICE) is None


@pytest.mark.asyncio
async def test_a_denied_approval_is_explained_and_stores_nothing(running) -> None:
    client: TestClient = running.client
    await _store_team_app(client, redirect_uri="http://localhost:8080/callback")
    page, authorize = await _open_sign_in(client, ALICE)
    state = parse_qs(urlparse(authorize).query)["state"][0]

    denied = await client.post(page, data={"redirect_url": f"http://localhost:8080/callback?state={state}&error=access_denied"})

    assert "X did not approve the sign-in (access_denied)" in await denied.text()
    assert _stored_sign_in(ALICE) is None


@pytest.mark.asyncio
async def test_without_the_team_app_the_page_says_who_sets_it_up(running) -> None:
    client: TestClient = running.client
    created = await client.post(
        "/v1/auth/x/sessions", headers=gateway_headers(), json={"context": _context(ALICE), "method": "web"}
    )
    text = await (await client.get(_page_path(await created.json()))).text()

    assert "No X sign-in app is set up for this team yet." in text
    assert "/auth x team_client" in text
    status = await client.post("/v1/auth/status/x", headers=gateway_headers(), json={"context": _context(ALICE)})
    assert (await status.json())["status"] == "auth_required"


# -- which X account a call uses ---------------------------------------------------------


def test_without_a_sign_in_x_uses_the_teams_link_and_otherwise_says_how_to_sign_in(
    broker_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team_account = json.dumps(
        {"client_id": "team-app", "client_secret": "", "username": "team_x", "access_token": "team-access",
         "refresh_token": "team-refresh", "expiration_time": 4000000000}
    )
    monkeypatch.setattr(
        hub_cli_executor,
        "resolve_secret_field",
        lambda key, field, scope: SimpleNamespace(secret=True, value=team_account),
    )
    team_link = account_link("cli:x", "x-team", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)

    with_team = _x_call(BOB, link=team_link)
    neither = _x_call(BOB)

    assert with_team.status == "success" and with_team.content["username"] == "team_x"
    assert "x-team" in _saved_sessions(TEST_TEAM, scope_kind="team")
    assert neither.status == "error"
    assert neither.content["error_code"] == "x_account_missing"
    assert "/link cli:x" in str(neither.error) and "/link cli:x team" in str(neither.error)


def test_the_gate_gives_x_only_the_teams_link_once_the_team_shares_x(tmp_path: Path) -> None:
    links = access_store(tmp_path / "access.db")
    store_account_link(links, "cli:x", "x-alice", owner_id=ALICE)
    team = store_account_link(links, "cli:x", "x-team", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)
    gate = linked_gate(links)

    # X posts as the account, so it starts private: the team's account serves nobody.
    private = gate.check_broker_action(BrokerCaller(CALLER_MANAGER), "xurl", "auth.status", {}, agent_context(ALICE))
    choose_sharing(links, "cli:x", AccountSharing.SHARED)
    shared = gate.check_broker_action(BrokerCaller(CALLER_MANAGER), "xurl", "auth.status", {}, agent_context(ALICE))

    assert private.allowed and private.account_link is None
    # Alice's own link is not used: her own X account is her sign-in (the executor reads it).
    assert shared.allowed
    assert shared.account_link == team


def test_the_gate_lets_x_through_without_any_link(tmp_path: Path) -> None:
    gate = linked_gate(access_store(tmp_path / "access.db"))
    result = gate.check_broker_action(BrokerCaller(CALLER_MANAGER), "xurl", "auth.status", {}, agent_context(ALICE))
    assert result.allowed and result.account_link is None


@pytest.mark.parametrize(
    ("tool_id", "owner", "expected"),
    [
        ("cli:x", LinkOwnerKind.PERSON, "X uses each person's own sign-in, not a linked account. Sign in with /link cli:x."),
        ("cli:github", LinkOwnerKind.TEAM, "GitHub sign-ins are personal"),
        ("cli:google", LinkOwnerKind.PERSON, "Sign in with /link cli:google."),
    ],
)
def test_links_for_sign_in_tools_are_refused_with_the_sign_in_command(
    tool_id: str, owner: LinkOwnerKind, expected: str, tmp_path: Path
) -> None:
    request = AccountLinkRequestV1(
        request_id="req-1", team_id=TEST_TEAM, tool_id=tool_id, owner_kind=owner,
        person_id=ALICE, person_roles=("owner",), reason="test",
    )
    sharing = team_sharing(access_store(tmp_path / "access.db"))
    with pytest.raises(AccountLinkRefused) as refused:
        linkable_tool(CatalogSource(None).current(), request, sharing)
    assert refused.value.reason_code == REASON_USES_SIGN_IN
    assert expected in str(refused.value)


def test_the_teams_x_account_is_linked_only_while_the_team_shares_x(tmp_path: Path) -> None:
    links = access_store(tmp_path / "access.db")
    request = AccountLinkRequestV1(
        request_id="req-2", team_id=TEST_TEAM, tool_id="cli:x", owner_kind=LinkOwnerKind.TEAM,
        person_id=ALICE, person_roles=("admin",), reason="test",
    )
    catalog = CatalogSource(None).current()

    with pytest.raises(AccountLinkRefused) as refused:
        linkable_tool(catalog, request, team_sharing(links))
    choose_sharing(links, "cli:x", AccountSharing.SHARED)

    assert refused.value.reason_code == REASON_TEAM_ACCOUNT_PRIVATE
    assert "/accounts share cli:x" in str(refused.value)
    assert linkable_tool(catalog, request, team_sharing(links)).tool_id == "cli:x"
