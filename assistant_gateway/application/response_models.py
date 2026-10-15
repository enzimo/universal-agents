"""Application response models shared by gateway orchestration use cases."""

from __future__ import annotations

from dataclasses import dataclass

from assistant_gateway.application.transient_presentation import (
    GatewayTransientAction,
)
from custom_messages.contracts.delivery_v1 import UserNotificationAttachment
from custom_messages.contracts.envelope_v1 import MessageEnvelope


@dataclass(frozen=True)
class GatewayResponse:
    """One user-facing response produced by gateway application orchestration."""

    correlation_id: str
    conversation_id: str | None
    task_id: str | None
    message_type: str
    text: str
    raw_envelope: MessageEnvelope
    message_level: str | None = None
    attachments: tuple[UserNotificationAttachment, ...] = ()
    transcript_text: str | None = None
    transient_action: GatewayTransientAction | None = None
