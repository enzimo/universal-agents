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
import contextlib
import contextvars
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

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
from tests.task_work_support import set_agent_ref
from mcp_control.hosting import (
    HOSTED_MCP_START_FAILED,
    HostedMCPLifecycle,
    HostedMCPStartError,
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
from secure_tool_broker.mcp_hosting_runtime import HostedMCPPortInUse, MCPHostedProcessSupervisor
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
from tests.tool_result_support import tool_json

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


def _supervisor_config(tmp_path: Path) -> SimpleNamespace:
    """What the sidecar supervisor reads from the instance settings: where the instance lives."""

    return SimpleNamespace(instance_root_path=tmp_path)


async def _start_on_a_free_port(
    supervisor: MCPHostedProcessSupervisor, save: Callable[[int], MCPHostedServerDefinitionV1]
) -> MCPHostedServerDefinitionV1:
    """Save a definition for a free port and start its sidecar; use another port when that one was taken.

    A sidecar listens on the port its definition names, so the test picks a free port and
    closes it before the sidecar binds it. Another test's server can take it in between; the
    start then says so (``HostedMCPPortInUse``) instead of reporting that server as running.
    """

    for _attempt in range(3):
        definition = save(_free_port())
        try:
            await supervisor.start(definition.hosted_server_id)
        except HostedMCPPortInUse:
            continue
        return definition
    raise AssertionError("Every port the test picked was taken before the sidecar could listen on it.")


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

    return tool_json(contextvars.copy_context().run(call))


def _hosted_connection(config: GatewayInstanceConfig, hosted_server_id: str):
    store = MCPConnectionAdministration.for_instance_config(config).store
    [record] = [item for item in store.list_records() if item.descriptor.source_locator == f"hosted:{hosted_server_id}"]
    return store, record


class _Broker:
    """The secure broker's hosted-server route, reached over HTTP by the runtime."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        status_code: int = 200,
        text: str = "",
        answer: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.answer = answer
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
            if self.answer is not None:
                return httpx.Response(self.status_code, json=self.answer)
            if self.status_code != 200:
                return httpx.Response(self.status_code, text=self.text)
            return httpx.Response(self.status_code, json={"status": status})

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


@pytest.mark.parametrize(
    ("status_code", "text"),
    [(403, ""), (503, '{"error": "The broker is busy."}')],
    ids=["refused", "unavailable-without-the-start-failure-code"],
)
def test_a_broker_answer_that_is_not_a_start_failure_leaves_the_hosted_server_as_it_was(
    tmp_path: Path, monkeypatch, status_code: int, text: str
) -> None:
    config, path = _hosting_config(tmp_path)
    agent = change_agent(config, path, authorization_enabled=False)
    definition = _propose(agent, config)
    MCPHostingAdministration(config).install(
        definition.hosted_server_id, approval=MCPConnectionAdministration.person_approval("admin-1")
    )
    _Broker(monkeypatch, status_code=status_code, text=text)

    result = _call_tool(agent, hosted_mcp_servers, action="start", hosted_server_id=definition.hosted_server_id)

    assert str(status_code) in result["error"], "an ordinary HTTP error, not a start failure"
    assert MCPHostingAdministration(config).store.require(definition.hosted_server_id).lifecycle == (
        HostedMCPLifecycle.STOPPED
    )


PORT_TAKEN = (
    "The hosted MCP server echo cannot start: 127.0.0.1:41000 is already in use, most likely by its "
    "sidecar left over from an earlier broker run."
)
# What the broker answers when it could not start a sidecar (HostedMCPStartError).
START_FAILED = {"error": PORT_TAKEN, "reason_code": HOSTED_MCP_START_FAILED}


def test_a_start_the_broker_could_not_finish_tells_the_agent_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, path = _hosting_config(tmp_path)
    agent = change_agent(config, path, authorization_enabled=False)
    definition = _propose(agent, config)
    MCPHostingAdministration(config).install(
        definition.hosted_server_id, approval=MCPConnectionAdministration.person_approval("admin-1")
    )
    _Broker(monkeypatch, status_code=503, answer=START_FAILED)

    result = _call_tool(agent, hosted_mcp_servers, action="start", hosted_server_id=definition.hosted_server_id)

    assert result["error"] == PORT_TAKEN
    assert MCPHostingAdministration(config).store.require(definition.hosted_server_id).lifecycle == (
        HostedMCPLifecycle.STOPPED
    )


@pytest.mark.asyncio
async def test_a_person_whose_start_the_broker_could_not_finish_sees_why(tmp_path: Path, monkeypatch) -> None:
    """The gateway's MCP controls (Web Chat, /mcp) start a server through ``perform_by_person``."""

    config, path = _hosting_config(tmp_path)
    definition = _propose(change_agent(config, path, authorization_enabled=False), config)
    administration = MCPHostingAdministration(config)
    administration.install(definition.hosted_server_id, approval=MCPConnectionAdministration.person_approval(ADMIN))
    _Broker(monkeypatch, status_code=503, answer=START_FAILED)

    with pytest.raises(HostedMCPStartError) as failed:
        await administration.perform_by_person(
            hosted_server_id=definition.hosted_server_id,
            action="start",
            expected_definition_digest=definition.definition_digest,
            expected_lifecycle="stopped",
            person_id=ADMIN,
        )

    assert str(failed.value) == PORT_TAKEN
    assert isinstance(failed.value, RuntimeError), "Web Chat answers it with 409 and its text, /mcp says it"
    assert administration.store.require(definition.hosted_server_id).lifecycle == HostedMCPLifecycle.STOPPED


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
    config = _supervisor_config(tmp_path)
    supervisor = MCPHostedProcessSupervisor(config)
    provider = None
    try:
        definition = await _start_on_a_free_port(supervisor, lambda port: _saved(config, _echo_definition(port)))
        port = definition.port
        result = await supervisor.start(definition.hosted_server_id)
        assert result["status"] == "running" and result["already_running"] is True
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
    config.mcp.network_policy.allow_loopback = True
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"

    def definition_for(port: int) -> MCPHostedServerDefinitionV1:
        return MCPHostedServerDefinitionV1.build(
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

    supervisor = MCPHostedProcessSupervisor(config)
    runtime = None
    try:
        definition = await _start_on_a_free_port(supervisor, lambda port: _saved(config, definition_for(port)))
        port = definition.port
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

    def definition_for(port: int) -> MCPHostedServerDefinitionV1:
        return MCPHostedServerDefinitionV1.build(
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

    # Every invocation passes the access gate (verified identity, then the tool catalog).
    gate = open_gate(team_id=config.manager.team_id, instance_root=config.instance_root_path)
    runtime = _broker_runtime(config, gate)
    try:
        # Started here so a taken port can be swapped; the discovery below finds it running.
        definition = await _start_on_a_free_port(runtime.hosting, lambda port: _saved(config, definition_for(port)))
    except BaseException:
        await runtime.close()
        raise
    port = definition.port
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

    gate_checks = []
    check = gate.check

    def recording_check(caller, resolution, context):
        result = check(caller, resolution, context)
        gate_checks.append(result)
        return result

    gate.check = recording_check
    task_context = signed_task_context(config.user.canonical_user_id, team_id=config.manager.team_id)
    manager = BrokerCaller(CALLER_MANAGER)
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


# -- a sidecar is running only when the sidecar this start launched says so ----------------


def _saved(config: Any, definition: MCPHostedServerDefinitionV1) -> MCPHostedServerDefinitionV1:
    MCPHostedServerStore(resolve_mcp_hosting_paths(config)).save(definition)
    return definition


def _echo_definition(port: int, *, lifecycle: HostedMCPLifecycle = HostedMCPLifecycle.STOPPED) -> MCPHostedServerDefinitionV1:
    return MCPHostedServerDefinitionV1.build(
        server_name="echo",
        purpose="Return an attested copy of reviewed arguments.",
        owner_canonical_user_id="usr_owner",
        team_id=TEAM,
        target_agent_types=("TeamArchitect",),
        tools=(ECHO_TOOL,),
        bind_host="127.0.0.1",
        port=port,
        lifecycle=lifecycle,
    )


@pytest.mark.asyncio
async def test_a_sidecar_left_over_from_an_earlier_broker_stops_the_start_and_says_what_to_do(
    tmp_path: Path,
) -> None:
    config = _supervisor_config(tmp_path)
    earlier_broker = MCPHostedProcessSupervisor(config)
    restarted_broker = MCPHostedProcessSupervisor(config)
    try:
        definition = await _start_on_a_free_port(earlier_broker, lambda port: _saved(config, _echo_definition(port)))
        port = definition.port
        # The earlier broker stopped without stopping its sidecar, which runs in its own session.

        with pytest.raises(HostedMCPPortInUse) as refused:
            await restarted_broker.start(definition.hosted_server_id)

        message = str(refused.value)
        assert f"127.0.0.1:{port} is already in use" in message, message
        assert "Stop that process" in message, message
        assert earlier_broker._processes[definition.hosted_server_id].returncode is None
        assert definition.hosted_server_id not in restarted_broker._processes
        # Not set to run: the person who started it got the reason in the answer.
        assert MCPHostedServerStore(resolve_mcp_hosting_paths(config)).start_failure(definition.hosted_server_id) is None
    finally:
        await restarted_broker.close()
        await earlier_broker.close()


class _Running:
    returncode = None


@pytest.mark.asyncio
async def test_an_earlier_starts_ready_line_in_the_sidecar_log_does_not_count(tmp_path: Path) -> None:
    supervisor = MCPHostedProcessSupervisor(_supervisor_config(tmp_path))
    log_path = tmp_path / "sidecar.log"
    log_path.write_text("INFO:     Uvicorn running on http://127.0.0.1:41000 (Press CTRL+C to quit)\n")

    with pytest.raises(TimeoutError):
        await supervisor._wait_ready(
            _echo_definition(41000),
            _Running(),  # type: ignore[arg-type]
            log_path=log_path,
            offset=log_path.stat().st_size,
            timeout_seconds=0.5,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        HostedMCPPortInUse(PORT_TAKEN),
        HostedMCPStartError("The hosted MCP server echo stopped while starting, with status 1."),
    ],
    ids=["port-taken", "stopped-while-starting"],
)
async def test_the_broker_answers_a_start_it_could_not_finish_with_the_reason_never_a_refusal(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from secure_tool_broker.service import SecureToolBrokerService
    from tests.broker_access_support import BROKER_ROOT, caller_headers

    class Runtime:
        """A person's start, and an agent's listing or call, which start a server set to run."""

        raised: Exception = error

        async def change_host_lifecycle(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
            raise self.raised

        async def discover_tools(self, *_args: object, **_kwargs: object) -> Any:
            raise self.raised

        async def invoke(self, *_args: object, **_kwargs: object) -> Any:
            raise self.raised

    runtime = Runtime()
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    monkeypatch.setattr(service, "_get_mcp_runtime", lambda: runtime)
    principal = PrincipalDescriptorV1(
        principal_type=PrincipalType.AGENT, principal_id="agent_1", team_id=TEAM, roles=("worker",),
        named_agent_type="TeamArchitect",
    )
    invocation = MCPInvocationRequestV1(
        record_digest="0" * 64, operation_name="echo", tool_use_id="call-1", principal=principal
    ).model_dump(mode="json", by_alias=True, exclude_none=True)
    discovery = {"record_digest": "0" * 64, "named_agent_type": "TeamArchitect"}
    async with TestClient(TestServer(service.build_app())) as client:
        answers = [
            await client.post(path, headers=caller_headers(), json=body)
            for path, body in (
                ("/v1/mcp/hosted/h1/start", {"definition_digest": "0" * 64}),
                ("/v1/mcp/connections/mcp_conn_x/tools", discovery),
                ("/v1/mcp/connections/mcp_conn_x/invoke", invocation),
            )
        ]
        bodies = [await answer.json() for answer in answers]
        runtime.raised = PermissionError("The MCP connection record changed before discovery.")
        refused = await client.post("/v1/mcp/connections/mcp_conn_x/tools", headers=caller_headers(), json=discovery)

    assert [answer.status for answer in answers] == [503, 503, 503]
    assert bodies == [{"error": str(error), "reason_code": HOSTED_MCP_START_FAILED}] * 3
    assert refused.status == 403, "a refusal is still a refusal"


@pytest.mark.asyncio
async def test_the_mcp_chat_command_shows_why_a_start_failed() -> None:
    from assistant_gateway.application.manager_control_command_service import GatewayManagerControlCommandService
    from assistant_gateway.application.commands.manager_control import ManagerControlCommandRequest
    from assistant_gateway.application.commands.context import GatewayActorContext

    hosted = SimpleNamespace(
        hosted_server_id="h1", server_name="echo", lifecycle="stopped", available_actions=("start",),
        definition_digest="d" * 64,
    )

    class Controls:
        def list_web_chat_mcp_connections(self, _request: object) -> SimpleNamespace:
            return SimpleNamespace(connections=[])

        def list_web_chat_hosted_mcp_servers(self, _request: object) -> SimpleNamespace:
            return SimpleNamespace(servers=[hosted])

        async def control_web_chat_mcp_lifecycle(self, _request: object) -> SimpleNamespace:
            raise HostedMCPStartError(PORT_TAKEN)

    service = GatewayManagerControlCommandService(SimpleNamespace(), mcp_controls=Controls())  # type: ignore[arg-type]
    result = await service.execute(
        ManagerControlCommandRequest(
            command="/mcp hosted start h1",
            current_session_key="web:session",
            actor=GatewayActorContext(channel_name="website", user_id="u1", chat_id="c1", canonical_user_id="usr_owner"),
        )
    )

    assert result.text == f"MCP hosted_server h1 was not changed: {PORT_TAKEN}"


# -- a server set to run whose sidecar cannot start says so wherever it is listed ----------


@contextlib.contextmanager
def _port_held_by_a_leftover() -> Iterator[int]:
    """A port another process listens on, as a sidecar left over from an earlier broker run does."""

    with socket.socket() as leftover:
        leftover.bind(("127.0.0.1", 0))
        leftover.listen()
        yield leftover.getsockname()[1]


async def _broker_start(config: Any) -> None:
    """What a broker start does for hosted servers: start every server set to run, then (here) stop."""

    broker = MCPHostedProcessSupervisor(config)
    try:
        await broker.restore()
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_a_server_set_to_run_whose_sidecar_cannot_start_stays_set_to_run_and_is_listed_with_why(
    tmp_path: Path,
) -> None:
    config, _path = _hosting_config(tmp_path)
    with _port_held_by_a_leftover() as port:
        definition = _saved(config, _echo_definition(port, lifecycle=HostedMCPLifecycle.RUNNING))
        await _broker_start(config)

    [listed] = MCPHostingAdministration(config).list_public()
    assert listed["lifecycle"] == "running", "the state people chose stays, so the next broker start tries again"
    assert f"127.0.0.1:{port} is already in use" in listed["start_failure"]["reason"]
    assert listed["start_failure"]["failed_at"]
    assert MCPHostingAdministration(config).store.require(definition.hosted_server_id).lifecycle == (
        HostedMCPLifecycle.RUNNING
    )


@pytest.mark.asyncio
async def test_a_later_start_that_succeeds_clears_the_reason(tmp_path: Path) -> None:
    config, _path = _hosting_config(tmp_path)
    with _port_held_by_a_leftover() as port:
        definition = _saved(config, _echo_definition(port, lifecycle=HostedMCPLifecycle.RUNNING))
        await _broker_start(config)
    assert MCPHostingAdministration(config).list_public()[0]["start_failure"] is not None

    # The leftover is gone; the next broker start brings the server up.
    broker = MCPHostedProcessSupervisor(config)
    try:
        await broker.restore()
        assert broker._processes[definition.hosted_server_id].returncode is None
        assert MCPHostingAdministration(config).list_public()[0]["start_failure"] is None
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_stopping_the_server_clears_the_reason(tmp_path: Path) -> None:
    config, _path = _hosting_config(tmp_path)
    with _port_held_by_a_leftover() as port:
        definition = _saved(config, _echo_definition(port, lifecycle=HostedMCPLifecycle.RUNNING))
        await _broker_start(config)

    broker = MCPHostedProcessSupervisor(config)
    try:
        assert (await broker.stop(definition.hosted_server_id))["already_stopped"] is True
    finally:
        await broker.close()

    assert MCPHostingAdministration(config).list_public()[0]["start_failure"] is None


@pytest.mark.asyncio
async def test_every_list_of_hosted_servers_says_why_a_server_set_to_run_is_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assistant_gateway.application.manager_control_command_service import _render_mcp_inventory
    from tests.test_status_page import _page

    config, path = _hosting_config(tmp_path)
    with _port_held_by_a_leftover() as port:
        definition = _saved(config, _echo_definition(port, lifecycle=HostedMCPLifecycle.RUNNING))
        await _broker_start(config)
    said = "Set to run, but it could not start:"

    # Web Chat's MCP settings and the /mcp command use the same projection.
    [shown] = [WebHostedMCPServerV1.model_validate(item) for item in MCPHostingAdministration(config).list_public()]
    assert shown.start_failure is not None and "already in use" in shown.start_failure.reason
    inventory = _render_mcp_inventory([], [shown])
    assert said in inventory and f"127.0.0.1:{port} is already in use" in inventory

    # The agents' list.
    agent = change_agent(config, path, authorization_enabled=False)
    listing = await asyncio.to_thread(_call_tool, agent, hosted_mcp_servers, action="list")
    [listed] = listing["hosted_servers"]
    assert listed["status"].startswith(said) and "already in use" in listed["status"]

    # The Status page, until the server starts.
    report = await _page(config).report()
    [finding] = [item for item in report.findings if item.finding_id.startswith("hosted_mcp_server_not_started:")]
    assert finding.finding_id == f"hosted_mcp_server_not_started:{definition.hosted_server_id}"
    assert "echo" in finding.title and "already in use" in str(finding.details)
    assert "stop the process that holds it" in finding.action

    broker = MCPHostedProcessSupervisor(config)
    try:
        await broker.restore()
        report = await _page(config).report()
    finally:
        await broker.close()
    assert not [item for item in report.findings if item.finding_id.startswith("hosted_mcp_server_not_started:")]
