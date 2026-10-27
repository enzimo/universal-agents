"""Task request, response, query, and local execution-loop helpers."""

from __future__ import annotations

from custom_agents.strands_parts import task_strands_messages
from custom_messages.records.task_results import TaskResultUpdate

import asyncio
import json
from datetime import datetime, timezone
from functools import partial
from typing import Any, Awaitable, Callable, Optional, Union


from custom_tools.runtime_context import task_work
from runtime_hosting.agent_execution import MAIN_LANE
from custom_agents.control_runtime import handle_interrupted_turn
from custom_agents.lease_loss_wind_down import may_start_next_task
from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.delegated_work_runtime import send_task_claim
from custom_agents.task_custody import (
    CUSTODY_FINISHED_MEANWHILE,
    CUSTODY_HANDED_BACK,
    CUSTODY_RELEASED_MEANWHILE,
    CUSTODY_TAKEN_MEANWHILE,
    CustodyDecision,
    log_dropped_copy,
)
from custom_agents.task_custody import check_admission as check_task_custody
from storage.task_custody_repository import TaskCustodyConflict, TaskCustodyFinished, TaskCustodyReleased
from custom_agents.worker_handback_runtime import apply_redispatch_notice
from custom_agents.conversation_delivery_runtime import handle_delivery_receipt
from custom_agents.conversation_runtime import (
    ensure_task_conversation_context,
    finalize_conversation_turn,
    record_task_conversation_history,
)
from custom_agents.message_routing import (
    build_outgoing_meta,
    construct_header,
    resolve_outgoing_routing,
)
from custom_agents.model_failover_runtime import ProviderFailoverRequeued
from custom_agents.runtime_budget_policy import runtime_pause_identity
from custom_agents.access_runtime import verified_meta_person_id
from custom_agents.task_context_runtime import sign_system_escalation
from custom_agents.provider_error_runtime import (
    PROVIDER_TIMEOUT_USER_MESSAGE,
)
from custom_agents.task_manager import TaskInfo
from custom_agents.task_admission_runtime import report_failed_admission
from custom_agents.task_queue_notifications import check_queue_pressure
from custom_messages.contracts.runtime_snapshot_v3 import RecoverablePauseRecord
from custom_agents.task_steering_runtime import (
    consume_pending_proactive_guidance_message,
)
from custom_agents.task_runtime_coordinator import (
    task_query_response_root_task_id,
    task_response_root_task_id,
)
from custom_agents.task_resume_runtime import resume_task_with_directive
from custom_agents.turn_action_outbox_runtime import TurnActionRecoveryScheduled
from custom_agents.turn_failure_runtime import (
    fail_task_after_turn_error,
    release_orphaned_execution_slot,
    settle_failed_turn,
)
from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SecureCredentialCollectionResult,
    SignupAutomationRequest,
)
from custom_messages.contracts.delivery_v1 import DeliveryReceipt
from custom_messages.contracts.envelope_v1 import CommsMessage, MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering, TaskQuery
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    RecoverablePauseTrigger,
    RuntimeBudgetPolicyMode,
    TaskHistoryImportance,
    TaskHistorySource,
    TaskHistoryType,
    RequesterType,
    TaskQueryDecision,
    TaskQueryReason,
)


# What a specialist that asked for a credential form or signup is told: only
# TeamArchitect may ask the person (ADR-018), so it escalates instead.
DELEGATE_CREDENTIAL_REQUEST_REFUSAL = (
    "Only TeamArchitect may ask the person for credentials, sign-ins or accounts. "
    "Use manager_escalation with escalation_type needs_user_input: name the service "
    "and what the task needs it for. TeamArchitect sets it up with the person and "
    "answers; then continue the task."
)

_CREDENTIAL_DELETION_FLOWS = {
    "credential_deletion",
    "credential_delete",
    "delete_credential",
}
_CREDENTIAL_COLLECTION_FLOWS = {"", "credential_collection"}
_TERMINAL_CREDENTIAL_COLLECTION_STATUSES = {
    "credential_stored",
    "credential_unavailable",
}
_ORPHANED_CREDENTIAL_WORKFLOW_ERROR = (
    "The secure credential workflow completed after its originating task was no "
    "longer active."
)


def _is_timestamped_copy_of_error(value: str, error_message: str) -> bool:
    timestamp, separator, detail = str(value).partition(": ")
    if not separator or detail != error_message:
        return False
    try:
        datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return True


def _task_error_response_errors(
    existing_errors: Optional[list[str]],
    error_message: str,
) -> list[str]:
    """Build terminal errors while keeping the timeout response clean and singular."""

    if error_message != PROVIDER_TIMEOUT_USER_MESSAGE:
        errors = list(existing_errors or [])
        if error_message not in errors:
            errors.append(error_message)
        return errors

    errors = [
        str(existing)
        for existing in list(existing_errors or [])
        if str(existing) != error_message
        and not _is_timestamped_copy_of_error(str(existing), error_message)
    ]
    errors.append(error_message)
    return errors


async def _fail_delegated_subtask_after_pause_resolution(
    self,
    *,
    parent_task_id: str,
    pending_pause: RecoverablePauseRecord,
    decision: str,
    timestamp: str,
) -> None:
    """Terminalize a timed-out worker before the parent continues."""

    if (
        pending_pause.trigger != RecoverablePauseTrigger.DELEGATED_SUBTASK_TIMEOUT.value
        or not pending_pause.affected_subtask_id
    ):
        return
    subtask_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, pending_pause.affected_subtask_id
    )
    if subtask_info is None:
        return
    transitioned = await self.task_manager.operations_runtime.run(
        self.task_manager.try_transition_task_state,
        subtask_info.task.task_id,
        allowed_from={
            ProgressState.NOT_STARTED,
            ProgressState.PREPARING,
            ProgressState.ACCEPTED,
            ProgressState.IN_PROGRESS,
            ProgressState.WAITING,
            ProgressState.PAUSED,
            ProgressState.PARTIAL,
            ProgressState.RETRYING,
            ProgressState.UNDEFINED,
        },
        to_state=ProgressState.FAILED,
        timestamp=timestamp,
        reason=(
            "Requester resolved a delegated-subtask timeout and "
            "continued the parent without that worker result."
        ),
        source="runtime_pause_resolution",
    )
    if not transitioned:
        return
    subtask_info = (
        await self.task_manager.operations_runtime.run(
            self.task_manager.get_task_info, pending_pause.affected_subtask_id
        )
        or subtask_info
    )
    timeout_summary = (
        "Delegated subtask timed out; requester chose "
        f"{decision}, so the parent task will decide whether to retry, "
        "narrow, redelegate, or complete with caveats."
    )
    subtask_info = await self.task_manager.operations_runtime.run(
        self.task_manager.update_task_result,
        subtask_info.task.task_id,
        TaskResultUpdate(
            errors=list(
                dict.fromkeys([*(subtask_info.task.errors or []), timeout_summary])
            )
        ),
    )
    if subtask_info is None:
        return
    await self.task_manager.operations_runtime.run(
        self.task_manager.bubble_subtask_result,
        parent_task_id,
        subtask_info.task.task_id,
    )
    await self.task_manager.operations_runtime.run(
        self.task_manager.append_history_entry,
        parent_task_id,
        timeout_summary,
        entry_type=TaskHistoryType.SUBTASK_EVENT,
        importance=TaskHistoryImportance.CRITICAL,
        timestamp=timestamp,
        agent_name=self.name,
        agent_id=self.id,
        related_task_id=subtask_info.task.task_id,
        details={
            "action": f"{decision}_after_user_guided_subtask_timeout",
            "subtask_id": subtask_info.task.task_id,
        },
    )


def _is_credential_deletion_result(
    credential_result: SecureCredentialCollectionResult,
) -> bool:
    flow = str((credential_result.metadata or {}).get("flow") or "").strip().lower()
    if flow in _CREDENTIAL_DELETION_FLOWS:
        return True
    if credential_result.status == "credential_deleted":
        return True
    failure_text = (
        f"{credential_result.message or ''}\n{credential_result.error or ''}".lower()
    )
    return (
        credential_result.status == "credential_unavailable"
        and "deletion" in failure_text
    )


def _credential_service_label(
    credential_result: SecureCredentialCollectionResult,
) -> str:
    """Return the request-owned service label used in user-facing copy."""

    for value in (credential_result.service_name, credential_result.display_name):
        label = " ".join(str(value or "").split()).strip(" .")
        if label:
            return label[:120]
    return ""


def _credential_service_suffix(
    credential_result: SecureCredentialCollectionResult,
) -> str:
    service_name = _credential_service_label(credential_result)
    return f" for {service_name}" if service_name else ""


def _credential_form_expired(
    credential_result: SecureCredentialCollectionResult,
) -> bool:
    failure_text = (
        f"{credential_result.error or ''}\n{credential_result.message or ''}".casefold()
    )
    return (
        "timed out before submission" in failure_text
        or "interactive response expired" in failure_text
        or "credential collection session expired" in failure_text
    )


def _credential_deletion_result_answer(
    credential_result: SecureCredentialCollectionResult,
) -> tuple[str, bool]:
    service_suffix = _credential_service_suffix(credential_result)
    if credential_result.status == "credential_deleted":
        return (
            f"Your saved credentials{service_suffix} were deleted successfully.",
            True,
        )

    if _credential_form_expired(credential_result):
        return (
            f"I couldn't delete your saved credentials{service_suffix} because "
            "the secure confirmation form expired. Nothing was changed. Please start the request again.",
            False,
        )
    return (
        f"I couldn't delete your saved credentials{service_suffix}. Nothing was changed.",
        False,
    )


def _is_terminal_credential_collection_result(
    credential_result: SecureCredentialCollectionResult,
) -> bool:
    flow = str((credential_result.metadata or {}).get("flow") or "").strip().lower()
    return (
        flow in _CREDENTIAL_COLLECTION_FLOWS
        and credential_result.status in _TERMINAL_CREDENTIAL_COLLECTION_STATUSES
    )


def _credential_collection_result_answer(
    credential_result: SecureCredentialCollectionResult,
) -> tuple[str, bool]:
    service_suffix = _credential_service_suffix(credential_result)
    if credential_result.status == "credential_stored":
        return f"Your credentials{service_suffix} were stored successfully.", True

    if _credential_form_expired(credential_result):
        return (
            f"I couldn't store your credentials{service_suffix} because the secure "
            "form expired before it was submitted. Please start the request again.",
            False,
        )
    return (
        f"I couldn't store your credentials{service_suffix}. Please try again.",
        False,
    )


def _credential_result_resume_directive(
    credential_result: SecureCredentialCollectionResult,
) -> str:
    """Build a secret-free instruction for signup and manual-flow continuation."""

    facts = {
        "kind": "secure_credential_workflow_result",
        "status": credential_result.status,
        "service_name": credential_result.service_name,
        "display_name": credential_result.display_name,
        "flow": str((credential_result.metadata or {}).get("flow") or ""),
        "error": credential_result.error,
    }
    return (
        "A trusted secure credential workflow returned the secret-free structured "
        "result below. Continue the original task from this result. Describe any "
        "user-visible outcome in short, natural language that names the service. "
        "Do not quote status identifiers, credential keys, broker metadata, opaque "
        "ids, or secret-handling mechanics. Do not repeat a completed collection or "
        "signup action. When the credential was stored, go on with the next step of "
        "the task, for example set_up_tools for the tool so the person chooses it in the "
        "account link form, then use the tool. Start a manual credential step "
        "only when the result explicitly requires manual signup and the original task "
        "still needs it.\n"
        f"Structured credential result: {json.dumps(facts, sort_keys=True)}"
    )


def _orphaned_credential_collection_response(
    credential_result: SecureCredentialCollectionResult,
) -> tuple[str, list[str]]:
    """Render a secret-free terminal result when the original task is gone."""

    if _is_credential_deletion_result(credential_result):
        answer, succeeded = _credential_deletion_result_answer(credential_result)
    elif _is_terminal_credential_collection_result(credential_result):
        answer, succeeded = _credential_collection_result_answer(credential_result)
    else:
        service_suffix = _credential_service_suffix(credential_result)
        answer = (
            f"The secure credential step{service_suffix} finished after its original "
            "request was no longer active. Send a new message to continue."
        )
        succeeded = False
    if succeeded:
        answer = f"{answer} Send a new message to continue the original request."
    return answer, [credential_result.error or _ORPHANED_CREDENTIAL_WORKFLOW_ERROR]


async def _publish_task_response(
    self,
    *,
    response_task: Task,
    incoming_header: MessageHeader,
    incoming_meta: MessageMeta,
) -> Task:
    """Publish one Task response through the original request route."""

    response_meta = await build_outgoing_meta(
        llm_meta=MessageMeta(conversation_id=incoming_meta.conversation_id),
        incoming_meta=incoming_meta,
        content=response_task,
        from_agent_name=self.name,
        repair_recorder=getattr(self, "_record_runtime_repair_anomaly", None),
    )
    header = await construct_header(
        comms_message=CommsMessage(
            msg_content=response_task,
            msg_meta=response_meta,
        ),
        explicit_to_path=None,
        route_response_to_self=False,
        agent_id=self.id,
        agent_name=self.name,
        team_id=self.team_id,
        named_agent_type_queue_map=self.named_agent_type_queue_map,
        agent_id_by_name=self._agent_id_by_name,
        logger=self._logger,
        repair_recorder=getattr(self, "_record_runtime_repair_anomaly", None),
    )
    routing_key = await resolve_outgoing_routing(
        content=response_task,
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
    publish_team_id = self._extract_team_id_from_routing_value(routing_key)
    await self.team_comms.send_message(
        message=MessageEnvelope(
            msg_content=response_task,
            msg_meta=response_meta,
            msg_header=header,
        ),
        routing_key=routing_key,
        team_id=(
            publish_team_id
            if publish_team_id and publish_team_id != self.team_id
            else None
        ),
    )
    return response_task


async def _send_terminal_task_response(
    self,
    *,
    task_info: TaskInfo,
    answer: str,
    errors: list[str] | None = None,
) -> Task:
    response_task = Task(
        task_id=task_info.task.task_id,
        parent_task_id=task_info.task.parent_task_id,
        task_type=MessageMetaType.RESPONSE,
        problem_statement=task_info.task.problem_statement,
        answer=answer,
        errors=errors or None,
        task_plan=task_info.task.task_plan,
    )
    return await _publish_task_response(
        self,
        response_task=response_task,
        incoming_header=task_info.header,
        incoming_meta=task_info.meta,
    )


async def refuse_credential_request_from_delegate(
    self,
    request: Union[SecureCredentialCollectionRequest, SignupAutomationRequest],
    incoming_header: MessageHeader,
    incoming_meta: MessageMeta,
) -> None:
    """Answer a credential-form or signup request another agent sent here.

    Specialists have no such actions, and TeamArchitect's own requests go to the
    person's chat (``message_routing.requester_bound_primary_route``), so a
    request reaching an agent comes from a specialist of another release or a
    legacy output path. Until 2026-10-07 it was dropped as an unhandled type and
    the specialist waited until the watchdog failed its task. The refusal is a
    non-terminal ``SecureCredentialCollectionResult`` with ``continue_task``, so
    the specialist's task resumes and its model escalates instead.
    """

    request_id = (
        request.credential_request_id
        if isinstance(request, SecureCredentialCollectionRequest)
        else request.signup_request_id
    )
    route = incoming_header.from_agent_id or incoming_header.response_routing_key
    self._logger.warning(
        "[%s] Refused a credential request another agent sent here. "
        "event=delegate_credential_request_refused request_type=%s request_id=%s "
        "sender_agent_id=%s parent_task_id=%s service_name=%s",
        self.id,
        type(request).__name__,
        request_id,
        route or "-",
        request.parent_task_id or "-",
        request.service_name,
    )
    if not route or route == self.id:
        self._logger.error(
            "[%s] Cannot answer a refused credential request without its sender. "
            "event=delegate_credential_request_refusal_unroutable request_id=%s",
            self.id,
            request_id,
        )
        return
    refusal = SecureCredentialCollectionResult(
        credential_request_id=request_id,
        status="credential_unavailable",
        credential_key=request.credential_key,
        service_name=request.service_name,
        display_name=getattr(request, "display_name", None),
        parent_task_id=request.parent_task_id,
        message=DELEGATE_CREDENTIAL_REQUEST_REFUSAL,
        error=DELEGATE_CREDENTIAL_REQUEST_REFUSAL,
        continue_task=True,
    )
    await self.team_comms.send_message(
        message=MessageEnvelope(
            msg_content=refusal,
            msg_header=MessageHeader(
                from_agent_id=self.id,
                to_routing_path=route,
                response_routing_key=self.id,
                idempotency_key=f"{request_id}:refused",
            ),
            msg_meta=MessageMeta(
                from_agent_name=self.name,
                msg_correlation_id=incoming_meta.msg_correlation_id,
                conversation_id=incoming_meta.conversation_id,
                requester_type=RequesterType.AGENT_REQUESTER,
                is_response_requested=False,
            ),
        ),
        routing_key=route,
    )


async def _handle_secure_credential_collection_result(
    self,
    credential_result: SecureCredentialCollectionResult,
    incoming_msg_header: MessageHeader,
    incoming_msg_meta: MessageMeta,
):
    """Resume a task after broker-owned credential collection completes."""
    parent_task_id = credential_result.parent_task_id
    self._logger.info(
        "[%s] Received secure credential result %s status=%s parent_task_id=%s credential_key=%s.",
        self.id,
        credential_result.credential_request_id,
        credential_result.status,
        parent_task_id,
        credential_result.credential_key,
    )
    if not parent_task_id:
        self._logger.error(
            "[%s] Secure credential result %s lacks parent_task_id. Closing the "
            "requester continuation without resuming a task.",
            self.id,
            credential_result.credential_request_id,
        )
        answer, errors = _orphaned_credential_collection_response(credential_result)
        await _publish_task_response(
            self,
            response_task=Task(
                task_id=credential_result.credential_request_id,
                task_type=MessageMetaType.RESPONSE,
                answer=answer,
                errors=errors,
            ),
            incoming_header=incoming_msg_header,
            incoming_meta=incoming_msg_meta,
        )
        return

    parent_task_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, parent_task_id
    )
    if not parent_task_info:
        self._logger.error(
            "[%s] Parent task %s for secure credential result %s was not active. "
            "Closing the requester continuation without reviving the task.",
            self.id,
            parent_task_id,
            credential_result.credential_request_id,
        )
        answer, errors = _orphaned_credential_collection_response(credential_result)
        await _publish_task_response(
            self,
            response_task=Task(
                task_id=parent_task_id,
                task_type=MessageMetaType.RESPONSE,
                answer=answer,
                errors=errors,
            ),
            incoming_header=incoming_msg_header,
            incoming_meta=incoming_msg_meta,
        )
        return

    now_ts = datetime.now(timezone.utc).isoformat()
    await self.task_manager.operations_runtime.run(
        self.task_manager.append_history_entry,
        parent_task_id,
        f"Secure credential result for {credential_result.credential_key}: {credential_result.status}.",
        source=TaskHistorySource.SYSTEM,
        entry_type=TaskHistoryType.QUERY_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=now_ts,
        related_task_id=credential_result.credential_request_id,
        details={
            "event": "secure_credential_result",
            "credential_request_id": credential_result.credential_request_id,
            "credential_key": credential_result.credential_key,
            "status": credential_result.status,
            "metadata": credential_result.metadata,
            "broker_session_id": credential_result.broker_session_id,
            "error": credential_result.error,
        },
    )
    if self._conversation_store and getattr(
        parent_task_info.meta, "conversation_id", None
    ):
        await self.task_manager.operations_runtime.run(
            self._conversation_store.append_clarification,
            task_id=parent_task_id,
            message=(
                f"secure credential result for {credential_result.credential_key}: "
                f"{credential_result.status}. {credential_result.message}"
            ).strip(),
        )

    terminal_result_kind: Optional[str] = None
    terminal_succeeded = False
    terminal_answer = ""
    if credential_result.continue_task:
        # The task has more to do after the form (it said so in its request):
        # it resumes through the model below instead of ending here.
        pass
    elif _is_credential_deletion_result(credential_result):
        terminal_result_kind = "credential deletion"
        terminal_answer, terminal_succeeded = _credential_deletion_result_answer(
            credential_result
        )
    elif _is_terminal_credential_collection_result(credential_result):
        terminal_result_kind = "credential collection"
        terminal_answer, terminal_succeeded = _credential_collection_result_answer(
            credential_result
        )

    if terminal_result_kind is not None:
        errors = (
            None
            if terminal_succeeded
            else [
                credential_result.error or credential_result.message or terminal_answer
            ]
        )
        response_task = await _send_terminal_task_response(
            self,
            task_info=parent_task_info,
            answer=terminal_answer,
            errors=errors,
        )
        parent_task_info = await self.task_manager.operations_runtime.run(
            self.task_manager.update_task_result,
            parent_task_info.task.task_id,
            TaskResultUpdate(answer=response_task.answer),
        )
        if parent_task_info is None:
            return
        if response_task.errors:
            parent_task_info = await self.task_manager.operations_runtime.run(
                self.task_manager.update_task_result,
                parent_task_info.task.task_id,
                TaskResultUpdate(errors=list(response_task.errors)),
            )
            if parent_task_info is None:
                return
        if terminal_succeeded:
            await finalize_conversation_turn(
                conversation_store=self._conversation_store,
                conversation_compactor=self._conversation_compactor,
                conversation_artifact_offloader=self._conversation_artifact_offloader,
                strands_messages=task_strands_messages(parent_task_info.task.task_id),
                task_manager=self.task_manager,
                task_info=parent_task_info,
                task_status=ProgressState.COMPLETED.value,
                logger=self._logger,
                agent_id=self.id,
                team_id=self.team_id,
                named_agent_type=self.named_agent_type,
                summary_generator=self._conversation_summary_generator,
            )
            await self._notify_context_after_turn(
                task_info=parent_task_info,
                task_status=ProgressState.COMPLETED.value,
            )
            await self._complete_task_and_schedule_aar_review(parent_task_id, now_ts)
        else:
            failure_summary = (
                credential_result.error or credential_result.message or terminal_answer
            )
            await self.task_manager.operations_runtime.run(
                self.task_manager.fail_task, parent_task_id, failure_summary, now_ts
            )
            await finalize_conversation_turn(
                conversation_store=self._conversation_store,
                conversation_compactor=self._conversation_compactor,
                conversation_artifact_offloader=self._conversation_artifact_offloader,
                strands_messages=task_strands_messages(parent_task_info.task.task_id),
                task_manager=self.task_manager,
                task_info=parent_task_info,
                task_status=ProgressState.FAILED.value,
                logger=self._logger,
                agent_id=self.id,
                team_id=self.team_id,
                named_agent_type=self.named_agent_type,
                summary_generator=self._conversation_summary_generator,
            )
            await self._notify_context_after_turn(
                task_info=parent_task_info,
                task_status=ProgressState.FAILED.value,
            )
        self._logger.info(
            "[%s] Handled %s result %s status=%s as terminal task response for parent_task_id=%s.",
            self.id,
            terminal_result_kind,
            credential_result.credential_request_id,
            credential_result.status,
            parent_task_id,
        )
        return

    await resume_task_with_directive(
        self,
        parent_task_info,
        _credential_result_resume_directive(credential_result),
    )


async def _handle_task_query_message(
    self: Any,
    task_query_msg: TaskQuery,
    incoming_msg_header: MessageHeader,
    incoming_msg_meta: MessageMeta,
    *,
    from_runtime_mailbox: bool = False,
) -> None:
    """Handle a TaskQuery as the handling of its parent task (``runtime_context.task_work``)."""

    with task_work(task_query_msg.parent_task_id):
        return await _handle_task_query_for_parent(
            self,
            task_query_msg,
            incoming_msg_header,
            incoming_msg_meta,
            from_runtime_mailbox=from_runtime_mailbox,
        )


async def _handle_task_query_for_parent(
    self,
    task_query_msg: TaskQuery,
    incoming_msg_header: MessageHeader,
    incoming_msg_meta: MessageMeta,
    *,
    from_runtime_mailbox: bool = False,
):
    """
    Receives and handles a TaskQuery message.

    TaskQuery messages allow agents to communicate (request/response) about a specific task
    to seek clarification or provide updates. The conversation history is tracked within
    the TaskQuery object and associated with the parent task.

    Args:
        task_query_msg (TaskQuery): The incoming TaskQuery message.
        incoming_msg_header (MessageHeader): The header of the incoming message.
        incoming_msg_meta (MessageMeta): The metadata of the incoming message.
    """
    self._logger.info(
        f"[{self.id}] Received TaskQuery {task_query_msg.task_query_id} of type {task_query_msg.query_type} for parent task {task_query_msg.parent_task_id}."
    )

    parent_task_id = task_query_msg.parent_task_id
    if not parent_task_id:
        self._logger.error(
            f"[{self.id}] TaskQuery {task_query_msg.task_query_id} lacks a parent_task_id. Discarding."
        )
        return

    if (
        task_query_msg.query_type == MessageMetaType.RESPONSE
        and not from_runtime_mailbox
    ):
        root_task_id = task_query_response_root_task_id(
            self.task_manager.resolve_root_task_id, task_query_msg
        )
        if await self.task_manager.operations_runtime.run(
            self._runtime_coordinator().defer_envelope_if_blocked,
            root_task_id=root_task_id,
            event_type="task_query_response",
            event_class="deferrable",
            content=task_query_msg,
            header=incoming_msg_header,
            meta=incoming_msg_meta,
            related_task_id=task_query_msg.task_query_id,
        ):
            return

    parent_task_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, parent_task_id
    )
    if not parent_task_info:
        from custom_agents.orphaned_subtasks import stop_report_for_ended_parent

        # A worker still asking about work whose request ended is told to stop.
        if not await stop_report_for_ended_parent(
            self,
            parent_task_id=parent_task_id,
            child_task_id=parent_task_id,
            msg_header=incoming_msg_header,
        ):
            self._logger.error(
                f"[{self.id}] Parent task {parent_task_id} for TaskQuery not found. Discarding."
            )
        return

    # Update task with new query information and preserve provenance.
    await self.task_manager.operations_runtime.run(
        self.task_manager.add_task_query,
        parent_task_id,
        task_query_msg,
        agent_name=getattr(incoming_msg_meta, "from_agent_name", None),
        agent_id=getattr(incoming_msg_header, "from_agent_id", None),
        timestamp=getattr(incoming_msg_header, "timestamp", None),
    )
    if self._conversation_store and getattr(
        parent_task_info.meta, "conversation_id", None
    ):
        actor = (
            getattr(incoming_msg_meta, "from_agent_name", None)
            or getattr(incoming_msg_header, "from_agent_id", None)
            or "unknown"
        )
        await self.task_manager.operations_runtime.run(
            self._conversation_store.append_clarification,
            task_id=parent_task_id,
            message=f"{task_query_msg.query_type.value} from {actor}: {task_query_msg.message}",
        )

    if task_query_msg.query_type == MessageMetaType.REQUEST:
        self._logger.info(
            f"[{self.id}] Processing TaskQuery REQUEST {task_query_msg.task_query_id} for LLM response."
        )
        try:
            return_query: TaskQuery = await self.process_llm_interaction(
                msg_content=task_query_msg,
                msg_header=incoming_msg_header,
                msg_meta=incoming_msg_meta,
            )
        except asyncio.CancelledError:
            # Runtime shutdown cancels this owner; a user stop cancels its child turn.
            if (owner := asyncio.current_task()) is not None and owner.cancelling():
                raise
            await handle_interrupted_turn(
                self, task_id=parent_task_id, task_info=parent_task_info
            )
            return
        self._logger.info(
            f"[{self.id}] Generated TaskQuery RESPONSE: [{return_query.task_query_id}]"
        )

    elif task_query_msg.query_type == MessageMetaType.RESPONSE:
        self._logger.info(
            f"[{self.id}] Received TaskQuery RESPONSE {task_query_msg.task_query_id}. Resuming parent task {parent_task_id}."
        )

        now_ts = datetime.now(timezone.utc).isoformat()
        pending_pause = await self.task_manager.operations_runtime.run(
            self.task_manager.get_pending_recoverable_pause, parent_task_id
        )
        original_query = self._find_original_task_query(
            parent_task_id, task_query_msg.task_query_id
        )
        is_recoverable_pause_response = pending_pause is not None or (
            original_query is not None
            and original_query.query_reason == TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE
        )
        if is_recoverable_pause_response:
            friendly_reason = (
                pending_pause.friendly_reason
                if pending_pause is not None
                else (
                    original_query.message
                    if original_query is not None
                    else "a recoverable runtime budget was reached"
                )
            )
            trigger_value = (
                pending_pause.trigger
                if pending_pause is not None
                else (
                    original_query.recoverable_pause_trigger.value
                    if original_query is not None
                    and original_query.recoverable_pause_trigger is not None
                    else None
                )
            )
            try:
                recoverable_trigger = (
                    RecoverablePauseTrigger(trigger_value) if trigger_value else None
                )
            except ValueError:
                recoverable_trigger = None
            allowed_decisions = (
                list(original_query.allowed_decisions)
                if original_query is not None and original_query.allowed_decisions
                else self._runtime_pause_allowed_decisions(recoverable_trigger)
            )
            decision = self._parse_runtime_pause_decision(
                task_query_msg.message,
                allowed_decisions=allowed_decisions,
            )
            if decision is None:
                paused_task = await self.task_manager.operations_runtime.run(self.task_manager.get_task, parent_task_id)
                clarification = TaskQuery(
                    query_type=MessageMetaType.REQUEST,
                    parent_task_id=parent_task_id,
                    message=runtime_pause_identity(
                        task_id=parent_task_id,
                        description=paused_task.problem_statement if paused_task else "",
                        subtask_id=pending_pause.affected_subtask_id if pending_pause else None,
                        worker=pending_pause.affected_named_agent_type if pending_pause else None,
                    ) + self._runtime_pause_prompt(
                        friendly_reason, trigger=recoverable_trigger
                    ),
                    query_reason=TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE,
                    allowed_decisions=allowed_decisions,
                    recoverable_pause_trigger=recoverable_trigger,
                )
                await self.task_manager.operations_runtime.run(
                    self.task_manager.add_task_query,
                    parent_task_id,
                    clarification,
                    agent_name=self.name,
                    agent_id=self.id,
                )
                outgoing_meta = await build_outgoing_meta(
                    llm_meta=MessageMeta(
                        conversation_id=incoming_msg_meta.conversation_id
                    ),
                    incoming_meta=incoming_msg_meta,
                    content=clarification,
                    from_agent_name=self.name,
                    repair_recorder=getattr(
                        self, "_record_runtime_repair_anomaly", None
                    ),
                )
                header = await construct_header(
                    comms_message=CommsMessage(
                        msg_content=clarification, msg_meta=outgoing_meta
                    ),
                    explicit_to_path=None,
                    route_response_to_self=False,
                    agent_id=self.id,
                    agent_name=self.name,
                    team_id=self.team_id,
                    named_agent_type_queue_map=self.named_agent_type_queue_map,
                    agent_id_by_name=self._agent_id_by_name,
                    logger=self._logger,
                    repair_recorder=getattr(
                        self, "_record_runtime_repair_anomaly", None
                    ),
                )
                routing_key = await resolve_outgoing_routing(
                    content=clarification,
                    header=header,
                    incoming_header=parent_task_info.header,
                    incoming_meta=parent_task_info.meta,
                    agent_id_by_name=self._agent_id_by_name,
                    named_agent_type_queue_map=self.named_agent_type_queue_map,
                    team_id=self.team_id,
                    logger=self._logger,
                    agent_id=self.id,
                    repair_recorder=getattr(
                        self, "_record_runtime_repair_anomaly", None
                    ),
                )
                await self.team_comms.send_message(
                    message=MessageEnvelope(
                        msg_content=clarification,
                        msg_meta=outgoing_meta,
                        msg_header=header,
                    ),
                    routing_key=routing_key,
                )
                return
            if pending_pause is not None:
                await self.task_manager.operations_runtime.run(
                    self.task_manager.resolve_recoverable_pause,
                    parent_task_id,
                    decision=decision,
                    response_text=task_query_msg.message,
                    timestamp=now_ts,
                )
            else:
                await self.task_manager.operations_runtime.run(
                    self.task_manager.append_history_entry,
                    parent_task_id,
                    f"Requester selected runtime pause option: {decision}.",
                    source=TaskHistorySource.SYSTEM,
                    entry_type=TaskHistoryType.QUERY_EVENT,
                    importance=TaskHistoryImportance.HIGH,
                    timestamp=now_ts,
                    related_task_id=task_query_msg.task_query_id,
                    details={
                        "reason": "recoverable_runtime_pause_decision",
                        "task_query_id": task_query_msg.task_query_id,
                        "trigger": trigger_value,
                        "decision": decision,
                        "recovered_without_pause_record": True,
                    },
                )
            if decision == TaskQueryDecision.CANCEL.value:
                cancel_reason = (
                    "Requester gave up after the model endpoint reported an overload."
                    if recoverable_trigger
                    == RecoverablePauseTrigger.PROVIDER_OVERLOADED
                    else "Requester stopped the task after a runtime budget pause."
                )
                await self._cancel_task_tree_locally(
                    task_info=parent_task_info,
                    reason=cancel_reason,
                    signal_pending_subtasks=True,
                )
                return
            delay_minutes = self._provider_retry_delay_minutes(decision)
            if delay_minutes is not None:
                await self.task_manager.operations_runtime.run(
                    self.task_manager.append_runtime_resume_directive,
                    parent_task_id,
                    self._runtime_resume_directive(
                        TaskQueryDecision.RETRY.value, trigger_value
                    ),
                )
                await self._schedule_provider_overload_retry(
                    task_info=parent_task_info,
                    delay_minutes=delay_minutes,
                    decision=decision,
                    pause_id=pending_pause.pause_id
                    if pending_pause is not None
                    else None,
                    task_query_id=task_query_msg.task_query_id,
                    timestamp=now_ts,
                )
                return
            if decision == TaskQueryDecision.PARTIAL.value:
                await self.task_manager.operations_runtime.run(
                    self.task_manager.record_partial_tool_call_limit,
                    parent_task_id,
                    (
                        pending_pause.partial_tool_call_limit
                        if pending_pause is not None
                        else self._partial_tool_call_limit(
                            self._resolved_max_tool_calls_per_task()
                        )
                    ),
                )
                await self.task_manager.operations_runtime.run(
                    self.task_manager.append_runtime_resume_directive,
                    parent_task_id,
                    self._runtime_resume_directive(decision, trigger_value),
                )
            if decision in {
                TaskQueryDecision.RESUME.value,
                TaskQueryDecision.RETRY.value,
            }:
                await self.task_manager.operations_runtime.run(
                    self.task_manager.append_runtime_resume_directive,
                    parent_task_id,
                    self._runtime_resume_directive(decision, trigger_value),
                )
            if (
                decision
                in {
                    TaskQueryDecision.RESUME.value,
                    TaskQueryDecision.PARTIAL.value,
                }
                and pending_pause is not None
            ):
                await _fail_delegated_subtask_after_pause_resolution(
                    self,
                    parent_task_id=parent_task_id,
                    pending_pause=pending_pause,
                    decision=decision,
                    timestamp=now_ts,
                )

        if parent_task_info.state in {ProgressState.WAITING, ProgressState.PAUSED}:
            await self.task_manager.operations_runtime.run(
                self.task_manager.update_main_task_state,
                parent_task_id,
                ProgressState.IN_PROGRESS,
                now_ts,
            )

        try:
            result = await self.process_llm_interaction(
                msg_content=parent_task_info.task,
                msg_header=parent_task_info.header,
                msg_meta=parent_task_info.meta,
            )
        except asyncio.CancelledError:
            # Runtime shutdown cancels this owner; a user stop cancels its child turn.
            if (owner := asyncio.current_task()) is not None and owner.cancelling():
                raise
            await handle_interrupted_turn(
                self, task_id=parent_task_id, task_info=parent_task_info
            )
            return
        except Exception as exc:
            # The parent holds the execution slot (IN_PROGRESS above); release it.
            await settle_failed_turn(self, parent_task_id, parent_task_info, exc)
            return

        if (
            isinstance(result, Task)
            and result.answer
            and not await self.task_manager.operations_runtime.run(
                self.task_manager.is_final_review_pending, parent_task_id
            )
        ):
            if await self.task_manager.operations_runtime.run(
                self.task_manager.get_task_info, parent_task_id
            ) is not None and not await self.task_manager.operations_runtime.run(
                self.task_manager.has_pending_subtasks, parent_task_id
            ):
                now_ts = datetime.now(timezone.utc).isoformat()
                await self._complete_task_and_schedule_aar_review(
                    parent_task_id, now_ts
                )
                self._logger.info(
                    f"[{self.id}] Task: {parent_task_id} is now marked as COMPLETED"
                )
    else:
        self._logger.warning(
            f"[{self.id}] Received TaskQuery with unknown query_type: {task_query_msg.query_type}"
        )


async def _custody_for_new_task(
    self: Any, task_request: Task, msg_header: MessageHeader, msg_meta: MessageMeta,
    source_queue_name: Optional[str],
) -> Optional[CustodyDecision]:
    """Whether this agent may take a task another agent may hold or finished; None drops it.

    Held by a live agent: a copy delivered again while it runs
    (``custom_agents.task_custody``). Finished by any agent of the team: a copy
    delivered again after the first one completed (team-wide since
    2026-10-17). Custody is read first: a holder archives the task before its
    save releases custody, so a task released between the two reads is found
    in the archive. Read the other way round, a task finished in between
    looked free.
    """
    custody = await check_task_custody(self, task_request.task_id, msg_meta)
    if not custody.admit:
        return None
    if await _finished_by_team(self, task_request.task_id, msg_header, source_queue_name):
        return None
    return custody


async def _finished_by_team(
    self: Any, task_id: str, msg_header: MessageHeader, source_queue_name: Optional[str]
) -> bool:
    """Whether this agent or any agent of its team already finished the task (logged)."""
    archived = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_from_db,
        task_id,
        include_all_agents=True,
    )
    if archived is None or not (
        archived.get("owner_agent_id") == self.id
        or (self.team_id and archived.get("team_id") == self.team_id)
    ):
        return False
    self._logger.warning(
        "[%s] event=duplicate_archived_delivery_ignored task_id=%s message_id=%s "
        "idempotency_key=%s queue=%s finished_by=%s",
        self.id,
        task_id,
        getattr(msg_header, "message_id", None),
        getattr(msg_header, "idempotency_key", None),
        source_queue_name,
        archived.get("owner_agent_id"),
    )
    return True


async def _admit_with_custody(
    self: Any,
    custody: CustodyDecision,
    admit: Callable[[Optional[str]], Awaitable[bool]],
    task_id: str,
    msg_header: MessageHeader,
    source_queue_name: Optional[str],
) -> Optional[bool]:
    """Run the admission save with the custody decision; None when the copy was dropped (logged)."""
    try:
        return await admit(custody.takeover_from)
    except TaskCustodyFinished as finished:
        await _drop_finished_copy(self, finished, msg_header, source_queue_name)
        return None
    except TaskCustodyReleased as released:
        return await _admit_after_holder_released(
            self, custody, released, admit, msg_header, source_queue_name
        )
    except TaskCustodyConflict as conflict:
        # Another agent claimed it between the check and the save.
        log_dropped_copy(self, task_id, CUSTODY_TAKEN_MEANWHILE, conflict.holder_agent_id)
        return None


async def _admit_after_holder_released(
    self: Any,
    custody: CustodyDecision,
    released: TaskCustodyReleased,
    admit: Callable[[Optional[str]], Awaitable[bool]],
    msg_header: MessageHeader,
    source_queue_name: Optional[str],
) -> Optional[bool]:
    """Admit or drop a copy whose holder released the task before the takeover's save.

    The holder named by the takeover was alive after all (2026-10-17). A
    task it finished is in the team's archive: the copy is dropped. A task it
    handed back after losing its lease is free, and this copy is the one the
    lead re-sent for it: it is admitted without a takeover. A holder judged
    gone that released the task otherwise (it handed it back, or dropped it
    because another agent holds it) left it to someone else: the copy is
    dropped, never claimed afresh. Returns the admission result, or None when
    the copy was dropped.
    """
    task_id = released.task_id
    if await _finished_by_team(self, task_id, msg_header, source_queue_name):
        return None
    if custody.reason != CUSTODY_HANDED_BACK:
        log_dropped_copy(self, task_id, CUSTODY_RELEASED_MEANWHILE, released.holder_agent_id)
        return None
    try:
        return await admit(None)
    except TaskCustodyFinished as finished:
        await _drop_finished_copy(self, finished, msg_header, source_queue_name)
        return None
    except TaskCustodyConflict as conflict:
        log_dropped_copy(self, task_id, CUSTODY_TAKEN_MEANWHILE, conflict.holder_agent_id)
        return None


async def _drop_finished_copy(
    self: Any, finished: TaskCustodyFinished, msg_header: MessageHeader, source_queue_name: Optional[str]
) -> None:
    """Drop a copy whose claim found the task finished, in the claim's own transaction (logged).

    The team's archive read before the claim did not show it: the agent that
    finished it archived and released it in between (2026-10-18).
    """
    if not await _finished_by_team(self, finished.task_id, msg_header, source_queue_name):
        log_dropped_copy(self, finished.task_id, CUSTODY_FINISHED_MEANWHILE, finished.finished_by)


async def _handle_task_request(
    self,
    task_request: Task,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
    *,
    message_handled_at: Optional[str] = None,
    source_queue_name: Optional[str] = None,
) -> None:
    """
    Handles a new incoming task request using the TaskManager.

    Args:
        task_request (Task): The incoming Task object representing the request.
        msg_header (MessageHeader): The header of the incoming message.
        msg_meta (MessageMeta): The metadata of the incoming message.
    """
    self._logger.info(
        f"Received task request {task_request.task_id} from {msg_header.from_agent_id}"
    )

    if not isinstance(task_request, Task):
        self._logger.error(
            f"Invalid task request content type: {type(task_request)}. Discarding."
        )
        return

    if (
        await self.task_manager.operations_runtime.run(
            self.task_manager.get_task_info, task_request.task_id
        )
        is not None
    ):
        self._logger.warning(
            "[%s] event=duplicate_delivery_ignored task_id=%s message_id=%s queue=%s",
            self.id,
            task_request.task_id,
            getattr(msg_header, "message_id", None),
            source_queue_name,
        )
        return
    custody = await _custody_for_new_task(self, task_request, msg_header, msg_meta, source_queue_name)
    if custody is None:
        return

    if await self.task_manager.operations_runtime.run(self.task_manager.is_buffer_full):
        await report_failed_admission(self, task_request, msg_header, msg_meta)
        return

    # A maintenance run's escalation works for the maintenance schedule's owner,
    # so its findings reach a person (task_context_runtime).
    msg_meta = sign_system_escalation(self, msg_meta, task_id=task_request.task_id)
    normalized_meta = self._ensure_request_timezone(
        msg_meta,
        top_level_request=task_request.parent_task_id is None,
    )
    # Decided once, at admission, from verified identity only: the task record
    # and its conversation belong to this person (None: work for nobody). It
    # may read storage (schedule run identities), so it runs in a thread.
    owner_person_id = await asyncio.to_thread(verified_meta_person_id, self, normalized_meta)
    if task_request.parent_task_id is None:
        try:
            normalized_meta = ensure_task_conversation_context(
                conversation_store=self._conversation_store,
                team_id=self.team_id,
                owner_agent_id=self.id,
                owner_agent_name=self.name,
                task_request=task_request,
                msg_header=msg_header,
                msg_meta=normalized_meta,
                owner_person_id=owner_person_id,
                owner_named_agent_type=self.named_agent_type,
            )
        except ValueError as exc:
            self._logger.warning(
                f"[{self.id}] Rejecting task {task_request.task_id}: {exc}"
            )
            await self._send_task_error_response(
                task_request, msg_header, msg_meta, str(exc)
            )
            return

    async def admit(takeover_from: Optional[str]) -> bool:
        return await self.task_manager.operations_runtime.run(
            self.task_manager.add_task,
            task_request,
            msg_header,
            normalized_meta,
            owner_person_id=owner_person_id,
            handled_at=message_handled_at,
            handled_queue_name=source_queue_name,
            custody_takeover_from=takeover_from,
        )

    admitted = await _admit_with_custody(
        self, custody, admit, task_request.task_id, msg_header, source_queue_name
    )
    if admitted is None:
        return
    if not admitted:
        await report_failed_admission(
            self, task_request, msg_header, normalized_meta, conversation_admitted=True,
        )
        return
    if not await apply_redispatch_notice(self, task_request.task_id, normalized_meta):
        return
    # Delegated work: tell the requester this worker holds the task, so it
    # checks this worker and counts a move if the task changes workers.
    await send_task_claim(
        self,
        task_id=task_request.task_id,
        parent_task_id=task_request.parent_task_id,
        header=msg_header,
        meta=normalized_meta,
    )
    if task_request.parent_task_id is None and getattr(
        normalized_meta, "conversation_id", None
    ):
        record_task_conversation_history(
            task_manager=self.task_manager,
            task_id=task_request.task_id,
            conversation_id=normalized_meta.conversation_id,
            followup_to_task_id=getattr(normalized_meta, "followup_to_task_id", None),
        )

    await check_queue_pressure(self)
    schedule_pending_work(self)


async def _handle_delivery_receipt(
    self,
    *,
    receipt: DeliveryReceipt,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    await handle_delivery_receipt(
        self,
        receipt=receipt,
        msg_header=msg_header,
        msg_meta=msg_meta,
    )


@staticmethod
async def _check_and_process_next_task(self, lane: str = MAIN_LANE) -> bool:
    """Start the next queued task of ``lane`` and run its turn; the caller holds that lane.

    The main lane takes ordinary work while its slot is free; a parallel
    request's lane takes only that request's tasks (custom_agents.execution_lanes).
    """
    if lane == MAIN_LANE and not self.paused:
        # The caller holds the main lane, so no main turn runs now: a task still
        # holding the main slot lost its turn and must not block the queue
        # (turn_failure_runtime).
        await release_orphaned_execution_slot(self)
    if (
        self.paused
        or (
            lane == MAIN_LANE
            and not await self.task_manager.operations_runtime.run(
                self.task_manager.can_start_new_task,
            )
        )
        # A winding-down worker only resumes tasks that already started here.
        or not await may_start_next_task(self)
    ):
        return False

    next_task_id = await self.task_manager.operations_runtime.run(
        self.task_manager.get_next_task_to_process, lane
    )
    if not next_task_id:
        return False

    task_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, next_task_id
    )
    if not task_info:
        return False

    # The task's handling: its turn and the saving of its result (runtime_context.task_work).
    with task_work(next_task_id):
        self._logger.info(f"Starting processing for task {next_task_id}")
        now_ts = datetime.now(timezone.utc).isoformat()
        if not await self.task_manager.operations_runtime.run(
            self.task_manager.update_main_task_state,
            next_task_id,
            ProgressState.IN_PROGRESS,
            now_ts,
        ):
            return False

        try:
            await self._announce_execution_state()
            task_result: Union[
                Task,
                TaskQuery,
                TaskSteering,
                SecureCredentialCollectionRequest,
                SignupAutomationRequest,
            ] = await self.process_llm_interaction(
                msg_content=task_info.task,
                msg_header=task_info.header,
                msg_meta=task_info.meta,
            )
            await _apply_task_processing_result(self, next_task_id, task_info, task_result)
        except asyncio.CancelledError:
            # Runtime shutdown cancels this owner; a user stop cancels its child turn.
            if (owner := asyncio.current_task()) is not None and owner.cancelling():
                raise
            await handle_interrupted_turn(
                self, task_id=next_task_id, task_info=task_info
            )
        except TurnActionRecoveryScheduled as e:
            self._logger.warning(
                "[%s] Paused task %s for runtime action outbox recovery: %s",
                self.id,
                next_task_id,
                e,
            )
        except ProviderFailoverRequeued as e:
            self._logger.warning(
                "[%s] Requeued task %s after model-provider failover: %s",
                self.id,
                next_task_id,
                e,
            )
        except Exception as e:
            await fail_task_after_turn_error(self, next_task_id, task_info, e)
        finally:
            await self._refresh_self_agent_state()

    return True


async def _apply_task_processing_result(
    self,
    task_id: str,
    task_info: TaskInfo,
    task_result: Union[
        Task,
        TaskQuery,
        TaskSteering,
        SecureCredentialCollectionRequest,
        SignupAutomationRequest,
    ],
) -> None:
    """Apply a completed or waiting task-turn result to local runtime state."""

    now_ts = datetime.now(timezone.utc).isoformat()
    if isinstance(task_result, Task):
        if task_result.task_type == MessageMetaType.RESPONSE:
            late_guidance = await consume_pending_proactive_guidance_message(
                self, task_id
            )
            if late_guidance:
                directive = (
                    "Runtime mid-turn requester steering arrived before the terminal task response "
                    "was committed. Discard the previous draft answer and continue using this "
                    "requester input as authoritative steering:\n"
                    f"{late_guidance}"
                )
                await self.task_manager.operations_runtime.run(
                    self.task_manager.append_runtime_resume_directive,
                    task_id,
                    directive,
                )
                await self.task_manager.operations_runtime.run(
                    self.task_manager.append_history_entry,
                    task_id,
                    "Terminal response deferred because mid-turn requester steering arrived before commit.",
                    entry_type=TaskHistoryType.TASK_EVENT,
                    importance=TaskHistoryImportance.HIGH,
                    timestamp=now_ts,
                    details={"event": "task_steering_terminal_commit_deferred"},
                )
                await self.task_manager.operations_runtime.run(
                    self.task_manager.update_main_task_state,
                    task_id,
                    ProgressState.ACCEPTED,
                    now_ts,
                )
                await self.task_manager.operations_runtime.run(
                    self.task_manager.enqueue_task, task_id, front=True
                )
                return
            if await self.task_manager.operations_runtime.run(
                self.task_manager.get_task_info, task_id
            ) is not None and not await self.task_manager.operations_runtime.run(
                self.task_manager.is_final_review_pending, task_id
            ):
                task_info = await self.task_manager.operations_runtime.run(
                    self.task_manager.update_task_result,
                    task_info.task.task_id,
                    TaskResultUpdate(
                        answer=task_result.answer, errors=task_result.errors
                    ),
                )
                if task_info is None:
                    return
                if task_result.task_plan:
                    await self.task_manager.operations_runtime.run(
                        self.task_manager.set_task_plan,
                        task_id,
                        task_result.task_plan,
                        agent_name=self.name,
                        agent_id=self.id,
                        rationale="Updated from completed task result.",
                    )
                await finalize_conversation_turn(
                    conversation_store=self._conversation_store,
                    conversation_compactor=self._conversation_compactor,
                    conversation_artifact_offloader=self._conversation_artifact_offloader,
                    strands_messages=task_strands_messages(task_info.task.task_id),
                    task_manager=self.task_manager,
                    task_info=task_info,
                    task_status=ProgressState.COMPLETED.value,
                    logger=self._logger,
                    agent_id=self.id,
                    team_id=self.team_id,
                    named_agent_type=self.named_agent_type,
                    summary_generator=self._conversation_summary_generator,
                )
                await self._notify_context_after_turn(
                    task_info=task_info,
                    task_status=ProgressState.COMPLETED.value,
                )
                await self._complete_task_and_schedule_aar_review(task_id, now_ts)
        elif task_result.task_type == MessageMetaType.REQUEST:
            self._logger.info(
                "Task %s generated a sub-Task REQUEST: %s", task_id, task_result.task_id
            )
            await self.task_manager.operations_runtime.run(
                self.task_manager.update_main_task_state,
                task_id,
                ProgressState.WAITING,
                now_ts,
            )
        return

    if isinstance(task_result, TaskQuery):
        self._logger.info(
            "Task %s generated a TaskQuery: %s", task_id, task_result.task_query_id
        )
        if task_result.query_reason == TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE:
            await self.task_manager.operations_runtime.run(
                self.task_manager.update_main_task_state,
                task_id,
                ProgressState.PAUSED,
                now_ts,
            )
        else:
            await self.task_manager.operations_runtime.run(
                self.task_manager.update_main_task_state,
                task_id,
                ProgressState.WAITING,
                now_ts,
            )
        return

    if (
        isinstance(task_result, SecureCredentialCollectionRequest)
        and task_result.request_type == MessageMetaType.REQUEST
    ):
        self._logger.info(
            "[%s] Task %s generated SecureCredentialCollectionRequest %s; waiting for broker result.",
            self.id,
            task_id,
            task_result.credential_request_id,
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.update_main_task_state,
            task_id,
            ProgressState.WAITING,
            now_ts,
        )
        return

    if (
        isinstance(task_result, SignupAutomationRequest)
        and task_result.request_type == MessageMetaType.REQUEST
    ):
        self._logger.info(
            "[%s] Task %s generated SignupAutomationRequest %s; waiting for signup or manual fallback result.",
            self.id,
            task_id,
            task_result.signup_request_id,
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.update_main_task_state,
            task_id,
            ProgressState.WAITING,
            now_ts,
        )
        return

    if isinstance(task_result, TaskSteering):
        self._logger.info(
            "Task %s generated TaskSteering: %s", task_id, task_result.steering_id
        )
        # send_interrupt_request_for_result persisted WAITING before publication.
        # A fast delegated decision may already have resumed this task.
        return


async def _handle_task_response(
    self,
    sub_task_response: Task,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
    *,
    from_runtime_mailbox: bool = False,
) -> None:
    """
    Handle a Task result returned by another agent for a subtask this agent initiated.

    Args:
        sub_task_response (Task): The Task object representing the completed subtask response.
        msg_header (MessageHeader): The MessageHeader of the incoming response message.
        msg_meta (MessageMeta): The MessageMeta of the incoming response message.
    """
    subtask_id = sub_task_response.task_id
    parent_task_id = sub_task_response.parent_task_id
    now_ts = datetime.now(timezone.utc).isoformat()

    if not parent_task_id:
        self._logger.error(
            f"[{self.id}] Received subtask response {subtask_id} without a parent_task_id. Discarding."
        )
        return

    if not from_runtime_mailbox:
        root_task_id = task_response_root_task_id(
            self.task_manager.resolve_root_task_id, sub_task_response
        )
        if await self.task_manager.operations_runtime.run(
            self._runtime_coordinator().defer_envelope_if_blocked,
            root_task_id=root_task_id,
            event_type="task_response",
            event_class="deferrable",
            content=sub_task_response,
            header=msg_header,
            meta=msg_meta,
            related_task_id=subtask_id,
        ):
            return

    if not await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, parent_task_id
    ):
        from custom_agents.orphaned_subtasks import stop_report_for_ended_parent

        # A worker still reporting progress on work whose request ended is told
        # to stop; a final result needs no reply.
        latest = sub_task_response.task_state[-1][0] if sub_task_response.task_state else None
        if latest is not None and await self.task_manager.operations_runtime.run(
            self.task_manager.is_terminal_state, latest
        ):
            stopped = False
        else:
            stopped = await stop_report_for_ended_parent(
                self,
                parent_task_id=parent_task_id,
                child_task_id=subtask_id,
                msg_header=msg_header,
            )
        if not stopped:
            self._logger.error(
                f"[{self.id}] Parent task {parent_task_id} for subtask {subtask_id} not found. Ignoring."
            )
        return

    subtask_info: TaskInfo = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, subtask_id
    )
    if not subtask_info:
        self._logger.error(
            f"[{self.id}] Received response for unknown subtask {subtask_id}. Discarding."
        )
        return
    from custom_agents.delegated_work_runtime import task_response_matches_holder

    if not await self.task_manager.operations_runtime.run(
        partial(task_response_matches_holder, self), subtask_info, sub_task_response, msg_header.from_agent_id
    ):
        return
    if subtask_info.state in {
        ProgressState.COMPLETED,
        ProgressState.FAILED,
        ProgressState.CANCELLED,
        ProgressState.ABANDONED,
    }:
        self._logger.warning(
            "[%s] event=duplicate_subtask_response_ignored subtask_id=%s parent_task_id=%s "
            "state=%s message_id=%s idempotency_key=%s",
            self.id,
            subtask_id,
            parent_task_id,
            subtask_info.state.value,
            getattr(msg_header, "message_id", None),
            getattr(msg_header, "idempotency_key", None),
        )
        return

    if sub_task_response.task_state:
        next_state = sub_task_response.task_state[-1][0]
        next_state_timestamp = sub_task_response.task_state[-1][1]
    else:
        next_state = (
            ProgressState.COMPLETED
            if sub_task_response.task_type == MessageMetaType.RESPONSE
            else ProgressState.WAITING
        )
        next_state_timestamp = now_ts
        self._logger.info(
            f"[{self.id}] Subtask {subtask_id} omitted task_state; "
            f"inferring {next_state.value} at {next_state_timestamp}."
        )

    # Update subtask payload first; terminal state changes are compare-and-set
    # so a late timeout cannot overwrite a completed worker response.
    subtask_info = await self.task_manager.operations_runtime.run(
        self.task_manager.update_task_result,
        subtask_info.task.task_id,
        TaskResultUpdate(
            answer=sub_task_response.answer,
            errors=sub_task_response.errors,
            failure=sub_task_response.failure,
        ),
    )
    if subtask_info is None:
        return
    if sub_task_response.execution_history:
        subtask_info = await self.task_manager.operations_runtime.run(
            self.task_manager.update_task_result,
            subtask_info.task.task_id,
            TaskResultUpdate(
                execution_history=self._task_response_history_transport_copy(
                    sub_task_response
                )
            ),
        )
        if subtask_info is None:
            return
    if sub_task_response.task_plan:
        await self.task_manager.operations_runtime.run(
            self.task_manager.set_task_plan,
            subtask_id,
            sub_task_response.task_plan,
            agent_name=msg_meta.from_agent_name,
            agent_id=msg_header.from_agent_id,
            rationale="Updated from delegated subtask response.",
        )
    if await self.task_manager.operations_runtime.run(
        self.task_manager.is_terminal_state, next_state
    ):
        transitioned = await self.task_manager.operations_runtime.run(
            self.task_manager.try_transition_task_state,
            subtask_id,
            allowed_from={
                ProgressState.ACCEPTED,
                ProgressState.IN_PROGRESS,
                ProgressState.WAITING,
                ProgressState.PAUSED,
                ProgressState.PARTIAL,
                ProgressState.RETRYING,
                ProgressState.UNDEFINED,
            },
            to_state=next_state,
            timestamp=next_state_timestamp,
            reason="Delegated subtask response arrived.",
            event_id=self._runtime_coordinator().replay_event_id,
            source="coordinator",
        )
        if not transitioned:
            return
    else:
        await self.task_manager.operations_runtime.run(
            self.task_manager.update_main_task_state,
            subtask_id,
            next_state,
            next_state_timestamp,
        )
    await self.task_manager.operations_runtime.run(
        self.task_manager.bubble_subtask_result,
        parent_task_id,
        subtask_info.task.task_id,
    )
    if next_state == ProgressState.PAUSED:
        paused_trigger = (
            self._recoverable_trigger_from_task_history(sub_task_response)
            or RecoverablePauseTrigger.SPECIALIST_RUNTIME_BUDGET_PAUSE
        )
        should_ask_requester = (
            paused_trigger == RecoverablePauseTrigger.PROVIDER_OVERLOADED
            and self._runtime_budget_trigger_enabled(paused_trigger)
        ) or (
            paused_trigger == RecoverablePauseTrigger.SPECIALIST_RUNTIME_BUDGET_PAUSE
            and self._runtime_budget_policy_mode()
            == RuntimeBudgetPolicyMode.USER_GUIDED.value
            and self._runtime_budget_trigger_enabled(paused_trigger)
        )
        parent_task_info = await self.task_manager.operations_runtime.run(
            self.task_manager.get_task_info, parent_task_id
        )
        if (
            parent_task_info is not None
            and should_ask_requester
            and await self.task_manager.operations_runtime.run(
                self.task_manager.count_recoverable_pauses,
                parent_task_info.task.task_id,
            )
            < self._max_user_guided_pauses_per_task()
        ):
            target_label = (
                msg_meta.from_agent_name
                or msg_header.from_agent_id
                or getattr(msg_meta, "from_named_agent_type", None)
                or "a delegated worker"
            )
            detail = f"{target_label} reached a runtime budget while working on a delegated part of the task"
            error_detail = " ".join(
                str(error)
                for error in (sub_task_response.errors or [])
                if str(error).strip()
            )
            friendly_reason = self._friendly_budget_reason(
                trigger=paused_trigger,
                msg_content=parent_task_info.task,
                detail=(error_detail or detail),
            )
            await self._send_runtime_pause_query(
                task_info=parent_task_info,
                trigger=paused_trigger,
                friendly_reason=friendly_reason,
                base_tool_call_limit=self._resolved_max_tool_calls_per_task(),
                affected_subtask_id=subtask_id,
                affected_agent_id=msg_header.from_agent_id,
                affected_named_agent_type=getattr(
                    msg_meta, "from_named_agent_type", None
                ),
            )
            return
        if parent_task_info is not None:
            failure_summary = (
                "Delegated work paused after a recoverable runtime error, but requester guidance is unavailable; "
                "marking the subtask failed so the parent can continue with the recorded error."
            )
            subtask_info = await self.task_manager.operations_runtime.run(
                self.task_manager.update_task_result,
                subtask_info.task.task_id,
                TaskResultUpdate(
                    errors=list(
                        dict.fromkeys(
                            [*(subtask_info.task.errors or []), failure_summary]
                        )
                    )
                ),
            )
            if subtask_info is None:
                return
            await self.task_manager.operations_runtime.run(
                self.task_manager.try_transition_task_state,
                subtask_info.task.task_id,
                allowed_from={ProgressState.PAUSED},
                to_state=ProgressState.FAILED,
                timestamp=now_ts,
                source="runtime_pause_resolution",
                reason=failure_summary,
            )
            await self.task_manager.operations_runtime.run(
                self.task_manager.bubble_subtask_result,
                parent_task_id,
                subtask_info.task.task_id,
            )
            await self.task_manager.operations_runtime.run(
                self.task_manager.append_history_entry,
                parent_task_id,
                failure_summary,
                entry_type=TaskHistoryType.SUBTASK_EVENT,
                importance=TaskHistoryImportance.CRITICAL,
                timestamp=now_ts,
                agent_name=self.name,
                agent_id=self.id,
                related_task_id=subtask_id,
                details={
                    "action": "paused_subtask_failed_without_requester_guidance",
                    "subtask_id": subtask_id,
                    "trigger": paused_trigger.value,
                },
            )
            await self._resume_parent_task_if_ready(parent_task_id)
        return
    if getattr(sub_task_response, "failure", None) is not None:
        from custom_agents.delegation_wake import wake_for_failed_delegation

        # A typed failure (provider limits, lost workers) needs a decision now,
        # even while other delegated tasks still run.
        if await wake_for_failed_delegation(
            self, parent_task_id=parent_task_id, subtask_info=subtask_info
        ):
            return
    await self._resume_parent_task_if_ready(parent_task_id)
