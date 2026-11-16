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
import dataclasses
import itertools
import json
import logging
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import task_message_runtime, task_steering_runtime
from custom_agents.access_workflows import AccessWorkflowService
from custom_agents.access_workflow_runtime import (
    LAST_REPLY_LABEL,
    WORKFLOW_CONTINUATION_NOTE,
    WorkflowCompletionRefused,
)
from custom_agents.pending_work_runtime import close_pending_work
from custom_agents.structured_llm_messages import (
    SecureCredentialCollectionAction,
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TaskResponseAction,
)
from custom_messages.contracts.credentials_v1 import SecureCredentialCollectionRequest
from custom_messages.contracts.access_workflow_v2 import (
    AccessWorkflowV2,
    WorkflowEventV2,
    WorkflowPhase,
    WorkflowProgressV2,
    WorkflowStepState,
    WorkflowStepV2,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskSteering
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType, TaskSteeringType
from custom_tools.tool_stop import ToolStop
from storage.access_workflow_repository import AccessWorkflowRepository
from runtime_hosting.agent_execution import MAIN_LANE
from tests.task_work_support import strands_parts
from tests.test_runtime_owner_boundaries import build_turn_owner
from tests.test_turn_action_outbox import (
    _add_active_parent_task,
    _delegation,
    _make_agent,
    _make_task_manager,
    _now,
)

OWNER = "person-1"


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
        owner_person_id=OWNER,
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


def _diagnostics(manager, workflow_id: str) -> dict:
    service = AccessWorkflowService(AccessWorkflowRepository(manager.storage_settings))
    return service.diagnostics(service.repository.get(workflow_id))


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
    assert WORKFLOW_CONTINUATION_NOTE in note and workflow_id in note
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


# -- the other holds keep their waits (A2) --


def _with_pending_query(manager, task_id: str) -> None:
    manager.add_task_query(
        task_id, TaskQuery(query_type=MessageMetaType.REQUEST, parent_task_id=task_id, message="Which registrar?")
    )


def _with_active_plan(manager, task_id: str) -> None:
    plan = manager.activate_plan_execution(
        task_id,
        steps=[{"title": "Check the prices", "objective": "Look up the domain prices."}],
        final_review=False,
        agent_name="manager",
        agent_id=manager.identity.owner_agent_id,
    )
    assert plan is not None and manager.is_plan_execution_root_active(task_id)


def _history_marks(manager, task_id: str) -> list[str]:
    """The events, plan actions and reasons the task's history records, in order."""

    marks = []
    for entry in manager.get_task_info(task_id).task.execution_history:
        details = entry.details or {}
        mark = details.get("event") or details.get("action") or details.get("reason")
        if mark:
            marks.append(str(mark))
    return marks


@pytest.mark.parametrize(
    ("setup", "actions", "hold"),
    [
        pytest.param(None, ["delegate", "answer"], "terminal_task_response_deferred", id="answer-with-pending-delegated-work"),
        pytest.param(_with_pending_query, ["answer"], "terminal_task_response_deferred", id="answer-with-unanswered-task-query"),
        pytest.param(_with_active_plan, ["answer"], "defer_root_response_for_plan_execution", id="answer-during-plan-execution"),
        pytest.param(_with_active_plan, ["delegate"], "suppress_post_activation_plan_bypass", id="delegation-after-plan-activation"),
        pytest.param(None, ["delegate"], None, id="plain-delegation"),
    ],
)
def test_holds_for_other_work_and_delegation_still_leave_the_task_waiting(tmp_path, setup, actions, hold) -> None:
    """Each hold below saves WAITING itself; a plain delegation's parent waits through the end-of-turn handling."""
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    if setup is not None:
        setup(manager, parent.task_id)
    built = {"delegate": lambda: _delegation("Look up the domain prices."), "answer": lambda: _final_answer("Done.").actions[0]}
    info = manager.get_task_info(parent.task_id)

    result = asyncio.run(
        agent._execute_turn_decision_outbox(
            decision=StructuredAgentTurnDecision(actions=[built[name]() for name in actions]),
            incoming_content=parent,
            incoming_header=info.header,
            incoming_meta=info.meta,
        )
    )
    asyncio.run(task_message_runtime._apply_task_processing_result(agent, parent.task_id, info, result))

    assert isinstance(result, Task) and result.task_type == MessageMetaType.REQUEST
    # A held answer names its own task; a delegation names its child.
    assert (result.task_id == parent.task_id) is (hold is not None)
    if hold is not None:
        assert hold in _history_marks(manager, parent.task_id)
    assert manager.get_task_info(parent.task_id).state == ProgressState.WAITING
    assert parent.task_id not in manager.task_state.task_queue
    sent = [call.kwargs["message"].msg_content for call in agent.team_comms.send_message.await_args_list]
    assert not any(isinstance(item, Task) and item.task_type == MessageMetaType.RESPONSE for item in sent)


# -- the queue runs the held task again (intent) --


def _queued_request(manager) -> Task:
    """A person's request, queued and not started yet."""

    task = Task(task_id="task_prices", task_type=MessageMetaType.REQUEST, problem_statement="Check domain prices.")
    header = MessageHeader(from_agent_id="user_agent", response_routing_key="user_reply")
    meta = MessageMeta(
        from_agent_name="user",
        requester_type=RequesterType.USER_REQUESTER,
        is_response_requested=True,
        msg_correlation_id="corr_prices",
        conversation_id="conv_prices",
    )
    assert manager.add_task(task, header, meta, owner_person_id=OWNER)
    return task


def _queue_agent(tmp_path, manager, turns, **agent_settings):
    """An agent whose queue loop runs ``turns`` in order; each one decides through the real outbox."""

    agent = _make_agent(tmp_path, task_manager=manager, **agent_settings)
    agent._conversation_store = None
    object.__setattr__(agent, "_refresh_self_agent_state", AsyncMock())
    object.__setattr__(agent, "_notify_context_after_turn", AsyncMock())
    pending = list(turns)

    async def process_llm_interaction(*, msg_content, msg_header, msg_meta):
        decision = await pending.pop(0)(agent, msg_content)
        return await agent._execute_turn_decision_outbox(
            decision=decision, incoming_content=msg_content, incoming_header=msg_header, incoming_meta=msg_meta
        )

    object.__setattr__(agent, "process_llm_interaction", process_llm_interaction)
    return agent


def _sent_tasks(agent) -> list[Task]:
    return [
        call.kwargs["message"].msg_content
        for call in agent.team_comms.send_message.await_args_list
        if isinstance(call.kwargs["message"].msg_content, Task)
    ]


def test_the_queue_runs_the_held_task_again_with_its_note_and_the_guidance_sent_meanwhile(tmp_path) -> None:
    manager = _make_task_manager(tmp_path)
    task = _queued_request(manager)
    workflow = _open_workflow(manager, task.task_id)
    second_turn: dict[str, str] = {}

    async def first(_agent, _content):
        return _final_answer("Here are the prices from the first registrar.")

    async def second(agent, content):
        # What every turn start reads (custom_agents.sdk.strands.turn): the note and the queued guidance.
        second_turn["task_id"] = content.task_id
        second_turn["note"] = manager.read_runtime_resume_directive(content.task_id) or ""
        second_turn["guidance"] = await task_steering_runtime.consume_pending_proactive_guidance_message(
            agent, content.task_id
        )
        service = AccessWorkflowService(AccessWorkflowRepository(manager.storage_settings))
        service.progress(
            service.repository.get(workflow.workflow_id),
            WorkflowProgressV2(step_id="do_the_work", state=WorkflowStepState.COMPLETE, summary="Reported the prices."),
        )
        return _final_answer("Here are the prices from both registrars.")

    agent = _queue_agent(tmp_path, manager, [first, second])

    async def scenario() -> None:
        assert await agent._check_and_process_next_task()
        _assert_held_to_run_again(manager, agent, task.task_id, workflow.workflow_id)
        await task_steering_runtime.handle_task_steering_message(
            agent,
            TaskSteering(
                steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
                target_task_id=task.task_id,
                guidance_text="Also check the price of the .test domain.",
                idempotency_key="guidance_prices",
            ),
            MessageHeader(from_agent_id="gateway"),
            MessageMeta(msg_correlation_id="corr_prices", conversation_id="conv_prices"),
        )
        assert await agent._check_and_process_next_task()  # the same task, at once
        assert not await agent._check_and_process_next_task()

    asyncio.run(scenario())

    assert second_turn["task_id"] == task.task_id
    assert WORKFLOW_CONTINUATION_NOTE in second_turn["note"] and workflow.workflow_id in second_turn["note"]
    assert "Also check the price of the .test domain." in second_turn["guidance"]
    answers = [item for item in _sent_tasks(agent) if item.task_type == MessageMetaType.RESPONSE]
    assert [item.answer for item in answers] == ["Here are the prices from both registrars."]
    assert manager.get_task_info(task.task_id) is None  # finished and archived
    saved = manager.get_task_from_db(task.task_id, owner_agent_id=agent.id)
    assert saved["main_task"].task_state[-1][0] == ProgressState.COMPLETED


# -- a repeated answer without progress fails the task and tells the person (A4) --


def test_a_second_answer_without_progress_fails_the_task_and_tells_the_person(tmp_path, caplog) -> None:
    """Before, the refusal went to model recovery, and each recovery turn made a new batch: no limit ever stopped it."""

    manager = _make_task_manager(tmp_path)
    task = _queued_request(manager)
    workflow = _open_workflow(manager, task.task_id)
    last_reply = "Here are the prices from the first registrar.\n\nI still need the second registrar's account ID."

    async def first(_agent, _content):
        return _final_answer("Here are the prices from the first registrar.")

    async def second(_agent, _content):
        return _final_answer(last_reply)  # nothing reported on the open step

    agent = _queue_agent(tmp_path, manager, [first, second])

    async def scenario() -> None:
        assert await agent._check_and_process_next_task()
        assert await agent._check_and_process_next_task()
        assert not await agent._check_and_process_next_task()  # nothing runs it again

    asyncio.run(scenario())

    # The person reads the plain statement first, then the labelled last reply.
    [message] = _sent_tasks(agent)
    assert message.task_type == MessageMetaType.RESPONSE and message.task_state[-1][0] == ProgressState.FAILED
    statement, _, rest = message.answer.partition("\n\n")
    assert statement.startswith('This task was stopped because its plan "Check domain prices" still has unfinished steps')
    assert "Reply here to go over the remaining work with the team lead" in statement
    assert rest == (
        f"{LAST_REPLY_LABEL}\n\n"
        "> Here are the prices from the first registrar.\n>\n> I still need the second registrar's account ID."
    )
    # The task failed, gave back the main slot and left nothing to retry.
    assert manager.get_task_info(task.task_id) is None
    saved = manager.get_task_from_db(task.task_id, owner_agent_id=agent.id)["main_task"]
    assert saved.task_state[-1][0] == ProgressState.FAILED
    assert not manager.task_state.task_queue and manager.can_start_new_task()
    # The refused action failed for good; no model recovery or automatic retry was tried.
    summaries = [entry.summary for entry in saved.execution_history]
    assert any("was refused for good" in summary for summary in summaries)
    assert not any("needs model recovery" in summary or "automatic retry" in summary for summary in summaries)
    assert "source_task_completion_rejected" in _event_codes(manager, workflow.workflow_id)
    # Both held answers can be recovered from the task's history.
    previews = {
        entry.details.get("event"): entry.details.get("attempted_answer_preview")
        for entry in saved.execution_history
        if entry.details and "attempted_answer_preview" in entry.details
    }
    assert "first registrar" in previews["workflow_completion_deferred"]
    assert "account ID" in previews["workflow_completion_refused"]
    # The workflow's diagnostic file says what happened, and never quotes the answers.
    diagnostics = _diagnostics(manager, workflow.workflow_id)
    explanations = {event["code"]: event["explanation"] for event in diagnostics["events"]}
    assert explanations["source_task_completion_rejected"] == (
        "The task tried to finish again without changing the unfinished steps. "
        "It was stopped as failed, and the person was told."
    )
    lifecycle = {event["code"] for event in diagnostics["source_task_history"]["events"]}
    assert {"workflow_completion_deferred", "workflow_completion_refused"} <= lifecycle
    assert "registrar" not in json.dumps(diagnostics)
    for event in ("event=workflow_completion_refused", "event=turn_action_refused"):
        [record] = [record for record in caplog.records if event in record.getMessage()]
        assert record.levelno == logging.WARNING
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_a_refused_answer_ends_the_turn_as_a_reported_failure_not_a_model_error(caplog) -> None:
    """The turn owner hands the refusal on at once; it is not a provider problem or an unexpected error."""

    async def invoke(_prompt, **_kwargs):
        return decided

    executor, decided = build_turn_owner(invoke=invoke, guidance=deque(["", ""]), committed=[], cleared=[])
    refusal = WorkflowCompletionRefused(
        workflow_id="access_flow_1", task_id="task_prices", title="Check domain prices", attempted_answer="Done."
    )

    async def refuse(**_kwargs):
        raise refusal

    executor.effects = dataclasses.replace(executor.effects, execute_turn_decision_outbox=refuse)
    task = Task(task_id="task_prices", task_type=MessageMetaType.REQUEST, problem_statement="Check domain prices.")

    with pytest.raises(WorkflowCompletionRefused) as raised:
        await executor.process_llm_interaction(task, MessageHeader(), MessageMeta())

    assert raised.value is refusal
    assert not any("Error in LLM interaction" in record.getMessage() for record in caplog.records)


def test_a_repeated_answer_replayed_by_the_outbox_retry_fails_the_task_and_frees_the_queue(tmp_path, caplog) -> None:
    """The outbox's retry of a batch also runs its final answer (not only a turn does).

    The continuation turn's first action (a credential form) failed to publish,
    so the task waited RETRYING (it keeps the main slot) with the final answer
    blocked behind it. The retry sends the form and then runs the answer; a
    refusal there must end the task as in a turn. Re-raised, it left the task
    RETRYING with nothing to retry, the queue stopped and nobody was told.
    """

    manager = _make_task_manager(tmp_path)
    task = _queued_request(manager)
    workflow = _open_workflow(manager, task.task_id)
    publishes = itertools.count(1)

    async def publish(**_kwargs):
        number = next(publishes)
        if number == 1:
            raise TimeoutError("broker publish timed out")
        return number

    async def first(_agent, _content):
        return _final_answer("Here are the prices from the first registrar.")

    async def second(_agent, content):
        form = SecureCredentialCollectionAction(
            secure_credential_collection_request=SecureCredentialCollectionRequest(
                parent_task_id=content.task_id,
                service_name="Example registrar",
                credential_key="registrar_api_key",
                purpose="Read domain prices.",
            )
        )
        return StructuredAgentTurnDecision(actions=[form, *_final_answer("Here are the prices.").actions])

    started: list[str] = []

    async def next_request(_agent, content):
        started.append(content.task_id)
        return _final_answer("Done.")

    retry_at_once = {"max_auto_attempts": 3, "initial_retry_delay_seconds": 0.0, "max_retry_delay_seconds": 0.0,
                     "retry_jitter_ratio": 0.0}
    agent = _queue_agent(tmp_path, manager, [first, second, next_request], turn_action_outbox=retry_at_once)
    agent.team_comms.send_message = AsyncMock(side_effect=publish)
    object.__setattr__(agent, "_continue_pending_work", AsyncMock())  # the test drives the drain itself

    async def scenario() -> None:
        assert await agent._check_and_process_next_task()  # held
        assert await agent._check_and_process_next_task()  # its first action waits for a retry
        assert manager.get_task_info(task.task_id).state == ProgressState.RETRYING
        assert await agent._drain_due_turn_action_outbox(MAIN_LANE)
        other = Task(task_id="task_next", task_type=MessageMetaType.REQUEST, problem_statement="Next request.")
        assert manager.add_task(other, MessageHeader(response_routing_key="user_reply"), MessageMeta(), owner_person_id=None)
        assert await agent._check_and_process_next_task()  # the queue goes on
        await close_pending_work(agent)

    asyncio.run(scenario())

    saved = manager.get_task_from_db(task.task_id, owner_agent_id=agent.id)["main_task"]
    assert saved.task_state[-1][0] == ProgressState.FAILED
    assert started == ["task_next"]
    failures = [item for item in _sent_tasks(agent) if item.task_id == task.task_id]
    assert len(failures) == 1 and failures[0].task_state[-1][0] == ProgressState.FAILED
    assert failures[0].answer.startswith('This task was stopped because its plan "Check domain prices"')
    assert "source_task_completion_rejected" in _event_codes(manager, workflow.workflow_id)
    assert any("event=turn_action_refused" in record.getMessage() for record in caplog.records)
