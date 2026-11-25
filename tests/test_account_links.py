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
    LinkPurpose,
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


def link(
    store: AccessRepository,
    owner_kind: LinkOwnerKind,
    owner_id: str,
    key: str,
    tool_id: str = "http:portainer",
    *,
    injections: tuple[CredentialInjectionV1, ...] | None = None,
):
    resolved_injections = injections
    if resolved_injections is None:
        resolved_injections = (
            (CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-API-Key", field="api_token"),)
            if tool_id == "http:portainer"
            else ()
        )
    return store.create_link(
        AccountLinkV1(
            link_id=f"account_link_{owner_kind.value}_{key}",
            team_id=TEST_TEAM,
            tool_id=tool_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            credential_key=key,
            injections=resolved_injections,
            created_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
            created_at_utc=NOW,
        )
    )


# -- store and choice ----------------------------------------------------------------


def test_a_new_link_replaces_the_owners_previous_link_and_is_recorded(world) -> None:
    _gate, store = world
    first = link(store, LinkOwnerKind.PERSON, "user-1", "old_key")
    second = link(store, LinkOwnerKind.PERSON, "user-1", "new_key")

    assert (
        store.active_link(TEST_TEAM, "http:portainer", LinkOwnerKind.PERSON, "user-1", purpose=LinkPurpose.ACCOUNT)
        == second
    )
    assert store.get_link(first.link_id).status is AccountLinkStatus.REVOKED
    kinds = [event.kind for event in store.events(TEST_TEAM)]
    assert kinds.count("account_link_created") == 2 and "account_link_revoked" in kinds
    revoked = store.revoke_link(second.link_id, actor=SubjectRefV1(kind=SubjectKind.USER, id="user-1"), at=NOW)
    assert revoked.status is AccountLinkStatus.REVOKED
    assert store.list_links(TEST_TEAM, purpose=None) == []


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
    stored = store.active_link(
        TEST_TEAM, "http:portainer", LinkOwnerKind.PERSON, "user-1", purpose=LinkPurpose.ACCOUNT
    )
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
    assert store.list_links(TEST_TEAM, purpose=None) == []


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
    stored = store.active_link(
        TEST_TEAM, "http:portainer", LinkOwnerKind.PERSON, "user-1", purpose=LinkPurpose.ACCOUNT
    )
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
    link = store.active_link(
        TEST_TEAM, "http:portainer", LinkOwnerKind.TEAM, TEST_TEAM, purpose=LinkPurpose.ACCOUNT
    )
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
    assert sealbox == {} and store.list_links(TEST_TEAM, purpose=None) == []
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
    assert store.list_links(TEST_TEAM, purpose=None) == []


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


@pytest.mark.asyncio
async def test_gated_web_request_requires_gate_link_sends_both_credentials_when_private_and_redacts_both(
    tmp_path: Path, monkeypatch
) -> None:
    from access_catalog.catalog import CatalogSource
    from access_control.account_sharing import TeamAccountSharing
    from custom_messages.contracts.access_v2 import AccountSharing
    from secure_tool_broker.access_gate import REASON_GATE_LINK_MISSING

    instance_root = tmp_path / "instance"
    tools_dir = instance_root / "control" / "access" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "proposed-gitea.yaml").write_text(
        """
tools:
  - tool_id: http:gitea
    display_name: Gitea
    account: linked_credential
    hosts: [gitea.example.com]
    credential_injections:
      - placement: header
        name: Authorization
        field: api_token
        template: "token {value}"
    gate_injections:
      - placement: header
        name: CF-Access-Client-Secret
        field: client_secret
    routes:
      - name: repos.read
        method: GET
        path: /api/**
        level: read
""",
        encoding="utf-8",
    )
    store = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )
    catalogs = CatalogSource(instance_root)
    gate = BrokerAccessGate(
        catalogs=catalogs,
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEST_TEAM,
        access=None,
        links=store,
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "catalog.db"))
    monkeypatch.setenv("GITEA_USER_TOKEN", "alice-gitea-secret")
    monkeypatch.setenv("GITEA_GATE_SECRET", "team-cf-gate-secret")
    save_credentials(CredentialOwnerScope.user("user-1"), alice_gitea=token_credential("GITEA_USER_TOKEN"))
    save_credentials(
        CredentialOwnerScope.team(TEST_TEAM),
        team_gitea_gate=token_credential("GITEA_GATE_SECRET", field="client_secret"),
    )
    tool = catalogs.current().tools["http:gitea"]
    TeamAccountSharing(store, TEST_TEAM).choose_tool(tool, AccountSharing.PRIVATE, by="user-1")
    store.create_link(
        AccountLinkV1(
            link_id="account_link_person_gitea",
            team_id=TEST_TEAM,
            tool_id="http:gitea",
            owner_kind=LinkOwnerKind.PERSON,
            owner_id="user-1",
            purpose=LinkPurpose.ACCOUNT,
            credential_key="alice_gitea",
            injections=tool.credential_injections,
            created_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
            created_at_utc=NOW,
        )
    )

    sent: list = []

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, _limit: int) -> bytes:
            return b'{"token": "alice-gitea-secret", "gate": "team-cf-gate-secret"}'

    def fake_send(request, *, timeout):
        sent.append(request)
        return Response()

    monkeypatch.setattr("secure_tool_broker.credential_catalog._send_api_request", fake_send)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    body = {
        "provider": "secure_requests",
        "action": "get",
        "params": {"url": "https://gitea.example.com/api/v1/repos"},
        "reason": "list repos",
        "context": agent_context(task_id="task-gate-1"),
    }

    async with TestClient(TestServer(service.build_app())) as client:
        # Without a gate link: refused before sending, naming /link http:gitea gate.
        missing_resp = await client.post("/v1/tools/secure_requests", headers=caller_headers(), json=body)
        missing_payload = json.loads(await missing_resp.text())
        assert missing_resp.status == 403
        assert missing_payload["reason_code"] == REASON_GATE_LINK_MISSING
        assert "/link http:gitea gate" in missing_payload["next_step"]
        assert sent == []

        # Link the team gate credential: even with PRIVATE account sharing, both headers are sent and redacted.
        store.create_link(
            AccountLinkV1(
                link_id="account_link_team_gitea_gate",
                team_id=TEST_TEAM,
                tool_id="http:gitea",
                owner_kind=LinkOwnerKind.TEAM,
                owner_id=TEST_TEAM,
                purpose=LinkPurpose.GATE,
                credential_key="team_gitea_gate",
                injections=tool.gate_injections,
                created_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
                created_at_utc=NOW,
            )
        )
        ok_resp = await client.post("/v1/tools/secure_requests", headers=caller_headers(), json=body)
        ok_payload = await ok_resp.json()
        assert ok_resp.status == 200 and ok_payload["status"] == "success", ok_payload
        assert len(sent) == 1
        sent_headers = {key.lower(): value for key, value in sent[0].header_items()}
        assert sent_headers["authorization"] == "token alice-gitea-secret"
        assert sent_headers["cf-access-client-secret"] == "team-cf-gate-secret"
        assert "alice-gitea-secret" not in str(ok_payload)
        assert "team-cf-gate-secret" not in str(ok_payload)
        assert ok_payload["content"]["account_link_id"] == "account_link_person_gitea"
        assert ok_payload["content"]["gate_link_id"] == "account_link_team_gitea_gate"

        # Agent-supplied gate header is refused without reaching the network.
        spoof_resp = await client.post(
            "/v1/tools/secure_requests",
            headers=caller_headers(),
            json={
                **body,
                "params": {**body["params"], "headers": {"CF-Access-Client-Secret": "agent-value"}},
            },
        )
        spoof_payload = await spoof_resp.json()
        assert spoof_payload["status"] == "error"
        assert spoof_payload["content"]["error_code"] == "api_request_header_reserved"
        assert len(sent) == 1


def test_every_linked_account_tool_says_where_the_credential_goes() -> None:
    from access_catalog.catalog import load_catalog
    from custom_messages.contracts.access_v2 import AccountMode
    from secure_tool_broker.account_links import FIXED_CLI_FIELDS

    catalog = load_catalog(None)
    linked = [tool for tool in catalog.tools.values() if tool.account is AccountMode.LINKED_CREDENTIAL]

    assert linked, "the built-in catalog has tools that use linked accounts"
    for tool in linked:
        assert tool.credential_injections or tool.tool_id in FIXED_CLI_FIELDS, tool.tool_id


def test_team_account_and_gate_links_coexist_and_replace_independently(world) -> None:
    import sqlite3

    _gate, store = world
    team_account = link(store, LinkOwnerKind.TEAM, TEST_TEAM, "team_account_key")
    team_gate = store.create_link(
        AccountLinkV1(
            link_id="account_link_team_gate_1",
            team_id=TEST_TEAM,
            tool_id="http:portainer",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEST_TEAM,
            purpose=LinkPurpose.GATE,
            credential_key="gate_key_1",
            injections=(
                CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate-Secret", field="gate_secret"),
            ),
            created_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
            created_at_utc=NOW,
        )
    )

    assert (
        store.active_link(TEST_TEAM, "http:portainer", LinkOwnerKind.TEAM, TEST_TEAM, purpose=LinkPurpose.ACCOUNT)
        == team_account
    )
    assert (
        store.active_link(TEST_TEAM, "http:portainer", LinkOwnerKind.TEAM, TEST_TEAM, purpose=LinkPurpose.GATE)
        == team_gate
    )
    assert [item.link_id for item in store.list_links(TEST_TEAM, purpose=LinkPurpose.ACCOUNT)] == [team_account.link_id]
    assert [item.link_id for item in store.list_links(TEST_TEAM, purpose=LinkPurpose.GATE)] == [team_gate.link_id]
    assert len(store.list_links(TEST_TEAM, purpose=None)) == 2

    # Replacing the gate link revokes only the old gate link; the account link stays active.
    team_gate_2 = store.create_link(
        team_gate.model_copy(update={"link_id": "account_link_team_gate_2", "credential_key": "gate_key_2"})
    )
    assert store.get_link(team_gate.link_id).status is AccountLinkStatus.REVOKED
    assert store.get_link(team_account.link_id).status is AccountLinkStatus.ACTIVE
    assert (
        store.active_link(TEST_TEAM, "http:portainer", LinkOwnerKind.TEAM, TEST_TEAM, purpose=LinkPurpose.GATE)
        == team_gate_2
    )
    assert any(
        event.kind == "account_link_created" and event.detail.get("purpose") == "gate"
        for event in store.events(TEST_TEAM)
    )

    # The partial unique index still refuses a second active gate link for the same tool.
    with sqlite3.connect(str(store.settings.sqlite_path)) as conn:
        row = conn.execute(
            "SELECT * FROM access_account_links WHERE link_id = ?", (team_gate_2.link_id,)
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO access_account_links VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("account_link_team_gate_dup", *row[1:]),
            )


def test_pre_purpose_access_store_upgrades_in_place_and_reads_existing_links_as_account(
    tmp_path: Path, caplog
) -> None:
    import sqlite3

    from storage.access_repository import ONE_ACTIVE_LINK_INDEX
    from storage.config import resolve_storage_settings

    db_path = tmp_path / "pre_purpose.db"
    legacy_payload = json.dumps(
        {
            "schema_version": 1,
            "link_id": "link_legacy_team",
            "team_id": TEST_TEAM,
            "tool_id": "http:portainer",
            "owner_kind": "team",
            "owner_id": TEST_TEAM,
            "credential_key": "portainer_team",
            "injections": [{"placement": "header", "name": "X-API-Key", "field": "api_token"}],
            "status": "active",
            "created_by": {"kind": "user", "id": "user-1"},
            "created_at_utc": NOW.isoformat(),
            "revoked_at_utc": None,
            "revoked_by": None,
        }
    )
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """CREATE TABLE access_account_links (
                link_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, tool_id TEXT NOT NULL,
                owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, status TEXT NOT NULL,
                revoked_at TEXT, revoked_by_json TEXT,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            "CREATE UNIQUE INDEX access_account_links_one_active "
            "ON access_account_links(team_id, tool_id, owner_kind, owner_id) "
            "WHERE status = 'active'"
        )
        conn.execute(
            "INSERT INTO access_account_links "
            "(link_id, team_id, tool_id, owner_kind, owner_id, status, payload_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("link_legacy_team", TEST_TEAM, "http:portainer", "team", TEST_TEAM, "active", legacy_payload, NOW.isoformat()),
        )

    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db_path)}})
    with caplog.at_level(logging.INFO, logger="storage.access_repository"):
        store = AccessRepository(settings)

    upgraded = store.active_link(
        TEST_TEAM, "http:portainer", LinkOwnerKind.TEAM, TEST_TEAM, purpose=LinkPurpose.ACCOUNT
    )
    assert upgraded is not None
    assert upgraded.link_id == "link_legacy_team"
    assert upgraded.purpose is LinkPurpose.ACCOUNT
    assert "event=access_account_links_purpose_added links=1" in caplog.text

    with sqlite3.connect(str(db_path)) as conn:
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        stored_json = json.loads(
            conn.execute("SELECT payload_json FROM access_account_links WHERE link_id='link_legacy_team'").fetchone()[0]
        )
    assert ONE_ACTIVE_LINK_INDEX in indexes
    assert "access_account_links_one_active" not in indexes
    assert stored_json["purpose"] == "account"

    # With the old index dropped and the purpose-scoped index in place, a team gate link coexists with the account link.
    gate_link = store.create_link(
        AccountLinkV1(
            link_id="link_gate_team",
            team_id=TEST_TEAM,
            tool_id="http:portainer",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEST_TEAM,
            purpose=LinkPurpose.GATE,
            credential_key="portainer_gate",
            injections=(CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate", field="secret"),),
            created_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
            created_at_utc=NOW,
        )
    )
    assert (
        store.active_link(TEST_TEAM, "http:portainer", LinkOwnerKind.TEAM, TEST_TEAM, purpose=LinkPurpose.GATE)
        == gate_link
    )
    assert (
        store.active_link(TEST_TEAM, "http:portainer", LinkOwnerKind.TEAM, TEST_TEAM, purpose=LinkPurpose.ACCOUNT)
        == upgraded
    )

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="storage.access_repository"):
        AccessRepository(settings)
    assert "access_account_links_purpose_added" not in caplog.text


@pytest.mark.asyncio
async def test_gate_link_form_is_admin_only_ignores_sharing_and_works_with_auth_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from access_catalog.catalog import CatalogSource
    from access_catalog.instance_tools import write_instance_tool
    from access_control.account_sharing import TeamAccountSharing
    from access_control.caller_tokens import CALLER_GATEWAY
    from access_control.identity import ContextVerifier
    from custom_messages.contracts.access_v2 import (
        AccessLevel,
        AccountSharing,
        HttpRouteV1,
        ProposedHttpToolV1,
    )
    from secure_tool_broker.access_gate import BrokerAccessGate
    from secure_tool_broker.service import SecureToolBrokerService
    from storage.access_repository import AccessRepository
    from storage.config import resolve_storage_settings
    from tests.broker_access_support import BROKER_ROOT, caller_headers, context_verify_keys

    instance_root = tmp_path / "instance"
    write_instance_tool(
        instance_root,
        ProposedHttpToolV1(
            tool_id="http:gitea",
            display_name="Gitea",
            description="Self-hosted Git behind a Cloudflare gate.",
            hosts=("gitea.example.com",),
            credential_injections=(
                CredentialInjectionV1(
                    placement=InjectionPlacement.HEADER,
                    name="Authorization",
                    field="api_token",
                    template="token {value}",
                ),
            ),
            gate_injections=(
                CredentialInjectionV1(
                    placement=InjectionPlacement.HEADER,
                    name="CF-Access-Client-Secret",
                    field="client_secret",
                ),
            ),
            routes=(HttpRouteV1(name="api.read", method="GET", path="/api/v1/**", level=AccessLevel.READ),),
        ),
    )
    write_instance_tool(
        instance_root,
        ProposedHttpToolV1(
            tool_id="http:public_gated",
            display_name="Public Gated Docs",
            description="Public API behind a team gate with no user account.",
            hosts=("docs.example.com",),
            uses_credential=False,
            gate_injections=(
                CredentialInjectionV1(
                    placement=InjectionPlacement.HEADER,
                    name="X-Gate-Key",
                    field="gate_key",
                ),
            ),
            routes=(HttpRouteV1(name="docs.read", method="GET", path="/**", level=AccessLevel.READ),),
        ),
    )

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    store = AccessRepository(storage)
    catalogs = CatalogSource(instance_root)
    # Authorization off (access=None) and private sharing: gate links still work and remain admin-only.
    gate = BrokerAccessGate(
        catalogs=catalogs,
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEST_TEAM,
        access=None,
        links=store,
    )
    gitea_tool = catalogs.current().tools["http:gitea"]
    TeamAccountSharing(store, TEST_TEAM).choose_tool(gitea_tool, AccountSharing.PRIVATE, by="admin-1")

    saved_env_values: dict[str, dict[str, str]] = {}

    def fake_collect(spec, values, *, scope):
        from secure_tool_broker.credential_catalog import (
            CatalogCredential,
            CatalogCredentialField,
            CatalogSourceRef,
            load_catalog,
            save_catalog,
        )
        from secure_tools.providers import ToolResult

        key = spec["credential_key"]
        saved_env_values[key] = dict(values)
        existing = load_catalog(scope=scope)
        fields = {}
        for field_spec in spec["fields"]:
            fname = field_spec["name"]
            env_var = f"TEST_{key.upper().replace('-', '_')}_{fname.upper()}"
            monkeypatch.setenv(env_var, values[fname])
            fields[fname] = CatalogCredentialField(
                kind="secret",
                visibility="secret",
                resolution_order=[CatalogSourceRef(source="env", env_var=env_var)],
            )
        existing.credentials[key] = CatalogCredential(
            display_name=spec["display_name"],
            purpose=spec["purpose"],
            fields=fields,
        )
        save_catalog(existing, scope=scope)
        return ToolResult(status="success", content={"credential_key": key})

    monkeypatch.setattr("secure_tool_broker.account_links.collect_credential", fake_collect)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    gw_headers = caller_headers(CALLER_GATEWAY)

    async with TestClient(TestServer(service.build_app())) as client:
        # Non-admin member is refused from opening a gate link form.
        member_resp = await client.post(
            "/v1/account-link/sessions",
            headers=gw_headers,
            json={
                "request_id": "req_gate_member",
                "team_id": TEST_TEAM,
                "tool_id": "http:gitea",
                "owner_kind": "team",
                "purpose": "gate",
                "person_id": "member-1",
                "person_roles": ["member"],
                "reason": "Link gate",
            },
        )
        member_err = json.loads(await member_resp.text())
        assert member_resp.status == 403
        assert member_err["reason_code"] == "account_link_team_admin_required"

        # Tool without gate_injections is refused.
        no_gate_resp = await client.post(
            "/v1/account-link/sessions",
            headers=gw_headers,
            json={
                "request_id": "req_gate_none",
                "team_id": TEST_TEAM,
                "tool_id": "http:github",
                "owner_kind": "team",
                "purpose": "gate",
                "person_id": "admin-1",
                "person_roles": ["admin"],
                "reason": "Link gate",
            },
        )
        no_gate_err = json.loads(await no_gate_resp.text())
        assert no_gate_resp.status == 403
        assert no_gate_err["reason_code"] == "account_link_gate_not_needed"

        # Administrator opens the gate form for private http:gitea, sees gate disclosure, and submits a new credential.
        created = await client.post(
            "/v1/account-link/sessions",
            headers=gw_headers,
            json={
                "request_id": "req_gate_admin",
                "team_id": TEST_TEAM,
                "tool_id": "http:gitea",
                "owner_kind": "team",
                "purpose": "gate",
                "person_id": "admin-1",
                "person_roles": ["admin"],
                "reason": "Cloudflare gate",
            },
        )
        assert created.status == 200
        created_data = await created.json()
        session_id = created_data["session_id"]

        form_resp = await client.get(f"/account-link/{session_id}")
        form_html = await form_resp.text()
        assert form_resp.status == 200
        assert "Team gate credential" in form_html
        assert "every request to Gitea" in form_html
        assert "gitea.example.com" in form_html
        assert "CF-Access-Client-Secret" in form_html
        assert "field__client_secret" in form_html

        submit_resp = await client.post(
            f"/account-link/{session_id}",
            data={
                "credential_source": "new",
                "field__client_secret": "CF-TOP-SECRET",
                "confirm": "yes",
            },
        )
        submit_html = await submit_resp.text()
        assert submit_resp.status == 200
        assert "http-gitea-gate" in submit_html
        assert "CF-TOP-SECRET" not in submit_html

        status_resp = await client.get(f"/v1/account-link/sessions/{session_id}", headers=gw_headers)
        status_data = await status_resp.json()
        assert status_data["status"] == "account_link_created"
        assert status_data["result"]["purpose"] == "gate"
        assert status_data["result"]["owner_kind"] == "team"
        assert "CF-TOP-SECRET" not in json.dumps(status_data)

        # Gate-only tool (account=none) also works through the same admin form.
        public_created = await client.post(
            "/v1/account-link/sessions",
            headers=gw_headers,
            json={
                "request_id": "req_public_gate_admin",
                "team_id": TEST_TEAM,
                "tool_id": "http:public_gated",
                "owner_kind": "team",
                "purpose": "gate",
                "person_id": "admin-1",
                "person_roles": ["owner"],
                "reason": "Public docs gate",
            },
        )
        assert public_created.status == 200
        public_session = (await public_created.json())["session_id"]
        pub_submit = await client.post(
            f"/account-link/{public_session}",
            data={"credential_source": "new", "field__gate_key": "DOCS-GATE-VAL", "confirm": "yes"},
        )
        assert pub_submit.status == 200

    stored_gitea_gate = store.active_link(TEST_TEAM, "http:gitea", LinkOwnerKind.TEAM, TEST_TEAM, purpose=LinkPurpose.GATE)
    assert stored_gitea_gate is not None
    assert stored_gitea_gate.credential_key == "http-gitea-gate"
    assert stored_gitea_gate.injections == gitea_tool.gate_injections
    assert store.active_link(TEST_TEAM, "http:gitea", LinkOwnerKind.TEAM, TEST_TEAM, purpose=LinkPurpose.ACCOUNT) is None
    stored_docs_gate = store.active_link(
        TEST_TEAM, "http:public_gated", LinkOwnerKind.TEAM, TEST_TEAM, purpose=LinkPurpose.GATE
    )
    assert stored_docs_gate is not None
    assert saved_env_values["http-gitea-gate"] == {"client_secret": "CF-TOP-SECRET"}


@pytest.mark.asyncio
async def test_link_form_pins_shown_definition_and_refuses_when_tool_changes_before_submit(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """F4 (R3): changing a tool's hosts or injections between GET and POST refuses linking for both account and gate forms."""

    import os
    import re
    import time
    from types import SimpleNamespace

    from access_catalog.catalog import CatalogSource
    from secure_tool_broker.account_links import DEFINITION_DIGEST_FIELD, REASON_DEFINITION_CHANGED

    instance_root = tmp_path / "instance"
    tools_dir = instance_root / "control" / "access" / "tools"
    tools_dir.mkdir(parents=True)
    tool_file = tools_dir / "proposed-http-gitea.yaml"

    def write_def(yaml_text: str, *, bump_ns: int) -> None:
        tool_file.write_text(yaml_text, encoding="utf-8")
        stamp = time.time_ns() + bump_ns
        os.utime(tool_file, ns=(stamp, stamp))

    def_a = """
tools:
  - tool_id: http:gitea
    display_name: Gitea
    account: linked_credential
    hosts: [gitea.example.com]
    credential_injections:
      - {placement: header, name: Authorization, field: api_token, template: "token {value}"}
    gate_injections:
      - {placement: header, name: CF-Access-Client-Secret, field: client_secret}
"""
    def_b = """
tools:
  - tool_id: http:gitea
    display_name: Gitea
    account: linked_credential
    hosts: [gitea.example.com, mirror.example.net]
    credential_injections:
      - {placement: query, name: access_token, field: api_token}
    gate_injections:
      - {placement: query, name: cf_secret, field: client_secret}
"""
    def_a_reordered_description = """
tools:
  - tool_id: http:gitea
    display_name: Gitea
    description: Updated description only.
    account: linked_credential
    hosts: [gitea.example.com]
    credential_injections:
      - {placement: header, name: authorization, field: api_token, template: "token {value}"}
    gate_injections:
      - {placement: header, name: cf-access-client-secret, field: client_secret}
"""
    write_def(def_a, bump_ns=0)

    store = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )
    gate = BrokerAccessGate(
        catalogs=CatalogSource(instance_root),
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEST_TEAM,
        access=None,
        links=store,
    )
    monkeypatch.setenv("GITEA_TOKEN", "personal-secret")
    save_credentials(CredentialOwnerScope.user("user-1"), gitea_personal=token_credential("GITEA_TOKEN"))
    stored_new: list[str] = []

    def fake_collect(payload, values, *, scope):
        stored_new.append(payload["credential_key"])
        return SimpleNamespace(status="success", content={}, error=None)

    monkeypatch.setattr("secure_tool_broker.account_links.collect_credential", fake_collect)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    caplog.set_level(logging.WARNING)

    async with TestClient(TestServer(service.build_app())) as client:
        # 1. Account link form: GET shows definition A, file changes to B before POST -> refused.
        acct_created = await (
            await client.post(
                "/v1/account-link/sessions",
                headers=gateway_headers(),
                json={
                    "request_id": "req-pin-account",
                    "team_id": TEST_TEAM,
                    "tool_id": "http:gitea",
                    "owner_kind": "person",
                    "purpose": "account",
                    "person_id": "user-1",
                    "person_roles": ["member"],
                    "reason": "Link Gitea",
                },
            )
        ).json()
        acct_sid = acct_created["session_id"]
        acct_form = await (await client.get(f"/account-link/{acct_sid}")).text()
        assert "Authorization" in acct_form and "mirror.example.net" not in acct_form
        digest_match = re.search(rf'name="{DEFINITION_DIGEST_FIELD}" value="([0-9a-f]{{64}})"', acct_form)
        assert digest_match is not None

        # Open a gate session at definition A too, then replace the file with definition B.
        gate_created = await (
            await client.post(
                "/v1/account-link/sessions",
                headers=gateway_headers(),
                json={
                    "request_id": "req-pin-gate",
                    "team_id": TEST_TEAM,
                    "tool_id": "http:gitea",
                    "owner_kind": "team",
                    "purpose": "gate",
                    "person_id": "admin-1",
                    "person_roles": ["admin"],
                    "reason": "Link Gitea gate",
                },
            )
        ).json()
        gate_sid = gate_created["session_id"]
        gate_form = await (await client.get(f"/account-link/{gate_sid}")).text()
        assert "CF-Access-Client-Secret" in gate_form and "mirror.example.net" not in gate_form

        write_def(def_b, bump_ns=2_000_000_000)

        acct_submit = await client.post(
            f"/account-link/{acct_sid}",
            data={"credential_key": "gitea_personal", "confirm": "yes"},
        )
        assert acct_submit.status == 200
        acct_status = await (await client.get(f"/v1/account-link/sessions/{acct_sid}", headers=gateway_headers())).json()
        assert acct_status["status"] == "account_link_unavailable"
        assert acct_status["result"]["reason_code"] == REASON_DEFINITION_CHANGED
        assert "/link http:gitea" in acct_status["result"]["error"]

        # Submitting a new gate credential after the change refuses BEFORE storing the credential.
        gate_submit = await client.post(
            f"/account-link/{gate_sid}",
            data={"credential_source": "new", "field__client_secret": "gate-secret", "confirm": "yes"},
        )
        assert gate_submit.status == 200
        gate_status = await (await client.get(f"/v1/account-link/sessions/{gate_sid}", headers=gateway_headers())).json()
        assert gate_status["status"] == "account_link_unavailable"
        assert gate_status["result"]["reason_code"] == REASON_DEFINITION_CHANGED
        assert "/link http:gitea gate" in gate_status["result"]["error"]
        assert stored_new == [], "nothing is stored in the credential store when the definition changed"
        assert store.list_links(TEST_TEAM, purpose=None) == []

        # 2. Stale rendered tab: GET 1 shows B, file changes back to A, GET 2 refreshes session to A,
        #    submitting form with B's definition_digest is still refused.
        tab_created = await (
            await client.post(
                "/v1/account-link/sessions",
                headers=gateway_headers(),
                json={
                    "request_id": "req-pin-stale-tab",
                    "team_id": TEST_TEAM,
                    "tool_id": "http:gitea",
                    "owner_kind": "person",
                    "purpose": "account",
                    "person_id": "user-1",
                    "person_roles": ["member"],
                    "reason": "Stale tab check",
                },
            )
        ).json()
        tab_sid = tab_created["session_id"]
        tab_page_b = await (await client.get(f"/account-link/{tab_sid}")).text()
        digest_b = re.search(rf'name="{DEFINITION_DIGEST_FIELD}" value="([0-9a-f]{{64}})"', tab_page_b)
        assert digest_b is not None
        write_def(def_a, bump_ns=4_000_000_000)
        await client.get(f"/account-link/{tab_sid}")  # second tab refreshes session to A
        await client.post(
            f"/account-link/{tab_sid}",
            data={
                "credential_key": "gitea_personal",
                DEFINITION_DIGEST_FIELD: digest_b.group(1),
                "confirm": "yes",
            },
        )
        tab_status = await (await client.get(f"/v1/account-link/sessions/{tab_sid}", headers=gateway_headers())).json()
        assert tab_status["status"] == "account_link_unavailable"
        assert tab_status["result"]["reason_code"] == REASON_DEFINITION_CHANGED

        # 3. Unrelated change (description + header case only) still succeeds.
        ok_created = await (
            await client.post(
                "/v1/account-link/sessions",
                headers=gateway_headers(),
                json={
                    "request_id": "req-pin-unchanged",
                    "team_id": TEST_TEAM,
                    "tool_id": "http:gitea",
                    "owner_kind": "person",
                    "purpose": "account",
                    "person_id": "user-1",
                    "person_roles": ["member"],
                    "reason": "Unchanged hosts and injections",
                },
            )
        ).json()
        ok_sid = ok_created["session_id"]
        ok_page = await (await client.get(f"/account-link/{ok_sid}")).text()
        ok_digest = re.search(rf'name="{DEFINITION_DIGEST_FIELD}" value="([0-9a-f]{{64}})"', ok_page)
        assert ok_digest is not None
        write_def(def_a_reordered_description, bump_ns=6_000_000_000)
        await client.post(
            f"/account-link/{ok_sid}",
            data={
                "credential_key": "gitea_personal",
                DEFINITION_DIGEST_FIELD: ok_digest.group(1),
                "confirm": "yes",
            },
        )
        ok_status = await (await client.get(f"/v1/account-link/sessions/{ok_sid}", headers=gateway_headers())).json()
        assert ok_status["status"] == "account_link_created"

    assert "event=account_link_definition_changed" in caplog.text


def test_read_only_access_repository_reads_pre_upgrade_links_without_purpose_column(tmp_path: Path) -> None:
    """F5: opening a pre-upgrade SQLite DB read-only must not crash on missing access_account_links.purpose."""

    import sqlite3

    db_path = tmp_path / "pre_upgrade_access.db"
    link = AccountLinkV1(
        link_id="link_pre_upgrade_ro",
        team_id=TEST_TEAM,
        tool_id="http:portainer",
        owner_kind=LinkOwnerKind.PERSON,
        owner_id="user-1",
        credential_key="portainer_token",
        created_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
        created_at_utc=NOW,
    )
    payload_without_purpose = link.model_dump(mode="json")
    payload_without_purpose.pop("purpose", None)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE access_account_links (
                link_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                tool_id TEXT NOT NULL,
                owner_kind TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL,
                revoked_at TEXT,
                revoked_by_json TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO access_account_links
            (link_id, team_id, tool_id, owner_kind, owner_id, status, revoked_at, revoked_by_json, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                link.link_id,
                link.team_id,
                link.tool_id,
                link.owner_kind.value,
                link.owner_id,
                "active",
                json.dumps(payload_without_purpose, sort_keys=True),
                NOW.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    from storage.read_only import read_only_repositories

    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db_path)}})
    with read_only_repositories():
        ro_store = AccessRepository(settings)
        links = ro_store.list_links(TEST_TEAM, purpose=None)
        assert len(links) == 1
        assert links[0].link_id == "link_pre_upgrade_ro"
        assert links[0].purpose is LinkPurpose.ACCOUNT
        fetched = ro_store.get_link("link_pre_upgrade_ro")
        assert fetched is not None and fetched.purpose is LinkPurpose.ACCOUNT


@pytest.mark.asyncio
async def test_real_broker_api_request_sends_default_user_agent_and_respects_explicit_header(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10: through SecureToolBrokerService, credential.api_request sends DEFAULT_API_REQUEST_USER_AGENT unless supplied."""

    import io
    from types import SimpleNamespace

    from secure_tool_broker.defaults import DEFAULT_API_REQUEST_USER_AGENT

    gate, store = world
    monkeypatch.setenv("PORTAINER_TOKEN", "portainer-secret")
    save_credentials(
        CredentialOwnerScope.user("user-1"),
        portainer_token=token_credential("PORTAINER_TOKEN"),
    )
    tool = gate.catalog.tools["http:portainer"]
    store.create_link(
        AccountLinkV1(
            link_id="link_ua_test",
            team_id=TEST_TEAM,
            tool_id="http:portainer",
            owner_kind=LinkOwnerKind.PERSON,
            owner_id="user-1",
            purpose=LinkPurpose.ACCOUNT,
            credential_key="portainer_token",
            injections=tool.credential_injections,
            created_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
            created_at_utc=NOW,
        )
    )

    captured_requests: list = []

    class _FakeResp:
        def __init__(self) -> None:
            self.status = self.code = 200
            self.headers = SimpleNamespace(
                get=lambda name, default="": "application/json" if name.lower() == "content-type" else default,
                get_content_charset=lambda default="utf-8": "utf-8",
            )
            self._buf = io.BytesIO(b'{"ok": true}')

        def read(self, n: int = -1) -> bytes:
            return self._buf.read(n)

        def getcode(self) -> int:
            return 200

        def close(self) -> None:
            self._buf.close()

    def fake_send(req, *, timeout):
        captured_requests.append(req)
        return _FakeResp()

    monkeypatch.setattr(credential_catalog, "_send_api_request", fake_send)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)

    async with TestClient(TestServer(service.build_app())) as client:
        # 1. No User-Agent header -> broker sends browser-like default.
        r1 = await client.post(
            "/v1/tools/credential_catalog",
            headers=caller_headers(),
            json={
                "provider": "credential_catalog",
                "action": "credential.api_request",
                "params": {"url": "https://portainer.example.test/api/status"},
                "reason": "Check status",
                "context": agent_context("user-1"),
            },
        )
        assert r1.status == 200
        body1 = await r1.json()
        assert body1["status"] == "success"

        # 2. Explicit User-Agent header -> agent's header wins.
        r2 = await client.post(
            "/v1/tools/credential_catalog",
            headers=caller_headers(),
            json={
                "provider": "credential_catalog",
                "action": "credential.api_request",
                "params": {
                    "url": "https://portainer.example.test/api/status",
                    "headers": {"User-Agent": "CustomClient/2.0"},
                },
                "reason": "Check status with custom UA",
                "context": agent_context("user-1"),
            },
        )
        assert r2.status == 200
        body2 = await r2.json()
        assert body2["status"] == "success"

    assert len(captured_requests) == 2
    headers1 = {k.lower(): v for k, v in captured_requests[0].header_items()}
    headers2 = {k.lower(): v for k, v in captured_requests[1].header_items()}
    assert headers1["user-agent"] == DEFAULT_API_REQUEST_USER_AGENT
    assert headers2["user-agent"] == "CustomClient/2.0"
