"""Pure outbound projection shared by interactive channel adapters."""

from __future__ import annotations

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.translation import ChannelOutboundMessage


def preview_text(text: str, *, limit: int = 80) -> str:
    """Normalize whitespace and truncate text for structured adapter logs."""
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."


def project_text_delivery(
    event: GatewayDeliveryEvent, *, channel_name: str
) -> ChannelOutboundMessage:
    """Project an event for channels that deliver attachments separately."""
    return ChannelOutboundMessage(
        channel_name=channel_name,
        chat_id=event.chat_id or "",
        thread_id=event.thread_id,
        text=event.text,
        in_reply_to_message_id=event.original_message_id,
        session_key=event.session_key,
        conversation_id=event.conversation_id,
        correlation_id=event.correlation_id,
        message_type=event.message_type,
        review_url=event.review_url,
        access_request_id=event.access_request_id,
        review_choices=event.review_choices,
    )


def project_flattened_delivery(
    event: GatewayDeliveryEvent, *, channel_name: str
) -> ChannelOutboundMessage:
    """Project an event for channels that expose attachment paths as text."""
    return ChannelOutboundMessage(
        channel_name=channel_name,
        chat_id=event.chat_id or "",
        thread_id=None,
        text=event.text_with_attachment_paths(),
        in_reply_to_message_id=event.original_message_id,
        session_key=event.session_key,
        conversation_id=event.conversation_id,
        correlation_id=event.correlation_id,
        message_type=event.message_type,
        review_url=event.review_url,
        access_request_id=event.access_request_id,
        review_choices=event.review_choices,
    )
