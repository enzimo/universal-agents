"""Tools follow access (Phase 7): the model's tool list equals what the agent holds for this person.

Real Cedar, SQLite, TaskManager, signed task contexts and a real Strands tool
registry: TeamArchitect starts with its role's standard tools only; a grant
given during a task is offered on the next model call; revoking takes it
away; the same agent shows each person only what that person authorized; a
block on a whole tool removes it; the operator's block list still applies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands.tools.registry import ToolRegistry

from access_catalog.catalog import load_catalog
from access_control.entities import PersonPrincipal
from custom_agents import access_runtime
from custom_agents.tool_loading import shown_names
from custom_agents.tool_sync import GrantToolSync, MCPServerSync, standard_functions, startup_tools
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    GrantKind,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.provider_contract import tool_name
from custom_tools.registry import ALL_PROVIDER_TOOLS
from tests.task_work_support import bind_task, install_test_tool_sync, set_agent_ref
from tests.broker_access_support import context_signer
from tests.test_access_request_tools import BINARY, OWNER, TEAM, make_agent, owner_service
from tests.test_access_request_tools import storage as access_storage

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

MEMBER = PersonPrincipal("member-2", TEAM, ("member",))
LEAD_TYPE = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect")
SCHEDULES = TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules")


storage = access_storage  # the shared fixture: a manager database and verify keys


@pytest.fixture
def lead(storage, tmp_path: Path):
    agent = make_agent(storage, tmp_path, agent_type="TeamArchitect", lead=True)
    agent.instance_root = str(tmp_path / "instance")
    agent.tools = list(ALL_PROVIDER_TOOLS)  # what its definition used to give it
    agent.tool_filter = {}
    set_agent_ref(agent)
    service = owner_service(storage)
    yield agent, service
    service.cedar.close()


def model_view(agent) -> tuple[ToolRegistry, GrantToolSync]:
    tools = startup_tools(agent, list(agent.tools))
    registry = ToolRegistry()
    registry.process_tools(tools)
    hook = install_test_tool_sync(agent, tools)
    assert isinstance(hook, GrantToolSync)
    return registry, hook


def next_model_call(hook: GrantToolSync | MCPServerSync, registry: ToolRegistry) -> set[str]:
    asyncio.run(hook.before_model_call(SimpleNamespace(agent=SimpleNamespace(tool_registry=registry))))
    return set(registry.registry) | set(registry.dynamic_tools)


def give(service, person: PersonPrincipal, target: TargetRefV1, *, kind=GrantKind.ACCESS, for_person: str | None = "default"):
    return service.grant_by_person(
        person,
        GrantV1(
            grant_id="new",
            team_id=TEAM,
            kind=kind,
            subject=LEAD_TYPE,
            target=target,
            level=AccessLevel.WRITE if kind is GrantKind.ACCESS else AccessLevel.FULL,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=person.person_id),
            created_at_utc=service.clock(),
            for_person_id=(person.person_id if for_person == "default" else for_person) if kind is GrantKind.ACCESS else None,
        ),
    )


def test_the_team_lead_starts_with_its_standard_tools_and_a_grant_appears_on_the_next_model_call(lead) -> None:
    agent, service = lead
    registry, hook = model_view(agent)
    catalog = load_catalog(None)
    started = set(registry.registry)

    standard = standard_functions(agent) & {tool_name(tool) for tool in ALL_PROVIDER_TOOLS}
    # Groups that load on request wait until the task loads them (tests/test_tool_loading.py).
    assert started == shown_names(agent, catalog, standard, at_start=True)
    assert "create_objective" in standard and "create_objective" not in started
    assert "activate_plan_execution" in started, "plan execution is kept shown from its group"
    assert "create_scheduled_job" not in started and "shell" not in started
    assert "request_access" in started and "exa_search" in started, "access tools and internet are standard"
    assert not started & catalog.not_offered

    grant = give(service, OWNER, SCHEDULES)
    seen = next_model_call(hook, registry)
    assert "create_scheduled_job" in seen and "shell" not in seen

    service.revoke(grant.grant_id, actor=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id))
    assert "create_scheduled_job" not in next_model_call(hook, registry)


@pytest.mark.parametrize("subject_kind", [SubjectKind.AGENT_TYPE, SubjectKind.USER])
@pytest.mark.parametrize("at_start", [False, True])
def test_a_stale_block_withdraws_standard_functions_and_unblocking_restores_them(lead, subject_kind, at_start) -> None:
    from access_catalog.tool_functions import functions_of_tools

    agent, service = lead
    subject = LEAD_TYPE if subject_kind is SubjectKind.AGENT_TYPE else SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id)
    block = GrantV1(
        grant_id="stale-standard-block", team_id=TEAM, kind=GrantKind.BLOCK, subject=subject,
        target=TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#old_exa_search"),
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id), created_at_utc=service.clock(),
    )
    if at_start:
        service.store.create(block)
    registry, hook = model_view(agent)
    web_functions = functions_of_tools(service.catalog, {"fn:web_research"})
    if not at_start:
        assert "exa_search" in next_model_call(hook, registry)
        service.store.create(block)

    assert not web_functions & next_model_call(hook, registry)
    assert not web_functions & {spec["name"] for spec in registry.get_all_tool_specs()}
    assert "request_access" in registry.get_all_tools_config()
    service.store.revoke(block.grant_id, actor=block.granted_by, at=service.clock())
    assert "exa_search" in next_model_call(hook, registry)
    assert "exa_search" in {spec["name"] for spec in registry.get_all_tool_specs()}


def test_a_current_operation_block_keeps_the_standard_tool_visible(lead) -> None:
    agent, service = lead
    registry, hook = model_view(agent)
    give(service, OWNER, TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#exa_search"), kind=GrantKind.BLOCK)
    assert "exa_search" in next_model_call(hook, registry)


@pytest.mark.parametrize("subject_kind", [SubjectKind.AGENT_TYPE, SubjectKind.USER])
def test_a_timed_standard_block_restores_tools_at_expiry_without_a_store_change(lead, monkeypatch, subject_kind) -> None:
    from datetime import timedelta
    from custom_messages.contracts.access_v2 import GrantLifetime

    agent, service = lead
    now = service.clock()
    monkeypatch.setattr(service, "clock", lambda: now)
    runtime_service = access_runtime.runtime_access(agent).service
    monkeypatch.setattr(runtime_service, "clock", lambda: now)
    subject = LEAD_TYPE if subject_kind is SubjectKind.AGENT_TYPE else SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id)
    block = service.store.create(GrantV1(
        grant_id="timed-standard-block", team_id=TEAM, kind=GrantKind.BLOCK, subject=subject,
        target=TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#old_exa_search"),
        lifetime=GrantLifetime.UNTIL, expires_at_utc=now + timedelta(hours=1),
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id), created_at_utc=now,
    ))
    registry, hook = model_view(agent)
    version = service.store.version(TEAM)
    assert "exa_search" not in next_model_call(hook, registry)
    assert hook.desired() is None, "unchanged views remain cached before expiry"

    now = block.expires_at_utc
    assert "exa_search" in next_model_call(hook, registry)
    assert hook.desired() is None, "the restored view is cached after expiry"
    assert service.store.version(TEAM) == version


def test_the_same_agent_shows_each_person_what_they_authorized(lead, storage, tmp_path) -> None:
    agent, service = lead
    registry, hook = model_view(agent)
    give(service, OWNER, SCHEDULES)
    assert "create_scheduled_job" in next_model_call(hook, registry)

    # The lead now works on a task for another person, who authorized nothing.
    meta = MessageMeta(conversation_id="conv-2")
    meta.gateway_context = SimpleNamespace(
        task_context=context_signer().issue(team_id=TEAM, person_id=MEMBER.person_id, person_roles=("member",))
    )
    agent.task_manager.add_task(Task(task_id="task-member", task_type=MessageMetaType.REQUEST), MessageHeader(), meta, owner_person_id=None)
    bind_task("task-member")
    assert "create_scheduled_job" not in next_model_call(hook, registry)

    give(service, MEMBER, SCHEDULES)  # the member authorizes it for their own work
    assert "create_scheduled_job" in next_model_call(hook, registry)


def test_blocks_on_a_whole_tool_remove_it_and_the_operators_block_list_applies(lead) -> None:
    agent, service = lead
    agent.tool_filter = {"tool_blocklist": ["update_scheduled_job"]}
    registry, hook = model_view(agent)
    give(service, OWNER, SCHEDULES)
    seen = next_model_call(hook, registry)
    assert "create_scheduled_job" in seen and "update_scheduled_job" not in seen

    give(service, OWNER, SCHEDULES, kind=GrantKind.BLOCK)
    assert "create_scheduled_job" not in next_model_call(hook, registry)


def test_a_block_on_one_operation_leaves_the_tool_unless_an_update_removed_that_operation(lead) -> None:
    agent, service = lead
    registry, hook = model_view(agent)
    give(service, OWNER, SCHEDULES)

    def stored_block(grant_id: str, operation: str) -> None:
        # Written as an older version stored it, without today's catalog check (access_control.stale_blocks).
        service.store.create(
            GrantV1(
                grant_id=grant_id, team_id=TEAM, kind=GrantKind.BLOCK, subject=LEAD_TYPE,
                target=TargetRefV1(kind=TargetKind.OPERATION, id=f"fn:schedules#{operation}"),
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id), created_at_utc=service.clock(),
            )
        )

    stored_block("grant_block_current", "remove_scheduled_job")
    assert "create_scheduled_job" in next_model_call(hook, registry), "its other operations still work"
    stored_block("grant_block_renamed", "delete_scheduled_job")
    assert "create_scheduled_job" not in next_model_call(hook, registry), "the decision refuses the whole tool"


def test_with_authorization_off_the_definitions_tools_stay_and_only_mcp_servers_sync(storage, tmp_path) -> None:
    agent = make_agent(storage, tmp_path, agent_type="TeamArchitect", lead=True, enabled=False)
    agent.tools = list(ALL_PROVIDER_TOOLS)
    tools = startup_tools(agent, list(agent.tools))
    # The definition's tools, without the groups that load on request.
    shown = shown_names(agent, load_catalog(None), (tool_name(tool) for tool in agent.tools), at_start=True)
    assert tools == [tool for tool in agent.tools if tool_name(tool) in shown]
    assert "create_objective" not in shown and "calculator" in shown
    registry = ToolRegistry()
    registry.process_tools(tools)
    hook = install_test_tool_sync(agent, tools)
    assert isinstance(hook, MCPServerSync), "MCP servers load before model calls (tests/test_mcp_server_loading.py)"

    assert next_model_call(hook, registry) == {tool_name(tool) for tool in tools}
    access_runtime._runtime_access.cache_clear()
