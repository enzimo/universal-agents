"""Hosted MCP servers: install and remove as approved changes, start and stop through the broker.

Covers, with real Cedar and SQLite where a person decides: installing a
proposed server is one exact change (``hosted_mcp_server``) that a person
approves; applying it installs the definition stopped, activates its local
connection (the team tool ``mcp:<name>``) and, with authorization on, lets
every agent type the definition names use all its tools for everyone's work
(grants recorded with the same approval). Starting and stopping are ordinary
calls that reach the broker (mocked here) with the definition digest only;
removing needs the server stopped and is another change. Publishing is not an
operation. In the gateway's MCP controls only people the access service allows
may act, and an administrator's install there gives the same grants. The
broker checks only the definition digest for start and stop, decides each
call with the access gate, and the sidecar serves only callers that hold its
token.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from strands.interrupt import InterruptException

from access_catalog.catalog import mcp_tool_id
from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller, agent_caller_label
from assistant_gateway.application.website_application import (
    WebHostedMCPServerV1,
    WebMCPLifecycleResultV1,
)
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents.access_runtime import runtime_requests
from custom_agents.mcp_hosting_changes import HOSTED_MCP_SERVER_CHANGE_KIND, HostedMCPServerChangeService
from custom_agents.providers.mcp_connections import MCPConnectionAdministration
from custom_agents.providers.mcp_hosting import MCPHostingAdministration
from custom_messages.contracts.access_v2 import ChangeApprovalMode
from custom_messages.contracts.common_v1 import PrincipalDescriptorV1, PrincipalType
from custom_messages.contracts.mcp_v1 import (
    MCPDiscoveryRequestV1,
    MCPHostLifecycleRequestV1,
    MCPInvocationRequestV1,
)
from custom_tools.providers.capabilities import hosted_mcp_servers
from custom_tools.runtime_context import set_agent_ref
from mcp_control.hosting import (
    HostedMCPLifecycle,
    MCPHostedServerDefinitionV1,
    MCPHostedServerStore,
    resolve_mcp_hosting_paths,
)
from mcp_control.registry import (
    MCPConnectionLifecycle,
    MCPConnectionSource,
    MCPConnectionStore,
    MCPConnectionTransport,
    _new_record,
    descriptor_from_server_config,
    reconcile_instance_mcp_connections,
    resolve_mcp_connection_paths,
)
from secure_tool_broker.access_gate import REASON_IDENTITY_REJECTED
from secure_tool_broker.mcp_client import BrokerMCPClient
from secure_tool_broker.mcp_config import BrokerMCPConfig
from secure_tool_broker.mcp_hosting_runtime import MCPHostedProcessSupervisor
from secure_tool_broker.mcp_runtime import BrokerMCPRuntime
from storage.access_repository import AccessRepository
from tests.broker_access_support import caller_token, open_gate, signed_task_context
from tests.change_approval_support import (
    ToolCall,
    agreement,
    change_agent,
    decide,
    needs_cedar,
    run_apply_change,
    stored_request,
    use_signed_contexts,
)
from tests.test_mcp_connections import ADMIN, MEMBER, _gateway

TEAM = "team_host"
ECHO_TOOL = {
    "name": "echo",
    "description": "Echo reviewed arguments.",
    "behavior": "echo_arguments",
    "input_schema": {"type": "object"},
}


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


def _free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _supervisor_config(tmp_path: Path, port: int) -> SimpleNamespace:
    return SimpleNamespace(
        instance_root_path=tmp_path,
        mcp=SimpleNamespace(
            hosting=SimpleNamespace(
                bind_host="127.0.0.1",
                port_range_start=port,
                port_range_end=port,
            )
        ),
    )


def _hosting_config(tmp_path: Path) -> tuple[GatewayInstanceConfig, Path]:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="hosting-test", instance_root=str(root), team_id=TEAM, timezone="UTC"
    )
    port = _free_port()
    config.mcp.hosting.enabled = True
    config.mcp.hosting.port_range_start = port
    config.mcp.hosting.port_range_end = port
    return config, config.save(root / "instance_config.yaml")


def _propose(agent: Any, config: GatewayInstanceConfig, *targets: str) -> MCPHostedServerDefinitionV1:
    return MCPHostingAdministration(config).propose_by_agent(
        agent,
        server_name="echo",
        purpose="Return an attested copy of reviewed arguments.",
        tools=[ECHO_TOOL],
        target_agent_types=targets or ("TeamArchitect",),
    )


def _call_tool(agent: Any, tool: Any, **arguments: Any) -> dict[str, Any]:
    """Call a Strands tool function as the agent, the way the runtime does."""

    def call() -> Any:
        set_agent_ref(agent)
        result = tool._tool_func(**arguments)
        return asyncio.run(result) if asyncio.iscoroutine(result) else result

    return json.loads(contextvars.copy_context().run(call))


def _hosted_connection(config: GatewayInstanceConfig, hosted_server_id: str):
    store = MCPConnectionAdministration.for_instance_config(config).store
    [record] = [item for item in store.list_records() if item.descriptor.source_locator == f"hosted:{hosted_server_id}"]
    return store, record


class _Broker:
    """The secure broker's hosted-server route, reached over HTTP by the runtime."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.requests: list[dict[str, Any]] = []
        monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", "http://broker.test")
        monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", caller_token(CALLER_MANAGER))
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(
                {
                    "url": str(request.url),
                    "authorization": request.headers.get("Authorization"),
                    "body": json.loads(request.content),
                }
            )
            operation = request.url.path.rsplit("/", 1)[-1]
            status = "running" if operation == "start" else "stopped"
            return httpx.Response(self.status_code, json={"status": status} if self.status_code == 200 else None)

        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
        )


# -- installing and removing are approved changes ----------------------------------


@needs_cedar
def test_a_person_approves_installing_a_hosted_server_for_the_agent_types_it_names(tmp_path: Path) -> None:
    config, path = _hosting_config(tmp_path)
    agent = change_agent(config, path, authorization_enabled=True)
    definition = _propose(agent, config, "TeamArchitect", "WebResearchAgent")

    plan = _call_tool(
        agent, hosted_mcp_servers, action="plan_install", hosted_server_id=definition.hosted_server_id,
        reason="Let the team echo reviewed arguments.",
    )
    call = ToolCall()
    with pytest.raises(InterruptException):
        run_apply_change(agent, plan["change_id"], call)
    waiting = MCPHostingAdministration(config).store.require(definition.hosted_server_id).lifecycle
    request = stored_request(agent, call)
    decide(agent, call)
    applied = run_apply_change(agent, plan["change_id"], call)
    again = run_apply_change(agent, plan["change_id"], call)

    assert plan["authority_mode"] == "person_approval"
    assert plan["definition_digest"] == definition.definition_digest
    assert waiting == HostedMCPLifecycle.PENDING
    assert request.change.kind == HOSTED_MCP_SERVER_CHANGE_KIND
    assert request.change.title == "Install the hosted MCP server echo"
    assert request.change.summary.startswith("Install the hosted MCP server echo with ")
    assert request.change.summary.endswith(" for TeamArchitect, WebResearchAgent. Its tools work once it is started.")
    assert any("TeamArchitect, WebResearchAgent" in line for line in request.change.effect)
    assert applied["status"] == "applied", applied
    assert applied["result"]["status"] == "stopped"
    installed = MCPHostingAdministration(config).store.require(definition.hosted_server_id)
    assert installed.lifecycle == HostedMCPLifecycle.STOPPED
    assert installed.installed_by_principal_id == config.user.canonical_user_id
    store, connection = _hosted_connection(config, definition.hosted_server_id)
    assert applied["result"]["connection_id"] == connection.connection_id
    assert connection.descriptor.source == MCPConnectionSource.HOSTED
    assert connection.lifecycle == MCPConnectionLifecycle.ACTIVE
    assert connection.approval is not None
    assert connection.approval.mode is ChangeApprovalMode.PERSON_APPROVAL
    assert connection.approval.approved_by == config.user.canonical_user_id
    assert connection.approval.access_request_id == request.request_id
    grants = [
        grant for grant in runtime_requests(agent).service.store.list_team(TEAM) if grant.target.id == "mcp:echo"
    ]
    assert sorted(grant.subject.id for grant in grants) == [f"{TEAM}/TeamArchitect", f"{TEAM}/WebResearchAgent"]
    assert {(grant.level.value, grant.for_person_id, grant.approval_id) for grant in grants} == {
        ("full", None, request.request_id)
    }, "one approval lets both types use every tool, for everyone's work"
    assert sorted(applied["result"]["grant_ids"]) == sorted(grant.grant_id for grant in grants)
    assert any("full access to mcp:echo" in line for line in request.change.effect)
    assert again["status"] == "refused" and "already applied" in again["reason"]


def test_starting_stopping_and_removing_a_hosted_server(tmp_path: Path, monkeypatch) -> None:
    config, path = _hosting_config(tmp_path)
    agent = change_agent(config, path, authorization_enabled=False)
    definition = _propose(agent, config)
    service = HostedMCPServerChangeService(agent)
    installed = service.apply(
        service.stage(hosted_server_id=definition.hosted_server_id, operation="install", reason="Install it.")[
            "change_id"
        ],
        agreement(),
    )
    broker = _Broker(monkeypatch)

    started = _call_tool(agent, hosted_mcp_servers, action="start", hosted_server_id=definition.hosted_server_id)
    with pytest.raises(PermissionError, match="stop it first"):
        service.stage(hosted_server_id=definition.hosted_server_id, operation="remove", reason="Remove it.")
    stopped = _call_tool(agent, hosted_mcp_servers, action="stop", hosted_server_id=definition.hosted_server_id)
    removal = service.stage(hosted_server_id=definition.hosted_server_id, operation="remove", reason="Remove it.")
    removed = service.apply(removal["change_id"], agreement())

    assert installed["authority"]["mode"] == "authorization_disabled_agreement"
    assert installed["grant_ids"] == [], "with authorization off there are no grants"
    _store, connection = _hosted_connection(config, definition.hosted_server_id)
    assert connection.approval is not None
    assert (connection.approval.mode, connection.approval.approved_by) == (ChangeApprovalMode.AGREEMENT, "agreement")
    assert (started["status"], stopped["status"]) == ("running", "stopped")
    assert [item["url"] for item in broker.requests] == [
        f"http://broker.test/v1/mcp/hosted/{definition.hosted_server_id}/start",
        f"http://broker.test/v1/mcp/hosted/{definition.hosted_server_id}/stop",
    ]
    assert {item["authorization"] for item in broker.requests} == {f"Bearer {caller_token(CALLER_MANAGER)}"}
    assert broker.requests[0]["body"] == {
        "schema_version": 1,
        "record_type": "mcp_host_lifecycle_request",
        "definition_digest": definition.definition_digest,
    }, "the broker gets the definition digest only; the per-call hook decided who asked"
    assert removed["status"] == "removed" and removed["recoverable"] is True
    paths = resolve_mcp_hosting_paths(config)
    assert not (paths.definitions / f"{definition.hosted_server_id}.json").exists()
    assert (paths.removed / f"{definition.hosted_server_id}.json").is_file()
    store, connection = _hosted_connection(config, definition.hosted_server_id)
    assert connection.lifecycle == MCPConnectionLifecycle.DISABLED, "a removed server leaves the catalog"


def test_a_refused_broker_call_leaves_the_hosted_server_as_it_was(tmp_path: Path, monkeypatch) -> None:
    config, path = _hosting_config(tmp_path)
    agent = change_agent(config, path, authorization_enabled=False)
    definition = _propose(agent, config)
    MCPHostingAdministration(config).install(
        definition.hosted_server_id, approval=MCPConnectionAdministration.person_approval("admin-1")
    )
    _Broker(monkeypatch, status_code=403)

    result = _call_tool(agent, hosted_mcp_servers, action="start", hosted_server_id=definition.hosted_server_id)

    assert "403" in result["error"]
    assert MCPHostingAdministration(config).store.require(definition.hosted_server_id).lifecycle == (
        HostedMCPLifecycle.STOPPED
    )


def test_publishing_a_hosted_server_is_not_an_operation(tmp_path: Path, monkeypatch) -> None:
    """Publishing stays unavailable until an operator registers trusted ingress."""

    config, path = _hosting_config(tmp_path)
    agent = change_agent(config, path, authorization_enabled=False)
    definition = _propose(agent, config)
    administration = MCPHostingAdministration(config)
    broker = _Broker(monkeypatch)

    with pytest.raises(ValueError, match="install or remove"):
        HostedMCPServerChangeService(agent).stage(
            hosted_server_id=definition.hosted_server_id, operation="publish", reason="Publish it."
        )
    for operation in ("publish", "install"):
        with pytest.raises(ValueError, match="started or stopped here"):
            asyncio.run(administration.change_running_state(definition.hosted_server_id, operation, by="agent_team_lead"))
        refused = _call_tool(
            agent, hosted_mcp_servers, action=operation, hosted_server_id=definition.hosted_server_id
        )
        # Installing is plan_install, a change a person approves; publishing is no action at all.
        assert f"hosted_mcp_servers has no action {operation!r}" in refused["error"]
    with pytest.raises(ValueError, match="Unsupported hosted MCP lifecycle action"):
        asyncio.run(
            administration.perform_by_person(
                hosted_server_id=definition.hosted_server_id,
                action="publish",
                expected_definition_digest=definition.definition_digest,
                expected_lifecycle="pending",
                person_id=ADMIN,
            )
        )
    assert broker.requests == []
    assert administration.store.require(definition.hosted_server_id).lifecycle == HostedMCPLifecycle.PENDING


def test_hosted_server_plans_belong_to_the_team_lead_and_the_task_that_staged_them(tmp_path: Path) -> None:
    config, path = _hosting_config(tmp_path)
    lead = change_agent(config, path, authorization_enabled=False)
    definition = _propose(lead, config)
    specialist = change_agent(config, path, authorization_enabled=False, is_team_lead=False)

    with pytest.raises(PermissionError, match="team lead stages"):
        HostedMCPServerChangeService(specialist).stage(
            hosted_server_id=definition.hosted_server_id, operation="install", reason="Install it."
        )
    plan = HostedMCPServerChangeService(lead).stage(
        hosted_server_id=definition.hosted_server_id, operation="install", reason="Install it."
    )
    lead.task_manager.current_task_id = "task_other"
    with pytest.raises(PermissionError, match="another agent, person or task"):
        HostedMCPServerChangeService(lead).apply(plan["change_id"], agreement())
    assert MCPHostingAdministration(config).store.require(definition.hosted_server_id).lifecycle == (
        HostedMCPLifecycle.PENDING
    )


def test_a_hosted_server_changed_after_approval_is_refused(tmp_path: Path) -> None:
    config, path = _hosting_config(tmp_path)
    agent = change_agent(config, path, authorization_enabled=False)
    definition = _propose(agent, config)
    service = HostedMCPServerChangeService(agent)
    plan = service.stage(hosted_server_id=definition.hosted_server_id, operation="install", reason="Install it.")
    MCPHostingAdministration(config).install(
        definition.hosted_server_id, approval=MCPConnectionAdministration.person_approval(ADMIN)
    )

    refused = run_apply_change(agent, plan["change_id"], confirmed=True)

    assert refused["status"] == "refused"
    assert "stopped; it cannot be installed now" in refused["reason"]


# -- the gateway's MCP controls ------------------------------------------------------


@needs_cedar
@pytest.mark.asyncio
async def test_only_people_the_access_service_allows_control_hosted_servers_in_the_gateway(
    tmp_path: Path, monkeypatch, request
) -> None:
    config, path = _hosting_config(tmp_path)
    definition = _propose(change_agent(config, path, authorization_enabled=False), config, "TeamArchitect")
    gateway = _gateway(config, authorization_enabled=True, request=request)
    broker = _Broker(monkeypatch)

    async def control(action: str, *, person_id: str | None, lifecycle: str) -> dict:
        return await gateway.control_mcp_lifecycle(
            resource_kind="hosted_server",
            resource_id=definition.hosted_server_id,
            action=action,
            expected_digest=definition.definition_digest,
            expected_lifecycle=lifecycle,
            person_id=person_id,
        )

    [member_view] = gateway.list_hosted_mcp_servers(person_id=MEMBER)
    [admin_view] = gateway.list_hosted_mcp_servers(person_id=ADMIN)
    for person_id in (MEMBER, "stranger-1", None):
        with pytest.raises(PermissionError, match="Only an administrator"):
            await control("install", person_id=person_id, lifecycle="pending")
    unchanged = MCPHostingAdministration(config).store.require(definition.hosted_server_id).lifecycle
    installed = await control("install", person_id=ADMIN, lifecycle="pending")
    started = await control("start", person_id=ADMIN, lifecycle="stopped")
    [running_view] = gateway.list_hosted_mcp_servers(person_id=ADMIN)

    WebHostedMCPServerV1.model_validate(member_view)
    WebHostedMCPServerV1.model_validate(admin_view)
    assert member_view["available_actions"] == []
    assert admin_view["available_actions"] == ["install", "remove"]
    assert unchanged == HostedMCPLifecycle.PENDING
    WebMCPLifecycleResultV1.model_validate(installed)
    assert (installed["status"], started["status"]) == ("stopped", "running")
    assert running_view["available_actions"] == ["stop"]
    _store, connection = _hosted_connection(config, definition.hosted_server_id)
    assert connection.approval is not None
    assert (connection.approval.mode, connection.approval.approved_by) == (ChangeApprovalMode.PERSON_APPROVAL, ADMIN)
    grants = [
        grant
        for grant in AccessRepository(config.manager_storage_settings).list_team(config.manager.team_id)
        if grant.target.id == connection.descriptor.capability_id
    ]
    assert [(grant.subject.id, grant.granted_by.id, grant.for_person_id) for grant in grants] == [
        (f"{TEAM}/TeamArchitect", ADMIN, None)
    ], "an administrator's install lets the named agent types use it"
    assert len(broker.requests) == 1


# -- the broker ------------------------------------------------------------------------


def _broker_runtime(config: GatewayInstanceConfig, gate: Any = None) -> BrokerMCPRuntime:
    broker_config = BrokerMCPConfig.from_runtime_settings(
        instance_root_path=str(config.instance_root_path),
        agent=config.agent,
        agent_types=config.agent_types,
        mcp_profiles=config.mcp_profiles,
        mcp=config.mcp,
    )
    return BrokerMCPRuntime(
        config=broker_config,
        access_gate=gate or open_gate(team_id=config.manager.team_id, instance_root=config.instance_root_path),
    )


@pytest.mark.asyncio
async def test_broker_starts_or_stops_only_the_exact_installed_definition(tmp_path: Path, monkeypatch) -> None:
    config, path = _hosting_config(tmp_path)
    definition = _propose(change_agent(config, path, authorization_enabled=False), config)
    runtime = _broker_runtime(config)
    started: list[str] = []

    async def start(hosted_server_id: str) -> dict:
        started.append(hosted_server_id)
        return {"status": "running"}

    monkeypatch.setattr(runtime.hosting, "start", start)
    try:
        with pytest.raises(PermissionError, match="changed before dispatch"):
            await runtime.change_host_lifecycle(
                definition.hosted_server_id, MCPHostLifecycleRequestV1(definition_digest="0" * 64), operation="start"
            )
        with pytest.raises(ValueError, match="Unsupported hosted MCP broker operation"):
            await runtime.change_host_lifecycle(
                definition.hosted_server_id,
                MCPHostLifecycleRequestV1(definition_digest=definition.definition_digest),
                operation="publish",
            )
        result = await runtime.change_host_lifecycle(
            definition.hosted_server_id,
            MCPHostLifecycleRequestV1(definition_digest=definition.definition_digest),
            operation="start",
        )
    finally:
        await runtime.close()

    assert result == {"status": "running"}
    assert started == [definition.hosted_server_id]


@pytest.mark.asyncio
async def test_declarative_host_sidecar_requires_broker_token_and_serves_tools(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config = _supervisor_config(tmp_path, port)
    definition = MCPHostedServerDefinitionV1.build(
        server_name="echo",
        purpose="Return an attested copy of reviewed arguments.",
        owner_canonical_user_id="usr_owner",
        team_id=TEAM,
        target_agent_types=("TeamArchitect",),
        tools=(ECHO_TOOL,),
        bind_host="127.0.0.1",
        port=port,
        lifecycle=HostedMCPLifecycle.STOPPED,
    )
    store = MCPHostedServerStore(resolve_mcp_hosting_paths(config))
    store.save(definition)
    supervisor = MCPHostedProcessSupervisor(config)
    provider = None
    try:
        result = await supervisor.start(definition.hosted_server_id)
        assert result["status"] == "running"
        async with httpx.AsyncClient(timeout=5.0) as client:
            denied = await client.post(f"http://127.0.0.1:{port}/mcp")
        assert denied.status_code == 403

        token = (
            (supervisor.paths.tokens / f"{definition.hosted_server_id}.token")
            .read_text(encoding="utf-8")
            .strip()
        )
        # The broker's own MCP client, as it talks to hosted servers.
        provider = BrokerMCPClient(
            {
                "name": "echo",
                "transport": "streamable_http",
                "server_url": f"http://127.0.0.1:{port}/mcp",
                "headers": {"Authorization": f"Bearer {token}"},
                "tool_name_prefix": "echo",
            }
        )
        tools = await provider.list_tools()
        assert [tool.name for tool in tools] == ["echo"]
        call_result = await provider.call_tool("echo", {"hello": "world"})
        assert call_result.isError is False
        assert call_result.structuredContent == {"arguments": {"hello": "world"}}
    finally:
        if provider is not None:
            await provider.close()
        await supervisor.close()

    log_path = supervisor.paths.logs / f"{definition.hosted_server_id}.log"
    assert log_path.is_file()
    assert log_path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_broker_discovers_authenticated_static_server_from_env_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config, _path = _hosting_config(tmp_path)
    port = config.mcp.hosting.port_range_start
    config.mcp.network_policy.allow_loopback = True
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    definition = MCPHostedServerDefinitionV1.build(
        server_name="authenticated-echo",
        purpose="Verify broker-owned static environment materialization.",
        owner_canonical_user_id=config.user.canonical_user_id,
        team_id=config.manager.team_id,
        target_agent_types=("TeamArchitect",),
        tools=({"name": "echo", "behavior": "echo_arguments", "input_schema": {"type": "object"}},),
        bind_host="127.0.0.1",
        port=port,
        lifecycle=HostedMCPLifecycle.STOPPED,
    )
    MCPHostedServerStore(resolve_mcp_hosting_paths(config)).save(definition)
    supervisor = MCPHostedProcessSupervisor(config)
    runtime = None
    try:
        await supervisor.start(definition.hosted_server_id)
        token = (
            (supervisor.paths.tokens / f"{definition.hosted_server_id}.token")
            .read_text(encoding="utf-8")
            .strip()
        )
        monkeypatch.setenv("MCP_STATIC_AUTH_TOKEN", token)
        config.agent.external_mcp_servers = [
            {
                "name": "authenticated-echo",
                "transport": "streamable_http",
                "server_url": f"http://127.0.0.1:{port}/mcp",
                "headers": {
                    "Authorization": "Bearer ${MCP_STATIC_AUTH_TOKEN}",
                },
                "tool_name_prefix": "authenticated_echo",
                "include_tools": ["echo"],
            }
        ]
        record = reconcile_instance_mcp_connections(config)[0]
        runtime = _broker_runtime(config)

        discovered = await runtime.discover_tools(
            record.connection_id,
            MCPDiscoveryRequestV1(
                record_digest=record.record_digest,
                named_agent_type="TeamArchitect",
            ),
            caller=BrokerCaller(CALLER_MANAGER),
        )
    finally:
        if runtime is not None:
            await runtime.close()
        await supervisor.close()

    assert [item.name for item in discovered.tools] == ["echo"]


@pytest.mark.asyncio
async def test_broker_dispatches_exact_hosted_operation_through_final_mcp_pep(
    tmp_path: Path,
) -> None:
    config, _path = _hosting_config(tmp_path)
    port = config.mcp.hosting.port_range_start
    definition = MCPHostedServerDefinitionV1.build(
        server_name="broker-echo",
        purpose="Exercise the final broker authorization boundary.",
        owner_canonical_user_id=config.user.canonical_user_id,
        team_id=config.manager.team_id,
        target_agent_types=("TeamArchitect",),
        tools=({"name": "echo", "behavior": "echo_arguments", "input_schema": {"type": "object"}},),
        bind_host="127.0.0.1",
        port=port,
        lifecycle=HostedMCPLifecycle.RUNNING,
    )
    MCPHostedServerStore(resolve_mcp_hosting_paths(config)).save(definition)
    descriptor = descriptor_from_server_config(
        {
            "name": definition.server_name,
            "transport": MCPConnectionTransport.STREAMABLE_HTTP.value,
            "server_url": f"http://127.0.0.1:{port}/mcp",
            "tool_name_prefix": definition.server_name,
            "include_tools": ["echo"],
        },
        source=MCPConnectionSource.HOSTED,
        source_locator=f"hosted:{definition.hosted_server_id}",
        team_id=config.manager.team_id,
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    now = datetime.now(timezone.utc)
    approval = MCPConnectionAdministration.person_approval(config.user.canonical_user_id)
    record = _new_record(
        descriptor,
        lifecycle=MCPConnectionLifecycle.ACTIVE,
        target_agent_types=("TeamArchitect",),
        approval=approval,
        now=now,
    )
    store = MCPConnectionStore(resolve_mcp_connection_paths(config.instance_root_path))
    store.save(record)
    principal = PrincipalDescriptorV1(
        principal_type=PrincipalType.AGENT,
        principal_id="agent_hosted_pep",
        on_behalf_of_user_id=config.user.canonical_user_id,
        team_id=config.manager.team_id,
        roles=("team_lead",),
        named_agent_type="TeamArchitect",
    )

    # Every invocation passes the access gate (verified identity, then the tool catalog).
    gate = open_gate(team_id=config.manager.team_id, instance_root=config.instance_root_path)
    gate_checks = []
    check = gate.check

    def recording_check(caller, resolution, context):
        result = check(caller, resolution, context)
        gate_checks.append(result)
        return result

    gate.check = recording_check
    task_context = signed_task_context(config.user.canonical_user_id, team_id=config.manager.team_id)
    manager = BrokerCaller(CALLER_MANAGER)
    runtime = _broker_runtime(config, gate)
    try:
        discovered = await runtime.discover_tools(
            record.connection_id,
            MCPDiscoveryRequestV1(record_digest=record.record_digest, named_agent_type="TeamArchitect"),
            caller=manager,
        )
        current = store.require(record.connection_id)
        cached_discovery = await runtime.discover_tools(
            record.connection_id,
            MCPDiscoveryRequestV1(record_digest=current.record_digest, named_agent_type="TeamArchitect"),
            caller=manager,
        )

        def echo(arguments, *, signed=task_context):
            return MCPInvocationRequestV1(
                record_digest=current.record_digest,
                operation_name="echo",
                arguments=arguments,
                tool_use_id="tool_use_brokered_echo",
                task_id="task_hosted_pep",
                principal=principal,
                task_context=signed,
            )

        with pytest.raises(PermissionError, match=REASON_IDENTITY_REJECTED):
            await runtime.invoke(record.connection_id, echo({"brokered": "unsigned"}, signed=None), caller=manager)
        with pytest.raises(PermissionError, match="cannot act as TeamArchitect"):
            await runtime.invoke(
                record.connection_id,
                echo({"brokered": "other-process"}),
                caller=BrokerCaller(agent_caller_label("ResearchAgent")),
            )
        result = await runtime.invoke(record.connection_id, echo({"brokered": True}), caller=manager)
    finally:
        await runtime.close()

    assert [item.name for item in discovered.tools] == ["echo"]
    assert cached_discovery == discovered
    assert result.is_error is not True
    assert result.structured_content == {"arguments": {"brokered": True}}
    assert [check.allowed for check in gate_checks] == [False, False, True]
    allowed = gate_checks[-1]
    assert allowed.resolution is not None
    assert allowed.resolution.tool_id == mcp_tool_id("broker-echo")
    assert allowed.resolution.operation == "echo"
    assert allowed.person_id == config.user.canonical_user_id


def test_static_response_is_schema_validated_before_install() -> None:
    with pytest.raises(Exception, match="not of type"):
        MCPHostedServerDefinitionV1.build(
            server_name="invalid",
            purpose="Invalid fixture.",
            owner_canonical_user_id="usr_owner",
            team_id=TEAM,
            target_agent_types=("TeamArchitect",),
            tools=(
                {
                    "name": "answer",
                    "behavior": "static_response",
                    "input_schema": {"type": "object"},
                    "output_schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "integer"}},
                        "required": ["answer"],
                    },
                    "static_response": {"answer": "not-an-integer"},
                },
            ),
            bind_host="127.0.0.1",
            port=18700,
            lifecycle=HostedMCPLifecycle.PENDING,
        )
