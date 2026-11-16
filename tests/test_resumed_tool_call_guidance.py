"""A tool call that resumes after its pause keeps its result; guidance waits for the next call.

The incident behind these tests: a person's message arrived while
``set_up_tools`` waited for setup pages. It was accepted as guidance, and when
the pages were done and the turn resumed, ``RuntimeSteeringPlugin`` cancelled
the resumed call with the guidance. ``set_up_tools`` never got its answer and
the model read "cancelled" instead of "all setup steps are done".

Real pieces: Strands agents on the locked SDK version with the production
executor (``ChangesFirstToolExecutor``), the real ``RuntimeSteeringPlugin``, a
real ``TaskManager``, and the real guidance acceptance
(``handle_task_steering_message``). Every call to the agent goes through
``invoke_with_tool_stop`` inside the task's work record, as turns do. Only the
model is scripted. The last tests pin the Strands hook behaviour the plugin
relies on to recognise a resumed call.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from strands import Agent, tool
from strands.hooks import AfterInvocationEvent, AfterToolCallEvent, BeforeToolCallEvent, HookRegistry
from strands.models.model import Model
from strands.tools.executors import SequentialToolExecutor
from strands.types.exceptions import EventLoopException
from strands.types.tools import ToolContext

from custom_agents.sdk.strands.changes_first import ChangesFirstToolExecutor
from custom_agents.sdk.strands.turn_tool_stop import invoke_with_tool_stop
from custom_agents.task_manager import TaskManager
from custom_agents.task_message_runtime import _apply_task_processing_result
from custom_agents.task_steering_runtime import (
    GUIDANCE_APPLIED_EVENT,
    RuntimeSteeringPlugin,
    handle_task_steering_message,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, ProgressState, TaskSteeringType
from custom_tools.runtime_context import task_work
from custom_tools.tool_stop import ToolStop

LOGGER = logging.getLogger(__name__)
TASK = "task_registrars"
GUIDANCE = "Check the second registrar first."
CANCELLED_PREFIX = "Tool call cancelled because new requester steering arrived"
ANSWER = "approved"
CALLS: list[str] = []


@pytest.fixture(autouse=True)
def _clear_calls() -> None:
    CALLS.clear()


@tool(context=True)
def wait_for_setup(name: str, tool_context: ToolContext) -> str:
    """Wait for a setup page, as set_up_tools does."""
    answer = tool_context.interrupt("setup", reason={"kind": "test_setup", "name": name})
    CALLS.append(f"wait_for_setup:{name}:{answer}")
    return f"setup {name} finished: {answer}"


@tool
def report(label: str) -> str:
    """Report progress."""
    CALLS.append(f"report:{label}")
    return f"reported {label}"


class TurnDecision(BaseModel):
    done: bool


class StepsModel(Model):
    """Answers each model call with the next scripted step and records what it read.

    A step is a list of tool calls ``(name, arguments)`` or a final text.
    Tool use ids are ``step<N>-call<I>``.
    """

    def __init__(self, steps: list[list[tuple[str, dict[str, Any]]] | str]) -> None:
        self.steps = steps
        self.inputs: list[str] = []

    def update_config(self, **config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted"}

    async def structured_output(self, *args: Any, **kwargs: Any):
        raise AssertionError("No structured output call expected")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.inputs.append(json.dumps(messages, default=str))
        number = len(self.inputs)
        assert number <= len(self.steps), "the model was called more often than scripted"
        step = self.steps[number - 1]
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(step, str):
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": step}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return
        for index, (name, arguments) in enumerate(step):
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"step{number}-call{index}", "name": name}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(arguments)}}}}
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


class ApprovalHook:
    """Pauses a ``report`` call labelled ``needs-approval`` from a BeforeToolCallEvent hook."""

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] != "report" or event.tool_use["input"].get("label") != "needs-approval":
            return
        answer = event.interrupt("approval", reason={"kind": "test_approval"})
        if answer != ANSWER:
            event.cancel_tool = "not approved"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_agent(tmp_path) -> SimpleNamespace:
    """The team lead's runtime state the steering code reads, with a real task store."""

    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    manager.add_task(
        Task(task_id=TASK, task_type=MessageMetaType.REQUEST, problem_statement="Check two registrars."),
        MessageHeader(), MessageMeta(), owner_person_id=None,
    )
    manager.update_main_task_state(TASK, ProgressState.IN_PROGRESS, _now())
    return SimpleNamespace(
        id="lead", name="Lead", team_id="team", task_manager=manager,
        mid_turn_steering={"enabled": True}, _pending_proactive_task_steering={},
        _processed_task_steering_ids=set(), _agent_id_by_name={}, named_agent_type_queue_map={},
        _logger=LOGGER,
    )


def _strands_agent(runtime: SimpleNamespace, model: StepsModel, *, before=(), after=(), executor=None) -> Agent:
    """A Strands agent with the real steering plugin between ``before`` and ``after`` hooks."""

    return Agent(
        model=model,
        tools=[wait_for_setup, report],
        hooks=[*before, RuntimeSteeringPlugin(runtime), *after],
        callback_handler=None,
        tool_executor=executor or ChangesFirstToolExecutor(lambda _name, _arguments: None),
    )


async def _call(sdk: Agent, prompt: Any, **invoke_kwargs: Any) -> Any:
    """One call to the agent for the task, as the turn runtime makes it."""

    with task_work(TASK):
        return await invoke_with_tool_stop(
            sdk, prompt, tool_stop=ToolStop(agent_id="lead", task_id=TASK), invoke_kwargs=invoke_kwargs,
            callback_handler=lambda **_: None, logger=LOGGER,
        )


async def _send_guidance(runtime: SimpleNamespace, *, while_waiting: bool = True) -> None:
    """The person's message arrives; the lead accepts it as guidance for the task."""

    manager = runtime.task_manager
    if while_waiting:
        # The paused turn left its task waiting (send_interrupt_request_for_result).
        manager.update_main_task_state(TASK, ProgressState.WAITING, _now())
    await handle_task_steering_message(
        runtime,
        TaskSteering(
            steering_type=TaskSteeringType.PROACTIVE_GUIDANCE, target_task_id=TASK,
            guidance_text=GUIDANCE, idempotency_key="guidance-1",
        ),
        MessageHeader(), MessageMeta(is_response_requested=False),
    )
    assert runtime._pending_proactive_task_steering[TASK][0].guidance_text == GUIDANCE
    if while_waiting:
        # The resume sets the task running again (_resume_interrupted_task_turn).
        manager.update_main_task_state(TASK, ProgressState.IN_PROGRESS, _now())


def _answers(paused: Any) -> list[dict[str, Any]]:
    return [{"interruptResponse": {"interruptId": interrupt.id, "response": ANSWER}} for interrupt in paused.interrupts]


def _tool_results(sdk: Agent) -> dict[str, tuple[str, str]]:
    """Each tool call's result as the model reads it: status and text, by tool use id."""

    results = {}
    for message in sdk.messages:
        for block in message["content"]:
            if "toolResult" in block:
                result = block["toolResult"]
                results[result["toolUseId"]] = (result["status"], result["content"][0].get("text", ""))
    return results


def _history_events(runtime: SimpleNamespace) -> list[str]:
    task = runtime.task_manager.get_task_info(TASK).task
    return [entry.details.get("event") for entry in task.execution_history if entry.details]


def _assert_cancelled_with_guidance(result: tuple[str, str]) -> None:
    status, text = result
    assert status == "error"
    assert text.startswith(CANCELLED_PREFIX)
    assert GUIDANCE in text


PAUSES = {
    # The paused call, and the hooks placed before and after the steering plugin.
    "tool": (("wait_for_setup", {"name": "porkbun"}), (), ()),
    "hook-before-plugin": (("report", {"label": "needs-approval"}), (ApprovalHook(),), ()),
    "hook-after-plugin": (("report", {"label": "needs-approval"}), (), (ApprovalHook(),)),
}
PAUSED_CALL_RESULT = {
    "wait_for_setup": ("success", f"setup porkbun finished: {ANSWER}"),
    "report": ("success", "reported needs-approval"),
}
PAUSED_CALL_RAN = {"wait_for_setup": f"wait_for_setup:porkbun:{ANSWER}", "report": "report:needs-approval"}


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", sorted(PAUSES))
async def test_a_resumed_call_keeps_its_result_and_the_next_call_brings_the_guidance(tmp_path, pause):
    paused_call, before, after = PAUSES[pause]
    runtime = _runtime_agent(tmp_path)
    model = StepsModel([[paused_call], [("report", {"label": "next"})], "Done."])
    sdk = _strands_agent(runtime, model, before=before, after=after)

    paused = await _call(sdk, "Check the registrars.")
    assert paused.stop_reason == "interrupt"
    assert CALLS == []
    await _send_guidance(runtime)
    resumed = await _call(sdk, _answers(paused))

    assert resumed.stop_reason == "end_turn"
    results = _tool_results(sdk)
    # The resumed call ran with its answer, and the model read its real result.
    assert results["step1-call0"] == PAUSED_CALL_RESULT[paused_call[0]]
    assert CALLS == [PAUSED_CALL_RAN[paused_call[0]]]
    # The next new call brought the guidance instead of running.
    _assert_cancelled_with_guidance(results["step2-call0"])
    assert GUIDANCE in model.inputs[-1]
    assert GUIDANCE_APPLIED_EVENT in _history_events(runtime)
    assert TASK not in runtime._pending_proactive_task_steering


@pytest.mark.asyncio
async def test_guidance_reaches_the_turn_decision_after_a_resumed_call(tmp_path):
    runtime = _runtime_agent(tmp_path)
    decision = ("TurnDecision", {"done": True})
    model = StepsModel([[("wait_for_setup", {"name": "porkbun"})], [decision], [decision]])
    sdk = _strands_agent(runtime, model)

    paused = await _call(sdk, "Check the registrars.", structured_output_model=TurnDecision)
    await _send_guidance(runtime)
    resumed = await _call(sdk, _answers(paused), structured_output_model=TurnDecision)

    results = _tool_results(sdk)
    assert results["step1-call0"] == ("success", f"setup porkbun finished: {ANSWER}")
    # The first decision brought the guidance; the model decided again after reading it.
    _assert_cancelled_with_guidance(results["step2-call0"])
    assert GUIDANCE in model.inputs[-1]
    assert resumed.structured_output == TurnDecision(done=True)


@pytest.mark.asyncio
async def test_guidance_left_after_a_resumed_turn_defers_its_final_answer(tmp_path):
    runtime = _runtime_agent(tmp_path)
    model = StepsModel([[("wait_for_setup", {"name": "porkbun"})], "Both registrars are ready."])
    sdk = _strands_agent(runtime, model)

    paused = await _call(sdk, "Check the registrars.")
    await _send_guidance(runtime)
    resumed = await _call(sdk, _answers(paused))

    assert resumed.stop_reason == "end_turn"
    assert _tool_results(sdk)["step1-call0"] == ("success", f"setup porkbun finished: {ANSWER}")
    # No later call read the guidance, so the existing check before a final
    # answer is committed finds it and runs the task again with it.
    manager = runtime.task_manager
    answer = Task(task_id=TASK, task_type=MessageMetaType.RESPONSE, answer="Both registrars are ready.")
    await _apply_task_processing_result(runtime, TASK, manager.get_task_info(TASK), answer)
    assert "task_steering_terminal_commit_deferred" in _history_events(runtime)
    assert GUIDANCE in (manager.read_runtime_resume_directive(TASK) or "")
    assert manager.get_task_info(TASK).state == ProgressState.ACCEPTED
    assert manager.get_task_info(TASK).task.answer in (None, "")


@pytest.mark.asyncio
async def test_two_paused_calls_in_one_step_both_keep_their_results(tmp_path):
    runtime = _runtime_agent(tmp_path)
    step = [("wait_for_setup", {"name": "porkbun"}), ("wait_for_setup", {"name": "cloudflare"}), ("report", {"label": "sibling"})]
    model = StepsModel([step, [("report", {"label": "next"})], "Done."])
    sdk = _strands_agent(runtime, model)

    paused = await _call(sdk, "Check the registrars.")
    assert paused.stop_reason == "interrupt" and len(paused.interrupts) == 2
    # The executor ran the sibling beside the two calls that paused.
    assert CALLS == ["report:sibling"]
    await _send_guidance(runtime)
    await _call(sdk, _answers(paused))

    results = _tool_results(sdk)
    assert results["step1-call0"] == ("success", f"setup porkbun finished: {ANSWER}")
    assert results["step1-call1"] == ("success", f"setup cloudflare finished: {ANSWER}")
    assert results["step1-call2"] == ("success", "reported sibling")
    # The finished sibling did not run again; the next new call brought the guidance.
    assert sorted(CALLS) == sorted(
        ["report:sibling", f"wait_for_setup:porkbun:{ANSWER}", f"wait_for_setup:cloudflare:{ANSWER}"]
    )
    _assert_cancelled_with_guidance(results["step2-call0"])


@pytest.mark.asyncio
async def test_a_new_call_with_pending_guidance_is_still_cancelled(tmp_path):
    runtime = _runtime_agent(tmp_path)
    model = StepsModel([[("report", {"label": "first"})], "Done."])
    sdk = _strands_agent(runtime, model)
    await _send_guidance(runtime, while_waiting=False)

    result = await _call(sdk, "Check the registrars.")

    assert result.stop_reason == "end_turn"
    _assert_cancelled_with_guidance(_tool_results(sdk)["step1-call0"])
    assert CALLS == []
    assert GUIDANCE in model.inputs[-1]
    assert TASK not in runtime._pending_proactive_task_steering


@pytest.mark.asyncio
async def test_a_call_that_never_ran_before_the_pause_is_cancelled_after_the_resume(tmp_path):
    # Strands' sequential executor stops a step at its first pause, so the
    # step's later call first runs after the resume: it is a new call.
    runtime = _runtime_agent(tmp_path)
    model = StepsModel([[("wait_for_setup", {"name": "porkbun"}), ("report", {"label": "after"})], "Done."])
    sdk = _strands_agent(runtime, model, executor=SequentialToolExecutor())

    paused = await _call(sdk, "Check the registrars.")
    assert CALLS == []
    await _send_guidance(runtime)
    await _call(sdk, _answers(paused))

    results = _tool_results(sdk)
    assert results["step1-call0"] == ("success", f"setup porkbun finished: {ANSWER}")
    _assert_cancelled_with_guidance(results["step1-call1"])
    assert CALLS == [f"wait_for_setup:porkbun:{ANSWER}"]


class HookRecorder:
    """Records the Strands hook events the steering plugin relies on."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, lambda event: self.events.append(("before", event.tool_use["toolUseId"])))
        registry.add_callback(AfterToolCallEvent, lambda event: self.events.append(("after", event.tool_use["toolUseId"])))
        registry.add_callback(
            AfterInvocationEvent,
            lambda event: self.events.append(("end", event.result.stop_reason if event.result else None)),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", ["tool", "hook-before-recorder"])
async def test_strands_runs_a_paused_call_again_without_an_after_event_for_the_pause(pause):
    """Pins Strands: a paused call fires no AfterToolCallEvent, the call ends with
    stop_reason "interrupt", and the resume fires BeforeToolCallEvent again for the
    same tool use. A hook that pauses the call does not stop later
    BeforeToolCallEvent callbacks from running."""

    recorder = HookRecorder()
    if pause == "tool":
        paused_call, hooks = ("wait_for_setup", {"name": "porkbun"}), [recorder]
    else:
        paused_call, hooks = ("report", {"label": "needs-approval"}), [ApprovalHook(), recorder]
    sdk = Agent(
        model=StepsModel([[paused_call], "Done."]), tools=[wait_for_setup, report], hooks=hooks,
        callback_handler=None, tool_executor=ChangesFirstToolExecutor(lambda _name, _arguments: None),
    )

    paused = await sdk.invoke_async("Check the registrars.")
    assert recorder.events == [("before", "step1-call0"), ("end", "interrupt")]
    await sdk.invoke_async(_answers(paused))
    assert recorder.events[2:] == [("before", "step1-call0"), ("after", "step1-call0"), ("end", "end_turn")]


class FailingHook:
    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)

    def before_tool_call(self, _event: BeforeToolCallEvent) -> None:
        raise RuntimeError("hook failed")


@pytest.mark.asyncio
async def test_strands_ends_a_failed_call_with_an_after_invocation_event_and_no_result():
    """Pins Strands: a call to the agent that raises still fires AfterInvocationEvent, with no result."""

    recorder = HookRecorder()
    sdk = Agent(
        model=StepsModel([[("report", {"label": "first"})]]), tools=[report], hooks=[recorder, FailingHook()],
        callback_handler=None, tool_executor=ChangesFirstToolExecutor(lambda _name, _arguments: None),
    )

    with pytest.raises(EventLoopException, match="hook failed"):
        await sdk.invoke_async("Report.")
    assert recorder.events == [("before", "step1-call0"), ("end", None)]
