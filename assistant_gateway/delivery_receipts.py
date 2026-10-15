"""Pure builders for gateway delivery receipts."""

from __future__ import annotations

from custom_messages.contracts.delivery_v1 import DeliveryReceipt, UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType


def _receipt_task_id(envelope: MessageEnvelope) -> str | None:
    content = envelope.msg_content
    meta = envelope.msg_meta
    if not getattr(meta, "conversation_id", None):
        return None
    if (
        isinstance(content, Task)
        and content.task_type == MessageMetaType.RESPONSE
        and getattr(meta, "requester_type", None)
        == RequesterType.TIMER_REQUESTER
    ):
        return content.task_id
    if isinstance(content, UserNotification) and content.source_task_id:
        return content.source_task_id
    return None


def is_receiptable_delivery(envelope: MessageEnvelope) -> bool:
    return _receipt_task_id(envelope) is not None


def build_delivery_receipt_envelope(
    *,
    envelope: MessageEnvelope,
    instance_name: str,
    response_queue_name: str,
    manager_agent_id: str,
    delivery_state: str,
    delivery_via: str,
    delivered_at: str,
) -> MessageEnvelope | None:
    """Build a receipt without reading gateway state or publishing transport."""

    if not is_receiptable_delivery(envelope):
        return None
    meta = envelope.msg_meta or MessageMeta()
    gateway_context = (
        meta.gateway_context.model_copy(deep=True)
        if meta.gateway_context is not None
        else None
    )
    task_id = _receipt_task_id(envelope)
    if task_id is None:
        return None
    receipt = DeliveryReceipt(
        task_id=task_id,
        conversation_id=meta.conversation_id,
        correlation_id=meta.msg_correlation_id,
        scheduled_job_id=getattr(gateway_context, "scheduled_job_id", None),
        session_key=getattr(gateway_context, "session_key", None),
        delivery_state=delivery_state,
        delivery_via=delivery_via,
        delivered_at=delivered_at,
    )
    return MessageEnvelope(
        msg_content=receipt,
        msg_meta=MessageMeta(
            from_agent_name=f"{instance_name}_gateway",
            msg_correlation_id=meta.msg_correlation_id,
            conversation_id=meta.conversation_id,
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
            gateway_context=gateway_context,
        ),
        msg_header=MessageHeader(
            from_agent_id=response_queue_name,
            to_routing_path=manager_agent_id,
            response_routing_key=response_queue_name,
            agent_signature="",
        ),
    )
