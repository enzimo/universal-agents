"""MCP servers follow grants and are easy to approve (access plan 7d), end to end.

Real Cedar, SQLite, TaskManager and signed task contexts; the real
``mcp_connections(action="propose")`` and ``request_access`` tools; the gateway's real
proposal applier; the real broker HTTP service, MCP runtime and access gate
(reached over HTTP by the agents' brokered MCP clients); and a real Strands
tool registry kept by ``GrantToolSync``. Only the MCP server itself is a
stand-in (``FakeServer`` replaces the broker's MCP client and records the
credential each session was made with).

The story the tests tell:

1. TeamArchitect proposes a server in one call; its task pauses on one review
   card that installs the server, lets TeamArchitect use all of it (and hand
   it on), and asks for the credential. Nothing is installed before approval.
2. An administrator approves once. The server is active, the grant serves the
   requesting person's work, and the credential form is queued.
3. The server's tools appear on TeamArchitect's next model call (after a
   failed first load because no account was linked yet); every function works,
   including one whose level nobody confirmed; the person's own account link
   is used.
4. Working for a second person, the same agent is refused until that person
   authorizes it; the refusal reaches the model with the tool, level and next
   step; there is no rule that only the proposer may call; the team's account
   link is used for them.
5. A worker asks with ``request_access`` and TeamArchitect hands the server on
   without asking anyone; revoking TeamArchitect's grant removes the tools and
   refuses the worker.
6. A failure while storing leaves the server pending and no grants; with
   authorization off, the person's agreement installs it and its tools load
   on the running agent's next model call.

While a server's tools are missing, ``find_tools`` says why (here: the broker
refused because no account was linked). Retries, giving up and reporting are
in ``tests/test_mcp_server_loading.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestServer
from mcp import types
from strands.interrupt import InterruptException
from strands.tools.registry import ToolRegistry

from access_catalog.catalog import CatalogSource, load_catalog
from access_control.caller_tokens import CALLER_MANAGER
from access_control.cedar import CedarAccessClient
from access_control.data_folders import DataFolderRegistry
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import PersonPrincipal, agent_role_resolver
from access_control.identity import ContextVerifier
from access_control.proposals import ACCESS_PROPOSAL_KIND, ProposalApplier
from access_control.requests import AccessRequests
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_request_runtime, access_runtime
from custom_agents.tool_sync import GrantToolSync, install_tool_sync, startup_tools
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestStatus,
    AccessReviewChoice,
    AccountLinkV1,
    ChangeApprovalMode,
    GrantLifetime,
    GrantV1,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.providers import access as access_tools
from custom_tools.providers import tool_loading
from custom_tools.providers.capabilities import mcp_connections
from custom_tools.registry import ALL_PROVIDER_TOOLS
from custom_tools.runtime_context import set_agent_ref
from mcp_control.registry import MCPConnectionLifecycle, MCPConnectionStore, resolve_mcp_connection_paths
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
from secure_tool_broker.service import SecureToolBrokerService
from storage.access_repository import AccessRepository
from tests.broker_access_support import BROKER_ROOT, caller_token, context_signer, context_verify_keys
from tests.test_access_request_tools import BINARY, OWNER, TEAM, Context, make_agent
from tests.test_access_request_tools import storage as access_storage

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

storage = access_storage  # the shared fixture: a manager database and verify keys
MEMBER = PersonPrincipal("member-2", TEAM, ("member",))
TOOL_ID = "mcp:tickets"
SLOT = {"target_kind": "header", "target_name": "Authorization", "field_name": "api_token", "template": "Bearer {value}"}
OWNER_TOKEN = "owner-token-synthetic"
TEAM_TOKEN = "team-token-synthetic"


class FakeServer:
    """The MCP server behind the broker: two tools, and a record of each call and its credential."""

    calls: list[tuple[str, str | None]] = []

    def __init__(self, config: dict[str, Any]) -> None:
        self.authorization = dict(config.get("headers") or {}).get("Authorization")

    async def list_tools(self) -> list[Any]:
        return [
            SimpleNamespace(name=name, description=f"{name} tickets", inputSchema={"type": "object"}, outputSchema=None)
            for name in ("search", "purge_index")
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        FakeServer.calls.append((name, self.authorization))
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"{name} done")], isError=False)

    async def close(self) -> None:
        return None


class World:
    """One instance: the lead and a worker, the gateway's applier, and the broker."""

    def __init__(self, storage: Any, tmp_path: Path, *, enabled: bool = True) -> None:
        root = tmp_path / "instance"
        self.config = GatewayInstanceConfig.create(
            instance_name="mcp-grants", instance_root=str(root), team_id=TEAM, timezone="UTC"
        )
        self.config.authorization.enabled = enabled
        self.config_path = self.config.save(root / "instance_config.yaml")
        self.root = self.config.instance_root_path
        self.lead = self._agent(storage, tmp_path, "TeamArchitect", lead=True, enabled=enabled)
        self.worker = self._agent(storage, tmp_path, "WebResearchAgent", lead=False, enabled=enabled)
        self.catalogs = CatalogSource(self.root)
        self.service = AccessService(
            store=AccessRepository(storage),
            catalog=self.catalogs.current(),
            cedar=CedarAccessClient(binary=str(BINARY)),
            team_id=TEAM,
            agent_role_of=agent_role_resolver("TeamArchitect"),
        )
        self.applier = ProposalApplier(
            requests=self.requests,
            instance_root=self.root,
            catalogs=self.catalogs,
            data_folders=lambda: DataFolderRegistry(self.service.store, TEAM),
            protected=(self.root,),
            person={OWNER.person_id: OWNER, MEMBER.person_id: MEMBER}.get,
            mcp_store=self.mcp_store,
        )

    def _agent(self, storage: Any, tmp_path: Path, agent_type: str, *, lead: bool, enabled: bool) -> Any:
        agent = make_agent(storage, tmp_path, agent_type=agent_type, lead=lead, enabled=enabled)
        agent.instance_root = str(self.root)
        agent.instance_config_path = str(self.config_path)
        agent.tool_filter = {}
        return agent

    def mcp_store(self) -> MCPConnectionStore:
        return MCPConnectionStore(resolve_mcp_connection_paths(self.root))

    def requests(self) -> AccessRequests:
        self.service.catalog = self.catalogs.current()
        return AccessRequests(
            service=self.service,
            inbox=access_runtime.StoredAccessInbox(self.service.store),
            lead_agent_type="TeamArchitect",
            appliers={ACCESS_PROPOSAL_KIND: self.applier},
        )

    def broker(self) -> SecureToolBrokerService:
        gate = BrokerAccessGate(
            catalogs=CatalogSource(self.root),
            verifier=ContextVerifier.from_value(context_verify_keys()),
            team_id=TEAM,
            access=self.service if self.config.authorization.enabled else None,
            links=self.service.store,
        )
        mcp_config = BrokerMCPConfig.from_runtime_settings(
            instance_root_path=self.config.instance_root,
            agent=self.config.agent,
            agent_types=self.config.agent_types,
            mcp_profiles=self.config.mcp_profiles,
            mcp=self.config.mcp,
        )
        return SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate, mcp_config=mcp_config)

    def link(self, owner_kind: LinkOwnerKind, owner_id: str, credential_key: str) -> AccountLinkV1:
        tool = load_catalog(self.root).tools[TOOL_ID]
        return self.service.store.create_link(
            AccountLinkV1(
                link_id=f"account_link_{credential_key}",
                team_id=TEAM,
                tool_id=TOOL_ID,
                owner_kind=owner_kind,
                owner_id=owner_id,
                credential_key=credential_key,
                injections=tool.credential_injections,
                created_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
                created_at_utc=self.service.clock(),
            )
        )

    def grants(self) -> list[GrantV1]:
        return [grant for grant in self.service.store.list_team(TEAM) if grant.target.id == TOOL_ID]


@pytest.fixture
def world(storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    FakeServer.calls = []
    monkeypatch.setattr("secure_tool_broker.mcp_runtime.BrokerMCPClient", FakeServer)
    # The stand-in server has no address to check.
    monkeypatch.setattr(BrokerMCPRuntime, "_validate_transport_policy", lambda _self, _record, _raw: ())
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", caller_token(CALLER_MANAGER))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "credentials.db"))
    monkeypatch.setenv("UA_TEST_OWNER_TOKEN", OWNER_TOKEN)
    monkeypatch.setenv("UA_TEST_TEAM_TOKEN", TEAM_TOKEN)
    for scope, key, env in (
        (CredentialOwnerScope.user(OWNER.person_id), "tickets_owner", "UA_TEST_OWNER_TOKEN"),
        (CredentialOwnerScope.team(TEAM), "tickets_team", "UA_TEST_TEAM_TOKEN"),
    ):
        field = CatalogCredentialField(
            kind="token", visibility="secret", resolution_order=[CatalogSourceRef(source="env", env_var=env)]
        )
        save_catalog(
            CredentialCatalog(credentials={key: CatalogCredential(display_name="Tickets", purpose="Tickets", fields={"api_token": field})}),
            scope=scope,
        )
    created = World(storage, tmp_path)
    yield created
    created.service.cedar.close()
    access_runtime._runtime_access.cache_clear()
    set_agent_ref(None)


async def propose(context: Context, **extra: Any) -> dict[str, Any]:
    return json.loads(
        await mcp_connections(
            action="propose",
            connection_name="tickets",
            endpoint="https://tickets.example.test/mcp",
            reason="The person asked me to triage their support tickets.",
            credential_injections=[SLOT],
            tool_context=context,
            **extra,
        )
    )


def model_view(agent: Any) -> tuple[ToolRegistry, Any]:
    """What the agent's model sees, and the hook that keeps it equal to its grants."""

    tools = startup_tools(agent, list(ALL_PROVIDER_TOOLS))
    registry = ToolRegistry()
    registry.process_tools(tools)
    hook = install_tool_sync(agent, tools)
    if agent.authorization_config["enabled"]:
        assert isinstance(hook, GrantToolSync)
    return registry, hook


async def next_model_call(hook: Any, registry: ToolRegistry) -> set[str]:
    await hook.before_model_call(SimpleNamespace(agent=SimpleNamespace(tool_registry=registry)))
    return {name for name in registry.dynamic_tools if name.startswith("tickets_")}


async def call(agent: Any, registry: ToolRegistry, name: str, tool_use_id: str) -> dict[str, Any]:
    set_agent_ref(agent)
    tool = registry.dynamic_tools[name]
    return await tool.mcp_client.call_tool_async(tool_use_id, tool.mcp_tool.name, {})


def work_for(agent: Any, person: PersonPrincipal, task_id: str) -> None:
    """The agent now runs a task the gateway signed for ``person``."""

    meta = MessageMeta(conversation_id=f"conv-{task_id}")
    meta.gateway_context = SimpleNamespace(
        task_context=context_signer().issue(team_id=TEAM, person_id=person.person_id, person_roles=person.roles)
    )
    agent.task_manager.add_task(Task(task_id=task_id, task_type=MessageMetaType.REQUEST), MessageHeader(), meta, owner_person_id=None)
    agent._active_execution_task_id = task_id


async def install(world: World) -> Any:
    """Steps 1 and 2: one call proposes, one review installs. Returns the decided request."""

    set_agent_ref(world.lead)
    context = Context("tooluse-propose-mcp")
    with pytest.raises(InterruptException):
        await propose(context)
    request = world.service.store.get_request(context.raised[0]["access_request_id"])
    decided = world.requests().decide(request.request_id, OWNER, AccessReviewChoice.APPROVE_ALL)
    context.responses[access_tools.ACCESS_REQUEST_INTERRUPT_NAME] = access_request_runtime.decision_response(
        world.service.store.get_request(request.request_id)
    )
    resumed = await propose(context)
    assert resumed["status"] == "applied", resumed
    return decided


@pytest.mark.asyncio
async def test_one_call_proposes_and_one_review_installs_the_server_and_its_use(world) -> None:
    set_agent_ref(world.lead)
    context = Context("tooluse-propose-mcp")

    with pytest.raises(InterruptException):
        await propose(context)

    request = world.service.store.get_request(context.raised[0]["access_request_id"])
    rows = {row.item_id: row for row in request.change.items}
    [pending] = world.mcp_store().list_records()
    assert request.status is AccessRequestStatus.WAITING and request.change.kind == ACCESS_PROPOSAL_KIND
    assert pending.lifecycle is MCPConnectionLifecycle.PENDING, "nothing is installed before the review"
    assert TOOL_ID not in world.catalogs.current().tools
    assert rows["server-0"].section == "tool" and "Install the MCP server tickets" in rows["server-0"].text
    assert "https://tickets.example.test/mcp" in rows["server-0"].text and "new_host" in rows["server-0"].flags
    assert "api_token (as Authorization)" in rows["server-0"].text, "the card names the credential slot"
    # The default row: TeamArchitect may use every function and hand it on.
    assert "TeamArchitect: full access to tickets, and it may hand it on" in rows["grant-0"].text
    assert {"full_access", "can_hand_on"} <= set(rows["grant-0"].flags) and rows["grant-0"].keepable
    assert "credential tickets uses" in rows["account-0"].text
    assert not any(row.item_id.startswith("person-") for row in rows.values()), "members hold installed servers"
    assert not world.requests().may_decide(MEMBER, request), "installing is an administrator's decision"

    decided = world.requests().decide(request.request_id, OWNER, AccessReviewChoice.APPROVE_ALL)

    active = world.mcp_store().require(pending.connection_id)
    assert active.lifecycle is MCPConnectionLifecycle.ACTIVE
    assert (active.approval.mode, active.approval.approved_by, active.approval.access_request_id) == (
        ChangeApprovalMode.PERSON_APPROVAL,
        OWNER.person_id,
        request.request_id,
    )
    [grant] = world.grants()
    assert decided.grant_ids == (grant.grant_id,)
    assert (grant.subject.id, grant.level, grant.delegation_depth) == (f"{TEAM}/TeamArchitect", AccessLevel.FULL, 1)
    assert (grant.for_person_id, grant.lifetime) == (OWNER.person_id, GrantLifetime.UNTIL_REVOKED)
    assert [item.tool_id for item in world.applier.account_links[request.request_id]] == [TOOL_ID]
    catalog_tool = world.catalogs.current().tools[TOOL_ID]
    assert catalog_tool.tool_sets == ("mcp_servers",)

    context.responses[access_tools.ACCESS_REQUEST_INTERRUPT_NAME] = access_request_runtime.decision_response(
        world.service.store.get_request(request.request_id)
    )
    resumed = await propose(context)
    assert resumed["status"] == "applied" and resumed["grant_ids"] == [grant.grant_id]
    assert len(world.mcp_store().list_records()) == 1, "the resumed call did not record another connection"


@pytest.mark.asyncio
async def test_a_second_approval_while_the_first_applies_is_refused_and_the_server_stays_installed(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before decisions claimed requests, a second approval that read the
    # server as pending put it back to pending when it lost the race.
    set_agent_ref(world.lead)
    context = Context("tooluse-propose-mcp-twice")
    with pytest.raises(InterruptException):
        await propose(context)
    request_id = context.raised[0]["access_request_id"]
    [pending] = world.mcp_store().list_records()
    store = world.service.store
    original = store.apply_together
    refusals: list[str] = []

    def second_approval(*args: Any, **kwargs: Any) -> Any:
        # The first approval has installed the server and is about to store its outcome.
        assert world.mcp_store().require(pending.connection_id).lifecycle is MCPConnectionLifecycle.ACTIVE
        try:
            world.requests().decide(request_id, OWNER, AccessReviewChoice.APPROVE_ALL)
        except HandDownRefused as exc:
            refusals.append(str(exc))
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "apply_together", second_approval)
    decided = world.requests().decide(request_id, OWNER, AccessReviewChoice.APPROVE_ALL)

    assert refusals and "deciding this request right now" in refusals[0]
    assert decided.status is AccessRequestStatus.GRANTED
    assert world.mcp_store().require(pending.connection_id).lifecycle is MCPConnectionLifecycle.ACTIVE
    [grant] = world.grants()
    assert decided.grant_ids == (grant.grant_id,)


@pytest.mark.asyncio
async def test_a_busy_database_while_applying_leaves_the_server_pending_and_a_later_approval_works(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before 2026-10-17 only refusals were undone: after "database is locked"
    # the server stayed installed while the request waited, and every later
    # approval was refused because the server was no longer pending.
    import sqlite3

    set_agent_ref(world.lead)
    context = Context("tooluse-propose-mcp-busy")
    with pytest.raises(InterruptException):
        await propose(context)
    request_id = context.raised[0]["access_request_id"]
    [pending] = world.mcp_store().list_records()
    store = world.service.store
    original = store.apply_together
    failures: list[int] = []

    def busy_once(*args: Any, **kwargs: Any) -> Any:
        if not failures:
            failures.append(1)
            raise sqlite3.OperationalError("database is locked")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "apply_together", busy_once)
    with pytest.raises(HandDownRefused, match="couldn't be saved because the access store was busy"):
        world.requests().decide(request_id, OWNER, AccessReviewChoice.APPROVE_ALL)

    assert world.mcp_store().require(pending.connection_id).lifecycle is MCPConnectionLifecycle.PENDING
    assert world.service.store.get_request(request_id).status is AccessRequestStatus.WAITING
    assert world.grants() == []

    decided = world.requests().decide(request_id, OWNER, AccessReviewChoice.APPROVE_ALL)
    assert decided.status is AccessRequestStatus.GRANTED
    assert world.mcp_store().require(pending.connection_id).lifecycle is MCPConnectionLifecycle.ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_an_unconfirmed_mcp_install_keeps_or_restores_the_server_on_recovery(world, monkeypatch, committed):
    from datetime import timedelta

    from access_control.requests import DecisionOutcomeUnknown
    from storage.access_repository import AccessRepository
    from tests.access_decision_support import uncertain_store

    set_agent_ref(world.lead)
    context = Context("tooluse-propose-mcp-uncertain")
    with pytest.raises(InterruptException):
        await propose(context)
    request_id = context.raised[0]["access_request_id"]
    [pending] = world.mcp_store().list_records()
    store = world.service.store
    with uncertain_store(monkeypatch, store, "apply_together", committed=committed):
        with pytest.raises(DecisionOutcomeUnknown):
            world.requests().decide(request_id, OWNER, AccessReviewChoice.APPROVE_ALL)
    assert world.mcp_store().require(pending.connection_id).lifecycle is MCPConnectionLifecycle.ACTIVE
    world.service.store = AccessRepository(store.settings)
    later = world.service.clock() + timedelta(minutes=11)
    monkeypatch.setattr(world.service, "clock", lambda: later)
    world.requests().recover_decisions()
    expected = MCPConnectionLifecycle.ACTIVE if committed else MCPConnectionLifecycle.PENDING
    assert world.mcp_store().require(pending.connection_id).lifecycle is expected
    assert world.service.store.decision_recoveries(world.service.team_id, now=later) == []


def refusal(result: dict[str, Any]) -> dict[str, Any]:
    """The refusal the model reads in a failed MCP tool result."""

    assert result["status"] == "error", result
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_agents_use_every_function_for_whom_it_was_authorized_and_hand_it_on(world, monkeypatch) -> None:
    lead_registry, lead_hook = model_view(world.lead)
    assert await next_model_call(lead_hook, lead_registry) == set(), "no server is installed yet"
    await install(world)

    async with TestServer(world.broker().build_app()) as server:
        monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", str(server.make_url("")).rstrip("/"))

        # 3. No account is linked yet, so the first load fails; find_tools
        # says why. Linking the accounts changes access, and the next model
        # call loads the tools.
        assert await next_model_call(lead_hook, lead_registry) == set()
        set_agent_ref(world.lead)
        [missing] = json.loads(tool_loading.find_tools(query="tickets"))["tools"]
        assert missing["load_status"].startswith("Not loaded: the secure broker refused to list its tools (No account")
        assert "tried again when access or the server changes" in missing["load_status"]
        world.link(LinkOwnerKind.PERSON, OWNER.person_id, "tickets_owner")
        world.link(LinkOwnerKind.TEAM, TEAM, "tickets_team")
        assert await next_model_call(lead_hook, lead_registry) == {"tickets_search", "tickets_purge_index"}
        search_tool = lead_registry.dynamic_tools["tickets_search"]
        set_agent_ref(world.lead)
        [found] = json.loads(tool_loading.find_tools(query="tickets_purge_index"))["tools"]
        assert (found["tool_id"], found["your_level"]) == (TOOL_ID, "full"), "found by its function's name"
        assert found["functions"] == ["tickets_purge_index", "tickets_search"]
        assert "load_status" not in found, "its tools are loaded"

        search = await call(world.lead, lead_registry, "tickets_search", "tu-1")
        purge = await call(world.lead, lead_registry, "tickets_purge_index", "tu-2")
        assert (search["status"], purge["status"]) == ("success", "success"), (search, purge)
        assert world.catalogs.current().resolve_mcp(TOOL_ID, "purge_index").level is AccessLevel.FULL, (
            "nobody confirmed its level; the grant on the whole server covers it"
        )
        assert FakeServer.calls == [("search", f"Bearer {OWNER_TOKEN}"), ("purge_index", f"Bearer {OWNER_TOKEN}")], (
            "the person's own account link comes first"
        )

        # 4. The same agent, now working for a person who has not authorized it.
        work_for(world.lead, MEMBER, "task-member")
        assert await next_model_call(lead_hook, lead_registry) == set(), "its tools follow this person's grants"
        set_agent_ref(world.lead)
        denied = refusal(await search_tool.mcp_client.call_tool_async("tu-3", "search", {}))
        assert (denied["reason_code"], denied["tool_id"], denied["operation"], denied["level"]) == (
            "access_denied",
            TOOL_ID,
            "search",
            "full",
        )
        assert "request_access" in denied["next_step"] and "TeamArchitect needs full access" in denied["reason"]
        world.service.grant_by_person(
            MEMBER,
            GrantV1(
                grant_id="new",
                team_id=TEAM,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect"),
                target=TargetRefV1(kind=TargetKind.TOOL, id=TOOL_ID),
                level=AccessLevel.FULL,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=MEMBER.person_id),
                created_at_utc=world.service.clock(),
                for_person_id=MEMBER.person_id,
            ),
        )
        assert await next_model_call(lead_hook, lead_registry) == {"tickets_search", "tickets_purge_index"}
        assert (await call(world.lead, lead_registry, "tickets_search", "tu-4"))["status"] == "success"
        assert FakeServer.calls[-1] == ("search", f"Bearer {TEAM_TOKEN}"), (
            "the member has no link of their own, so the team's is used; and no rule reserves the server "
            "for the person who proposed it"
        )

        # 5. A worker asks; TeamArchitect's row may be handed on, so nobody is asked.
        set_agent_ref(world.worker)
        asked = json.loads(
            access_tools.request_access(
                tool_id=TOOL_ID, level="full", reason="Search the tickets", tool_context=Context("tu-worker")
            )
        )
        assert asked["status"] == "granted", asked
        worker_registry, worker_hook = model_view(world.worker)
        assert await next_model_call(worker_hook, worker_registry) == {"tickets_search", "tickets_purge_index"}
        assert (await call(world.worker, worker_registry, "tickets_purge_index", "tu-5"))["status"] == "success"

        # Revoking TeamArchitect's grant ends its hand-down too.
        work_for(world.lead, OWNER, "task-owner-2")
        [lead_grant] = [
            grant for grant in world.grants() if grant.for_person_id == OWNER.person_id and grant.parent_grant_id is None
        ]
        world.service.revoke(lead_grant.grant_id, actor=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id))
        assert await next_model_call(lead_hook, lead_registry) == set()
        worker_tool = worker_registry.dynamic_tools["tickets_search"]
        set_agent_ref(world.worker)
        assert refusal(await worker_tool.mcp_client.call_tool_async("tu-6", "search", {}))["reason_code"] == (
            "access_denied"
        )
        assert await next_model_call(worker_hook, worker_registry) == set()


@pytest.mark.asyncio
async def test_a_failure_while_storing_leaves_the_server_pending_and_no_grants(world, monkeypatch) -> None:
    set_agent_ref(world.lead)
    context = Context("tooluse-propose-mcp")
    with pytest.raises(InterruptException):
        await propose(context)
    request_id = context.raised[0]["access_request_id"]

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("disk full")

    monkeypatch.setattr(world.service.store, "apply_together", broken)
    with pytest.raises(HandDownRefused, match="nothing changed"):
        world.requests().decide(request_id, OWNER, AccessReviewChoice.APPROVE_ALL)

    [record] = world.mcp_store().list_records()
    assert record.lifecycle is MCPConnectionLifecycle.PENDING and record.approval is None
    assert TOOL_ID not in world.catalogs.current().tools
    assert world.grants() == []
    assert world.service.store.get_request(request_id).status is AccessRequestStatus.WAITING
    # The pending record is exactly the one proposed, so the same review can still be approved.
    monkeypatch.undo()
    assert world.requests().decide(request_id, OWNER, AccessReviewChoice.APPROVE_ALL).applied_at_utc is not None


@pytest.mark.asyncio
async def test_a_server_already_installed_is_not_proposed_again(world) -> None:
    await install(world)
    set_agent_ref(world.lead)

    again = await propose(Context("tooluse-again"))

    assert again["status"] == "refused" and "already installed as mcp:tickets" in again["reason"]
    assert "request_access" in again["reason"]


@pytest.mark.asyncio
async def test_recovery_prevents_a_paused_decision_from_activating_its_server(world, monkeypatch) -> None:
    from contextlib import contextmanager
    from datetime import timedelta

    set_agent_ref(world.lead)
    context = Context("tooluse-paused-activation")
    with pytest.raises(InterruptException):
        await propose(context)
    request_id = context.raised[0]["access_request_id"]
    publish = world.service.store.decision_effect_transaction
    later = world.service.clock() + timedelta(minutes=11)

    @contextmanager
    def paused(claim):
        # The server is journaled, but activation has not acquired its lock.
        monkeypatch.setattr(world.service, "clock", lambda: later)
        world.requests().recover_decisions()
        with publish(claim):
            yield

    monkeypatch.setattr(world.service.store, "decision_effect_transaction", paused)
    with pytest.raises(HandDownRefused):
        world.requests().decide(request_id, OWNER, AccessReviewChoice.APPROVE_ALL)
    [record] = world.mcp_store().list_records()
    assert record.lifecycle is MCPConnectionLifecycle.PENDING
    assert world.service.store.get_request(request_id).status is AccessRequestStatus.WAITING
    assert world.service.store.decision_recoveries(TEAM) == []
    assert world.grants() == []


@pytest.mark.asyncio
async def test_with_authorization_off_the_persons_agreement_installs_it_and_its_tools_load(
    storage, tmp_path, monkeypatch
) -> None:
    FakeServer.calls = []
    monkeypatch.setattr("secure_tool_broker.mcp_runtime.BrokerMCPClient", FakeServer)
    monkeypatch.setattr(BrokerMCPRuntime, "_validate_transport_policy", lambda _self, _record, _raw: ())
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", caller_token(CALLER_MANAGER))
    world = World(storage, tmp_path, enabled=False)
    registry, hook = model_view(world.lead)
    set_agent_ref(world.lead)
    try:
        async with TestServer(world.broker().build_app()) as server:
            monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", str(server.make_url("")).rstrip("/"))
            assert await next_model_call(hook, registry) == set(), "nothing is installed yet"
            asked = json.loads(
                await mcp_connections(
                    action="propose",
                    connection_name="tickets", endpoint="https://tickets.example.test/mcp", tool_context=Context("t1")
                )
            )
            assert asked["status"] == "agreement_needed" and asked["changes"] == [
                "Install the MCP server tickets (https://tickets.example.test/mcp)"
            ]
            assert world.mcp_store().list_records()[0].lifecycle is MCPConnectionLifecycle.PENDING

            applied = json.loads(
                await mcp_connections(
                    action="propose",
                    connection_name="tickets",
                    endpoint="https://tickets.example.test/mcp",
                    confirmed=True,
                    tool_context=Context("t2"),
                )
            )
            assert applied["status"] == "applied", applied
            assert applied["mcp_servers"] == {
                TOOL_ID: "Installed. Its tools appear in your tool list from your next step, once the server answers."
            }
            [record] = world.mcp_store().list_records()
            assert (record.lifecycle, record.approval.mode) == (MCPConnectionLifecycle.ACTIVE, ChangeApprovalMode.AGREEMENT)

            # The running agent's next model call loads it: the tool catalog
            # reloaded when the record changed.
            assert await next_model_call(hook, registry) == {"tickets_search", "tickets_purge_index"}
            result = await call(world.lead, registry, "tickets_search", "tu")
            assert result["status"] == "success" and FakeServer.calls == [("search", None)]
            assert world.grants() == [], "with authorization off there are no grants"
    finally:
        world.service.cedar.close()
        access_runtime._runtime_access.cache_clear()
        set_agent_ref(None)
