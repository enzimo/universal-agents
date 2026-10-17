"""Communication support for the trusted Strands tool surface."""

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from strands import tool

from assistant_gateway.attachments import validate_user_notification_attachments
from custom_tools.workspace_scope import current_workspace_scope
from custom_messages.contracts.runtime_v1 import MEM_REVIEW_AGENT_TYPE
from custom_agents.instance_delivery_context import default_notification_gateway_context
from custom_agents.memory_plugins.config import memory_notification_delivery_target
from custom_agents.schedule_management import (
    is_system_managed_schedule,
)
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import (
    GATEWAY_MANAGER_CONTROL_TYPES,
    SCHEDULED_RUN_CONTEXT_TYPES,
    CommsMessage,
    MessageEnvelope,
)
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import (
    ScheduledMessage,
    ScheduleTaskMemory,
)
from custom_messages.enums import RequesterType, TaskHistoryImportance, TaskHistoryType
from custom_tools import file_access
from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import (
    active_task_context as _active_task_context,
)
from custom_tools.runtime_context import (
    get_agent_ref,
)
from id_generation import (
    new_id,
)

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent


from custom_tools.providers._configuration import (
    _resolve_notify_user_instance_config_path,
)
from custom_tools.providers._runtime import (
    _run_agent_coroutine,
    _schedule_store,
)
from custom_agents.schedule_fields import schedule_memory_now
from custom_tools.providers.schedules import _resolve_active_schedule_id

_MEMORY_NOTIFICATION_REASON_MARKERS = (
    "memory",
    "memreview",
    "coremem",
    "usermem",
    "pinned_memory",
    "topic_tag",
    "topic_memory",
)

logger = logging.getLogger(__name__)


@tool
async def send_message(message_json: str) -> str:
    """
    Send a structured message to another agent via the configured transport.

    Use this tool to communicate with other agents in the system. The message
    should be a valid JSON string representing a CommsMessage object with
    msg_content, msg_meta, and optionally msg_header fields.

    Args:
        message_json: JSON string representing a CommsMessage object. Must include
                     msg_content with the actual message payload, and msg_meta with
                     routing information like to_agent_name or to_named_agent_type.

    Returns:
        Status message indicating success or failure of the send operation.

    Example:
        message = {
            "msg_content": {"message_class": "Task", "task_type": "request", ...},
            "msg_meta": {"to_named_agent_type": CODE_WRITING_AGENT_TYPE, "from_agent_name": "Manager"}
        }
        send_message(json.dumps(message))
    """
    agent = get_agent_ref()
    if agent is None:
        return "Error: Agent reference not set. Cannot send message."

    try:
        # Parse the JSON message
        comms_obj = CommsMessage.model_validate_json(message_json)
        if isinstance(comms_obj.msg_content, GATEWAY_MANAGER_CONTROL_TYPES):
            logger.warning(
                "Agent-authored gateway manager control message rejected. "
                "event=reserved_gateway_manager_message_rejected agent_id=%s content_type=%s",
                agent.id,
                type(comms_obj.msg_content).__name__,
            )
            return (
                "Error: Manager control messages are reserved for the authenticated "
                "gateway and cannot be sent by an agent."
            )
        if isinstance(comms_obj.msg_content, SCHEDULED_RUN_CONTEXT_TYPES):
            logger.warning(
                "Agent-authored scheduled run context message rejected. "
                "event=reserved_run_context_message_rejected agent_id=%s content_type=%s",
                agent.id,
                type(comms_obj.msg_content).__name__,
            )
            return (
                "Error: Scheduled run context messages are reserved for the agent "
                "runtimes and cannot be sent by an agent."
            )
        if isinstance(
            comms_obj.msg_content, ScheduledMessage
        ) and is_system_managed_schedule(comms_obj.msg_content):
            logger.warning(
                "Agent-authored system schedule message rejected. "
                "event=reserved_system_schedule_message_rejected agent_id=%s schedule_id=%s",
                agent.id,
                comms_obj.msg_content.schedule_id,
            )
            return (
                "Error: System-managed schedule messages are reserved for trusted "
                "runtime producers and cannot be sent by an agent."
            )

        # Construct header using agent's method
        header = await agent._construct_header(comms_message=comms_obj)

        # Create full envelope
        envelope = MessageEnvelope(
            msg_content=comms_obj.msg_content,
            msg_meta=comms_obj.msg_meta,
            msg_header=header,
        )

        await agent.team_comms.send_message(
            message=envelope, routing_key=header.to_routing_path
        )

        return f"Message sent successfully to {header.to_routing_path}"

    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON format - {e}"
    except Exception as e:
        return f"Error sending message: {e}"


def _active_gateway_context_can_receive_notification(active_context: Any) -> bool:
    delivery_mode = (
        str(getattr(active_context, "delivery_mode", "") or "").strip().lower()
    )
    if delivery_mode != "interactive":
        return False
    channel_name = (
        str(getattr(active_context, "channel_name", "") or "").strip().lower()
    )
    if not channel_name:
        return False
    if channel_name in {"cli", "local"}:
        return True
    return bool(str(getattr(active_context, "chat_id", "") or "").strip())


def _is_memory_notification(agent: Any, *, reason: str) -> bool:
    agent_type = str(getattr(agent, "named_agent_type", "") or "").strip()
    agent_name = str(getattr(agent, "name", "") or "").strip()
    if agent_type == MEM_REVIEW_AGENT_TYPE or agent_name == MEM_REVIEW_AGENT_TYPE:
        return True
    normalized_reason = str(reason or "").strip().lower()
    return bool(
        normalized_reason
        and any(
            marker in normalized_reason
            for marker in _MEMORY_NOTIFICATION_REASON_MARKERS
        )
    )


def _auto_target(config: Any, active_context: Any) -> str:
    routing_mode = (
        str(
            getattr(getattr(getattr(config, "delivery", None), "notification_routing", None), "mode", "")
            or "source_thread_or_default"
        )
        .strip()
        .lower()
    )
    if routing_mode == "default_target":
        return "default"
    if active_context is not None and _active_gateway_context_can_receive_notification(active_context):
        return "current_thread"
    return "default"


def _current_thread_context(config: Any, active_context: Any) -> GatewayContext:
    if active_context is None:
        raise ValueError("target='current_thread' requires an active task with gateway_context.")
    return GatewayContext(
        gateway_instance_name=getattr(active_context, "gateway_instance_name", None) or config.instance_name,
        session_key=getattr(active_context, "session_key", None) or config.user.default_session_key,
        channel_name=getattr(active_context, "channel_name", None),
        user_id=getattr(active_context, "user_id", None),
        canonical_user_id=getattr(active_context, "canonical_user_id", None) or config.user.canonical_user_id,
        chat_id=getattr(active_context, "chat_id", None),
        thread_id=getattr(active_context, "thread_id", None),
        delivery_mode="interactive",
        scheduled_job_id=getattr(active_context, "scheduled_job_id", None),
        schedule_owner_agent_id=(
            getattr(active_context, "schedule_owner_agent_id", None) or config.manager.manager_agent_id
        ),
        heartbeat_source_id=getattr(active_context, "heartbeat_source_id", None) or config.heartbeat.source_id,
        original_message_id=getattr(active_context, "original_message_id", None),
    )


def _default_context(config: Any, active_context: Any, person_id: str | None) -> GatewayContext:
    """The default destination of the person the task works for.

    The configured default target is the configured user's chat, so it serves
    only their work and work for no person. For anyone else the notice goes
    through their own task's route (its session, or its schedule's delivery
    target, as for their memory reviews); with none, it is refused rather than
    sent to someone else's chat.
    """

    if not person_id or person_id == config.user.canonical_user_id:
        return default_notification_gateway_context(config)
    active_person = str(getattr(active_context, "canonical_user_id", "") or "") if active_context is not None else ""
    routed = active_context is not None and active_person == person_id and (
        getattr(active_context, "session_key", None) or getattr(active_context, "scheduled_job_id", None)
    )
    if not routed:
        logger.warning(
            "A notice for a person other than the configured user has no route to them and was not sent. "
            "event=notify_user_no_route_for_person person_id=%s",
            person_id,
        )
        raise ValueError(
            "No chat is known for the person this task works for, so the notice was not sent. "
            "They can set a default chat with /user default-channel."
        )
    return _current_thread_context(config, active_context)


def _notify_user_gateway_context(
    config: Any, *, target: str = "auto", source_meta: Any = None, person_id: str | None = None
) -> GatewayContext:
    normalized_target = str(target or "auto").strip().lower()
    active_context = getattr(source_meta, "gateway_context", None) if source_meta is not None else None
    if normalized_target == "auto":
        normalized_target = _auto_target(config, active_context)
    if normalized_target in {"current", "current_thread", "thread"}:
        return _current_thread_context(config, active_context)
    if normalized_target not in {"default", "preferred", "preferred_profile"}:
        raise ValueError("target must be 'auto', 'default', or 'current_thread'.")
    return _default_context(config, active_context, person_id)


def _attachment_access_errors(agent: Any, attachments: List[Any]) -> List[str]:
    """Why the agent may not send each attachment: the folder rules and, with
    authorization on, the folder decision (custom_tools.file_access), so a block
    on a folder also stops attaching files from it."""

    errors: List[str] = []
    for index, attachment in enumerate(attachments):
        try:
            file_access.status(attachment.path, agent=agent)
        except file_access.PathPolicyError as exc:
            errors.append(f"attachments[{index}].path cannot be sent: {exc}")
    return errors


def _compact_notify_user_message(value: Any, *, max_chars: int = 900) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


@tool
def notify_user(
    message: str,
    urgency: str = "normal",
    reason: str = "",
    attachments: Optional[List[Dict[str, Any]]] = None,
    target: str = "auto",
) -> Dict[str, Any]:
    """
    Send a user-visible notification through the gateway delivery layer.

    Use this when a task came from GitHub, a schedule, automation, or another
    non-chat source but you need to proactively tell the human something before
    the final task response. Do not use low-level `send_message` for user
    communication. To send binary output such as a screenshot, first save the
    file under the instance workspace or instance root, then pass an attachment
    object with the local file path. Do not inline bytes or base64 data in this
    tool call. The gateway owns final channel delivery, quiet hours, and
    Telegram/Discord/WhatsApp mechanics.

    Args:
        message: User-visible notification text.
        urgency: "normal" or "urgent". Urgent messages can bypass quiet hours
            when the gateway policy allows urgent delivery.
        reason: Short reason for auditability, e.g. "github_task_progress".
        attachments: Optional list of local file references. Each item may be
            {"path": "screenshots/site.png",
             "filename": "site.png",
             "content_type": "image/png",
             "caption": "Screenshot",
             "kind": "image"}. Only path is required. Relative paths start in
             this task's folder; files must be in the person's folder or the
             shared team/ and agents/ folders. The message stores file
             references, not binary bytes.
        target: "auto" follows delivery.notification_routing.mode. The default
            source_thread_or_default mode routes to the active interactive
            gateway session when the current task has one, otherwise through
            the configured proactive delivery target. default_target mode
            always uses the configured proactive delivery target. "default"
            uses the default chat of the person this task works for: the
            configured proactive delivery target for the configured user (and
            for work for no person), otherwise that person's own task or
            schedule route; with no route to them the notice is refused, never
            sent to someone else. "current_thread"
            requires an active task gateway session and is treated as
            interactive by the gateway. Memory-class notifications from
            MemReviewAgent or memory-related reasons are always routed through
            agent.memory.notifications.delivery_target, regardless of this
            requested target.
    """
    agent = get_agent_ref()
    if agent is None:
        return {
            "status": "error",
            "error": "Agent reference not set. Cannot notify user.",
        }

    rendered = str(message or "").strip()
    if not rendered:
        return {"status": "error", "error": "message is required."}

    urgency_value = str(urgency or "normal").strip().lower()
    if urgency_value not in {"normal", "urgent"}:
        return {"status": "error", "error": "urgency must be 'normal' or 'urgent'."}

    instance_config_path = _resolve_notify_user_instance_config_path(agent)
    if not instance_config_path:
        return {
            "status": "error",
            "error": (
                "No instance_config_path is available; cannot resolve the gateway preferred contact profile. "
                "Set agent.instance_config_path, agent.instance_root, TEAM_INSTANCE_CONFIG, or "
                "TEAM_INSTANCE_ROOT so notify_user can load instance_config.yaml."
            ),
        }

    source_task_id, source_meta = _active_task_context(agent)
    requested_target = str(target or "auto").strip().lower() or "auto"
    effective_target = requested_target
    notification_class = (
        "memory" if _is_memory_notification(agent, reason=reason) else "general"
    )
    if notification_class == "memory":
        try:
            effective_target = memory_notification_delivery_target(
                getattr(agent, "memory_config", None)
            )
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}

    try:
        from assistant_gateway.config import GatewayInstanceConfig

        config = GatewayInstanceConfig.load_for_process(instance_config_path)
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Failed to load instance config for user notification: {exc}",
        }

    routing_key = str(config.manager.response_queue_name or "").strip()
    if not routing_key:
        return {
            "status": "error",
            "error": "manager.response_queue_name is not configured; cannot route to gateway.",
        }

    try:
        gateway_context = _notify_user_gateway_context(
            config,
            target=effective_target,
            source_meta=source_meta,
            person_id=current_workspace_scope(agent).person_id,
        )
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    if (
        gateway_context.channel_name not in {"cli", "local"}
        and not gateway_context.chat_id
    ):
        return {
            "status": "error",
            "error": (
                "Preferred delivery target is missing chat_id. Set "
                "delivery.default_target.chat_id in instance_config.yaml."
            ),
            "channel_name": gateway_context.channel_name,
        }

    validated_attachments, attachment_errors = validate_user_notification_attachments(
        attachments,
        config=config,
        person_id=current_workspace_scope(agent).person_id,
    )
    attachment_errors += _attachment_access_errors(agent, validated_attachments)
    if attachment_errors:
        return {
            "status": "error",
            "error": "One or more attachments are invalid.",
            "attachment_errors": attachment_errors,
        }

    correlation_id = new_id("notify")
    envelope = MessageEnvelope(
        msg_content=UserNotification(
            text=rendered,
            urgency=urgency_value,  # type: ignore[arg-type]
            attachments=validated_attachments,
            reason=str(reason or "").strip() or None,
            source_task_id=source_task_id,
            source_agent_id=getattr(agent, "id", None),
            source_agent_name=getattr(agent, "name", None),
        ),
        msg_meta=MessageMeta(
            from_agent_name=getattr(agent, "name", None),
            msg_correlation_id=correlation_id,
            conversation_id=getattr(source_meta, "conversation_id", None)
            if source_meta
            else None,
            followup_to_task_id=source_task_id,
            request_timezone=(
                getattr(source_meta, "request_timezone", None)
                if source_meta and getattr(source_meta, "request_timezone", None)
                else config.user.timezone
            ),
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
            gateway_context=gateway_context,
        ),
        msg_header=MessageHeader(
            from_agent_id=getattr(agent, "id", ""),
            to_routing_path=routing_key,
            response_routing_key=getattr(agent, "id", None),
            agent_signature="",
        ),
    )

    try:
        _run_agent_coroutine(
            agent,
            agent.team_comms.send_message(message=envelope, routing_key=routing_key),
        )
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Failed to publish user notification: {exc}",
        }

    history_recorded = False
    history_error = None
    if source_task_id and hasattr(
        getattr(agent, "task_manager", None), "append_history_entry"
    ):
        try:
            message_preview = _compact_notify_user_message(rendered)
            agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.append_history_entry,
                source_task_id,
                (
                    f"User-visible notification sent through notify_user: {message_preview}"
                    if message_preview
                    else "User-visible notification sent through notify_user."
                ),
                entry_type=TaskHistoryType.TASK_EVENT,
                importance=TaskHistoryImportance.NORMAL,
                agent_name=getattr(agent, "name", None),
                agent_id=getattr(agent, "id", None),
                details={
                    "event": "notify_user_sent",
                    "notification_id": envelope.msg_content.notification_id,
                    "correlation_id": correlation_id,
                    "channel_name": gateway_context.channel_name,
                    "chat_id": gateway_context.chat_id,
                    "thread_id": gateway_context.thread_id,
                    "target": effective_target,
                    "requested_target": requested_target,
                    "notification_class": notification_class,
                    "urgency": urgency_value,
                    "reason": str(reason or "").strip() or None,
                    "message_preview": message_preview,
                    "attachment_count": len(validated_attachments),
                },
            )
            history_recorded = True
        except Exception as exc:
            history_error = (
                f"Failed to record notify_user task-history breadcrumb: {exc}"
            )
            logger.warning(
                "Failed to record notify_user task-history breadcrumb: source_task_id=%s "
                "notification_id=%s correlation_id=%s channel_name=%s chat_id=%s thread_id=%s.",
                source_task_id,
                envelope.msg_content.notification_id,
                correlation_id,
                gateway_context.channel_name,
                gateway_context.chat_id,
                gateway_context.thread_id,
                exc_info=True,
            )

    schedule_memory_notification = _record_schedule_notification_if_active(
        agent,
        message=rendered,
        notification_id=envelope.msg_content.notification_id,
        reason=str(reason or "").strip(),
    )

    return {
        "status": "success",
        "notification_id": envelope.msg_content.notification_id,
        "routing_key": routing_key,
        "channel_name": gateway_context.channel_name,
        "chat_id": gateway_context.chat_id,
        "thread_id": gateway_context.thread_id,
        "requested_target": requested_target,
        "effective_target": effective_target,
        "notification_class": notification_class,
        "correlation_id": correlation_id,
        "attachment_count": len(validated_attachments),
        "attachments": [
            attachment.model_dump(mode="json") for attachment in validated_attachments
        ],
        "task_history_recorded": history_recorded,
        "task_history_warning": history_error,
        "schedule_memory_notification": schedule_memory_notification,
        "user_visible_message_already_sent": rendered,
        "final_response_instruction": (
            "This notify_user call already sent the exact user-visible message above. "
            "If you still return a terminal task_response, do not repeat that message verbatim; "
            "continue the logical thread with only new details, caveats, or next steps."
        ),
    }


def _record_schedule_notification_if_active(
    agent: "UniversalAgent",
    *,
    message: str,
    notification_id: str,
    reason: str,
) -> dict[str, Any]:
    try:
        schedule_id = _resolve_active_schedule_id(agent)
    except ValueError:
        return {"recorded": False, "reason": "no_active_schedule"}
    try:
        store = _schedule_store(agent)
        summary = _compact_notify_user_message(message)

        def record(memory: ScheduleTaskMemory) -> bool:
            # Runs on the run notes as stored now, inside the store's write transaction.
            now = schedule_memory_now()
            memory.ledger.total_notifications += 1
            memory.ledger.last_notification_at = now
            memory.ledger.recent_notifications.append(
                {
                    "at": now,
                    "notification_id": notification_id,
                    "reason": str(reason or "").strip() or None,
                    "summary": summary,
                }
            )
            return True

        if store.edit_schedule_memory(schedule_id, record) is None:
            return {
                "recorded": False,
                "reason": "schedule_not_found",
                "schedule_id": schedule_id,
            }
        return {"recorded": True, "schedule_id": schedule_id}
    except Exception as exc:
        logger.warning(
            "Failed to record scheduled notification in schedule memory: schedule_id=%s notification_id=%s",
            schedule_id,
            notification_id,
            exc_info=True,
        )
        return {
            "recorded": False,
            "reason": "record_failed",
            "schedule_id": schedule_id,
            "error": str(exc),
        }


PROVIDER = ToolProvider(
    name="communication",
    tools=(
        send_message,
        notify_user,
    ),
)
