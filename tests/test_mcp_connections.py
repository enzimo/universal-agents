"""Installed MCP connections: proposals, the registry, people's controls and the broker.

Covers: an agent's proposal is inert, secret-free and takes its person from
the signed task context; servers from the operator's configuration are
recorded once with the ``operator_configuration`` approval and later changes
wait for review without network access; agents receive only the ids of the
configured servers their configuration lists, never a server's configuration;
with authorization off, agents show the active installed servers whose record
names their type and the active configured servers their configuration lists;
people decide in the
gateway's MCP controls only when the access service lets them make the change
(real Cedar with authorization on, any active member with it off), their
decision is recorded as their approval, and with authorization on it lets the
agent types the record names use the server for everyone's work (grants on
``mcp:<name>``); the broker lists tools for any agent type the calling process
may act for; the operator's tool filters, environment references and network
policy hold.

Installing a server through TeamLead's access proposal, tools following
grants, and refusals reaching the model are in ``tests/test_mcp_follows_grants.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_catalog.catalog import CatalogSource
from access_control.caller_tokens import (
    CALLER_GATEWAY,
    CALLER_MANAGER,
    BrokerCaller,
    agent_caller_label,
)
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.identity import IdentityRejected
from access_control.inbox import StoredAccessInbox
from access_control.requests import AccessRequests
from assistant_gateway.application.website_application import (
    WebHostedMCPServerListRequestV1,
    WebMCPConnectionDecisionRequestV1,
    WebMCPConnectionDecisionResultV1,
    WebMCPConnectionListRequestV1,
    WebMCPConnectionV1,
    WebMCPLifecycleRequestV1,
    WebMCPLifecycleResultV1,
)
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.access_setup import MembershipRoles, PersonChangeCheck
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.mcp_controls import GatewayMCPControls
from custom_agents import access_runtime
from custom_agents.installed_permissions import INSTALLED_PROCEDURE_CHANGE_KIND
from custom_agents.providers.mcp_connections import (
    CONFIGURED_CONNECTION_IDS,
    MCPConnectionAdministration,
    apply_mcp_connection_reconciliation,
    shown_without_authorization,
)
from custom_messages.contracts.access_v2 import AccessLevel, ChangeApprovalMode, SubjectKind, TargetKind
from custom_messages.contracts.common_v1 import PrincipalDescriptorV1, PrincipalType
from custom_messages.contracts.mcp_v1 import MCPDiscoveryRequestV1, MCPInvocationRequestV1
from mcp_control.registry import (
    OPERATOR_CONFIGURATION_APPROVER,
    MCPConnectionLifecycle,
    MCPConnectionSource,
    MCPOperationDefinitionV1,
    brokered_mcp_server_config,
    reconcile_instance_mcp_connections,
    resolve_record_source_config,
    validate_mcp_network_target,
)
from runtime_settings.mcp import MCPNetworkPolicySettings
from secure_tool_broker.mcp_config import BrokerMCPConfig
from secure_tool_broker.mcp_runtime import BrokerMCPRuntime
from storage.access_repository import AccessRepository
from tests.broker_access_support import open_gate, signed_task_context
from tests.change_approval_support import CEDAR_BINARY, change_agent, needs_cedar, use_signed_contexts

TEAM = "team_mcp"
ADMIN = "admin-1"
MEMBER = "member-1"


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


def _config(tmp_path: Path) -> tuple[GatewayInstanceConfig, Path]:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="mcp-test",
        instance_root=str(root),
        team_id=TEAM,
        timezone="UTC",
    )
    path = config.save(root / "instance_config.yaml")
    return config, path


def _agent(config: GatewayInstanceConfig, path: Path) -> SimpleNamespace:
    """TeamArchitect working for the instance owner (gateway-signed task context)."""

    return change_agent(config, path, authorization_enabled=False)


def _propose(config: GatewayInstanceConfig, path: Path, name: str, *targets: str):
    return MCPConnectionAdministration.for_instance_config(config).propose_by_agent(
        _agent(config, path),
        connection_name=name,
        endpoint=f"https://{name}.example.test/mcp",
        target_agent_types=targets or ("TeamArchitect",),
    )


class _Identity:
    """Active gateway users and their team roles, as the gateway identity store keeps them."""

    def __init__(self, team_id: str, roles: dict[str, str], *, inactive: tuple[str, ...] = ()) -> None:
        self.team_id = team_id
        self.roles = roles
        self.inactive = set(inactive)

    def get_gateway_user(self, person_id: str) -> Any:
        if person_id not in self.roles:
            return None
        return SimpleNamespace(status="disabled" if person_id in self.inactive else "active")

    def list_team_memberships(self, person_id: str, status: str = "active") -> list[Any]:
        role = self.roles.get(person_id)
        return [SimpleNamespace(team_id=self.team_id, role=role)] if role else []


class _SignedInAs:
    """Tests name the signed-in person in the route; the gateway reads it from the session.

    An empty person reaches ``PersonChangeCheck``, which refuses it like anyone
    without the change.
    """

    def person_id(self, context: GatewayRouteContextV1) -> str:
        return str(context.canonical_user_id or "")

    def person_or_none(self, context: GatewayRouteContextV1) -> str | None:
        return context.canonical_user_id or None


def _route(person_id: str | None) -> GatewayRouteContextV1:
    return GatewayRouteContextV1(session_key="session-test", canonical_user_id=person_id)


class _Gateway:
    """The gateway's MCP controls (``GatewayMCPControls``) acting as one person per call."""

    def __init__(self, controls: GatewayMCPControls, changes: PersonChangeCheck) -> None:
        self.controls = controls
        self.changes = changes

    def list_mcp_connections(self, *, person_id: str | None) -> list[dict[str, Any]]:
        listed = self.controls.list_web_chat_mcp_connections(WebMCPConnectionListRequestV1(context=_route(person_id)))
        return [item.model_dump(mode="json") for item in listed.connections]

    def list_hosted_mcp_servers(self, *, person_id: str | None) -> list[dict[str, Any]]:
        listed = self.controls.list_web_chat_hosted_mcp_servers(
            WebHostedMCPServerListRequestV1(context=_route(person_id))
        )
        return [item.model_dump(mode="json") for item in listed.servers]

    def decide_mcp_connection(self, *, person_id: str | None, **decision: Any) -> dict[str, Any]:
        return self.controls.decide_web_chat_mcp_connection(
            WebMCPConnectionDecisionRequestV1(context=_route(person_id), **decision)
        ).model_dump(mode="json")

    async def control_mcp_lifecycle(self, *, person_id: str | None, **control: Any) -> dict[str, Any]:
        result = await self.controls.control_web_chat_mcp_lifecycle(
            WebMCPLifecycleRequestV1(context=_route(person_id), **control)
        )
        return result.model_dump(mode="json")

    def require_person_change(self, person_id: str | None, kind: str) -> str:
        return self.changes.require(person_id, kind)


def _gateway(config: GatewayInstanceConfig, *, authorization_enabled: bool, request) -> _Gateway:
    """The gateway's MCP controls for an owner, an administrator and a member.

    Shared with ``tests/test_mcp_hosting.py``.
    """

    config.authorization.enabled = authorization_enabled
    config.authorization.cedar_binary_path = str(CEDAR_BINARY)
    identity = _Identity(
        config.manager.team_id,
        {config.user.canonical_user_id: "owner", ADMIN: "admin", MEMBER: "member", "former-1": "admin"},
        inactive=("former-1",),
    )
    changes = PersonChangeCheck(config, MembershipRoles(identity))
    request.addfinalizer(changes.close)
    controls = GatewayMCPControls(
        config, person_changes=changes, signed_in=_SignedInAs(), access_requests=_access_requests(config, request)
    )
    return _Gateway(controls, changes)


def _access_requests(config: GatewayInstanceConfig, request):
    """The gateway's access service (real Cedar and SQLite), with the catalog reloading as servers install."""

    catalogs = CatalogSource(config.instance_root_path)
    service = AccessService(
        store=AccessRepository(config.manager_storage_settings),
        catalog=catalogs.current(),
        cedar=CedarAccessClient(binary=str(CEDAR_BINARY)),
        team_id=config.manager.team_id,
    )
    request.addfinalizer(service.cedar.close)

    def requests() -> AccessRequests:
        service.catalog = catalogs.current()
        return AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type="TeamArchitect")

    return requests


def _server_grants(config: GatewayInstanceConfig, tool_id: str) -> list[Any]:
    store = AccessRepository(config.manager_storage_settings)
    return [grant for grant in store.list_team(config.manager.team_id) if grant.target.id == tool_id]


def _broker_runtime(config: GatewayInstanceConfig) -> BrokerMCPRuntime:
    broker_config = BrokerMCPConfig.from_runtime_settings(
        instance_root_path=config.instance_root,
        agent=config.agent,
        agent_types=config.agent_types,
        mcp_profiles=config.mcp_profiles,
        mcp=config.mcp,
    )
    return BrokerMCPRuntime(
        config=broker_config,
        access_gate=open_gate(team_id=config.manager.team_id, instance_root=config.instance_root),
    )


class _CatalogClient:
    """A connection's MCP server, as the broker's client sees it."""

    def __init__(self, *names: str) -> None:
        self.names = names or ("search",)
        self.listed = 0

    async def list_tools(self):
        self.listed += 1
        return [
            SimpleNamespace(name=name, description=name, inputSchema={"type": "object"}, outputSchema=None)
            for name in self.names
        ]

    async def close(self):
        return None


# -- proposals ---------------------------------------------------------------------


def test_agent_proposal_is_inert_secret_free_and_reconciles_to_broker_proxy(
    tmp_path: Path,
) -> None:
    config, path = _config(tmp_path)
    agent = _agent(config, path)
    administration = MCPConnectionAdministration.for_instance_config(config)

    record = administration.propose_by_agent(
        agent,
        connection_name="reviewed-api",
        endpoint="https://mcp.example.test/mcp",
        target_agent_types=("TeamArchitect",),
        credential_injections=(
            {
                "owner_scope_kind": "team",
                "target_kind": "header",
                "target_name": "Authorization",
                "field_name": "access_token",
            },
        ),
    )

    assert record.lifecycle == MCPConnectionLifecycle.PENDING
    assert record.approval is None
    assert record.descriptor.source == MCPConnectionSource.AGENT_PROPOSAL
    assert record.descriptor.command is None
    assert record.descriptor.header_names == ()
    assert record.descriptor.literal_credential_material_present is False
    assert resolve_record_source_config(config, record)["server_url"] == (
        "https://mcp.example.test/mcp"
    )

    def reconciled() -> dict:
        return apply_mcp_connection_reconciliation(
            {
                "instance_config_path": str(path),
                "named_agent_type": "TeamArchitect",
                "external_mcp_servers": [],
            }
        )

    administration.approve_by_person(record.connection_id, person_id=config.user.canonical_user_id)
    agent_config = reconciled()
    assert "external_mcp_servers" not in agent_config, "agents never receive a server's configuration"
    assert "mcp_profiles" not in agent_config
    assert agent_config[CONFIGURED_CONNECTION_IDS] == [], "a proposal is not part of the operator's configuration"
    # The agent builds its client from the record while it runs (custom_agents.mcp_server_tools).
    approved = administration.store.get(record.connection_id)
    assert approved is not None
    proxy = brokered_mcp_server_config(approved, named_agent_type="TeamArchitect")
    assert proxy["mcp_connection_id"] == record.connection_id
    assert proxy["mcp_connection_record_digest"] == approved.record_digest
    assert proxy["mcp_brokered"] is True
    assert "server_url" not in proxy
    assert "headers" not in proxy
    assert "credential_injections" not in proxy
    assert "protected_capability_invocations" not in agent_config


def test_agent_proposal_takes_its_person_from_the_signed_task_context(tmp_path: Path) -> None:
    config, path = _config(tmp_path)
    agent = _agent(config, path)
    administration = MCPConnectionAdministration.for_instance_config(config)

    record = administration.propose_by_agent(
        agent, connection_name="owned-api", endpoint="https://owned.example.test/mcp"
    )

    assert record.descriptor.owner_canonical_user_id == config.user.canonical_user_id
    assert record.descriptor.team_id == TEAM
    assert record.descriptor.source_locator.startswith(f"agent-proposal:{agent.id}:")
    assert record.target_agent_types == ("TeamArchitect",), "defaults to the proposing agent's type"

    # A task whose signed context is missing (for example a forged message) cannot propose.
    agent.task_manager.meta.gateway_context.task_context = None
    with pytest.raises(IdentityRejected):
        administration.propose_by_agent(
            agent, connection_name="unsigned-api", endpoint="https://unsigned.example.test/mcp"
        )


def test_agent_proposal_rejects_stdio_and_semantic_name_drift(tmp_path: Path) -> None:
    config, path = _config(tmp_path)
    agent = _agent(config, path)
    administration = MCPConnectionAdministration.for_instance_config(config)

    with pytest.raises(PermissionError, match="cannot propose stdio"):
        administration.propose_by_agent(
            agent,
            connection_name="local-command",
            endpoint="https://example.test/mcp",
            transport="stdio",
        )

    first = administration.propose_by_agent(
        agent,
        connection_name="stable-name",
        endpoint="https://one.example.test/mcp",
    )
    again = administration.propose_by_agent(agent, connection_name="stable-name", endpoint="https://one.example.test/mcp")
    assert again == first, "proposing the same server again (a resumed tool call) finds the pending record"
    with pytest.raises(ValueError, match="semantic name"):
        administration.propose_by_agent(
            agent,
            connection_name="stable-name",
            endpoint="https://two.example.test/mcp",
        )
    assert [item.connection_id for item in administration.store.list_records()] == [first.connection_id]


# -- the registry and the operator's configuration -----------------------------------


def test_with_authorization_off_agents_load_active_servers_that_name_their_type(tmp_path: Path) -> None:
    config, path = _config(tmp_path)
    administration = MCPConnectionAdministration.for_instance_config(config)
    for name, targets in (("lead-only", ("TeamArchitect",)), ("research", ("WebResearchAgent",))):
        pending = _propose(config, path, name, *targets)
        administration.approve_by_person(pending.connection_id, person_id=config.user.canonical_user_id)
    _propose(config, path, "still-pending")

    def loaded(agent_type: str) -> list[str]:
        return _shown_without_authorization(administration, path, agent_type, [])

    assert loaded("TeamArchitect") == ["lead-only"]
    assert loaded("WebResearchAgent") == ["research"]
    assert loaded("CodeWritingAgent") == []
    assert not (administration.store.paths.root / "attachments").exists(), "no attachment records are written"


def _shown_without_authorization(
    administration: MCPConnectionAdministration, path: Path, agent_type: str, configured: list[dict[str, Any]]
) -> list[str]:
    """The active servers an agent of this type shows with authorization off (custom_agents.tool_sync)."""

    agent_config = apply_mcp_connection_reconciliation(
        {
            "instance_config_path": str(path),
            "named_agent_type": agent_type,
            "external_mcp_servers": configured,
        }
    )
    return [
        record.descriptor.connection_name
        for record in administration.store.list_records()
        if record.lifecycle is MCPConnectionLifecycle.ACTIVE
        and shown_without_authorization(
            record, agent_type=agent_type, configured_ids=agent_config[CONFIGURED_CONNECTION_IDS]
        )
    ]


def test_configured_servers_follow_the_agents_own_configuration(tmp_path: Path) -> None:
    """A global server is for every type, yet a type whose settings list their own servers keeps that meaning."""

    config, path = _config(tmp_path)
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    everyone = {"name": "everyone", "transport": "streamable_http", "server_url": "https://everyone.example.test/mcp"}
    config.agent.external_mcp_servers = [everyone]
    config.save(path)
    administration = MCPConnectionAdministration.for_instance_config(config)
    [record] = reconcile_instance_mcp_connections(config)
    assert record.permits_agent_type("CodeWritingAgent"), "the record alone would show it to every type"

    assert _shown_without_authorization(administration, path, "TeamArchitect", [everyone]) == ["everyone"]
    assert _shown_without_authorization(administration, path, "CodeWritingAgent", []) == [], (
        "an agent type whose own list leaves the server out does not show it"
    )


def test_a_tampered_connection_record_is_refused(tmp_path: Path) -> None:
    config, path = _config(tmp_path)
    administration = MCPConnectionAdministration.for_instance_config(config)
    pending = _propose(config, path, "tamper-check")
    record_path = administration.store.paths.records / f"{pending.connection_id}.json"
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["target_agent_types"] = ["CodeWritingAgent"]
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="record digest mismatch"):
        administration.store.require(pending.connection_id)


def test_active_connection_records_require_a_saved_approval(tmp_path: Path) -> None:
    config, path = _config(tmp_path)
    administration = MCPConnectionAdministration.for_instance_config(config)
    pending = _propose(config, path, "needs-approval")

    with pytest.raises(ValueError, match="require approval provenance"):
        administration.store.update_lifecycle(pending.connection_id, lifecycle=MCPConnectionLifecycle.ACTIVE)


def test_static_mcp_config_without_instance_registry_fails_closed() -> None:
    agent_config = apply_mcp_connection_reconciliation(
        {
            "named_agent_type": "TeamArchitect",
            "external_mcp_servers": [
                {
                    "name": "unregistered",
                    "transport": "streamable_http",
                    "server_url": "https://mcp.example.test/mcp",
                }
            ],
        }
    )

    assert "external_mcp_servers" not in agent_config
    assert agent_config[CONFIGURED_CONNECTION_IDS] == []
    assert "protected_capability_invocations" not in agent_config


def test_configured_servers_are_recorded_once_and_later_changes_wait_without_network(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config, path = _config(tmp_path)
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.agent.external_mcp_servers = [
        {
            "name": "existing",
            "transport": "streamable_http",
            "server_url": "https://existing.example.test/mcp",
            "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
            "tool_name_prefix": "existing",
            "include_tools": ["search"],
        }
    ]
    config.save(path)
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconciliation attempted network resolution")
        ),
    )

    [first] = reconcile_instance_mcp_connections(config)
    administration = MCPConnectionAdministration.for_instance_config(config)

    assert first.lifecycle == MCPConnectionLifecycle.ACTIVE
    assert first.schema_version == 2
    assert first.from_operator_configuration is True
    assert first.approval is not None
    assert first.approval.mode is ChangeApprovalMode.OPERATOR_CONFIGURATION
    assert first.approval.approved_by == OPERATOR_CONFIGURATION_APPROVER
    assert first.approval.access_request_id is None
    assert first.permits_agent_type("AnyAgentType"), "a global server is for every agent type"
    [listed] = administration.list_public()
    assert listed["from_operator_configuration"] is True
    assert listed["approval"]["mode"] == "operator_configuration"
    assert "grandfathered" not in listed
    # Reconciling again changes nothing.
    assert reconcile_instance_mcp_connections(config) == (first,)

    config.agent.external_mcp_servers = [
        {
            "name": "existing",
            "transport": "streamable_http",
            "server_url": "https://changed.example.test/mcp",
            "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
            "tool_name_prefix": "existing",
            "include_tools": ["search"],
        }
    ]
    config.save(path)
    [second] = reconcile_instance_mcp_connections(config)
    assert second.lifecycle == MCPConnectionLifecycle.PENDING
    assert second.from_operator_configuration is False, "only the first registry creation is trusted"
    assert {item.lifecycle for item in administration.store.list_records()} == {
        MCPConnectionLifecycle.DISABLED,
        MCPConnectionLifecycle.PENDING,
    }

    agent_config = apply_mcp_connection_reconciliation(
        {
            "instance_config_path": str(path),
            "named_agent_type": "TeamArchitect",
            "external_mcp_servers": list(config.agent.external_mcp_servers),
        }
    )
    assert agent_config[CONFIGURED_CONNECTION_IDS] == [second.connection_id], (
        "the changed server waits for review, and loads without a restart once approved"
    )


def test_new_configured_servers_need_review_when_the_registry_already_exists(tmp_path: Path) -> None:
    config, path = _config(tmp_path)
    config.mcp.static_reconciliation.upgrade_mode = "review_required"
    config.agent.external_mcp_servers = [
        {"name": "reviewed", "transport": "streamable_http", "server_url": "https://reviewed.example.test/mcp"}
    ]
    config.save(path)

    [pending] = reconcile_instance_mcp_connections(config)

    assert pending.lifecycle == MCPConnectionLifecycle.PENDING
    assert pending.approval is None


# -- people deciding in the gateway's MCP controls -----------------------------------


@needs_cedar
def test_an_administrator_approves_a_proposal_and_its_agent_types_may_use_it(tmp_path: Path, request) -> None:
    config, path = _config(tmp_path)
    pending = _propose(config, path, "docs", "TeamArchitect", "WebResearchAgent")
    gateway = _gateway(config, authorization_enabled=True, request=request)

    [before] = gateway.list_mcp_connections(person_id=ADMIN)
    result = gateway.decide_mcp_connection(
        connection_id=pending.connection_id,
        expected_record_digest=pending.record_digest,
        decision="approve",
        reason="",
        person_id=ADMIN,
    )

    administration = MCPConnectionAdministration.for_instance_config(config)
    active = administration.store.require(pending.connection_id)
    assert before["can_decide"] is True
    assert result["status"] == "active"
    assert result["record_digest"] == active.record_digest
    WebMCPConnectionDecisionResultV1.model_validate(result)
    assert active.lifecycle == MCPConnectionLifecycle.ACTIVE
    assert active.approval is not None
    assert active.approval.mode is ChangeApprovalMode.PERSON_APPROVAL
    assert active.approval.approved_by == ADMIN
    assert active.approval.access_request_id is None, "a decision in the MCP controls has no Inbox request"
    # One decision: the agent types the proposal names may use every tool of the
    # server, for everyone's work (the approver is an administrator).
    grants = _server_grants(config, "mcp:docs")
    assert sorted(grant.subject.id for grant in grants) == [f"{TEAM}/TeamArchitect", f"{TEAM}/WebResearchAgent"]
    assert {(grant.subject.kind, grant.target.kind, grant.level) for grant in grants} == {
        (SubjectKind.AGENT_TYPE, TargetKind.TOOL, AccessLevel.FULL)
    }
    assert {(grant.for_person_id, grant.granted_by.id) for grant in grants} == {(None, ADMIN)}
    [after] = gateway.list_mcp_connections(person_id=ADMIN)
    assert (after["can_decide"], after["available_actions"], after["tool_id"]) == (False, ["disable"], "mcp:docs")


@needs_cedar
def test_a_member_sees_no_mcp_controls_and_cannot_decide(tmp_path: Path, request) -> None:
    config, path = _config(tmp_path)
    pending = _propose(config, path, "docs")
    gateway = _gateway(config, authorization_enabled=True, request=request)

    for person_id in (MEMBER, "former-1", "stranger-1", None):
        [view] = gateway.list_mcp_connections(person_id=person_id)
        assert (view["can_decide"], view["available_actions"]) == (False, []), person_id
        with pytest.raises(PermissionError, match="Only an administrator"):
            gateway.decide_mcp_connection(
                connection_id=pending.connection_id,
                expected_record_digest=pending.record_digest,
                decision="approve",
                reason="",
                person_id=person_id,
            )
    with pytest.raises(PermissionError, match="Only an administrator"):
        gateway.require_person_change(MEMBER, INSTALLED_PROCEDURE_CHANGE_KIND)

    assert gateway.require_person_change(ADMIN, INSTALLED_PROCEDURE_CHANGE_KIND) == ADMIN
    assert gateway.require_person_change(config.user.canonical_user_id, INSTALLED_PROCEDURE_CHANGE_KIND) == (
        config.user.canonical_user_id
    )
    record = MCPConnectionAdministration.for_instance_config(config).store.require(pending.connection_id)
    assert record.lifecycle == MCPConnectionLifecycle.PENDING
    assert record.approval is None


def test_with_authorization_off_any_active_member_decides(tmp_path: Path, request) -> None:
    config, path = _config(tmp_path)
    approved = _propose(config, path, "approved-api")
    rejected = _propose(config, path, "rejected-api")
    gateway = _gateway(config, authorization_enabled=False, request=request)

    assert all(item["can_decide"] for item in gateway.list_mcp_connections(person_id=MEMBER))
    assert not any(item["can_decide"] for item in gateway.list_mcp_connections(person_id="former-1"))
    with pytest.raises(PermissionError):
        gateway.decide_mcp_connection(
            connection_id=approved.connection_id,
            expected_record_digest=approved.record_digest,
            decision="approve",
            reason="",
            person_id="stranger-1",
        )
    gateway.decide_mcp_connection(
        connection_id=approved.connection_id,
        expected_record_digest=approved.record_digest,
        decision="approve",
        reason="",
        person_id=MEMBER,
    )
    gateway.decide_mcp_connection(
        connection_id=rejected.connection_id,
        expected_record_digest=rejected.record_digest,
        decision="reject",
        reason="Not needed.",
        person_id=MEMBER,
    )

    store = MCPConnectionAdministration.for_instance_config(config).store
    active = store.require(approved.connection_id)
    assert active.lifecycle == MCPConnectionLifecycle.ACTIVE
    assert active.approval is not None and active.approval.approved_by == MEMBER
    assert active.approval.mode is ChangeApprovalMode.PERSON_APPROVAL
    assert _server_grants(config, "mcp:approved-api") == [], "with authorization off there are no grants"
    refused = store.require(rejected.connection_id)
    assert (refused.lifecycle, refused.rejection_reason, refused.approval) == (
        MCPConnectionLifecycle.REJECTED,
        "Not needed.",
        None,
    )


def test_a_decision_on_a_changed_proposal_is_refused(tmp_path: Path, request) -> None:
    config, path = _config(tmp_path)
    pending = _propose(config, path, "moving-api")
    gateway = _gateway(config, authorization_enabled=False, request=request)
    MCPConnectionAdministration.for_instance_config(config).store.set_operations(
        pending.connection_id,
        (MCPOperationDefinitionV1.build(operation_name="search", input_schema={"type": "object"}),),
    )

    with pytest.raises(PermissionError, match="changed after"):
        gateway.decide_mcp_connection(
            connection_id=pending.connection_id,
            expected_record_digest=pending.record_digest,
            decision="approve",
            reason="",
            person_id=MEMBER,
        )
    assert (
        MCPConnectionAdministration.for_instance_config(config).store.require(pending.connection_id).lifecycle
        == MCPConnectionLifecycle.PENDING
    )


@needs_cedar
@pytest.mark.asyncio
async def test_an_administrator_disables_and_removes_a_connection_recoverably(tmp_path: Path, request) -> None:
    config, path = _config(tmp_path)
    pending = _propose(config, path, "operator-controlled", "TeamArchitect", "WebResearchAgent")
    gateway = _gateway(config, authorization_enabled=True, request=request)
    administration = MCPConnectionAdministration.for_instance_config(config)
    active = administration.approve_by_person(pending.connection_id, person_id=ADMIN)

    async def control(action: str, record, *, person_id: str = ADMIN) -> dict:
        return await gateway.control_mcp_lifecycle(
            resource_kind="connection",
            resource_id=record.connection_id,
            action=action,
            expected_digest=record.record_digest,
            expected_lifecycle=record.lifecycle.value,
            person_id=person_id,
        )

    with pytest.raises(PermissionError, match="Only an administrator"):
        await control("disable", active, person_id=MEMBER)
    with pytest.raises(PermissionError, match="Disable the MCP connection"):
        await control("remove", active)
    disabled = await control("disable", active)
    WebMCPLifecycleResultV1.model_validate(disabled)
    assert disabled["status"] == "disabled"
    current = administration.store.require(active.connection_id)
    removed = await control("remove", current)

    assert removed["status"] == "removed"
    assert administration.store.require(active.connection_id).lifecycle == MCPConnectionLifecycle.REMOVED


@needs_cedar
def test_mcp_controls_listing_matches_the_web_contract(tmp_path: Path, request) -> None:
    """The gateway validates each listed connection with WebMCPConnectionV1 before the browser sees it."""

    config, path = _config(tmp_path)
    pending = _propose(config, path, "listed-api")
    _propose(config, path, "waiting-api")
    MCPConnectionAdministration.for_instance_config(config).approve_by_person(pending.connection_id, person_id=ADMIN)
    gateway = _gateway(config, authorization_enabled=True, request=request)

    for person_id in (ADMIN, MEMBER):
        listed = gateway.list_mcp_connections(person_id=person_id)
        assert len(listed) == 2
        for item in listed:
            WebMCPConnectionV1.model_validate(item)


# -- the broker ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broker_lists_tools_for_any_agent_type_the_calling_process_may_act_for(tmp_path: Path) -> None:
    """Listing tools grants nothing (grants decide each call), so only the process is checked."""

    config, path = _config(tmp_path)
    pending = _propose(config, path, "listed", "TeamArchitect")
    administration = MCPConnectionAdministration.for_instance_config(config)
    administration.approve_by_person(pending.connection_id, person_id=config.user.canonical_user_id)
    record = administration.store.require(pending.connection_id)
    runtime = _broker_runtime(config)
    catalog = _CatalogClient("search")
    runtime._clients[(record.connection_id, "")] = catalog

    def discovery(agent_type: str) -> MCPDiscoveryRequestV1:
        current = administration.store.require(record.connection_id)
        return MCPDiscoveryRequestV1(record_digest=current.record_digest, named_agent_type=agent_type)

    try:
        refusals = {}
        for label, agent_type in (
            (CALLER_GATEWAY, "TeamArchitect"),  # the gateway never acts as an agent
            (agent_caller_label("CodeWritingAgent"), "TeamArchitect"),  # another agent type's process
        ):
            with pytest.raises(PermissionError) as refused:
                await runtime.discover_tools(record.connection_id, discovery(agent_type), caller=BrokerCaller(label))
            refusals[(label, agent_type)] = str(refused.value)
        assert catalog.listed == 0, "nothing reaches the server before the checks pass"

        by_manager = await runtime.discover_tools(
            record.connection_id, discovery("TeamArchitect"), caller=BrokerCaller(CALLER_MANAGER)
        )
        by_own_process = await runtime.discover_tools(
            record.connection_id,
            discovery("TeamArchitect"),
            caller=BrokerCaller(agent_caller_label("TeamArchitect")),
        )
        # A worker the proposal never named may still hold access (a hand-down),
        # so its process may list the tools too.
        by_worker_process = await runtime.discover_tools(
            record.connection_id,
            discovery("WebResearchAgent"),
            caller=BrokerCaller(agent_caller_label("WebResearchAgent")),
        )
    finally:
        await runtime.close()

    assert "gateway process cannot list tools" in refusals[(CALLER_GATEWAY, "TeamArchitect")]
    assert "cannot list tools for TeamArchitect" in refusals[(agent_caller_label("CodeWritingAgent"), "TeamArchitect")]
    assert [tool.name for tool in by_manager.tools] == ["search"]
    assert by_own_process == by_manager == by_worker_process
    assert catalog.listed == 1, "the attested catalog is reused"


@pytest.mark.asyncio
async def test_broker_refuses_discovery_of_a_pending_or_changed_connection(tmp_path: Path) -> None:
    config, path = _config(tmp_path)
    pending = _propose(config, path, "waiting")
    runtime = _broker_runtime(config)
    manager = BrokerCaller(CALLER_MANAGER)
    try:
        with pytest.raises(PermissionError, match="not active"):
            await runtime.discover_tools(
                pending.connection_id,
                MCPDiscoveryRequestV1(record_digest=pending.record_digest, named_agent_type="TeamArchitect"),
                caller=manager,
            )
        MCPConnectionAdministration.for_instance_config(config).approve_by_person(
            pending.connection_id, person_id=config.user.canonical_user_id
        )
        with pytest.raises(PermissionError, match="changed before discovery"):
            await runtime.discover_tools(
                pending.connection_id,
                MCPDiscoveryRequestV1(record_digest=pending.record_digest, named_agent_type="TeamArchitect"),
                caller=manager,
            )
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    ("label", "path", "allowed"),
    [
        (CALLER_MANAGER, "/v1/mcp/connections/mcp_conn_x/tools", True),
        (agent_caller_label("WebResearchAgent"), "/v1/mcp/connections/mcp_conn_x/tools", True),
        (CALLER_GATEWAY, "/v1/mcp/connections/mcp_conn_x/tools", False),
        (CALLER_GATEWAY, "/v1/mcp/connections/mcp_conn_x/invoke", False),
        (CALLER_MANAGER, "/v1/mcp/hosted/mcp_host_x/start", True),
        (CALLER_GATEWAY, "/v1/mcp/hosted/mcp_host_x/stop", True),
        (agent_caller_label("WebResearchAgent"), "/v1/mcp/hosted/mcp_host_x/start", False),
    ],
)
def test_broker_mcp_routes_admit_only_their_processes(label: str, path: str, allowed: bool) -> None:
    from secure_tool_broker.service import SecureToolBrokerService

    assert SecureToolBrokerService._caller_may_use(BrokerCaller(label), path) is allowed


@pytest.mark.asyncio
async def test_operator_configured_server_lists_tools_without_a_person(tmp_path: Path) -> None:
    config, _path = _config(tmp_path)
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.agent.external_mcp_servers = [
        {
            "name": "existing",
            "transport": "streamable_http",
            "server_url": "https://existing.example.test/mcp",
            "tool_name_prefix": "existing",
        }
    ]
    [record] = reconcile_instance_mcp_connections(config)
    runtime = _broker_runtime(config)
    runtime._clients[(record.connection_id, "")] = _CatalogClient("search")
    try:
        response = await runtime.discover_tools(
            record.connection_id,
            MCPDiscoveryRequestV1(record_digest=record.record_digest, named_agent_type="TeamArchitect"),
            caller=BrokerCaller(CALLER_MANAGER),
        )
    finally:
        await runtime.close()

    assert [tool.name for tool in response.tools] == ["search"]
    assert runtime.store.require(record.connection_id).operations[0].operation_name == "search"


@pytest.mark.asyncio
async def test_operator_tool_filters_bound_discovery_invocation_and_agent_proxy(
    tmp_path: Path,
) -> None:
    """include_tools/exclude_tools must hold after discovery.

    An operation the operator filtered out must never enter the catalog, and
    an older catalog that still lists it must not be invocable.
    """

    config, _path = _config(tmp_path)
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.agent.external_mcp_servers = [
        {
            "name": name,
            "transport": "streamable_http",
            "server_url": f"https://{name}.example.test/mcp",
            "tool_name_prefix": name,
            "include_tools": ["search", "purge"],
            "exclude_tools": ["purge"],
        }
        for name in ("filtered", "legacy")
    ]
    records = {item.descriptor.connection_name: item for item in reconcile_instance_mcp_connections(config)}
    record = records["filtered"]
    runtime = _broker_runtime(config)
    runtime._clients[(record.connection_id, "")] = _CatalogClient("search", "purge", "admin_reset")
    try:
        response = await runtime.discover_tools(
            record.connection_id,
            MCPDiscoveryRequestV1(record_digest=record.record_digest, named_agent_type="TeamArchitect"),
            caller=BrokerCaller(CALLER_MANAGER),
        )
        assert [tool.name for tool in response.tools] == ["search"]

        # A catalog attested before the filter fix still lists excluded operations.
        stale = runtime.store.set_operations(
            records["legacy"].connection_id,
            tuple(
                MCPOperationDefinitionV1.build(
                    operation_name=name, description=name, input_schema={"type": "object"}, output_schema=None
                )
                for name in ("search", "purge", "admin_reset")
            ),
        )
        for excluded in ("purge", "admin_reset"):
            with pytest.raises(PermissionError, match="excluded by the connection"):
                await runtime.invoke(
                    stale.connection_id,
                    MCPInvocationRequestV1(
                        record_digest=stale.record_digest,
                        operation_name=excluded,
                        tool_use_id="tool-use-1",
                        principal=PrincipalDescriptorV1(
                            principal_type=PrincipalType.AGENT,
                            principal_id="agent-1",
                            team_id=stale.descriptor.team_id,
                            on_behalf_of_user_id=stale.descriptor.owner_canonical_user_id,
                            named_agent_type="TeamArchitect",
                        ),
                        task_context=signed_task_context(
                            stale.descriptor.owner_canonical_user_id, team_id=stale.descriptor.team_id
                        ),
                    ),
                    caller=BrokerCaller(CALLER_MANAGER),
                )
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["variable", "secret_file"])
async def test_broker_materializes_static_environment_references_only_in_memory(
    tmp_path: Path,
    monkeypatch,
    source: str,
) -> None:
    config, _path = _config(tmp_path)
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.agent.external_mcp_servers = [
        {
            "name": "authenticated",
            "transport": "sse",
            "server_url": "https://mcp.example.test/sse",
            "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
            "tool_name_prefix": "authenticated",
        }
    ]
    if source == "variable":
        monkeypatch.setenv("MCP_TEST_TOKEN", "broker-only-token")
    else:
        # A reference may name any variable, so its Docker secret file form
        # (NAME_FILE) works without adding the name to the shared list.
        secret_path = tmp_path / "mcp_test_token"
        secret_path.write_text("broker-only-token\n", encoding="utf-8")
        monkeypatch.delenv("MCP_TEST_TOKEN", raising=False)
        monkeypatch.setenv("MCP_TEST_TOKEN_FILE", str(secret_path))
    record = reconcile_instance_mcp_connections(config)[0]
    captured: dict[str, dict] = {}

    class CapturingClient:
        def __init__(self, server_config):
            captured["server_config"] = dict(server_config)

        async def close(self):
            return None

    runtime = _broker_runtime(config)
    monkeypatch.setattr("secure_tool_broker.mcp_runtime.BrokerMCPClient", CapturingClient)
    monkeypatch.setattr(runtime, "_validate_transport_policy", lambda _record, _raw: ())

    try:
        await runtime._client(record, link=None)
    finally:
        await runtime.close()

    persisted_source = resolve_record_source_config(config, record)
    assert persisted_source["headers"] == {"Authorization": "Bearer ${MCP_TEST_TOKEN}"}
    record_payload = (runtime.store.paths.records / f"{record.connection_id}.json").read_text(encoding="utf-8")
    assert "broker-only-token" not in record_payload
    assert captured["server_config"]["headers"] == {"Authorization": "Bearer broker-only-token"}


@pytest.mark.asyncio
async def test_broker_rejects_missing_static_environment_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config, _path = _config(tmp_path)
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.agent.external_mcp_servers = [
        {
            "name": "authenticated",
            "transport": "sse",
            "server_url": "https://mcp.example.test/sse",
            "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
            "tool_name_prefix": "authenticated",
        }
    ]
    monkeypatch.delenv("MCP_TEST_TOKEN", raising=False)
    monkeypatch.delenv("MCP_TEST_TOKEN_FILE", raising=False)
    record = reconcile_instance_mcp_connections(config)[0]
    runtime = _broker_runtime(config)

    with pytest.raises(PermissionError) as exc_info:
        await runtime._client(record, link=None)

    message = str(exc_info.value)
    assert "MCP_TEST_TOKEN" in message
    assert "${MCP_TEST_TOKEN}" not in message


@pytest.mark.asyncio
async def test_broker_says_when_the_shared_list_keeps_a_referenced_secret_from_it(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    """XAI_API_KEY does not go to the broker, so a broker MCP reference to it names the fix."""

    config, _path = _config(tmp_path)
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.agent.external_mcp_servers = [
        {
            "name": "search",
            "transport": "sse",
            "server_url": "https://mcp.example.test/sse",
            "headers": {"Authorization": "Bearer ${XAI_API_KEY}"},
            "tool_name_prefix": "search",
        }
    ]
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY_FILE", raising=False)
    record = reconcile_instance_mcp_connections(config)[0]
    runtime = _broker_runtime(config)

    with caplog.at_level("WARNING"), pytest.raises(PermissionError) as exc_info:
        await runtime._client(record, link=None)

    assert "secret_env_vars.yaml" in str(exc_info.value)
    assert "add secure_broker" in str(exc_info.value)
    assert "event=mcp_environment_reference_withheld names=XAI_API_KEY" in caplog.text


@pytest.mark.asyncio
async def test_broker_rejects_an_unreadable_environment_reference_secret_file(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    config, _path = _config(tmp_path)
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.agent.external_mcp_servers = [
        {
            "name": "authenticated",
            "transport": "sse",
            "server_url": "https://mcp.example.test/sse",
            "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
            "tool_name_prefix": "authenticated",
        }
    ]
    monkeypatch.delenv("MCP_TEST_TOKEN", raising=False)
    monkeypatch.setenv("MCP_TEST_TOKEN_FILE", str(tmp_path / "missing"))
    record = reconcile_instance_mcp_connections(config)[0]
    runtime = _broker_runtime(config)

    with caplog.at_level("WARNING"), pytest.raises(PermissionError, match="MCP_TEST_TOKEN_FILE"):
        await runtime._client(record, link=None)

    assert "event=mcp_environment_reference_file_unreadable name=MCP_TEST_TOKEN" in caplog.text


# -- network policy ----------------------------------------------------------------------


def test_network_policy_rejects_loopback_and_private_targets_unless_explicitly_allowed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(PermissionError, match="loopback"):
        validate_mcp_network_target(
            "https://mcp.example.test/mcp",
            allow_loopback=False,
            allow_private_networks=False,
        )
    assert validate_mcp_network_target(
        "https://mcp.example.test/mcp",
        allow_loopback=True,
        allow_private_networks=False,
    ) == ("127.0.0.1",)
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("10.0.0.8", 443))],
    )
    with pytest.raises(PermissionError, match="private"):
        validate_mcp_network_target(
            "https://mcp.example.test/mcp",
            allow_loopback=False,
            allow_private_networks=False,
        )
    assert validate_mcp_network_target(
        "https://mcp.example.test/mcp",
        allow_loopback=False,
        allow_private_networks=True,
    ) == ("10.0.0.8",)


def test_network_policy_allows_only_an_exact_named_private_host(monkeypatch) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("172.19.0.4", 11235))],
    )

    assert validate_mcp_network_target(
        "http://crawl4ai:11235/mcp/sse",
        allow_loopback=False,
        allow_private_networks=False,
        allowed_private_hosts=("Crawl4AI.",),
    ) == ("172.19.0.4",)
    with pytest.raises(PermissionError, match="private"):
        validate_mcp_network_target(
            "http://other-service:11235/mcp/sse",
            allow_loopback=False,
            allow_private_networks=False,
            allowed_private_hosts=("crawl4ai",),
        )


def test_private_host_allowlist_rejects_wildcards_and_urls() -> None:
    with pytest.raises(ValueError, match="exact host"):
        MCPNetworkPolicySettings(allowed_private_hosts=("*",))
    with pytest.raises(ValueError, match="exact host"):
        MCPNetworkPolicySettings(allowed_private_hosts=("http://crawl4ai",))




def test_processes_starting_together_record_one_baseline(tmp_path: Path, monkeypatch) -> None:
    """The manager and a startup agent reconcile at the same moment on a new instance.

    Each read "no baseline yet" and then recorded its own baseline, stamped
    with its own time; the second one stopped its process with "MCP static
    upgrade baseline is immutable once recorded". The barrier lets both reads
    happen before either write whenever the code allows it.
    """

    import itertools
    import threading
    from datetime import UTC, datetime, timedelta

    from mcp_control import registry

    config, _path = _config(tmp_path)
    config.agent.external_mcp_servers = [
        {"name": "shared", "transport": "streamable_http", "server_url": "https://shared.example.test/mcp"}
    ]
    start = datetime(2026, 10, 19, tzinfo=UTC)
    ticks = itertools.count()
    monkeypatch.setattr(registry, "utc_now", lambda: start + timedelta(seconds=next(ticks)))
    both_read = threading.Barrier(2, timeout=1.0)
    read_baseline = registry.MCPConnectionStore.baseline

    def baseline_after_both_read(self):
        value = read_baseline(self)
        try:
            both_read.wait()
        except threading.BrokenBarrierError:
            pass  # the other process could not read yet
        return value

    monkeypatch.setattr(registry.MCPConnectionStore, "baseline", baseline_after_both_read)
    results: list[tuple] = []
    errors: list[BaseException] = []

    def process_start() -> None:
        try:
            results.append(reconcile_instance_mcp_connections(config))
        except BaseException as exc:
            errors.append(exc)

    starts = [threading.Thread(target=process_start) for _ in range(2)]
    for thread in starts:
        thread.start()
    for thread in starts:
        thread.join(10)

    assert errors == []
    assert results[0] == results[1], "the second process finds the first one's records"
    paths = registry.resolve_mcp_connection_paths(config.instance_root_path)
    assert registry.MCPConnectionStore(paths).baseline() is not None
    assert len(list(paths.records.glob("mcp_conn_*.json"))) == 1
