"""Skills that load the tool groups they declare (``ua-tools``, custom_agents.tool_loading).

The real AgentSkills plugin of this runtime (``PromptBoundedAgentSkills``),
real Cedar, SQLite, signed task contexts and a real Strands tool registry with
the tool-list sync (the fixtures of tests/test_tool_loading.py): activating a
skill loads the groups it declares that the agent holds and is offered,
within the model's tool cap, through the same path as ``use_tools``. The skill
result names what loaded, what the agent lacks and what did not fit; nothing
is granted, and ids that are no catalog tool are logged and skipped.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands import Agent, Skill

from access_catalog.tool_functions import functions_of_tools
from custom_agents.sdk.strands.skills import PromptBoundedAgentSkills
from custom_agents.tool_loading import SKILL_TOOLS_METADATA_KEY, declared_skill_tool_ids, loaded_tools
from custom_tools.registry import ALL_PROVIDER_TOOLS
from custom_tools.providers import tool_loading
from runtime_settings.tool_presentation_contract import TOOL_PRESENTATION_INDEX, TOOL_PRESENTATION_INDEX_AND_SCRIPTS
from tests.task_work_support import set_agent_ref
from tests.test_access_request_tools import TEAM, make_agent, owner_service
from tests.test_call_tool import ScriptedStepsModel
from tests.test_tool_loading import BINARY, CATALOG, OBJECTIVES, cap, lead_agent, model_view, next_model_call
from tests.test_tool_loading import storage as loading_storage

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

storage = loading_storage
HEARTBEAT = "fn:heartbeat"
INSTRUCTIONS = "Plan the work, then track it as an objective."


def skill(name: str = "objective-planning", ua_tools: object | None = None) -> Skill:
    metadata = {} if ua_tools is None else {SKILL_TOOLS_METADATA_KEY: ua_tools}
    return Skill(name=name, description="Plans and tracks objectives.", instructions=INSTRUCTIONS, metadata=metadata)


def activate(found: Skill) -> str:
    """Call the plugin's skills tool as Strands does, with a real (never invoked) Strands agent as context."""

    plugin = PromptBoundedAgentSkills(skills=[found])
    sdk_agent = Agent(model=ScriptedStepsModel([]), callback_handler=None)
    return asyncio.run(plugin.skills(skill_name=found.name, tool_context=SimpleNamespace(agent=sdk_agent)))


def test_ua_tools_is_read_as_space_separated_ids_and_anything_else_is_ignored_with_a_warning(caplog) -> None:
    assert declared_skill_tool_ids({SKILL_TOOLS_METADATA_KEY: " fn:a  fn:b fn:a "}) == ["fn:a", "fn:b"]
    assert declared_skill_tool_ids({}) == [] and declared_skill_tool_ids(None) == []
    with caplog.at_level(logging.WARNING, logger="custom_agents.tool_loading"):
        assert declared_skill_tool_ids({SKILL_TOOLS_METADATA_KEY: ["fn:a"]}, skill_name="bad") == []
    assert "event=skill_tools_metadata_invalid skill_name=bad value_type=list" in caplog.text


def test_with_full_definitions_a_skill_loads_its_held_groups_for_the_task_and_logs_unknown_ids(storage, tmp_path, caplog) -> None:
    lead = lead_agent(storage, tmp_path)
    registry, hook = model_view(lead)
    before = next_model_call(hook, registry)
    assert "create_objective" not in before and "add_heartbeat_item" not in before
    version = owner_service(storage).store.version(TEAM)

    with caplog.at_level(logging.INFO):
        result = activate(skill(ua_tools=f"{OBJECTIVES} {HEARTBEAT} fn:tool_loading fn:no_such_group"))

    assert result.startswith(INSTRUCTIONS)
    assert f"- Loaded for this task: {OBJECTIVES}, {HEARTBEAT}." in result
    assert "- Already in your tool list: fn:tool_loading." in result
    assert "fn:no_such_group" not in result, "a wrong id is the skill's fault; it is logged, not shown"
    assert "event=skill_declared_tool_unknown" in caplog.text and "tool_ids=fn:no_such_group" in caplog.text
    assert loaded_tools(lead).loaded() == {OBJECTIVES, HEARTBEAT}
    after = next_model_call(hook, registry)
    assert {"create_objective", "add_heartbeat_item"} <= after, "loaded from the next model call"
    assert owner_service(storage).store.version(TEAM) == version, "loading a skill's tools gives no access"


@pytest.mark.parametrize("presentation", [TOOL_PRESENTATION_INDEX, TOOL_PRESENTATION_INDEX_AND_SCRIPTS])
def test_in_the_index_setups_a_skills_catalog_groups_are_not_loaded_but_called_with_call_tool(storage, tmp_path, presentation) -> None:
    # Lead ruling (turn 4): the index lists these functions and call_tool reaches them; full definitions would undo the saving.
    lead = lead_agent(storage, tmp_path)
    lead.tool_filter = {"tool_presentation": presentation}
    registry, hook = model_view(lead)
    before = next_model_call(hook, registry)

    result = activate(skill(ua_tools=f"{OBJECTIVES} {HEARTBEAT} fn:tool_loading"))

    assert f"- In your tool index, nothing to load: {OBJECTIVES}, {HEARTBEAT}." in result
    assert "call_tool(name='<function>', inputs={...})" in result
    assert "- Already in your tool list: fn:tool_loading." in result
    assert "Loaded for this task" not in result and "use_tools" not in result
    assert loaded_tools(lead).loaded() == frozenset()
    assert next_model_call(hook, registry) == before, "no definitions added"


def test_in_the_index_setups_a_declared_mcp_server_goes_through_the_use_tools_path(storage, tmp_path, monkeypatch) -> None:
    from custom_agents.access_runtime import runtime_access
    from mcp_control.registry import MCPConnectionStore, resolve_mcp_connection_paths
    from tests.test_mcp_server_loading import install

    lead = lead_agent(storage, tmp_path)
    lead.tool_filter = {"tool_presentation": TOOL_PRESENTATION_INDEX}
    model_view(lead)
    runtime_access(lead).catalog.add_mcp_tool(install(MCPConnectionStore(resolve_mcp_connection_paths(tmp_path))))
    handed: list[list[str]] = []
    real = tool_loading.load_tool_groups

    def spy(agent, catalog, tool_ids):
        handed.append(list(tool_ids))
        return real(agent, catalog, tool_ids)

    monkeypatch.setattr(tool_loading, "load_tool_groups", spy)

    result = activate(skill(ua_tools=f"mcp:tickets {OBJECTIVES}"))

    assert handed == [["mcp:tickets"]], "only the MCP server goes to the loader; the catalog group is in the index"
    assert f"- In your tool index, nothing to load: {OBJECTIVES}." in result
    assert "mcp:tickets" in result


def test_groups_unloaded_to_make_room_are_named_in_the_skill_result(storage, tmp_path) -> None:
    lead = lead_agent(storage, tmp_path)
    registry, hook = model_view(lead)
    shown = len(next_model_call(hook, registry))
    activate(skill(name="heartbeat-skill", ua_tools=HEARTBEAT))
    next_model_call(hook, registry)
    cap(lead, shown + len(functions_of_tools(CATALOG, {OBJECTIVES})))

    result = activate(skill(ua_tools=OBJECTIVES))

    assert f"- Loaded for this task: {OBJECTIVES}." in result
    assert f"- Unloaded to make room: {HEARTBEAT}. Call one of their functions to load it again." in result
    assert loaded_tools(lead).loaded() == {OBJECTIVES}


def test_a_group_the_agent_lacks_is_reported_with_the_request_access_call_and_not_loaded(storage, tmp_path) -> None:
    worker = make_agent(storage, tmp_path, agent_type="WebResearchAgent")
    worker.tools, worker.tool_filter, worker.default_tools = list(ALL_PROVIDER_TOOLS), {}, ()
    set_agent_ref(worker)
    registry, hook = model_view(worker)
    next_model_call(hook, registry)
    version = owner_service(storage).store.version(TEAM)

    result = activate(skill(ua_tools=OBJECTIVES))

    assert f"- Not loaded, {OBJECTIVES}: You have no access to {OBJECTIVES}" in result
    assert f"request_access(tool_id='{OBJECTIVES}'" in result
    assert loaded_tools(worker).loaded() == frozenset()
    assert "create_objective" not in next_model_call(hook, registry)
    assert owner_service(storage).store.version(TEAM) == version


def test_a_group_that_does_not_fit_under_the_cap_is_reported_and_the_rest_still_load(storage, tmp_path) -> None:
    lead = lead_agent(storage, tmp_path)
    registry, hook = model_view(lead)
    shown = len(next_model_call(hook, registry))
    heartbeat = len(functions_of_tools(CATALOG, {HEARTBEAT}))
    cap(lead, shown + heartbeat)

    result = activate(skill(ua_tools=f"{HEARTBEAT} {OBJECTIVES}"))

    assert f"- Loaded for this task: {HEARTBEAT}." in result
    assert f"- Not loaded, {OBJECTIVES}: Loading {OBJECTIVES} adds" in result and "takes at most" in result
    assert loaded_tools(lead).loaded() == {HEARTBEAT}


def test_a_skill_without_ua_tools_or_with_invalid_ua_tools_returns_only_its_instructions(storage, tmp_path) -> None:
    lead = lead_agent(storage, tmp_path)
    model_view(lead)

    assert activate(skill()) == INSTRUCTIONS
    assert activate(skill(name="bad-metadata", ua_tools=["fn:objectives"])) == INSTRUCTIONS
    assert activate(skill(name="only-unknown", ua_tools="fn:no_such_group")) == INSTRUCTIONS
    assert loaded_tools(lead).loaded() == frozenset()


def test_a_failure_while_loading_still_returns_the_instructions_with_the_use_tools_call(storage, tmp_path, monkeypatch, caplog) -> None:
    lead = lead_agent(storage, tmp_path)
    model_view(lead)

    def broken(*_args, **_kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr("custom_tools.providers.tool_loading.load_skill_tool_groups", broken)
    with caplog.at_level(logging.ERROR, logger="custom_agents.sdk.strands.skills"):
        result = activate(skill(ua_tools=OBJECTIVES))

    assert result.startswith(INSTRUCTIONS)
    assert f"use_tools(tool_ids=['{OBJECTIVES}'])" in result
    assert "event=skill_tool_groups_failed" in caplog.text

    # In the index setups the note sends catalog groups to call_tool and only MCP servers to use_tools.
    lead.tool_filter = {"tool_presentation": TOOL_PRESENTATION_INDEX}
    indexed = activate(skill(name="indexed", ua_tools=f"{OBJECTIVES} mcp:tickets"))
    assert f"Call the functions of {OBJECTIVES} from your tool index with call_tool(name='<function>', inputs={{...}})." in indexed
    assert "use_tools(tool_ids=['mcp:tickets'])" in indexed
    assert f"use_tools(tool_ids=['{OBJECTIVES}'" not in indexed


def test_a_skill_on_disk_declares_ua_tools_in_its_frontmatter_metadata(tmp_path: Path) -> None:
    folder = tmp_path / "planner"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: planner\ndescription: Plans work.\nmetadata:\n  ua-tools: fn:objectives fn:heartbeat\n---\n\nPlan.\n",
        encoding="utf-8",
    )

    loaded = Skill.from_file(folder / "SKILL.md")

    assert declared_skill_tool_ids(loaded.metadata) == [OBJECTIVES, HEARTBEAT]
