"""Tool presentation and the tool index (custom_agents.tool_presentation, custom_agents.tool_index).

Unit checks for the setting, the catalog's always-shown list, index lines,
signatures and which inputs a tool script may use; then real Cedar, SQLite and
a real Strands tool registry (the fixtures of tests/test_tool_loading.py): with
an index the team lead starts with its core only, the index lists the rest
and follows its grants, use_tools and calls by name still load groups,
describe_tools gives exact inputs, and a name that is no tool gets the closest
real names.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from access_catalog.catalog import load_catalog
from access_catalog.tool_functions import functions_of_tools
from custom_agents.tool_index import (
    ScriptInputs,
    MAX_INDEX_SENTENCE_CHARS,
    ToolIndexHook,
    first_sentence,
    index_text,
    script_inputs,
    signature,
)
from custom_agents.tool_loading import OnRequestToolHook, loaded_tools
from custom_agents.tool_presentation import on_request_groups, presentation
from custom_tools.provider_contract import tool_name
from custom_tools.providers import tool_loading
from custom_tools.registry import ALL_PROVIDER_TOOLS
from runtime_settings.agents import AgentRuntimeDefaultsSettings, AgentRuntimeOverrideSettings
from runtime_settings.tool_presentation_contract import (
    TOOL_PRESENTATION_INDEX,
    TOOL_PRESENTATION_INDEX_AND_SCRIPTS,
    TOOL_PRESENTATIONS,
)
from tests.task_work_support import main_parts
from tests.test_tool_loading import BINARY, lead_agent, model_view, next_model_call, use
from tests.test_tool_loading import storage as loading_storage

storage = loading_storage
CATALOG = load_catalog(None)
TOOLS = {tool_name(tool): tool for tool in ALL_PROVIDER_TOOLS}
needs_cedar = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


def an_agent(mode: str | None = None, **attributes):
    return SimpleNamespace(id="agent-1", named_agent_type="", default_tools=(), tool_filter={"tool_presentation": mode}, **attributes)


# -- the setting -------------------------------------------------------------------------------


@pytest.mark.parametrize("model", [AgentRuntimeDefaultsSettings, AgentRuntimeOverrideSettings])
def test_the_setting_accepts_the_known_presentations_and_refuses_others(model) -> None:
    for value in TOOL_PRESENTATIONS:
        assert model(tool_presentation=value).tool_presentation == value
    with pytest.raises(ValidationError, match="tool_presentation must be one of"):
        model(tool_presentation="tiny")
    assert AgentRuntimeDefaultsSettings().tool_presentation == "full_definitions"
    assert AgentRuntimeOverrideSettings().tool_presentation is None


def test_an_unknown_presentation_on_an_agent_is_logged_and_shows_tools_in_full(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert presentation(an_agent("tiny")) == "full_definitions"
    assert "event=tool_presentation_unknown" in caplog.text
    assert presentation(an_agent(None)) == "full_definitions"


# -- the catalog ------------------------------------------------------------------------------


def test_always_shown_names_function_tools_of_every_agent() -> None:
    assert CATALOG.always_shown, "the catalog names the core"
    with_functions = {binding.tool_id for binding in CATALOG.functions.values()}
    assert CATALOG.always_shown <= with_functions
    assert {"fn:tool_loading", "fn:communication", "fn:files"} <= CATALOG.always_shown


def test_with_an_index_every_function_tool_outside_the_core_loads_on_request() -> None:
    full = on_request_groups(an_agent("full_definitions"), CATALOG)
    index = on_request_groups(an_agent(TOOL_PRESENTATION_INDEX), CATALOG)

    assert full == CATALOG.on_request
    with_functions = {binding.tool_id for binding in CATALOG.functions.values()}
    assert set(index) == with_functions - CATALOG.always_shown
    for tool_id, kept in CATALOG.on_request.items():
        assert index[tool_id] == kept, "kept-shown functions stay shown with an index"
    assert on_request_groups(an_agent(TOOL_PRESENTATION_INDEX), CATALOG) is index, "built once per catalog"


def test_every_offered_function_has_a_short_first_sentence_for_its_index_line() -> None:
    too_long = {
        name: len(first_sentence(tool.tool_spec.get("description", "")))
        for name, tool in TOOLS.items()
        if not 0 < len(first_sentence(tool.tool_spec.get("description", ""))) <= MAX_INDEX_SENTENCE_CHARS
    }
    assert not too_long, f"shorten the first sentence of: {too_long}"


def test_first_sentence_stops_at_the_first_sentence_end() -> None:
    assert first_sentence("Read a file.  Then more.\n\nDetails.") == "Read a file."
    assert first_sentence("No sentence end here\nstill going\n\nNext paragraph") == "No sentence end here still going"


# -- tool scripts' inputs and signatures ------------------------------------------------------


def test_a_script_may_call_only_operations_the_catalog_rates_read() -> None:
    assert script_inputs(CATALOG, "knowledge_search") == ScriptInputs()
    assert script_inputs(CATALOG, "knowledge_write") is None, "write level"
    memory = script_inputs(CATALOG, "memory")
    assert memory.argument == "action" and "list" in memory.values and "create" not in memory.values, "only read values"
    assert memory.allows({"action": "list"}) and not memory.allows({"action": "create"})
    assert script_inputs(CATALOG, "secure_cli") is None, "dispatchers are not catalog functions"
    assert script_inputs(CATALOG, "find_tools") is None and script_inputs(CATALOG, "use_tools") is None
    assert script_inputs(CATALOG, "load_local_media_for_model_context") is None


def test_a_signature_lists_required_inputs_first_with_types() -> None:
    fake = SimpleNamespace(
        tool_spec={
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 10},
                        "query": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "mode": {"enum": ["a", "b"]},
                        "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "required": ["query"],
                }
            }
        }
    )
    assert signature("lookup", fake) == (
        "lookup(query: str, limit: int = 10, tags: list[str] = None, mode: Literal['a', 'b'] = None, maybe: str | None = None)"
    )
    assert signature("knowledge_search", TOOLS["knowledge_search"]).startswith("knowledge_search(query: str")


def test_compact_signature_inlines_short_enums_and_caps_optional_args() -> None:
    fake = SimpleNamespace(
        tool_spec={
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "create", "delete"]},
                        "big_enum": {"type": "string", "enum": ["a", "b", "c", "d", "e", "f", "g"]},
                        "urls": {"type": "array", "items": {"type": "string"}},
                        "proposal": {"type": "object"},
                        "opt1": {"type": "string"},
                        "opt2": {"type": "integer"},
                        "opt3": {"type": "boolean"},
                        "opt4": {"type": "string"},
                        "opt5": {"type": "string"},
                        "opt6": {"type": "string"},
                    },
                    "required": ["action", "big_enum", "urls", "proposal"],
                }
            }
        }
    )
    assert signature("demo", fake, compact=True) == "demo(action=list|create|delete, big_enum, urls[], proposal{}, opt1?, opt2?, opt3?, opt4?, +2?)"
    no_args = SimpleNamespace(tool_spec={"inputSchema": {"json": {"type": "object", "properties": {}}}})
    assert signature("ping", no_args, compact=True) == "ping()"
    assert signature("exa_get_contents", TOOLS["exa_get_contents"], compact=True).startswith("exa_get_contents(urls[],")
    assert signature("memory", TOOLS["memory"], compact=True).startswith(
        "memory(action=list|create|replace|delete|undo, bank=coremem|usermem,"
    )


# -- the index text -----------------------------------------------------------------------------


def test_the_index_groups_functions_by_the_group_use_tools_loads() -> None:
    text = index_text(CATALOG, {"knowledge_search", "knowledge_write", "memory"}, TOOLS, scripts=False)
    lines = text.splitlines()
    assert lines[0] == "Your other tools:"
    assert "call_tool(name='<function>', inputs={...})" in lines[1]
    assert "already in your tool list are called directly" in lines[1]
    assert "fn:knowledge (Knowledge base)" in lines and "fn:memory (Team and personal memory)" in lines
    assert any(line.startswith("- knowledge_search(query?,") and "Search the shared knowledge base" in line for line in lines)
    assert any(line.startswith("- memory(action=list|create|replace|delete|undo, bank=coremem|usermem,") for line in lines)
    assert "script" not in text.split("\n", 2)[2], "no script marks without tool scripts"
    assert index_text(CATALOG, set(), TOOLS, scripts=False) == ""


def test_with_scripts_the_index_marks_what_a_script_may_call() -> None:
    text = index_text(CATALOG, {"knowledge_search", "knowledge_write", "memory"}, TOOLS, scripts=True)
    assert any(line.startswith("- knowledge_search(") and ") [script]:" in line for line in text.splitlines())
    assert any(line.startswith("- knowledge_write(") and "[script" not in line for line in text.splitlines())
    assert any(line.startswith("- memory(") and "[script: action=" in line for line in text.splitlines())
    assert "run_tool_script" in text


# -- a real team lead with an index ----------------------------------------------------------------


def index_lead(storage, tmp_path, mode: str = TOOL_PRESENTATION_INDEX, *, enabled: bool = True):
    agent = lead_agent(storage, tmp_path, enabled=enabled)
    agent.tool_filter = {"tool_presentation": mode}
    return agent


def prompt_after_call(agent, registry, hook) -> str:
    """Run the tool-list sync and the index hook as before a model call; the Strands agent's system prompt."""

    strands = SimpleNamespace(tool_registry=registry, system_prompt="BASE")
    next_model_call(hook, registry)
    ToolIndexHook(agent, main_parts(agent), "BASE").before_model_call(SimpleNamespace(agent=strands))
    return strands.system_prompt


@needs_cedar
def test_with_an_index_the_team_lead_starts_with_its_core_and_the_index_lists_the_rest(storage, tmp_path) -> None:
    lead = index_lead(storage, tmp_path)
    registry, hook = model_view(lead)

    shown = next_model_call(hook, registry)
    prompt = prompt_after_call(lead, registry, hook)

    core = functions_of_tools(CATALOG, CATALOG.always_shown)
    assert {"notify_user", "file_read", "find_tools", "describe_tools", "use_tools", "call_tool", "request_access"} <= shown
    assert "knowledge_search" not in shown and "memory" not in shown, "outside the core"
    assert "plan_runtime_setting_change" in shown, "kept-shown functions stay shown"
    assert prompt.startswith("BASE\n\nYour other tools:")
    assert "- knowledge_search(" in prompt and "- memory(" in prompt
    assert not any(f"- {name}(" in prompt for name in core), "the core is not repeated in the index"


@needs_cedar
def test_loading_a_group_shows_it_without_changing_the_index(storage, tmp_path) -> None:
    lead = index_lead(storage, tmp_path)
    registry, hook = model_view(lead)
    before = prompt_after_call(lead, registry, hook)

    [loaded] = use("fn:knowledge")["tools"]
    after = prompt_after_call(lead, registry, hook)

    assert loaded["status"] == "loaded" and "knowledge_search" in loaded["functions"]
    assert "knowledge_search" in next_model_call(hook, registry)
    assert after == before, "the index lists what the agent holds, loaded or not"


@needs_cedar
def test_a_call_by_name_from_the_index_loads_its_group(storage, tmp_path) -> None:
    lead = index_lead(storage, tmp_path)
    registry, hook = model_view(lead)
    next_model_call(hook, registry)
    event = SimpleNamespace(tool_use={"name": "knowledge_search"}, selected_tool=None)

    OnRequestToolHook(lead, main_parts(lead)).before_tool_call(event)

    assert tool_name(event.selected_tool) == "knowledge_search"
    assert "fn:knowledge" in loaded_tools(lead).loaded()


@needs_cedar
def test_a_name_that_is_no_tool_gets_the_closest_names_and_a_real_tool_keeps_strands_answer(storage, tmp_path) -> None:
    lead = index_lead(storage, tmp_path)
    registry, hook = model_view(lead)
    next_model_call(hook, registry)
    hook_under_test = OnRequestToolHook(lead, main_parts(lead))
    strands = SimpleNamespace(tool_registry=registry)

    typo = SimpleNamespace(tool_use={"name": "knowledge_serch"}, selected_tool=None, cancel_tool=False, agent=strands)
    hook_under_test.before_tool_call(typo)
    real = SimpleNamespace(tool_use={"name": "secure_cli"}, selected_tool=None, cancel_tool=False, agent=strands)
    hook_under_test.before_tool_call(real)

    assert "No tool is named 'knowledge_serch'" in typo.cancel_tool and "knowledge_search" in typo.cancel_tool
    assert real.cancel_tool is False, "a real tool the agent lacks is explained by the access hook and find_tools"


@needs_cedar
def test_describe_tools_gives_exact_inputs_and_whether_the_agent_holds_them(storage, tmp_path) -> None:
    lead = index_lead(storage, tmp_path, TOOL_PRESENTATION_INDEX_AND_SCRIPTS)
    registry, hook = model_view(lead)
    next_model_call(hook, registry)

    answer = json.loads(tool_loading.describe_tools(names=["knowledge_search", "knowledge_write", "knowlege_search"]))
    search, write, typo = answer["tools"]

    assert search["signature"].startswith("knowledge_search(query: str") and search["group"] == "fn:knowledge"
    assert search["you_hold_it"] is True and search["tool_script"] == "script"
    assert write["tool_script"] == "no: call it directly"
    assert typo["error"] and "knowledge_search" in typo["closest"]


@needs_cedar
def test_with_full_definitions_nothing_changes(storage, tmp_path) -> None:
    lead = index_lead(storage, tmp_path, "full_definitions")
    registry, hook = model_view(lead)

    shown = next_model_call(hook, registry)

    assert "knowledge_search" in shown and "memory" in shown
    assert "call_tool" not in shown, "call_tool is withheld in full_definitions mode"
    assert "objectives" not in shown, "the catalog's on_request groups still wait"

    # C11: find_tools and describe_tools leave out functions this setting withholds.
    found = json.loads(tool_loading.find_tools(query="find_tools"))["tools"]
    [loading_group] = [item for item in found if item["tool_id"] == "fn:tool_loading"]
    assert loading_group["in_your_tool_list"] is True
    assert loading_group["functions"] == ["describe_tools", "find_tools", "use_tools"]

    described = json.loads(tool_loading.describe_tools(names=["call_tool", "run_tool_script"]))["tools"]
    assert [entry["name"] for entry in described] == ["call_tool", "run_tool_script"]
    for entry in described:
        assert entry["error"] == tool_loading.WITHHELD_FUNCTION_ERROR
        assert "you_hold_it" not in entry and "signature" not in entry

    # C16: a group whose every function this setting withholds is not offered at all.
    for query in ("run_tool_script", "tool scripts", "run a script that calls several tools"):
        found = json.loads(tool_loading.find_tools(query=query))["tools"]
        assert "fn:tool_scripts" not in [item["tool_id"] for item in found], query


@needs_cedar
def test_with_full_definitions_and_authorization_off_find_tools_leaves_out_call_tool(storage, tmp_path) -> None:
    lead = index_lead(storage, tmp_path, "full_definitions", enabled=False)
    registry, hook = model_view(lead)
    next_model_call(hook, registry)

    found = json.loads(tool_loading.find_tools(query="find_tools"))["tools"]

    [loading_group] = [item for item in found if item["tool_id"] == "fn:tool_loading"]
    assert loading_group["in_your_tool_list"] is True
    assert "call_tool" not in loading_group["functions"]
    scripts_found = json.loads(tool_loading.find_tools(query="run_tool_script"))["tools"]
    assert "fn:tool_scripts" not in [item["tool_id"] for item in scripts_found]


@needs_cedar
def test_a_call_by_name_gets_a_standard_function_the_definition_does_not_list(storage, tmp_path) -> None:
    # Live (2026-11-26): with an index, a call to get_self_status by name answered "Unknown tool",
    # because only the definition's tools were searched and the role's standard functions are not there.
    lead = index_lead(storage, tmp_path)
    lead.tools = [tool for tool in lead.tools if tool_name(tool) != "get_self_status"]
    registry, hook = model_view(lead)
    next_model_call(hook, registry)
    event = SimpleNamespace(tool_use={"name": "get_self_status"}, selected_tool=None)

    OnRequestToolHook(lead, main_parts(lead)).before_tool_call(event)

    assert tool_name(event.selected_tool) == "get_self_status"
