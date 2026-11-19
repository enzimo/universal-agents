"""A fresh turn whose call pauses hands the pause on; guidance waits for the resumed turn.

The defect behind these tests (found in the review of the resumed-call fix):
a fresh turn read waiting guidance right after its call to the Strands agent
returned, also when that call had paused for a person (a local Strands
interrupt, such as ``request_access`` waiting for a decision). It then called
the paused agent again with the guidance as text. Strands refuses that with a
``TypeError`` (a paused agent takes only interrupt answers), so the pause was
never saved, the task failed, and the agent stayed paused for later turns.

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
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from strands import Agent, tool
from strands.types.tools import ToolContext

from custom_agents.sdk.strands.changes_first import ChangesFirstToolExecutor
from custom_agents.structured_llm_messages import ModelWorkerTurnDecision
from custom_agents.task_steering_runtime import (
    GUIDANCE_APPLIED_EVENT,
    RuntimeSteeringPlugin,
    _run_resumed_turn,
    consume_pending_proactive_guidance_message,
    ensure_fresh_strands_agent_for_new_turn,
    send_interrupt_request_for_result,
)
from custom_messages.contracts.task_v1 import (
    ACCESS_REQUEST_ID_KEY,
    ACCESS_REQUEST_INTERRUPT_KIND,
    TaskSteering,
)
from custom_messages.enums import ProgressState, TaskSteeringType
from custom_tools.runtime_context import task_work
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


def _decide(read: str) -> list[tuple[str, dict[str, Any]]]:
    """The worker's turn decision as a call to Strands' structured-output tool.

    It follows the guidance once the model has read it.
    """

    answer = ANSWER_WITH_GUIDANCE if GUIDANCE in read else ANSWER_WITHOUT_GUIDANCE
    decision = ModelWorkerTurnDecision.model_validate(
        {"actions": [{"action_type": "task_response", "task": {"answer": answer}}]}
    )
    return [(ModelWorkerTurnDecision.__name__, json.loads(decision.model_dump_json(exclude_none=True)))]


def _tools(runtime: Any, guidance_from: str) -> list[Any]:
    """A tool that pauses for an access decision, as ``request_access`` does, and a sibling.

    ``guidance_from`` is where the person's guidance is accepted: in the
    sibling while it runs beside the pausing call, or in the pausing tool just
    before it pauses (only on its first run; the resume runs it again).
    """

    sent: list[bool] = []

    async def send_once() -> None:
        if not sent:
            sent.append(True)
            await _send_guidance(runtime, while_waiting=False)

    @tool(context=True)
    async def wait_for_access(service: str, tool_context: ToolContext) -> str:
        """Wait for a person's access decision."""
        if guidance_from == "pausing-tool":
            await send_once()
        answer = tool_context.interrupt("access", reason=ACCESS_REASON)
        return f"access to {service}: {answer}"

    @tool
    async def check_service(service: str) -> str:
        """Check a service."""
        await asyncio.sleep(0.05)
        if guidance_from == "sibling":
            await send_once()
        return f"{service} checked"

    return [wait_for_access, check_service]


def _turn_host(tmp_path: Any) -> Any:
    """The team lead's runtime state the turn and the steering code read, with a real task store."""

    runtime = _runtime_agent(tmp_path)
    runtime.named_agent_type = "TeamArchitect"
    runtime._interrupted_turn_sessions = {}
    return runtime


def _fresh_turn(runtime: Any, sdk: Agent) -> Any:
    """The turn owner of a fresh turn calling ``sdk``, with the real steering effects."""

    executor, _ = build_turn_owner(invoke=None, guidance=deque(), committed=[], cleared=[], model=sdk)
    executor.effects = replace(
        executor.effects,
        ensure_fresh_strands_agent_for_new_turn=lambda **kwargs: ensure_fresh_strands_agent_for_new_turn(
            runtime, **kwargs
        ),
        reset_llm_conversation=lambda: sdk.messages.clear(),
        consume_pending_proactive_guidance_message=lambda task_id: consume_pending_proactive_guidance_message(
            runtime, task_id
        ),
        send_interrupt_request_for_result=lambda **kwargs: send_interrupt_request_for_result(runtime, **kwargs),
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
    sdk = Agent(
        model=model,
        tools=_tools(runtime, guidance_from),
        hooks=[RuntimeSteeringPlugin(runtime)],
        callback_handler=None,
        tool_executor=ChangesFirstToolExecutor(lambda _name, _arguments: None),
    )
    parts = runtime._strands_parts = strands_parts(sdk)
    calls = _count_calls(sdk)
    info = manager.get_task_info(TASK)

    with task_work(TASK):
        outcome = await _fresh_turn(runtime, sdk).process_llm_interaction(info.task, info.header, info.meta)

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
