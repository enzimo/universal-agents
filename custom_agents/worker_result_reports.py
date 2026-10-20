"""A worker's report to the team lead, which gets no reply (``worker_result`` escalations).

A worker hands TeamArchitect a result it did not get from a TeamArchitect
delegation (most often what a scheduled run changed) with a
``manager_escalation`` of type ``worker_result``. TeamArchitect decides whether
to tell the person, record it, or do nothing; the worker needs no answer.

Why: until 2026-10-19 a report was a delegated subtask like every other
escalation. The worker's task waited for TeamArchitect's reply (an
acknowledgment), its own final answer was held back meanwhile
(``turn_action_outbox_runtime._defer_terminal_task_response_for_pending_parent_work``),
and the worker took another model turn when the reply came. A weekly
knowledge-base audit re-ran itself on every such turn
(``custom_agents.turn_work_notes`` covers the lost work; this module removes
the needless wait).

Now the report is a no-reply task for TeamArchitect. Its parent identifies the
source task, but the worker registers no pending subtask and waits for no
response. The worker's task finishes in the same turn
(the batch must end with its ``task_response``,
``structured_llm_messages.WORKER_RESULT_REQUIRES_RESPONSE``), and
TeamArchitect's answer to the report is not sent back
(``turn_action_outbox_runtime._should_suppress_unrequested_agent_response``).
The report carries the worker task's gateway context unchanged, as the
subtask did, so TeamArchitect's task works for the same person
(``access_control.task_identity``); trusted code sets the escalation's source
fields. Its task id derives from the outbox action, so a retried publish
sends the same task and TeamArchitect keeps one copy.
"""

from __future__ import annotations

from typing import Any, Optional

from custom_agents.task_records import TaskInfo
from custom_messages.contracts.envelope_v1 import CommsMessage
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    TaskHistoryImportance,
    TaskHistoryType,
)
from id_generation import operational_id

REPORT_TASK_ID_PREFIX = "worker_report"
REPORT_SENT_EVENT = "worker_result_report_sent"


def is_worker_result_report(meta: Optional[MessageMeta]) -> bool:
    """Whether an outgoing task request is a worker's report (no reply expected)."""

    escalation = getattr(meta, "manager_escalation", None)
    return getattr(escalation, "escalation_type", None) == "worker_result"


def report_task_id(record_id: str) -> str:
    """The report's task id: one per outbox action, so a retry sends the same task."""

    return operational_id(REPORT_TASK_ID_PREFIX, record_id)


def _source_kind(parent_meta: Optional[MessageMeta]) -> str:
    gateway_context = getattr(parent_meta, "gateway_context", None)
    if getattr(parent_meta, "requester_type", None) == RequesterType.TIMER_REQUESTER or getattr(
        gateway_context, "scheduled_job_id", None
    ):
        return "worker_schedule"
    return "worker_task"


async def prepare_worker_result_report(
    agent: Any,
    *,
    comms_message: CommsMessage,
    parent_task_info: TaskInfo,
    header: MessageHeader,
    record_id: str,
) -> tuple[Task, MessageHeader, MessageMeta]:
    """The report task, header and metadata to publish to TeamArchitect's queue.

    ``header`` is the outbox's header for the action (routed to the team
    lead's queue); replies, which TeamArchitect does not send, would come to
    this agent. Records on the worker's task that the report went out.
    """

    model_task = comms_message.msg_content
    if not isinstance(model_task, Task):
        raise RuntimeError("A worker_result report must be a Task request.")
    parent_task = parent_task_info.task
    parent_meta = parent_task_info.meta
    escalation = getattr(comms_message.msg_meta, "manager_escalation", None)
    if escalation is None:
        raise RuntimeError("A worker_result report needs its manager_escalation metadata.")
    gateway_context = getattr(parent_meta, "gateway_context", None)
    task = Task(
        task_id=report_task_id(record_id),
        # Keep request lineage without registering a wait. A parentless report
        # would own and close the original signed request's change ledger.
        parent_task_id=parent_task.task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement=model_task.problem_statement,
        attachments=[attachment.model_copy(deep=True) for attachment in model_task.attachments or []],
    )
    meta = MessageMeta(
        from_agent_name=agent.name,
        msg_correlation_id=getattr(parent_meta, "msg_correlation_id", None),
        conversation_id=getattr(parent_meta, "conversation_id", None),
        objective_id=getattr(parent_meta, "objective_id", None),
        request_timezone=getattr(parent_meta, "request_timezone", None),
        gateway_context=gateway_context.model_copy(deep=True) if gateway_context is not None else None,
        requester_type=RequesterType.AGENT_REQUESTER,
        is_response_requested=False,
        to_named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
        # Where the report came from is trusted code's to say, not the model's.
        manager_escalation=escalation.model_copy(
            update={
                "source_task_id": parent_task.task_id,
                "source_agent_id": agent.id,
                "source_agent_name": agent.name,
                "source_named_agent_type": getattr(agent, "named_agent_type", None),
                "original_requester": _source_kind(parent_meta),
            },
            deep=True,
        ),
    )
    report_header = header.model_copy(
        update={"from_agent_id": agent.id, "response_routing_key": agent.id}, deep=True
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        parent_task.task_id,
        "Sent TeamArchitect a report; no reply is expected.",
        entry_type=TaskHistoryType.SUBTASK_EVENT,
        importance=TaskHistoryImportance.NORMAL,
        agent_name=agent.name,
        agent_id=agent.id,
        related_task_id=task.task_id,
        details={"event": REPORT_SENT_EVENT, "report_task_id": task.task_id, "outbox_action_id": record_id},
    )
    agent._logger.info(
        "A worker sent TeamArchitect a report that needs no reply. event=worker_result_report_sent "
        "agent_id=%s task_id=%s report_task_id=%s action_id=%s",
        agent.id,
        parent_task.task_id,
        task.task_id,
        record_id,
    )
    return task, report_header, meta


__all__ = [
    "REPORT_SENT_EVENT",
    "is_worker_result_report",
    "prepare_worker_result_report",
    "report_task_id",
]
