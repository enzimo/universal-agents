"""Record changes run first in a model step; a failed one skips the step's later calls.

Real pieces: Strands agents on the locked SDK version, with the executor
(``custom_agents.sdk.strands.changes_first``), Strands' own interrupt, resume
and structured-output handling, and a hook that denies a call the way the
access hook does. Only the model is scripted. These tests also guard the
private SDK names the executor uses.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import BaseModel
from strands import Agent, tool
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from strands.interrupt import _InterruptState
from strands.models.model import Model
from strands.tools.executors import ConcurrentToolExecutor
from strands.tools.executors._executor import ToolExecutor
from strands.types.tools import ToolContext

from custom_agents.default_tools import builtin_tool_catalog
from custom_agents.sdk.strands.changes_first import NOT_RUN_TEXT, ChangesFirstToolExecutor
from custom_messages.enums import ChangeRecordKind

CALLS: list[str] = []


@tool
def read_note() -> str:
    """Read a note."""
    CALLS.append("read_note")
    return "note"


@tool
def change_a(outcome: str) -> Any:
    """A record change that succeeds, is refused, or fails."""
    CALLS.append(f"change_a:{outcome}")
    if outcome == "refused":
        return {"status": "error", "content": [{"text": json.dumps({"ok": False, "error_code": "refused"})}]}
    if outcome == "raises":
        raise RuntimeError("store unavailable")
    return "saved"


@tool
def change_b() -> str:
    """A second record change."""
    CALLS.append("change_b")
    return "saved"


@tool(context=True)
def change_after_approval(tool_context: ToolContext) -> str:
    """A record change that waits for a person's decision first."""
    CALLS.append("change_after_approval:start")
    answer = tool_context.interrupt("test-approval", reason={"why": "approval"})
    CALLS.append(f"change_after_approval:{answer}")
    return "saved after approval"


TOOLS = [read_note, change_a, change_b, change_after_approval]
CHANGES = {"change_a", "change_b", "change_after_approval"}


def record_kind_of(name: str, _arguments: Mapping[str, Any]) -> ChangeRecordKind | None:
    return ChangeRecordKind.MEMORY_FACT if name in CHANGES else None


class TurnDecision(BaseModel):
    done: bool


class ScriptedModel(Model):
    """Asks for the given calls in one step; later steps answer, or give a TurnDecision when asked."""

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls
        self.steps = 0

    def update_config(self, **config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted"}

    async def structured_output(self, *args: Any, **kwargs: Any):
        raise AssertionError("No structured output call expected")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.steps += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.steps == 1:
            for index, (name, arguments) in enumerate(self.calls):
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"call-{index}", "name": name}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(arguments)}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        if any(spec["name"] == "TurnDecision" for spec in tool_specs or ()):
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"decision-{self.steps}", "name": "TurnDecision"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps({"done": True})}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "done"}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


class DenyCall(HookProvider):
    """Refuses one tool before it runs, as the access hook refuses a call."""

    def __init__(self, name: str) -> None:
        self.name = name

    def register_hooks(self, registry: HookRegistry, **_kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before)

    def _before(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] == self.name:
            event.cancel_tool = "Access denied."


def make_agent(calls: list[tuple[str, dict[str, Any]]], hooks: list[HookProvider] | None = None) -> Agent:
    CALLS.clear()
    return Agent(
        model=ScriptedModel(calls),
        tools=TOOLS,
        callback_handler=None,
        hooks=hooks or [],
        tool_executor=ChangesFirstToolExecutor(record_kind_of),
    )


def step_results(agent: Agent) -> list[dict[str, Any]]:
    """The first step's tool results, in the order the model sees them."""

    for message in agent.messages:
        results = [block["toolResult"] for block in message["content"] if "toolResult" in block]
        if results:
            return results
    raise AssertionError("No tool results")


def not_run(result: Mapping[str, Any]) -> bool:
    return result["status"] == "error" and result["content"][0]["text"] == NOT_RUN_TEXT


def test_changes_run_first_one_at_a_time_and_results_keep_call_order() -> None:
    agent = make_agent([("read_note", {}), ("change_a", {"outcome": "ok"}), ("change_b", {})])

    asyncio.run(agent.invoke_async("go"))

    assert CALLS == ["change_a:ok", "change_b", "read_note"]
    assert [result["toolUseId"] for result in step_results(agent)] == ["call-0", "call-1", "call-2"]
    assert all(result["status"] == "success" for result in step_results(agent))


@pytest.mark.parametrize("outcome", ["refused", "raises"])
def test_a_failed_change_skips_every_later_call_of_the_step(outcome: str, caplog: pytest.LogCaptureFixture) -> None:
    agent = make_agent(
        [("read_note", {}), ("change_a", {"outcome": outcome}), ("change_b", {}), ("read_note", {})]
    )

    with caplog.at_level("INFO", logger="custom_agents.sdk.strands.changes_first"):
        asyncio.run(agent.invoke_async("go"))

    assert CALLS == [f"change_a:{outcome}"]
    results = step_results(agent)
    assert [result["toolUseId"] for result in results] == ["call-0", "call-1", "call-2", "call-3"]
    assert results[1]["status"] == "error" and not not_run(results[1])
    assert [not_run(results[index]) for index in (0, 2, 3)] == [True, True, True]
    assert "event=changes_first_step_stopped" in caplog.text
    assert "outcome=failed" in caplog.text


def test_an_access_denial_of_a_change_skips_the_rest_of_the_step() -> None:
    agent = make_agent([("change_a", {"outcome": "ok"}), ("read_note", {})], hooks=[DenyCall("change_a")])

    asyncio.run(agent.invoke_async("go"))

    assert CALLS == []
    assert not_run(step_results(agent)[1])


def test_a_denied_other_call_does_not_stop_the_changes() -> None:
    agent = make_agent([("read_note", {}), ("change_a", {"outcome": "ok"})], hooks=[DenyCall("read_note")])

    asyncio.run(agent.invoke_async("go"))

    assert CALLS == ["change_a:ok"]
    results = step_results(agent)
    assert results[1]["status"] == "success"
    assert results[0]["status"] == "error" and not not_run(results[0])


def test_skipped_calls_never_run_after_a_paused_change_resumes() -> None:
    agent = make_agent([("read_note", {}), ("change_after_approval", {}), ("change_b", {})])

    paused = asyncio.run(agent.invoke_async("go"))

    assert paused.stop_reason == "interrupt"
    assert CALLS == ["change_after_approval:start"]
    pending = agent._interrupt_state.pending_tool_execution
    assert {result["toolUseId"] for result in pending.completed_tool_results} == {"call-0", "call-2"}
    # A session manager stores the pause and restores it before the resume.
    agent._interrupt_state = _InterruptState.from_dict(json.loads(json.dumps(agent._interrupt_state.to_dict())))

    interrupt = paused.interrupts[0]
    asyncio.run(agent.invoke_async([{"interruptResponse": {"interruptId": interrupt.id, "response": "approved"}}]))

    # Strands starts the paused call again; the skipped calls never run.
    assert CALLS == [
        "change_after_approval:start",
        "change_after_approval:start",
        "change_after_approval:approved",
    ]
    results = {result["toolUseId"]: result for result in step_results(agent)}
    assert not_run(results["call-0"]) and not_run(results["call-2"])
    assert results["call-1"]["status"] == "success"


def test_a_turn_decision_skipped_with_the_step_is_asked_for_again() -> None:
    agent = make_agent([("change_a", {"outcome": "refused"}), ("TurnDecision", {"done": True})])

    result = asyncio.run(agent.invoke_async("go", structured_output_model=TurnDecision))

    assert CALLS == ["change_a:refused"]
    assert not_run(step_results(agent)[1])
    assert result.structured_output == TurnDecision(done=True)


def test_a_step_without_changes_runs_its_calls_as_before() -> None:
    agent = make_agent([("read_note", {}), ("read_note", {})])

    asyncio.run(agent.invoke_async("go"))

    assert CALLS == ["read_note", "read_note"]
    assert all(result["status"] == "success" for result in step_results(agent))


def test_the_private_sdk_names_the_executor_uses_are_unchanged() -> None:
    """The executor overrides ``_execute`` and calls ``_stream_with_trace`` (Strands' own executors do too)."""

    execute = inspect.signature(ConcurrentToolExecutor._execute)
    assert list(execute.parameters) == [
        "self",
        "agent",
        "tool_uses",
        "tool_results",
        "cycle_trace",
        "cycle_span",
        "invocation_state",
        "structured_output_context",
    ]
    stream = inspect.signature(ToolExecutor._stream_with_trace)
    assert list(stream.parameters)[:7] == [
        "agent",
        "tool_use",
        "tool_results",
        "cycle_trace",
        "cycle_span",
        "invocation_state",
        "structured_output_context",
    ]
    assert "tool_executor" in inspect.signature(Agent).parameters


@pytest.mark.parametrize(
    ("function", "arguments", "kind"),
    [
        ("memory", {"action": "replace"}, ChangeRecordKind.MEMORY_FACT),
        ("memory", {"action": "list"}, None),
        ("topic_tags", {"action": "tag_thread"}, None),
        ("knowledge_write", {}, ChangeRecordKind.KNOWLEDGE_PAGE),
        ("knowledge_read", {}, None),
        ("update_scheduled_job", {}, ChangeRecordKind.SCHEDULE),
        ("pause_scheduled_job", {}, ChangeRecordKind.SCHEDULE),
        ("authorize_scheduled_job_access", {}, None),
        ("update_heartbeat_item", {}, ChangeRecordKind.HEARTBEAT_ITEM),
        ("objectives", {"action": "update_status"}, ChangeRecordKind.OBJECTIVE),
        ("objectives", {"action": "get"}, None),
        ("objective_work_profiles", {"action": "apply"}, ChangeRecordKind.OBJECTIVE),
        ("objective_work_profiles", {"action": "export"}, None),
        ("start_isolated_recovery", {}, None),
        ("file_write", {}, ChangeRecordKind.FILE),
        ("file_read", {}, None),
        ("python_env", {"action": "write_file"}, ChangeRecordKind.FILE),
        ("python_env", {"action": "run"}, None),
        ("shell", {}, None),
    ],
)
def test_the_shipped_catalog_names_which_calls_change_records(
    function: str, arguments: dict[str, Any], kind: ChangeRecordKind | None
) -> None:
    assert builtin_tool_catalog().record_kind(function, arguments) is kind
