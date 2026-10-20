"""Turn-action outbox execution helpers for UniversalAgent."""

from __future__ import annotations

import random
from functools import partial
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from pydantic import ValidationError
from default_values import AGENT_DEFAULTS

from custom_agents.delegated_work_runtime import (
    delegation_holder,
    end_unpublished_delegation,
    record_check_back,
    record_publication,
    record_publish_sequence,
)
from custom_agents.pending_work_runtime import wake_pending_work_after
from custom_agents.task_runtime_state import TurnStartNotes
from custom_agents.task_records import TaskInfo
from custom_agents.worker_result_reports import is_worker_result_report, prepare_worker_result_report
from custom_agents.conversation_delivery_states import (
    DELIVERY_STATE_NOTIFICATION_QUEUED,
    DELIVERY_STATE_SUPPRESSED,
)
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_agents.message_routing import (
    build_outgoing_meta,
    construct_header,
    requester_bound_primary_route,
    resolve_outgoing_routing,
)
from custom_agents.structured_llm_messages import (
    DelegateTaskAction,
    FireworksModelAgentTurnDecision,
    ModelAgentTurnDecision,
    NoOpAction,
    ScheduleMessageAction,
    SecureCredentialCollectionAction,
    SendMessageAction,
    SignupAutomationAction,
    StructuredLLMResponse,
    StructuredAgentTurnDecision,
    StructuredScheduledMessagePayload,
    StructuredTaskPayload,
    TaskQueryAction,
    TaskResponseAction,
    TurnAction,
)
from custom_agents.task_runtime_coordinator import RuntimeBatchInterrupted
from custom_agents.tool_call_limits import (
    compact_debug_preview as _compact_debug_preview,
)
from custom_messages.contracts.control_v1 import NoOpMessage
from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SignupAutomationRequest,
)
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import (
    CommsMessage,
    ContentType,
    MessageEnvelope,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import TURN_ACTION_HELD_BACK
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import (
    MessageMetaType,
    PlanActionStatus,
    PlanActionType,
    ProgressState,
    RequesterType,
    RoutingType,
    TaskActivityStage,
    TaskActivityType,
    TaskHistoryImportance,
    TaskHistoryType,
    TaskQueryReason,
)
from id_generation import DELEGATION_WAIT_NOTIFICATION_ID_PREFIX, operational_id
from reply_routing_trace import (
    compact_content_context,
    compact_gateway_context,
    is_reply_routing_trace_enabled,
)


# The outbox action type of a delegation.
_DELEGATE_ACTION = DelegateTaskAction.model_fields["action_type"].default
# Outbox statuses of an action the outbox stopped trying to carry out.
_PUBLISH_GIVEN_UP_STATUSES = frozenset({"failed_recoverable", "failed_terminal"})
# Outbox side effect: a delegated task's publish went through, recorded as
# soon as it is known (the publish call returned, or a worker claimed the
# task). Side effects are kept across later status changes and restarts, so
# a step that fails after publishing never makes the task look unsent
# (2026-10-18). Values: PUBLISHED_BY_CALL or PUBLISHED_BY_CLAIM.
PUBLISH_CONFIRMED = "publish_confirmed"
PUBLISHED_BY_CALL = "publish_call"
PUBLISHED_BY_CLAIM = "worker_claim"


class TurnActionRecoveryScheduled(RuntimeError):
    """Raised when runtime action execution has been durably paused for recovery."""


def _external_wait_barrier_reason(content: ContentType) -> str | None:
    if (
        isinstance(content, SecureCredentialCollectionRequest)
        and content.request_type == MessageMetaType.REQUEST
        and content.parent_task_id
    ):
        return (
            "Secure credential collection request dispatched; later actions from the same model turn "
            "are blocked until the broker returns SecureCredentialCollectionResult."
        )
    if (
        isinstance(content, SignupAutomationRequest)
        and content.request_type == MessageMetaType.REQUEST
        and content.parent_task_id
    ):
        return (
            "Signup automation request dispatched; later actions from the same model turn "
            "are blocked until signup automation or manual fallback returns a result."
        )
    return None


def _is_report_from_worker(self: Any, comms_message: CommsMessage, incoming_content: ContentType) -> bool:
    """A worker's ``worker_result`` escalation for its own task (``worker_result_reports``)."""

    return (
        getattr(self, "named_agent_type", None) != TEAM_ARCHITECT_AGENT_TYPE
        and isinstance(incoming_content, Task)
        and incoming_content.task_type == MessageMetaType.REQUEST
        and is_worker_result_report(comms_message.msg_meta)
    )


async def _incoming_task_info(self: Any, incoming_content: ContentType) -> TaskInfo:
    task_id = getattr(incoming_content, "task_id", None)
    task_info: Optional[TaskInfo] = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, task_id
    )
    if task_info is None:
        raise RuntimeError(f"Cannot send a report for task {task_id}: its local task record is missing.")
    return task_info


def _validate_manager_mediated_worker_delegation(self, action: TurnAction) -> None:
    if not isinstance(action, DelegateTaskAction):
        return
    if getattr(self, "named_agent_type", None) == TEAM_ARCHITECT_AGENT_TYPE:
        return
    if (
        action.manager_escalation is not None
        and action.target_named_agent_type == TEAM_ARCHITECT_AGENT_TYPE
        and not action.target_agent_name
    ):
        return
    raise RuntimeError(
        "Non-manager agents must route user input, cross-specialist support, and worker-origin "
        "manager review through a manager_escalation task to TeamArchitect."
    )


def _comms_message_from_turn_action(self, action: TurnAction) -> Optional[CommsMessage]:
    if isinstance(action, DelegateTaskAction):
        meta = MessageMeta(
            to_named_agent_type=action.target_named_agent_type,
            to_agent_name=action.target_agent_name,
            requester_type=action.requester_type,
            manager_escalation=action.manager_escalation,
        )
        # A delegation is always a new task, so its id is generated, never taken
        # from the model: task ids are team-wide keys (one custody row per task,
        # and a copy of a finished task is dropped), and a reused id would make
        # a new task look like a copy of an earlier one.
        if action.task.task_id:
            self._logger.info(
                "A delegated task gets a generated id instead of the one the model gave. "
                "event=delegated_task_id_generated agent_id=%s model_task_id=%s",
                self.id,
                action.task.task_id,
            )
        return CommsMessage(
            msg_content=action.task.model_copy(update={"task_id": None}).to_task(),
            msg_meta=meta
            if meta.model_dump(exclude_none=True, exclude_defaults=True)
            else None,
        )
    if isinstance(action, TaskResponseAction):
        return CommsMessage(msg_content=action.task.to_task())
    if isinstance(action, TaskQueryAction):
        return CommsMessage(msg_content=action.task_query)
    if isinstance(action, ScheduleMessageAction):
        return CommsMessage(msg_content=action.scheduled_message.to_scheduled_message())
    if isinstance(action, SecureCredentialCollectionAction):
        return CommsMessage(msg_content=action.secure_credential_collection_request)
    if isinstance(action, SignupAutomationAction):
        return CommsMessage(msg_content=action.signup_automation_request)
    if isinstance(action, SendMessageAction):
        return action.message.to_comms_message()
    return None


def _supply_reply_linkage(action: TurnAction, incoming: ContentType) -> None:
    """Set omitted runtime ids before durable serialization and before dispatch.

    The outbox must retain known reply ids across restarts. Explicitly supplied
    mismatches stay visible to the later correction and anomaly recorder.
    """

    if isinstance(action, TaskResponseAction) and isinstance(incoming, Task):
        if action.task.task_id is None:
            action.task.task_id = incoming.task_id
        if incoming.parent_task_id and action.task.parent_task_id is None:
            action.task.parent_task_id = incoming.parent_task_id
        return
    if not isinstance(action, TaskQueryAction):
        return
    query = action.task_query
    if isinstance(incoming, Task) and query.parent_task_id is None:
        query.parent_task_id = incoming.task_id
    elif isinstance(incoming, TaskQuery):
        if query.parent_task_id is None:
            query.parent_task_id = incoming.parent_task_id
        if (
            query.query_type == MessageMetaType.RESPONSE
            and "task_query_id" not in query.model_fields_set
        ):
            query.task_query_id = incoming.task_query_id


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _explicit_suppressed_task_response_invalid_reason(
    *,
    incoming_content: ContentType,
    incoming_meta: MessageMeta,
    outgoing_content: ContentType,
) -> tuple[str | None, Dict[str, Any]]:
    details: Dict[str, Any] = {
        "incoming_content_type": type(incoming_content).__name__,
        "outgoing_content_type": type(outgoing_content).__name__,
        "incoming_task_id": getattr(incoming_content, "task_id", None),
        "outgoing_task_id": getattr(outgoing_content, "task_id", None),
        "outgoing_task_type": _enum_value(getattr(outgoing_content, "task_type", None)),
        "requester_type": _enum_value(getattr(incoming_meta, "requester_type", None)),
        "conversation_id": incoming_meta.conversation_id,
        "scheduled_job_id": getattr(incoming_meta.gateway_context, "scheduled_job_id", None),
        "delivery_mode": getattr(incoming_meta.gateway_context, "delivery_mode", None),
        "incoming_parent_task_id": getattr(incoming_content, "parent_task_id", None),
        "incoming_followup_to_task_id": getattr(
            incoming_meta,
            "followup_to_task_id",
            None,
        ),
    }
    if not (
        isinstance(incoming_content, Task)
        and isinstance(outgoing_content, Task)
        and outgoing_content.task_type == MessageMetaType.RESPONSE
        and outgoing_content.task_id == incoming_content.task_id
    ):
        return "not_current_terminal_response", details
    if incoming_content.parent_task_id is not None:
        return "delegated_subtask", details
    if incoming_meta.followup_to_task_id is not None:
        return "followup_work", details
    requester_type = getattr(incoming_meta, "requester_type", None)
    if requester_type != RequesterType.TIMER_REQUESTER:
        return "non_timer_requester", details
    return None, details


async def _repair_invalid_explicit_suppressed_task_response(
    self,
    *,
    action: TaskResponseAction,
    incoming_content: ContentType,
    incoming_meta: MessageMeta,
    outgoing_content: ContentType,
) -> None:
    if not action.suppress_outbound_dispatch:
        return
    invalid_reason, details = _explicit_suppressed_task_response_invalid_reason(
        incoming_content=incoming_content,
        incoming_meta=incoming_meta,
        outgoing_content=outgoing_content,
    )
    if invalid_reason is None:
        return

    action.suppress_outbound_dispatch = False
    details = {
        **details,
        "invalid_reason": invalid_reason,
        "repair": "cleared_suppress_outbound_dispatch",
    }
    repair_recorder = getattr(self, "_record_runtime_repair_anomaly", None)
    summary = (
        "Invalid suppress_outbound_dispatch on task_response; runtime cleared "
        "the flag and will dispatch the response normally."
    )
    if callable(repair_recorder):
        await repair_recorder(
            "task_response_suppression_cleared",
            summary,
            related_task_id=getattr(incoming_content, "task_id", None),
            details=details,
        )
    else:
        self._logger.warning(
            "[%s] event=runtime_repair_anomaly repair_event=task_response_suppression_cleared "
            "summary=%s details=%s",
            self.id,
            summary,
            _compact_debug_preview(details, max_chars=1200),
        )


async def _mark_suppressed_task_delivery(
    self,
    *,
    task: Task,
    incoming_meta: MessageMeta,
    record_id: str,
    summary: str = "Task completed successfully without user-visible delivery.",
    reason: str = "no_user_message_required",
    delivery_state: str = DELIVERY_STATE_SUPPRESSED,
    delivery_via: str = "runtime_noop",
) -> None:
    now_ts = datetime.now(timezone.utc).isoformat()
    await self.task_manager.operations_runtime.run(
        self.task_manager.append_history_entry,
        task.task_id,
        summary,
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=now_ts,
        agent_name=self.name,
        agent_id=self.id,
        details={
            "event": "task_response_suppressed",
            "reason": reason,
            "outbox_action_id": record_id,
            "requester_type": getattr(incoming_meta, "requester_type", None).value
            if hasattr(getattr(incoming_meta, "requester_type", None), "value")
            else getattr(incoming_meta, "requester_type", None),
        },
    )
    conversation_store = getattr(self, "_conversation_store", None)
    conversation_id = getattr(incoming_meta, "conversation_id", None)
    if conversation_store is None or not conversation_id:
        return
    try:
        conversation_store.update_turn_delivery(
            task_id=task.task_id,
            conversation_id=conversation_id,
            correlation_id=getattr(incoming_meta, "msg_correlation_id", None),
            delivery_state=delivery_state,
            delivery_via=delivery_via,
            delivered_at=now_ts,
        )
    except Exception as exc:
        self._logger.warning(
            "[%s] Failed to mark suppressed task delivery for task %s: %s",
            self.id,
            task.task_id,
            exc,
        )


async def _defer_terminal_task_response_for_pending_parent_work(
    self,
    *,
    incoming_content: ContentType,
    outgoing_content: ContentType,
    record_id: str,
) -> Task | None:
    if not (
        isinstance(incoming_content, Task)
        and isinstance(outgoing_content, Task)
        and outgoing_content.task_type == MessageMetaType.RESPONSE
        and outgoing_content.task_id == incoming_content.task_id
    ):
        return None
    if incoming_content.parent_task_id is not None:
        return None

    from custom_agents.access_workflow_runtime import defer_workflow_completion

    pending_subtasks = await self.task_manager.operations_runtime.run(
        self.task_manager.get_pending_subtasks, incoming_content.task_id
    )
    waiting_on_task_query = await self.task_manager.operations_runtime.run(
        self.task_manager.is_waiting_on_task_query, incoming_content.task_id
    )
    if not pending_subtasks and not waiting_on_task_query:
        if await self.task_manager.operations_runtime.run(
            self.task_manager.is_plan_execution_root_active, incoming_content.task_id
        ):
            return None  # The caller retains the runtime plan's existing wait.
        if await self.task_manager.operations_runtime.run(
            partial(defer_workflow_completion, self.task_manager),
            task_id=incoming_content.task_id, team_id=self.team_id,
        ):
            return Task(task_id=incoming_content.task_id, task_type=MessageMetaType.REQUEST,
                        problem_statement="Continue the saved workflow after its outstanding work or reviews finish.")
        return None

    now_ts = datetime.now(timezone.utc).isoformat()
    pending_subtask_ids = [
        subtask.task.task_id
        for subtask in pending_subtasks
        if getattr(subtask, "task", None) is not None
    ]
    wait_reasons = []
    if pending_subtask_ids:
        wait_reasons.append(f"{len(pending_subtask_ids)} delegated subtask(s)")
    if waiting_on_task_query:
        wait_reasons.append("an unanswered TaskQuery")
    reason_text = " and ".join(wait_reasons) or "pending child work"
    answer_preview = _compact_debug_preview(
        outgoing_content.answer or "", max_chars=600
    )
    self._logger.warning(
        "[%s] event=terminal_task_response_deferred task_id=%s action_id=%s "
        "pending_subtasks=%s waiting_on_task_query=%s",
        self.id,
        incoming_content.task_id,
        record_id,
        pending_subtask_ids,
        waiting_on_task_query,
    )
    await self.task_manager.operations_runtime.run(
        self.task_manager.append_history_entry,
        incoming_content.task_id,
        f"Deferred terminal task response because the parent is still waiting on {reason_text}.",
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=now_ts,
        agent_name=self.name,
        agent_id=self.id,
        details={
            "event": "terminal_task_response_deferred",
            "outbox_action_id": record_id,
            "pending_subtask_ids": pending_subtask_ids,
            "waiting_on_task_query": waiting_on_task_query,
            "attempted_answer_preview": answer_preview,
        },
    )
    await self.task_manager.operations_runtime.run(
        self.task_manager.update_main_task_state,
        incoming_content.task_id,
        ProgressState.WAITING,
        now_ts,
    )
    return Task(
        task_id=incoming_content.task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement=(
            "Parent task is waiting for delegated work or requester clarification before final synthesis."
        ),
    )


async def _timer_task_already_used_notify_user(self, task_id: str) -> bool:
    task_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, task_id
    )
    task = getattr(task_info, "task", None) if task_info is not None else None
    for entry in getattr(task, "execution_history", None) or []:
        details = getattr(entry, "details", {}) or {}
        if details.get("event") == "notify_user_sent":
            return True
    return False


async def _delegation_wait_notification_already_sent(self, task_id: str) -> bool:
    for entry in await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_history_entries, task_id, limit=None
    ):
        event = (getattr(entry, "details", {}) or {}).get("event")
        if event in {"notify_user_sent", "delegation_wait_notification_sent"}:
            return True
    return False


async def _notify_user_delegated_work_started(
    self,
    *,
    parent_task_id: str,
    delegated_task_id: Optional[str],
    target_named_agent_type: Optional[str],
) -> bool:
    """Send one nonterminal progress note for an interactive delegated task."""

    parent_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, parent_task_id
    )
    if parent_info is None or parent_info.task.parent_task_id is not None:
        return False
    if (
        getattr(parent_info.meta, "requester_type", None)
        != RequesterType.USER_REQUESTER
    ):
        return False
    gateway_context = getattr(parent_info.meta, "gateway_context", None)
    if (
        str(getattr(gateway_context, "delivery_mode", "") or "").strip().lower()
        != "interactive"
    ):
        return False
    if await _delegation_wait_notification_already_sent(self, parent_task_id):
        return False

    routing_key = str(
        getattr(parent_info.header, "response_routing_key", "") or ""
    ).strip()
    if not routing_key:
        self._logger.warning(
            "[%s] event=delegation_wait_notification_skipped parent_task_id=%s "
            "reason=missing_gateway_response_route",
            self.id,
            parent_task_id,
        )
        return False

    notification_id = operational_id(
        DELEGATION_WAIT_NOTIFICATION_ID_PREFIX,
        parent_task_id,
    )
    correlation_id = f"delegation_wait_{parent_task_id}"
    message = (
        "I've started the multi-step work and am monitoring its progress. It may take a few minutes; "
        "I'll send the completed result when it is ready."
    )
    envelope = MessageEnvelope(
        msg_content=UserNotification(
            notification_id=notification_id,
            text=message,
            urgency="normal",
            reason="delegated_work_started",
            source_task_id=parent_task_id,
            source_agent_id=self.id,
            source_agent_name=self.name,
        ),
        msg_meta=MessageMeta(
            from_agent_name=self.name,
            msg_correlation_id=correlation_id,
            conversation_id=getattr(parent_info.meta, "conversation_id", None),
            followup_to_task_id=parent_task_id,
            objective_id=getattr(parent_info.meta, "objective_id", None),
            request_timezone=getattr(parent_info.meta, "request_timezone", None),
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
            gateway_context=gateway_context.model_copy(deep=True),
        ),
        msg_header=MessageHeader(
            from_agent_id=self.id,
            to_routing_path=routing_key,
            response_routing_key=self.id,
            idempotency_key=notification_id,
            agent_signature="",
        ),
    )
    try:
        await self.team_comms.send_message(message=envelope, routing_key=routing_key)
    except Exception as exc:
        self._logger.warning(
            "[%s] event=delegation_wait_notification_failed parent_task_id=%s "
            "delegated_task_id=%s error=%s",
            self.id,
            parent_task_id,
            delegated_task_id,
            exc,
        )
        return False

    await self.task_manager.operations_runtime.run(
        self.task_manager.append_history_entry,
        parent_task_id,
        "Sent a user-visible progress note before waiting on delegated work.",
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.NORMAL,
        agent_name=self.name,
        agent_id=self.id,
        related_task_id=delegated_task_id,
        details={
            "event": "delegation_wait_notification_sent",
            "notification_id": notification_id,
            "correlation_id": correlation_id,
            "delegated_task_id": delegated_task_id,
            "target_named_agent_type": target_named_agent_type,
            "channel_name": getattr(gateway_context, "channel_name", None),
            "chat_id": getattr(gateway_context, "chat_id", None),
            "thread_id": getattr(gateway_context, "thread_id", None),
        },
    )
    return True


async def _should_suppress_timer_response_after_notify_user(
    self,
    *,
    incoming_content: ContentType,
    incoming_meta: MessageMeta,
    outgoing_content: ContentType,
) -> bool:
    if not (
        isinstance(incoming_content, Task)
        and isinstance(outgoing_content, Task)
        and outgoing_content.task_type == MessageMetaType.RESPONSE
        and outgoing_content.task_id == incoming_content.task_id
        and incoming_content.parent_task_id is None
    ):
        return False
    if getattr(incoming_meta, "requester_type", None) != RequesterType.TIMER_REQUESTER:
        return False
    return await _timer_task_already_used_notify_user(self, incoming_content.task_id)


def _should_suppress_unrequested_agent_response(
    *,
    incoming_content: ContentType,
    incoming_meta: MessageMeta,
    outgoing_content: ContentType,
) -> bool:
    """Honor no-reply tasks while ordinary delegated work always answers.

    Reports keep source parentage for identity and request lifecycle, without
    making their sender wait for an acknowledgment.
    """
    if not (
        isinstance(incoming_content, Task)
        and isinstance(outgoing_content, Task)
        and incoming_content.task_type == MessageMetaType.REQUEST
        and outgoing_content.task_type == MessageMetaType.RESPONSE
        and outgoing_content.task_id == incoming_content.task_id
        and (incoming_content.parent_task_id is None or is_worker_result_report(incoming_meta))
    ):
        return False
    return (
        getattr(incoming_meta, "requester_type", None) == RequesterType.AGENT_REQUESTER
        and getattr(incoming_meta, "is_response_requested", None) is False
    )


def _turn_decision_from_structured_output(
    self, structured_output: Any
) -> Optional[StructuredAgentTurnDecision]:
    if isinstance(structured_output, StructuredAgentTurnDecision):
        return structured_output
    if isinstance(structured_output, FireworksModelAgentTurnDecision):
        return structured_output.to_runtime_decision()
    if isinstance(structured_output, ModelAgentTurnDecision):
        return structured_output.to_runtime_decision()
    if isinstance(structured_output, dict):
        if (
            structured_output.get("selected_message_type") == "AgentTurnDecision"
            or "actions" in structured_output
        ):
            try:
                return StructuredAgentTurnDecision.model_validate(structured_output)
            except ValidationError as runtime_error:
                try:
                    return ModelAgentTurnDecision.model_validate(
                        structured_output
                    ).to_runtime_decision()
                except ValidationError:
                    raise runtime_error
    return None


def _turn_decision_from_comms_message(
    self, comms_message: CommsMessage
) -> StructuredAgentTurnDecision:
    payload = comms_message.msg_content
    meta = comms_message.msg_meta or MessageMeta()
    if isinstance(payload, Task):
        task_payload = StructuredTaskPayload.model_validate(
            payload.model_dump(mode="json")
        )
        if payload.task_type == MessageMetaType.REQUEST:
            return StructuredAgentTurnDecision(
                actions=[
                    DelegateTaskAction(
                        task=task_payload,
                        target_named_agent_type=getattr(
                            meta, "to_named_agent_type", None
                        ),
                        target_agent_name=getattr(meta, "to_agent_name", None),
                        requester_type=getattr(meta, "requester_type", None),
                    )
                ]
            )
        return StructuredAgentTurnDecision(
            actions=[TaskResponseAction(task=task_payload)]
        )
    if isinstance(payload, TaskQuery):
        return StructuredAgentTurnDecision(
            actions=[TaskQueryAction(task_query=payload)]
        )
    if isinstance(payload, ScheduledMessage):
        scheduled_payload = payload.model_dump(mode="json")
        scheduled_payload["payload"] = StructuredTaskPayload.model_validate(
            payload.payload.model_dump(mode="json")
        )
        return StructuredAgentTurnDecision(
            actions=[
                ScheduleMessageAction(
                    scheduled_message=StructuredScheduledMessagePayload.model_validate(
                        scheduled_payload
                    )
                )
            ]
        )
    if payload.__class__.__name__ == "AgentCreationRequest":
        raise RuntimeError("Unsupported final LLM message type AgentCreationRequest")
    payload_field_by_name = {
        "AgentStatus": "agent_status",
        "ControlInstruction": "control_instruction",
        "NoOpMessage": "no_op_message",
        "SecureCredentialCollectionRequest": "secure_credential_collection_request",
        "SecureCredentialCollectionResult": "secure_credential_collection_result",
        "SignupAutomationRequest": "signup_automation_request",
        "SignupAutomationResult": "signup_automation_result",
        "SystemStatusMessage": "system_status_message",
    }
    payload_name = payload.__class__.__name__
    payload_field = payload_field_by_name.get(payload_name)
    if payload_field is None:
        return StructuredAgentTurnDecision(
            actions=[
                NoOpAction(
                    reason=(
                        f"Unsupported runtime payload {payload_name} could not be converted "
                        "into a turn action."
                    )
                )
            ]
        )
    return StructuredAgentTurnDecision(
        actions=[
            SendMessageAction(
                message=StructuredLLMResponse(
                    selected_message_type=payload_name,
                    **{payload_field: payload},
                )
            )
        ]
    )


def _turn_action_retry_delay_seconds(self, attempt_count: int) -> float:
    initial = self._turn_action_outbox_float("initial_retry_delay_seconds", 2.0)
    maximum = self._turn_action_outbox_float("max_retry_delay_seconds", 60.0)
    jitter_ratio = self._turn_action_outbox_float("retry_jitter_ratio", 0.15)
    delay = min(maximum, initial * (2 ** max(0, attempt_count - 1)))
    if jitter_ratio > 0:
        spread = delay * jitter_ratio
        delay = max(0.0, delay + random.uniform(-spread, spread))
    return delay


def delegation_publish_given_up(task_manager: Any, subtask_id: str) -> bool:
    """Whether no outbox action is still trying to publish this delegated task (task lane).

    True when every action that delegated it failed (the outbox gave up), or
    the outbox holds none. An action still pending, publishing, waiting for
    its retry or blocked behind an earlier action may still publish it, and
    one dispatched did publish it, whatever its record says. The subtask
    watchdog checks queued records without a stream sequence with this,
    because a record's age alone misjudged a busy or restarted requester.
    """
    actions = [
        record
        for record in task_manager.execution_state.turn_action_outbox.values()
        if record.action_type == _DELEGATE_ACTION
        and (record.side_effects or {}).get("delegated_task_id") == subtask_id
    ]
    if any((record.side_effects or {}).get(PUBLISH_CONFIRMED) for record in actions):
        return False
    return all(record.status in _PUBLISH_GIVEN_UP_STATUSES for record in actions)


async def _end_unpublished_delegation(self: Any, record: Any, exc: Exception) -> bool:
    """End the subtask of a delegation that leaves automatic retry unpublished.

    Called when the action goes to model recovery, isolated recovery or a
    terminal failure (``delegated_work_runtime.end_unpublished_delegation``).
    A delegation whose publish already succeeded (status ``dispatched``) is
    left alone: a worker may hold it. One a worker claimed although no publish
    call returned is normally finished before any recovery starts
    (``_finish_claim_confirmed_delegation``); a claim that arrives while the
    recovery is being scheduled is caught here instead: the action is recorded
    dispatched and True is returned, so the recovery that follows does not
    mark it failed.
    """
    subtask_id = (record.side_effects or {}).get("delegated_task_id")
    if (
        record.action_type != _DELEGATE_ACTION
        or not subtask_id
        or record.status == "dispatched"
        or (record.side_effects or {}).get(PUBLISH_CONFIRMED)
    ):
        # Published (now or on an earlier attempt): a worker may hold it.
        return False
    try:
        outcome = await self.task_manager.operations_runtime.run(
            partial(
                end_unpublished_delegation,
                self,
                subtask_id=str(subtask_id),
                parent_task_id=record.source_task_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
    except Exception:
        # The watchdog's never-published check still ends it later.
        self._logger.exception(
            "Could not stop a delegated task that was never sent. "
            "event=delegated_task_end_failed agent_id=%s task_id=%s subtask_id=%s action_id=%s",
            self.id,
            record.source_task_id,
            subtask_id,
            record.action_id,
        )
        return False
    if outcome.claimed_by:
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_record,
            record.action_id,
            status="dispatched",
            side_effects={PUBLISH_CONFIRMED: PUBLISHED_BY_CLAIM, "publish_confirmed_by_claim": outcome.claimed_by},
        )
        self._logger.warning(
            "A delegated task's publish reported a failure, but a worker took the task, so it "
            "was sent. event=delegated_task_publish_confirmed_by_claim agent_id=%s task_id=%s "
            "subtask_id=%s action_id=%s holder_agent_id=%s error_type=%s",
            self.id,
            record.source_task_id,
            subtask_id,
            record.action_id,
            outcome.claimed_by,
            type(exc).__name__,
        )
        return True
    if outcome.ended:
        self._logger.warning(
            "A delegated task could not be sent to a worker and was stopped. "
            "event=delegated_task_never_published agent_id=%s task_id=%s subtask_id=%s "
            "action_id=%s attempts=%s error_type=%s",
            self.id,
            record.source_task_id,
            subtask_id,
            record.action_id,
            record.attempt_count,
            type(exc).__name__,
        )
    return False


async def _schedule_turn_action_recovery(
    self,
    *,
    batch_id: str,
    action_id: str,
    exc: Exception,
) -> None:
    record = await self.task_manager.operations_runtime.run(
        self.task_manager.get_turn_action_record, action_id
    )
    if record is None:
        raise RuntimeError(f"Missing turn action record {action_id}") from exc
    source_task_id = record.source_task_id
    max_auto_attempts = self._turn_action_outbox_int(
        "max_auto_attempts",
        AGENT_DEFAULTS.turn_action_outbox.value["max_auto_attempts"],
    )
    now_ts = datetime.now(timezone.utc).isoformat()
    failure_report = {
        "event": "turn_action_failed",
        "batch_id": batch_id,
        "action_id": action_id,
        "action_index": record.action_index,
        "action_type": record.action_type,
        "phase": record.phase,
        "attempt_count": record.attempt_count,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "target_route": record.target_route,
        "message_id": record.message_id,
        "side_effects": dict(record.side_effects or {}),
    }
    await self.task_manager.operations_runtime.run(
        self.task_manager.block_turn_actions_after,
        batch_id,
        record.action_index,
        reason=f"Action {action_id} failed before later actions ran.",
    )

    automatic_retry_phases = {"persist", "publish", "post_publish_finalize"}
    if (
        record.recoverability == "automatic_retry"
        and record.phase in automatic_retry_phases
        and record.attempt_count < max_auto_attempts
    ):
        retry_delay = self._turn_action_retry_delay_seconds(record.attempt_count + 1)
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=retry_delay)
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_record,
            action_id,
            status="retry_scheduled",
            next_retry_at=retry_at.isoformat(),
            last_error=str(exc),
            failure_report=failure_report,
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_batch_status,
            batch_id,
            "retrying",
            current_action_index=record.action_index,
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.append_history_entry,
            source_task_id,
            f"Runtime action {action_id} failed at phase {record.phase}; automatic retry scheduled.",
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.HIGH,
            timestamp=now_ts,
            agent_name=self.name,
            agent_id=self.id,
            details={**failure_report, "next_retry_at": retry_at.isoformat()},
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.update_main_task_state,
            source_task_id,
            ProgressState.RETRYING,
            now_ts,
        )
        # The retry is saved above; a timer wakes the retained drain at the
        # retry time, and shutdown cancels the timer.
        wake_pending_work_after(self, retry_delay)
        raise TurnActionRecoveryScheduled(
            f"Runtime action {action_id} scheduled automatic retry after {type(exc).__name__}: {exc}"
        ) from exc

    # Automatic retries are over: a delegation that never reached NATS ends
    # its subtask instead of leaving it "waiting in the queue". One a worker
    # took was sent; its action stays dispatched in the recovery below.
    publish_confirmed = await _end_unpublished_delegation(self, record, exc)

    def failed(status: str) -> Optional[str]:
        return None if publish_confirmed else status

    inline_limit = self._turn_action_outbox_int(
        "max_inline_recovery_attempts_per_batch", 2
    )
    batch = await self.task_manager.operations_runtime.run(
        self.task_manager.get_turn_action_batch, batch_id
    )
    inline_count = batch.inline_recovery_attempt_count if batch else 0
    if inline_count < inline_limit:
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_record,
            action_id,
            status=failed("failed_recoverable"),
            recoverability="model_recovery",
            last_error=str(exc),
            failure_report=failure_report,
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_batch_status,
            batch_id,
            "needs_model_recovery",
            current_action_index=record.action_index,
            increment_inline_recovery=True,
        )
        recovery_brief = await self.task_manager.operations_runtime.run(
            self.task_manager.build_turn_action_recovery_brief, batch_id
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.append_runtime_resume_directive,
            source_task_id,
            recovery_brief,
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.append_history_entry,
            source_task_id,
            f"Runtime action {action_id} needs model recovery after failing at phase {record.phase}.",
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.CRITICAL,
            timestamp=now_ts,
            agent_name=self.name,
            agent_id=self.id,
            tracks_to_plan=True,
            details=failure_report,
        )
        if await self.task_manager.operations_runtime.run(
            self.task_manager.get_task_plan_id, source_task_id
        ):
            await self.task_manager.operations_runtime.run(
                self.task_manager.record_plan_action,
                source_task_id,
                PlanActionType.RECORD_FAILURE,
                f"Runtime action {action_id} failed at phase {record.phase}.",
                status=PlanActionStatus.COMPLETED,
                reason="turn_action_failure",
                details=failure_report,
                agent_name=self.name,
                agent_id=self.id,
                timestamp=now_ts,
            )
            await self.task_manager.operations_runtime.run(
                self.task_manager.record_plan_action,
                source_task_id,
                PlanActionType.START_INLINE_RECOVERY,
                "Queued original task for inline runtime action recovery.",
                status=PlanActionStatus.ACTIVE,
                reason="turn_action_failure",
                details={"batch_id": batch_id, "action_id": action_id},
                agent_name=self.name,
                agent_id=self.id,
                timestamp=now_ts,
            )
        await self.task_manager.operations_runtime.run(
            self.task_manager.update_main_task_state,
            source_task_id,
            ProgressState.PARTIAL,
            now_ts,
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.enqueue_task, source_task_id, front=True
        )
        raise TurnActionRecoveryScheduled(
            f"Runtime action {action_id} queued inline model recovery after {type(exc).__name__}: {exc}"
        ) from exc

    recovery_brief = await self.task_manager.operations_runtime.run(
        self.task_manager.build_turn_action_recovery_brief, batch_id
    )
    source_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, source_task_id
    )
    if source_info is not None and await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_plan_id, source_task_id
    ):
        recovery_info = await self.task_manager.operations_runtime.run(
            self.task_manager.start_isolated_recovery_task,
            source_task_id,
            failure_type="turn_action_recovery_limit",
            failure_summary=f"Runtime action {action_id} failed after inline recovery attempts were exhausted.",
            recovery_brief=recovery_brief,
            initial_recovery_context=recovery_brief,
            header=source_info.header,
            meta=source_info.meta,
            affected_step_id=batch.plan_step_id if batch else None,
            notify_user=False,
            details={
                "batch_id": batch_id,
                "action_id": action_id,
                "action_index": record.action_index,
                "phase": record.phase,
            },
            agent_name=self.name,
            agent_id=self.id,
        )
        if recovery_info is not None:
            await self.task_manager.operations_runtime.run(
                self.task_manager.mark_turn_action_record,
                action_id,
                status=failed("failed_recoverable"),
                recoverability="isolated_recovery",
                last_error=str(exc),
                failure_report=failure_report,
            )
            await self.task_manager.operations_runtime.run(
                self.task_manager.mark_turn_action_batch_status,
                batch_id,
                "isolated_recovery_started",
                current_action_index=record.action_index,
            )
            await self.task_manager.operations_runtime.run(
                self.task_manager.append_history_entry,
                source_task_id,
                f"Started isolated recovery task {recovery_info.task.task_id} for runtime action batch {batch_id}.",
                entry_type=TaskHistoryType.PLAN_EVENT,
                importance=TaskHistoryImportance.CRITICAL,
                timestamp=now_ts,
                agent_name=self.name,
                agent_id=self.id,
                related_task_id=recovery_info.task.task_id,
                tracks_to_plan=True,
                details={
                    "event": "turn_action_isolated_recovery_started",
                    "batch_id": batch_id,
                    "action_id": action_id,
                    "recovery_task_id": recovery_info.task.task_id,
                },
            )
            await self.task_manager.operations_runtime.run(
                self.task_manager.update_main_task_state,
                source_task_id,
                ProgressState.WAITING,
                now_ts,
            )
            raise TurnActionRecoveryScheduled(
                f"Runtime action {action_id} started isolated recovery after {type(exc).__name__}: {exc}"
            ) from exc

    await self.task_manager.operations_runtime.run(
        self.task_manager.mark_turn_action_record,
        action_id,
        status=failed("failed_terminal"),
        recoverability="terminal",
        last_error=str(exc),
        failure_report=failure_report,
    )
    await self.task_manager.operations_runtime.run(
        self.task_manager.mark_turn_action_batch_status,
        batch_id,
        "failed",
        current_action_index=record.action_index,
    )
    await self.task_manager.operations_runtime.run(
        self.task_manager.append_history_entry,
        source_task_id,
        f"Runtime action {action_id} exhausted recovery attempts and failed terminally.",
        entry_type=TaskHistoryType.ERROR_EVENT,
        importance=TaskHistoryImportance.CRITICAL,
        timestamp=now_ts,
        agent_name=self.name,
        agent_id=self.id,
        tracks_to_plan=True,
        details=failure_report,
    )
    raise exc


def _is_waiting_delegation(content: Any, meta: MessageMeta) -> bool:
    """Source lineage does not imply the sender waits for a report's reply."""

    return (
        isinstance(content, Task)
        and content.task_type == MessageMetaType.REQUEST
        and bool(content.parent_task_id)
        and not (is_worker_result_report(meta) and meta.is_response_requested is False)
    )


async def _record_delegation_before_publish(
    self: Any, content: Any, meta: MessageMeta, named_agent_type: Optional[str]
) -> None:
    """Record a delegated task as queued before it is published.

    A free worker can take the task and its claim can be handled before the
    publish call returns (``delegated_work_runtime.record_publication``).
    """
    if not _is_waiting_delegation(content, meta):
        return
    await self.task_manager.operations_runtime.run(
        partial(
            record_publication,
            self,
            subtask_id=content.task_id,
            parent_task_id=content.parent_task_id,
            named_agent_type=named_agent_type,
            target_agent_name=getattr(meta, "to_agent_name", None),
            publish_sequence=None,
        )
    )


async def _execute_turn_action_record(
    self,
    *,
    record_id: str,
    action: TurnAction,
    incoming_content: ContentType,
    incoming_header: MessageHeader,
    incoming_meta: MessageMeta,
    structured_output_details: Optional[Dict[str, Any]] = None,
) -> ContentType:
    record = await self.task_manager.operations_runtime.run(
        self.task_manager.get_turn_action_record, record_id
    )
    if record is None:
        raise RuntimeError(f"Turn action record {record_id} is missing.")

    if isinstance(action, NoOpAction):
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_record,
            record_id,
            status="completed",
            phase="complete",
        )
        return NoOpMessage(info_message=action.reason)

    if (
        isinstance(action, TaskResponseAction)
        and record.phase == "post_publish_finalize"
        and record.status
        in {"retry_scheduled", "failed_recoverable", "dispatching", "dispatched"}
    ):
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_record,
            record_id,
            status="dispatching",
            phase="post_publish_finalize",
            increment_attempt=True,
        )
        task_id = action.task.task_id or record.source_task_id
        if (
            not task_id
            or not await self._complete_local_task_finalization_after_dispatch(
                task_id=task_id,
                after_action_report_sent=False,
            )
        ):
            raise RuntimeError(
                f"Unable to finalize already-dispatched task response for task {task_id or record.source_task_id}."
            )
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_record,
            record_id,
            status="completed",
            phase="complete",
        )
        return action.task.to_task()

    if isinstance(action, DelegateTaskAction) and (record.side_effects or {}).get(PUBLISH_CONFIRMED):
        # Published on an earlier attempt; a later step failed. Only the steps
        # after publishing run again: publishing again could fail and end a
        # task a worker may already run (2026-10-18).
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_record, record_id,
            status="dispatching", phase="publish", increment_attempt=True,
        )
        return await _complete_published_delegation(self, record=record, action=action)

    _supply_reply_linkage(action, incoming_content)
    _validate_manager_mediated_worker_delegation(self, action)
    comms_message = self._comms_message_from_turn_action(action)
    if comms_message is None:
        raise RuntimeError(f"Unsupported turn action type: {type(action).__name__}")

    after_action_report = (
        action.after_action_report if isinstance(action, TaskResponseAction) else None
    )
    content = await self._apply_runtime_content_defaults(
        comms_message.msg_content, incoming_content
    )
    comms_message.msg_content = content
    await self.task_manager.operations_runtime.run(
        self.task_manager.mark_turn_action_record,
        record_id,
        status="prepared",
        phase="validate",
    )
    if isinstance(action, TaskResponseAction):
        await _repair_invalid_explicit_suppressed_task_response(
            self,
            action=action,
            incoming_content=incoming_content,
            incoming_meta=incoming_meta,
            outgoing_content=content,
        )
    self._validate_task_turn_llm_output(
        incoming_content=incoming_content,
        outgoing_content=content,
    )

    header = await construct_header(
        comms_message=comms_message,
        explicit_to_path=requester_bound_primary_route(
            content,
            incoming_header,
            instance_config_path=self.instance_config_path,
            team_id=self.team_id,
            requester_type=incoming_meta.requester_type,
            sender_is_team_lead=bool(getattr(self, "is_team_lead", False)),
        ),
        route_response_to_self=False,
        agent_id=self.id,
        agent_name=self.name,
        team_id=self.team_id,
        named_agent_type_queue_map=self.named_agent_type_queue_map,
        agent_id_by_name=self._agent_id_by_name,
        logger=self._logger,
        repair_recorder=getattr(self, "_record_runtime_repair_anomaly", None),
    )
    parsed_comms_snapshot = self._parsed_comms_routing_snapshot(comms_message, header)
    if (
        isinstance(content, Task)
        and content.task_type == MessageMetaType.REQUEST
        and header.to_routing_path == RoutingType.NULL_ROUTE.value
    ):
        self._logger.warning(
            "[%s] Outbound Task request resolved to NULL_ROUTE before dispatch. parsed=%s structured_tool_input=%s",
            self.id,
            _compact_debug_preview(parsed_comms_snapshot, max_chars=1600),
            _compact_debug_preview(structured_output_details, max_chars=2200),
        )
    meta = await build_outgoing_meta(
        llm_meta=comms_message.msg_meta,
        incoming_meta=incoming_meta,
        content=content,
        from_agent_name=self.name,
        repair_recorder=getattr(self, "_record_runtime_repair_anomaly", None),
    )
    self._validate_outbound_request_routing(
        content=content,
        header=header,
        meta=comms_message.msg_meta,
        incoming_content=incoming_content,
        structured_output_details=structured_output_details,
        parsed_comms_snapshot=parsed_comms_snapshot,
    )

    local_finalization_task_id: Optional[str] = None
    after_action_report_sent = False
    suppress_outbound_dispatch = False
    post_activation_guard_content = await self._suppress_post_activation_plan_bypass(
        incoming_content=incoming_content,
        outgoing_content=content,
    )
    if post_activation_guard_content is not None:
        content = post_activation_guard_content
        suppress_outbound_dispatch = True

    delegated_target_type: Optional[str] = None
    delegated_child_task_id: Optional[str] = None
    delegated_target_verified = False
    delegated_side_effects = dict(record.side_effects or {})

    await self.task_manager.operations_runtime.run(
        self.task_manager.mark_turn_action_record, record_id, phase="local_commit"
    )
    if isinstance(content, Task):
        if suppress_outbound_dispatch and post_activation_guard_content is not None:
            pass
        elif content.task_type == MessageMetaType.REQUEST and _is_report_from_worker(
            self, comms_message, incoming_content
        ):
            content, header, meta = await prepare_worker_result_report(
                self,
                comms_message=comms_message,
                parent_task_info=await _incoming_task_info(self, incoming_content),
                header=header,
                record_id=record_id,
            )
        elif content.task_type == MessageMetaType.REQUEST:
            existing_delegated_task_id = delegated_side_effects.get("delegated_task_id")
            if existing_delegated_task_id:
                task_info = await self.task_manager.operations_runtime.run(
                    self.task_manager.get_task_info, str(existing_delegated_task_id)
                )
                if task_info is None:
                    raise RuntimeError(
                        f"Runtime action {record_id} recorded delegated subtask "
                        f"{existing_delegated_task_id}, but the local task record is missing."
                    )
            else:
                task_info = await self._create_subtask_request(comms_message)
                delegated_target_verified = task_info is not None
            if not task_info:
                raise RuntimeError(
                    "Failed to register delegated subtask locally; aborting outbound dispatch."
                )
            content = self._task_request_transport_copy(task_info.task)
            header = task_info.header
            meta = task_info.meta
            delegated_target_type = getattr(
                meta, "to_named_agent_type", None
            )
            delegated_child_task_id = content.task_id
            await self.task_manager.operations_runtime.run(
                self.task_manager.mark_turn_action_record,
                record_id,
                side_effects={
                    "delegated_task_id": content.task_id,
                    "parent_task_id": content.parent_task_id,
                    "target_named_agent_type": delegated_target_type,
                    "target_route": header.to_routing_path,
                },
            )
        elif (
            isinstance(incoming_content, Task)
            and content.task_type == MessageMetaType.RESPONSE
            and content.task_id == incoming_content.task_id
        ):
            deferred_wait_content = (
                await _defer_terminal_task_response_for_pending_parent_work(
                    self,
                    incoming_content=incoming_content,
                    outgoing_content=content,
                    record_id=record_id,
                )
            )
            if deferred_wait_content is not None:
                content = deferred_wait_content
                suppress_outbound_dispatch = True
            elif await self.task_manager.operations_runtime.run(
                self.task_manager.is_plan_execution_root_active,
                incoming_content.task_id,
            ):
                now_ts = datetime.now(timezone.utc).isoformat()
                await self.task_manager.operations_runtime.run(
                    self.task_manager.update_main_task_state,
                    incoming_content.task_id,
                    ProgressState.WAITING,
                    now_ts,
                )
                await self.task_manager.operations_runtime.run(
                    self.task_manager.append_history_entry,
                    incoming_content.task_id,
                    "Runtime plan execution is active; suppressing immediate root response until plan steps complete.",
                    entry_type=TaskHistoryType.PLAN_EVENT,
                    importance=TaskHistoryImportance.HIGH,
                    timestamp=now_ts,
                    agent_name=self.name,
                    agent_id=self.id,
                    tracks_to_plan=True,
                    details={"action": "defer_root_response_for_plan_execution"},
                )
                content = Task(
                    task_id=incoming_content.task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Runtime plan execution is active; root task is waiting for internal plan steps.",
                )
                suppress_outbound_dispatch = True
            else:
                final_review = await self.task_manager.operations_runtime.run(
                    self.task_manager.begin_final_review_if_needed,
                    incoming_content.task_id,
                    attempted_answer=content.answer,
                    agent_name=self.name,
                    agent_id=self.id,
                )
                if final_review is not None:
                    self._logger.info(
                        "[%s] Suppressing terminal response for bounded final review. task_id=%s attempt=%s",
                        self.id,
                        incoming_content.task_id,
                        final_review.get("attempt"),
                    )
                    suppress_outbound_dispatch = True
                    content = Task(
                        task_id=incoming_content.task_id,
                        task_type=MessageMetaType.RESPONSE,
                        answer=content.answer,
                        errors=content.errors,
                        task_plan=content.task_plan,
                    )
                else:
                    self.emit_activity_event(
                        activity_type=TaskActivityType.SYNTHESIZING,
                        stage=TaskActivityStage.SYNTHESIZING,
                        task_id=content.task_id,
                        meta=incoming_meta,
                        summary="Synthesizing final task response.",
                        detail="Preparing the final structured task reply.",
                    )
                    explicit_suppressed_response = (
                        isinstance(action, TaskResponseAction)
                        and action.suppress_outbound_dispatch
                    )
                    auto_suppressed_unrequested_agent_response = (
                        not explicit_suppressed_response
                        and _should_suppress_unrequested_agent_response(
                            incoming_content=incoming_content,
                            incoming_meta=incoming_meta,
                            outgoing_content=content,
                        )
                    )
                    auto_suppressed_timer_notify_user_response = (
                        not explicit_suppressed_response
                        and not auto_suppressed_unrequested_agent_response
                        and await _should_suppress_timer_response_after_notify_user(
                            self,
                            incoming_content=incoming_content,
                            incoming_meta=incoming_meta,
                            outgoing_content=content,
                        )
                    )
                    if explicit_suppressed_response:
                        if (
                            await self.task_manager.operations_runtime.run(
                                self.task_manager.get_task_info,
                                incoming_content.task_id,
                            )
                            is None
                        ):
                            raise RuntimeError(
                                f"Unable to finalize suppressed task response for task {incoming_content.task_id}."
                            )
                        await _mark_suppressed_task_delivery(
                            self,
                            task=content,
                            incoming_meta=incoming_meta,
                            record_id=record_id,
                        )
                    if auto_suppressed_unrequested_agent_response:
                        if (
                            await self.task_manager.operations_runtime.run(
                                self.task_manager.get_task_info,
                                incoming_content.task_id,
                            )
                            is None
                        ):
                            raise RuntimeError(
                                "Unable to finalize unrequested agent task response for task "
                                f"{incoming_content.task_id}."
                            )
                        await _mark_suppressed_task_delivery(
                            self,
                            task=content,
                            incoming_meta=incoming_meta,
                            record_id=record_id,
                            summary=(
                                "Top-level agent task completed without a terminal reply because "
                                "the requester did not request one."
                            ),
                            reason="response_not_requested",
                            delivery_via="runtime_no_response_requested",
                        )
                    if auto_suppressed_timer_notify_user_response:
                        if (
                            await self.task_manager.operations_runtime.run(
                                self.task_manager.get_task_info,
                                incoming_content.task_id,
                            )
                            is None
                        ):
                            raise RuntimeError(
                                f"Unable to finalize auto-suppressed timer response for task {incoming_content.task_id}."
                            )
                        await _mark_suppressed_task_delivery(
                            self,
                            task=content,
                            incoming_meta=incoming_meta,
                            record_id=record_id,
                            summary=(
                                "Task completed after notify_user delivery; terminal response suppressed "
                                "to avoid duplicate user-visible delivery."
                            ),
                            reason="notify_user_already_sent",
                            delivery_state=DELIVERY_STATE_NOTIFICATION_QUEUED,
                            delivery_via="runtime_notify_user",
                        )
                    if await self._prepare_local_task_finalization_before_dispatch(
                        incoming_task=incoming_content,
                        outgoing_task=content,
                    ):
                        local_finalization_task_id = incoming_content.task_id
                        suppress_outbound_dispatch = (
                            await self.task_manager.operations_runtime.run(
                                self.task_manager.is_isolated_recovery_task,
                                incoming_content.task_id,
                            )
                            or await self.task_manager.operations_runtime.run(
                                self.task_manager.is_plan_step_task,
                                incoming_content.task_id,
                            )
                            or explicit_suppressed_response
                            or auto_suppressed_unrequested_agent_response
                            or auto_suppressed_timer_notify_user_response
                        )
                    elif (
                        explicit_suppressed_response
                        or auto_suppressed_unrequested_agent_response
                        or auto_suppressed_timer_notify_user_response
                    ):
                        raise RuntimeError(
                            f"Unable to finalize suppressed task response for task {incoming_content.task_id}."
                        )
    elif isinstance(content, TaskQuery):
        if content.query_type == MessageMetaType.REQUEST:
            parent_task_id = content.parent_task_id
            if parent_task_id:
                await self.task_manager.operations_runtime.run(
                    self.task_manager.add_task_query,
                    parent_task_id,
                    content,
                    agent_name=self.name,
                    agent_id=self.id,
                )
                if self._conversation_store:
                    await self.task_manager.operations_runtime.run(
                        self._conversation_store.append_clarification,
                        task_id=parent_task_id,
                        message=f"{content.query_type.value} from {self.name}: {content.message}",
                    )
        else:
            self.emit_activity_event(
                activity_type=TaskActivityType.SYNTHESIZING,
                stage=TaskActivityStage.SYNTHESIZING,
                task_id=content.parent_task_id,
                meta=incoming_meta,
                summary="Preparing clarification response.",
                detail=content.message,
            )
    elif isinstance(content, SecureCredentialCollectionRequest):
        if content.request_type == MessageMetaType.REQUEST and content.parent_task_id:
            await self.task_manager.operations_runtime.run(
                self.task_manager.append_history_entry,
                content.parent_task_id,
                f"Secure credential request emitted for {content.credential_key}.",
                entry_type=TaskHistoryType.QUERY_EVENT,
                importance=TaskHistoryImportance.HIGH,
                agent_name=self.name,
                agent_id=self.id,
                details={
                    "event": "secure_credential_request",
                    "credential_request_id": content.credential_request_id,
                    "credential_key": content.credential_key,
                    "service_name": content.service_name,
                    "field_names": [field.name for field in content.fields],
                },
            )
    elif isinstance(content, SignupAutomationRequest):
        if content.request_type == MessageMetaType.REQUEST and content.parent_task_id:
            await self.task_manager.operations_runtime.run(
                self.task_manager.append_history_entry,
                content.parent_task_id,
                f"Approved signup automation request emitted for {content.credential_key}.",
                entry_type=TaskHistoryType.QUERY_EVENT,
                importance=TaskHistoryImportance.HIGH,
                agent_name=self.name,
                agent_id=self.id,
                details={
                    "event": "signup_automation_request",
                    "signup_request_id": content.signup_request_id,
                    "credential_key": content.credential_key,
                    "service_name": content.service_name,
                    "signup_url": content.signup_url,
                    "password_field_name": content.password_field_name,
                },
            )
    elif isinstance(content, ScheduledMessage):
        # The model's schedule schema leaves routing to trusted runtime code.
        # Supplying it here is normal construction, not a malformed-message repair.
        if content.payload_header is None:
            content.payload_header = incoming_header.model_copy(deep=True)
        if content.payload_meta is None:
            content.payload_meta = incoming_meta.model_copy(deep=True)
        await self._handle_scheduled_message(
            content, msg_header=incoming_header, msg_meta=incoming_meta
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_record,
            record_id,
            status="completed",
            phase="complete",
        )
        return content

    header.outbox_action_id = record_id
    header.idempotency_key = record.idempotency_key or record_id
    routing_key = await resolve_outgoing_routing(
        content=content,
        header=header,
        incoming_header=incoming_header,
        incoming_meta=incoming_meta,
        agent_id_by_name=self._agent_id_by_name,
        named_agent_type_queue_map=self.named_agent_type_queue_map,
        team_id=self.team_id,
        logger=self._logger,
        agent_id=self.id,
        repair_recorder=getattr(self, "_record_runtime_repair_anomaly", None),
    )
    await self.task_manager.operations_runtime.run(
        self.task_manager.mark_turn_action_record,
        record_id,
        status="dispatching",
        phase="publish",
        target_route=routing_key,
        message_id=header.message_id,
        increment_attempt=True,
    )
    if is_reply_routing_trace_enabled(getattr(self, "debug", None)):
        self._logger.info(
            "[%s] event=turn_action_dispatch_prepared action_id=%s action_type=%s "
            "task_id=%s routing_key=%s header_to=%s header_response_routing_key=%s "
            "msg_correlation_id=%s conversation_id=%s gateway_context=%s content=%s "
            "suppress_outbound_dispatch=%s",
            self.id,
            record_id,
            getattr(action, "action_type", None),
            getattr(content, "task_id", None),
            routing_key,
            header.to_routing_path,
            header.response_routing_key,
            getattr(meta, "msg_correlation_id", None),
            getattr(meta, "conversation_id", None),
            compact_gateway_context(getattr(meta, "gateway_context", None)),
            compact_content_context(content),
            suppress_outbound_dispatch,
        )
    self._logger.info(
        f"[agent: {self.name}] Generated runtime action CommsMessage | action_id: {record_id} "
        f"| content Type: {content.__class__.__name__} (id: {content.get_id()}) | to_routing_path: {header.to_routing_path}"
    )

    active_task_id = self._history_task_id_for_content(incoming_content)
    self._raise_if_stop_requested(active_task_id)
    if await self.task_manager.operations_runtime.run(
        self.task_manager.is_execution_cancelled, active_task_id
    ):
        raise RuntimeBatchInterrupted(
            f"Execution graph for task {active_task_id} was cancelled before action publication."
        )
    publish_sequence: Optional[int] = None
    # Held back: the runtime swallowed this action and the task goes on. A
    # suppressed answer that finishes the task locally is not held back.
    held_back = suppress_outbound_dispatch and local_finalization_task_id is None
    if suppress_outbound_dispatch:
        self._logger.info(
            "[%s] Suppressing outbound dispatch for internal runtime response task_id=%s action_id=%s.",
            self.id,
            getattr(content, "task_id", None),
            record_id,
        )
    else:
        if (
            _is_waiting_delegation(content, meta)
            and not delegated_target_verified
        ):
            target_agent_name = getattr(meta, "to_agent_name", None)
            target_named_agent_type = getattr(meta, "to_named_agent_type", None)
            if target_agent_name:
                routing_key = await _current_exact_route(
                    self, header, routing_key, target_named_agent_type, target_agent_name
                )
            elif not await self._verify_delegation_target(target_named_agent_type, None):
                raise RuntimeError(
                    "Delegated task was not published because its worker type is not "
                    "registered."
                )
        await _record_delegation_before_publish(self, content, meta, delegated_target_type)
        out_env = MessageEnvelope(msg_content=content, msg_meta=meta, msg_header=header)
        publish_sequence = await self.team_comms.send_message(
            message=out_env, routing_key=routing_key
        )
    await self.task_manager.operations_runtime.run(
        self.task_manager.mark_turn_action_record,
        record_id,
        status=TURN_ACTION_HELD_BACK if held_back else "dispatched",
        phase="publish",
        side_effects=None if suppress_outbound_dispatch else _publish_confirmation(content, meta, publish_sequence),
    )

    if _is_waiting_delegation(content, meta):
        await _after_delegation_published(
            self,
            record_id=record_id,
            action=action,
            content=content,
            delegated_child_task_id=delegated_child_task_id,
            delegated_target_type=delegated_target_type,
            publish_sequence=publish_sequence,
        )
    elif (
        isinstance(content, TaskQuery)
        and content.query_type == MessageMetaType.REQUEST
        and content.parent_task_id
    ):
        next_state = (
            ProgressState.PAUSED
            if content.query_reason == TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE
            else ProgressState.WAITING
        )
        parent_root_task_id = (
            await self.task_manager.operations_runtime.run(
                self.task_manager.resolve_root_task_id, content.parent_task_id
            )
            or content.parent_task_id
        )
        if (
            await self.task_manager.operations_runtime.run(
                self.task_manager.get_active_runtime_lease, parent_root_task_id
            )
            is None
        ):
            await self.task_manager.operations_runtime.run(
                self.task_manager.update_main_task_state,
                content.parent_task_id,
                next_state,
                datetime.now(timezone.utc).isoformat(),
            )
    elif (
        isinstance(
            content, SecureCredentialCollectionRequest
        )
        and content.request_type == MessageMetaType.REQUEST
        and content.parent_task_id
    ):
        parent_root_task_id = (
            await self.task_manager.operations_runtime.run(
                self.task_manager.resolve_root_task_id, content.parent_task_id
            )
            or content.parent_task_id
        )
        if (
            await self.task_manager.operations_runtime.run(
                self.task_manager.get_active_runtime_lease, parent_root_task_id
            )
            is None
        ):
            await self.task_manager.operations_runtime.run(
                self.task_manager.update_main_task_state,
                content.parent_task_id,
                ProgressState.WAITING,
                datetime.now(timezone.utc).isoformat(),
            )
    elif (
        isinstance(content, SignupAutomationRequest)
        and content.request_type == MessageMetaType.REQUEST
        and content.parent_task_id
    ):
        parent_root_task_id = (
            await self.task_manager.operations_runtime.run(
                self.task_manager.resolve_root_task_id, content.parent_task_id
            )
            or content.parent_task_id
        )
        if (
            await self.task_manager.operations_runtime.run(
                self.task_manager.get_active_runtime_lease, parent_root_task_id
            )
            is None
        ):
            await self.task_manager.operations_runtime.run(
                self.task_manager.update_main_task_state,
                content.parent_task_id,
                ProgressState.WAITING,
                datetime.now(timezone.utc).isoformat(),
            )

    if (
        not suppress_outbound_dispatch
        and after_action_report is not None
        and isinstance(incoming_content, Task)
        and isinstance(content, Task)
        and content.task_type == MessageMetaType.RESPONSE
    ):
        try:
            after_action_report_sent = await self._send_after_action_report_followup(
                report=after_action_report,
                source_task=incoming_content,
                incoming_header=incoming_header,
                incoming_meta=incoming_meta,
            )
        except Exception as exc:
            self._logger.warning(
                "[%s] Failed to send non-blocking AfterActionReport for task %s: %s",
                self.id,
                incoming_content.task_id,
                exc,
            )

    if local_finalization_task_id:
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_record,
            record_id,
            phase="post_publish_finalize",
        )
        await self._complete_local_task_finalization_after_dispatch(
            task_id=local_finalization_task_id,
            after_action_report_sent=after_action_report_sent,
        )

    await self.task_manager.operations_runtime.run(
        self.task_manager.mark_turn_action_record,
        record_id,
        status=TURN_ACTION_HELD_BACK if held_back else "completed",
        phase="complete",
    )
    return content


async def _after_delegation_published(
    self: Any,
    *,
    record_id: str,
    action: Any,
    content: Task,
    delegated_child_task_id: Optional[str],
    delegated_target_type: Optional[str],
    publish_sequence: Optional[int],
) -> None:
    """What follows a published delegation: notice, parent wait, stream sequence, check-back."""
    wait_notification_sent = await _notify_user_delegated_work_started(
        self,
        parent_task_id=content.parent_task_id,
        delegated_task_id=delegated_child_task_id,
        target_named_agent_type=delegated_target_type,
    )
    if wait_notification_sent:
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_record,
            record_id,
            side_effects={"delegation_wait_notification_sent": True},
        )
    parent_root_task_id = (
        await self.task_manager.operations_runtime.run(
            self.task_manager.resolve_root_task_id, content.parent_task_id
        )
        or content.parent_task_id
    )
    if (
        await self.task_manager.operations_runtime.run(
            self.task_manager.get_active_runtime_lease, parent_root_task_id
        )
        is None
    ):
        await self.task_manager.operations_runtime.run(
            self.task_manager.update_main_task_state,
            content.parent_task_id,
            ProgressState.WAITING,
            datetime.now(timezone.utc).isoformat(),
        )
    # The task waits in its worker type's shared queue until a free worker
    # takes it (ADR-019); its record (written before publishing) gets the
    # stream sequence so a stop can withdraw it while no worker holds it.
    await self.task_manager.operations_runtime.run(
        partial(
            record_publish_sequence,
            self,
            subtask_id=content.task_id,
            publish_sequence=publish_sequence,
        )
    )
    if isinstance(action, DelegateTaskAction) and action.check_back_after_minutes:
        await self.task_manager.operations_runtime.run(
            partial(
                record_check_back,
                self,
                parent_task_id=content.parent_task_id,
                minutes=action.check_back_after_minutes,
            )
        )
    await self.task_manager.operations_runtime.run(
        self.task_manager.mark_turn_action_record,
        record_id,
        side_effects={"delegated_publish_sequence": publish_sequence},
    )


async def _finish_claim_confirmed_delegation(
    self: Any, *, record_id: str, action: Any, exc: Exception
) -> Optional[Task]:
    """Finish a delegation whose publish call failed although a worker already took the task.

    JetStream stored the message and only its acknowledgement was lost: the
    worker's claim (the delegated record has a holder) proves the publish.
    The action is finished as dispatched, its post-publish steps run, and
    the caller goes on with the rest of the batch; the outbox recovery (later
    actions blocked, the parent PARTIAL with a recovery turn, the check-back
    lost) would treat a sent task as a failure (2026-10-17). Returns the
    delegated task as the action's result, or None when the publish is not
    confirmed.
    """
    if not isinstance(action, DelegateTaskAction):
        return None
    record = await self.task_manager.operations_runtime.run(
        self.task_manager.get_turn_action_record, record_id
    )
    if record is None or (record.status, record.phase) != ("dispatching", "publish"):
        return None
    subtask_id = (record.side_effects or {}).get("delegated_task_id")
    holder = (
        await self.task_manager.operations_runtime.run(partial(delegation_holder, self, str(subtask_id)))
        if subtask_id
        else None
    )
    task_info = (
        await self.task_manager.operations_runtime.run(self.task_manager.get_task_info, str(subtask_id))
        if holder
        else None
    )
    if task_info is None:
        return None
    await self.task_manager.operations_runtime.run(
        self.task_manager.mark_turn_action_record,
        record_id,
        status="dispatched",
        side_effects={PUBLISH_CONFIRMED: PUBLISHED_BY_CLAIM, "publish_confirmed_by_claim": holder},
    )
    self._logger.warning(
        "A delegated task's publish reported a failure, but a worker took the task, so it was "
        "sent. event=delegated_task_publish_confirmed_by_claim agent_id=%s task_id=%s subtask_id=%s "
        "action_id=%s holder_agent_id=%s error_type=%s",
        self.id,
        record.source_task_id,
        subtask_id,
        record_id,
        holder,
        type(exc).__name__,
    )
    refreshed = await self.task_manager.operations_runtime.run(
        self.task_manager.get_turn_action_record, record_id
    )
    return await _complete_published_delegation(self, record=refreshed or record, action=action)


def _publish_confirmation(content: Any, meta: MessageMeta, publish_sequence: Optional[int]) -> Optional[Dict[str, Any]]:
    """Side effects that record a delegated task's successful publish, or None for other actions."""
    if not _is_waiting_delegation(content, meta):
        return None
    return {PUBLISH_CONFIRMED: PUBLISHED_BY_CALL, "delegated_publish_sequence": publish_sequence}


async def _complete_published_delegation(self: Any, *, record: Any, action: Any) -> Task:
    """Run the steps after a delegated task's publish, for an action whose publish is confirmed."""
    side_effects = record.side_effects or {}
    subtask_id = str(side_effects.get("delegated_task_id"))
    task_info = await self.task_manager.operations_runtime.run(self.task_manager.get_task_info, subtask_id)
    if task_info is None:
        raise RuntimeError(
            f"Runtime action {record.action_id} published delegated subtask {subtask_id}, "
            "but the local task record is missing."
        )
    content = self._task_request_transport_copy(task_info.task)
    await self.task_manager.operations_runtime.run(
        self.task_manager.mark_turn_action_record, record.action_id, status="dispatched", phase="publish",
    )
    await _after_delegation_published(
        self,
        record_id=record.action_id,
        action=action,
        content=content,
        delegated_child_task_id=subtask_id,
        delegated_target_type=side_effects.get("target_named_agent_type"),
        publish_sequence=side_effects.get("delegated_publish_sequence"),
    )
    await self.task_manager.operations_runtime.run(
        self.task_manager.mark_turn_action_record, record.action_id, status="completed", phase="complete"
    )
    return content


async def _execute_turn_action_with_recovery(
    self,
    *,
    batch_id: str,
    record_id: str,
    action: TurnAction,
    incoming_content: ContentType,
    incoming_header: MessageHeader,
    incoming_meta: MessageMeta,
    structured_output_details: Optional[Dict[str, Any]] = None,
) -> ContentType:
    try:
        return await self._execute_turn_action_record(
            record_id=record_id,
            action=action,
            incoming_content=incoming_content,
            incoming_header=incoming_header,
            incoming_meta=incoming_meta,
            structured_output_details=structured_output_details,
        )
    except TurnActionRecoveryScheduled:
        raise
    except Exception as exc:
        confirmed = await _finish_claim_confirmed_delegation_or_none(
            self, record_id=record_id, action=action, exc=exc
        )
        if confirmed is not None:
            return confirmed
        await self._schedule_turn_action_recovery(
            batch_id=batch_id,
            action_id=record_id,
            exc=exc,
        )
        raise


async def _finish_claim_confirmed_delegation_or_none(
    self: Any, *, record_id: str, action: Any, exc: Exception
) -> Optional[Task]:
    """``_finish_claim_confirmed_delegation``; a failure while finishing falls back to the outbox recovery (logged)."""
    try:
        return await _finish_claim_confirmed_delegation(self, record_id=record_id, action=action, exc=exc)
    except Exception:
        self._logger.exception(
            "Could not finish a delegated task a worker already took; the outbox recovery handles it. "
            "event=delegated_task_confirm_failed agent_id=%s action_id=%s",
            self.id,
            record_id,
        )
        return None


async def _current_exact_route(
    agent: Any,
    header: Any,
    routing_key: str,
    named_agent_type: Optional[str],
    target_agent_name: str,
) -> str:
    """The named worker's mailbox now, for a retried publish.

    A worker restarted since the first attempt answers under a new id; its
    old mailbox would hold the task for nobody.
    """

    from custom_agents.delegation_runtime import exact_delegation_route

    route = await exact_delegation_route(agent, named_agent_type, target_agent_name)
    if route is None:
        raise RuntimeError(
            "Delegated task was not published because its exact target agent is not live."
        )
    if route != routing_key:
        agent._logger.warning(
            "A named worker answers under a new id; sending the task to its current mailbox. "
            "event=exact_delegation_target_moved agent_id=%s target_agent_name=%s "
            "old_route=%s new_route=%s",
            agent.id,
            target_agent_name,
            routing_key,
            route,
        )
        header.to_routing_path = route
    return route


async def _execute_turn_decision_outbox(
    self,
    *,
    decision: StructuredAgentTurnDecision,
    incoming_content: ContentType,
    incoming_header: MessageHeader,
    incoming_meta: MessageMeta,
    structured_output_details: Optional[Dict[str, Any]] = None,
    start_notes: Optional[TurnStartNotes] = None,
) -> ContentType:
    """Record the turn's decision as a durable batch, then carry out its actions.

    ``start_notes`` (what the deciding turn read) are released in the batch's
    own save (``TurnStartNotes``): an action that fails later, or a shutdown
    during the actions, leaves them released, because the decision is recorded.
    """
    source_task_id = self._history_task_id_for_content(incoming_content)
    if not source_task_id:
        if len(decision.actions) != 1:
            raise RuntimeError(
                "Cannot execute multi-action turn without a source task id."
            )
        comms_message = self._comms_message_from_turn_action(decision.actions[0])
        if comms_message is None:
            raise RuntimeError(
                f"Unsupported turn action type: {type(decision.actions[0]).__name__}"
            )
        return comms_message.msg_content
    await self._ensure_turn_action_source_task(
        source_task_id=source_task_id,
        incoming_content=incoming_content,
        incoming_header=incoming_header,
        incoming_meta=incoming_meta,
    )

    action_payloads = []
    for action in decision.actions:
        _supply_reply_linkage(action, incoming_content)
        action_payload = action.model_dump(mode="json", exclude_none=True)
        task_id = None
        if isinstance(action, (DelegateTaskAction, TaskResponseAction)):
            task_id = action.task.task_id
        action_payloads.append(
            {
                "action_type": action_payload.get("action_type"),
                "action": action_payload,
                "task_id": task_id,
                "incoming_header": incoming_header.model_dump(mode="json"),
                "incoming_meta": incoming_meta.model_dump(mode="json"),
                "recoverability": "automatic_retry",
            }
        )
    batch = await self.task_manager.operations_runtime.run(
        self.task_manager.create_turn_action_batch,
        source_task_id,
        action_payloads,
        plan_id=await self.task_manager.operations_runtime.run(
            self.task_manager.get_task_plan_id, source_task_id
        ),
        release_notes=start_notes,
    )
    if batch is None:
        raise RuntimeError("Failed to create durable runtime action batch.")
    root_task_id = (
        await self.task_manager.operations_runtime.run(
            self.task_manager.resolve_root_task_id, source_task_id
        )
        or source_task_id
    )
    coordinator = self._runtime_coordinator()
    lease = await self.task_manager.operations_runtime.run(
        coordinator.begin_batch,
        root_task_id=root_task_id,
        source_task_id=source_task_id,
        batch_id=batch.batch_id,
    )
    lease_id = lease.lease_id if lease is not None else None

    if decision.summary_for_history:
        await self.task_manager.operations_runtime.run(
            self.task_manager.append_history_entry,
            source_task_id,
            f"Runtime action decision: {decision.summary_for_history}",
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.NORMAL,
            agent_name=self.name,
            agent_id=self.id,
            details={"event": "turn_decision_validated", "batch_id": batch.batch_id},
        )

    result_content: ContentType = NoOpMessage(
        info_message="No runtime action executed."
    )
    records = await self.task_manager.operations_runtime.run(
        self.task_manager.list_turn_action_records, batch.batch_id
    )
    batch_final_status = "completed"
    try:
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_batch_status, batch.batch_id, "executing"
        )
        for record, action in zip(records, decision.actions):
            await self.task_manager.operations_runtime.run(
                self.task_manager.mark_turn_action_batch_status,
                batch.batch_id,
                "executing",
                current_action_index=record.action_index,
            )
            await self.task_manager.operations_runtime.run(
                coordinator.update_batch_action_index, lease_id, record.action_index
            )
            result_content = await self._execute_turn_action_with_recovery(
                batch_id=batch.batch_id,
                record_id=record.action_id,
                action=action,
                incoming_content=incoming_content,
                incoming_header=incoming_header,
                incoming_meta=incoming_meta,
                structured_output_details=structured_output_details,
            )
            await self.task_manager.operations_runtime.run(
                coordinator.interrupt_if_requested,
                lease_id,
                batch.batch_id,
                record.action_index,
            )
            external_wait_reason = _external_wait_barrier_reason(result_content)
            if external_wait_reason:
                now_ts = datetime.now(timezone.utc).isoformat()
                await self.task_manager.operations_runtime.run(
                    self.task_manager.update_main_task_state,
                    source_task_id,
                    ProgressState.WAITING,
                    now_ts,
                )
                await self.task_manager.operations_runtime.run(
                    self.task_manager.block_turn_actions_after,
                    batch.batch_id,
                    record.action_index,
                    reason=external_wait_reason,
                )
                await self.task_manager.operations_runtime.run(
                    self.task_manager.append_history_entry,
                    source_task_id,
                    external_wait_reason,
                    entry_type=TaskHistoryType.QUERY_EVENT,
                    importance=TaskHistoryImportance.HIGH,
                    agent_name=self.name,
                    agent_id=self.id,
                    details={
                        "event": "turn_action_external_wait_barrier",
                        "batch_id": batch.batch_id,
                        "action_id": record.action_id,
                        "action_index": record.action_index,
                        "content_type": type(result_content).__name__,
                    },
                )
                batch_final_status = "waiting_external_result"
                break
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_batch_status,
            batch.batch_id,
            batch_final_status,
        )
    except RuntimeBatchInterrupted:
        batch_final_status = "interrupted"
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_turn_action_batch_status, batch.batch_id, "failed"
        )
        raise
    except Exception:
        batch_final_status = "failed"
        raise
    finally:
        await self.task_manager.operations_runtime.run(
            coordinator.complete_batch,
            lease_id=lease_id,
            root_task_id=root_task_id,
            batch_id=batch.batch_id,
            status=batch_final_status,
        )
    return result_content
