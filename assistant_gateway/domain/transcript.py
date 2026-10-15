"""Gateway-owned presentation transcript records.

The records in this module deliberately contain no SQL, HTTP, Pydantic, or
browser concepts.  They are the stable persistence-neutral contract that a
future gateway implementation in another language must reproduce.
"""

from __future__ import annotations

from dataclasses import dataclass


TRANSCRIPT_SCHEMA_VERSION = 1
TRANSCRIPT_CHANNEL_WEBSITE = "website"
TRANSCRIPT_CHAT_WEB = "web-chat"
TRANSCRIPT_ROLE_USER = "user"
TRANSCRIPT_ROLE_ASSISTANT = "assistant"
TRANSCRIPT_ROLE_SYSTEM = "system"
TRANSCRIPT_SOURCE_CLIENT_REQUEST = "client_request"
# An access review announcement: its source id is the access request id, so
# the Web Chat card finds the request again after a reload.
TRANSCRIPT_SOURCE_ACCESS_REVIEW = "access_review"
TRANSCRIPT_SOURCE_DELIVERY_EVENT = "delivery_event"


@dataclass(frozen=True)
class TranscriptAttachmentReference:
    """Display-safe reference to attachment bytes stored outside a transcript."""

    attachment_id: str
    filename: str
    content_type: str
    kind: str
    size_bytes: int | None = None
    caption: str | None = None


@dataclass(frozen=True)
class TranscriptAppendRequest:
    """Application-owned input for one idempotent transcript append."""

    message_id: str
    session_key: str
    conversation_id: str | None
    owner_canonical_user_id: str
    channel_name: str
    chat_id: str
    role: str
    content: str
    message_type: str
    message_level: str
    correlation_id: str | None
    source_kind: str
    source_id: str
    idempotency_key: str
    attachment_references: tuple[TranscriptAttachmentReference, ...] = ()
    delivery_status: str = "presented"
    reply_to_message_id: str | None = None


@dataclass(frozen=True)
class GatewayTranscriptMessage:
    """One immutable user-visible message in gateway presentation order."""

    schema_version: int
    message_id: str
    session_key: str
    conversation_id: str | None
    owner_canonical_user_id: str
    channel_name: str
    chat_id: str
    session_sequence: int
    event_sequence: int
    role: str
    content: str
    message_type: str
    message_level: str
    correlation_id: str | None
    source_kind: str
    source_id: str
    idempotency_key: str
    attachment_references: tuple[TranscriptAttachmentReference, ...]
    delivery_status: str
    reply_to_message_id: str | None
    created_at_utc: str


@dataclass(frozen=True)
class TranscriptStreamState:
    """Persistent identity and high-water mark for one gateway transcript."""

    stream_id: str
    latest_event_sequence: int
