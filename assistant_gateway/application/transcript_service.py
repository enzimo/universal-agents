"""Application ownership for durable gateway presentation transcripts."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from assistant_gateway.application.gateway_application import (
    GatewayApplicationResponse,
    GatewayDeliveryEvent,
)
from assistant_gateway.application.ports import GatewayEventOutput, GatewayIdGenerator
from assistant_gateway.application.request_context import GatewayRequestRoute
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.application.transient_presentation import (
    SECURE_CREDENTIAL_COLLECTION_READY_TEXT,
    TOOL_SETUP_READY_TEXT,
)
from assistant_gateway.domain.message_types import (
    ACCESS_REVIEW_MESSAGE_TYPE,
    ACCOUNT_LINK_COMMAND_MESSAGE_TYPE,
    LINK_RESULT_MESSAGE_TYPE,
    TOOL_SETUP_RESULT_MESSAGE_TYPE,
    SECURE_CREDENTIAL_RESULT_MESSAGE_TYPE,
    SYSTEM_NOTIFICATION_MESSAGE_TYPE,
    TOOL_SETUP_MESSAGE_TYPE,
    TOOL_SETUP_UNAVAILABLE_MESSAGE_TYPE,
)
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.domain.transcript import (
    TRANSCRIPT_CHANNEL_WEBSITE,
    TRANSCRIPT_CHAT_WEB,
    TRANSCRIPT_ROLE_ASSISTANT,
    TRANSCRIPT_ROLE_SYSTEM,
    TRANSCRIPT_ROLE_USER,
    TRANSCRIPT_SOURCE_ACCESS_REVIEW,
    TRANSCRIPT_SOURCE_CLIENT_REQUEST,
    TRANSCRIPT_SOURCE_DELIVERY_EVENT,
    GatewayTranscriptMessage,
    TranscriptAppendRequest,
    TranscriptAttachmentReference,
    TranscriptStreamState,
)
from assistant_gateway.ports.repositories import (
    DeliveryRepository,
    SessionRepository,
    TranscriptRepository,
    TranscriptUnitOfWorkFactory,
)


class TranscriptTaskAttachmentInput(Protocol):
    """Attachment fields needed by transcript acceptance."""

    @property
    def attachment_id(self) -> str: ...

    @property
    def filename(self) -> str: ...

    @property
    def content_type(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def sha256(self) -> str: ...

    @property
    def caption(self) -> str | None: ...

    def local_path(self) -> str | None: ...


class TranscriptNotificationAttachmentInput(Protocol):
    """Response attachment fields needed by presentation projection.

    ``path`` and ``sha256`` describe the gateway's staged copy; the download
    route reads it back only while it still has that digest.
    """

    @property
    def path(self) -> str: ...

    @property
    def sha256(self) -> str | None: ...

    @property
    def filename(self) -> str | None: ...

    @property
    def content_type(self) -> str | None: ...

    @property
    def kind(self) -> str: ...

    @property
    def size_bytes(self) -> int | None: ...

    @property
    def caption(self) -> str | None: ...


TRANSCRIPT_HISTORY_DEFAULT_LIMIT = 50
TRANSCRIPT_HISTORY_MAX_LIMIT = 100
TRANSCRIPT_EVENT_DEFAULT_LIMIT = 100
TRANSCRIPT_EVENT_MAX_LIMIT = 250
_CURSOR_VERSION = 1
_SENSITIVE_RESPONSE_TEXT = {
    "secure_credential_collection": (SECURE_CREDENTIAL_COLLECTION_READY_TEXT),
    TOOL_SETUP_MESSAGE_TYPE: (TOOL_SETUP_READY_TEXT),
    "command_auth": (
        "Authentication workflow status was returned through trusted Web Chat controls."
    ),
    # A /link reply that opened a page keeps its card's own line instead
    # (GatewayTransientAction.receipt); this covers usage and error replies.
    ACCOUNT_LINK_COMMAND_MESSAGE_TYPE: (
        "The /link reply was shown when it ran and is not kept in chat history."
    ),
    "command_user_sensitive": (
        "The requested Web Chat access operation was processed. One-time material is not "
        "stored in transcript history."
    ),
}


class TranscriptCursorError(ValueError):
    """Raised when a durable cursor is malformed or belongs to another stream."""


@dataclass(frozen=True)
class TranscriptRequestRecord:
    """Request row written atomically with an accepted user transcript row."""

    correlation_id: str
    request_kind: str
    request_text: str
    followup_to_task_id: str | None
    status: str = "pending"


@dataclass(frozen=True)
class TranscriptHistoryPage:
    """Newest history page plus an opaque cursor for the preceding page."""

    messages: tuple[GatewayTranscriptMessage, ...]
    before_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class TranscriptEventPage:
    """Owner-wide ordered changes after one durable cursor."""

    messages: tuple[GatewayTranscriptMessage, ...]
    next_cursor: str
    has_more: bool


@dataclass(frozen=True)
class TranscriptAcceptance:
    """Accepted user message plus whether this caller won idempotent creation."""

    message: GatewayTranscriptMessage
    created: bool


@dataclass(frozen=True)
class _PreparedNotificationAttachment:
    """Validated attachment data awaiting the transcript insert decision."""

    reference: TranscriptAttachmentReference
    local_path: str
    sha256: str


class GatewayTranscriptService:
    """Own transcript ordering, idempotency, safe projection, and attachment refs."""

    def __init__(
        self,
        *,
        repository: TranscriptRepository,
        unit_of_work_factory: TranscriptUnitOfWorkFactory,
        session_repository: SessionRepository,
        id_generator: GatewayIdGenerator,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        self._unit_of_work_factory = unit_of_work_factory
        self._sessions = session_repository
        self._ids = id_generator
        self._logger = logger or logging.getLogger(__name__)

    @staticmethod
    def is_web_chat_route(
        session: SessionRecord,
        route: GatewayRequestRoute,
    ) -> bool:
        del session
        return (
            route.channel_name == TRANSCRIPT_CHANNEL_WEBSITE
            and route.chat_id == TRANSCRIPT_CHAT_WEB
        )

    def find_retry_exchange(
        self,
        *,
        session: SessionRecord,
        route: GatewayRequestRoute,
    ) -> tuple[GatewayTranscriptMessage, GatewayTranscriptMessage | None] | None:
        client_request_id = str(route.original_message_id or "").strip()
        if not client_request_id or not self.is_web_chat_route(session, route):
            return None
        owner = self._required_owner(session, route.canonical_user_id)
        idempotency_key = self._idempotency_key(
            owner,
            session.session_key,
            "client_request",
            client_request_id,
        )
        user_message = self._repository.get_transcript_message_by_idempotency_key(
            idempotency_key
        )
        if user_message is None:
            return None
        reply = self._repository.get_transcript_reply(
            owner_canonical_user_id=owner,
            session_key=session.session_key,
            reply_to_message_id=user_message.message_id,
        )
        return user_message, reply

    def accept_user_message(
        self,
        *,
        session: SessionRecord,
        route: GatewayRequestRoute,
        conversation_id: str | None,
        text: str,
        attachments: tuple[TranscriptTaskAttachmentInput, ...],
        request_record: TranscriptRequestRecord | None = None,
    ) -> GatewayTranscriptMessage | None:
        acceptance = self.accept_user_message_with_status(
            session=session,
            route=route,
            conversation_id=conversation_id,
            text=text,
            attachments=attachments,
            request_record=request_record,
        )
        return acceptance.message if acceptance is not None else None

    def accept_user_message_with_status(
        self,
        *,
        session: SessionRecord,
        route: GatewayRequestRoute,
        conversation_id: str | None,
        text: str,
        attachments: tuple[TranscriptTaskAttachmentInput, ...],
        request_record: TranscriptRequestRecord | None = None,
    ) -> TranscriptAcceptance | None:
        """Persist an accepted Web Chat message, optionally with its request row."""

        if not self.is_web_chat_route(session, route):
            return None
        owner = self._required_owner(session, route.canonical_user_id)
        client_request_id = str(
            route.original_message_id
            or (request_record.correlation_id if request_record is not None else "")
            or self._ids.new_transcript_message_id()
        ).strip()
        if not client_request_id:
            raise ValueError("Web Chat message identity could not be allocated.")
        correlation_id = (
            request_record.correlation_id if request_record is not None else None
        )
        append_request = TranscriptAppendRequest(
            message_id=self._ids.new_transcript_message_id(),
            session_key=session.session_key,
            conversation_id=conversation_id,
            owner_canonical_user_id=owner,
            channel_name=TRANSCRIPT_CHANNEL_WEBSITE,
            chat_id=TRANSCRIPT_CHAT_WEB,
            role=TRANSCRIPT_ROLE_USER,
            content=text,
            message_type="user_message",
            message_level="normal",
            correlation_id=correlation_id,
            source_kind=TRANSCRIPT_SOURCE_CLIENT_REQUEST,
            source_id=client_request_id,
            idempotency_key=self._idempotency_key(
                owner,
                session.session_key,
                "client_request",
                client_request_id,
            ),
            attachment_references=self._task_attachment_references(attachments),
            delivery_status="accepted",
        )
        with self._unit_of_work_factory.transcript_unit_of_work() as unit_of_work:
            message = unit_of_work.transcript.append_transcript_message(append_request)
            if message.message_id != append_request.message_id:
                unit_of_work.commit()
                return TranscriptAcceptance(message=message, created=False)
            for attachment in attachments:
                local_path = attachment.local_path()
                if local_path is None:
                    raise ValueError(
                        "Web Chat attachments require a validated local storage reference."
                    )
                unit_of_work.delivery.upsert_attachment(
                    attachment_id=attachment.attachment_id,
                    session_key=session.session_key,
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    filename=attachment.filename,
                    content_type=attachment.content_type,
                    kind=attachment.kind,
                    size_bytes=attachment.size_bytes,
                    sha256=attachment.sha256,
                    local_path=local_path,
                    owner_canonical_user_id=owner,
                )
            if request_record is not None:
                unit_of_work.request.record_request(
                    correlation_id=request_record.correlation_id,
                    session_key=session.session_key,
                    request_kind=request_record.request_kind,
                    request_text=request_record.request_text,
                    conversation_id=conversation_id,
                    followup_to_task_id=request_record.followup_to_task_id,
                    status=request_record.status,
                    owner_canonical_user_id=owner,
                )
            unit_of_work.commit()
        return TranscriptAcceptance(message=message, created=True)

    def append_gateway_response(
        self,
        *,
        session: SessionRecord,
        route: GatewayRequestRoute,
        response: GatewayResponse | GatewayApplicationResponse,
        reply_to: GatewayTranscriptMessage,
        source_kind: str = "gateway_response",
    ) -> GatewayTranscriptMessage | None:
        if not self.is_web_chat_route(session, route):
            return None
        owner = self._required_owner(session, route.canonical_user_id)
        existing_reply = self._repository.get_transcript_reply(
            owner_canonical_user_id=owner,
            session_key=session.session_key,
            reply_to_message_id=reply_to.message_id,
        )
        if existing_reply is not None:
            return existing_reply
        message_type = str(response.message_type or "assistant_message")
        safe_content = (
            str(response.transcript_text)
            if response.transcript_text is not None
            else self.safe_presentation_text(
                content=str(response.text or ""),
                message_type=message_type,
            )
        )
        role = self._role_for_message_type(message_type)
        with self._unit_of_work_factory.transcript_unit_of_work() as unit_of_work:
            prepared_attachments = self._prepare_notification_attachments(
                session=session,
                correlation_id=response.correlation_id,
                attachments=tuple(response.attachments),
            )
            message_id = self._ids.new_transcript_message_id()
            message = unit_of_work.transcript.append_transcript_message(
                TranscriptAppendRequest(
                    message_id=message_id,
                    session_key=session.session_key,
                    conversation_id=response.conversation_id,
                    owner_canonical_user_id=owner,
                    channel_name=TRANSCRIPT_CHANNEL_WEBSITE,
                    chat_id=TRANSCRIPT_CHAT_WEB,
                    role=role,
                    content=safe_content,
                    message_type=message_type,
                    message_level=str(response.message_level or "normal"),
                    correlation_id=response.correlation_id,
                    source_kind=source_kind,
                    source_id=(response.correlation_id or reply_to.message_id),
                    idempotency_key=self._idempotency_key(
                        owner,
                        session.session_key,
                        "reply",
                        reply_to.message_id,
                    ),
                    attachment_references=tuple(
                        item.reference for item in prepared_attachments
                    ),
                    reply_to_message_id=reply_to.message_id,
                )
            )
            if message.message_id == message_id:
                self._persist_prepared_attachments(
                    repository=unit_of_work.delivery,
                    session=session,
                    owner=owner,
                    conversation_id=response.conversation_id,
                    correlation_id=response.correlation_id,
                    attachments=prepared_attachments,
                )
            unit_of_work.commit()
        return message

    def append_local_exchange(
        self,
        *,
        session: SessionRecord,
        route: GatewayRequestRoute,
        conversation_id: str | None,
        text: str,
        response_text: str,
        response_message_type: str,
    ) -> tuple[GatewayTranscriptMessage, GatewayTranscriptMessage]:
        user_message = self.accept_user_message(
            session=session,
            route=route,
            conversation_id=conversation_id,
            text=text,
            attachments=(),
        )
        if user_message is None:
            raise ValueError("Local transcript exchange requires a Web Chat route.")
        response = GatewayApplicationResponse(
            text=response_text,
            conversation_id=conversation_id,
            message_type=response_message_type,
        )
        reply = self.append_gateway_response(
            session=session,
            route=route,
            response=response,
            reply_to=user_message,
            source_kind="gateway_command",
        )
        if reply is None:
            raise RuntimeError("Web Chat command response was not persisted.")
        return user_message, reply

    def find_system_message(
        self,
        *,
        session_key: str,
        owner_canonical_user_id: str,
        source_kind: str,
        source_id: str,
    ) -> GatewayTranscriptMessage | None:
        """Find a trusted local receipt without exposing repository key details."""
        return self._repository.get_transcript_message_by_idempotency_key(
            self._idempotency_key(
                owner_canonical_user_id, session_key, source_kind, source_id
            )
        )

    def append_system_message(
        self,
        *,
        session: SessionRecord,
        owner_canonical_user_id: str,
        conversation_id: str | None,
        source_kind: str,
        source_id: str,
        content: str,
        message_type: str,
    ) -> GatewayTranscriptMessage:
        """Append one trusted gateway-local receipt without inventing user input."""

        return self._repository.append_transcript_message(
            TranscriptAppendRequest(
                message_id=self._ids.new_transcript_message_id(),
                session_key=session.session_key,
                conversation_id=conversation_id or session.conversation_id,
                owner_canonical_user_id=owner_canonical_user_id,
                channel_name=TRANSCRIPT_CHANNEL_WEBSITE,
                chat_id=TRANSCRIPT_CHAT_WEB,
                role=TRANSCRIPT_ROLE_SYSTEM,
                content=self.safe_presentation_text(
                    content=content,
                    message_type=message_type,
                ),
                message_type=message_type,
                message_level="normal",
                correlation_id=None,
                source_kind=source_kind,
                source_id=source_id,
                idempotency_key=self._idempotency_key(
                    owner_canonical_user_id,
                    session.session_key,
                    source_kind,
                    source_id,
                ),
            )
        )

    def append_delivery_event(
        self,
        event: GatewayDeliveryEvent,
    ) -> GatewayTranscriptMessage | None:
        if (
            event.channel_name != TRANSCRIPT_CHANNEL_WEBSITE
            or event.chat_id != TRANSCRIPT_CHAT_WEB
        ):
            return None
        session = self._sessions.get_session(event.session_key)
        if session is None or not session.owner_canonical_user_id:
            self._logger.error(
                "Gateway refused to persist Web Chat delivery without owner state. "
                "event=web_chat_transcript_delivery_owner_missing session_key=%s "
                "correlation_id=%s",
                event.session_key,
                event.correlation_id or "-",
            )
            raise RuntimeError("Web Chat delivery requires an owner-scoped session.")
        owner = session.owner_canonical_user_id
        source_kind, source_id = self._delivery_source(event)
        message_type = str(event.message_type or "assistant_message")
        with self._unit_of_work_factory.transcript_unit_of_work() as unit_of_work:
            prepared_attachments = self._prepare_notification_attachments(
                session=session,
                correlation_id=event.correlation_id,
                attachments=event.attachments,
            )
            message_id = self._ids.new_transcript_message_id()
            message = unit_of_work.transcript.append_transcript_message(
                TranscriptAppendRequest(
                    message_id=message_id,
                    session_key=session.session_key,
                    conversation_id=event.conversation_id,
                    owner_canonical_user_id=owner,
                    channel_name=TRANSCRIPT_CHANNEL_WEBSITE,
                    chat_id=TRANSCRIPT_CHAT_WEB,
                    role=self._role_for_message_type(message_type),
                    content=self.safe_presentation_text(
                        content=event.text,
                        message_type=message_type,
                    ),
                    message_type=message_type,
                    message_level=str(event.message_level or "normal"),
                    correlation_id=event.correlation_id,
                    source_kind=source_kind,
                    source_id=source_id,
                    idempotency_key=self._idempotency_key(
                        owner,
                        session.session_key,
                        source_kind,
                        source_id,
                    ),
                    attachment_references=tuple(
                        item.reference for item in prepared_attachments
                    ),
                )
            )
            if message.message_id == message_id:
                self._persist_prepared_attachments(
                    repository=unit_of_work.delivery,
                    session=session,
                    owner=owner,
                    conversation_id=event.conversation_id,
                    correlation_id=event.correlation_id,
                    attachments=prepared_attachments,
                )
            unit_of_work.commit()
        event.message_id = message.message_id
        event.session_sequence = message.session_sequence
        event.event_sequence = message.event_sequence
        event.created_at_utc = message.created_at_utc
        return message

    @staticmethod
    def _delivery_source(event: GatewayDeliveryEvent) -> tuple[str, str]:
        """Where a delivered message came from: its access request, or the delivery event.

        An access review announcement is keyed by its request id, so the Web
        Chat card can read the request again, and announcing the same request
        twice in one conversation records one message.
        """

        if event.message_type == ACCESS_REVIEW_MESSAGE_TYPE and event.access_request_id:
            return TRANSCRIPT_SOURCE_ACCESS_REVIEW, event.access_request_id
        source_id = str(event.event_id or event.correlation_id or "").strip()
        if not source_id:
            raise ValueError("Gateway delivery events require a stable source id.")
        return TRANSCRIPT_SOURCE_DELIVERY_EVENT, source_id

    def history_page(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        before_cursor: str | None,
        limit: int = TRANSCRIPT_HISTORY_DEFAULT_LIMIT,
    ) -> TranscriptHistoryPage:
        bounded_limit = max(1, min(int(limit), TRANSCRIPT_HISTORY_MAX_LIMIT))
        before_sequence = self._decode_history_cursor(
            before_cursor,
            session_key=session_key,
        )
        rows = self._repository.list_transcript_messages(
            owner_canonical_user_id=owner_canonical_user_id,
            session_key=session_key,
            before_session_sequence=before_sequence,
            limit=bounded_limit + 1,
        )
        has_more = len(rows) > bounded_limit
        messages = rows[-bounded_limit:] if has_more else rows
        next_before = (
            self._encode_history_cursor(session_key, messages[0].session_sequence)
            if has_more and messages
            else None
        )
        page = TranscriptHistoryPage(
            messages=tuple(messages),
            before_cursor=next_before,
            has_more=has_more,
        )
        self._logger.debug(
            "event=gateway_transcript_page_loaded session_key=%s row_count=%s "
            "has_more=%s",
            session_key,
            len(page.messages),
            page.has_more,
        )
        return page

    def event_page(
        self,
        *,
        owner_canonical_user_id: str,
        after_cursor: str | None,
        limit: int = TRANSCRIPT_EVENT_DEFAULT_LIMIT,
    ) -> TranscriptEventPage:
        bounded_limit = max(1, min(int(limit), TRANSCRIPT_EVENT_MAX_LIMIT))
        stream = self._repository.get_transcript_stream_state()
        after_sequence = self._decode_event_cursor(after_cursor, stream=stream)
        rows = self._repository.list_transcript_events(
            owner_canonical_user_id=owner_canonical_user_id,
            after_event_sequence=after_sequence,
            limit=bounded_limit,
        )
        next_sequence = (
            rows[-1].event_sequence if rows else stream.latest_event_sequence
        )
        page = TranscriptEventPage(
            messages=tuple(rows),
            next_cursor=self._encode_event_cursor(stream.stream_id, next_sequence),
            has_more=(
                bool(rows)
                and rows[-1].event_sequence < stream.latest_event_sequence
                and len(rows) == bounded_limit
            ),
        )
        if page.messages or page.has_more:
            self._logger.info(
                "event=gateway_transcript_event_page_loaded row_count=%s has_more=%s",
                len(page.messages),
                page.has_more,
            )
        return page

    def snapshot_cursor(self) -> str:
        stream = self._repository.get_transcript_stream_state()
        return self._encode_event_cursor(
            stream.stream_id,
            stream.latest_event_sequence,
        )

    @staticmethod
    def safe_presentation_text(*, content: str, message_type: str) -> str:
        return _SENSITIVE_RESPONSE_TEXT.get(message_type, content)

    @staticmethod
    def _role_for_message_type(message_type: str) -> str:
        if message_type in {
            SYSTEM_NOTIFICATION_MESSAGE_TYPE,
            "system_status",
            "status",
            "task_progress_status",
            ACCESS_REVIEW_MESSAGE_TYPE,
            "secure_credential_collection",
            TOOL_SETUP_MESSAGE_TYPE,
            TOOL_SETUP_UNAVAILABLE_MESSAGE_TYPE,
            "command",
            "command_auth",
            ACCOUNT_LINK_COMMAND_MESSAGE_TYPE,
            LINK_RESULT_MESSAGE_TYPE,
            TOOL_SETUP_RESULT_MESSAGE_TYPE,
            SECURE_CREDENTIAL_RESULT_MESSAGE_TYPE,
            "command_user_sensitive",
        }:
            return TRANSCRIPT_ROLE_SYSTEM
        return TRANSCRIPT_ROLE_ASSISTANT

    @staticmethod
    def _required_owner(
        session: SessionRecord,
        route_owner: str | None,
    ) -> str:
        owner = str(session.owner_canonical_user_id or route_owner or "").strip()
        if not owner:
            raise ValueError("Web Chat transcripts require a canonical owner.")
        return owner

    @staticmethod
    def _idempotency_key(
        owner: str,
        session_key: str,
        source_kind: str,
        source_id: str,
    ) -> str:
        material = "\x1f".join(
            ("gateway-transcript-v1", owner, session_key, source_kind, source_id)
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _task_attachment_references(
        attachments: tuple[TranscriptTaskAttachmentInput, ...],
    ) -> tuple[TranscriptAttachmentReference, ...]:
        return tuple(
            TranscriptAttachmentReference(
                attachment_id=attachment.attachment_id,
                filename=attachment.filename,
                content_type=attachment.content_type,
                kind=attachment.kind,
                size_bytes=attachment.size_bytes,
                caption=attachment.caption,
            )
            for attachment in attachments
        )

    def _prepare_notification_attachments(
        self,
        *,
        session: SessionRecord,
        correlation_id: str | None,
        attachments: tuple[TranscriptNotificationAttachmentInput, ...],
    ) -> tuple[_PreparedNotificationAttachment, ...]:
        prepared: list[_PreparedNotificationAttachment] = []
        for attachment in attachments:
            # The gateway staged this copy and recorded its size and SHA-256;
            # the file is not opened here (agents' code can change the folder
            # it is in), the download route checks it when it is read.
            path = Path(attachment.path)
            digest = str(attachment.sha256 or "").strip()
            if not digest:
                self._logger.warning(
                    "Gateway omitted an attachment that was not staged from the transcript. "
                    "event=web_chat_transcript_attachment_unstaged session_key=%s "
                    "correlation_id=%s filename=%s",
                    session.session_key,
                    correlation_id or "-",
                    attachment.filename or path.name,
                )
                continue
            attachment_id = self._ids.new_transcript_attachment_id()
            filename = str(attachment.filename or path.name)
            content_type = str(
                attachment.content_type
                or mimetypes.guess_type(filename)[0]
                or "application/octet-stream"
            )
            kind = str(attachment.kind or "auto")
            if kind == "auto":
                kind = self._kind_for_content_type(content_type)
            size_bytes = int(attachment.size_bytes or 0)
            prepared.append(
                _PreparedNotificationAttachment(
                    reference=TranscriptAttachmentReference(
                        attachment_id=attachment_id,
                        filename=filename,
                        content_type=content_type,
                        kind=kind,
                        size_bytes=size_bytes,
                        caption=attachment.caption,
                    ),
                    local_path=str(path),
                    sha256=digest,
                )
            )
        return tuple(prepared)

    @staticmethod
    def _persist_prepared_attachments(
        *,
        repository: DeliveryRepository,
        session: SessionRecord,
        owner: str,
        conversation_id: str | None,
        correlation_id: str | None,
        attachments: tuple[_PreparedNotificationAttachment, ...],
    ) -> None:
        for attachment in attachments:
            reference = attachment.reference
            repository.upsert_attachment(
                attachment_id=reference.attachment_id,
                session_key=session.session_key,
                conversation_id=conversation_id,
                correlation_id=correlation_id,
                filename=reference.filename,
                content_type=reference.content_type,
                kind=reference.kind,
                size_bytes=int(reference.size_bytes or 0),
                sha256=attachment.sha256,
                local_path=attachment.local_path,
                owner_canonical_user_id=owner,
            )

    @staticmethod
    def _kind_for_content_type(content_type: str) -> str:
        normalized = content_type.lower()
        for kind in ("image", "audio", "video"):
            if normalized.startswith(f"{kind}/"):
                return kind
        return "document"

    @staticmethod
    def _encode_payload(payload: dict[str, object]) -> str:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_payload(cursor: str) -> dict[str, object]:
        try:
            padded = cursor + ("=" * (-len(cursor) % 4))
            payload = json.loads(
                base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            )
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise TranscriptCursorError("Transcript cursor is malformed.") from exc
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise TranscriptCursorError("Transcript cursor version is unsupported.")
        return payload

    def _encode_history_cursor(self, session_key: str, sequence: int) -> str:
        return self._encode_payload(
            {"kind": "history", "session": session_key, "sequence": sequence, "v": 1}
        )

    def _decode_history_cursor(
        self,
        cursor: str | None,
        *,
        session_key: str,
    ) -> int | None:
        if not cursor:
            return None
        payload = self._decode_payload(cursor)
        if payload.get("kind") != "history" or payload.get("session") != session_key:
            raise TranscriptCursorError("History cursor does not match this session.")
        raw_sequence = payload.get("sequence")
        if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, (int, str)):
            raise TranscriptCursorError("History cursor sequence is invalid.")
        try:
            sequence = int(raw_sequence)
        except ValueError as exc:
            raise TranscriptCursorError("History cursor sequence is invalid.") from exc
        if sequence < 1:
            raise TranscriptCursorError("History cursor sequence is invalid.")
        return sequence

    def _encode_event_cursor(self, stream_id: str, sequence: int) -> str:
        return self._encode_payload(
            {"kind": "events", "sequence": sequence, "stream": stream_id, "v": 1}
        )

    def _decode_event_cursor(
        self,
        cursor: str | None,
        *,
        stream: TranscriptStreamState,
    ) -> int:
        if not cursor:
            return 0
        payload = self._decode_payload(cursor)
        if payload.get("kind") != "events":
            raise TranscriptCursorError("Event cursor kind is invalid.")
        if payload.get("stream") != stream.stream_id:
            raise TranscriptCursorError(
                "Event cursor belongs to a replaced transcript stream."
            )
        raw_sequence = payload.get("sequence")
        if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, (int, str)):
            raise TranscriptCursorError("Event cursor sequence is invalid.")
        try:
            sequence = int(raw_sequence)
        except ValueError as exc:
            raise TranscriptCursorError("Event cursor sequence is invalid.") from exc
        if sequence < 0:
            raise TranscriptCursorError("Event cursor sequence is invalid.")
        if sequence > stream.latest_event_sequence:
            raise TranscriptCursorError(
                "Event cursor is ahead of this transcript stream."
            )
        return sequence


class TranscriptRecordingGatewayEventOutput:
    """Persist eligible Web Chat events before delegating channel delivery."""

    def __init__(
        self,
        *,
        transcript: GatewayTranscriptService,
        downstream: GatewayEventOutput,
    ) -> None:
        self._transcript = transcript
        self._downstream = downstream

    async def emit(self, event: GatewayDeliveryEvent) -> None:
        self._transcript.append_delivery_event(event)
        await self._downstream.emit(event)


def gateway_response_from_transcript(
    message: GatewayTranscriptMessage,
    *,
    template: GatewayResponse,
) -> GatewayResponse:
    """Rebuild a synchronous retry response without exposing storage to adapters."""

    return replace(
        template,
        correlation_id=message.correlation_id or template.correlation_id,
        conversation_id=message.conversation_id,
        message_type=message.message_type,
        text=message.content,
        message_level=message.message_level,
        attachments=(),
    )
