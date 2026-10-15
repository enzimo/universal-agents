"""Phase 2 translation models and deterministic text translator."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.replied_message_v1 import RepliedMessage
from id_generation import new_id


@dataclass
class ChannelInboundMessage:
    channel_name: str
    user_id: str
    chat_id: str
    text: str
    thread_id: str | None = None
    session_key: str | None = None
    message_id: str | None = None
    attachments: list[UserTaskAttachment] = field(default_factory=list)
    reply_to_message_id: str | None = None
    # The message ``reply_to_message_id`` names, as the channel reported it;
    # handed to the agent beside the text (custom_messages.contracts.replied_message_v1).
    replied_message: RepliedMessage | None = None


@dataclass
class ChannelOutboundMessage:
    channel_name: str
    chat_id: str
    text: str
    thread_id: str | None = None
    in_reply_to_message_id: str | None = None
    session_key: str | None = None
    conversation_id: str | None = None
    correlation_id: str | None = None
    message_type: str | None = None
    suppress_delivery: bool = False
    review_url: str | None = None
    # An access review announcement: channels with buttons offer these
    # choices (assistant_gateway.application.access_reviews).
    access_request_id: str | None = None
    review_choices: tuple[str, ...] = ()


@dataclass
class GatewayAction:
    action_type: str
    session_key: str | None
    text: str | None = None
    cron_name: str | None = None
    cron_instruction: str | None = None
    interval_seconds: int | None = None
    cron_expression: str | None = None


class DeterministicTextTranslator:
    """Translate inbound text into concrete gateway actions."""

    REMIND_EVERY_PATTERN = re.compile(
        r"^\s*remind me every\s+(?P<count>\d+)\s+(?P<unit>second|seconds|minute|minutes|hour|hours)\s+to\s+(?P<instruction>.+?)\s*$",
        re.IGNORECASE,
    )

    def translate_inbound(
        self,
        message: ChannelInboundMessage,
        *,
        has_pending_query: bool = False,
    ) -> GatewayAction:
        text = (message.text or "").strip()
        if text.lower() == "/new":
            return GatewayAction(
                action_type="new_session",
                session_key=None,
                text=text,
            )

        if has_pending_query:
            return GatewayAction(
                action_type="reply_to_query",
                session_key=message.session_key,
                text=text,
            )

        if text.lower() in {
            "show my scheduled jobs",
            "list my scheduled jobs",
            "what reminders do i have",
        }:
            return GatewayAction(
                action_type="list_cron_jobs",
                session_key=message.session_key,
            )

        if text.lower() in {
            "deliver pending messages",
            "show deferred messages",
            "what happened while i was away",
        }:
            return GatewayAction(
                action_type="deliver_pending",
                session_key=message.session_key,
            )

        reminder_match = self.REMIND_EVERY_PATTERN.match(text)
        if reminder_match:
            interval_seconds = self._unit_to_seconds(
                count=int(reminder_match.group("count")),
                unit=reminder_match.group("unit").lower(),
            )
            instruction = reminder_match.group("instruction").strip()
            return GatewayAction(
                action_type="add_cron_job",
                session_key=message.session_key,
                cron_name=new_id("reminder"),
                cron_instruction=instruction,
                interval_seconds=interval_seconds,
                text=text,
            )

        return GatewayAction(
            action_type="submit_task",
            session_key=message.session_key,
            text=text,
        )

    def render_outbound(
        self,
        *,
        inbound: ChannelInboundMessage,
        text: str,
        session_key: str | None = None,
        conversation_id: str | None = None,
        correlation_id: str | None = None,
        message_type: str | None = None,
    ) -> ChannelOutboundMessage:
        return ChannelOutboundMessage(
            channel_name=inbound.channel_name,
            chat_id=inbound.chat_id,
            text=text.strip(),
            thread_id=inbound.thread_id,
            in_reply_to_message_id=inbound.message_id,
            session_key=session_key,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            message_type=message_type,
        )

    @staticmethod
    def _unit_to_seconds(*, count: int, unit: str) -> int:
        if unit.startswith("second"):
            return count
        if unit.startswith("minute"):
            return count * 60
        if unit.startswith("hour"):
            return count * 3600
        raise ValueError(f"Unsupported unit: {unit}")
