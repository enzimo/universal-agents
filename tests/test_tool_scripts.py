"""Tool scripts (custom_agents.tool_script_runtime, custom_tools.tool_scripts).

- Which calls a script may make, checked before anything runs.
- The real Landlock sandbox: a script reads no instance or person files,
  opens no network connection, stops at its time limit, and hands back what
  it printed.
- A real TeamArchitect built from its definition, with a scripted model: one
  run_tool_script call chains two read-level tools that wait in the index,
  a write-level call is refused inside the script, the access hook still sees
  every call, nothing is loaded into the model's tool list, and the model's
  next request carries only the script's result.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from access_catalog.catalog import load_catalog
from custom_agents import access_runtime
from custom_agents.tool_script_runtime import ScriptLimits, ScriptRefused, check_call, run_script, sandbox_refusal
from runtime_settings.tool_presentation_contract import TOOL_PRESENTATION_INDEX, TOOL_PRESENTATION_INDEX_AND_SCRIPTS
from tests.task_work_support import set_agent_ref

CATALOG = load_catalog(None)
needs_sandbox = pytest.mark.skipif(sandbox_refusal() is not None, reason="needs Landlock with network control (ABI 4)")


def scripts_agent(**tool_filter):
    return SimpleNamespace(id="agent-1", named_agent_type="", default_tools=(), tool_filter={"tool_presentation": TOOL_PRESENTATION_INDEX_AND_SCRIPTS, **tool_filter})


# -- which calls a script may make ------------------------------------------------------------


HELD = {"knowledge_search", "knowledge_write", "memory", "get_self_status", "secure_cli", "find_tools"}


@pytest.mark.parametrize(
    ("name", "inputs", "expected"),
    [
        ("knowledge_search", {"query": "x"}, None),
        ("memory", {"action": "list", "bank": "coremem"}, None),
        ("memory", {"action": "store", "bank": "coremem"}, "runs only with action set to one of"),
        ("knowledge_write", {"title": "x"}, "changes things"),
        ("secure_cli", {}, "changes things or cannot run from a script"),
        ("find_tools", {"query": "x"}, "cannot run from a script"),
        ("calculator", {"expression": "1+1"}, "You do not hold calculator"),
        ("knowledge_serch", {}, "closest names are: knowledge_search"),
        ("knowledge_search", ["x"], "inputs must be given by name"),
        ("", {}, "needs a function name"),
    ],
)
def test_a_script_may_call_only_held_read_level_operations(name, inputs, expected) -> None:
    refusal = check_call(scripts_agent(), CATALOG, name, inputs, HELD)
    if expected is None:
        assert refusal is None
    else:
        assert expected in refusal


def test_the_operators_block_list_applies_to_scripts_too() -> None:
    agent = scripts_agent(tool_blocklist=["knowledge_search"])
    assert "do not offer knowledge_search" in check_call(agent, CATALOG, "knowledge_search", {"query": "x"}, HELD)


def test_scripts_are_refused_when_the_setting_is_not_on() -> None:
    agent = SimpleNamespace(id="a", tool_filter={"tool_presentation": TOOL_PRESENTATION_INDEX})
    with pytest.raises(ScriptRefused, match="not on for this team"):
        asyncio.run(run_script(agent, None, {}, "return 1"))


def test_scripts_are_refused_without_a_sandbox_that_closes_the_network(monkeypatch) -> None:
    from custom_tools import code_sandbox

    monkeypatch.setattr(
        code_sandbox, "code_sandbox_status", lambda env=None: SimpleNamespace(mode="off", available=False, landlock_abi=0)
    )
    with pytest.raises(ScriptRefused, match="operator turned it off"):
        asyncio.run(run_script(scripts_agent(), None, {}, "return 1"))


def test_strands_executor_stream_keeps_the_signature_scripts_rely_on() -> None:
    from strands.tools.executors._executor import ToolExecutor

    parameters = list(inspect.signature(ToolExecutor._stream).parameters)
    assert parameters[:4] == ["agent", "tool_use", "tool_results", "invocation_state"]


# -- the real sandbox ---------------------------------------------------------------------------


def sandboxed(code: str, monkeypatch, **limits) -> dict:
    """Run a script with no tool calls in the real sandbox; the payload the model would read."""

    from custom_agents import tool_script_runtime  # noqa: F401 - patched names below live there

    monkeypatch.setattr("custom_agents.access_runtime.runtime_access", lambda agent: SimpleNamespace(catalog=CATALOG))
    monkeypatch.setattr("custom_agents.tool_index.held_functions", lambda agent, parts: set())
    return asyncio.run(run_script(scripts_agent(), None, {}, code, limits=ScriptLimits(**limits))).payload()


@needs_sandbox
def test_a_script_returns_its_value_and_what_it_printed(monkeypatch) -> None:
    payload = sandboxed('print("looking")\ntext = """a\n\n  b"""\nreturn {"n": 2 + 2, "text": text}', monkeypatch)
    assert payload["status"] == "success" and payload["result"] == {"n": 4, "text": "a\n\n  b"}
    assert payload["printed"] == "looking" and payload["calls"] == []


@needs_sandbox
def test_a_script_that_closes_stdout_still_returns_its_result_or_its_error(monkeypatch) -> None:
    ok_payload = sandboxed("import sys\nsys.stdout.close()\nreturn 42", monkeypatch)
    assert ok_payload["status"] == "success" and ok_payload["result"] == 42

    err_payload = sandboxed('import sys\nsys.stdout.close()\nraise ValueError("after close")', monkeypatch)
    assert err_payload["status"] == "error" and "after close" in err_payload["error"]


@needs_sandbox
def test_a_script_reads_no_instance_or_person_files(tmp_path: Path, monkeypatch) -> None:
    secret = tmp_path / "workspaces" / "users" / "alice" / "notes.txt"
    secret.parent.mkdir(parents=True)
    secret.write_text("private", encoding="utf-8")
    code = f"""
try:
    open({str(secret)!r}).read()
    return "read it"
except PermissionError:
    return "denied"
"""
    assert sandboxed(code, monkeypatch)["result"] == "denied"


@needs_sandbox
def test_a_script_opens_no_network_connection(monkeypatch) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    code = f"""
import socket
try:
    socket.create_connection(("127.0.0.1", {port}), timeout=2)
    return "connected"
except OSError as exc:
    return "refused"
"""
    try:
        assert sandboxed(code, monkeypatch)["result"] == "refused"
    finally:
        listener.close()


@needs_sandbox
def test_a_script_stops_at_its_time_limit(monkeypatch) -> None:
    payload = sandboxed("while True:\n    pass", monkeypatch, wall_seconds=1.0)
    assert payload["status"] == "error" and "longer than 1 seconds" in payload["stopped"]


@needs_sandbox
def test_a_script_error_names_the_script_line(monkeypatch) -> None:
    payload = sandboxed("x = 1\ny = 2\nraise ValueError('bad input')", monkeypatch)
    assert payload["status"] == "error" and "script line 3" in payload["error"] and "bad input" in payload["error"]


# -- a real team lead -------------------------------------------------------------------------------


SCRIPT = """
status = tools.get_self_status()
config = tools.get_runtime_configuration_context(include_config_file_snapshot=False)
total = tools.calculator(expression="17*3")
try:
    tools.knowledge_write(title="x", content="y")
    wrote = "wrote"
except ToolError as exc:
    wrote = str(exc)
print("checked")
return {"status_kind": type(status).__name__, "config_kind": type(config).__name__, "total": total, "write": wrote}
"""


@needs_sandbox
def test_a_real_team_lead_chains_index_tools_in_one_script_without_loading_them(tmp_path, monkeypatch) -> None:
    from strands.hooks import BeforeToolCallEvent
    from strands.models import Model

    from custom_agents.strands_team import AGENT_TYPE_MAP, create_strands_agent
    from custom_agents.tool_loading import loaded_tools

    seen: list[list[str]] = []
    results: list[dict] = []
    decided: list[str] = []

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
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1", "name": "run_tool_script"}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps({"code": SCRIPT})}}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}
                return
            last = messages[-1]["content"][0].get("toolResult", {})
            results.append(json.loads(last["content"][0]["text"]))
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockDelta": {"delta": {"text": "OK"}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}

    original = access_runtime.AccessDecisionHook.before_tool_call

    async def watched(self, event: BeforeToolCallEvent) -> None:
        decided.append(str(event.tool_use.get("name")))
        await original(self, event)

    monkeypatch.setattr(access_runtime.AccessDecisionHook, "before_tool_call", watched)
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(tmp_path / "instance"))
    config = dict(AGENT_TYPE_MAP["TeamArchitect"])
    config.update({
        "team_id": "team_real", "is_team_lead": True, "named_agent_type": "TeamArchitect",
        "authorization_config": {"enabled": False},
        "task_history_db_path": str(tmp_path / "t.db"), "storage_sqlite_path": str(tmp_path / "t.db"),
        "instance_root": str(tmp_path / "instance"),
        "tool_presentation": TOOL_PRESENTATION_INDEX_AND_SCRIPTS,
    })
    with patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=Scripted()):
        agent = create_strands_agent(config)
    set_agent_ref(agent)
    inner = agent._strands_agent

    inner("How am I doing, and what is 17 times 3?")

    first, second = seen
    [payload] = results
    assert "run_tool_script" in first and "describe_tools" in first
    assert "get_self_status" not in first and "calculator" not in first, "both wait in the index"
    prompt_lines = inner.system_prompt.splitlines()
    assert any(line.startswith("- get_self_status(") and ") [script]:" in line for line in prompt_lines)
    assert any(line.startswith("- calculator(expression,") and ") [script]:" in line for line in prompt_lines)
    assert payload["status"] == "success", payload
    assert payload["result"]["total"] and payload["result"]["status_kind"] in {"dict", "str", "list"}
    assert "changes things" in payload["result"]["write"]
    assert payload["printed"] == "checked"
    assert [call["tool"] for call in payload["calls"]] == [
        "get_self_status", "get_runtime_configuration_context", "calculator", "knowledge_write"
    ]
    assert all(call["ok"] for call in payload["calls"][:3]), payload["calls"]
    assert payload["calls"][3]["refused"] is True
    assert {"run_tool_script", "get_self_status", "calculator"} <= set(decided), "the access hook saw every call"
    assert "knowledge_write" not in decided, "refused before it reached any hook"
    assert "get_self_status" not in second and "calculator" not in second, "a script loads nothing"
    assert loaded_tools(agent).loaded() == frozenset()
    access_runtime._runtime_access.cache_clear()


def test_notes_hooks_add_to_a_result_reach_the_model_and_the_script_keeps_the_answer() -> None:
    from custom_agents.tool_script_runtime import _result_value

    value, notes = _result_value({"content": [{"text": '{"rows": [1, 2]}'}, {"text": "Tool call budget pressure is high."}]})

    assert value == {"rows": [1, 2]} and notes == ["Tool call budget pressure is high."]
    assert _result_value({"content": [{"text": "plain"}]}) == ("plain", [])


@pytest.mark.parametrize("name", ["delegate_task", "task_response"])
def test_a_turn_action_called_as_a_tool_keeps_the_guardrails_explanation(name) -> None:
    from custom_agents.runtime_guardrails import TURN_ACTION_TYPES
    from custom_agents.tool_loading import OnRequestToolHook

    assert name in TURN_ACTION_TYPES
    event = SimpleNamespace(tool_use={"name": name}, selected_tool=None, cancel_tool=False, agent=SimpleNamespace(tool_registry=None))
    OnRequestToolHook(SimpleNamespace(id="a"), None)._no_such_tool(event, name)
    assert event.cancel_tool is False
