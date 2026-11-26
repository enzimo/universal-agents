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
    # Verify the cache entry's deadline was capped to the ~10s remaining, not the 300s default TTL.
    assert len(credential_catalog._SECRET_CACHE) == 1
    _val, deadline = next(iter(credential_catalog._SECRET_CACHE.values()))
    assert deadline - credential_catalog.time.monotonic() <= 15.0

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
    for tool in tools:
        merged_tools[tool.tool_id] = tool
    custom_catalog = replace(base_catalog, tools=merged_tools)
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

    http_called = False
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
    assert http_called is False

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
