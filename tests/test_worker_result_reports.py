"""A worker's report to the team lead gets no reply and does not hold the worker's task.

custom_agents.worker_result_reports: a ``worker_result`` escalation is its own
task for TeamArchitect (no parent, no response requested) with trusted source
fields, and the worker's batch must end with its own final answer.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

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
    assert task.parent_task_id is None and task.problem_statement == REPORT
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
