"""Automation-message normalization helpers for UniversalAgent."""

from __future__ import annotations

from typing import Any, Optional

from custom_agents.automation_prompt_context import build_automation_prompt_context
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import AutomationMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    TaskHistoryImportance,
    TaskHistoryType,
)


def _source_task_id(message: AutomationMessage) -> Optional[str]:
    task_id = message.original_envelope_snapshot.msg_content.get("task_id")
    return str(task_id) if task_id else None


def _normalized_automation_meta(
    message: AutomationMessage,
    msg_meta: MessageMeta,
) -> MessageMeta:
    normalized = msg_meta.model_copy(deep=True) if msg_meta else MessageMeta()
    snapshot_meta = message.original_envelope_snapshot.msg_meta
    if normalized.conversation_id is None and snapshot_meta is not None:
        normalized.conversation_id = snapshot_meta.conversation_id
    if normalized.request_timezone is None and snapshot_meta is not None:
        normalized.request_timezone = snapshot_meta.request_timezone
    if (
        normalized.gateway_context is None
        and snapshot_meta is not None
        and snapshot_meta.gateway_context is not None
    ):
        normalized.gateway_context = snapshot_meta.gateway_context.model_copy(deep=True)
    if normalized.followup_to_task_id is None:
        normalized.followup_to_task_id = (
            getattr(snapshot_meta, "followup_to_task_id", None)
            if snapshot_meta is not None
            else None
        ) or _source_task_id(message)
    normalized.requester_type = RequesterType.TIMER_REQUESTER
    normalized.is_response_requested = True
    return normalized


async def handle_automation_message(
    agent: Any,
    *,
    automation_message: AutomationMessage,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    normalized_meta = _normalized_automation_meta(automation_message, msg_meta)
    task_request = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement=build_automation_prompt_context(automation_message),
    )
    await agent._handle_task_request(task_request, msg_header, normalized_meta)

    task_info = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_task_info, task_request.task_id
    )
    if task_info is None:
        return
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task_request.task_id,
        f"Automation hook {automation_message.hook_name} created this follow-up.",
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={
            "automation_id": automation_message.automation_id,
            "hook_name": automation_message.hook_name,
            "allowed_decisions": list(automation_message.allowed_decisions),
            "source_task_id": _source_task_id(automation_message),
            "source_message_class": automation_message.original_envelope_snapshot.msg_content.get(
                "message_class"
            ),
        },
    )
