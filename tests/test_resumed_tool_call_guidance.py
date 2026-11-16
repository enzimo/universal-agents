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
model is scripted. The later tests pin the Strands hook behaviour the plugin
relies on to recognise a resumed call, and test that rule
(``custom_agents.sdk.strands.paused_tool_calls``) on its own.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel
from strands import Agent, tool
from strands.hooks import AfterInvocationEvent, AfterToolCallEvent, BeforeToolCallEvent, HookRegistry
from strands.models.model import Model
from strands.tools.executors import SequentialToolExecutor
from strands.types.exceptions import EventLoopException
from strands.types.tools import ToolContext

from custom_agents.sdk.strands.changes_first import ChangesFirstToolExecutor
from custom_agents.sdk.strands.paused_tool_calls import INTERRUPT_STOP_REASON, PausedToolCalls
from custom_agents.sdk.strands.turn_tool_stop import invoke_with_tool_stop
from custom_agents.structured_llm_messages import StructuredAgentTurnDecision, StructuredTaskPayload, TaskResponseAction
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import (
    GUIDANCE_APPLIED_EVENT,
    RuntimeSteeringPlugin,
    StrandsInterruptedTurnSession,
    _run_resumed_turn,
    handle_task_steering_message,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, ProgressState, TaskSteeringType
from custom_tools.runtime_context import task_work
from custom_tools.tool_stop import ToolStop
from tests.task_work_support import strands_parts
from tests.test_task_wait_limit import _executor

LOGGER = logging.getLogger(__name__)
TASK = "task_two_services"
GUIDANCE = "Check the second service first."
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


Step = list[tuple[str, dict[str, Any]]] | str


class StepsModel(Model):
    """Answers each model call with the next scripted step and records what it read.

    A step is a list of tool calls ``(name, arguments)``, a final text, or a
    function that gives one of those from what the model read (its messages as
    JSON). Tool use ids are ``step<N>-call<I>``.
    """

    def __init__(self, steps: list[Step | Callable[[str], Step]]) -> None:
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
        if callable(step):
            step = step(self.inputs[-1])
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
        Task(task_id=TASK, task_type=MessageMetaType.REQUEST, problem_statement="Check two services."),
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


def _resumed_call_log(tool_name: str, tool_use_id: str, *, waiting: int) -> str:
    return (
        f"event=task_steering_resumed_tool_call task_id={TASK} tool={tool_name} "
        f"tool_use_id={tool_use_id} guidance_waiting={waiting}"
    )


def _resumed_call_logs(caplog) -> list[str]:
    """The structured part of each resumed-call log line."""

    return [
        record.getMessage().split("waits for the next call. ", 1)[1]
        for record in caplog.records
        if "event=task_steering_resumed_tool_call" in record.getMessage()
    ]


PAUSES = {
    # The paused call, and the hooks placed before and after the steering plugin.
    "tool": (("wait_for_setup", {"name": "service-a"}), (), ()),
    "hook-before-plugin": (("report", {"label": "needs-approval"}), (ApprovalHook(),), ()),
    "hook-after-plugin": (("report", {"label": "needs-approval"}), (), (ApprovalHook(),)),
}
PAUSED_CALL_RESULT = {
    "wait_for_setup": ("success", f"setup service-a finished: {ANSWER}"),
    "report": ("success", "reported needs-approval"),
}
PAUSED_CALL_RAN = {"wait_for_setup": f"wait_for_setup:service-a:{ANSWER}", "report": "report:needs-approval"}


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", sorted(PAUSES))
async def test_a_resumed_call_keeps_its_result_and_the_next_call_brings_the_guidance(tmp_path, caplog, pause):
    paused_call, before, after = PAUSES[pause]
    runtime = _runtime_agent(tmp_path)
    model = StepsModel([[paused_call], [("report", {"label": "next"})], "Done."])
    sdk = _strands_agent(runtime, model, before=before, after=after)

    paused = await _call(sdk, "Check the services.")
    assert paused.stop_reason == "interrupt"
    assert CALLS == []
    await _send_guidance(runtime)
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    resumed = await _call(sdk, _answers(paused))

    assert resumed.stop_reason == "end_turn"
    results = _tool_results(sdk)
    # The resumed call ran with its answer, and the model read its real result.
    assert results["step1-call0"] == PAUSED_CALL_RESULT[paused_call[0]]
    assert CALLS == [PAUSED_CALL_RAN[paused_call[0]]]
    assert _resumed_call_logs(caplog) == [_resumed_call_log(paused_call[0], "step1-call0", waiting=1)]
    # The next new call brought the guidance instead of running.
    _assert_cancelled_with_guidance(results["step2-call0"])
    assert GUIDANCE in model.inputs[-1]
    assert GUIDANCE_APPLIED_EVENT in _history_events(runtime)
    assert TASK not in runtime._pending_proactive_task_steering


@pytest.mark.asyncio
async def test_a_resumed_call_without_guidance_logs_none_waiting(tmp_path, caplog):
    runtime = _runtime_agent(tmp_path)
    model = StepsModel([[("wait_for_setup", {"name": "service-a"})], [("report", {"label": "next"})], "Done."])
    sdk = _strands_agent(runtime, model)

    paused = await _call(sdk, "Check the services.")
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    await _call(sdk, _answers(paused))

    results = _tool_results(sdk)
    assert results["step1-call0"] == ("success", f"setup service-a finished: {ANSWER}")
    assert results["step2-call0"] == ("success", "reported next")
    assert _resumed_call_logs(caplog) == [_resumed_call_log("wait_for_setup", "step1-call0", waiting=0)]


@pytest.mark.asyncio
async def test_steering_turned_on_during_the_pause_still_keeps_the_resumed_result(tmp_path):
    # The paused call started while steering was off; it must still count as
    # resumed once steering is on again.
    runtime = _runtime_agent(tmp_path)
    runtime.mid_turn_steering = {"enabled": False}
    model = StepsModel([[("wait_for_setup", {"name": "service-a"})], [("report", {"label": "next"})], "Done."])
    sdk = _strands_agent(runtime, model)

    paused = await _call(sdk, "Check the services.")
    runtime.mid_turn_steering = {"enabled": True}
    await _send_guidance(runtime)
    await _call(sdk, _answers(paused))

    results = _tool_results(sdk)
    assert results["step1-call0"] == ("success", f"setup service-a finished: {ANSWER}")
    _assert_cancelled_with_guidance(results["step2-call0"])


@pytest.mark.asyncio
async def test_guidance_reaches_the_turn_decision_after_a_resumed_call(tmp_path):
    runtime = _runtime_agent(tmp_path)
    decision = ("TurnDecision", {"done": True})
    model = StepsModel([[("wait_for_setup", {"name": "service-a"})], [decision], [decision]])
    sdk = _strands_agent(runtime, model)

    paused = await _call(sdk, "Check the services.", structured_output_model=TurnDecision)
    await _send_guidance(runtime)
    resumed = await _call(sdk, _answers(paused), structured_output_model=TurnDecision)

    results = _tool_results(sdk)
    assert results["step1-call0"] == ("success", f"setup service-a finished: {ANSWER}")
    # The first decision brought the guidance; the model decided again after reading it.
    _assert_cancelled_with_guidance(results["step2-call0"])
    assert GUIDANCE in model.inputs[-1]
    assert resumed.structured_output == TurnDecision(done=True)


def _final_answer(text: str) -> StructuredAgentTurnDecision:
    return StructuredAgentTurnDecision(
        actions=[TaskResponseAction(task=StructuredTaskPayload(task_type=MessageMetaType.RESPONSE, answer=text))]
    )


ANSWER_WITHOUT_GUIDANCE = _final_answer("Both services are ready.")
ANSWER_WITH_GUIDANCE = _final_answer("Checked the second service first; both are ready.")


def _decide(read: str) -> Step:
    """The model's turn decision: it follows the guidance once the model has read it."""

    decision = ANSWER_WITH_GUIDANCE if GUIDANCE in read else ANSWER_WITHOUT_GUIDANCE
    return [(StructuredAgentTurnDecision.__name__, json.loads(decision.model_dump_json()))]


def _resume_executor(manager: TaskManager) -> Any:
    """The turn executor _run_resumed_turn uses, set up as tests/test_resumed_turn_work.py does."""

    executor = _executor(manager, 5)
    executor.read_settings = lambda: SimpleNamespace(id="lead", name="Lead", max_waits_per_task=5, debug={})
    executor.tasks = replace(executor.tasks, record_turn_work=manager.record_turn_work)
    executor.effects = replace(
        executor.effects, transform_result=lambda _content, result: result,
        turn_decision_from_structured_output=lambda output: output,
    )
    executor.model = lambda: SimpleNamespace(messages=[])
    return executor


@pytest.mark.asyncio
async def test_a_resumed_turn_carries_out_the_decision_made_after_reading_the_guidance(tmp_path):
    # The production resume path. A resumed turn carries out its decision's
    # actions, a final answer included, before its result is applied, and it
    # has no check after the model call; so the guidance must reach the model
    # at its turn decision call.
    runtime = _runtime_agent(tmp_path)
    manager = runtime.task_manager
    model = StepsModel([[("wait_for_setup", {"name": "service-a"})], _decide, _decide])
    sdk = _strands_agent(runtime, model)
    invoke_kwargs = {"structured_output_model": StructuredAgentTurnDecision}
    paused = await _call(sdk, "Check the services.", **invoke_kwargs)
    assert paused.stop_reason == "interrupt"
    await _send_guidance(runtime)

    info = manager.get_task_info(TASK)
    session = StrandsInterruptedTurnSession(
        task_id=TASK, strands=strands_parts(sdk), invoke_kwargs=invoke_kwargs, callback_handler=lambda **_: None,
        full_prompt="", msg_content=info.task, msg_header=info.header, msg_meta=info.meta, created_at=_now(),
        interrupts=[],
    )
    carried_out = []

    async def outbox(**kwargs):
        carried_out.append(kwargs["decision"])
        return info.task

    runtime.turn_executor = _resume_executor(manager)
    runtime._execute_turn_decision_outbox = outbox
    runtime._apply_task_processing_result = AsyncMock()
    runtime._interrupted_turn_sessions = {TASK: session}
    answer = TaskSteering(steering_type=TaskSteeringType.INTERRUPT_RESPONSE, target_task_id=TASK)
    with task_work(TASK):
        await _run_resumed_turn(
            runtime, session, answer, info, _answers(paused), ToolStop(agent_id="lead", task_id=TASK),
        )

    results = _tool_results(sdk)
    assert results["step1-call0"] == ("success", f"setup service-a finished: {ANSWER}")
    # The first decision brought the guidance; the decision carried out follows it.
    _assert_cancelled_with_guidance(results["step2-call0"])
    assert carried_out == [ANSWER_WITH_GUIDANCE]
    runtime._apply_task_processing_result.assert_awaited_once()
    assert TASK not in runtime._pending_proactive_task_steering
    assert TASK not in runtime._interrupted_turn_sessions


@pytest.mark.asyncio
async def test_two_paused_calls_in_one_step_both_keep_their_results(tmp_path):
    runtime = _runtime_agent(tmp_path)
    step = [("wait_for_setup", {"name": "service-a"}), ("wait_for_setup", {"name": "service-b"}), ("report", {"label": "sibling"})]
    model = StepsModel([step, [("report", {"label": "next"})], "Done."])
    sdk = _strands_agent(runtime, model)

    paused = await _call(sdk, "Check the services.")
    assert paused.stop_reason == "interrupt" and len(paused.interrupts) == 2
    # The executor ran the sibling beside the two calls that paused.
    assert CALLS == ["report:sibling"]
    await _send_guidance(runtime)
    await _call(sdk, _answers(paused))

    results = _tool_results(sdk)
    assert results["step1-call0"] == ("success", f"setup service-a finished: {ANSWER}")
    assert results["step1-call1"] == ("success", f"setup service-b finished: {ANSWER}")
    assert results["step1-call2"] == ("success", "reported sibling")
    # The finished sibling did not run again; the next new call brought the guidance.
    assert sorted(CALLS) == sorted(
        ["report:sibling", f"wait_for_setup:service-a:{ANSWER}", f"wait_for_setup:service-b:{ANSWER}"]
    )
    _assert_cancelled_with_guidance(results["step2-call0"])


@pytest.mark.asyncio
async def test_a_new_call_with_pending_guidance_is_still_cancelled(tmp_path):
    runtime = _runtime_agent(tmp_path)
    model = StepsModel([[("report", {"label": "first"})], "Done."])
    sdk = _strands_agent(runtime, model)
    await _send_guidance(runtime, while_waiting=False)

    result = await _call(sdk, "Check the services.")

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
    model = StepsModel([[("wait_for_setup", {"name": "service-a"}), ("report", {"label": "after"})], "Done."])
    sdk = _strands_agent(runtime, model, executor=SequentialToolExecutor())

    paused = await _call(sdk, "Check the services.")
    assert CALLS == []
    await _send_guidance(runtime)
    await _call(sdk, _answers(paused))

    results = _tool_results(sdk)
    assert results["step1-call0"] == ("success", f"setup service-a finished: {ANSWER}")
    _assert_cancelled_with_guidance(results["step1-call1"])
    assert CALLS == [f"wait_for_setup:service-a:{ANSWER}"]


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
        paused_call, hooks = ("wait_for_setup", {"name": "service-a"}), [recorder]
    else:
        paused_call, hooks = ("report", {"label": "needs-approval"}), [ApprovalHook(), recorder]
    sdk = Agent(
        model=StepsModel([[paused_call], "Done."]), tools=[wait_for_setup, report], hooks=hooks,
        callback_handler=None, tool_executor=ChangesFirstToolExecutor(lambda _name, _arguments: None),
    )

    paused = await sdk.invoke_async("Check the services.")
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


def _tool_use(tool_use_id: str) -> dict[str, Any]:
    return {"toolUseId": tool_use_id, "name": "report", "input": {}}


class _TrackerEvents:
    """Drives a PausedToolCalls through a real HookRegistry with real Strands events."""

    def __init__(self) -> None:
        self.tracker = PausedToolCalls()
        self.registry = HookRegistry()
        self.tracker.register_hooks(self.registry)

    def start(self, tool_use_id: str) -> bool:
        return self.tracker.start(
            BeforeToolCallEvent(agent=None, selected_tool=None, tool_use=_tool_use(tool_use_id), invocation_state={})
        )

    def finish(self, tool_use_id: str) -> None:
        result = {"toolUseId": tool_use_id, "status": "success", "content": []}
        self.registry.invoke_callbacks(
            AfterToolCallEvent(
                agent=None, selected_tool=None, tool_use=_tool_use(tool_use_id), invocation_state={}, result=result,
            )
        )

    def end(self, stop_reason: str | None) -> None:
        result = SimpleNamespace(stop_reason=stop_reason) if stop_reason else None
        self.registry.invoke_callbacks(AfterInvocationEvent(agent=None, invocation_state={}, result=result))


@pytest.mark.parametrize("stop_reason", [None, "end_turn"], ids=["failed", "finished"])
def test_a_call_to_the_agent_that_does_not_pause_forgets_its_unfinished_calls(stop_reason):
    calls = _TrackerEvents()
    assert calls.start("call-a") is False
    calls.end(stop_reason)
    # A later new call that reuses the id is new.
    assert calls.start("call-a") is False


def test_only_the_calls_a_pause_left_unfinished_resume_until_they_finish():
    calls = _TrackerEvents()
    calls.start("paused")
    calls.start("sibling")
    calls.finish("sibling")
    calls.end(INTERRUPT_STOP_REASON)

    assert calls.start("paused") is True
    assert calls.start("sibling") is False
    calls.finish("paused")
    calls.finish("sibling")
    assert calls.start("paused") is False


def test_a_call_that_pauses_again_still_resumes_at_the_next_resume():
    calls = _TrackerEvents()
    calls.start("paused")
    calls.end(INTERRUPT_STOP_REASON)
    assert calls.start("paused") is True
    calls.end(INTERRUPT_STOP_REASON)
    assert calls.start("paused") is True
