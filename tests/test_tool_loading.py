"""Tools that load on request (custom_agents.tool_loading).

Real Cedar, SQLite, signed task contexts and a real Strands tool registry
(the fixtures of tests/test_tool_sync.py): the team lead starts without the
groups the catalog loads on request, use_tools adds one for its task from the
next model call, an unused group leaves after LOADED_TOOL_IDLE_MODEL_CALLS
model calls and when the task changes, the model's tool cap is kept, and a
call to a function that is not loaded loads its group. The last test drives a
real TeamArchitect built from its definition through a scripted model.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from strands.tools.registry import ToolRegistry

from access_catalog.catalog import load_catalog
from access_catalog.tool_functions import functions_of_tools
from custom_agents import access_runtime
from custom_agents.tool_loading import (
    LOADED_TOOL_IDLE_MODEL_CALLS,
    LoadedTools,
    OnRequestToolHook,
    ToolLimitHook,
    loadable_tools_text,
    loaded_tools,
    tool_limit,
)
from custom_agents.tool_sync import startup_tools
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.provider_contract import tool_name
from custom_tools.providers import tool_loading
from custom_tools.registry import ALL_PROVIDER_TOOLS
from tests.task_work_support import bind_task, install_test_tool_sync, main_parts, set_agent_ref
from storage.access_repository import AccessRepository
from tests.test_access_request_tools import BINARY, make_agent
from tests.test_access_request_tools import storage as access_storage

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

storage = access_storage  # a manager database and verify keys
OBJECTIVES = "fn:objectives"
CATALOG = load_catalog(None)


def objectives_functions() -> set[str]:
    return functions_of_tools(CATALOG, {OBJECTIVES}) - CATALOG.on_request[OBJECTIVES]


def lead_agent(storage, tmp_path: Path, *, enabled: bool = True):
    agent = make_agent(storage, tmp_path, agent_type="TeamArchitect", lead=True, enabled=enabled)
    agent.tools = list(ALL_PROVIDER_TOOLS)
    agent.tool_filter = {}
    agent.default_tools = ()
    agent.model_provider, agent.model_id = "litellm", "openai/gpt-6-luna"
    agent.model_capabilities = {}
    set_agent_ref(agent)
    return agent


def model_view(agent):
    tools = startup_tools(agent, list(agent.tools))
    registry = ToolRegistry()
    registry.process_tools(tools)
    hook = install_test_tool_sync(agent, tools)
    agent._strands_agent = SimpleNamespace(tool_registry=registry)
    return registry, hook


def next_model_call(hook, registry) -> set[str]:
    event = SimpleNamespace(agent=SimpleNamespace(tool_registry=registry))
    asyncio.run(hook.before_model_call(event))
    return set(registry.registry) | set(registry.dynamic_tools)


def use(*tool_ids: str) -> dict:
    return json.loads(tool_loading.use_tools(tool_ids=list(tool_ids)))


# -- the catalog ------------------------------------------------------------------------


def test_every_group_that_loads_on_request_is_a_function_tool_and_kept_functions_are_its_own() -> None:
    assert CATALOG.on_request, "the catalog names groups that load on request"
    for tool_id, kept in CATALOG.on_request.items():
        assert tool_id in CATALOG.tools and tool_id.startswith("fn:"), tool_id
        assert kept <= functions_of_tools(CATALOG, {tool_id}), tool_id
    loading = CATALOG.tools["fn:tool_loading"]
    assert "agent_runtime" in loading.tool_sets and {o.name for o in loading.operations} == {"find_tools", "use_tools"}
    assert "fn:schedule_runs" not in CATALOG.on_request, "scheduled runs must record themselves"


# -- the loaded set ---------------------------------------------------------------------


def test_a_loaded_group_leaves_after_idle_model_calls_and_when_the_task_changes() -> None:
    state = LoadedTools()
    state.begin_model_call("task-1")
    state.load(["fn:objectives", "fn:heartbeat"])
    for _ in range(LOADED_TOOL_IDLE_MODEL_CALLS):
        state.begin_model_call("task-1")
        state.used("fn:objectives")

    _dropped, idle = state.begin_model_call("task-1")

    assert idle == ["fn:heartbeat"], "unused for more than the idle count"
    assert state.loaded() == {"fn:objectives"}, "a group in use stays"
    dropped, _idle = state.begin_model_call("task-2")
    assert dropped == ["fn:objectives"] and state.loaded() == frozenset()


def test_the_least_recently_used_group_goes_first() -> None:
    state = LoadedTools()
    state.load(["fn:objectives"])
    state.begin_model_call(None)
    state.load(["fn:heartbeat"])
    state.begin_model_call(None)
    state.used("fn:objectives")

    assert state.least_recently_used() == "fn:heartbeat"
    assert state.least_recently_used(excluding={"fn:heartbeat"}) == "fn:objectives"


# -- with authorization on ----------------------------------------------------------------


def test_the_team_lead_loads_a_group_for_its_task_and_it_leaves_when_unused(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    registry, hook = model_view(agent)
    started = next_model_call(hook, registry)
    assert not started & objectives_functions() and "activate_plan_execution" in started
    assert {"find_tools", "use_tools"} <= started

    result = use(OBJECTIVES)

    assert result["tools"][0]["status"] == "loaded" and "create_objective" in result["tools"][0]["functions"]
    seen = next_model_call(hook, registry)
    assert objectives_functions() <= seen, "from the next model call"
    # Shown for LOADED_TOOL_IDLE_MODEL_CALLS calls without use, the one above included.
    for _ in range(LOADED_TOOL_IDLE_MODEL_CALLS - 1):
        assert "create_objective" in next_model_call(hook, registry)
    assert "create_objective" not in next_model_call(hook, registry), "unused for too long"
    assert "activate_plan_execution" in next_model_call(hook, registry), "kept shown"


def test_using_a_loaded_function_keeps_its_group(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    registry, hook = model_view(agent)
    use(OBJECTIVES)
    tool_hook = OnRequestToolHook(agent, main_parts(agent))
    for _ in range(LOADED_TOOL_IDLE_MODEL_CALLS * 2):
        seen = next_model_call(hook, registry)
        call = SimpleNamespace(tool_use={"name": "objectives"}, selected_tool=registry.registry.get("objectives") or registry.dynamic_tools.get("objectives"))
        tool_hook.before_tool_call(call)
    assert "objectives" in seen


def test_use_tools_on_a_group_always_shown_says_it_is_in_the_list_and_find_tools_ranks_plain_words(storage, tmp_path) -> None:
    # Live, TeamArchitect once asked to load fn:memory, read "refused ... find_tools
    # shows how to get access", found nothing with a plain-words query, and told
    # the person memory was unavailable (2026-10-10).
    lead = lead_agent(storage, tmp_path)
    registry, hook = model_view(lead)
    next_model_call(hook, registry)

    answer = use("fn:memory")
    [memory] = answer["tools"]
    found = json.loads(tool_loading.find_tools(query="save a team memory fact and list them"))["tools"]

    assert memory["status"] == "already_in_your_tool_list"
    assert memory["functions"] == ["memory", "resolve_memory_review_job", "topic_tags"]
    assert "call their functions directly" in answer["message"]
    assert found[0]["tool_id"] == "fn:memory" and found[0]["in_your_tool_list"] is True
    assert json.loads(tool_loading.find_tools(query="no-such-word-anywhere"))["tools"] == []


def test_use_tools_refuses_what_does_not_load_on_request_or_is_not_held(storage, tmp_path) -> None:
    lead = lead_agent(storage, tmp_path)
    model_view(lead)
    refused = {item["tool_id"]: item for item in use("fn:schedules", "fn:nothing")["tools"]}
    assert refused["fn:schedules"]["status"] == "refused"
    assert "none of its functions is in your tool list" in refused["fn:schedules"]["reason"]
    assert "Not in the tool catalog" in refused["fn:nothing"]["reason"]

    worker = make_agent(storage, tmp_path / "worker", agent_type="WebResearchAgent")
    worker.tools, worker.tool_filter, worker.default_tools = list(ALL_PROVIDER_TOOLS), {}, ()
    set_agent_ref(worker)
    [objectives] = use(OBJECTIVES)["tools"]
    assert objectives["status"] == "refused" and "request_access(tool_id='fn:objectives'" in objectives["reason"]


def test_default_tools_do_not_claim_to_load_a_group_without_access(storage, tmp_path) -> None:
    worker = make_agent(storage, tmp_path, agent_type="WebResearchAgent")
    worker.tools, worker.tool_filter, worker.default_tools = list(ALL_PROVIDER_TOOLS), {}, (OBJECTIVES,)
    set_agent_ref(worker)
    registry, hook = model_view(worker)

    [result] = use(OBJECTIVES)["tools"]

    assert result["status"] == "refused"
    assert "list_objectives" not in next_model_call(hook, registry)


def test_a_worker_can_load_automatic_hand_on_tools_without_creating_access(storage, tmp_path) -> None:
    from custom_messages.contracts.access_v2 import AccessLevel, DelegationMode, GrantV1, SubjectKind, SubjectRefV1, TargetKind, TargetRefV1
    from tests.test_access_request_tools import OWNER, TEAM, owner_service

    service = owner_service(storage)
    service.grant_by_person(OWNER, GrantV1(
        grant_id="new", team_id=TEAM,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect"),
        target=TargetRefV1(kind=TargetKind.TOOL, id="fn:heartbeat"), level=AccessLevel.WRITE,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id), created_at_utc=service.clock(),
        for_person_id=OWNER.person_id, delegation_depth=1, delegation_mode=DelegationMode.AUTOMATIC,
    ))
    worker = make_agent(storage, tmp_path, agent_type="WebResearchAgent")
    worker.tools, worker.tool_filter, worker.default_tools = list(ALL_PROVIDER_TOOLS), {}, ()
    set_agent_ref(worker)
    registry, hook = model_view(worker)
    next_model_call(hook, registry)
    before = service.store.version(TEAM)

    [found] = [t for t in json.loads(tool_loading.find_tools(query="list_heartbeat_items"))["tools"] if t["tool_id"] == "fn:heartbeat"]
    [result] = use("fn:heartbeat")["tools"]

    assert found["your_level"] == "write" and found["load_with"] == "use_tools(tool_ids=['fn:heartbeat'])"
    assert result["status"] == "loaded"
    assert "list_heartbeat_items" in next_model_call(hook, registry)
    assert service.store.version(TEAM) == before, "discovery and loading must not give access"
    assert access_runtime.decide_call(worker, "list_heartbeat_items", {}).allowed
    assert service.store.version(TEAM) > before, "the actual decision hands access on for this task"


def test_a_group_the_definition_names_stays_shown_and_is_not_in_the_prompt_list(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    agent.default_tools = ("fn:heartbeat",)
    registry, hook = model_view(agent)
    seen = next_model_call(hook, registry)
    text = loadable_tools_text(agent, CATALOG)

    assert "add_heartbeat_item" in seen
    assert "- fn:objectives (Objectives and work sessions)" in text and "create_objective" in text
    assert "fn:heartbeat" not in text and "activate_plan_execution" not in text, "shown ones are not listed"
    assert "use_tools(tool_ids=[...])" in text


def test_an_objective_work_session_has_the_objectives_tools(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    registry, hook = model_view(agent)
    meta = MessageMeta(conversation_id="conv-session")
    meta.gateway_context = agent.task_manager.get_task_info(agent.test_task_id).meta.gateway_context
    meta.objective_work_session_id = "session-1"
    agent.task_manager.add_task(Task(task_id="task-session", task_type=MessageMetaType.REQUEST), MessageHeader(), meta, owner_person_id=None)
    bind_task("task-session")

    assert "record_objective_work_checkpoint" in next_model_call(hook, registry)


# -- the cap -------------------------------------------------------------------------------


def cap(agent, limit: int) -> None:
    agent.model_capabilities = {"litellm": {"openai/gpt-6-luna": {"max_tools_per_request": limit}}}


def test_the_cap_comes_from_the_override_file_over_the_configuration_for_the_model_in_use(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    assert tool_limit(agent) == (128, "litellm", "openai/gpt-6-luna"), "the default without an entry"
    cap(agent, 90)
    assert tool_limit(agent)[0] == 90
    agent._persistent_model_capabilities = {"litellm/openai/gpt-6-luna": {"max_tools_per_request": 100}}
    assert tool_limit(agent)[0] == 100, "LLM_OVERRIDES.yaml wins"
    person_model = SimpleNamespace(provider="openai_compatible", profile=SimpleNamespace(model_id="their/model"))
    with patch("custom_agents.person_model_runtime.active_bundle", return_value=person_model):
        assert tool_limit(agent) == (128, "openai_compatible", "their/model"), "a person's own model has its own cap"


def test_a_load_past_the_cap_unloads_the_oldest_group_first_and_otherwise_is_refused(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    registry, hook = model_view(agent)
    shown = len(next_model_call(hook, registry))
    heartbeat = len(functions_of_tools(CATALOG, {"fn:heartbeat"}))
    use("fn:heartbeat")
    next_model_call(hook, registry)
    # Room for the heartbeat group, not for the larger objectives group even after unloading it.
    assert len(objectives_functions()) > heartbeat
    cap(agent, shown + heartbeat)

    [objectives] = use(OBJECTIVES)["tools"]
    assert objectives["status"] == "refused" and "max_tools_per_request" in objectives["reason"]
    assert loaded_tools(agent).loaded() == {"fn:heartbeat"}, "a refused load unloads nothing"

    cap(agent, shown + len(objectives_functions()))
    [objectives] = use(OBJECTIVES)["tools"]
    assert objectives["status"] == "loaded" and objectives["unloaded_to_make_room"] == ["fn:heartbeat"]
    seen = next_model_call(hook, registry)
    assert "create_objective" in seen and "add_heartbeat_item" not in seen


def test_concurrent_loads_project_and_commit_capacity_together(storage, tmp_path, monkeypatch) -> None:
    from custom_agents.tool_loading import projected_names

    agent = lead_agent(storage, tmp_path, enabled=False)
    registry, hook = model_view(agent)
    base = len(next_model_call(hook, registry))
    cap(agent, base + len(objectives_functions()))
    first_projected, second_projected = threading.Event(), threading.Event()
    original = tool_loading._fits

    def paused_projection(agent, catalog, tool_id, keep):
        result = original(agent, catalog, tool_id, keep)
        if tool_id == OBJECTIVES:
            first_projected.set()
            second_projected.wait(1)
        else:
            second_projected.set()
        return result

    def load(tool_id):
        set_agent_ref(agent)
        return use(tool_id)["tools"][0]

    monkeypatch.setattr(tool_loading, "_fits", paused_projection)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(load, OBJECTIVES)
        assert first_projected.wait(5)
        second = executor.submit(load, "fn:heartbeat")
        first_result, second_result = first.result(timeout=5), second.result(timeout=5)

    assert first_result["status"] == second_result["status"] == "loaded"
    assert second_result.get("unloaded_to_make_room") == [OBJECTIVES]
    assert len(projected_names(agent, CATALOG, registry)) <= tool_limit(agent)[0]
    seen = next_model_call(hook, registry)
    assert "list_heartbeat_items" in seen and "list_objectives" not in seen


def test_a_loaded_group_that_becomes_required_for_the_work_cannot_be_evicted(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    registry, hook = model_view(agent)
    base = len(next_model_call(hook, registry))
    use(OBJECTIVES)
    info = agent.task_manager.get_task_info(agent.test_task_id)
    info.meta.objective_work_session_id = "session-started-in-this-task"
    cap(agent, base + len(objectives_functions()))

    [result] = use("fn:heartbeat")["tools"]

    assert result["status"] == "refused"
    assert loaded_tools(agent).loaded() == {OBJECTIVES}
    seen = next_model_call(hook, registry)
    assert len(seen) <= tool_limit(agent)[0] and "record_objective_work_checkpoint" in seen
    cap(agent, len(seen) - 1)
    ToolLimitHook(agent, main_parts(agent)).before_model_call(SimpleNamespace(agent=SimpleNamespace(tool_registry=registry)))
    assert loaded_tools(agent).loaded() == {OBJECTIVES}, "required tools cannot be removed to meet a smaller cap"


def test_over_the_cap_the_hook_unloads_loaded_groups_then_tells_administrators_once(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    registry, hook = model_view(agent)
    use(OBJECTIVES)
    shown = len(next_model_call(hook, registry))
    limit_hook = ToolLimitHook(agent, main_parts(agent))
    cap(agent, shown - 1)

    limit_hook.before_model_call(SimpleNamespace(agent=SimpleNamespace(tool_registry=registry)))

    assert loaded_tools(agent).loaded() == frozenset() and "create_objective" not in registry.dynamic_tools
    cap(agent, 10)
    for _ in range(2):
        limit_hook.before_model_call(SimpleNamespace(agent=SimpleNamespace(tool_registry=registry)))
    stored = AccessRepository(storage).notices_for(agent.team_id, person_id="admin-1", roles=("admin",))
    notices = [notice for notice, _read in stored if notice.kind.value == "tool_limit_reached"]
    assert len(notices) == 1 and "TeamArchitect has more tools than openai/gpt-6-luna accepts" == notices[0].title
    assert "max_tools_per_request" in notices[0].summary


# -- a call to a function that is not loaded -----------------------------------------------


def test_a_call_to_a_function_not_loaded_gets_its_tool_and_loads_the_group(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    registry, hook = model_view(agent)
    next_model_call(hook, registry)
    event = SimpleNamespace(tool_use={"name": "objectives"}, selected_tool=None)

    OnRequestToolHook(agent, main_parts(agent)).before_tool_call(event)

    assert tool_name(event.selected_tool) == "objectives" and OBJECTIVES in loaded_tools(agent).loaded()
    assert "objectives" in next_model_call(hook, registry), "its definition is shown from the next call"


# -- with authorization off ------------------------------------------------------------------


def test_with_authorization_off_only_the_definitions_tools_load(storage, tmp_path) -> None:
    from custom_agents.strands_team import AGENT_TYPE_MAP
    from custom_agents.team_config_helpers import filter_agent_tools

    agent = lead_agent(storage, tmp_path, enabled=False)
    # Its real definition's tools, as create_strands_agent gives them, without one objectives function.
    definition = filter_agent_tools(AGENT_TYPE_MAP["TeamArchitect"]["tools"], authorization_enabled=False)
    agent.tools = [tool for tool in definition if tool_name(tool) != "objectives"]
    registry, hook = model_view(agent)
    assert "create_objective" not in next_model_call(hook, registry)
    [found] = [t for t in json.loads(tool_loading.find_tools(query="create_objective"))["tools"] if t["tool_id"] == OBJECTIVES]
    assert found["load_with"] == "use_tools(tool_ids=['fn:objectives'])" and found["in_your_tool_list"] is False

    use(OBJECTIVES)
    seen = next_model_call(hook, registry)
    assert "create_objective" in seen and "objectives" not in seen, "only functions the definition gives"
    event = SimpleNamespace(tool_use={"name": "objectives"}, selected_tool=None)
    OnRequestToolHook(agent, main_parts(agent)).before_tool_call(event)
    assert event.selected_tool is None, "authorization off allows every catalog tool, so only the definition's load"


def test_a_shown_group_reports_only_the_functions_the_agent_can_be_shown(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path, enabled=False)
    agent.default_tools = (OBJECTIVES,)
    agent.tools = [t for t in agent.tools if tool_name(t) != "list_objectives"]
    agent.tool_filter = {"tool_blocklist": ["create_objective"]}
    registry, hook = model_view(agent)

    [result] = use(OBJECTIVES)["tools"]

    assert result["status"] == "loaded"
    assert "list_objectives" not in result["functions"] and "create_objective" not in result["functions"]
    assert set(result["functions"]) <= next_model_call(hook, registry)
    access_runtime._runtime_access.cache_clear()


# -- a real TeamArchitect ----------------------------------------------------------------------


def test_a_real_team_lead_loads_objectives_and_its_next_request_carries_them(tmp_path, monkeypatch) -> None:
    """Built from its definition by create_strands_agent; the scripted model asks for the group, then answers."""

    from strands.models import Model

    from custom_agents.strands_team import AGENT_TYPE_MAP, create_strands_agent

    seen: list[list[str]] = []

    class Scripted(Model):
        config: dict = {}

        def update_config(self, **_kw) -> None: ...

        def get_config(self) -> dict:
            return {}

        async def structured_output(self, *_a, **_k):
            raise NotImplementedError

        async def stream(self, messages, tool_specs=None, system_prompt=None, **_kw):
            seen.append(sorted(spec["name"] for spec in tool_specs or []))
            if len(seen) == 1:
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1", "name": "use_tools"}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps({"tool_ids": [OBJECTIVES]})}}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}
                return
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockDelta": {"delta": {"text": "OK"}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}

    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(tmp_path / "instance"))
    config = dict(AGENT_TYPE_MAP["TeamArchitect"])
    config.update({
        "team_id": "team_real", "is_team_lead": True, "named_agent_type": "TeamArchitect",
        "authorization_config": {"enabled": False},
        "task_history_db_path": str(tmp_path / "t.db"), "storage_sqlite_path": str(tmp_path / "t.db"),
        "instance_root": str(tmp_path / "instance"),
    })
    with patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=Scripted()):
        agent = create_strands_agent(config)
    set_agent_ref(agent)
    inner = agent._strands_agent

    inner("Plan the quarter")

    first, second = seen
    assert len(first) <= 128 and "create_objective" not in first and "use_tools" in first
    assert "create_objective" in second, "the request after use_tools carries the loaded group"
    assert "Tools you can load:" in inner.system_prompt and "- fn:objectives" in inner.system_prompt
    access_runtime._runtime_access.cache_clear()


def test_find_tools_gives_the_team_lead_the_use_tools_call_for_a_group_it_holds(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    model_view(agent)
    [objectives] = [t for t in json.loads(tool_loading.find_tools(query="create_objective"))["tools"] if t["tool_id"] == OBJECTIVES]

    assert objectives["loads_on_request"] is True and objectives["load_with"] == "use_tools(tool_ids=['fn:objectives'])"
    assert objectives["your_level"] is not None and objectives["in_your_tool_list"] is False


# -- the setting -----------------------------------------------------------------------------


def test_max_tools_per_request_is_a_whole_number_set_only_for_a_model() -> None:
    from config_loader import lookup_configured_model_tool_limit, validate_llm_override_payload

    valid = validate_llm_override_payload({"model_capabilities": {"litellm": {"openai/gpt-6-luna": {"max_tools_per_request": 128}}}})
    zero = validate_llm_override_payload({"model_capabilities": {"litellm": {"m": {"max_tools_per_request": 0}}}})
    agent_type = validate_llm_override_payload({"agent_types": {"TeamArchitect": {"max_tools_per_request": 5}}})

    assert valid["valid"] and valid["summary"]["model_capability_entries"] == ["litellm/openai/gpt-6-luna"]
    assert zero["errors"] == ["model_capabilities.litellm.m.max_tools_per_request must be a whole number of at least 1."]
    assert agent_type["warnings"] == ["agent_types.TeamArchitect.max_tools_per_request is not recognized by the runtime and will be ignored."]
    flat = {"litellm/openai/gpt-6-luna": {"max_tools_per_request": "100"}}
    assert lookup_configured_model_tool_limit(flat, provider="litellm", model_id="openai/gpt-6-luna") == 100
    assert lookup_configured_model_tool_limit({"litellm": {"x": {"max_tools_per_request": "lots"}}}, provider="litellm", model_id="x") is None


@pytest.mark.parametrize("value", [128.9, float("inf"), float("-inf"), float("nan"), True, 0, -1, "lots"])
def test_invalid_tool_caps_are_rejected_and_logged_without_crashing(value, caplog, monkeypatch) -> None:
    from config_loader import lookup_configured_model_tool_limit, validate_llm_override_payload

    monkeypatch.setattr("config_loader._REPORTED_TOOL_LIMITS", set())
    payload = {"model_capabilities": {"litellm": {"invalid/model": {"max_tools_per_request": value}}}}
    assert not validate_llm_override_payload(payload)["valid"]
    assert lookup_configured_model_tool_limit(payload["model_capabilities"], provider="litellm", model_id="invalid/model") is None
    assert "max_tools_per_request" in caplog.text


def test_a_real_team_lead_calling_a_function_it_has_not_loaded_runs_it(tmp_path, monkeypatch) -> None:
    """Strands still runs the hooks for a tool not in its registry; ours gives the call its tool."""

    from strands.models import Model

    from custom_agents.strands_team import AGENT_TYPE_MAP, create_strands_agent

    results: list[str] = []

    class Scripted(Model):
        config: dict = {}
        calls = 0

        def update_config(self, **_kw) -> None: ...

        def get_config(self) -> dict:
            return {}

        async def structured_output(self, *_a, **_k):
            raise NotImplementedError

        async def stream(self, messages, tool_specs=None, system_prompt=None, **_kw):
            Scripted.calls += 1
            if Scripted.calls == 1:
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1", "name": "objectives"}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{\"action\": \"list\"}"}}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}
                return
            last = messages[-1]["content"][0].get("toolResult", {})
            results.append(json.dumps(last.get("content", [])))
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockDelta": {"delta": {"text": "OK"}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}

    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(tmp_path / "instance"))
    config = dict(AGENT_TYPE_MAP["TeamArchitect"])
    config.update({
        "team_id": "team_real", "is_team_lead": True, "named_agent_type": "TeamArchitect",
        "authorization_config": {"enabled": False},
        "task_history_db_path": str(tmp_path / "t.db"), "storage_sqlite_path": str(tmp_path / "t.db"),
        "instance_root": str(tmp_path / "instance"),
    })
    with patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=Scripted()):
        agent = create_strands_agent(config)
    set_agent_ref(agent)
    assert "objectives" not in agent._strands_agent.tool_names

    agent._strands_agent("Which objectives are open?")

    [result] = results
    assert "Unknown tool" not in result, result
    assert OBJECTIVES in loaded_tools(agent).loaded()
    access_runtime._runtime_access.cache_clear()


def limit_check(agent, registry) -> None:
    ToolLimitHook(agent, main_parts(agent)).before_model_call(SimpleNamespace(agent=SimpleNamespace(tool_registry=registry)))


def test_a_group_unloaded_for_the_cap_and_loaded_again_comes_back(storage, tmp_path) -> None:
    # Only the tool-list sync changes the registry, so its record of what it shows stays true.
    agent = lead_agent(storage, tmp_path)
    registry, hook = model_view(agent)
    shown = len(next_model_call(hook, registry))
    use(OBJECTIVES)
    next_model_call(hook, registry)
    use("fn:heartbeat")
    next_model_call(hook, registry)
    heartbeat = len(functions_of_tools(CATALOG, {"fn:heartbeat"}))
    cap(agent, shown + len(objectives_functions()))
    limit_check(agent, registry)  # objectives was used least recently: it leaves
    assert loaded_tools(agent).loaded() == {"fn:heartbeat"}

    [again] = use(OBJECTIVES)["tools"]  # makes room by unloading heartbeat
    seen = next_model_call(hook, registry)
    limit_check(agent, registry)

    assert again["status"] == "loaded" and again["unloaded_to_make_room"] == ["fn:heartbeat"]
    assert objectives_functions() <= seen and "add_heartbeat_item" not in seen
    assert len(seen) <= shown + len(objectives_functions()) and heartbeat


def test_two_groups_loaded_at_once_cannot_go_over_the_cap(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    registry, hook = model_view(agent)
    shown = len(next_model_call(hook, registry))
    cap(agent, shown + len(objectives_functions()))

    loaded, refused = use(OBJECTIVES, "fn:heartbeat")["tools"]

    assert loaded["status"] == "loaded" and refused["status"] == "refused", "the model is never told it is ready"
    assert len(next_model_call(hook, registry)) <= shown + len(objectives_functions())


def test_the_call_hook_does_not_load_a_group_that_is_shown(storage, tmp_path) -> None:
    agent = lead_agent(storage, tmp_path)
    agent.default_tools = ("fn:heartbeat",)
    registry, hook = model_view(agent)
    next_model_call(hook, registry)
    event = SimpleNamespace(tool_use={"name": "add_heartbeat_item"}, selected_tool=None)

    OnRequestToolHook(agent, main_parts(agent)).before_tool_call(event)

    assert event.selected_tool is None and loaded_tools(agent).loaded() == frozenset()
