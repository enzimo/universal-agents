"""Account links: the store, the broker's choice of link, the labeled form, and use.

An account link chooses which stored credential a tool uses for the team or for
one person; it grants nothing. These tests use the real broker app, the real
SQLite access store and the file-backed credential catalog.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from access_catalog.catalog import CatalogSource
from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.identity import ContextVerifier
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccountLinkRequestV1,
    AccountLinkStatus,
    AccountLinkV1,
    CredentialInjectionV1,
    GrantLifetime,
    GrantV1,
    InjectionPlacement,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from secure_tool_broker import credential_catalog
from secure_tool_broker.access_gate import REASON_ACCOUNT_LINK_MISSING, BrokerAccessGate
from secure_tool_broker.account_links import (
    CREDENTIAL_SOURCE_FIELD,
    NEW_CREDENTIAL,
    NEW_FIELD_PREFIX,
    REASON_FIELDS_MISSING,
    REASON_NOT_STORED,
    AccountLinkRefused,
    LinkChoice,
    link_account,
    used_by_text,
)
from secure_tool_broker.credential_catalog import (
    CatalogCredential,
    CatalogCredentialField,
    CatalogSourceRef,
    CredentialCatalog,
    load_catalog,
    save_catalog,
)
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.credential_sources import sealbox as sealbox_source
from secure_tool_broker.service import SecureToolBrokerService
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import (
    BROKER_ROOT,
    TEST_TEAM,
    agent_context,
    caller_headers,
    context_verify_keys,
    gateway_headers,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
PORTAINER_TOOL = """
tools:
  - tool_id: http:portainer
    display_name: Portainer
    account: linked_credential
    hosts: [portainer.example.test]
    credential_injections:
      - {placement: header, name: X-API-Key, field: api_token}
    routes:
      - {name: stacks.restart, method: POST, path: '/api/stacks/{id}/restart', level: write}
"""


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "credential_catalog.db"))
    monkeypatch.setattr(credential_catalog, "sealbox_tenant_environment", lambda **_kwargs: {})
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path / "broker_credentials"))


@pytest.fixture
def world(tmp_path: Path):
    tools = tmp_path / "instance" / "control" / "access" / "tools"
    tools.mkdir(parents=True)
    (tools / "portainer.yaml").write_text(PORTAINER_TOOL, encoding="utf-8")
    store = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )
    gate = BrokerAccessGate(
        catalogs=CatalogSource(tmp_path / "instance"),
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEST_TEAM,
        access=None,
        links=store,
    )
    return gate, store


@pytest.fixture
def sealbox(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """An in-memory Sealbox, the broker's default credential store.

    ``secret set`` keeps the value sent on standard input and ``secret get``
    returns it, so a credential typed into the form can be read back when a
    later request needs it. The returned dict is what the store holds.
    """

    held: dict[str, str] = {}

    def run(args, *, input_text=None, env=None, timeout_seconds=30):
        verb, key = list(args[:2]), args[2]
        if verb == ["secret", "set"]:
            held[key] = input_text or ""
            return subprocess.CompletedProcess(["sealbox-cli", *args], 0, stdout='{"version":1}\n', stderr="")
        if verb == ["secret", "get"] and key in held:
            return subprocess.CompletedProcess(["sealbox-cli", *args], 0, stdout=json.dumps({"value": held[key]}), stderr="")
        return subprocess.CompletedProcess(["sealbox-cli", *args], 1, stdout="", stderr=f"[SealboxError] secret not found: {key}")

    monkeypatch.delenv("SECURE_TOOL_BROKER_DEFAULT_CREDENTIAL_WRITE_SOURCE", raising=False)
    monkeypatch.setenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS", "0")
    monkeypatch.setattr(credential_catalog, "run_sealbox", run)
    monkeypatch.setattr(sealbox_source, "run_sealbox", run)
    monkeypatch.setattr(sealbox_source, "sealbox_tenant_environment", lambda **_kwargs: {})
    return held


def token_credential(env_var: str, *, field: str = "api_token", name: str = "Token") -> CatalogCredential:
    return CatalogCredential(
        display_name=name,
        purpose="Tests account links.",
        fields={
            field: CatalogCredentialField(
                kind="token",
                visibility="secret",
                resolution_order=[CatalogSourceRef(source="env", env_var=env_var)],
            )
        },
    )


def save_credentials(scope: CredentialOwnerScope, **credentials: CatalogCredential) -> None:
    """Replace the owner's stored credentials with these (save_catalog replaces the scope)."""

    save_catalog(CredentialCatalog(credentials=credentials), scope=scope)


def link(store: AccessRepository, owner_kind: LinkOwnerKind, owner_id: str, key: str, tool_id: str = "http:portainer"):
    return store.create_link(
        AccountLinkV1(
            link_id=f"account_link_{owner_kind.value}_{key}",
            team_id=TEST_TEAM,
            tool_id=tool_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            credential_key=key,
            injections=(CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-API-Key", field="api_token"),),
            created_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
            created_at_utc=NOW,
        )
    )


# -- store and choice ----------------------------------------------------------------


def test_a_new_link_replaces_the_owners_previous_link_and_is_recorded(world) -> None:
    _gate, store = world
    first = link(store, LinkOwnerKind.PERSON, "user-1", "old_key")
    second = link(store, LinkOwnerKind.PERSON, "user-1", "new_key")

    assert store.active_link(TEST_TEAM, "http:portainer", LinkOwnerKind.PERSON, "user-1") == second
    assert store.get_link(first.link_id).status is AccountLinkStatus.REVOKED
    kinds = [event.kind for event in store.events(TEST_TEAM)]
    assert kinds.count("account_link_created") == 2 and "account_link_revoked" in kinds
    revoked = store.revoke_link(second.link_id, actor=SubjectRefV1(kind=SubjectKind.USER, id="user-1"), at=NOW)
    assert revoked.status is AccountLinkStatus.REVOKED
    assert store.list_links(TEST_TEAM) == []


def test_the_persons_own_link_wins_over_the_teams(world) -> None:
    gate, store = world
    team = link(store, LinkOwnerKind.TEAM, TEST_TEAM, "team_key")
    assert gate.account_link_for("http:portainer", "user-1") == team
    own = link(store, LinkOwnerKind.PERSON, "user-1", "own_key")
    assert gate.account_link_for("http:portainer", "user-1") == own
    assert gate.account_link_for("http:portainer", "user-2") == team


def test_a_linked_credential_tool_without_a_link_is_refused(world) -> None:
    gate, _store = world
    result = gate.check_broker_action(
        BrokerCaller(CALLER_MANAGER),
        "secure_requests",
        "get",
        {"url": "https://portainer.example.test/api/stacks"},
        agent_context(),
    )

    assert not result.allowed and result.reason_code == REASON_ACCOUNT_LINK_MISSING
    assert "http:portainer" in result.next_step


# -- the broker form ---------------------------------------------------------------


def link_request(owner_kind: str = "person", roles: tuple[str, ...] = ("member",), request_id: str = "req-1") -> dict:
    return AccountLinkRequestV1(
        request_id=request_id,
        team_id=TEST_TEAM,
        tool_id="http:portainer",
        owner_kind=LinkOwnerKind(owner_kind),
        person_id="user-1",
        person_roles=roles,
        reason="Restart stacks after deploys.",
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_a_person_links_their_own_credential_through_the_labeled_form(world, monkeypatch) -> None:
    gate, store = world
    monkeypatch.setenv("PORTAINER_TOKEN", "portainer-secret")
    save_credentials(
        CredentialOwnerScope.user("user-1"),
        portainer_token=token_credential("PORTAINER_TOKEN"),
        no_api_token=token_credential("PORTAINER_TOKEN", field="password"),
    )
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as client:
        created = await client.post("/v1/account-link/sessions", headers=gateway_headers(), json=link_request())
        session = await created.json()
        again = await (
            await client.post("/v1/account-link/sessions", headers=gateway_headers(), json=link_request())
        ).json()
        form = await client.get(f"/account-link/{session['session_id']}")
        page = await form.text()
        submitted = await client.post(
            f"/account-link/{session['session_id']}", data={"credential_key": "portainer_token", "confirm": "yes"}
        )
        status_path = f"/v1/account-link/sessions/{session['session_id']}"
        status = await (await client.get(status_path, headers=gateway_headers())).json()

    assert created.status == 200 and again["session_id"] == session["session_id"]
    assert "Link an account to Portainer?" in page
    assert "portainer.example.test" in page and "X-API-Key" in page
    assert "gives nobody access" in page
    assert "portainer_token" in page and "no_api_token" not in page, "only credentials with the needed field"
    assert submitted.status == 200
    assert status["status"] == "account_link_created"
    stored = store.active_link(TEST_TEAM, "http:portainer", LinkOwnerKind.PERSON, "user-1")
    assert stored is not None and stored.credential_key == "portainer_token"
    assert stored.link_id == status["result"]["link_id"]
    assert "portainer-secret" not in page


@pytest.mark.asyncio
async def test_team_links_need_an_administrator_and_only_the_gateway_opens_forms(world) -> None:
    gate, _store = world
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as client:
        member = await client.post(
            "/v1/account-link/sessions", headers=gateway_headers(), json=link_request("team", ("member",))
        )
        member_text = await member.text()
        agent = await client.post("/v1/account-link/sessions", headers=caller_headers(), json=link_request())
        admin = await client.post(
            "/v1/account-link/sessions", headers=gateway_headers(), json=link_request("team", ("admin",), "req-2")
        )
        reused = await client.post(
            "/v1/account-link/sessions",
            headers=gateway_headers(),
            json={**link_request("team", ("admin",), "req-2"), "reason": "different"},
        )

    assert member.status == 403
    assert "account_link_team_admin_required" in member_text
    assert agent.status == 403
    assert admin.status == 200
    assert reused.status == 409


@pytest.mark.asyncio
async def test_the_teams_account_form_is_refused_while_the_team_keeps_the_tool_private(world) -> None:
    from access_control.account_sharing import TeamAccountSharing
    from custom_messages.contracts.access_v2 import AccountSharing

    gate, store = world
    tool = gate.catalog.tools["http:portainer"]
    TeamAccountSharing(store, TEST_TEAM).choose_tool(tool, AccountSharing.PRIVATE, by="user-1")
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as client:
        team = await client.post(
            "/v1/account-link/sessions", headers=gateway_headers(), json=link_request("team", ("admin",))
        )
        team_text = await team.text()
        own = await client.post(
            "/v1/account-link/sessions", headers=gateway_headers(), json=link_request("person", ("member",), "req-2")
        )

    assert team.status == 403
    assert "account_link_team_private" in team_text and "/accounts share http:portainer" in team_text
    assert own.status == 200


@pytest.mark.asyncio
async def test_an_unconfirmed_form_creates_nothing(world, monkeypatch) -> None:
    gate, store = world
    monkeypatch.setenv("PORTAINER_TOKEN", "portainer-secret")
    save_credentials(CredentialOwnerScope.user("user-1"), portainer_token=token_credential("PORTAINER_TOKEN"))
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as client:
        session = await (
            await client.post("/v1/account-link/sessions", headers=gateway_headers(), json=link_request())
        ).json()
        await client.post(f"/account-link/{session['session_id']}", data={"credential_key": "portainer_token"})
        second = await client.post(
            f"/account-link/{session['session_id']}", data={"credential_key": "portainer_token", "confirm": "yes"}
        )

    assert second.status == 409, "a form is used once"
    assert store.list_links(TEST_TEAM) == []


# -- adding a new credential in the form ------------------------------------------------


def new_credential_form(value: str) -> dict[str, str]:
    return {CREDENTIAL_SOURCE_FIELD: NEW_CREDENTIAL, f"{NEW_FIELD_PREFIX}api_token": value, "confirm": "yes"}


class _PortainerResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def read(self, _limit: int) -> bytes:
        return b'{"stacks": []}'


def agent_web_request() -> dict:
    return {
        "provider": "secure_requests",
        "action": "get",
        "params": {"url": "https://portainer.example.test/api/stacks"},
        "reason": "list stacks",
        "context": agent_context(task_id="task-1"),
    }


@pytest.mark.asyncio
async def test_a_person_adds_a_new_credential_in_the_form_and_a_later_request_carries_it(
    world, sealbox, monkeypatch, caplog
) -> None:
    gate, store = world
    typed = "typed-portainer-secret-value"
    sent: list = []

    def fake_send(request, *, timeout):
        sent.append(request)
        return _PortainerResponse()

    monkeypatch.setattr("secure_tool_broker.credential_catalog._send_api_request", fake_send)
    caplog.set_level(logging.DEBUG)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as client:
        session = await (
            await client.post("/v1/account-link/sessions", headers=gateway_headers(), json=link_request())
        ).json()
        form_path = f"/account-link/{session['session_id']}"
        page = await (await client.get(form_path)).text()
        submitted = await client.post(form_path, data=new_credential_form(typed))
        result_page = await submitted.text()
        status_path = f"/v1/account-link/sessions/{session['session_id']}"
        status = await (await client.get(status_path, headers=gateway_headers())).json()
        used = await client.post("/v1/tools/secure_requests", headers=caller_headers(), json=agent_web_request())
        used_payload = await used.json()

    assert "Add a new credential" in page and "Use a stored credential" not in page, "no stored credential fits"
    assert 'name="field__api_token" type="password"' in page and "data-secret-toggle" in page
    assert "<strong>Used by:</strong> Any agent that uses Portainer, because access checks are off" in page
    assert submitted.status == 200 and status["status"] == "account_link_created"
    stored = store.active_link(TEST_TEAM, "http:portainer", LinkOwnerKind.PERSON, "user-1")
    assert stored is not None and stored.credential_key == "http-portainer-personal"
    assert set(load_catalog(scope=CredentialOwnerScope.user("user-1")).credentials) == {"http-portainer-personal"}
    assert load_catalog(scope=CredentialOwnerScope.team(TEST_TEAM)).credentials == {}, "a person's link stays theirs"
    assert list(sealbox.values()) == [typed], "the value is only in the credential store"
    assert "http-portainer-personal" in result_page and "your own credentials" in result_page
    assert used.status == 200 and used_payload["status"] == "success", used_payload
    assert len(sent) == 1 and sent[0].get_header("X-api-key") == typed
    assert "event=account_link_credential_added credential_key=http-portainer-personal" in caplog.text
    for shown in (result_page, json.dumps(status), json.dumps(used_payload), caplog.text):
        assert typed not in shown


@pytest.mark.asyncio
async def test_an_administrator_adds_the_teams_credential_without_replacing_a_stored_one(
    world, sealbox, monkeypatch
) -> None:
    gate, store = world
    monkeypatch.setenv("PORTAINER_TOKEN", "older-team-secret")
    team = CredentialOwnerScope.team(TEST_TEAM)
    save_credentials(team, **{"http-portainer-team": token_credential("PORTAINER_TOKEN", name="Older token")})
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as client:
        session = await (
            await client.post(
                "/v1/account-link/sessions", headers=gateway_headers(), json=link_request("team", ("admin",))
            )
        ).json()
        form_path = f"/account-link/{session['session_id']}"
        page = await (await client.get(form_path)).text()
        submitted = await client.post(form_path, data=new_credential_form("fresh-team-secret"))
        result_page = await submitted.text()

    assert "Use a stored credential" in page and "Older token" in page and "Add a new credential" in page
    assert submitted.status == 200
    credentials = load_catalog(scope=team).credentials
    older = credentials["http-portainer-team"].fields["api_token"].resolution_order[0]
    assert older.source == "env" and older.env_var == "PORTAINER_TOKEN", "the stored credential is untouched"
    assert set(credentials) == {"http-portainer-team", "http-portainer-team-2"}
    link = store.active_link(TEST_TEAM, "http:portainer", LinkOwnerKind.TEAM, TEST_TEAM)
    assert link is not None and link.credential_key == "http-portainer-team-2"
    assert list(sealbox.values()) == ["fresh-team-secret"]
    assert "http-portainer-team-2" in result_page and "fresh-team-secret" not in result_page
    assert load_catalog(scope=CredentialOwnerScope.user("user-1")).credentials == {}


def test_a_member_cannot_store_or_link_a_credential_for_the_team(world, sealbox) -> None:
    """The broker refuses a member's team form when it is opened (see the admin test above);
    the check runs again before anything is stored."""

    gate, store = world
    member = AccountLinkRequestV1.model_validate(link_request("team", ("member",)))

    with pytest.raises(AccountLinkRefused) as refused:
        link_account(store, gate.catalog, member, LinkChoice(new_values={"api_token": "member-secret"}), now=NOW)

    assert refused.value.reason_code == "account_link_team_admin_required"
    assert sealbox == {} and store.list_links(TEST_TEAM) == []
    assert load_catalog(scope=CredentialOwnerScope.team(TEST_TEAM)).credentials == {}


def test_a_new_credential_needs_every_field_and_a_store_failure_links_nothing(
    world, sealbox, monkeypatch, caplog
) -> None:
    gate, store = world
    person = AccountLinkRequestV1.model_validate(link_request())
    with pytest.raises(AccountLinkRefused) as empty:
        link_account(store, gate.catalog, person, LinkChoice(new_values={"api_token": "  "}), now=NOW)

    def refusing_store(args, *, input_text=None, env=None, timeout_seconds=30):
        return subprocess.CompletedProcess(["sealbox-cli", *args], 1, stdout="", stderr=f"refused {input_text}")

    monkeypatch.setattr(credential_catalog, "run_sealbox", refusing_store)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(AccountLinkRefused) as failed:
        link_account(store, gate.catalog, person, LinkChoice(new_values={"api_token": "leaky-secret"}), now=NOW)

    assert empty.value.reason_code == REASON_FIELDS_MISSING and "api_token" in str(empty.value)
    assert failed.value.reason_code == REASON_NOT_STORED and "nothing was linked" in str(failed.value)
    assert "leaky-secret" not in str(failed.value) and "leaky-secret" not in caplog.text
    assert "event=account_link_credential_not_stored" in caplog.text
    assert store.list_links(TEST_TEAM) == []


def agent_type_grant(grant_id: str, agent_type: str, target: TargetRefV1, **extra) -> GrantV1:
    return GrantV1(
        grant_id=grant_id,
        team_id=TEST_TEAM,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEST_TEAM}/{agent_type}"),
        target=target,
        level=AccessLevel.WRITE,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
        created_at_utc=NOW,
        **extra,
    )


@pytest.mark.asyncio
async def test_the_form_says_which_agents_use_the_account_for_whose_work(world) -> None:
    gate, store = world
    gate.access = AccessService(
        store=store, catalog=gate.catalog, cedar=CedarAccessClient(), team_id=TEST_TEAM, clock=lambda: NOW
    )
    tool = gate.catalog.tools["http:portainer"]
    person = AccountLinkRequestV1.model_validate(link_request())
    team = AccountLinkRequestV1.model_validate(link_request("team", ("admin",), "req-2"))
    nobody_yet = used_by_text(gate.access, tool, person)
    portainer = TargetRefV1(kind=TargetKind.TOOL, id="http:portainer")
    for grant in (
        # A grant for one task (the default when a proposal is approved) counts while it lasts.
        agent_type_grant(
            "g-boa", "BackgroundOperationsAgent", portainer,
            for_person_id="user-1", lifetime=GrantLifetime.TASK, task_id="task-1",
        ),
        agent_type_grant("g-lead", "TeamArchitect", TargetRefV1(kind=TargetKind.OPERATION, id="http:portainer#stacks.restart")),
        agent_type_grant("g-other", "OtherPersonsAgent", portainer, for_person_id="user-2"),
        agent_type_grant("g-elsewhere", "WebResearchAgent", TargetRefV1(kind=TargetKind.TOOL, id="http:github")),
        agent_type_grant(
            "g-ended", "ExpiredAgent", portainer, lifetime=GrantLifetime.UNTIL, expires_at_utc=NOW - timedelta(minutes=1)
        ),
    ):
        store.create(grant)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as client:
        session = await (
            await client.post("/v1/account-link/sessions", headers=gateway_headers(), json=link_request())
        ).json()
        page = await (await client.get(f"/account-link/{session['session_id']}")).text()

    assert nobody_yet.startswith("No agent holds access to Portainer yet.")
    assert used_by_text(gate.access, tool, person) == "BackgroundOperationsAgent and TeamArchitect, when working for you."
    assert used_by_text(gate.access, tool, team) == (
        "BackgroundOperationsAgent, OtherPersonsAgent and TeamArchitect, "
        "when working for anyone in the team who has not linked their own account."
    )
    assert "<strong>Used by:</strong> BackgroundOperationsAgent and TeamArchitect, when working for you." in page
    assert "Where it goes:" in page and "How it is sent:" in page and "Reason:" in page


# -- use -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_requests_carry_the_linked_credential_only_in_the_linked_header(world, monkeypatch) -> None:
    gate, store = world
    monkeypatch.setenv("PORTAINER_TOKEN", "portainer-secret")
    save_credentials(CredentialOwnerScope.team(TEST_TEAM), team_portainer=token_credential("PORTAINER_TOKEN"))
    link(store, LinkOwnerKind.TEAM, TEST_TEAM, "team_portainer")
    sent: list = []

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, _limit: int) -> bytes:
            return b'{"echo": "portainer-secret"}'

    def fake_send(request, *, timeout):
        sent.append(request)
        return Response()

    monkeypatch.setattr("secure_tool_broker.credential_catalog._send_api_request", fake_send)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    body = {
        "provider": "secure_requests",
        "action": "get",
        "params": {"url": "https://portainer.example.test/api/stacks"},
        "reason": "list stacks",
        "context": agent_context(task_id="task-1"),
    }
    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post("/v1/tools/secure_requests", headers=caller_headers(), json=body)
        payload = await response.json()
        placeholder = await client.post(
            "/v1/tools/secure_requests",
            headers=caller_headers(),
            json={**body, "params": {**body["params"], "headers": {"X-Other": "${PORTAINER_TOKEN}"}}},
        )
        placeholder_payload = await placeholder.json()

    assert response.status == 200 and payload["status"] == "success", payload
    assert len(sent) == 1 and sent[0].get_header("X-api-key") == "portainer-secret"
    assert "portainer-secret" not in str(payload)
    assert payload["content"]["account_link_id"] == "account_link_team_team_portainer"
    assert placeholder_payload["content"]["error_code"] == "api_request_placeholders_unsupported"


def test_every_linked_account_tool_says_where_the_credential_goes() -> None:
    from access_catalog.catalog import load_catalog
    from custom_messages.contracts.access_v2 import AccountMode
    from secure_tool_broker.account_links import FIXED_CLI_FIELDS

    catalog = load_catalog(None)
    linked = [tool for tool in catalog.tools.values() if tool.account is AccountMode.LINKED_CREDENTIAL]

    assert linked, "the built-in catalog has tools that use linked accounts"
    for tool in linked:
        assert tool.credential_injections or tool.tool_id in FIXED_CLI_FIELDS, tool.tool_id
