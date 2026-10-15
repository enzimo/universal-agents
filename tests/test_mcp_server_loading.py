"""Installed MCP servers load before model calls in both modes, retry with a limit, and report when they give up.

Real SQLite access store, MCP registry, tool catalog, Strands tool registry,
brokered MCP client and loader (``custom_agents.mcp_server_tools``) behind
the model-call hook (``custom_agents.tool_sync``). The broker's HTTP answer is
scripted (``Broker``) so each test chooses whether the server answers, does
not answer, or is refused; the end-to-end tests at the bottom use the real
broker service and MCP runtime from ``tests/test_mcp_follows_grants.py``.

The story:

1. A server that does not answer is tried again after 1, 2, 4, 8 and 16
   minutes, then every 30 minutes, until the retry window (60 minutes by
   default) ends; then the agent stops and administrators get one Inbox
   notice per server and day, however many agents give up.
2. A refusal from the broker (no account linked) is not retried on a timer;
   an access change (an account link) tries again at once, as does a change
   to the server's record after giving up.
3. With authorization off, a server that is down when the agent starts loads
   when it answers again within the window, and ``mcp_connections(action="list")``
   tells the agent why it is missing meanwhile.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from aiohttp.test_utils import TestServer
from strands.tools.registry import ToolRegistry

from access_control.caller_tokens import CALLER_MANAGER
from custom_agents import access_runtime
from custom_agents.mcp_server_tools import (
    LATER_RETRY_GAP,
    RETRY_GAPS,
    LoadFailure,
    load_retry_window,
    retry_gap,
    server_status,
    unavailable_notice_id,
)
from custom_agents.sdk.strands import mcp as strands_mcp
from custom_agents.tool_sync import MCPServerSync, install_tool_sync
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    AccountLinkV1,
    ChangeApprovalMode,
    ChangeApprovalRecordV1,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
)
from custom_tools.providers.capabilities import mcp_connections
from custom_tools.runtime_context import set_agent_ref
from mcp_control.registry import (
    MCPConnectionLifecycle,
    MCPConnectionRecordV1,
    MCPConnectionSource,
    MCPConnectionStore,
    _new_record,
    descriptor_from_server_config,
    resolve_mcp_connection_paths,
)
from secure_tool_broker.mcp_runtime import BrokerMCPRuntime
from storage.access_repository import AccessRepository
from tests.broker_access_support import caller_token
from tests.test_access_request_tools import BINARY, OWNER, TEAM, make_agent
from tests.test_access_request_tools import storage as access_storage
from tests.test_mcp_follows_grants import TOOL_ID, FakeServer, World

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

storage = access_storage  # the shared fixture: a manager database and verify keys
START = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
ADMINISTRATOR = ("admin",)
NOT_ANSWERING = "not_answering"
REFUSED = "refused"
TOOLS = "tools"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def at(self, minutes: float) -> None:
        self.now = START + timedelta(minutes=minutes)


class Broker:
    """The secure broker's answer to a tool listing, and when it was asked (minutes after START)."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.answer = NOT_ANSWERING
        self.asked: list[float] = []

    async def post(self, client: Any, suffix: str, _payload: dict[str, Any]) -> dict[str, Any]:
        assert suffix == "tools"
        self.asked.append((self.clock.now - START).total_seconds() / 60)
        if self.answer == NOT_ANSWERING:
            raise httpx.ConnectError("All connection attempts failed")
        if self.answer == REFUSED:
            raise strands_mcp.BrokerRefusal(
                403,
                {
                    "error": "No account is linked for the MCP connection tickets. Ask TeamLead to request an account link for it.",
                    "reason_code": "account_link_missing",
                },
            )
        return {
            "schema_version": 1,
            "record_type": "mcp_discovery_response",
            "connection_id": client.ua_connection_id,
            "record_digest": client.ua_record_digest,
            "descriptor_sha256": "b" * 64,
            "operation_catalog_sha256": "c" * 64,
            "tools": [{"name": "search", "inputSchema": {"type": "object"}, "operation_digest": "d" * 64}],
        }


def install(store: MCPConnectionStore, *, endpoint: str = "https://tickets.example.test/mcp") -> MCPConnectionRecordV1:
    """An active server installed with the person's agreement, for both test agent types."""

    descriptor = descriptor_from_server_config(
        {"name": "tickets", "transport": "streamable_http", "server_url": endpoint},
        source=MCPConnectionSource.AGENT_PROPOSAL,
        source_locator=f"agent-proposal:test:{endpoint}",
        team_id=TEAM,
        owner_canonical_user_id=OWNER.person_id,
    )
    approval = ChangeApprovalRecordV1(mode=ChangeApprovalMode.AGREEMENT, approved_by="agreement", approved_at_utc=START)
    return store.save(
        _new_record(
            descriptor,
            lifecycle=MCPConnectionLifecycle.ACTIVE,
            target_agent_types=("WebResearchAgent", "CodeWritingAgent"),
            approval=approval,
        )
    )


class Agent:
    """One agent with authorization off: its model-call hook, tool registry and clock."""

    def __init__(self, storage: Any, tmp_path: Path, clock: Clock, agent_type: str = "WebResearchAgent") -> None:
        self.agent = make_agent(storage, tmp_path, agent_type=agent_type, enabled=False)
        # With an instance root the tool catalog reloads when a record changes, as in production.
        self.agent.instance_root = str(tmp_path)
        self.registry = ToolRegistry()
        self.hook = install_tool_sync(self.agent, [])
        assert isinstance(self.hook, MCPServerSync)
        self.hook.servers.clock = clock

    async def next_model_call(self) -> set[str]:
        await self.hook.before_model_call(SimpleNamespace(agent=SimpleNamespace(tool_registry=self.registry)))
        return set(self.registry.dynamic_tools)

    def status(self) -> str | None:
        return server_status(self.agent, TOOL_ID)

    def failure(self) -> Any:
        return self.hook.servers._failures.get(TOOL_ID)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def broker(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> Broker:
    scripted = Broker(clock)
    monkeypatch.setattr(strands_mcp.BrokeredMCPClient, "_post", lambda client, suffix, payload: scripted.post(client, suffix, payload))
    return scripted


@pytest.fixture
def store(storage, tmp_path: Path) -> MCPConnectionStore:
    # The agents' instance root is tmp_path (World), so this is their registry.
    yield MCPConnectionStore(resolve_mcp_connection_paths(tmp_path))
    access_runtime._runtime_access.cache_clear()
    set_agent_ref(None)


def administrator_notices(storage: Any) -> list[Any]:
    return [notice for notice, _read in AccessRepository(storage).notices_for(TEAM, person_id="admin-1", roles=ADMINISTRATOR)]


def link_an_account(storage: Any) -> None:
    """Any account link changes the access store's version, as linking one in the broker's form does."""

    AccessRepository(storage).create_link(
        AccountLinkV1(
            link_id="account_link_tickets",
            team_id=TEAM,
            tool_id=TOOL_ID,
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM,
            credential_key="tickets_team",
            created_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            created_at_utc=START,
        )
    )


def test_the_retry_schedule_grows_then_repeats_every_half_hour() -> None:
    assert [retry_gap(tries) for tries in range(1, 8)] == [*RETRY_GAPS, LATER_RETRY_GAP, LATER_RETRY_GAP]
    assert [gap.total_seconds() / 60 for gap in RETRY_GAPS] == [1, 2, 4, 8, 16]
    assert load_retry_window(SimpleNamespace()) == timedelta(minutes=60), "the default window"
    assert load_retry_window(SimpleNamespace(mcp_config={"load_retry_window_minutes": 15})) == timedelta(minutes=15)


@pytest.mark.asyncio
async def test_a_server_that_does_not_answer_is_retried_then_given_up_and_reported(storage, tmp_path, store, clock, broker) -> None:
    install(store)
    agent = Agent(storage, tmp_path, clock)

    assert await agent.next_model_call() == set()
    assert agent.failure().kind is LoadFailure.NOT_ANSWERING
    assert agent.status() == (
        "Not loaded: the server has not answered since 2026-09-25 10:00 UTC (All connection attempts failed); "
        "trying again at 2026-09-25 10:01 UTC."
    )
    clock.at(0.5)
    await agent.next_model_call()
    assert broker.asked == [0], "not before the next try is due"

    for minute in (1, 3, 7, 15, 31, 45, 59):
        clock.at(minute)
        await agent.next_model_call()
    assert broker.asked == [0, 1, 3, 7, 15, 31], "1, 2, 4, 8 and 16 minutes apart"
    assert agent.failure().next_try_at == START + timedelta(minutes=60), "the last try is at the end of the window"
    assert administrator_notices(storage) == []

    clock.at(60)
    await agent.next_model_call()
    assert broker.asked[-1] == 60 and agent.failure().kind is LoadFailure.GAVE_UP
    assert "Agents stopped trying at 2026-09-25 11:00 UTC and administrators were told" in agent.status()
    [notice] = administrator_notices(storage)
    assert notice.kind is AccessNoticeKind.MCP_SERVER_UNAVAILABLE and notice.recipient_roles == ADMINISTRATOR
    assert notice.title == "The MCP server tickets is not answering"
    assert "since 2026-09-25 10:00 UTC" in notice.summary and "All connection attempts failed" in notice.summary
    assert "cannot use its tools" in notice.summary and "Check that the server is running" in notice.summary
    assert "when it changes or when they restart" in notice.summary

    clock.at(600)
    broker.answer = TOOLS
    assert await agent.next_model_call() == set(), "after giving up nothing is retried on a timer"
    assert broker.asked[-1] == 60


@pytest.mark.asyncio
async def test_every_agent_that_gives_up_on_a_server_stores_one_notice_a_day(storage, tmp_path, store, clock, broker) -> None:
    record = install(store)
    agents = [Agent(storage, tmp_path, clock, agent_type) for agent_type in ("WebResearchAgent", "CodeWritingAgent")]

    for minute in (0, 1, 3, 7, 15, 31, 60):
        clock.at(minute)
        for agent in agents:
            await agent.next_model_call()

    assert all(agent.failure().kind is LoadFailure.GAVE_UP for agent in agents)
    [notice] = administrator_notices(storage)
    assert notice.notice_id == unavailable_notice_id(record.connection_id, START)
    assert unavailable_notice_id(record.connection_id, START + timedelta(days=1)) != notice.notice_id, "a new one each day"
    assert len(notice.notice_id) <= 120


@pytest.mark.asyncio
async def test_an_access_change_tries_a_server_that_is_still_retried_at_once_and_keeps_its_window(
    storage, tmp_path, store, clock, broker
) -> None:
    install(store)
    agent = Agent(storage, tmp_path, clock)
    await agent.next_model_call()

    clock.at(0.5)
    link_an_account(storage)
    await agent.next_model_call()

    assert broker.asked == [0, 0.5]
    assert agent.failure().first_failed_at == START, "the window still counts from the first failure"
    assert agent.failure().tries == 2


@pytest.mark.asyncio
async def test_a_refusal_waits_for_an_access_change_not_a_timer(storage, tmp_path, store, clock, broker) -> None:
    install(store)
    broker.answer = REFUSED
    agent = Agent(storage, tmp_path, clock)

    assert await agent.next_model_call() == set()
    assert agent.failure().kind is LoadFailure.REFUSED
    assert agent.status() == (
        "Not loaded: the secure broker refused to list its tools (No account is linked for the MCP connection "
        "tickets. Ask TeamLead to request an account link for it.). It is tried again when access or the server changes."
    )
    clock.at(24 * 60)
    await agent.next_model_call()
    assert broker.asked == [0], "no timer retries a refusal"
    assert administrator_notices(storage) == [], "a refusal is not an outage"

    # The person links an account: the next model call tries again, and the server answers.
    broker.answer = TOOLS
    link_an_account(storage)
    assert await agent.next_model_call() == {"tickets_search"}
    assert agent.status() is None


@pytest.mark.asyncio
async def test_after_giving_up_a_reinstalled_server_is_tried_again_at_once(storage, tmp_path, store, clock, broker) -> None:
    old = install(store)
    agent = Agent(storage, tmp_path, clock)
    for minute in (0, 1, 3, 7, 15, 31, 60):
        clock.at(minute)
        await agent.next_model_call()
    assert agent.failure().kind is LoadFailure.GAVE_UP

    # An administrator reinstalls it at a new address: a new record for the same tool.
    clock.at(90)
    store.update_lifecycle(old.connection_id, lifecycle=MCPConnectionLifecycle.DISABLED)
    install(store, endpoint="https://tickets-2.example.test/mcp")
    broker.answer = TOOLS

    assert await agent.next_model_call() == {"tickets_search"}
    assert broker.asked[-1] == 90 and agent.status() is None


# -- end to end with the real broker, authorization off ------------------------------


@pytest.mark.asyncio
async def test_with_authorization_off_a_server_down_at_start_loads_when_it_answers_again(
    storage, tmp_path, monkeypatch
) -> None:
    down = {"now": True}
    original_list_tools = FakeServer.list_tools

    async def list_tools(self: FakeServer) -> list[Any]:
        if down["now"]:
            raise ConnectionError("connection refused")
        return await original_list_tools(self)

    FakeServer.calls = []
    monkeypatch.setattr(FakeServer, "list_tools", list_tools)
    monkeypatch.setattr("secure_tool_broker.mcp_runtime.BrokerMCPClient", FakeServer)
    monkeypatch.setattr(BrokerMCPRuntime, "_validate_transport_policy", lambda _self, _record, _raw: ())
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", caller_token(CALLER_MANAGER))
    world = World(storage, tmp_path, enabled=False)
    install(world.mcp_store())
    agent = world.worker  # a WebResearchAgent, a type the installed record names
    clock = Clock()
    registry = ToolRegistry()
    hook = install_tool_sync(agent, [])
    hook.servers.clock = clock
    set_agent_ref(agent)
    event = SimpleNamespace(agent=SimpleNamespace(tool_registry=registry))
    try:
        async with TestServer(world.broker().build_app()) as server:
            monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", str(server.make_url("")).rstrip("/"))

            await hook.before_model_call(event)
            assert set(registry.dynamic_tools) == set(), "the server is down when the agent starts"
            [listed] = json.loads(await mcp_connections(action="list", tool_context=None))["connections"]
            assert listed["tool_id"] == TOOL_ID
            assert listed["load_status"].startswith("Not loaded: the server has not answered since 2026-09-25 10:00 UTC")
            assert "HTTP 502" in listed["load_status"], "the broker reports the server's failure as a bad gateway"

            down["now"] = False
            clock.at(1)
            await hook.before_model_call(event)
            assert set(registry.dynamic_tools) == {"tickets_search", "tickets_purge_index"}
            assert "load_status" not in json.loads(await mcp_connections(action="list", tool_context=None))["connections"][0]
            result = await registry.dynamic_tools["tickets_search"].mcp_client.call_tool_async("tu", "search", {})
            assert result["status"] == "success" and FakeServer.calls == [("search", None)]
    finally:
        world.service.cedar.close()
        access_runtime._runtime_access.cache_clear()
        set_agent_ref(None)
