"""Real SDK interrupt/resume obeys the same per-task rules as a fresh turn."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from strands import Agent, tool
from strands.types.tools import ToolContext

from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import StrandsInterruptedTurnSession, _run_resumed_turn
from custom_agents.task_wait_limit import TaskWaitLimitReached
from custom_agents.turn_work_notes import ENDED_BY_WAIT, note_details, turn_work_notes
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, TaskSteeringType
from custom_tools.tool_stop import ToolStop
from tests.test_changes_first_executor import ScriptedModel
from tests.test_task_wait_limit import ESCALATE, _executor, _manager, _record_waits, _turn


@pytest.mark.asyncio
async def test_child_history_does_not_spend_its_parents_waits_or_become_its_own_turns(tmp_path):
    manager = _manager(tmp_path)
    _record_waits(manager, 1)
    manager.add_subtask(
        "run_1", Task(task_id="child", parent_task_id="run_1", task_type=MessageMetaType.REQUEST),
        MessageHeader(), MessageMeta(),
    )
    for _ in range(4):
        manager.record_turn_work("child", note_details(
            tool_calls=[{"tool": "knowledge_write", "input": "child-only work", "status": "success"}],
            sent=["child question"], started_wait=True, ended_by=ENDED_BY_WAIT,
        ))
    manager.bubble_subtask_result("run_1", "child")
    manager.bubble_subtask_result("run_1", "child")
    assert manager.count_started_waits("run_1") == 1
    assert manager.count_started_waits("child") == 4
    assert "child-only work" not in manager.render_earlier_turn_work_markdown("run_1")
    assert "child-only work" in manager.render_earlier_turn_work_markdown("child")
    restored = TaskManager(db_path=str(tmp_path / "worker.db"))
    restored.set_owner_identity("worker_1", "worker", "team", "KnowledgeCurator")
    restored.restore_runtime_state()
    assert restored.count_started_waits("run_1") == 1
    assert restored.count_started_waits("child") == 4
    await _executor(restored, 5)._stop_at_wait_limit(_turn(restored), ESCALATE)


@tool(context=True)
def edit_after_access(tool_context: ToolContext) -> str:
    """Pause for access before recording this test edit."""
    tool_context.interrupt("test-access", reason={"access_request_id": "test-request"})
    return "saved edit"


class _ResumedDecisionModel(ScriptedModel):
    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if self.steps == 0:
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                yield event
            return
        self.steps += 1
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": "decision", "name": "StructuredAgentTurnDecision",
        }}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": ESCALATE.model_dump_json()}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("waits", [4, 5], ids=["fifth-allowed", "sixth-refused"])
async def test_real_interrupt_resume_checks_waits_and_saves_work_before_dispatch(tmp_path, waits):
    manager = _manager(tmp_path)
    _record_waits(manager, waits)
    executor = _executor(manager, 5)
    executor.read_settings = lambda: SimpleNamespace(id="worker_1", name="worker", max_waits_per_task=5, debug={})
    executor.tasks = replace(executor.tasks, record_turn_work=manager.record_turn_work)
    executor.effects = replace(
        executor.effects, transform_result=lambda _content, result: result,
        turn_decision_from_structured_output=lambda output: output,
    )
    # Another task may have replaced the facade's model while this session waited.
    executor.model = lambda: SimpleNamespace(messages=[])
    sdk = Agent(model=_ResumedDecisionModel([("edit_after_access", {})]), tools=[edit_after_access], callback_handler=None)
    decision_model = type(ESCALATE)
    paused = await sdk.invoke_async("Edit, then ask for the missing detail.", structured_output_model=decision_model)
    assert paused.stop_reason == "interrupt"
    info = manager.get_task_info("run_1")
    session = StrandsInterruptedTurnSession(
        task_id="run_1", strands_agent=sdk, invoke_kwargs={"structured_output_model": decision_model},
        callback_handler=lambda **_: None, full_prompt="", msg_content=info.task, msg_header=info.header, msg_meta=info.meta,
        created_at="2026-10-20T00:00:00Z", interrupts=[],
    )
    dispatched = []

    async def outbox(**kwargs):
        assert manager.count_started_waits("run_1") == waits + 1
        dispatched.append(kwargs["decision"])
        return info.task

    agent = SimpleNamespace(
        id="worker_1", turn_executor=executor, task_manager=manager, _logger=executor.logger,
        _execute_turn_decision_outbox=outbox, _apply_task_processing_result=AsyncMock(),
        _interrupted_turn_sessions={"run_1": session}, _strands_agent=sdk,
    )
    steering = TaskSteering(steering_type=TaskSteeringType.INTERRUPT_RESPONSE, target_task_id="run_1")
    response = [{"interruptResponse": {"interruptId": paused.interrupts[0].id, "response": "approved"}}]
    call = _run_resumed_turn(agent, session, steering, info, response, ToolStop(agent_id=agent.id, task_id="run_1"))
    if waits == 5:
        with pytest.raises(TaskWaitLimitReached):
            await call
        assert not dispatched and manager.count_started_waits("run_1") == 5
    else:
        await call
        assert dispatched == [ESCALATE]
        last_note = turn_work_notes(manager.get_task("run_1"))[-1]
        assert [(item["tool"], item["status"]) for item in last_note.details["tool_calls"]] == [
            ("edit_after_access", "success"),
        ]
        assert "StructuredAgentTurnDecision" not in json.dumps(last_note.details["tool_calls"])
