"""Gateway account links: the /link command, its result messages, and the broker client.

Agent tasks ask for account links (and sign-ins) with ``ToolSetupRequestV1``;
``tests/test_gateway_tool_setup.py`` covers those with the fake broker below.

The fake broker below mirrors the secure broker's real account link routes
(``secure_tool_broker/service.py``): the same JSON shapes, a JSON refusal body
sent as plain text with HTTP 403, and the same admin rule for team links. The
gateway talks to it through the real ``HttpGatewaySecureBroker``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from assistant_gateway.application.account_links import (
    AccountLinkForm,
    AccountLinkUnavailable,
    GatewayAccountLinks,
)
from assistant_gateway.application.link_results import GatewayLinkResults, LinkResultTarget
from assistant_gateway.application.interactive_commands import GatewayCommandRequest
from assistant_gateway.application.ports import GatewaySecureBrokerRefusal
from assistant_gateway.domain.message_types import LINK_RESULT_MESSAGE_TYPE
from assistant_gateway.gateway import UserGateway
from assistant_gateway.http_secure_broker import HttpGatewaySecureBroker
from assistant_gateway.runtime_ports import SystemGatewayClock
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.access_v2 import AccountLinkRequestV1, LinkOwnerKind
from access_control.caller_tokens import CALLER_GATEWAY
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.service import SecureToolBrokerService
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, caller_token
from tests.test_account_links import (  # noqa: F401 - pytest fixtures
    _isolated_credentials,
    save_credentials,
    token_credential,
    world,
)
from tests.test_gateway_phase1 import _build_test_config, _CaptureGatewayManagerTransport


BROKER_TOKEN = "test-gateway-caller-token"
PUBLIC_BROKER_BASE = "https://agents.example.test:18600"
ADMIN_ROLES = {"admin", "owner"}


class FakeAccountLinkBroker:
    """The broker's account link routes, with a two-tool catalog."""

    # tool id -> display name; None marks a tool that needs no account.
    TOOLS = {"http:portainer": "Portainer", "http:public_weather": None, "cli:x": "X (xurl)"}

    def __init__(self) -> None:
        # How long a new sign-in page stays open (a negative value opens an expired one).
        self.sign_in_expires_in = timedelta(minutes=10)
        self.requests: list[dict[str, object]] = []
        # Sign-in pages opened with /v1/auth/<provider>/sessions (provider, body).
        self.sign_ins: list[tuple[str, dict[str, object]]] = []
        self.sessions: dict[str, dict[str, object]] = {}
        self.status_reads = 0
        # Sign-in page status by provider, as the broker's page status route reports it.
        self.sign_in_status: dict[str, dict[str, object]] = {}
        # Authorization headers seen on sign-in status reads (the route is public).
        self.sign_in_status_auth: list[str | None] = []

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/account-link/sessions", self._create)
        app.router.add_get("/v1/account-link/sessions/{session_id}", self._status)
        app.router.add_post("/v1/auth/{provider}/sessions", self._sign_in)
        app.router.add_get("/auth/{provider}/{session_id}/status", self._sign_in_page_status)
        return app

    async def _sign_in_page_status(self, request: web.Request) -> web.Response:
        self.sign_in_status_auth.append(request.headers.get("Authorization"))
        provider = request.match_info["provider"]
        return web.json_response(self.sign_in_status.get(provider, {"status": "pending"}))

    def decline(self, session_id: str) -> None:
        self.sessions[session_id].update(
            status="account_link_declined",
            result={
                "status": "account_link_declined",
                "message": "The person did not confirm the account link.",
            },
        )

    def refuse(self, session_id: str, *, error: str, reason_code: str) -> None:
        self.sessions[session_id].update(
            status="account_link_unavailable",
            result={"status": "account_link_unavailable", "error": error, "reason_code": reason_code},
        )

    def drop(self, session_id: str) -> None:
        # The broker restarted or pruned the form: its status route answers 404.
        del self.sessions[session_id]

    async def _sign_in(self, request: web.Request) -> web.Response:
        self._authorized(request)
        provider = request.match_info["provider"]
        payload = await request.json()
        self.sign_ins.append((provider, payload))
        expires_at = (datetime.now(timezone.utc) + self.sign_in_expires_in).isoformat()
        base_url = f"{request.scheme}://{request.host}"
        return web.json_response(
            {
                "status": "created",
                "provider": provider,
                "method": payload.get("method"),
                "login_url": f"{base_url}/auth/{provider}/v1.signed_session",
                "expires_at": expires_at,
            }
        )

    def complete(self, session_id: str, *, link_id: str = "account_link_1") -> None:
        request = self.sessions[session_id]["request"]
        assert isinstance(request, dict)
        self.sessions[session_id].update(
            status="account_link_created",
            result={
                "status": "account_link_created",
                "link_id": link_id,
                "tool_id": request["tool_id"],
                "owner_kind": request["owner_kind"],
                "message": "Account linked; no credential value was returned.",
            },
        )

    def _authorized(self, request: web.Request) -> None:
        if request.headers.get("Authorization") != f"Bearer {BROKER_TOKEN}":
            raise web.HTTPUnauthorized(text="Missing or invalid broker token.")

    @staticmethod
    def _refuse(message: str, reason_code: str) -> web.HTTPForbidden:
        # The real broker sends this JSON as text/plain.
        return web.HTTPForbidden(
            text=json.dumps({"error": message, "reason_code": reason_code})
        )

    async def _create(self, request: web.Request) -> web.Response:
        self._authorized(request)
        payload = await request.json()
        self.requests.append(payload)
        tool_id = payload["tool_id"]
        if tool_id not in self.TOOLS:
            raise self._refuse(
                f"{tool_id} is not in the tool catalog.", "account_link_unknown_tool"
            )
        if self.TOOLS[tool_id] is None:
            raise self._refuse(
                "Public Weather does not use a linked account.",
                "account_link_not_needed",
            )
        if payload["owner_kind"] == "team" and not ADMIN_ROLES & set(
            payload["person_roles"]
        ):
            raise self._refuse(
                "Only a team administrator or owner can link the team's account.",
                "account_link_team_admin_required",
            )
        session_id = f"link_session_{len(self.sessions) + 1}"
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        self.sessions[session_id] = {
            "request": payload,
            "status": "pending",
            "result": None,
            "expires_at": expires_at,
        }
        base_url = f"{request.scheme}://{request.host}"
        return web.json_response(
            {
                "status": "pending",
                "session_id": session_id,
                "tool_display_name": self.TOOLS[tool_id],
                "form_url": f"{base_url}/account-link/{session_id}",
                "status_url": f"{base_url}/v1/account-link/sessions/{session_id}",
                "expires_at": expires_at,
                "message": "Open the link to choose which stored credential this tool uses.",
            }
        )

    async def _status(self, request: web.Request) -> web.Response:
        self._authorized(request)
        self.status_reads += 1
        session_id = request.match_info["session_id"]
        session = self.sessions.get(session_id)
        if session is None:
            raise web.HTTPNotFound(text="Account link form not found or expired.")
        return web.json_response(
            {
                "session_id": session_id,
                "status": session["status"],
                "result": session["result"],
                "expires_at": session["expires_at"],
            }
        )


@pytest_asyncio.fixture
async def fake_broker():
    broker = FakeAccountLinkBroker()
    server = TestServer(broker.build_app())
    await server.start_server()
    client = HttpGatewaySecureBroker(
        internal_base_url=str(server.make_url("")).rstrip("/"),
        public_base_url=PUBLIC_BROKER_BASE,
        token=BROKER_TOKEN,
    )
    yield broker, client
    await server.close()


@pytest_asyncio.fixture
async def linked_gateway(tmp_path: Path, fake_broker):
    broker, client = fake_broker
    config = _build_test_config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=store,
        manager_transport=transport,
        secure_broker=client,
    )
    # Poll the fake broker quickly instead of every two seconds.
    gateway._credential_workflow._settings = replace(
        gateway._credential_workflow._settings, poll_interval_seconds=0.01
    )
    yield gateway, broker, transport
    await gateway._credential_workflow.close()


def _member(gateway: UserGateway, *, role: str = "member") -> str:
    user = gateway.state_store.create_gateway_user(
        display_name="Mia Member", timezone="UTC"
    )
    gateway.state_store.create_team_membership(
        canonical_user_id=user.canonical_user_id,
        team_id=gateway.config.manager.team_id,
        role=role,
    )
    return user.canonical_user_id


def _waiting_task(gateway: UserGateway, *, owner: str) -> None:
    gateway.state_store.create_session(
        session_key="session_link",
        conversation_id="conversation_link",
        manager_agent_id=gateway.config.manager.manager_agent_id,
        channel_name="cli",
        user_id=owner,
        chat_id=f"cli:{gateway.config.instance_name}",
        owner_canonical_user_id=owner,
    )
    gateway.state_store.record_request(
        correlation_id="correlation_link",
        session_key="session_link",
        request_kind="interactive",
        request_text="list my containers",
        conversation_id="conversation_link",
        followup_to_task_id=None,
        status="pending",
        owner_canonical_user_id=owner,
    )


# The broker client ---------------------------------------------------------


@pytest.mark.asyncio
async def test_broker_client_posts_the_account_link_request_and_reads_status(
    fake_broker,
) -> None:
    broker, client = fake_broker
    request = AccountLinkRequestV1(
        request_id="acct_link_req_client",
        team_id="team_test",
        tool_id="http:portainer",
        owner_kind=LinkOwnerKind.PERSON,
        person_id="usr_alice",
        person_roles=("member",),
        reason="Read the container list.",
    )

    created = await client.create_account_link_session(request)
    status = await client.get_account_link_status(
        status_url=created["status_url"], session_id=created["session_id"]
    )

    assert broker.requests == [request.model_dump(mode="json")]
    assert created["session_id"] == "link_session_1"
    assert status == {
        "session_id": "link_session_1",
        "status": "pending",
        "result": None,
        "expires_at": created["expires_at"],
    }
    assert client.user_form_url(created["form_url"]) == (
        f"{PUBLIC_BROKER_BASE}/account-link/link_session_1"
    )


@pytest.mark.asyncio
async def test_broker_client_keeps_the_broker_refusal_and_reason_code(
    fake_broker,
) -> None:
    _, client = fake_broker
    request = AccountLinkRequestV1(
        request_id="acct_link_req_refused",
        team_id="team_test",
        tool_id="http:portainer",
        owner_kind=LinkOwnerKind.TEAM,
        person_id="usr_mia",
        person_roles=("member",),
    )

    with pytest.raises(GatewaySecureBrokerRefusal) as refused:
        await client.create_account_link_session(request)

    assert refused.value.status == 403
    assert refused.value.reason_code == "account_link_team_admin_required"
    assert refused.value.message == (
        "Only a team administrator or owner can link the team's account."
    )


@pytest.mark.asyncio
async def test_broker_client_reports_plain_text_errors_without_a_reason_code(
    fake_broker,
) -> None:
    _, client = fake_broker
    wrong_token = HttpGatewaySecureBroker(
        internal_base_url=client._internal_base_url,
        public_base_url=PUBLIC_BROKER_BASE,
        token="another-process-token",
    )

    with pytest.raises(GatewaySecureBrokerRefusal) as refused:
        await wrong_token.get_account_link_status(status_url="", session_id="missing")

    assert refused.value.status == 401
    assert refused.value.reason_code is None
    assert refused.value.message == "Missing or invalid broker token."


# The /link command -----------------------------------------------------------


async def _run_link_command(gateway: UserGateway, raw_text: str, *, person: str):
    return await gateway.command_application.execute(
        GatewayCommandRequest(
            raw_text=raw_text,
            current_session_key=None,
            channel_name="website",
            user_id=person,
            chat_id="web-chat",
            canonical_user_id=person,
        )
    )


@pytest.mark.asyncio
async def test_link_command_opens_the_persons_own_form(linked_gateway) -> None:
    gateway, broker, transport = linked_gateway
    owner = gateway.config.user.canonical_user_id

    result = await _run_link_command(gateway, "/link http:portainer", person=owner)

    assert result.handled is True
    assert broker.requests == [
        {
            "request_id": broker.requests[0]["request_id"],
            "team_id": gateway.config.manager.team_id,
            "tool_id": "http:portainer",
            "owner_kind": "person",
            "person_id": owner,
            "person_roles": list(gateway._request_context_service.team_roles(owner)),
            "reason": "You asked for this with /link.",
        }
    ]
    assert str(broker.requests[0]["request_id"]).startswith("acct_link_cmd")
    form_url = f"{PUBLIC_BROKER_BASE}/account-link/link_session_1"
    action = result.transient_action
    assert action is not None
    assert action.kind == "account_link"
    assert action.label == "Open account link form"
    assert action.url == form_url
    assert action.description == (
        "Link an account to http:portainer (your own account). Open the form to "
        "choose which stored credential it uses. Linking an account gives nobody "
        "access by itself."
    )
    assert result.text == f"{action.description}\nOpen account link form: {form_url}"
    assert action.receipt == "Account link form opened for http:portainer (your own account)."
    # No agent waits on a /link form, and without a chat session nobody can be
    # told how it ended, so nothing watches it.
    assert gateway._credential_workflow.pending_poll_count == 0
    assert gateway._credential_workflow.link_results.pending_count == 0
    assert transport.durable == []


@pytest.mark.asyncio
async def test_link_command_opens_the_teams_form_for_an_administrator(
    linked_gateway,
) -> None:
    gateway, broker, _ = linked_gateway
    owner = gateway.config.user.canonical_user_id

    result = await _run_link_command(gateway, "/link http:portainer team", person=owner)

    assert broker.requests[0]["owner_kind"] == "team"
    assert result.transient_action is not None
    assert "(the team's account)" in result.transient_action.description


@pytest.mark.asyncio
async def test_link_command_team_link_refused_for_a_member(linked_gateway) -> None:
    gateway, broker, _ = linked_gateway
    member = _member(gateway)

    result = await _run_link_command(gateway, "/link http:portainer team", person=member)

    assert broker.requests[0]["person_roles"] == ["member"]
    assert result.transient_action is None
    assert result.text == (
        "Could not open the account link form for http:portainer. Only a team "
        "administrator or owner can link the team's account."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "/link http:nothing",
            "Could not open the account link form for http:nothing. "
            "http:nothing is not in the tool catalog.",
        ),
        (
            "/link http:public_weather",
            "Could not open the account link form for http:public_weather. "
            "Public Weather does not use a linked account.",
        ),
    ],
)
async def test_link_command_reports_broker_refusals_for_unusable_tools(
    linked_gateway, command: str, expected: str
) -> None:
    gateway, broker, _ = linked_gateway
    owner = gateway.config.user.canonical_user_id

    result = await _run_link_command(gateway, command, person=owner)

    assert len(broker.requests) == 1
    assert result.transient_action is None
    assert result.text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected_start"),
    [
        ("/link", "Usage: /link <tool_id> [me|team]"),
        ("/link help", "Usage: /link <tool_id> [me|team]"),
        ("/link http:portainer everyone", "Usage: /link <tool_id> [me|team]"),
        ("/link cli:github everyone", "Usage: /link cli:github [token | web | web-all]"),
        ("/link cli:x token", "Usage: /link cli:x [web]"),
        (
            "/link Portainer",
            "Could not open the account link form for Portainer. Portainer is not "
            "a tool id.",
        ),
    ],
)
async def test_link_command_explains_usage_without_calling_the_broker(
    linked_gateway, command: str, expected_start: str
) -> None:
    gateway, broker, _ = linked_gateway
    owner = gateway.config.user.canonical_user_id

    result = await _run_link_command(gateway, command, person=owner)

    assert result.handled is True
    assert result.text.startswith(expected_start)
    assert broker.requests == []


@pytest.mark.asyncio
async def test_link_command_needs_a_reachable_broker(tmp_path: Path) -> None:
    config = _build_test_config(tmp_path)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
        secure_broker=HttpGatewaySecureBroker(
            internal_base_url=None, public_base_url=None, token=None
        ),
    )

    result = await _run_link_command(
        gateway, "/link http:portainer", person=config.user.canonical_user_id
    )

    assert result.text == (
        "Could not open the account link form for http:portainer. Account link "
        "forms are unavailable. Secure tool broker is not enabled for this gateway."
    )


def test_link_command_is_listed_in_help() -> None:
    from assistant_gateway.application.command_help import (
        build_interactive_session_help,
        build_web_chat_help,
    )

    assert "/link <tool_id> [me|team]" in build_interactive_session_help()
    assert "/link <tool_id> [ me | team ]" in build_web_chat_help()


# /link for tools a person signs in to ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "provider", "method", "purpose"),
    [
        # Browser sign-in is GitHub's default; pasting a token is asked for by name.
        ("/link cli:github", "github", "web", "complete the GitHub browser sign-in and MFA"),
        ("/link cli:github token", "github", "token", "paste a GitHub token"),
        ("/link cli:github me", "github", "web", "complete the GitHub browser sign-in and MFA"),
        ("/link cli:google", "google", "web", "sign in with your Google account"),
        ("/link cli:google json_token", "google", "json_token", "paste a gog token export JSON"),
        ("/link cli:x", "x", "web", "sign in with your X account"),
    ],
)
async def test_link_command_starts_the_tools_sign_in_for_the_person(
    linked_gateway, command: str, provider: str, method: str, purpose: str
) -> None:
    gateway, broker, transport = linked_gateway
    owner = gateway.config.user.canonical_user_id

    result = await _run_link_command(gateway, command, person=owner)

    # The sign-in page, not an account link form, for the person who typed it.
    assert broker.requests == []
    assert len(broker.sign_ins) == 1
    called_provider, body = broker.sign_ins[0]
    assert called_provider == provider
    assert body["method"] == method
    assert body["context"] == {
        "canonical_user_id": owner,
        "team_id": gateway.config.manager.team_id,
        "principal_type": "user",
    }
    action = result.transient_action
    assert action is not None
    assert action.kind == "broker_authentication"
    assert action.url == f"{PUBLIC_BROKER_BASE}/auth/{provider}/v1.signed_session"
    assert purpose in action.description
    assert transport.durable == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected", "absent"),
    [
        (
            "/link cli:github team",
            "GitHub sign-ins are personal: each person signs in with their own account with /link cli:github",
            "team_client",
        ),
        (
            "/link cli:google team",
            "Administrators set the team's Google sign-in app, which people sign in through, "
            "with /auth google team_client.",
            None,
        ),
    ],
)
async def test_link_command_explains_that_sign_ins_are_personal(
    linked_gateway, command: str, expected: str, absent: str | None
) -> None:
    gateway, broker, _ = linked_gateway
    owner = gateway.config.user.canonical_user_id

    result = await _run_link_command(gateway, command, person=owner)

    assert expected in result.text
    assert absent is None or absent not in result.text
    assert result.transient_action is None
    assert broker.requests == [] and broker.sign_ins == []


@pytest.mark.asyncio
async def test_link_command_links_the_teams_x_account_as_the_fallback(linked_gateway) -> None:
    gateway, broker, _ = linked_gateway
    owner = gateway.config.user.canonical_user_id

    result = await _run_link_command(gateway, "/link cli:x team", person=owner)

    assert broker.sign_ins == []
    assert broker.requests[0]["tool_id"] == "cli:x"
    assert broker.requests[0]["owner_kind"] == "team"
    assert result.transient_action is not None
    assert result.transient_action.kind == "account_link"


@pytest.mark.asyncio
async def test_account_link_opener_rejects_an_expired_broker_session(
    fake_broker,
) -> None:
    _, client = fake_broker

    class _LateClock:
        def now_utc(self) -> datetime:
            return SystemGatewayClock().now_utc() + timedelta(hours=1)

    opener = GatewayAccountLinks(broker=client, team_id="team_test", clock=_LateClock())

    with pytest.raises(AccountLinkUnavailable) as unavailable:
        await opener.open_form(
            request_id="acct_link_req_late",
            person_id="usr_alice",
            person_roles=("admin",),
            tool_id="http:portainer",
            owner="person",
            reason="",
        )

    assert unavailable.value.reason_code == "account_link_session_invalid"
    assert unavailable.value.message == (
        "The secure tool broker returned an unusable form. Try again later."
    )


# Against the real broker ------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_opener_links_an_account_through_the_real_broker(
    world, monkeypatch  # noqa: F811 - the imported pytest fixture
) -> None:
    gate, store = world
    monkeypatch.setenv("PORTAINER_TOKEN", "portainer-secret")
    save_credentials(
        CredentialOwnerScope.user("user-1"),
        portainer_token=token_credential("PORTAINER_TOKEN"),
    )
    server = TestServer(SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate).build_app())
    await server.start_server()
    try:
        client = HttpGatewaySecureBroker(
            internal_base_url=str(server.make_url("")).rstrip("/"),
            public_base_url=None,
            token=caller_token(CALLER_GATEWAY),
        )
        opener = GatewayAccountLinks(
            broker=client, team_id=TEST_TEAM, clock=SystemGatewayClock()
        )
        form = await opener.open_form(
            request_id="acct_link_req_real_broker",
            person_id="user-1",
            person_roles=("member",),
            tool_id="http:portainer",
            owner="person",
            reason="Restart stacks after deploys.",
        )
        async with aiohttp.ClientSession() as browser:
            page = await browser.get(form.form_url)
            assert "Link an account to Portainer?" in await page.text()
            submitted = await browser.post(
                form.form_url,
                data={"credential_key": "portainer_token", "confirm": "yes"},
            )
            assert submitted.status == 200
        status = await client.get_account_link_status(
            status_url=form.status_url, session_id=form.session_id
        )
        with pytest.raises(AccountLinkUnavailable) as refused:
            await opener.open_form(
                request_id="acct_link_req_real_broker_team",
                person_id="user-1",
                person_roles=("member",),
                tool_id="http:portainer",
                owner="team",
                reason="",
            )
    finally:
        await server.close()

    stored = store.active_link(TEST_TEAM, "http:portainer", LinkOwnerKind.PERSON, "user-1")
    assert stored is not None
    assert status["status"] == "account_link_created"
    assert status["result"]["link_id"] == stored.link_id
    assert status["result"]["tool_id"] == "http:portainer"
    assert status["result"]["owner_kind"] == "person"
    assert refused.value.reason_code == "account_link_team_admin_required"
    assert refused.value.message == (
        "Only a team administrator or owner can link the team's account."
    )


# How a /link page ended, told in the chat that ran /link -----------------------

WEB_SESSION = "session_web"
LINK_COMMAND_ID = "corr_link_cmd"


@pytest_asyncio.fixture
async def chat_gateway(tmp_path: Path, fake_broker):
    """A gateway with a Web Chat session; delivered events are captured."""

    broker, client = fake_broker
    events: list = []
    config = _build_test_config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=events.append,
        state_store=store,
        manager_transport=transport,
        secure_broker=client,
    )
    gateway._credential_workflow.link_results._poll_interval_seconds = 0.01
    owner = config.user.canonical_user_id
    store.create_session(
        session_key=WEB_SESSION,
        conversation_id="conversation_web",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="website",
        user_id=owner,
        chat_id="web-chat",
        owner_canonical_user_id=owner,
    )
    yield gateway, broker, events, transport
    await gateway._credential_workflow.link_results.close()


async def _link_in_chat(gateway: UserGateway, raw_text: str):
    owner = gateway.config.user.canonical_user_id
    return await gateway.command_application.execute(
        GatewayCommandRequest(
            raw_text=raw_text,
            current_session_key=WEB_SESSION,
            channel_name="website",
            user_id=owner,
            chat_id="web-chat",
            canonical_user_id=owner,
            correlation_id=LINK_COMMAND_ID,
        )
    )


async def _posted(events: list, *, timeout: float = 5.0):
    async def wait():
        while not events:
            await asyncio.sleep(0.01)
        return events[-1]

    return await asyncio.wait_for(wait(), timeout=timeout)


def _stored_result(gateway: UserGateway):
    page = gateway._transcript_service.history_page(
        owner_canonical_user_id=gateway.config.user.canonical_user_id,
        session_key=WEB_SESSION,
        before_cursor=None,
    )
    return page.messages[-1]


@pytest.mark.asyncio
async def test_link_result_says_the_teams_account_was_linked_in_the_same_chat(
    chat_gateway, caplog
) -> None:
    gateway, broker, events, transport = chat_gateway
    caplog.set_level("DEBUG")

    result = await _link_in_chat(gateway, "/link http:portainer team")
    assert result.transient_action is not None
    assert result.transient_action.receipt == (
        "Account link form opened for http:portainer (the team's account)."
    )
    assert gateway._credential_workflow.link_results.pending_count == 1
    broker.complete("link_session_1")
    event = await _posted(events)

    text = (
        "Linked the team's account to Portainer (http:portainer). "
        "Settings → Access → By Tool lists it. "
        "Linking an account gives nobody access by itself."
    )
    assert event.text == text
    assert event.message_type == LINK_RESULT_MESSAGE_TYPE
    assert event.correlation_id == LINK_COMMAND_ID
    assert (event.session_key, event.channel_name, event.chat_id) == (
        WEB_SESSION,
        "website",
        "web-chat",
    )
    # Web Chat keeps it in the transcript as a System line with the card's id.
    stored = _stored_result(gateway)
    assert (stored.content, stored.message_type, stored.correlation_id, stored.role) == (
        text,
        LINK_RESULT_MESSAGE_TYPE,
        LINK_COMMAND_ID,
        "system",
    )
    assert len(events) == 1
    assert transport.durable == []  # No agent is told anything.
    await asyncio.sleep(0.05)
    assert gateway._credential_workflow.link_results.pending_count == 0
    # Neither the message nor the gateway's logs carry the form's session.
    gateway_logs = " ".join(
        record.getMessage() for record in caplog.records if record.name.startswith("assistant_gateway")
    )
    assert "posted" in gateway_logs and "event=gateway_link_result_posted" in gateway_logs
    assert "link_session_1" not in event.text
    assert "link_session_1" not in gateway_logs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        (
            lambda broker: broker.decline("link_session_1"),
            "The account link form for Portainer (http:portainer) was closed without linking anything.",
        ),
        (
            lambda broker: broker.refuse(
                "link_session_1",
                error="The account link form expired before it was used.",
                reason_code="account_link_session_expired",
            ),
            "The account link form for Portainer (http:portainer) expired; nothing was linked. "
            "Run /link http:portainer again.",
        ),
        (
            lambda broker: broker.refuse(
                "link_session_1",
                error="That credential is not one of the stored credentials offered for this tool",
                reason_code="account_link_credential_unavailable",
            ),
            "Nothing was linked to Portainer (http:portainer). That credential is not one of the "
            "stored credentials offered for this tool.",
        ),
        (
            lambda broker: broker.drop("link_session_1"),
            "Couldn't confirm whether the account link for Portainer (http:portainer) finished. "
            "Check Settings → Access → By Tool.",
        ),
    ],
    ids=["closed", "expired_at_broker", "refused", "broker_forgot_the_form"],
)
async def test_link_result_says_how_an_unfinished_form_ended(
    chat_gateway, finish, expected: str
) -> None:
    gateway, broker, events, _ = chat_gateway

    await _link_in_chat(gateway, "/link http:portainer")
    finish(broker)
    event = await _posted(events)

    assert event.text == expected
    assert event.correlation_id == LINK_COMMAND_ID
    assert _stored_result(gateway).content == expected


@pytest.mark.asyncio
async def test_link_result_says_the_form_expired_when_time_ran_out(chat_gateway) -> None:
    gateway, _, events, _ = chat_gateway
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    gateway._credential_workflow.link_results.watch_account_link(
        form=AccountLinkForm(
            session_id="link_session_old",
            form_url=f"{PUBLIC_BROKER_BASE}/account-link/link_session_old",
            status_url="http://broker.invalid/v1/account-link/sessions/link_session_old",
            expires_at_utc=expired,
            tool_display_name="Crawl4AI",
        ),
        tool_id="mcp:crawl4ai",
        owner="team",
        restart_command="/link mcp:crawl4ai team",
        target=LinkResultTarget(session_key=WEB_SESSION, correlation_id="corr_old"),
    )
    event = await _posted(events)

    assert event.text == (
        "The account link form for Crawl4AI (mcp:crawl4ai) expired; nothing was linked. "
        "Run /link mcp:crawl4ai team again."
    )
    assert event.correlation_id == "corr_old"


@pytest.mark.asyncio
async def test_link_result_for_another_tool_is_never_reported_as_linked(
    chat_gateway, caplog
) -> None:
    gateway, broker, events, _ = chat_gateway

    await _link_in_chat(gateway, "/link http:portainer")
    broker.complete("link_session_1")
    broker.sessions["link_session_1"]["result"]["tool_id"] = "http:grafana"
    event = await _posted(events)

    assert event.text.startswith("Couldn't confirm whether the account link for Portainer")
    assert "event=gateway_link_result_mismatch" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "provider", "status", "expected"),
    [
        (
            "/link cli:github",
            "github",
            {"status": "success"},
            "Signed in to GitHub. Signing in gives nobody access by itself.",
        ),
        (
            "/link cli:x",
            "x",
            {"status": "success", "username": "alice", "message": "Signed in to X as @alice."},
            "Signed in to X as alice. Signing in gives nobody access by itself.",
        ),
        (
            "/link cli:google json_token",
            "google",
            {"status": "error", "message": "The token export was not valid JSON."},
            "The Google sign-in did not finish; the sign-in page says why. "
            "Run /link cli:google json_token to try again.",
        ),
    ],
    ids=["github", "x_with_account", "google_failed"],
)
async def test_link_sign_in_result_is_posted_in_the_same_chat(
    chat_gateway, command: str, provider: str, status: dict, expected: str
) -> None:
    gateway, broker, events, _ = chat_gateway

    result = await _link_in_chat(gateway, command)
    assert result.transient_action is not None
    assert result.transient_action.kind == "broker_authentication"
    broker.sign_in_status[provider] = status
    event = await _posted(events)

    assert event.text == expected
    assert event.message_type == LINK_RESULT_MESSAGE_TYPE
    assert event.correlation_id == LINK_COMMAND_ID
    # The sign-in page's status route is public; the broker token never goes there.
    assert broker.sign_in_status_auth and set(broker.sign_in_status_auth) == {None}


@pytest.mark.asyncio
async def test_link_result_is_posted_after_linking_with_the_real_broker(
    world, monkeypatch, caplog  # noqa: F811 - the imported pytest fixture
) -> None:
    """End to end with the real broker: the message names the link, never the credential."""

    gate, _ = world
    monkeypatch.setenv("PORTAINER_TOKEN", "portainer-secret")
    save_credentials(
        CredentialOwnerScope.user("user-1"),
        portainer_token=token_credential("PORTAINER_TOKEN"),
    )
    caplog.set_level("DEBUG")
    server = TestServer(SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate).build_app())
    await server.start_server()
    events: list = []

    class _Output:
        async def emit(self, event) -> None:
            events.append(event)

    class _Sessions:
        def get_session(self, session_key: str):
            return SimpleNamespace(
                session_key=session_key,
                channel_name="telegram",
                user_id="tg_user_1",
                chat_id="4242",
                thread_id=None,
                conversation_id="conversation_tg",
            )

    try:
        client = HttpGatewaySecureBroker(
            internal_base_url=str(server.make_url("")).rstrip("/"),
            public_base_url=None,
            token=caller_token(CALLER_GATEWAY),
        )
        form = await GatewayAccountLinks(
            broker=client, team_id=TEST_TEAM, clock=SystemGatewayClock()
        ).open_form(
            request_id="acct_link_cmd_real",
            person_id="user-1",
            person_roles=("member",),
            tool_id="http:portainer",
            owner="person",
            reason="You asked for this with /link.",
        )
        results = GatewayLinkResults(
            broker=client,
            sessions=_Sessions(),  # type: ignore[arg-type]
            event_output=_Output(),
            clock=SystemGatewayClock(),
            poll_interval_seconds=0.01,
        )
        results.watch_account_link(
            form=form,
            tool_id="http:portainer",
            owner="person",
            restart_command="/link http:portainer",
            target=LinkResultTarget(session_key="session_tg", correlation_id="link_cmd_tg"),
        )
        async with aiohttp.ClientSession() as browser:
            submitted = await browser.post(
                form.form_url,
                data={"credential_key": "portainer_token", "confirm": "yes"},
            )
            assert submitted.status == 200
        event = await _posted(events)
        await results.close()
    finally:
        await server.close()

    assert form.tool_display_name == "Portainer"
    assert event.text == (
        "Linked your own account to Portainer (http:portainer). "
        "Settings → Access → By Tool lists it. "
        "Linking an account gives nobody access by itself."
    )
    assert (event.channel_name, event.chat_id) == ("telegram", "4242")
    for secret_or_key in ("portainer-secret", "portainer_token", form.session_id):
        assert secret_or_key not in event.text
    assert "portainer-secret" not in caplog.text

