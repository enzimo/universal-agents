"""A workflow's batch of approvals, the pause around it, and the completion guard.

Real Cedar, SQLite and TaskManager. Covers: the batch tool submits one change
request per staged change (each linked to its step) and pauses once, listing
every request; the task resumes only after the last of them is decided, from
a live turn or after a restart; ``apply_change`` applies each approved change
found by its change id, once; a denied change is reported, closes its step and
is never asked for again; the batch is refused before anything is asked when
a change has no running step; and a task cannot finish while its workflow
has unfinished steps.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from strands.interrupt import InterruptException

from access_control.inbox import StoredAccessInbox
from access_control.requests import AccessRequests
from tests.task_work_support import bind_turn
from custom_agents import access_request_runtime, access_runtime, task_steering_runtime
from custom_agents.access_workflow_runtime import defer_workflow_completion
from custom_agents.access_workflows import workflow_service_for
from custom_agents.task_human_interaction_blockers import (
    ACCESS_REQUEST_BLOCKER,
    clear_answered_access_blocker,
    get_human_interaction_blocker,
)
from custom_messages.contracts.access_v2 import (
    AccessRequestResolvedV1,
    AccessRequestStatus,
    AccessReviewChoice,
    ChangeApprovalMode,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import ProgressState
from custom_tools.providers import access_workflows as tools
from custom_tools.providers.changes import apply_change
from tests.access_workflow_support import (
    OWNER,
    TEAM,
    StagedChanges,
    ToolCall,
    make_agent,
    needs_cedar,
    proposal,
    run_tool,
    use_signed_contexts,
    use_staged_changes,
)

pytestmark = needs_cedar


@pytest.fixture
def changes(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    specialists = StagedChanges("agent_definition", "specialist_test_")
    restarts = StagedChanges("runtime_restart", "runtime_restart_test_")
    use_staged_changes(monkeypatch, specialists, restarts)
    yield specialists, restarts
    access_runtime._runtime_access.cache_clear()


@pytest.fixture
def lead(tmp_path: Path):
    agent = make_agent(tmp_path)
    # What the steering runtime needs to record an interrupt.
    agent.mid_turn_steering = {}
    agent._agent_id_by_name = {}
    agent.named_agent_type_queue_map = {}
    agent._logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    agent.team_comms = SimpleNamespace(send_message=AsyncMock())
    return agent


def running_workflow(agent) -> str:
    """A published plan with the specialist and restart steps running."""

    flow = run_tool(agent, tools.submit_access_workflow, proposal(), "weekly-report")
    for step, state in (("account", "complete"), ("specialist", "running"), ("restart", "running")):
        run_tool(agent, tools.update_access_workflow, flow["workflow_id"], {"step_id": step, "state": state, "summary": "ok"})
    return flow["workflow_id"]


def people_side(agent) -> AccessRequests:
    service = access_runtime.runtime_access(agent).service
    return AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type="TeamArchitect")


def store(agent):
    return access_runtime.runtime_access(agent).service.store


def batch(agent, workflow_id: str, change_ids: list[str], call: ToolCall) -> dict:
    return run_tool(agent, tools.request_access_workflow_approvals, workflow_id, change_ids, "Set up the crawler", call)


def apply(agent, change_id: str, tool_use_id: str) -> dict:
    return run_tool(agent, apply_change, change_id, "Apply the approved change", ToolCall(tool_use_id, "apply_change"))


def start_batch(agent, changes) -> tuple[str, list[str], ToolCall, InterruptException]:
    specialists, restarts = changes
    workflow_id = running_workflow(agent)
    ids = [specialists.stage("crawler"), restarts.stage("team")]
    call = ToolCall()
    with pytest.raises(InterruptException) as raised:
        batch(agent, workflow_id, ids, call)
    return workflow_id, ids, call, raised.value


def test_a_batch_asks_once_per_change_links_each_to_its_step_and_pauses_once(lead, changes) -> None:
    workflow_id, ids, call, _raised = start_batch(lead, changes)

    [reason] = call.reasons  # one pause
    requests = store(lead).list_requests(TEAM)
    assert len(requests) == 2 and all(item.status is AccessRequestStatus.WAITING for item in requests)
    assert sorted(reason["access_request_ids"]) == sorted(item.request_id for item in requests)
    by_change = {item.change.change_id: item for item in requests}
    assert {item.change.plan_digest for item in requests} == {
        changes[0].describe(lead, ids[0]).plan_digest, changes[1].describe(lead, ids[1]).plan_digest,
    }
    record = workflow_service_for(lead).repository.get(workflow_id)
    assert record.step("specialist").request_ids == (by_change[ids[0]].request_id,)
    assert record.step("restart").request_ids == (by_change[ids[1]].request_id,)

    # The resumed call finds its own requests instead of asking again.
    for item in requests:
        people_side(lead).decide(item.request_id, OWNER, AccessReviewChoice.APPROVE)
    call.response = {"kind": "access_request_decision"}
    result = batch(lead, workflow_id, ids, call)
    assert [entry["status"] for entry in result["results"]] == ["granted", "granted"]
    assert len(store(lead).list_requests(TEAM)) == 2


def test_apply_change_applies_each_approved_change_found_by_its_change_id_once(lead, changes) -> None:
    specialists, restarts = changes
    workflow_id, ids, call, _raised = start_batch(lead, changes)
    by_change = {item.change.change_id: item for item in store(lead).list_requests(TEAM)}
    for item in by_change.values():
        people_side(lead).decide(item.request_id, OWNER, AccessReviewChoice.APPROVE)

    applied = apply(lead, ids[0], "tooluse-apply-1")

    assert applied["status"] == "applied"
    approval = specialists.applied[ids[0]]
    assert approval.mode is ChangeApprovalMode.PERSON_APPROVAL
    assert approval.request_id == by_change[ids[0]].request_id and approval.approved_by == OWNER.person_id
    assert store(lead).get_request(by_change[ids[0]].request_id).applied_at_utc is not None
    again = apply(lead, ids[0], "tooluse-apply-2")
    assert again["status"] == "refused" and "already applied" in again["reason"]
    assert apply(lead, ids[1], "tooluse-apply-3")["status"] == "applied"
    assert len(store(lead).list_requests(TEAM)) == 2, "no change was asked for again"
    # With every request approved and applied the steps can be finished.
    for step in ("specialist", "restart"):
        done = run_tool(lead, tools.update_access_workflow, workflow_id, {"step_id": step, "state": "complete", "summary": "Applied"})
        assert "phase" in done, done


def test_a_denied_change_is_reported_closes_its_step_and_is_not_asked_for_again(lead, changes) -> None:
    specialists, _restarts = changes
    workflow_id, ids, call, _raised = start_batch(lead, changes)
    by_change = {item.change.change_id: item for item in store(lead).list_requests(TEAM)}
    people_side(lead).decide(by_change[ids[0]].request_id, OWNER, AccessReviewChoice.DENY)
    people_side(lead).decide(by_change[ids[1]].request_id, OWNER, AccessReviewChoice.APPROVE)
    call.response = {"kind": "access_request_decision"}

    result = batch(lead, workflow_id, ids, call)

    statuses = {entry["change_id"]: entry["status"] for entry in result["results"]}
    assert statuses == {ids[0]: "denied", ids[1]: "granted"}
    record = workflow_service_for(lead).repository.get(workflow_id)
    assert record.step("specialist").blocked_by_person and record.step("specialist").state.value == "failed"
    denied = apply(lead, ids[0], "tooluse-apply-denied")
    assert denied["status"] == "denied" and "Do not apply it" in denied["message"]
    assert ids[0] not in specialists.applied
    retry = batch(lead, workflow_id, [specialists.stage("again")], ToolCall("tooluse-batch-2"))
    assert retry["status"] == "refused" and "denied" in retry["reason"]
    assert len(store(lead).list_requests(TEAM)) == 2


def test_a_batch_is_refused_before_asking_when_a_change_has_no_running_step(lead, changes) -> None:
    specialists, restarts = changes
    flow = run_tool(lead, tools.submit_access_workflow, proposal(), "weekly-report")
    run_tool(lead, tools.update_access_workflow, flow["workflow_id"], {"step_id": "account", "state": "complete", "summary": "ok"})
    run_tool(lead, tools.update_access_workflow, flow["workflow_id"], {"step_id": "specialist", "state": "running", "summary": "ok"})

    refused = batch(lead, flow["workflow_id"], [specialists.stage("crawler"), restarts.stage("team")], ToolCall())

    assert refused["status"] == "refused" and "No running step of this workflow expects runtime_restart" in refused["reason"]
    assert store(lead).list_requests(TEAM) == [], "nothing was asked"


@pytest.mark.asyncio
async def test_the_task_resumes_only_after_every_request_of_the_batch_is_decided(lead, changes) -> None:
    _workflow_id, _ids, call, raised = start_batch(lead, changes)
    manager = lead.task_manager
    info = manager.get_task_info("task-lead")
    bind_turn(info.task.task_id)
    await task_steering_runtime.send_interrupt_request_for_result(
        lead, result=SimpleNamespace(interrupts=[raised.interrupt]), task=info.task, msg_header=info.header,
        msg_meta=info.meta, invoke_kwargs={}, callback_handler=lambda **_: None, full_prompt="",
    )
    blocker = get_human_interaction_blocker(manager, "task-lead")
    first, second = call.reasons[0]["access_request_ids"]
    assert blocker["kind"] == ACCESS_REQUEST_BLOCKER and blocker["access_request_ids"] == [first, second]

    steered = AsyncMock()
    original = task_steering_runtime.handle_task_steering_message
    task_steering_runtime.handle_task_steering_message = steered
    try:
        for request_id in (first, second):
            people_side(lead).decide(request_id, OWNER, AccessReviewChoice.APPROVE)
            await access_request_runtime.handle_access_request_resolved(
                lead, message=AccessRequestResolvedV1(message_id=f"m-{request_id}", team_id=TEAM, request_id=request_id),
                msg_header=MessageHeader(), msg_meta=MessageMeta(),
            )
            if request_id == first:
                steered.assert_not_awaited()
    finally:
        task_steering_runtime.handle_task_steering_message = original
    steered.assert_awaited_once()
    steering = steered.await_args.kwargs["steering"]
    assert steering.idempotency_key == f"access_resume:{raised.interrupt.id}"
    answered = [(item["interrupt_id"], item["response"]["access_request_id"]) for item in steering.interrupt_responses]
    assert clear_answered_access_blocker(manager, "task-lead", answered)
    assert get_human_interaction_blocker(manager, "task-lead") is None


@pytest.mark.asyncio
async def test_after_a_restart_the_task_resumes_from_a_directive_once_the_batch_is_decided(lead, changes) -> None:
    _workflow_id, _ids, call, raised = start_batch(lead, changes)
    manager = lead.task_manager
    info = manager.get_task_info("task-lead")
    bind_turn(info.task.task_id)
    await task_steering_runtime.send_interrupt_request_for_result(
        lead, result=SimpleNamespace(interrupts=[raised.interrupt]), task=info.task, msg_header=info.header,
        msg_meta=info.meta, invoke_kwargs={}, callback_handler=lambda **_: None, full_prompt="",
    )
    lead._interrupted_turn_sessions.clear()  # the paused turn is gone after a restart
    first, second = call.reasons[0]["access_request_ids"]

    people_side(lead).decide(first, OWNER, AccessReviewChoice.APPROVE)
    await access_request_runtime.handle_access_request_resolved(
        lead, message=AccessRequestResolvedV1(message_id="m1", team_id=TEAM, request_id=first),
        msg_header=MessageHeader(), msg_meta=MessageMeta(),
    )
    assert get_human_interaction_blocker(manager, "task-lead") is not None
    assert manager.get_task_info("task-lead").state == ProgressState.WAITING

    people_side(lead).decide(second, OWNER, AccessReviewChoice.DENY)
    await access_request_runtime.handle_access_request_resolved(
        lead, message=AccessRequestResolvedV1(message_id="m2", team_id=TEAM, request_id=second),
        msg_header=MessageHeader(), msg_meta=MessageMeta(),
    )
    assert get_human_interaction_blocker(manager, "task-lead") is None
    assert manager.get_task_info("task-lead").state == ProgressState.ACCEPTED
    directive = manager.task_state.runtime_resume_directives["task-lead"]
    assert "apply_change" in json.dumps(directive) and first in json.dumps(directive)
    await asyncio.wait_for(lead._pending_work_task, 2)  # the retained drain runs the task
    lead._continue_pending_work.assert_awaited()


def test_a_task_cannot_finish_while_its_workflow_has_unfinished_steps(lead, changes) -> None:
    workflow_id = running_workflow(lead)
    manager = lead.task_manager

    assert defer_workflow_completion(manager, task_id="task-lead", team_id=TEAM) is True

    assert manager.get_task_info("task-lead").state == ProgressState.ACCEPTED
    assert "get_access_workflow" in json.dumps(manager.task_state.runtime_resume_directives["task-lead"])
    with pytest.raises(RuntimeError, match="still has unfinished steps"):
        defer_workflow_completion(manager, task_id="task-lead", team_id=TEAM)
    codes = [event.code for event in workflow_service_for(lead).repository.events(workflow_id, limit=50)]
    assert "source_task_reconciliation_requested" in codes and "source_task_completion_rejected" in codes
    assert defer_workflow_completion(manager, task_id="task-other", team_id=TEAM) is False


def test_holding_a_task_for_its_workflow_keeps_the_restart_note_it_has_not_read(lead, changes) -> None:
    """A final answer processed after a restart must not erase what recovery told the task.

    Startup recovery records the restart the task asked for before the
    scheduler drains final answers saved before the restart; holding that
    answer back for the workflow adds its own note instead of replacing it.
    """

    running_workflow(lead)
    manager = lead.task_manager
    restart_note = "The manager restarted for the restart this task asked for (change_restart_1). It is done."
    manager.append_runtime_resume_directive("task-lead", restart_note)

    assert defer_workflow_completion(manager, task_id="task-lead", team_id=TEAM) is True

    directive = manager.read_runtime_resume_directive("task-lead")
    assert restart_note in directive
    assert "get_access_workflow" in directive
