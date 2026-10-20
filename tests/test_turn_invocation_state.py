"""A turn calls Strands only with the keywords ``Agent.invoke_async`` declares.

Strands treats any other keyword as deprecated: it warns
("`**kwargs` parameter is deprecating, use `invocation_state` instead") and
hands tools and hooks ``{**kwargs, "invocation_state": <the turn's dict>}``,
with the turn's values one level down. Turns used to pass their callback
handler that way. Now the turn's handler is the Strands agent's own callback
handler for the length of each call, and each call gets its own copy of the
turn's ``invocation_state`` (``custom_agents.sdk.strands.turn_tool_stop``).

Real pieces: a Strands agent, the production turn executor
(``LLMTurnExecutor.process_llm_interaction``), the production entry point
``invoke_with_tool_stop`` and the real guardrails and context plugins. Only
the model is scripted; nothing calls a provider.

The retry tests pin where each plugin keeps ``request_state`` across a turn's
retries: Strands' state is fresh for every call, as it always was, and the
turn's own count of fresh reinvocations after a provider overflow survives
between them.
"""

from __future__ import annotations

import json
import logging
import warnings
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from strands import Agent, tool
from strands.handlers.callback_handler import null_callback_handler
from strands.hooks import AfterModelCallEvent, BeforeInvocationEvent, BeforeToolCallEvent, HookProvider, HookRegistry
from strands.models.model import Model
from strands.types.exceptions import ContextWindowOverflowException, StructuredOutputException
from strands.types.tools import ToolContext

from custom_agents import turn_action_outbox_runtime
from custom_agents.context_debug_dump import ContextDebugDumpPlugin
from custom_agents.runtime_guardrails import RuntimeGuardrailsPlugin
from custom_agents.sdk.strands.turn_tool_stop import invoke_with_tool_stop
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.tool_stop import TOOL_STOP_STATE_KEY, ToolStop, tool_stop_for
from tests.test_filtered_tool_result_context import (
    _AlwaysOverflowModel,
    _ProgressiveTestConversationManager,
    _storage,
)
from tests.test_runtime_owner_boundaries import build_turn_owner
from tests.turn_tool_stop_support import turn_invoke_kwargs

LOGGER = logging.getLogger("tests.turn_invocation_state")
KWARGS_DEPRECATION = "`**kwargs` parameter is deprecating"
TASK_ID = "turn-1"
DECISION = {"actions": [{"action_type": "task_response", "task": {"answer": "done"}}]}


class ScriptedTurnModel(Model):
    """Answers each model call with the next step: ``probe``, ``decision`` or ``text``.

    ``decision`` calls the turn-answer tool the runtime offers (specialists get
    ModelWorkerTurnDecision), through Strands' real structured-output tool.
    """

    def __init__(self, *steps: str) -> None:
        self.steps = deque(steps)
        self.calls = 0

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted-turn"}

    async def structured_output(self, *args: Any, **kwargs: Any):
        raise AssertionError("The runtime uses the structured-output tool protocol")
        yield

    async def stream(self, messages, tool_specs=None, *args: Any, **kwargs: Any):
        self.calls += 1
        step = self.steps.popleft()
        yield {"messageStart": {"role": "assistant"}}
        if step == "text":
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "plain text"}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return
        if step == "probe":
            name, tool_input = "probe", {}
        else:
            [name] = [spec["name"] for spec in tool_specs or () if spec["name"].endswith("TurnDecision")]
            tool_input = DECISION
        yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": uuid4().hex, "name": name}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


class StateRecorder(HookProvider):
    """Keeps the invocation_state each hook event carries."""

    def __init__(self) -> None:
        self.states: list[tuple[str, dict[str, Any]]] = []

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        for event_type in (BeforeInvocationEvent, BeforeToolCallEvent, AfterModelCallEvent):
            registry.add_callback(event_type, self._record)

    def _record(self, event: Any) -> None:
        self.states.append((type(event).__name__, event.invocation_state))


def recording_probe(seen: list[dict[str, Any]]):
    @tool(context=True)
    def probe(tool_context: ToolContext) -> str:
        """Record the invocation_state this tool call sees."""

        seen.append(tool_context.invocation_state)
        return "probed"

    return probe


async def run_real_turn(*steps: str) -> SimpleNamespace:
    """One production turn around a real Strands agent and a scripted model."""

    tool_states: list[dict[str, Any]] = []
    recorder = StateRecorder()
    agent = Agent(
        model=ScriptedTurnModel(*steps),
        tools=[recording_probe(tool_states)],
        hooks=[recorder],
        callback_handler=None,
    )
    executor, _ = build_turn_owner(invoke=None, guidance=deque(["", ""]), committed=[], cleared=[], model=agent)
    stops: list[ToolStop] = []
    activity: list[dict[str, Any]] = []

    def set_active_execution_context(task_id: str) -> ToolStop:
        stops.append(ToolStop(agent_id="independent", task_id=task_id))
        return stops[-1]

    executor.effects = replace(
        executor.effects,
        set_active_execution_context=set_active_execution_context,
        resolved_max_tool_calls_per_task=lambda: 10,
        emit_activity_event=lambda **event: activity.append(event),
        turn_decision_from_structured_output=lambda output: (
            turn_action_outbox_runtime._turn_decision_from_structured_output(None, output)
        ),
    )
    task = Task(task_id=TASK_ID, task_type=MessageMetaType.REQUEST, problem_statement="probe and answer")
    response = await executor.process_llm_interaction(task, MessageHeader(), MessageMeta())
    return SimpleNamespace(
        response=response, agent=agent, stop=stops[0], activity=activity, tool_states=tool_states, hooks=recorder
    )


def kwargs_deprecations(caught: list[warnings.WarningMessage]) -> list[str]:
    return [str(item.message) for item in caught if KWARGS_DEPRECATION in str(item.message)]


@pytest.mark.asyncio
async def test_a_real_turn_calls_strands_without_deprecated_keywords() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        turn = await run_real_turn("probe", "decision")

    assert kwargs_deprecations(caught) == []
    assert turn.response.answer == "done"


@pytest.mark.asyncio
async def test_the_turns_callback_handler_gets_the_events_and_the_agent_gets_its_own_back() -> None:
    turn = await run_real_turn("probe", "decision")

    # ActivityPhaseCallbackHandler is one of the turn's handlers; it reports
    # each tool the model starts.
    assert "probe" in [event["details"]["tool_name"] for event in turn.activity]
    assert turn.agent.callback_handler is null_callback_handler


@pytest.mark.asyncio
async def test_tools_and_hooks_see_the_turns_values_at_the_top_level() -> None:
    turn = await run_real_turn("probe", "decision")

    [state] = turn.tool_states
    assert state[TOOL_STOP_STATE_KEY] is turn.stop
    assert tool_stop_for(SimpleNamespace(invocation_state=state)) is turn.stop
    assert state[RuntimeGuardrailsPlugin._TURN_DECISION_CONTEXT_KEY]["task_id"] == TASK_ID
    assert state[ContextDebugDumpPlugin.INVOCATION_CONTEXT_KEY]["task_id"] == TASK_ID
    assert "invocation_state" not in state and "callback_handler" not in state
    # Every hook of the call gets that same dict, Strands' request_state included.
    assert {name for name, _ in turn.hooks.states} == {"BeforeInvocationEvent", "BeforeToolCallEvent", "AfterModelCallEvent"}
    assert all(hook_state is state for _, hook_state in turn.hooks.states)
    assert isinstance(state["request_state"], dict)


def turn_handler() -> tuple[list[dict[str, Any]], Any]:
    events: list[dict[str, Any]] = []
    return events, lambda **event: events.append(event)


@pytest.mark.asyncio
async def test_each_call_gets_fresh_strands_state_and_the_turns_dict_stays_its_own() -> None:
    """A turn reuses its keywords for every call: retries, late guidance, a resume."""

    seen: list[dict[str, Any]] = []
    already_marked: list[bool] = []

    @tool(context=True)
    def mark(tool_context: ToolContext) -> str:
        """Leave a mark in Strands' request_state."""

        seen.append(tool_context.invocation_state)
        already_marked.append("marked" in tool_context.invocation_state["request_state"])
        tool_context.invocation_state["request_state"]["marked"] = True
        return "marked"

    class MarkThenText(ScriptedTurnModel):
        async def stream(self, messages, tool_specs=None, *args: Any, **kwargs: Any):
            if self.calls % 2 == 0:
                self.calls += 1
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": uuid4().hex, "name": "mark"}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}
                return
            async for event in super().stream(messages, tool_specs, *args, **kwargs):
                yield event

    agent = Agent(model=MarkThenText("text", "text"), tools=[mark], callback_handler=None)
    invoke_kwargs = {"invocation_state": {"turn_value": "kept"}}
    turn_state = invoke_kwargs["invocation_state"]
    first_stop, second_stop = ToolStop(agent_id="a", task_id="t"), ToolStop(agent_id="a", task_id="t")
    events, handler = turn_handler()

    await invoke_with_tool_stop(
        agent, "go", tool_stop=first_stop, invoke_kwargs=invoke_kwargs, callback_handler=handler, logger=LOGGER
    )
    await invoke_with_tool_stop(
        agent, "again", tool_stop=second_stop, invoke_kwargs=invoke_kwargs, callback_handler=handler, logger=LOGGER
    )

    first, second = seen
    assert already_marked == [False, False]
    assert first is not second and first["request_state"] is not second["request_state"]
    assert first[TOOL_STOP_STATE_KEY] is first_stop and second[TOOL_STOP_STATE_KEY] is second_stop
    assert first["turn_value"] == second["turn_value"] == "kept"
    # Strands' bookkeeping (request_state, agent, cycle ids) never reaches the turn's dict.
    assert turn_state == {"turn_value": "kept"} and invoke_kwargs == {"invocation_state": turn_state}
    assert [event for event in events if "result" in event] and agent.callback_handler is null_callback_handler


@pytest.mark.asyncio
async def test_the_agents_handler_is_restored_when_the_call_fails() -> None:
    agent = Agent(model=_AlwaysOverflowModel(), callback_handler=None, retry_strategy=None)
    events, handler = turn_handler()

    with pytest.raises(ContextWindowOverflowException):
        await invoke_with_tool_stop(
            agent,
            "go",
            tool_stop=ToolStop(agent_id="a", task_id="t"),
            invoke_kwargs=turn_invoke_kwargs(),
            callback_handler=handler,
            logger=LOGGER,
        )

    assert events and agent.callback_handler is null_callback_handler


@pytest.mark.asyncio
async def test_inner_reductions_count_per_call_across_strands_overflow_retries(tmp_path, caplog) -> None:
    """filtered_tool_result_context keeps its budget in the call's request_state.

    Within one call Strands reduces and retries, and every attempt counts
    against ``max_total_recovery_attempts`` (2: three model calls). A fresh
    call of the same turn starts again from zero, as it always did.
    """

    caplog.set_level(logging.WARNING)
    model = _AlwaysOverflowModel()
    manager = _ProgressiveTestConversationManager(
        storage=_storage(tmp_path), config={"reactive_overflow": {"max_total_recovery_attempts": 2}}, model=model
    )
    agent = Agent(model=model, conversation_manager=manager, callback_handler=None, retry_strategy=None)
    # The turn's values without its answer tool: that tool's schema alone
    # exceeds this model's 1,000-token window, and the reducer would give up.
    invoke_kwargs = {"invocation_state": dict(turn_invoke_kwargs()["invocation_state"])}
    _events, handler = turn_handler()

    for expected_calls in (3, 6):
        with pytest.raises(Exception, match="synthetic provider overflow"):
            await invoke_with_tool_stop(
                agent,
                "x" * 10_000,
                tool_stop=ToolStop(agent_id="a", task_id="t"),
                invoke_kwargs=invoke_kwargs,
                callback_handler=handler,
                logger=LOGGER,
            )
        assert model.calls == expected_calls

    attempts = [
        line.split(" attempt=")[1].split()[0]
        for line in caplog.text.splitlines()
        if "event=inner_context_overflow_reduction" in line
    ]
    assert attempts == ["1", "2", "1", "2"]
    assert "request_state" not in invoke_kwargs["invocation_state"]


@pytest.mark.asyncio
async def test_guardrails_count_the_forced_answer_retry_within_one_call(caplog) -> None:
    """runtime_guardrails keeps its forced-retry count in the call's request_state.

    A text reply makes Strands force the answer tool once; a second text reply
    in the same call is the exhausted retry. The turn's next call starts again.
    """

    caplog.set_level(logging.WARNING)
    agent = Agent(
        model=ScriptedTurnModel("text", "text", "text", "decision"),
        hooks=[RuntimeGuardrailsPlugin()],
        callback_handler=None,
    )
    invoke_kwargs = turn_invoke_kwargs(task_id=TASK_ID)
    _events, handler = turn_handler()

    async def call() -> Any:
        return await invoke_with_tool_stop(
            agent,
            "answer",
            tool_stop=ToolStop(agent_id="a", task_id=TASK_ID),
            invoke_kwargs=invoke_kwargs,
            callback_handler=handler,
            logger=LOGGER,
        )

    with pytest.raises(StructuredOutputException):
        await call()
    result = await call()

    events = [
        line.split("event=")[1].split()[0]
        for line in caplog.text.splitlines()
        if "event=structured_output_forced_retry" in line
    ]
    assert events == [
        "structured_output_forced_retry",
        "structured_output_forced_retry_exhausted",
        "structured_output_forced_retry",
    ]
    # The guardrails read the turn's context from the top level of the state.
    assert f"task_id={TASK_ID}" in caplog.text
    assert result.state[RuntimeGuardrailsPlugin._STRUCTURED_FORCE_COUNT_KEY] == 1


@pytest.mark.asyncio
async def test_a_turns_reinvocations_after_provider_overflow_share_one_budget() -> None:
    """The turn keeps its count of fresh reinvocations outside invocation_state.

    ``max_provider_retry_attempts`` allows two reinvocations, but
    ``max_total_recovery_attempts`` (1) allows only one: the count must survive
    from one call to the next. Each call still starts Strands with a fresh
    request_state, so reductions inside a call count separately, as before.
    """

    error = RuntimeError("maximum context length exceeded")
    states: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []

    async def invoke(prompt: Any, **kwargs: Any) -> Any:
        states.append(dict(kwargs["invocation_state"]))
        raise error

    executor, _ = build_turn_owner(
        invoke=invoke,
        guidance=deque([""]),
        committed=[],
        cleared=[],
        task_operations={
            "append_history_entry": lambda task_id, text, **entry: history.append(entry["details"]),
            "get_task_info": lambda task_id: None,
        },
        settings_overrides={
            "context_engine": {
                "strands_state_snapshots": {"enabled": False},
                "hard_limit_behavior": "recover_then_error",
                "overflow_recovery": {"enabled": True, "max_provider_retry_attempts": 2},
            },
            "inner_loop_context": {"reactive_overflow": {"max_total_recovery_attempts": 1}},
        },
    )
    task = Task(task_id=TASK_ID, task_type=MessageMetaType.REQUEST, problem_statement="too large")

    with pytest.raises(RuntimeError) as raised:
        await executor.process_llm_interaction(task, MessageHeader(), MessageMeta())

    assert raised.value is error
    assert len(states) == 2
    assert [item["reason"] for item in history] == [
        "provider_context_overflow_retry",
        "provider_context_overflow_retry_suppressed",
    ]
    assert history[-1]["suppression_reason"] == "total_recovery_budget_exhausted"
    assert all("request_state" not in state for state in states)
