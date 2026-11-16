"""A final answer held back for an open access workflow runs its task again.

Incident (2026-11-14, summarised): TeamLead's task published a workflow whose
last step was the requested work. When TeamLead gave a final answer before
reporting that step, ``access_workflow_runtime`` held the answer back and
queued the task to run again with a continuation note, but the end-of-turn
handling then marked the task WAITING. That removed its queue entry, so nothing
ran it again and the person's later messages were accepted but never read.

These tests use a real TaskManager, a real UniversalAgent and its real outbox,
and a real workflow record in the task manager's own database. They cover the
end-of-turn rule (``task_message_runtime._keep_held_answer_state``): a request
naming the turn's own task is a held answer and keeps the state its hold saved.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_agents import task_message_runtime, task_steering_runtime
from custom_agents.structured_llm_messages import (
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TaskResponseAction,
)
from custom_messages.contracts.access_workflow_v2 import (
    AccessWorkflowV2,
    WorkflowEventV2,
    WorkflowPhase,
    WorkflowStepState,
    WorkflowStepV2,
)
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, ProgressState, TaskSteeringType
from custom_tools.tool_stop import ToolStop
from storage.access_workflow_repository import AccessWorkflowRepository
from tests.task_work_support import strands_parts
from tests.test_turn_action_outbox import _add_active_parent_task, _make_agent, _make_task_manager, _now

CONTINUATION_NOTE = "Continue the published access workflow in this same task."


def _open_workflow(manager, task_id: str) -> AccessWorkflowV2:
    """A published plan whose account step is done and whose work step still runs."""

    steps = (
        WorkflowStepV2(
            step_id="link_account", title="Link the registrar account", kind="account",
            reason="The price check needs the account", success_criterion="The account is linked",
            state=WorkflowStepState.COMPLETE,
        ),
        WorkflowStepV2(
            step_id="do_the_work", title="Do the requested work", kind="work", depends_on=("link_account",),
            reason="The person asked for domain prices", success_criterion="The prices are reported",
            state=WorkflowStepState.RUNNING,
        ),
    )
    record = AccessWorkflowV2(
        owner_person_id="person-1",
        team_id=manager.identity.team_id,
        requester_agent_id=manager.identity.owner_agent_id,
        requester_agent_type="TeamArchitect",
        source_task_id=task_id,
        idempotency_key=f"{task_id}:check-prices",
        proposal_digest="digest-check-prices",
        target_agent_type="TeamArchitect",
        title="Check domain prices",
        goal="Report domain availability and prices",
        steps=steps,
        phase=WorkflowPhase.RUNNING,
    )
    event = WorkflowEventV2(workflow_id=record.workflow_id, kind="progress", code="plan_submitted")
    return AccessWorkflowRepository(manager.storage_settings).create(record, event)


def _event_codes(manager, workflow_id: str) -> list[str]:
    return [event.code for event in AccessWorkflowRepository(manager.storage_settings).events(workflow_id, limit=50)]


def _final_answer(text: str) -> StructuredAgentTurnDecision:
    return StructuredAgentTurnDecision(
        actions=[TaskResponseAction(task=StructuredTaskPayload(task_type=MessageMetaType.RESPONSE, answer=text))]
    )


def _records(manager) -> list:
    return sorted(manager.execution_state.turn_action_outbox.values(), key=lambda item: (item.created_at, item.action_index))


def _assert_held_to_run_again(manager, agent, task_id: str, workflow_id: str) -> None:
    """The answer was not sent and the task waits in the queue to run again with the workflow note."""

    info = manager.get_task_info(task_id)
    assert info.state == ProgressState.PARTIAL
    assert list(manager.task_state.task_queue) == [task_id]
    note = manager.read_runtime_resume_directive(task_id) or ""
    assert CONTINUATION_NOTE in note and workflow_id in note
    agent.team_comms.send_message.assert_not_awaited()
    assert _records(manager)[-1].status == "held_back"
    assert "source_task_reconciliation_requested" in _event_codes(manager, workflow_id)


def test_a_final_answer_held_for_open_workflow_steps_queues_the_task_to_run_again(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    workflow = _open_workflow(manager, parent.task_id)
    info = manager.get_task_info(parent.task_id)

    result = asyncio.run(
        agent._execute_turn_decision_outbox(
            decision=_final_answer("Here are the prices."),
            incoming_content=parent,
            incoming_header=info.header,
            incoming_meta=info.meta,
        )
    )
    assert isinstance(result, Task)
    assert (result.task_type, result.task_id) == (MessageMetaType.REQUEST, parent.task_id)

    asyncio.run(task_message_runtime._apply_task_processing_result(agent, parent.task_id, info, result))

    _assert_held_to_run_again(manager, agent, parent.task_id, workflow.workflow_id)
    held = [record for record in caplog.records if "event=task_answer_held" in record.getMessage()]
    assert len(held) == 1 and "state=partial" in held[0].getMessage()
    assert manager.get_next_task_to_process() == parent.task_id


def test_a_final_answer_held_in_a_resumed_turn_also_queues_the_task_to_run_again(tmp_path) -> None:
    """The turn resumed after an approval ends through the same end-of-turn handling."""

    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    workflow = _open_workflow(manager, parent.task_id)
    info = manager.get_task_info(parent.task_id)
    resumed_model = AsyncMock(
        return_value=SimpleNamespace(stop_reason="end_turn", structured_output=_final_answer("Set up; here are the prices."))
    )
    session = task_steering_runtime.StrandsInterruptedTurnSession(
        task_id=parent.task_id,
        strands=strands_parts(SimpleNamespace(invoke_async=resumed_model)),
        invoke_kwargs={},
        callback_handler=lambda **_: None,
        full_prompt="",
        msg_content=parent,
        msg_header=info.header,
        msg_meta=info.meta,
        created_at=_now(),
        interrupts=[],
    )
    agent._interrupted_turn_sessions[parent.task_id] = session
    steering = TaskSteering(steering_type=TaskSteeringType.INTERRUPT_RESPONSE, target_task_id=parent.task_id)

    result = asyncio.run(
        task_steering_runtime._run_resumed_turn(
            agent, session, steering, info, [{"interruptResponse": {}}], ToolStop(agent_id=agent.id, task_id=parent.task_id)
        )
    )

    resumed_model.assert_awaited_once()
    assert isinstance(result, Task) and result.task_id == parent.task_id
    _assert_held_to_run_again(manager, agent, parent.task_id, workflow.workflow_id)
    assert parent.task_id not in agent._interrupted_turn_sessions
    assert manager.get_next_task_to_process() == parent.task_id


def test_a_held_answer_whose_hold_saved_no_state_is_reported(tmp_path, caplog) -> None:
    """Every hold saves the task's state before returning; one that does not is logged, not hidden."""

    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    info = manager.get_task_info(parent.task_id)
    held = Task(task_id=parent.task_id, task_type=MessageMetaType.REQUEST, problem_statement="Held.")

    asyncio.run(task_message_runtime._apply_task_processing_result(agent, parent.task_id, info, held))

    assert manager.get_task_info(parent.task_id).state == ProgressState.IN_PROGRESS
    warnings = [record for record in caplog.records if "event=held_answer_state_not_saved" in record.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert f"task_id={parent.task_id}" in warnings[0].getMessage() and "state=in_progress" in warnings[0].getMessage()
