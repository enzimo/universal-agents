"""A fresh turn whose call pauses hands the pause on; guidance waits for the resumed turn.

The defect behind these tests (found in the review of the resumed-call fix):
a fresh turn read waiting guidance right after its call to the Strands agent
returned, also when that call had paused for a person (a local Strands
interrupt, such as ``request_access`` waiting for a decision). It then called
the paused agent again with the guidance as text. Strands refuses that with a
``TypeError`` (a paused agent takes only interrupt answers), so the pause was
never saved, the task failed, and the agent stayed paused for later turns.
Now the turn hands the pause on and the guidance waits for the resumed turn.
The agent is marked for replacement as soon as its call pauses
(``strands_parts.mark_paused``), so a turn that still ends before it saves
the pause leaves no stuck agent to the next turn.

Real pieces: the turn owner (``LLMTurnExecutor``), a Strands agent on the
locked SDK with the production executor (``ChangesFirstToolExecutor``) and the
real ``RuntimeSteeringPlugin``, the real guidance acceptance
(``handle_task_steering_message``), the real ``send_interrupt_request_for_result``
and resume (``_run_resumed_turn``), and a real ``TaskManager``. Only the model
is scripted (``tests.test_resumed_tool_call_guidance.StepsModel``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from strands import Agent, tool
from strands.types.tools import ToolContext

from custom_agents.sdk.strands.changes_first import ChangesFirstToolExecutor
from custom_agents.strands_parts import mark_paused, turn_strands_agent
from custom_agents.structured_llm_messages import ModelWorkerTurnDecision
from custom_agents.task_steering_runtime import (
    GUIDANCE_APPLIED_EVENT,
    RuntimeSteeringPlugin,
    _run_resumed_turn,
    consume_pending_proactive_guidance_message,
    ensure_fresh_strands_agent_for_new_turn,
    send_interrupt_request_for_result,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import (
    ACCESS_REQUEST_ID_KEY,
    ACCESS_REQUEST_INTERRUPT_KIND,
    Task,
    TaskSteering,
)
from custom_messages.enums import MessageMetaType, ProgressState, TaskSteeringType
from custom_tools.runtime_context import current_task_work, task_work
from custom_tools.tool_stop import ToolStop
from tests.task_work_support import strands_parts
from tests.test_resumed_tool_call_guidance import (
    ANSWER,
    GUIDANCE,
    TASK,
    StepsModel,
    _assert_cancelled_with_guidance,
    _history_events,
    _resume_executor,
    _runtime_agent,
    _send_guidance,
    _tool_results,
)
from tests.test_runtime_owner_boundaries import build_turn_owner

ACCESS_REASON = {
    "kind": ACCESS_REQUEST_INTERRUPT_KIND,
    ACCESS_REQUEST_ID_KEY: "areq_service_a",
    "summary": "Use service A.",
}
ANSWER_WITHOUT_GUIDANCE = "Both services are ready."
ANSWER_WITH_GUIDANCE = "Checked the second service first; both are ready."
NEXT_TASK = "task_next_request"
# Runs inside a tool, once (_tools).
Hook = Callable[[], Awaitable[None]]


def _decide(read: str) -> list[tuple[str, dict[str, Any]]]:
    """The worker's turn decision as a call to Strands' structured-output tool.

    It follows the guidance once the model has read it.
    """

    answer = ANSWER_WITH_GUIDANCE if GUIDANCE in read else ANSWER_WITHOUT_GUIDANCE
    decision = ModelWorkerTurnDecision.model_validate(
        {"actions": [{"action_type": "task_response", "task": {"answer": answer}}]}
    )
    return [(ModelWorkerTurnDecision.__name__, json.loads(decision.model_dump_json(exclude_none=True)))]


def _tools(*, in_pausing_tool: Hook | None = None, in_sibling: Hook | None = None) -> list[Any]:
    """A tool that pauses for an access decision, as ``request_access`` does, and a sibling.

    ``in_pausing_tool`` runs inside the pausing tool just before it pauses,
    ``in_sibling`` inside the sibling while it runs beside the pausing call.
    Each runs on the tool's first run only: the resume runs the pausing tool
    again.
    """

    ran: set[str] = set()

    async def once(name: str, hook: Hook | None) -> None:
        if hook is not None and name not in ran:
            ran.add(name)
            await hook()

    @tool(context=True)
    async def wait_for_access(service: str, tool_context: ToolContext) -> str:
        """Wait for a person's access decision."""
        await once("pausing", in_pausing_tool)
        answer = tool_context.interrupt("access", reason=ACCESS_REASON)
        return f"access to {service}: {answer}"

    @tool
    async def check_service(service: str) -> str:
        """Check a service."""
        await asyncio.sleep(0.05)
        await once("sibling", in_sibling)
        return f"{service} checked"

    return [wait_for_access, check_service]


def _strands_agent(runtime: Any, model: StepsModel, tools: list[Any]) -> Agent:
    """A Strands agent as the team lead builds it: the real steering plugin and executor."""

    return Agent(
        model=model,
        tools=tools,
        hooks=[RuntimeSteeringPlugin(runtime)],
        callback_handler=None,
        tool_executor=ChangesFirstToolExecutor(lambda _name, _arguments: None),
    )


def _turn_host(tmp_path: Any) -> Any:
    """The team lead's runtime state the turn and the steering code read, with a real task store."""

    runtime = _runtime_agent(tmp_path)
    runtime.named_agent_type = "TeamArchitect"
    runtime._interrupted_turn_sessions = {}
    return runtime


def _fresh_turn(runtime: Any, *, committed: list[Any] | None = None, **effects: Any) -> Any:
    """The turn owner of a fresh turn, with the real steering effects.

    Like production it calls the Strands agent its turn's work record holds
    (``strands_parts.turn_strands_agent``). ``effects`` replace others.
    """

    executor, _ = build_turn_owner(
        invoke=None,
        guidance=deque(),
        committed=committed if committed is not None else [],
        cleared=[],
        task_operations={"get_task_info": runtime.task_manager.get_task_info},
    )
    executor.model = partial(turn_strands_agent, runtime)
    executor.effects = replace(
        executor.effects,
        **{
            "ensure_fresh_strands_agent_for_new_turn": lambda **kwargs: ensure_fresh_strands_agent_for_new_turn(
                runtime, **kwargs
            ),
            "reset_llm_conversation": lambda: turn_strands_agent(runtime).messages.clear(),
            "consume_pending_proactive_guidance_message": lambda task_id: consume_pending_proactive_guidance_message(
                runtime, task_id
            ),
            "send_interrupt_request_for_result": lambda **kwargs: send_interrupt_request_for_result(runtime, **kwargs),
            **effects,
        },
    )
    return executor


def _count_calls(sdk: Agent) -> list[Any]:
    """Record every call to the Strands agent (its prompt) before Strands sees it."""

    calls: list[Any] = []
    invoke = sdk.invoke_async

    async def counted(prompt: Any = None, **kwargs: Any) -> Any:
        calls.append(prompt)
        return await invoke(prompt, **kwargs)

    sdk.invoke_async = counted  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("guidance_from", ["sibling", "pausing-tool"])
async def test_guidance_accepted_during_a_pausing_call_waits_for_the_resume(tmp_path, guidance_from):
    runtime = _turn_host(tmp_path)
    manager = runtime.task_manager
    step = [("wait_for_access", {"service": "service-a"})]
    if guidance_from == "sibling":
        step.append(("check_service", {"service": "service-b"}))
    model = StepsModel([step, _decide, _decide])
    send_guidance = partial(_send_guidance, runtime, while_waiting=False)
    tools = _tools(**{"in_sibling" if guidance_from == "sibling" else "in_pausing_tool": send_guidance})
    sdk = _strands_agent(runtime, model, tools)
    parts = runtime._strands_parts = strands_parts(sdk)
    calls = _count_calls(sdk)
    info = manager.get_task_info(TASK)

    with task_work(TASK):
        outcome = await _fresh_turn(runtime).process_llm_interaction(info.task, info.header, info.meta)

    # The pause went to send_interrupt_request_for_result; the paused agent was not called again.
    assert len(calls) == 1
    assert isinstance(outcome, TaskSteering)
    assert outcome.steering_type == TaskSteeringType.INTERRUPT_REQUEST
    session = runtime._interrupted_turn_sessions[TASK]
    assert session.strands is parts and parts.replace_before_next_turn is True
    assert manager.get_task_info(TASK).state == ProgressState.WAITING
    # The guidance waits, unread.
    assert [item.guidance_text for item in runtime._pending_proactive_task_steering[TASK]] == [GUIDANCE]
    assert GUIDANCE_APPLIED_EVENT not in _history_events(runtime)
    assert all(GUIDANCE not in read for read in model.inputs)

    # The person decides; the resumed turn reads the guidance before its decision is carried out.
    carried_out = []

    async def outbox(**kwargs: Any) -> Any:
        carried_out.append(kwargs["decision"])
        return manager.get_task_info(TASK).task

    runtime.turn_executor = _resume_executor(manager)
    runtime._execute_turn_decision_outbox = outbox
    runtime._apply_task_processing_result = AsyncMock()
    manager.update_main_task_state(TASK, ProgressState.IN_PROGRESS, datetime.now(timezone.utc).isoformat())
    answers = [
        {"interruptResponse": {"interruptId": record.interrupt_id, "response": ANSWER}} for record in session.interrupts
    ]
    with task_work(TASK):
        await _run_resumed_turn(
            runtime,
            session,
            TaskSteering(steering_type=TaskSteeringType.INTERRUPT_RESPONSE, target_task_id=TASK),
            manager.get_task_info(TASK),
            answers,
            ToolStop(agent_id="lead", task_id=TASK),
        )

    results = _tool_results(sdk)
    # The resumed call kept its answer; the next new call, the decision, brought the guidance.
    assert results["step1-call0"] == ("success", f"access to service-a: {ANSWER}")
    _assert_cancelled_with_guidance(results["step2-call0"])
    assert [decision.actions[0].task.answer for decision in carried_out] == [ANSWER_WITH_GUIDANCE]
    assert GUIDANCE_APPLIED_EVENT in _history_events(runtime)
    assert TASK not in runtime._pending_proactive_task_steering
    # The finished resume frees the agent for later turns again.
    assert TASK not in runtime._interrupted_turn_sessions
    assert parts.replace_before_next_turn is False


class _PauseNotSaved(Exception):
    """An error inside send_interrupt_request_for_result before it saved the pause."""


def _add_task(runtime: Any, task_id: str) -> Task:
    manager = runtime.task_manager
    task = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Report the status.")
    manager.add_task(task, MessageHeader(), MessageMeta(), owner_person_id=None)
    manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, datetime.now(timezone.utc).isoformat())
    return task


EXITS_BEFORE_SAVING = ["stop-recorded-as-the-call-pauses", "error-while-saving-the-pause"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_before_saving", EXITS_BEFORE_SAVING)
async def test_a_turn_that_ends_before_saving_its_pause_leaves_no_stuck_agent(tmp_path, caplog, exit_before_saving):
    runtime = _turn_host(tmp_path)
    manager = runtime.task_manager
    stops: set[str] = set()

    async def record_stop() -> None:
        # control_runtime records a person's stop for the task as its call pauses.
        stops.add(TASK)

    def raise_if_stop_requested(task_id: str | None) -> None:
        if task_id in stops:
            raise asyncio.CancelledError

    async def fail_to_save(**_kwargs: Any) -> Any:
        raise _PauseNotSaved("the pause could not be saved")

    stop = exit_before_saving == "stop-recorded-as-the-call-pauses"
    paused_sdk = _strands_agent(
        runtime,
        StepsModel([[("wait_for_access", {"service": "service-a"})]]),
        _tools(in_pausing_tool=record_stop if stop else None),
    )
    paused_parts = runtime._strands_parts = strands_parts(paused_sdk)
    runtime._strands_agent = paused_sdk
    fresh_model = StepsModel([_decide])

    def create_strands_agent() -> None:
        runtime._strands_agent = _strands_agent(runtime, fresh_model, _tools())
        runtime._strands_parts = strands_parts(runtime._strands_agent)

    runtime._create_strands_agent = create_strands_agent
    effects: dict[str, Any] = {"raise_if_stop_requested": raise_if_stop_requested}
    if not stop:
        effects["send_interrupt_request_for_result"] = fail_to_save
    info = manager.get_task_info(TASK)

    with pytest.raises(asyncio.CancelledError if stop else _PauseNotSaved), task_work(TASK):
        await _fresh_turn(runtime, **effects).process_llm_interaction(info.task, info.header, info.meta)

    # No session holds the paused agent, but it is marked: no later turn may call it.
    assert runtime._interrupted_turn_sessions == {}
    assert paused_parts.replace_before_next_turn is True

    committed: list[Any] = []
    next_task = _add_task(runtime, NEXT_TASK)
    caplog.set_level(logging.WARNING)
    with task_work(NEXT_TASK):
        outcome = await _fresh_turn(runtime, committed=committed).process_llm_interaction(
            next_task, MessageHeader(), MessageMeta()
        )

    # The next main-lane turn got a new Strands agent and finished its work.
    assert runtime._strands_parts is not paused_parts
    assert fresh_model.inputs and committed
    assert isinstance(outcome, Task) and outcome.task_type == MessageMetaType.RESPONSE
    rotations = [record.getMessage() for record in caplog.records if "event=strands_agent_rotated_after_interrupt" in record.getMessage()]
    assert len(rotations) == 1
    assert f"task_id={NEXT_TASK} interrupted_task_ids=[]" in rotations[0]


@pytest.mark.asyncio
async def test_a_paused_agent_the_turn_does_not_hold_is_reported(tmp_path, caplog):
    # Production calls the agent its work record holds; this checks the warning if it ever does not.
    runtime = _turn_host(tmp_path)
    info = runtime.task_manager.get_task_info(TASK)
    sdk = _strands_agent(runtime, StepsModel([[("wait_for_access", {"service": "service-a"})]]), _tools())
    other_parts = strands_parts(SimpleNamespace(messages=[]))
    handed: list[Any] = []

    def bind_other_parts(**_kwargs: Any) -> bool:
        current_task_work().strands = other_parts
        return False

    async def hand_on(**kwargs: Any) -> TaskSteering:
        handed.append(kwargs["result"])
        return TaskSteering(steering_type=TaskSteeringType.INTERRUPT_REQUEST, target_task_id=TASK)

    executor = _fresh_turn(runtime, ensure_fresh_strands_agent_for_new_turn=bind_other_parts, send_interrupt_request_for_result=hand_on)
    executor.model = lambda: sdk
    caplog.set_level(logging.WARNING)
    with task_work(TASK):
        await executor.process_llm_interaction(info.task, info.header, info.meta)

    assert len(handed) == 1  # the pause was still handed on
    assert other_parts.replace_before_next_turn is False
    warnings = [record.getMessage() for record in caplog.records if "event=paused_strands_agent_unmarked" in record.getMessage()]
    assert len(warnings) == 1
    assert f"agent_id=independent task_id={TASK}" in warnings[0]


def test_mark_paused_marks_only_the_bound_turns_parts_that_hold_the_agent():
    strands_agent = object()
    assert mark_paused(strands_agent) is False  # no turn bound
    with task_work(TASK) as work:
        work.strands = parts = strands_parts(strands_agent)
        assert mark_paused(object()) is False
        assert parts.replace_before_next_turn is False
        assert mark_paused(strands_agent) is True
        assert parts.replace_before_next_turn is True
