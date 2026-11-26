"""Application-owned projection of manager envelopes to delivery events."""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Protocol

from assistant_gateway.application.delivery_presentation import (
    GatewayDeliveryAttachmentResolver,
    GatewayDeliveryPresentation,
)
from assistant_gateway.application.delivery_service import GatewayDeliveryService
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.domain.request_state import RequestRecord
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.ports.repositories import SessionRepository
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext


class GatewayDeliveryRefResolver(Protocol):
    """Opaque delivery references of gateway sessions (``custom_transport.protocol1.delivery_refs``)."""

    def ref(self, session_key: str) -> str: ...


@dataclass(frozen=True)
class GatewayDeliveryProjectionSettings:
    """Stable fallback settings for inbound delivery projection."""

    default_session_key: str


@dataclass(frozen=True)
class _DeliveryMetadata:
    gateway_context: GatewayContext | None
    conversation_id: str | None
    scheduled_job_id: str | None
    correlation_id: str | None
    session_key: str | None
    delivery_ref: str | None = None


@dataclass(frozen=True)
class _DeliveryRoute:
    channel_name: str | None = None
    user_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None


class DeliveryPersonMismatch(RuntimeError):
    """A message for one person would have been delivered to another person's session."""


_DELIVERY_REF_SCAN_LIMIT = 10_000


class GatewayDeliveryProjection:
    """Resolve trusted gateway state into a channel-neutral delivery event."""

    def __init__(
        self,
        *,
        settings: GatewayDeliveryProjectionSettings,
        sessions: SessionRepository,
        delivery: GatewayDeliveryService,
        presentation: GatewayDeliveryPresentation,
        attachments: GatewayDeliveryAttachmentResolver,
        delivery_refs: GatewayDeliveryRefResolver | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._delivery = delivery
        self._presentation = presentation
        self._attachments = attachments
        self._delivery_refs = delivery_refs
        self._logger = logger or logging.getLogger(__name__)

    def project(
        self,
        *,
        envelope: MessageEnvelope,
        rendered_text: str,
        request_record: RequestRecord | None,
    ) -> GatewayDeliveryEvent:
        """Project one envelope using persisted route fallbacks when required."""

        metadata = self._metadata(envelope)
        session_key, session = self._resolve_session(
            metadata,
            request_record=request_record,
        )
        scheduled_job_id = self._resolve_scheduled_job_id(metadata)
        context_route = self._context_route(metadata.gateway_context)
        session_route = self._session_route(session)
        return GatewayDeliveryEvent(
            text=rendered_text,
            session_key=session_key,
            channel_name=self._prefer(
                context_route.channel_name,
                session_route.channel_name,
            ),
            user_id=self._prefer(context_route.user_id, session_route.user_id),
            chat_id=self._prefer(context_route.chat_id, session_route.chat_id),
            thread_id=self._prefer(context_route.thread_id, session_route.thread_id),
            conversation_id=metadata.conversation_id,
            scheduled_job_id=scheduled_job_id,
            correlation_id=metadata.correlation_id,
            original_message_id=(
                metadata.gateway_context.original_message_id
                if metadata.gateway_context
                else None
            ),
            message_type=self._presentation.message_type(envelope.msg_content),
            message_level=self._presentation.message_level(envelope.msg_content),
            attachments=self._attachments.resolve(
                envelope.msg_content,
                person_id=(
                    metadata.gateway_context.canonical_user_id
                    if metadata.gateway_context
                    else None
                ),
            ),
            event_id=(
                envelope.msg_header.idempotency_key or envelope.msg_header.message_id
            ),
        )

    def _resolve_session(
        self,
        metadata: _DeliveryMetadata,
        *,
        request_record: RequestRecord | None,
    ) -> tuple[str, SessionRecord | None]:
        session_key = metadata.session_key
        self._log_missing_route_context(
            gateway_context=metadata.gateway_context,
            session_key=session_key,
            delivery_ref=metadata.delivery_ref,
            correlation_id=metadata.correlation_id,
            conversation_id=metadata.conversation_id,
            scheduled_job_id=metadata.scheduled_job_id,
        )
        if session_key is None and request_record and request_record.session_key:
            session_key = request_record.session_key
            self._logger.warning(
                "Gateway reply routing fell back to request-record session mapping "
                "(correlation_id=%s, conversation_id=%s, session_key=%s).",
                metadata.correlation_id,
                metadata.conversation_id,
                session_key,
            )
        existing_target = None
        target_checked = False
        if session_key is None and metadata.scheduled_job_id:
            existing_target = self._delivery.get_scheduled_target(metadata.scheduled_job_id)
            target_checked = True
            if existing_target is not None and existing_target.session_key:
                person = (
                    getattr(metadata.gateway_context, "canonical_user_id", None)
                    if metadata.gateway_context
                    else None
                )
                if not person or not existing_target.owner_canonical_user_id or person == existing_target.owner_canonical_user_id:
                    session_key = existing_target.session_key
        session = self._sessions.get_session(session_key) if session_key else None
        if session is None and metadata.delivery_ref and self._delivery_refs is not None:
            session = self._session_by_delivery_ref(metadata)
            if session is not None:
                session_key = session.session_key
                if metadata.scheduled_job_id:
                    self._record_scheduled_target(
                        metadata=metadata,
                        session=session,
                        existing_target=existing_target,
                        target_checked=target_checked,
                    )
        if session is None and metadata.conversation_id:
            session = self._sessions.get_session_by_conversation_id(
                metadata.conversation_id
            )
            if session is not None:
                session_key = session.session_key
                self._logger.warning(
                    "Gateway reply routing fell back to conversation_id -> session mapping "
                    "(correlation_id=%s, conversation_id=%s, session_key=%s).",
                    metadata.correlation_id,
                    metadata.conversation_id,
                    session_key,
                )
        if session_key is None:
            session_key = self._settings.default_session_key
            session = self._sessions.get_session(session_key)
            self._refuse_other_persons_session(metadata, session)
            self._logger.warning(
                "Gateway reply routing fell back to default session "
                "(correlation_id=%s, conversation_id=%s, session_key=%s).",
                metadata.correlation_id,
                metadata.conversation_id,
                session_key,
            )
        return session_key, session

    def _record_scheduled_target(
        self,
        *,
        metadata: _DeliveryMetadata,
        session: SessionRecord,
        existing_target: object,
        target_checked: bool,
    ) -> None:
        """Record a schedule's resolved chat in gateway_scheduled_targets (plan 13C.6) without
        overwriting another person's existing target or letting a write error break delivery."""

        scheduled_job_id = metadata.scheduled_job_id
        if not scheduled_job_id:
            return
        person_id = (
            getattr(metadata.gateway_context, "canonical_user_id", None)
            if metadata.gateway_context is not None
            else None
        )
        reply_owner = person_id or session.owner_canonical_user_id
        try:
            existing = existing_target if target_checked else self._delivery.get_scheduled_target(scheduled_job_id)
        except Exception as exc:
            self._logger.warning(
                "Could not read existing scheduled target before recording resolved chat "
                "(event=gateway_scheduled_target_record_failed scheduled_job_id=%s session_key=%s "
                "correlation_id=%s error=%s).",
                scheduled_job_id,
                session.session_key,
                metadata.correlation_id,
                exc,
            )
            return
        if existing is not None:
            existing_owner = getattr(existing, "owner_canonical_user_id", None)
            if existing_owner and existing_owner != reply_owner:
                self._logger.warning(
                    "Refused to overwrite another person's scheduled target from reply delivery_ref "
                    "(event=gateway_scheduled_target_owner_mismatch scheduled_job_id=%s "
                    "target_owner=%s reply_person=%s correlation_id=%s).",
                    scheduled_job_id,
                    existing_owner,
                    reply_owner or "",
                    metadata.correlation_id,
                )
                return
        try:
            self._delivery.upsert_scheduled_target(
                scheduled_job_id=scheduled_job_id,
                session_key=session.session_key,
                conversation_id=metadata.conversation_id or session.conversation_id,
                owner_canonical_user_id=reply_owner,
            )
        except Exception as exc:
            self._logger.warning(
                "Failed to record resolved chat in gateway_scheduled_targets; continuing delivery "
                "(event=gateway_scheduled_target_record_failed scheduled_job_id=%s session_key=%s "
                "correlation_id=%s error=%s).",
                scheduled_job_id,
                session.session_key,
                metadata.correlation_id,
                exc,
            )

    def _session_by_delivery_ref(self, metadata: _DeliveryMetadata) -> SessionRecord | None:
        """Resolve an opaque ``delivery_ref`` against the person's own gateway sessions."""

        if not metadata.delivery_ref or self._delivery_refs is None:
            return None
        person_id = (
            getattr(metadata.gateway_context, "canonical_user_id", None)
            if metadata.gateway_context is not None
            else None
        )
        candidates = self._sessions.list_sessions(
            limit=_DELIVERY_REF_SCAN_LIMIT,
            owner_canonical_user_id=person_id,
        )
        for candidate in candidates:
            if hmac.compare_digest(
                self._delivery_refs.ref(candidate.session_key),
                metadata.delivery_ref,
            ):
                return candidate
        if len(candidates) >= _DELIVERY_REF_SCAN_LIMIT:
            self._logger.warning(
                "Delivery reference lookup reached the person session scan limit without a match. "
                "event=gateway_delivery_ref_scan_limit_reached scope=person person_id=%s "
                "limit=%d scheduled_job_id=%s correlation_id=%s",
                person_id,
                _DELIVERY_REF_SCAN_LIMIT,
                metadata.scheduled_job_id,
                metadata.correlation_id,
            )
        if person_id is not None:
            # Legacy single-user sessions may have owner_canonical_user_id=None;
            # query only ownerless sessions so a miss never scans other people's chats.
            unowned = self._sessions.list_unowned_sessions(limit=_DELIVERY_REF_SCAN_LIMIT)
            for candidate in unowned:
                if hmac.compare_digest(
                    self._delivery_refs.ref(candidate.session_key),
                    metadata.delivery_ref,
                ):
                    return candidate
            if len(unowned) >= _DELIVERY_REF_SCAN_LIMIT:
                self._logger.warning(
                    "Delivery reference lookup reached the unowned session scan limit without a match. "
                    "event=gateway_delivery_ref_scan_limit_reached scope=unowned person_id=%s "
                    "limit=%d scheduled_job_id=%s correlation_id=%s",
                    person_id,
                    _DELIVERY_REF_SCAN_LIMIT,
                    metadata.scheduled_job_id,
                    metadata.correlation_id,
                )
        return None

    def _refuse_other_persons_session(self, metadata: _DeliveryMetadata, session: SessionRecord | None) -> None:
        """The default session belongs to its owner: never deliver another person's message there."""

        person = getattr(metadata.gateway_context, "canonical_user_id", None) if metadata.gateway_context else None
        owner = getattr(session, "owner_canonical_user_id", None) if session is not None else None
        if person and owner and person != owner:
            self._logger.error(
                "A message for one person had no route to them and would have gone to another person's "
                "default session; it was not delivered. event=gateway_delivery_person_mismatch "
                "person_id=%s session_owner=%s correlation_id=%s",
                person,
                owner,
                metadata.correlation_id,
            )
            raise DeliveryPersonMismatch(person)

    def _resolve_scheduled_job_id(self, metadata: _DeliveryMetadata) -> str | None:
        scheduled_job_id = metadata.scheduled_job_id
        scheduled_target = (
            self._delivery.get_scheduled_target(scheduled_job_id)
            if scheduled_job_id
            else None
        )
        if scheduled_target is None and metadata.conversation_id:
            scheduled_target = self._delivery.get_scheduled_target_by_conversation_id(
                metadata.conversation_id
            )
            if scheduled_target is not None:
                self._logger.warning(
                    "Gateway reply routing fell back to conversation_id -> scheduled target mapping "
                    "(correlation_id=%s, conversation_id=%s, scheduled_job_id=%s).",
                    metadata.correlation_id,
                    metadata.conversation_id,
                    scheduled_target.scheduled_job_id,
                )
        if scheduled_job_id is None and scheduled_target is not None:
            scheduled_job_id = scheduled_target.scheduled_job_id
        return scheduled_job_id

    @staticmethod
    def _metadata(envelope: MessageEnvelope) -> _DeliveryMetadata:
        msg_meta = envelope.msg_meta
        if msg_meta is None:
            return _DeliveryMetadata(
                gateway_context=None,
                conversation_id=None,
                scheduled_job_id=None,
                correlation_id=None,
                session_key=None,
            )
        gateway_context = msg_meta.gateway_context
        return _DeliveryMetadata(
            gateway_context=gateway_context,
            conversation_id=msg_meta.conversation_id,
            scheduled_job_id=(
                gateway_context.scheduled_job_id if gateway_context else None
            ),
            correlation_id=msg_meta.msg_correlation_id,
            session_key=gateway_context.session_key if gateway_context else None,
            delivery_ref=gateway_context.delivery_ref if gateway_context else None,
        )

    @staticmethod
    def _context_route(gateway_context: GatewayContext | None) -> _DeliveryRoute:
        if gateway_context is None:
            return _DeliveryRoute()
        return _DeliveryRoute(
            channel_name=gateway_context.channel_name,
            user_id=gateway_context.user_id,
            chat_id=gateway_context.chat_id,
            thread_id=gateway_context.thread_id,
        )

    @staticmethod
    def _session_route(session: SessionRecord | None) -> _DeliveryRoute:
        if session is None:
            return _DeliveryRoute()
        return _DeliveryRoute(
            channel_name=session.channel_name,
            user_id=session.user_id,
            chat_id=session.chat_id,
            thread_id=session.thread_id,
        )

    @staticmethod
    def _prefer(primary: str | None, fallback: str | None) -> str | None:
        return primary or fallback

    def _log_missing_route_context(
        self,
        *,
        gateway_context: object | None,
        session_key: str | None,
        delivery_ref: str | None,
        correlation_id: str | None,
        conversation_id: str | None,
        scheduled_job_id: str | None,
    ) -> None:
        if gateway_context is None:
            self._logger.warning(
                "Gateway reply missing gateway_context; attempting fallback routing via persisted state "
                "(correlation_id=%s, conversation_id=%s).",
                correlation_id,
                conversation_id,
            )
        elif session_key is None and not delivery_ref:
            self._logger.warning(
                "Gateway reply gateway_context missing session_key; attempting fallback routing via persisted state "
                "(correlation_id=%s, conversation_id=%s, scheduled_job_id=%s).",
                correlation_id,
                conversation_id,
                scheduled_job_id,
            )
