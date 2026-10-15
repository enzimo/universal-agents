"""Channel-neutral presentation for gateway delivery envelopes."""

from __future__ import annotations

import json
from typing import Protocol

from assistant_gateway.domain.message_types import (
    SYSTEM_NOTIFICATION_MESSAGE_TYPE,
    TOOL_SETUP_MESSAGE_TYPE,
)
from custom_messages.contracts.notification_v1 import (
    MODEL_PROVIDER_FAILOVER_NOTIFICATION_REASON,
)
from custom_messages.contracts.control_v1 import AgentStatus, SystemStatusMessage
from custom_messages.contracts.credentials_v1 import (
    TOOL_SETUP_COMPLETED,
    SecureCredentialCollectionRequest,
    SecureCredentialCollectionResult,
    ToolSetupRequestV1,
    ToolSetupResultV1,
)
from custom_messages.contracts.delivery_v1 import UserNotification, UserNotificationAttachment
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.task_v1 import Task, TaskAnswer, TaskQuery, TaskSteering
from custom_messages.presentation.task_query import render_task_query_text
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    TaskSteeringStatus,
    TaskSteeringType,
)


def _status_message_type(content: object) -> str | None:
    """Return the presentation type for supported status payloads."""

    if isinstance(content, AgentStatus):
        return "status"
    if isinstance(content, SystemStatusMessage):
        return "system_status"
    return None


def _credential_service_suffix(content: SecureCredentialCollectionResult) -> str:
    for value in (content.service_name, content.display_name):
        label = " ".join(str(value or "").split()).strip(" .")
        if label:
            return f" for {label[:120]}"
    return ""


def _render_credential_result_fallback(
    content: SecureCredentialCollectionResult,
) -> str:
    service_suffix = _credential_service_suffix(content)
    flow = str((content.metadata or {}).get("flow") or "").strip().lower()
    if content.status in {"credential_stored", "manual_signup_collected"}:
        return f"Your credentials{service_suffix} were stored successfully."
    if content.status == "credential_deleted":
        return f"Your saved credentials{service_suffix} were deleted successfully."
    if content.status == "signup_completed":
        service = " ".join(str(content.service_name or "").split()).strip(" .")
        account = f"Your {service} account" if service else "Your account"
        return f"{account} was created and its credentials were stored successfully."
    if content.status == "manual_signup_required":
        service = " ".join(str(content.service_name or "").split()).strip(" .")
        account = f"your {service} account" if service else "your account"
        return (
            f"More information is needed to finish creating {account}. "
            "Use the secure form to continue."
        )
    if content.status == "signup_failed":
        return (
            f"I couldn't create the requested account{service_suffix}. Please try again."
        )
    if flow in {"credential_deletion", "credential_delete", "delete_credential"}:
        return (
            f"I couldn't delete your saved credentials{service_suffix}. "
            "Nothing was changed."
        )
    return f"I couldn't store your credentials{service_suffix}. Please try again."


def _render_tool_setup_result_fallback(content: ToolSetupResultV1) -> str:
    """Plain-language outcome of a tool setup page, without status codes.

    The result carries its own plain sentence (``message``); a completed
    setup also says that it gives nobody access by itself.
    """

    message = " ".join(str(content.message or "").split())
    if content.status == TOOL_SETUP_COMPLETED:
        return f"{message or f'{content.tool_id} is set up.'} Setting up a tool gives nobody access by itself."
    return message or f"{content.tool_id} was not set up. Nothing was changed."


class GatewayDeliveryAttachmentResolver(Protocol):
    """Resolve safe, user-visible attachments for one message payload."""

    def resolve(
        self,
        content: object,
        *,
        person_id: str | None,
    ) -> tuple[UserNotificationAttachment, ...]:
        """Return the attachments that may cross a channel boundary.

        Files must sit in ``person_id``'s folder or the shared team/ and
        agents/ folders (runtime_settings.instance_layout).
        """


class GatewayDeliveryPresentation:
    """Render typed gateway messages without runtime or configuration access."""

    @staticmethod
    def supports_user_delivery(content: object) -> bool:
        """Return whether content has an explicit user-facing presentation."""

        return isinstance(
            content,
            (
                Task,
                TaskQuery,
                TaskSteering,
                UserNotification,
                AgentStatus,
                SystemStatusMessage,
                SecureCredentialCollectionRequest,
                SecureCredentialCollectionResult,
                ToolSetupRequestV1,
                ToolSetupResultV1,
            ),
        )

    @staticmethod
    def message_type(content: object) -> str:
        if isinstance(content, Task):
            return "task"
        if isinstance(content, TaskQuery):
            return "task_query"
        if isinstance(content, TaskSteering):
            return "task_steering"
        if isinstance(content, UserNotification):
            if content.reason == MODEL_PROVIDER_FAILOVER_NOTIFICATION_REASON:
                return SYSTEM_NOTIFICATION_MESSAGE_TYPE
            return "user_notification"
        status_message_type = _status_message_type(content)
        if status_message_type is not None:
            return status_message_type
        if isinstance(
            content,
            (SecureCredentialCollectionRequest, SecureCredentialCollectionResult),
        ):
            return "secure_credential_collection"
        if isinstance(content, (ToolSetupRequestV1, ToolSetupResultV1)):
            return TOOL_SETUP_MESSAGE_TYPE
        return type(content).__name__

    @staticmethod
    def message_level(content: object) -> str:
        """Return the portable user-attention level for a delivery payload."""

        if isinstance(content, Task) and GatewayDeliveryPresentation._task_attention(
            content
        ):
            return "attention"
        if isinstance(
            content,
            TaskQuery,
        ) and GatewayDeliveryPresentation._query_attention(content):
            return "attention"
        if isinstance(
            content,
            SystemStatusMessage,
        ) and GatewayDeliveryPresentation._system_attention(content):
            return "attention"
        return "normal"

    @staticmethod
    def render_envelope(envelope: MessageEnvelope) -> str:
        content = envelope.msg_content
        if isinstance(content, Task):
            return GatewayDeliveryPresentation._render_task(content)
        if isinstance(content, TaskQuery):
            return render_task_query_text(content)
        if isinstance(content, SecureCredentialCollectionRequest):
            return (
                f"Secure credential collection required for {content.credential_key} "
                f"({content.service_name})."
            )
        if isinstance(content, SecureCredentialCollectionResult):
            return _render_credential_result_fallback(content)
        if isinstance(content, ToolSetupRequestV1):
            return " ".join(str(content.reason or "").split()) or f"{content.tool_id} needs to be set up."
        if isinstance(content, ToolSetupResultV1):
            return _render_tool_setup_result_fallback(content)
        if isinstance(content, AgentStatus):
            return json.dumps(content.model_dump(mode="json"), indent=2)
        if isinstance(content, TaskSteering):
            return GatewayDeliveryPresentation._render_task_steering(content)
        if isinstance(content, UserNotification):
            return content.text
        if isinstance(content, SystemStatusMessage):
            return content.system_status
        raise ValueError(
            f"Unsupported gateway delivery content type: {type(content).__name__}."
        )

    @staticmethod
    def _render_task_steering(content: TaskSteering) -> str:
        reason = (content.status_reason or "").strip()
        if content.steering_type == TaskSteeringType.INTERRUPT_REQUEST:
            return GatewayDeliveryPresentation._render_interrupt_request(
                content,
                reason=reason,
            )
        if content.steering_type == TaskSteeringType.STATUS:
            return GatewayDeliveryPresentation._render_steering_status(
                content,
                reason=reason,
            )
        if content.steering_type == TaskSteeringType.PROACTIVE_GUIDANCE:
            return "Mid-turn guidance received."
        if content.steering_type == TaskSteeringType.INTERRUPT_RESPONSE:
            return "Interrupt response received."
        return reason or "Task steering update received."

    @staticmethod
    def _task_attention(content: Task) -> bool:
        latest_state = content.task_state[-1][0] if content.task_state else None
        latest_value = str(getattr(latest_state, "value", latest_state) or "").lower()
        return bool(content.errors) or latest_value in {
            ProgressState.FAILED.value,
            ProgressState.CANCELLED.value,
        }

    @staticmethod
    def _query_attention(content: TaskQuery) -> bool:
        if content.query_type != MessageMetaType.REQUEST:
            return False
        query_reason = str(
            getattr(
                getattr(content, "query_reason", None),
                "value",
                content.query_reason,
            )
            or ""
        )
        return query_reason == "recoverable_runtime_pause" or bool(
            content.recoverable_pause_trigger
        )

    @staticmethod
    def _system_attention(content: SystemStatusMessage) -> bool:
        status = str(content.system_status or "").lower()
        return any(
            marker in status
            for marker in ("error", "failed", "unavailable", "timed out", "timeout")
        )

    @staticmethod
    def _render_task(content: Task) -> str:
        latest_state = content.task_state[-1][0] if content.task_state else None
        if latest_state == ProgressState.CANCELLED or str(
            latest_state
        ).lower().endswith("cancelled"):
            return GatewayDeliveryPresentation._render_cancelled_task(content)
        answer = GatewayDeliveryPresentation._task_answer(content)
        if answer is not None and answer.strip():
            return answer
        if content.errors:
            return "Task failed:\n" + "\n".join(
                f"- {error}" for error in content.errors
            )
        return answer if answer is not None else content.problem_statement or ""

    @staticmethod
    def _render_cancelled_task(content: Task) -> str:
        answer = GatewayDeliveryPresentation._task_answer(content)
        if answer is not None:
            return answer
        if content.errors:
            return "Task cancelled:\n" + "\n".join(
                f"- {error}" for error in content.errors
            )
        return "Task cancelled."

    @staticmethod
    def _task_answer(content: Task) -> str | None:
        if isinstance(content.answer, TaskAnswer):
            return content.answer.answer
        if content.answer is not None:
            return str(content.answer)
        return None

    @staticmethod
    def _render_interrupt_request(content: TaskSteering, *, reason: str) -> str:
        if reason:
            return reason
        if content.interrupts:
            details = "\n".join(
                f"- {item.reason or item.name or item.interrupt_id}"
                for item in content.interrupts
            )
            return f"The task needs input before it can continue:\n{details}"
        return "The task needs input before it can continue."

    @staticmethod
    def _render_steering_status(content: TaskSteering, *, reason: str) -> str:
        if content.status == TaskSteeringStatus.ACCEPTED:
            return "👍"
        if content.status == TaskSteeringStatus.STALE:
            return (
                reason
                or "Mid-turn guidance arrived after the target task was no longer active."
            )
        if content.status == TaskSteeringStatus.REJECTED:
            return reason or "Mid-turn guidance was not applied."
        status = getattr(content.status, "value", content.status)
        return reason or f"Mid-turn guidance status: {status}."
