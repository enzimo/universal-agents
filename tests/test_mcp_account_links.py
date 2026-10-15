"""An MCP connection's declared credential slot is filled from its account link.

A Crawl4AI-style server over SSE requires ``Authorization: Bearer <token>``.
The connection declares a slot; the team's account link names the stored
credential; discovery and invocation then authenticate, a new broker process
recovers the same link, and revoking the link stops the next connection.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from access_catalog.catalog import CatalogSource, load_catalog
from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller
from access_control.identity import ContextVerifier
from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.contracts.access_v2 import (
    AccountLinkV1,
    AccountMode,
    InjectionPlacement,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
)
from custom_messages.contracts.common_v1 import PrincipalDescriptorV1, PrincipalType
from custom_messages.contracts.mcp_v1 import MCPDiscoveryRequestV1, MCPInvocationRequestV1
from mcp_control.registry import reconcile_instance_mcp_connections
from secure_tool_broker.access_gate import BrokerAccessGate
from secure_tool_broker.credential_catalog import (
    CatalogCredential,
    CatalogCredentialField,
    CatalogSourceRef,
    CredentialCatalog,
    save_catalog,
)
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.mcp_config import BrokerMCPConfig
from secure_tool_broker.mcp_runtime import BrokerMCPRuntime
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_verify_keys, signed_task_context
from tests.test_strands_mcp import _MockMcpSseService

TOKEN = "synthetic-crawl4ai-token-$not-an-environment-reference"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def mock_crawl4ai() -> tuple[web.Application, dict, list]:
    queues: dict[str, asyncio.Queue] = {}
    called: list[str] = []

    async def sse(request):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.Response(status=401)
        session_id = str(len(queues) + 1)
        queue = queues[session_id] = asyncio.Queue()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(f"event: endpoint\ndata: /messages?session_id={session_id}\n\n".encode())
        try:
            while request.transport is not None and not request.transport.is_closing():
                try:
                    message = await asyncio.wait_for(queue.get(), 0.1)
                except TimeoutError:
                    continue
                await response.write(f"event: message\ndata: {json.dumps(message)}\n\n".encode())
        except ConnectionError:
            pass
        return response

    async def post(request):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.Response(status=401)
        message = await request.json()
        if message.get("method") == "tools/call":
            called.append(message["params"]["name"])
        result = _MockMcpSseService._response_for(message)
        if result is not None:
            await queues[request.query["session_id"]].put(result)
        return web.Response(status=202)

    app = web.Application()
    app.router.add_get("/mcp/sse", sse)
    app.router.add_post("/messages", post)
    return app, queues, called


def prepare(tmp_path: Path, monkeypatch, endpoint: str):
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="mcp-account-link-test", instance_root=str(root), team_id="team-1", timezone="UTC"
    )
    path = config.save(root / "instance_config.yaml")
    config.authorization.enabled = False
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.mcp.legacy_sse.enabled = True
    config.mcp.network_policy.allow_loopback = True
    config.agent.external_mcp_servers = [
        {
            "name": "crawl4ai",
            "transport": "sse",
            "server_url": endpoint,
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
    config.save(path)
    record = reconcile_instance_mcp_connections(config)[0]
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "credentials.db"))
    monkeypatch.setenv("UA_STORED_TEST_TOKEN", TOKEN)
    save_catalog(
        CredentialCatalog(
            credentials={
                "crawl4ai_api_token": CatalogCredential(
                    display_name="Crawl4AI",
                    purpose="Crawling",
                    fields={
                        "api_token": CatalogCredentialField(
                            kind="token",
                            visibility="secret",
                            resolution_order=[CatalogSourceRef(source="env", env_var="UA_STORED_TEST_TOKEN")],
                        )
                    },
                )
            }
        ),
        scope=CredentialOwnerScope.team(config.manager.team_id),
    )
    store = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )
    broker_config = BrokerMCPConfig.from_runtime_settings(
        instance_root_path=config.instance_root,
        agent=config.agent,
        agent_types=config.agent_types,
        mcp_profiles=config.mcp_profiles,
        mcp=config.mcp,
    )
    return config, record, store, broker_config


def gate_for(config, store: AccessRepository) -> BrokerAccessGate:
    return BrokerAccessGate(
        catalogs=CatalogSource(config.instance_root),
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=config.manager.team_id,
        access=None,
        links=store,
    )


def runtime_for(broker_config, gate) -> BrokerMCPRuntime:
    return BrokerMCPRuntime(config=broker_config, access_gate=gate)


def team_link(config, store: AccessRepository) -> AccountLinkV1:
    tool = load_catalog(config.instance_root).tools["mcp:crawl4ai"]
    return store.create_link(
        AccountLinkV1(
            link_id="account_link_crawl4ai",
            team_id=config.manager.team_id,
            tool_id=tool.tool_id,
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=config.manager.team_id,
            credential_key="crawl4ai_api_token",
            injections=tool.credential_injections,
            created_by=SubjectRefV1(kind=SubjectKind.USER, id=config.user.canonical_user_id),
            created_at_utc=NOW,
        )
    )


def test_declared_slots_make_the_connection_need_a_linked_account(tmp_path, monkeypatch) -> None:
    config, _record, _store, _broker = prepare(tmp_path, monkeypatch, "http://127.0.0.1:11235/mcp/sse")

    tool = load_catalog(config.instance_root).tools["mcp:crawl4ai"]

    assert tool.account is AccountMode.LINKED_CREDENTIAL
    [slot] = tool.credential_injections
    assert (slot.placement, slot.name, slot.field, slot.template) == (
        InjectionPlacement.HEADER,
        "Authorization",
        "api_token",
        "Bearer {value}",
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_linked_account_authenticates_discovery_and_invocation_until_revoked(
    tmp_path, monkeypatch, caplog
) -> None:
    app, queues, called = mock_crawl4ai()
    async with TestServer(app) as server:
        config, record, store, broker_config = prepare(tmp_path, monkeypatch, str(server.make_url("/mcp/sse")))
        gate = gate_for(config, store)
        runtime = runtime_for(broker_config, gate)
        try:
            caller = BrokerCaller(CALLER_MANAGER)
            with pytest.raises(PermissionError, match="No account is linked"):
                await runtime.discover_tools(
                    record.connection_id,
                    MCPDiscoveryRequestV1(record_digest=record.record_digest, named_agent_type="TeamArchitect"),
                    caller=caller,
                )
            assert not queues
            link = team_link(config, store)
            discovery = await runtime.discover_tools(
                record.connection_id,
                MCPDiscoveryRequestV1(record_digest=record.record_digest, named_agent_type="TeamArchitect"),
                caller=caller,
            )
            assert [tool.name for tool in discovery.tools] == ["screenshot"]
            invocation = MCPInvocationRequestV1(
                record_digest=discovery.record_digest,
                operation_name="screenshot",
                tool_use_id="call-test",
                task_id="task-test",
                principal=PrincipalDescriptorV1(
                    principal_type=PrincipalType.AGENT,
                    principal_id="test-lead",
                    named_agent_type="TeamArchitect",
                    team_id=config.manager.team_id,
                    on_behalf_of_user_id=config.user.canonical_user_id,
                    roles=("team_lead",),
                ),
                task_context=signed_task_context(config.user.canonical_user_id, team_id=config.manager.team_id),
            )
            assert not (await runtime.invoke(record.connection_id, invocation, caller=caller)).is_error
            assert called == ["screenshot"]
        finally:
            await runtime.close()

        restarted = runtime_for(broker_config, gate_for(config, store))
        try:
            assert not (await restarted.invoke(record.connection_id, invocation, caller=caller)).is_error
            store.revoke_link(
                link.link_id, actor=SubjectRefV1(kind=SubjectKind.USER, id=config.user.canonical_user_id), at=NOW
            )
            with pytest.raises(PermissionError):
                await restarted.invoke(record.connection_id, invocation, caller=caller)
            assert called == ["screenshot", "screenshot"]
            assert not restarted._clients
        finally:
            await restarted.close()
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_a_missing_account_link_is_not_hidden_from_the_loader(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from tests.test_strands_mcp import _brokered_client

    provider = _brokered_client()
    monkeypatch.setattr(provider, "_post", AsyncMock(side_effect=PermissionError("No account is linked")))
    with pytest.raises(PermissionError, match="No account is linked"):
        await provider.load_tools()
