"""A worker's report to the team lead gets no reply and does not hold the worker's task.

custom_agents.worker_result_reports: a ``worker_result`` escalation is its own
task for TeamArchitect (source parent, no response requested) with trusted source
fields, and the worker's batch must end with its own final answer.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tests.task_work_support import bind_task
from custom_agents.structured_llm_messages import (
    WORKER_RESULT_REQUIRES_RESPONSE,
    ModelWorkerTurnDecision,
    StructuredAgentTurnDecision,
)
from custom_agents.task_manager import TaskManager
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_agents.worker_result_reports import (
    REPORT_SENT_EVENT,
    is_worker_result_report,
    prepare_worker_result_report,
    report_task_id,
)
from custom_messages.contracts.envelope_v1 import CommsMessage
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    ManagerEscalationContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts

REPORT = "Audit findings: linked the home page to five pages."


def _decision(*actions: dict) -> dict:
    return {"actions": list(actions)}


def _report_action(escalation_type: str = "worker_result") -> dict:
    return {
        "action_type": "manager_escalation",
        "task": {"problem_statement": REPORT},
        "escalation": {"escalation_type": escalation_type},
    }


_RESPONSE = {"action_type": "task_response", "task": {"answer": "done"}, "suppress_outbound_dispatch": True}


def test_a_report_must_end_with_the_workers_own_answer():
    with pytest.raises(ValidationError, match="worker_result is a report"):
        ModelWorkerTurnDecision.model_validate(_decision(_report_action()))

    decision = ModelWorkerTurnDecision.model_validate(_decision(_report_action(), _RESPONSE)).to_runtime_decision()

    report, _ = decision.actions
    assert report.manager_escalation.escalation_type == "worker_result"
    assert report.target_named_agent_type == TEAM_ARCHITECT_AGENT_TYPE
    # The runtime contract applies the same rule (a decision built elsewhere).
    with pytest.raises(ValidationError):
        StructuredAgentTurnDecision(actions=[report])
    assert "end the same action batch" in WORKER_RESULT_REQUIRES_RESPONSE


def test_other_escalations_still_wait_for_the_reply():
    from custom_agents.turn_work_notes import decision_starts_wait

    decision = ModelWorkerTurnDecision.model_validate(_decision(_report_action("needs_user_input"))).to_runtime_decision()
    report = ModelWorkerTurnDecision.model_validate(_decision(_report_action(), _RESPONSE)).to_runtime_decision()

    assert decision_starts_wait(decision)
    assert not decision_starts_wait(report)


def test_a_delegation_has_no_option_to_skip_the_reply():
    """Every delegated task answers its requester; the model cannot ask otherwise."""

    with pytest.raises(ValidationError, match="is_response_requested"):
        ModelWorkerTurnDecision.model_validate(
            _decision(
                {
                    "action_type": "delegate_task",
                    "task": {"problem_statement": "Look this up."},
                    "target_named_agent_type": "WebResearchAgent",
                    "is_response_requested": False,
                }
            )
        )


def _worker(tmp_path) -> SimpleNamespace:
    manager = TaskManager(db_path=str(tmp_path / "worker.db"))
    manager.set_owner_identity("worker_1", "curator", "team", "KnowledgeCurator")
    context = GatewayContext(canonical_user_id="person_1", scheduled_job_id="schedule_audit")
    manager.add_task(
        Task(task_id="run_1", task_type=MessageMetaType.REQUEST, problem_statement="Weekly audit."),
        MessageHeader(from_agent_id="scheduler"),
        MessageMeta(
            requester_type=RequesterType.TIMER_REQUESTER,
            conversation_id="conversation_1",
            msg_correlation_id="corr_1",
            request_timezone="America/Chicago",
            gateway_context=context,
        ),
        owner_person_id="person_1",
    )
    manager.update_main_task_state("run_1", ProgressState.IN_PROGRESS, "2026-10-19T14:00:00+00:00")
    manager.operations_runtime = TaskOperationRuntime()
    return SimpleNamespace(
        id="worker_1",
        name="curator",
        named_agent_type="KnowledgeCurator",
        task_manager=manager,
        _logger=logging.getLogger("worker-report-test"),
    )


def test_the_report_is_its_own_task_for_the_lead_with_trusted_source_fields(tmp_path):
    agent = _worker(tmp_path)
    # The model claims another source; trusted code replaces it.
    model_escalation = ManagerEscalationContext(
        escalation_type="worker_result", source_task_id="someone_elses_task", source_agent_id="mallory"
    )
    comms = CommsMessage(
        msg_content=Task(task_type=MessageMetaType.REQUEST, problem_statement=REPORT),
        msg_meta=MessageMeta(to_named_agent_type=TEAM_ARCHITECT_AGENT_TYPE, manager_escalation=model_escalation),
    )
    outbox_header = MessageHeader(from_agent_id="worker_1", to_routing_path="team_TeamArchitect")

    task, header, meta = asyncio.run(
        prepare_worker_result_report(
            agent,
            comms_message=comms,
            parent_task_info=agent.task_manager.get_task_info("run_1"),
            header=outbox_header,
            record_id="action_7",
        )
    )

    assert task.task_id == report_task_id("action_7")
    assert task.parent_task_id == "run_1" and task.problem_statement == REPORT
    assert (header.to_routing_path, header.response_routing_key) == ("team_TeamArchitect", "worker_1")
    assert meta.is_response_requested is False
    assert meta.requester_type == RequesterType.AGENT_REQUESTER
    assert is_worker_result_report(meta)
    # The same person and run as the worker's task.
    assert meta.gateway_context == agent.task_manager.get_task_info("run_1").meta.gateway_context
    assert (meta.conversation_id, meta.msg_correlation_id) == ("conversation_1", "corr_1")
    escalation = meta.manager_escalation
    assert (escalation.source_task_id, escalation.source_agent_id) == ("run_1", "worker_1")
    assert (escalation.source_named_agent_type, escalation.original_requester) == ("KnowledgeCurator", "worker_schedule")
    # The worker's task records it, so its own history shows the report.
    history = agent.task_manager.get_task("run_1").execution_history
    assert any((entry.details or {}).get("event") == REPORT_SENT_EVENT for entry in history)
    # The worker's task waits for nothing.
    assert not agent.task_manager.has_pending_subtasks("run_1")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [ProgressState.COMPLETED, ProgressState.CANCELLED])
async def test_report_terminal_state_keeps_its_original_signed_request_open(tmp_path, monkeypatch, terminal):
    from custom_tools.providers.knowledge import knowledge_actor, knowledge_service
    from knowledge_base.service import DraftInput
    from storage.knowledge_repository import create_knowledge_repository

    use_signed_contexts(monkeypatch)
    db = str(tmp_path / "shared.db")
    lead = TaskManager(db_path=db)
    lead.set_owner_identity("lead", "Lead", "team", TEAM_ARCHITECT_AGENT_TYPE)
    signed = context_signer().issue(team_id="team", person_id="alice", person_roles=("member",))
    meta = MessageMeta(gateway_context=GatewayContext(task_context=signed))
    lead.add_task(Task(task_id="request", task_type=MessageMetaType.REQUEST), MessageHeader(), meta, owner_person_id="alice")
    lead.update_main_task_state("request", ProgressState.WAITING, datetime.now(timezone.utc).isoformat())
    worker_tasks = TaskManager(db_path=db)
    worker_tasks.set_owner_identity("worker", "Worker", "team", "WebResearchAgent")
    worker_tasks.add_task(
        Task(task_id="child", parent_task_id="request", task_type=MessageMetaType.REQUEST),
        MessageHeader(), meta, owner_person_id="alice",
    )
    worker = SimpleNamespace(id="worker", name="Worker", named_agent_type="WebResearchAgent", task_manager=worker_tasks, _logger=logging.getLogger(__name__))
    report, header, report_meta = await prepare_worker_result_report(
        worker, comms_message=CommsMessage(
            msg_content=Task(task_type=MessageMetaType.REQUEST, problem_statement=REPORT),
            msg_meta=MessageMeta(manager_escalation=ManagerEscalationContext(escalation_type="worker_result")),
        ), parent_task_info=worker_tasks.get_task_info("child"), header=MessageHeader(), record_id="report",
    )
    assert report.parent_task_id == "child"
    assert lead.add_task(report, header, report_meta, owner_person_id="alice")
    lead.update_main_task_state(report.task_id, terminal, datetime.now(timezone.utc).isoformat())
    assert lead.get_task_info("request").state is ProgressState.WAITING
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT state FROM change_requests WHERE request_id=?", (signed.context.context_id,)).fetchone()[0] == "open"
    # Use the tools' verified actor, not an actor assembled from claimed identity.
    facade = SimpleNamespace(
        id="lead", name="Lead", team_id="team", named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
        task_manager=lead, knowledge_config={}, _knowledge_repository=create_knowledge_repository(lead.storage_settings),
    )
    lead.task_state.current_in_progress_task_id = "request"
    bind_task(lead.task_state.current_in_progress_task_id)
    saved = knowledge_service(facade).write(knowledge_actor(facade), "personal/notes", DraftInput(title="Notes", body="Still running."))
    assert saved.page.revision == 1
    lead.complete_task("request", datetime.now(timezone.utc).isoformat())
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT state FROM change_requests WHERE request_id=?", (signed.context.context_id,)).fetchone()[0] == "ended"


@pytest.mark.parametrize(("report", "suppressed"), [(True, True), (False, False)])
def test_only_no_reply_reports_suppress_a_response_despite_source_parentage(report, suppressed):
    from custom_agents.turn_action_outbox_runtime import _should_suppress_unrequested_agent_response

    request = Task(task_id="child", parent_task_id="parent", task_type=MessageMetaType.REQUEST)
    meta = MessageMeta(
        requester_type=RequesterType.AGENT_REQUESTER, is_response_requested=False,
        manager_escalation=ManagerEscalationContext(escalation_type="worker_result") if report else None,
    )
    assert _should_suppress_unrequested_agent_response(
        incoming_content=request, incoming_meta=meta,
        outgoing_content=Task(task_id="child", task_type=MessageMetaType.RESPONSE),
    ) is suppressed


@pytest.mark.asyncio
async def test_report_retry_after_publish_keeps_its_id_without_delegation_state(tmp_path, monkeypatch):
    from custom_agents.delegated_work_runtime import delegated_tasks
    from custom_agents.turn_action_outbox_runtime import PUBLISH_CONFIRMED, TurnActionRecoveryScheduled
    from tests.test_turn_action_outbox import _add_active_parent_task, _make_agent, _make_task_manager

    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    agent.named_agent_type = "WebResearchAgent"
    agent.named_agent_type_queue_map[TEAM_ARCHITECT_AGENT_TYPE] = "test_team_TeamArchitect"
    mark = manager.mark_turn_action_record
    failed = False

    def fail_completion_once(*args, **kwargs):
        nonlocal failed
        if kwargs.get("status") == "completed" and not failed:
            failed = True
            raise sqlite3.OperationalError("report completion save unavailable")
        return mark(*args, **kwargs)

    monkeypatch.setattr(manager, "mark_turn_action_record", fail_completion_once)
    decision = ModelWorkerTurnDecision.model_validate(
        _decision(_report_action(), {**_RESPONSE, "suppress_outbound_dispatch": False})
    ).to_runtime_decision()
    info = manager.get_task_info(parent.task_id)
    with pytest.raises(TurnActionRecoveryScheduled):
        await agent._execute_turn_decision_outbox(
            decision=decision, incoming_content=parent, incoming_header=info.header, incoming_meta=info.meta,
        )
    report_record = next(record for record in manager.execution_state.turn_action_outbox.values() if record.action_type == "delegate_task")
    assert not report_record.side_effects.get(PUBLISH_CONFIRMED)
    assert "delegated_task_id" not in report_record.side_effects
    manager.mark_turn_action_record(report_record.action_id, next_retry_at="2000-01-01T00:00:00+00:00")
    assert await agent._drain_due_turn_action_outbox()
    reports = [
        call.kwargs["message"].msg_content
        for call in agent.team_comms.send_message.await_args_list
        if is_worker_result_report(call.kwargs["message"].msg_meta)
    ]
    assert len(reports) == 2 and reports[0].task_id == reports[1].task_id
    assert reports[0].parent_task_id == parent.task_id
    assert delegated_tasks(agent).get(str(agent.team_id), reports[0].task_id) is None
    assert not manager.has_pending_subtasks(parent.task_id)
    final_answer = agent.team_comms.send_message.await_args_list[-1].kwargs["message"].msg_content
    assert final_answer.task_type is MessageMetaType.RESPONSE
    assert final_answer.task_id == parent.task_id
    assert manager.get_task_info(parent.task_id) is None  # Completed work was pruned.
    assert manager.get_turn_action_record(report_record.action_id) is None
