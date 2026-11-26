"""Tests for immediate expired-credential enforcement on every broker read path (A3, Feature 4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_catalog.catalog import CatalogSource
from access_control.account_sharing import TeamAccountSharing
from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller
from access_control.identity import ContextVerifier
from access_control.tool_setup_states import STEP_ACCOUNT_LINK, STEP_GATE_LINK, ToolSetupStates, verdict_from_status
from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.contracts.access_v2 import (
    REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED,
    AccessLevel,
    AccountLinkRequestV1,
    AccountLinkV1,
    AccountMode,
    AccountSharing,
    CredentialInjectionV1,
    InjectionPlacement,
    LinkOwnerKind,
    LinkPurpose,
    OperationResolutionV1,
    SubjectKind,
    SubjectRefV1,
    ToolDefinitionV1,
    ToolKind,
    ToolSetupState,
)
from custom_messages.contracts.cli_tool_v1 import InstalledCliV1
from custom_messages.contracts.common_v1 import PrincipalDescriptorV1, PrincipalType
from custom_messages.contracts.mcp_v1 import (
    MCPDiscoveryRequestV1,
    MCPInvocationRequestV1,
)
from mcp_control.registry import MCPOperationDefinitionV1, reconcile_instance_mcp_connections
from secure_tool_broker import (
    credential_catalog,
    hub_cli_executor,
    installed_clis,
    mcp_runtime,
)
from secure_tool_broker.access_gate import BrokerAccessGate
from secure_tool_broker.account_links import (
    AccountLinkRefused,
    AccountLinkSession,
    create_link,
    credential_choices,
    render_form,
)
from secure_tool_broker.credential_catalog import (
    CatalogCredential,
    CatalogCredentialField,
    CatalogSourceRef,
    CredentialOwnerScope,
    api_request,
    bind_for_openshell,
    get_credential_metadata,
    read_credential_metadata_readonly,
    resolve_secret_field,
)
from secure_tool_broker.credential_diagnostics import CREDENTIAL_EXPIRED
from secure_tool_broker.credential_expiry import format_expiry_label, format_utc_timestamp
from secure_tool_broker.credential_transfer import export_credentials
from secure_tool_broker.mcp_config import BrokerMCPConfig
from secure_tool_broker.mcp_runtime import BrokerMCPRuntime, MCPAccessRefused
from secure_tool_broker.tool_setup_status import tool_status_entry
from secure_tools.hub_providers import AUTH_STATUS, HF_PROVIDER
from secure_tools.providers import ToolContext, ValidatedAction
from storage.access_repository import AccessRepository
from tests.broker_access_support import TEST_TEAM, access_store, context_verify_keys, signed_task_context

TEAM_ID = TEST_TEAM
PERSON_ID = "person-expiry-1"
USER_SCOPE = CredentialOwnerScope.user(PERSON_ID)
TEAM_SCOPE = CredentialOwnerScope.team(TEAM_ID)
BASE_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
PAST_EXPIRY = format_utc_timestamp(datetime.now(timezone.utc) - timedelta(minutes=10))
FUTURE_EXPIRY = format_utc_timestamp(datetime.now(timezone.utc) + timedelta(hours=4))


@pytest.fixture(autouse=True)
def _isolated_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "catalog.db"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path / "creds"))
    credential_catalog._SECRET_CACHE.clear()
    yield
    credential_catalog._SECRET_CACHE.clear()


def _save_temp_credential(
    key: str,
    *,
    scope: CredentialOwnerScope,
    expires_at: str | None,
    field_name: str = "api_token",
    display_name: str = "Temp Service",
    purpose: str = "Short-lived credential",
) -> CatalogCredential:
    cred = CatalogCredential(
        display_name=display_name,
        purpose=purpose,
        expires_at=expires_at,
        fields={
            "username": CatalogCredentialField(
                kind="username",
                visibility="metadata_visible",
                value_source="user_input",
                value="alice",
            ),
            field_name: CatalogCredentialField(
                kind="token",
                visibility="secret",
                value_source="user_input",
                resolution_order=[
                    CatalogSourceRef(
                        source="sealbox",
                        item_id=f"team-nats-agents:default:{key}:{field_name}",
                        item_name=f"team-nats-agents:default:{key}:{field_name}",
                        field_ref="value",
                    )
                ],
            ),
        },
    )
    credential_catalog._save_catalog_credential(key, cred, scope=scope)
    return cred


def _http_tool(
    tool_id: str = "http:demo_api",
    *,
    link_owners: tuple[str, ...] = ("person", "team"),
    gate_injections: tuple[CredentialInjectionV1, ...] = (),
    account: AccountMode = AccountMode.LINKED_CREDENTIAL,
) -> ToolDefinitionV1:
    return ToolDefinitionV1(
        tool_id=tool_id,
        kind=ToolKind.HTTP,
        display_name="Demo API",
        hosts=("api.demo.example",),
        account=account,
        link_owners=link_owners,
        credential_injections=(
            CredentialInjectionV1(
                placement=InjectionPlacement.HEADER,
                name="Authorization",
                field="api_token",
                template="Bearer {value}",
            ),
        )
        if account is AccountMode.LINKED_CREDENTIAL
        else (),
        gate_injections=gate_injections,
    )


def _make_link(
    *,
    tool_id: str,
    credential_key: str,
    owner_kind: LinkOwnerKind = LinkOwnerKind.PERSON,
    owner_id: str = PERSON_ID,
    purpose: LinkPurpose = LinkPurpose.ACCOUNT,
    injections: tuple[CredentialInjectionV1, ...] = (
        CredentialInjectionV1(
            placement=InjectionPlacement.HEADER,
            name="Authorization",
            field="api_token",
            template="Bearer {value}",
        ),
    ),
) -> AccountLinkV1:
    return AccountLinkV1(
        link_id=f"link_{owner_kind.value}_{credential_key}",
        team_id=TEAM_ID,
        tool_id=tool_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        purpose=purpose,
        credential_key=credential_key,
        injections=injections,
        created_by=SubjectRefV1(kind=SubjectKind.USER, id=PERSON_ID),
        created_at_utc=datetime.now(timezone.utc),
    )


# --- 1. Metadata & secret resolution + cache (A3, Lead Intervention 0005 #1) --


def test_metadata_and_secret_resolution_refuse_expired_without_touching_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expiry = BASE_NOW + timedelta(seconds=30)
    _save_temp_credential("temp_api", scope=USER_SCOPE, expires_at=format_utc_timestamp(expiry))

    source_calls = 0

    class FakeSource:
        def get_secret(self, _request: Any) -> str:
            nonlocal source_calls
            source_calls += 1
            return "older-or-live-sealbox-secret"

    monkeypatch.setattr(credential_catalog, "get_credential_source", lambda _name: FakeSource())

    # Before expiry: metadata, readonly metadata, and secret resolution succeed and populate cache.
    meta_ok = get_credential_metadata("temp_api", scope=USER_SCOPE, now=BASE_NOW)
    assert meta_ok.status == "success"
    assert meta_ok.content["credential"]["expires_at"] == format_utc_timestamp(expiry)
    assert read_credential_metadata_readonly("temp_api", scope=USER_SCOPE, now=BASE_NOW) is not None

    first_secret = resolve_secret_field("temp_api", "api_token", scope=USER_SCOPE, now=BASE_NOW)
    assert not isinstance(first_secret, credential_catalog.ToolResult)
    assert first_secret.value == "older-or-live-sealbox-secret"
    assert source_calls == 1

    # Cache hit before expiry serves cached value without calling source again, and never extends past expiry.
    second_secret = resolve_secret_field(
        "temp_api", "api_token", scope=USER_SCOPE, now=BASE_NOW + timedelta(seconds=20)
    )
    assert not isinstance(second_secret, credential_catalog.ToolResult)
    assert second_secret.value == "older-or-live-sealbox-secret"
    assert source_calls == 1
    # Verify the cache entry's deadline was capped to the ~10s remaining and records the hard UTC expiry.
    assert len(credential_catalog._SECRET_CACHE) == 1
    _val, deadline, hard_expiry = next(iter(credential_catalog._SECRET_CACHE.values()))
    assert deadline - credential_catalog.time.monotonic() <= 15.0
    assert hard_expiry == expiry

    # At exact expiry (inclusive): every read path treats the credential as absent with credential_expired.
    # Even if Sealbox still had an older non-expired version behind this key, the source is NEVER called.
    meta_exp = get_credential_metadata("temp_api", scope=USER_SCOPE, now=expiry)
    assert meta_exp.status == "error"
    assert meta_exp.content["error_code"] == CREDENTIAL_EXPIRED
    assert read_credential_metadata_readonly("temp_api", scope=USER_SCOPE, now=expiry) is None

    secret_exp = resolve_secret_field("temp_api", "api_token", scope=USER_SCOPE, now=expiry)
    assert isinstance(secret_exp, credential_catalog.ToolResult)
    assert secret_exp.status == "error"
    assert secret_exp.content["error_code"] == CREDENTIAL_EXPIRED
    assert source_calls == 1
    assert credential_catalog._SECRET_CACHE == {}

    # Non-secret metadata fields on an expired credential are also refused with credential_expired.
    public_exp = resolve_secret_field("temp_api", "username", scope=USER_SCOPE, now=expiry)
    assert isinstance(public_exp, credential_catalog.ToolResult)
    assert public_exp.status == "error"
    assert public_exp.content["error_code"] == CREDENTIAL_EXPIRED


# --- 2. Account link picker hides expired and labels temporary credentials ----


def test_account_link_picker_hides_expired_and_labels_active_temporary(tmp_path: Path) -> None:
    _save_temp_credential("expired_key", scope=USER_SCOPE, expires_at=PAST_EXPIRY, display_name="Expired Key")
    _save_temp_credential("active_temp", scope=USER_SCOPE, expires_at=FUTURE_EXPIRY, display_name="Active Temp")
    _save_temp_credential("lasting_key", scope=USER_SCOPE, expires_at=None, display_name="Lasting Key")

    tool = _http_tool()
    choices = credential_choices(USER_SCOPE, tool)
    keys = [item["credential_key"] for item in choices]
    assert keys == ["active_temp", "lasting_key"]
    assert "expired_key" not in keys

    request = AccountLinkRequestV1(
        request_id="req-picker-1",
        team_id=TEAM_ID,
        tool_id=tool.tool_id,
        owner_kind=LinkOwnerKind.PERSON,
        person_id=PERSON_ID,
        person_roles=("member",),
        reason="Test picker",
    )
    session = AccountLinkSession(
        request=request,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    rendered = render_form(session, tool, choices, used_by="TeamArchitect")
    assert f"(deletes {format_expiry_label(FUTURE_EXPIRY)})" in rendered
    assert "Expired Key" not in rendered

    store = access_store(tmp_path / "access.db")
    with pytest.raises(AccountLinkRefused):
        create_link(store, request, tool, "expired_key", now=datetime.now(timezone.utc))


# --- 3. Access gate & tool setup status (Lead Intervention 0005 #3) ----------


def _gate_with_tools(tmp_path: Path, *tools: ToolDefinitionV1) -> tuple[BrokerAccessGate, AccessRepository]:
    store = access_store(tmp_path / "access.db")
    catalog_source = CatalogSource(None)
    base_catalog = catalog_source.current()
    merged_tools = dict(base_catalog.tools)
    merged_hosts = dict(base_catalog.http_hosts)
    for tool in tools:
        merged_tools[tool.tool_id] = tool
        for host in tool.hosts:
            merged_hosts[host.lower()] = tool.tool_id
    custom_catalog = replace(base_catalog, tools=merged_tools, http_hosts=merged_hosts)
    gate = BrokerAccessGate(
        catalogs=SimpleNamespace(current=lambda: custom_catalog),  # type: ignore[arg-type]
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEAM_ID,
        access=None,
        links=store,
    )
    return gate, store


def _agent_context() -> dict[str, Any]:
    return {
        "agent_id": "worker-1",
        "named_agent_type": "WebResearchAgent",
        "team_id": TEAM_ID,
        "canonical_user_id": PERSON_ID,
        "task_id": "task-1",
        "task_context": signed_task_context(person_id=PERSON_ID, team_id=TEAM_ID),
    }


def test_access_gate_shared_team_fallback_vs_private_and_gate_link_and_x_fallback(
    tmp_path: Path,
) -> None:
    demo_tool = _http_tool("http:demo_api")
    gated_tool = _http_tool(
        "http:gated_api",
        account=AccountMode.NONE,
        gate_injections=(
            CredentialInjectionV1(
                placement=InjectionPlacement.HEADER,
                name="X-Gate-Key",
                field="api_token",
                template="{value}",
            ),
        ),
    )
    gate, store = _gate_with_tools(tmp_path, demo_tool, gated_tool)
    caller = BrokerCaller(CALLER_MANAGER)
    resolution = OperationResolutionV1(
        tool_id=demo_tool.tool_id,
        operation="get:api.demo.example",
        level=AccessLevel.READ,
        source="trusted",
    )

    _save_temp_credential("own_expired", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    _save_temp_credential("team_valid", scope=TEAM_SCOPE, expires_at=FUTURE_EXPIRY)
    own_link = store.create_link(_make_link(tool_id=demo_tool.tool_id, credential_key="own_expired"))
    team_link = store.create_link(
        _make_link(
            tool_id=demo_tool.tool_id,
            credential_key="team_valid",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM_ID,
        )
    )

    # Case A (Lead Intervention 0005 #3): tool is shared and team has a valid link ->
    # person's expired own link falls back to the team's link immediately, before any janitor run.
    TeamAccountSharing(store, TEAM_ID).choose_tool(demo_tool, AccountSharing.SHARED, by="admin-1")
    shared_result = gate.check(caller, resolution, _agent_context())
    assert shared_result.allowed is True
    assert shared_result.account_link is not None
    assert shared_result.account_link.link_id == team_link.link_id

    shared_status = tool_status_entry(demo_tool.tool_id, PERSON_ID, gate)
    assert shared_status["link"] == {"own": False, "team": True}
    assert verdict_from_status(shared_status) == {
        "state": ToolSetupState.READY,
        "how": "team_link",
        "account_label": None,
    }

    # Case B (Lead Intervention 0005 #3): tool is private -> no fallback applies, so gate refuses
    # with account_link_credential_expired, names /link, and flips ToolSetupStates to needs_setup.
    TeamAccountSharing(store, TEAM_ID).choose_tool(demo_tool, AccountSharing.PRIVATE, by="admin-1")
    private_result = gate.check(caller, resolution, _agent_context())
    assert private_result.allowed is False
    assert private_result.reason_code == REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED
    assert f"/link {demo_tool.tool_id}" in private_result.next_step
    saved_state = ToolSetupStates(store).get(TEAM_ID, PERSON_ID, demo_tool.tool_id)
    assert saved_state is not None
    assert saved_state.state is ToolSetupState.NEEDS_SETUP
    assert saved_state.missing_step == STEP_ACCOUNT_LINK

    private_status = tool_status_entry(demo_tool.tool_id, PERSON_ID, gate)
    assert private_status["link"] == {"own": False, "team": False}
    assert verdict_from_status(private_status) == {
        "state": ToolSetupState.NEEDS_SETUP,
        "step": STEP_ACCOUNT_LINK,
    }

    # Case C: shared tool where team link is also expired (or absent) -> refuses with account_link_credential_expired.
    _save_temp_credential("team_valid", scope=TEAM_SCOPE, expires_at=PAST_EXPIRY)
    TeamAccountSharing(store, TEAM_ID).choose_tool(demo_tool, AccountSharing.SHARED, by="admin-1")
    both_expired = gate.check(caller, resolution, _agent_context())
    assert both_expired.allowed is False
    assert both_expired.reason_code == REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED
    assert own_link.link_id != team_link.link_id

    # Case D: expired team gate link -> refuses with account_link_credential_expired and flips setup state to gate_link.
    _save_temp_credential("gate_expired", scope=TEAM_SCOPE, expires_at=PAST_EXPIRY)
    store.create_link(
        _make_link(
            tool_id=gated_tool.tool_id,
            credential_key="gate_expired",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM_ID,
            purpose=LinkPurpose.GATE,
            injections=gated_tool.gate_injections,
        )
    )
    gate_resolution = OperationResolutionV1(
        tool_id=gated_tool.tool_id,
        operation="get:api.demo.example",
        level=AccessLevel.READ,
        source="trusted",
    )
    gate_result = gate.check(caller, gate_resolution, _agent_context())
    assert gate_result.allowed is False
    assert gate_result.reason_code == REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED
    assert f"/link {gated_tool.tool_id} gate" in gate_result.next_step
    gate_state = ToolSetupStates(store).get(TEAM_ID, PERSON_ID, gated_tool.tool_id)
    assert gate_state is not None
    assert gate_state.missing_step == STEP_GATE_LINK
    assert tool_status_entry(gated_tool.tool_id, PERSON_ID, gate)["gate"] == {"state": "missing"}

    # Case E: sign-in tool with team fallback (cli:x) skips an expired team fallback link so personal sign-in works.
    x_tool = gate.catalog.tools["cli:x"]
    TeamAccountSharing(store, TEAM_ID).choose_tool(x_tool, AccountSharing.SHARED, by="admin-1")
    _save_temp_credential("x_team_expired", scope=TEAM_SCOPE, expires_at=PAST_EXPIRY, field_name="oauth_json")
    store.create_link(
        _make_link(
            tool_id="cli:x",
            credential_key="x_team_expired",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM_ID,
            injections=(),
        )
    )
    x_resolution = gate.catalog.resolve_broker_action("x_cli", "me", {})
    x_result = gate.check(caller, x_resolution, _agent_context())
    assert x_result.allowed is True
    assert x_result.account_link is None


# --- 4. Web API, OpenShell, Hub CLI, Installed CLI, MCP, Transfer Export ------


def test_web_api_and_openshell_refuse_expired_linked_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_temp_credential("expired_web", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    _save_temp_credential("expired_env", scope=USER_SCOPE, expires_at=PAST_EXPIRY)

    monkeypatch.setattr(
        credential_catalog,
        "_send_api_request",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("HTTP should not be called")),
    )
    web_link = _make_link(tool_id="http:demo_api", credential_key="expired_web")
    web_ctx = ToolContext(agent_id="worker-1", canonical_user_id=PERSON_ID, account_link=web_link)
    web_res = api_request({"method": "GET", "url": "https://api.demo.example/items"}, context=web_ctx)
    assert web_res.status == "error"
    assert web_res.content["error_code"] == REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED

    monkeypatch.setattr(
        credential_catalog,
        "_create_and_attach_openshell_provider",
        lambda **_k: (_ for _ in ()).throw(AssertionError("OpenShell should not be called")),
    )
    os_link = _make_link(
        tool_id="http:demo_api",
        credential_key="expired_env",
        injections=(
            CredentialInjectionV1(
                placement=InjectionPlacement.ENV,
                name="DEMO_API_TOKEN",
                field="api_token",
                template="{value}",
            ),
        ),
    )
    os_ctx = ToolContext(agent_id="worker-1", canonical_user_id=PERSON_ID, account_link=os_link)
    os_res = bind_for_openshell({"tool_id": "http:demo_api"}, context=os_ctx)
    assert os_res.status == "error"
    assert os_res.content["error_code"] == REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED


def test_hub_cli_and_installed_cli_refuse_expired_linked_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_temp_credential("expired_hf", scope=USER_SCOPE, expires_at=PAST_EXPIRY, field_name="api_token")
    hf_link = _make_link(tool_id="cli:huggingface", credential_key="expired_hf", injections=())
    hf_ctx = ToolContext(agent_id="worker-1", canonical_user_id=PERSON_ID, account_link=hf_link)
    monkeypatch.setattr(hub_cli_executor.shutil, "which", lambda _name: "/usr/bin/hf")
    hf_res = hub_cli_executor.execute_hub_cli(
        ValidatedAction(provider=HF_PROVIDER, action=AUTH_STATUS, params={}),
        hf_ctx,
    )
    assert hf_res.status == "error"
    assert hf_res.content["error_code"] == REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED

    _save_temp_credential("expired_cli", scope=USER_SCOPE, expires_at=PAST_EXPIRY, field_name="api_token")
    cli_tool = ToolDefinitionV1(
        tool_id="cli:mytool",
        kind=ToolKind.CLI,
        display_name="My Tool",
        account=AccountMode.LINKED_CREDENTIAL,
        link_owners=("person",),
        credential_injections=(
            CredentialInjectionV1(
                placement=InjectionPlacement.ENV,
                name="MYTOOL_TOKEN",
                field="api_token",
                template="{value}",
            ),
        ),
        installed_cli=InstalledCliV1(
            binary_path="team/tools/mytool/mytool",
            sha256="a" * 64,
            commands={"status": "read"},
        ),
    )
    cli_link = _make_link(
        tool_id="cli:mytool",
        credential_key="expired_cli",
        injections=cli_tool.credential_injections,
    )
    cli_ctx = ToolContext(agent_id="worker-1", canonical_user_id=PERSON_ID, account_link=cli_link)
    monkeypatch.setattr(
        installed_clis,
        "check_installed_command_line",
        lambda _policy, argv: SimpleNamespace(argv=tuple(argv), command_path="status", level=SimpleNamespace(value="read")),
    )
    cli_res = installed_clis.run_installed_cli(
        cli_tool,
        object(),
        ValidatedAction(provider="installed_cli", action="run", params={"argv": ["status"]}),
        cli_ctx,
    )
    assert cli_res.status == "error"
    assert cli_res.content["error_code"] == REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED


@pytest.mark.asyncio
async def test_mcp_runtime_refuses_expired_credential_and_discards_cached_client(
    tmp_path: Path,
) -> None:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="mcp-expiry-test", instance_root=str(root), team_id=TEAM_ID, timezone="UTC"
    )
    config.user.canonical_user_id = PERSON_ID
    config.authorization.enabled = False
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.mcp.legacy_sse.enabled = True
    config.mcp.network_policy.allow_loopback = True
    config.agent.external_mcp_servers = [
        {
            "name": "demo",
            "transport": "sse",
            "server_url": "http://127.0.0.1:19999/mcp/sse",
            "credential_injections": [
                {
                    "target_kind": "header",
                    "target_name": "Authorization",
                    "field_name": "api_token",
                    "template": "Bearer {value}",
                }
            ],
        }
    ]
    config.save(root / "instance_config.yaml")
    record = reconcile_instance_mcp_connections(config)[0]

    store = access_store(tmp_path / "access.db")
    gate = BrokerAccessGate(
        catalogs=CatalogSource(root),
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEAM_ID,
        access=None,
        links=store,
    )
    mcp_tool = gate.catalog.tools["mcp:demo"]
    _save_temp_credential("mcp_expired", scope=TEAM_SCOPE, expires_at=PAST_EXPIRY)
    link = store.create_link(
        _make_link(
            tool_id="mcp:demo",
            credential_key="mcp_expired",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM_ID,
            injections=mcp_tool.credential_injections,
        )
    )
    broker_config = BrokerMCPConfig.from_runtime_settings(
        instance_root_path=str(root),
        agent=config.agent,
        agent_types=config.agent_types,
        mcp_profiles=config.mcp_profiles,
        mcp=config.mcp,
    )
    runtime = BrokerMCPRuntime(config=broker_config, access_gate=gate)
    caller = BrokerCaller(CALLER_MANAGER)

    # Plant a fake cached client to confirm expiry discards it immediately.
    closed = False

    class FakeClient:
        async def close(self) -> None:
            nonlocal closed
            closed = True

    runtime._clients[(record.connection_id, link.link_id)] = FakeClient()  # type: ignore[assignment]

    # Discovery refuses with account_link_credential_expired and closes cached client.
    with pytest.raises(MCPAccessRefused) as disc_exc:
        await runtime.discover_tools(
            record.connection_id,
            MCPDiscoveryRequestV1(record_digest=record.record_digest, named_agent_type="WebResearchAgent"),
            caller=caller,
        )
    assert disc_exc.value.payload["reason_code"] == REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED
    assert closed is True
    assert runtime._clients == {}

    # Pin an operation, re-plant a cached client, and confirm invoke refuses and discards it too.
    record = runtime.store.set_operations(
        record.connection_id,
        (MCPOperationDefinitionV1.build(operation_name="ping", description="Ping", input_schema={}),),
    )
    closed = False
    runtime._clients[(record.connection_id, link.link_id)] = FakeClient()  # type: ignore[assignment]
    with pytest.raises(MCPAccessRefused) as invoke_exc:
        await runtime.invoke(
            record.connection_id,
            MCPInvocationRequestV1(
                record_digest=record.record_digest,
                operation_name="ping",
                tool_use_id="call-1",
                arguments={},
                task_id="task-1",
                task_context=signed_task_context(person_id=PERSON_ID, team_id=TEAM_ID),
                principal=PrincipalDescriptorV1(
                    principal_id="worker-1",
                    principal_type=PrincipalType.AGENT,
                    named_agent_type="WebResearchAgent",
                    team_id=TEAM_ID,
                    on_behalf_of_user_id=PERSON_ID,
                ),
            ),
            caller=caller,
        )
    assert invoke_exc.value.payload["reason_code"] == REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED
    assert closed is True
    assert runtime._clients == {}

    # Direct injection helper also translates expiry into MCPAccessRefused.
    with pytest.raises(MCPAccessRefused) as inject_exc:
        mcp_runtime.BrokerMCPRuntime._inject_linked_credentials(link, {"headers": {}, "env": {}})
    assert inject_exc.value.payload["reason_code"] == REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED


def test_team_transfer_export_skips_expired_credentials_and_keeps_active_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_temp_credential("expired_export", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    _save_temp_credential("active_export", scope=USER_SCOPE, expires_at=FUTURE_EXPIRY)
    _save_temp_credential("lasting_export", scope=USER_SCOPE, expires_at=None)

    class FakeSource:
        def get_secret(self, request: Any) -> str:
            return f"secret-for-{request.ref.item_id}"

    monkeypatch.setattr(credential_catalog, "get_credential_source", lambda _name: FakeSource())
    monkeypatch.setattr("secure_tool_broker.credential_state_store.read_credential_state", lambda _k: None)

    exported = export_credentials(TEAM_ID, [PERSON_ID])
    catalog_records = [rec for rec in exported if rec.get("kind") == "catalog_entry"]
    exported_keys = [rec["credential_key"] for rec in catalog_records]
    assert exported_keys == ["active_export", "lasting_export"]
    active_rec = next(rec for rec in catalog_records if rec["credential_key"] == "active_export")
    assert active_rec["credential"]["expires_at"] == FUTURE_EXPIRY


# --- 5. C18-C21 hardening: aliases, cache hard-expiry, slow reads, export race, gate tracking ---


def test_c18_sealbox_alias_refused_in_both_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    from secure_tool_broker.credential_catalog import collect_credential, register_existing_source
    from secure_tool_broker.credential_expiry import CREDENTIAL_EXPIRY_SHARED_RECORD

    source_calls = 0

    class FakeSealboxSource:
        def get_secret(self, _request: Any) -> str:
            nonlocal source_calls
            source_calls += 1
            return "secret-value"

    monkeypatch.setattr(credential_catalog, "get_credential_source", lambda _name: FakeSealboxSource())
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    monkeypatch.setattr(
        credential_catalog,
        "run_sealbox",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout='{"key":"k"}', stderr=""),
    )

    # Order 1: temporary credential exists first -> register_existing_source naming its Sealbox record is refused.
    _save_temp_credential("temp_primary", scope=USER_SCOPE, expires_at=FUTURE_EXPIRY)
    alias_exact = register_existing_source(
        {
            "credential_key": "alias_exact",
            "field_name": "api_token",
            "source": "sealbox",
            "item_id": "team-nats-agents:default:temp_primary:api_token",
            "field_ref": "value",
        },
        scope=USER_SCOPE,
    )
    assert alias_exact.status == "error"
    assert alias_exact.content["error_code"] == CREDENTIAL_EXPIRY_SHARED_RECORD
    assert alias_exact.content["conflicting_credential_key"] == "temp_primary"
    assert source_calls == 0

    # Shorthand field_ref form ("team-nats-agents:default:temp_primary" + field_ref="api_token") resolves to the same record and is refused.
    alias_shorthand = register_existing_source(
        {
            "credential_key": "alias_shorthand",
            "field_name": "api_token",
            "source": "sealbox",
            "item_id": "team-nats-agents:default:temp_primary",
            "field_ref": "api_token",
        },
        scope=USER_SCOPE,
    )
    assert alias_shorthand.status == "error"
    assert alias_shorthand.content["error_code"] == CREDENTIAL_EXPIRY_SHARED_RECORD
    assert source_calls == 0

    # Order 2: lasting alias exists first -> collect_credential with expires_at targeting the same record is refused
    # before writing anything to Sealbox.
    _save_temp_credential("lasting_holder", scope=USER_SCOPE, expires_at=None)
    write_called = False

    def fail_if_written(*_a: Any, **_k: Any) -> Any:
        nonlocal write_called
        write_called = True
        raise AssertionError("Sealbox write should not happen when record is shared")

    monkeypatch.setattr(credential_catalog, "write_credential_to_source", fail_if_written)
    temp_over_shared = collect_credential(
        {
            "credential_key": "temp_conflict",
            "write_source": "sealbox",
            "item_id": "team-nats-agents:default:lasting_holder",
            "fields": [{"name": "api_token", "kind": "token", "secret": True, "required": True}],
        },
        {"api_token": "temp-val"},
        scope=USER_SCOPE,
        expires_at=FUTURE_EXPIRY,
        allow_expiry_change=True,
    )
    assert temp_over_shared.status == "error"
    assert temp_over_shared.content["error_code"] == CREDENTIAL_EXPIRY_SHARED_RECORD
    assert temp_over_shared.content["conflicting_credential_key"] == "lasting_holder"
    assert write_called is False


def test_c24_lasting_collect_onto_temporary_sealbox_record_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C24: collecting a lasting credential targeting a temporary credential's Sealbox record is refused."""

    from secure_tool_broker.credential_expiry import CREDENTIAL_EXPIRY_SHARED_RECORD

    _save_temp_credential("temp_a", scope=USER_SCOPE, expires_at=FUTURE_EXPIRY)
    write_called = False

    def fail_if_written(*_a: Any, **_k: Any) -> Any:
        nonlocal write_called
        write_called = True
        raise AssertionError("Sealbox write must not happen onto a temporary credential's record")

    monkeypatch.setattr(credential_catalog, "write_credential_to_source", fail_if_written)
    lasting_over_temp = credential_catalog.collect_credential(
        {
            "credential_key": "lasting_b",
            "write_source": "sealbox",
            "item_id": "team-nats-agents:default:temp_a",
            "fields": [{"name": "api_token", "kind": "token", "secret": True, "required": True}],
        },
        {"api_token": "lasting-val"},
        scope=USER_SCOPE,
    )
    assert lasting_over_temp.status == "error"
    assert lasting_over_temp.content["error_code"] == CREDENTIAL_EXPIRY_SHARED_RECORD
    assert lasting_over_temp.content["conflicting_credential_key"] == "temp_a"
    assert write_called is False
    assert "lasting_b" not in credential_catalog.load_catalog(scope=USER_SCOPE).credentials


def test_c18_secret_cache_never_serves_or_extends_past_hard_expiry_even_without_caller_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS", "300")
    expiry = BASE_NOW + timedelta(seconds=30)
    calls = 0

    class FakeSource:
        def get_secret(self, _request: Any) -> str:
            nonlocal calls
            calls += 1
            return f"secret-v{calls}"

    monkeypatch.setattr(credential_catalog, "get_credential_source", lambda _name: FakeSource())
    ref = CatalogSourceRef(source="sealbox", item_id="shared-item", item_name="shared-item", field_ref="value")

    # Populate cache via a temporary credential read before expiry.
    first = credential_catalog._resolve_source_ref(
        ref, scope=USER_SCOPE, expires_at=expiry.isoformat().replace("+00:00", "Z"), now=BASE_NOW
    )
    assert first == "secret-v1"
    assert calls == 1

    # Another read BEFORE expiry with expires_at=None gets the cached value but CANNOT extend past hard_expiry.
    second = credential_catalog._resolve_source_ref(
        ref, scope=USER_SCOPE, expires_at=None, now=BASE_NOW + timedelta(seconds=20)
    )
    assert second == "secret-v1"
    assert calls == 1
    _val, deadline, hard_expiry = next(iter(credential_catalog._SECRET_CACHE.values()))
    assert hard_expiry == expiry
    assert deadline - credential_catalog.time.monotonic() <= 15.0

    # Once expiry passes, even a caller with expires_at=None MUST NOT get the cached temporary value.
    third = credential_catalog._resolve_source_ref(ref, scope=USER_SCOPE, expires_at=None, now=expiry)
    assert calls == 2
    assert third == "secret-v2"


def test_c18_use_credential_for_action_refuses_expired_sealbox_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from secure_tool_broker.credential_automations import use_credential_for_action

    _save_temp_credential("gh_temp", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    source_called = False

    class FakeSealboxSource:
        def get_secret(self, _request: Any) -> str:
            nonlocal source_called
            source_called = True
            return "older-version-token"

    monkeypatch.setattr("secure_tool_broker.credential_automations.get_credential_source", lambda _n: FakeSealboxSource())
    login_called = False

    def fake_login(_token: str, *, hostname: str | None = None) -> Any:
        nonlocal login_called
        login_called = True
        return credential_catalog.ToolResult(status="success", content={"hostname": hostname})

    ctx = ToolContext(agent_id="worker-1", canonical_user_id=PERSON_ID)
    result = use_credential_for_action(
        credential_provider="sealbox",
        target_provider="github",
        target_action="auth.login",
        item_id="team-nats-agents:default:gh_temp:api_token",
        item_name=None,
        field_ref="value",
        context=ctx,
        github_token_login=fake_login,
    )
    assert result.status == "error"
    assert result.content["error_code"] == CREDENTIAL_EXPIRED
    assert source_called is False
    assert login_called is False


def test_c19_slow_source_read_finishing_after_expiry_returns_credential_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as time_mod

    monkeypatch.setenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS", "300")
    # Real wall-clock expiry 1 second from now; fake source sleeps past it before returning an older value.
    near_expiry = (datetime.now(timezone.utc) + timedelta(seconds=1)).replace(microsecond=0)
    # Ensure at least ~0.2s remain before the read starts.
    if (near_expiry - datetime.now(timezone.utc)).total_seconds() < 0.2:
        near_expiry = near_expiry + timedelta(seconds=1)
    expiry_iso = near_expiry.isoformat().replace("+00:00", "Z")
    _save_temp_credential("slow_temp", scope=USER_SCOPE, expires_at=expiry_iso)

    class SlowSource:
        def get_secret(self, _request: Any) -> str:
            remaining = (near_expiry - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                time_mod.sleep(remaining + 0.05)
            return "earlier-lasting-version"

    monkeypatch.setattr(credential_catalog, "get_credential_source", lambda _name: SlowSource())
    result = resolve_secret_field("slow_temp", "api_token", scope=USER_SCOPE)
    assert isinstance(result, credential_catalog.ToolResult)
    assert result.status == "error"
    assert result.content["error_code"] == CREDENTIAL_EXPIRED
    assert credential_catalog._SECRET_CACHE == {}


def test_c20_export_skips_credential_that_expires_during_secret_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_temp_credential("raced_temp", scope=USER_SCOPE, expires_at=FUTURE_EXPIRY)
    _save_temp_credential("lasting_keep", scope=USER_SCOPE, expires_at=None)

    def fake_resolve(key: str, field_name: str, **_k: Any) -> Any:
        if key == "raced_temp":
            return credential_catalog.ToolResult(
                status="error",
                error="Temporary credential has expired.",
                content={"error_code": CREDENTIAL_EXPIRED, "credential_key": key},
            )
        return credential_catalog.ResolvedCredentialField(
            field_name=field_name,
            value="lasting-secret",
            source="sealbox",
            source_ref={"source": "sealbox", "item_id": "team-nats-agents:default:lasting_keep:api_token", "field_ref": "value"},
            secret=True,
        )

    monkeypatch.setattr(credential_catalog, "resolve_secret_field", fake_resolve)
    monkeypatch.setattr("secure_tool_broker.credential_state_store.read_credential_state", lambda _k: None)

    exported = export_credentials(TEAM_ID, [PERSON_ID])
    catalog_keys = [rec["credential_key"] for rec in exported if rec.get("kind") == "catalog_entry"]
    assert catalog_keys == ["lasting_keep"]


def test_c21_note_broker_result_maps_gate_only_failures_to_gate_link_and_single_row_readonly(
    tmp_path: Path,
) -> None:
    import sqlite3

    from secure_tool_broker.credential_catalog import linked_credential_expired
    from secure_tool_broker.tool_setup_tracking import note_broker_result
    from secure_tools.providers import ValidatedAction

    gated_tool = ToolDefinitionV1(
        tool_id="http:gate_only_api",
        kind=ToolKind.HTTP,
        display_name="Gate Only API",
        account=AccountMode.NONE,
        hosts=("gate.demo.example",),
        gate_injections=(
            CredentialInjectionV1(
                placement=InjectionPlacement.HEADER,
                name="X-Gate-Key",
                field="api_token",
            ),
        ),
    )
    gate, store = _gate_with_tools(tmp_path, gated_tool)
    validated = ValidatedAction(
        provider="secure_requests",
        action="get",
        params={"url": "https://gate.demo.example/ping"},
    )
    ctx = ToolContext(agent_id="worker-1", canonical_user_id=PERSON_ID)
    for code in (REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED, "account_link_outdated", "auth_required"):
        note_broker_result(
            gate,
            validated,
            ctx,
            credential_catalog.ToolResult(status="error", error="setup", content={"error_code": code}),
        )
        state = ToolSetupStates(store).get(TEAM_ID, PERSON_ID, gated_tool.tool_id)
        assert state is not None
        assert state.state is ToolSetupState.NEEDS_SETUP
        assert state.missing_step == STEP_GATE_LINK

    # C21(4): linked_credential_expired reads only its single catalog row (corrupt sibling row is never decoded).
    _save_temp_credential("good_expired", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    db_path = credential_catalog.resolve_catalog_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO credential_catalog_entries (scope_kind, owner_id, credential_key, credential_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("user", PERSON_ID, "corrupt_sibling", "{not-valid-json", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
    link = _make_link(tool_id="http:demo_api", credential_key="good_expired")
    assert linked_credential_expired(link) is True


def test_f1_agent_collect_refuses_dropped_sealbox_records_while_same_layout_and_form_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C33 + F1: agent credential.collect refuses layout changes that drop Sealbox records before writing; same layout and person form still work."""

    from secure_tool_broker.credential_catalog_store import CredentialCatalogStore, resolve_catalog_db_path
    from secure_tool_broker.credential_expiry import (
        CREDENTIAL_EXPIRY_SHARED_RECORD,
        CREDENTIAL_REPLACEMENT_FORM_ONLY,
    )

    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    sealbox_writes: list[list[str]] = []

    def fake_run_sealbox(args: list[str], **_k: Any) -> Any:
        sealbox_writes.append(list(args))
        return SimpleNamespace(returncode=0, stdout='{"key":"k","version":1,"password":"gen-password"}', stderr="")

    monkeypatch.setattr(credential_catalog, "run_sealbox", fake_run_sealbox)

    # Seed temporary credential on original Sealbox record.
    _save_temp_credential("temp_move", scope=USER_SCOPE, expires_at=FUTURE_EXPIRY)
    old_record_key = "team-nats-agents:default:temp_move:api_token"
    deleted_items: list[tuple[str, str]] = []

    def fake_delete(*, item_key: str, item_kind: str, env: Any = None) -> dict[str, Any]:
        deleted_items.append((item_key, item_kind))
        return {"source": "sealbox", "item_key": item_key, "item_kind": item_kind, "already_absent": False}

    monkeypatch.setattr(credential_catalog, "delete_sealbox_item", fake_delete)

    # 1. Agent collect changing item_id (dropping old_record_key) is refused BEFORE writing anything.
    refused_move = credential_catalog.collect_credential(
        {
            "credential_key": "temp_move",
            "write_source": "sealbox",
            "item_id": "relocated-temp-move",
            "fields": [{"name": "api_token", "kind": "token", "secret": True, "value_source": "store_generated_secret"}],
        },
        {},
        scope=USER_SCOPE,
        now=BASE_NOW,
    )
    assert refused_move.status == "error"
    assert refused_move.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert refused_move.content["dropped_sealbox_records"] == [old_record_key]
    assert refused_move.content["form_creation_action"] == "secure_credential_collection_request"
    assert "secure_credential_collection_request" in (refused_move.error or "")
    assert sealbox_writes == []
    assert deleted_items == []

    # 2. Agent collect keeping the SAME Sealbox record layout still succeeds and preserves expiry.
    same_layout = credential_catalog.collect_credential(
        {
            "credential_key": "temp_move",
            "write_source": "sealbox",
            "fields": [{"name": "api_token", "kind": "token", "secret": True, "value_source": "store_generated_secret"}],
        },
        {},
        scope=USER_SCOPE,
        now=BASE_NOW,
    )
    assert same_layout.status == "success", same_layout.error
    assert same_layout.content["expires_at"] == FUTURE_EXPIRY
    assert len(sealbox_writes) >= 1
    assert deleted_items == []

    # 3. Person's submitted form (allow_expiry_change=True) changing item_id deletes dropped Sealbox records.
    moved_by_form = credential_catalog.collect_credential(
        {
            "credential_key": "temp_move",
            "write_source": "sealbox",
            "item_id": "relocated-temp-move",
            "fields": [{"name": "api_token", "kind": "token", "secret": True, "value_source": "user_input"}],
        },
        {"api_token": "person-submitted-secret"},
        scope=USER_SCOPE,
        expires_at=FUTURE_EXPIRY,
        allow_expiry_change=True,
        now=BASE_NOW,
    )
    assert moved_by_form.status == "success", moved_by_form.error
    assert moved_by_form.content["expires_at"] == FUTURE_EXPIRY
    assert deleted_items == [(old_record_key, "secret")]

    # 4. Person's form still refuses BEFORE writing anything when another catalog entry references a dropped record.
    _save_temp_credential("lasting_shared_owner", scope=USER_SCOPE, expires_at=None)
    shared_key = "team-nats-agents:default:lasting_shared_owner:api_token"
    CredentialCatalogStore(resolve_catalog_db_path()).put_credential(
        USER_SCOPE,
        "lasting_alias",
        CatalogCredential(
            display_name="Lasting Alias",
            purpose="Shares Sealbox record",
            fields={
                "api_token": CatalogCredentialField(
                    kind="token",
                    visibility="secret",
                    resolution_order=[
                        CatalogSourceRef(source="sealbox", item_id=shared_key, item_name=shared_key, field_ref="value")
                    ],
                )
            },
        ).model_dump(mode="json", exclude_none=True),
    )
    writes: list[Any] = []
    monkeypatch.setattr(
        credential_catalog,
        "write_credential_to_source",
        lambda **kwargs: writes.append(kwargs) or credential_catalog.ToolResult(status="error", error="unexpected"),
    )
    refused = credential_catalog.collect_credential(
        {
            "credential_key": "lasting_shared_owner",
            "write_source": "sealbox",
            "item_id": "different-item-id",
            "fields": [{"name": "api_token", "kind": "token", "secret": True, "value_source": "user_input"}],
        },
        {"api_token": "new-secret"},
        scope=USER_SCOPE,
        allow_expiry_change=True,
    )
    assert refused.status == "error"
    assert refused.content["error_code"] == CREDENTIAL_EXPIRY_SHARED_RECORD
    assert refused.content["conflicting_credential_key"] == "lasting_alias"
    assert writes == []


def test_f3_cli_sealbox_search_and_get_metadata_hide_and_refuse_expired_catalog_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3: cli:sealbox credential.search omits expired catalog records and get_metadata refuses them with credential_expired."""

    import json as json_mod
    from secure_tool_broker.credential_catalog_store import CredentialCatalogStore, resolve_catalog_db_path
    from secure_tool_broker.credential_sources.base import CredentialRef, CredentialSearchRequest
    from secure_tool_broker.credential_sources.sealbox import SealboxCredentialSource

    expired_key = "team-nats-agents:default:expired_login"
    active_key = "team-nats-agents:default:active_login"
    store = CredentialCatalogStore(resolve_catalog_db_path())
    for key, sb_key, exp in (
        ("expired_login", expired_key, PAST_EXPIRY),
        ("active_login", active_key, FUTURE_EXPIRY),
    ):
        store.put_credential(
            USER_SCOPE,
            key,
            CatalogCredential(
                display_name=key,
                purpose="test",
                expires_at=exp,
                fields={
                    "password": CatalogCredentialField(
                        kind="password",
                        visibility="secret",
                        resolution_order=[
                            CatalogSourceRef(source="sealbox", item_id=sb_key, item_name=sb_key, field_ref="password")
                        ],
                    )
                },
            ).model_dump(mode="json", exclude_none=True),
        )

    list_calls = 0

    def fake_run_sealbox(args: list[str], **_k: Any) -> Any:
        nonlocal list_calls
        list_calls += 1
        return SimpleNamespace(
            returncode=0,
            stdout=json_mod.dumps(
                [
                    {"key": expired_key, "username": "old-lasting-user"},
                    {"key": active_key, "username": "active-user"},
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr("secure_tool_broker.credential_sources.sealbox.run_sealbox", fake_run_sealbox)
    monkeypatch.setattr(
        "secure_tool_broker.credential_sources.sealbox.sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )

    source = SealboxCredentialSource()
    search_result = source.search(
        CredentialSearchRequest(owner_scope_kind="user", owner_id=PERSON_ID)
    )
    assert search_result.status == "success"
    returned_ids = [item["credential_ref"]["item_id"] for item in search_result.content["credentials"]]
    assert returned_ids == [active_key]
    assert search_result.content["count"] == 1

    calls_before_get = list_calls
    meta_expired = source.get_metadata(
        CredentialRef(
            source_provider="sealbox",
            item_id=expired_key,
            owner_scope_kind="user",
            owner_id=PERSON_ID,
        )
    )
    assert meta_expired.status == "error"
    assert meta_expired.content["error_code"] == CREDENTIAL_EXPIRED
    assert list_calls == calls_before_get, "expired record must be refused without querying Sealbox"

    meta_active = source.get_metadata(
        CredentialRef(
            source_provider="sealbox",
            item_id=active_key,
            owner_scope_kind="user",
            owner_id=PERSON_ID,
        )
    )
    assert meta_active.status == "success"
    assert meta_active.content["credential"]["username"] == "active-user"


def test_f5_f6_missing_catalog_row_counts_as_missing_link_and_allows_team_fallback(tmp_path: Path) -> None:
    """Root cause B (F5/F6): a link whose credential key has no catalog row falls back to a shared team account or refuses as missing."""

    from access_control.account_sharing import TeamAccountSharing
    from custom_messages.contracts.access_v2 import (
        AccountSharing,
        ClassificationSource,
        REASON_ACCOUNT_LINK_MISSING,
    )

    demo_tool = ToolDefinitionV1(
        tool_id="http:missing_row_demo",
        kind=ToolKind.HTTP,
        display_name="Missing Row Demo",
        account=AccountMode.LINKED_CREDENTIAL,
        hosts=("missing-row.demo.example",),
        credential_injections=(
            CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="Authorization", field="api_token"),
        ),
    )
    gate, store = _gate_with_tools(tmp_path, demo_tool)
    TeamAccountSharing(store, TEAM_ID).choose_tool(demo_tool, AccountSharing.SHARED, by="admin-1")

    # Personal link points to a deleted/missing catalog key; team link has a valid credential.
    _save_temp_credential("team_valid_cred", scope=TEAM_SCOPE, expires_at=None)
    team_link = store.create_link(
        AccountLinkV1(
            link_id="link-team-valid",
            team_id=TEAM_ID,
            tool_id=demo_tool.tool_id,
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM_ID,
            purpose=LinkPurpose.ACCOUNT,
            credential_key="team_valid_cred",
            injections=demo_tool.credential_injections,
            created_by=SubjectRefV1(kind=SubjectKind.USER, id="admin-1"),
            created_at_utc=BASE_NOW - timedelta(hours=1),
        )
    )
    store.create_link(
        AccountLinkV1(
            link_id="link-person-missing-row",
            team_id=TEAM_ID,
            tool_id=demo_tool.tool_id,
            owner_kind=LinkOwnerKind.PERSON,
            owner_id=PERSON_ID,
            purpose=LinkPurpose.ACCOUNT,
            credential_key="deleted_personal_key",
            injections=demo_tool.credential_injections,
            created_by=SubjectRefV1(kind=SubjectKind.USER, id=PERSON_ID),
            created_at_utc=BASE_NOW - timedelta(hours=1),
        )
    )
    usable, expired = gate.resolve_account_link(demo_tool.tool_id, PERSON_ID, now=BASE_NOW)
    assert usable == team_link
    assert expired is None

    # When the tool is private (no shared team fallback), the missing-row link refuses with account_link_missing + /link.
    TeamAccountSharing(store, TEAM_ID).choose_tool(demo_tool, AccountSharing.PRIVATE, by="admin-1")
    refused = gate._with_account(
        OperationResolutionV1(
            tool_id=demo_tool.tool_id,
            operation="GET /items",
            level=AccessLevel.READ,
            source=ClassificationSource.TRUSTED,
        ),
        PERSON_ID,
        {},
    )
    assert refused.allowed is False
    assert refused.reason_code == REASON_ACCOUNT_LINK_MISSING
    assert f"/link {demo_tool.tool_id}" in refused.next_step


def test_c33_register_existing_source_refuses_dropping_sealbox_records_and_allows_same_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C33: register_existing_source refuses any save over an existing key that would drop its Sealbox record(s); same layout still works."""

    from secure_tool_broker.credential_catalog import register_existing_source
    from secure_tool_broker.credential_expiry import CREDENTIAL_REPLACEMENT_FORM_ONLY

    monkeypatch.setenv("C28_ENV_SECRET", "env-token-value")
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    deleted_items: list[tuple[str, str]] = []

    def fake_delete(*, item_key: str, item_kind: str, env: Any = None) -> Any:
        deleted_items.append((item_key, item_kind))
        return {"source": "sealbox", "item_key": item_key, "item_kind": item_kind, "already_absent": False}

    monkeypatch.setattr(credential_catalog, "delete_sealbox_item", fake_delete)
    monkeypatch.setattr(
        credential_catalog,
        "_resolve_source_ref",
        lambda source_ref, **_k: "resolved-secret-value",
    )

    # 1. Moving a lasting Sealbox-backed key to an env-var source is refused BEFORE writing or deleting anything.
    _save_temp_credential("svc", scope=USER_SCOPE, expires_at=None)
    r1_key = "team-nats-agents:default:svc:api_token"
    refused_env = register_existing_source(
        {
            "credential_key": "svc",
            "field_name": "api_token",
            "env_var": "C28_ENV_SECRET",
        },
        scope=USER_SCOPE,
    )
    assert refused_env.status == "error"
    assert (refused_env.content or {}).get("error_code") == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert (refused_env.content or {}).get("dropped_sealbox_records") == [r1_key]
    assert (refused_env.content or {}).get("form_creation_action") == "secure_credential_collection_request"
    assert deleted_items == []
    # Catalog entry remains unchanged on its Sealbox record.
    still_there = credential_catalog.load_catalog(scope=USER_SCOPE).credentials["svc"]
    assert still_there.fields["api_token"].resolution_order[0].source == "sealbox"

    # 2. Moving a lasting Sealbox-backed key to a DIFFERENT Sealbox item_id is also refused.
    refused_other_item = register_existing_source(
        {
            "credential_key": "svc",
            "field_name": "api_token",
            "source": "sealbox",
            "item_id": "other-sealbox-item",
            "field_ref": "value",
        },
        scope=USER_SCOPE,
    )
    assert refused_other_item.status == "error"
    assert (refused_other_item.content or {}).get("error_code") == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert deleted_items == []

    # 3. Re-registering over the SAME Sealbox record (no dropped records) still succeeds.
    same_layout = register_existing_source(
        {
            "credential_key": "svc",
            "display_name": "Service Renamed",
            "field_name": "api_token",
            "source": "sealbox",
            "item_id": r1_key,
            "field_ref": "value",
        },
        scope=USER_SCOPE,
    )
    assert same_layout.status == "success", same_layout.error
    assert credential_catalog.load_catalog(scope=USER_SCOPE).credentials["svc"].display_name == "Service Renamed"
    assert deleted_items == []


def test_c34_c35_pending_dropped_record_inline_retry_and_collect_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C34(2) + C35(a): collect_credential and register_existing_source retry queued dropped deletes inline before writing, refusing only if deletion still fails."""

    from secure_tool_broker.credential_catalog import register_existing_source
    from secure_tool_broker.credential_catalog_store import (
        CredentialCatalogStore,
        resolve_catalog_db_path,
    )
    from secure_tool_broker.credential_expiry import CREDENTIAL_DROPPED_CLEANUP_PENDING

    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    writes: list[Any] = []

    def fake_run_sealbox(args: list[str], **_k: Any) -> Any:
        writes.append(list(args))
        return SimpleNamespace(returncode=0, stdout='{"key":"k","version":1}', stderr="")

    monkeypatch.setattr(credential_catalog, "run_sealbox", fake_run_sealbox)
    monkeypatch.setattr(
        credential_catalog,
        "_resolve_source_ref",
        lambda source_ref, **_k: "resolved-secret-value",
    )

    # Seed key via form onto item_id "orig-item", then move it via form onto "moved-item" while Sealbox delete fails.
    _save_temp_credential("k1", scope=USER_SCOPE, expires_at=None)
    orig_record = "team-nats-agents:default:k1:api_token"
    fail_delete = True
    deleted_items: list[str] = []

    def fake_delete(*, item_key: str, item_kind: str, env: Any = None) -> Any:
        if fail_delete:
            return credential_catalog.ToolResult(status="error", error="sealbox timeout")
        deleted_items.append(item_key)
        return {"source": "sealbox", "item_key": item_key, "item_kind": item_kind, "already_absent": False}

    monkeypatch.setattr(credential_catalog, "delete_sealbox_item", fake_delete)

    form_move = credential_catalog.collect_credential(
        {
            "credential_key": "k1",
            "write_source": "sealbox",
            "item_id": "moved-item",
            "fields": [{"name": "api_token", "kind": "token", "secret": True, "value_source": "user_input"}],
        },
        {"api_token": "new-secret"},
        scope=USER_SCOPE,
        allow_expiry_change=True,
    )
    assert form_move.status == "success"
    assert (form_move.content or {}).get("dropped_records_not_deleted") == [orig_record]
    assert (form_move.content or {}).get("dropped_cleanup_retry_scheduled") is True

    # 1. While Sealbox delete still fails, collect_credential targeting orig_record refuses BEFORE any Sealbox write.
    writes.clear()
    refused_collect = credential_catalog.collect_credential(
        {
            "credential_key": "k1",
            "write_source": "sealbox",
            "fields": [{"name": "api_token", "kind": "token", "secret": True, "value_source": "user_input"}],
        },
        {"api_token": "another-secret"},
        scope=USER_SCOPE,
        allow_expiry_change=True,
    )
    assert refused_collect.status == "error"
    assert (refused_collect.content or {}).get("error_code") == CREDENTIAL_DROPPED_CLEANUP_PENDING
    assert "6 hours" in (refused_collect.error or "")
    assert writes == []

    # 2. register_existing_source for a new key targeting orig_record also refuses while deletion still fails.
    refused_reg = register_existing_source(
        {
            "credential_key": "k_alias",
            "field_name": "api_token",
            "source": "sealbox",
            "item_id": orig_record,
            "field_ref": "value",
        },
        scope=USER_SCOPE,
    )
    assert refused_reg.status == "error"
    assert (refused_reg.content or {}).get("error_code") == CREDENTIAL_DROPPED_CLEANUP_PENDING
    assert "6 hours" in (refused_reg.error or "")

    # 3. Once Sealbox works again, collect_credential retries the queued deletion inline and succeeds immediately.
    fail_delete = False
    recovered = credential_catalog.collect_credential(
        {
            "credential_key": "k1",
            "write_source": "sealbox",
            "fields": [{"name": "api_token", "kind": "token", "secret": True, "value_source": "user_input"}],
        },
        {"api_token": "restored-secret"},
        scope=USER_SCOPE,
        allow_expiry_change=True,
    )
    assert recovered.status == "success", recovered.error
    assert orig_record in deleted_items
    assert CredentialCatalogStore(resolve_catalog_db_path()).list_pending_dropped_sealbox_deletes(scope=USER_SCOPE) == []


def test_c35c_cli_sealbox_hides_records_queued_for_dropped_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C35(c): cli:sealbox search, get_metadata, and use_for_action hide records queued in pending_dropped_sealbox_deletes."""

    import json as json_mod
    from secure_tool_broker.credential_automations import use_credential_for_action
    from secure_tool_broker.credential_catalog_store import CredentialCatalogStore, resolve_catalog_db_path
    from secure_tool_broker.credential_sources.base import CredentialRef, CredentialSearchRequest
    from secure_tool_broker.credential_sources.sealbox import SealboxCredentialSource
    from secure_tools.providers import ToolContext

    queued_key = "team-nats-agents:default:queued_dropped_login"
    active_key = "team-nats-agents:default:active_login"
    store = CredentialCatalogStore(resolve_catalog_db_path())
    store.put_credential(
        USER_SCOPE,
        "active_login",
        CatalogCredential(
            display_name="active_login",
            purpose="test",
            fields={
                "password": CatalogCredentialField(
                    kind="password",
                    visibility="secret",
                    resolution_order=[
                        CatalogSourceRef(source="sealbox", item_id=active_key, item_name=active_key, field_ref="password")
                    ],
                )
            },
        ).model_dump(mode="json", exclude_none=True),
        dropped_sealbox_targets=[(queued_key, "credential")],
    )

    def fake_run_sealbox(args: list[str], **_k: Any) -> Any:
        return SimpleNamespace(
            returncode=0,
            stdout=json_mod.dumps(
                [
                    {"key": queued_key, "username": "old-replaced-user"},
                    {"key": active_key, "username": "active-user"},
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr("secure_tool_broker.credential_sources.sealbox.run_sealbox", fake_run_sealbox)
    monkeypatch.setattr(
        "secure_tool_broker.credential_sources.sealbox.sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )

    source = SealboxCredentialSource()
    search_result = source.search(CredentialSearchRequest(owner_scope_kind="user", owner_id=PERSON_ID))
    assert search_result.status == "success"
    assert [item["credential_ref"]["item_id"] for item in search_result.content["credentials"]] == [active_key]

    meta_queued = source.get_metadata(
        CredentialRef(
            source_provider="sealbox",
            item_id=queued_key,
            owner_scope_kind="user",
            owner_id=PERSON_ID,
        )
    )
    assert meta_queued.status == "error"
    assert (meta_queued.content or {}).get("error_code") == "credential_not_found"

    auto_queued = use_credential_for_action(
        credential_provider="sealbox",
        target_provider="github",
        target_action="auth.login",
        item_id=queued_key,
        item_name=None,
        field_ref="password",
        context=ToolContext(team_id=TEAM_ID, canonical_user_id=PERSON_ID),
        github_token_login=lambda token, hostname=None: credential_catalog.ToolResult(status="success"),
    )
    assert auto_queued.status == "error"
    assert (auto_queued.content or {}).get("error_code") == "credential_not_found"
