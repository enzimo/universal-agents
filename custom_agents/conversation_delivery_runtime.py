"""Conversation delivery-state helpers for UniversalAgent."""

from __future__ import annotations

from typing import Any

from custom_messages.contracts.delivery_v1 import DeliveryReceipt
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import TaskHistoryImportance, TaskHistoryType


async def handle_delivery_receipt(
    agent: Any,
    *,
    receipt: DeliveryReceipt,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    if agent._conversation_store is None:
        agent._logger.warning(
            "[%s] Ignoring DeliveryReceipt %s because no conversation store is configured.",
            agent.id,
            receipt.receipt_id,
        )
        return

    updated_turn = agent._conversation_store.update_turn_delivery(
        task_id=receipt.task_id,
        conversation_id=receipt.conversation_id,
        correlation_id=receipt.correlation_id,
        delivery_state=receipt.delivery_state,
        delivery_via=receipt.delivery_via,
        delivered_at=receipt.delivered_at,
    )
    if updated_turn is None:
        agent._logger.warning(
            "[%s] DeliveryReceipt %s did not match a conversation turn: task_id=%s conversation_id=%s correlation_id=%s scheduled_job_id=%s",
            agent.id,
            receipt.receipt_id,
            receipt.task_id,
            receipt.conversation_id,
            receipt.correlation_id,
            receipt.scheduled_job_id,
        )
        return

    task_id = updated_turn.get("task_id")
    if task_id and await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_task_info, task_id
    ):
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_history_entry,
            task_id,
            f"Gateway marked this turn as {receipt.delivery_state} via {receipt.delivery_via or 'unknown'}.",
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.NORMAL,
            details={
                "delivery_receipt_id": receipt.receipt_id,
                "delivery_state": receipt.delivery_state,
                "delivery_via": receipt.delivery_via,
                "delivered_at": receipt.delivered_at,
                "session_key": receipt.session_key,
                "scheduled_job_id": receipt.scheduled_job_id,
                "gateway_from_agent_id": msg_header.from_agent_id
                if msg_header
                else None,
                "gateway_from_agent_name": getattr(msg_meta, "from_agent_name", None),
            },
        )

    agent._logger.info(
        "[%s] Applied DeliveryReceipt %s to task_id=%s conversation_id=%s state=%s via=%s",
        agent.id,
        receipt.receipt_id,
        updated_turn.get("task_id"),
        updated_turn.get("conversation_id"),
        receipt.delivery_state,
        receipt.delivery_via,
    )
