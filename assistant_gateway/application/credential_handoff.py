"""Show tool setup pages that arrive after the chat response has closed.

An agent task can ask for a setup page (sign-in, account link, team sign-in
app) after a permission approval ended the Web Chat HTTP response, or from
delegated work that never had one. The gateway then opens the page for the
Web Chat session owner (``tool_setup.GatewayToolSetups``) and keeps only a
safe notice in the transcript. Bearer page links stay in gateway memory and
are read through the authenticated Website facet, never through delivery
replay.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from assistant_gateway.application.credential_workflow import GatewayCredentialWorkflow
from assistant_gateway.application.secure_results import REQUEST_STATUS_WAITING_FOR_CREDENTIAL
from assistant_gateway.application.request_service import (
    GatewayRequestService,
    TERMINAL_REQUEST_STATUSES,
)
from assistant_gateway.application.transcript_service import GatewayTranscriptService
from assistant_gateway.application.transient_presentation import PendingCredentialAction
from assistant_gateway.ports.repositories import SessionRepository
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.domain.request_state import RequestRecord
from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1
from custom_messages.contracts.envelope_v1 import MessageEnvelope


CREDENTIAL_HANDOFF_SOURCE_KIND = "credential_handoff"


class ToolSetupHandoffs:
    """Bounded, process-local handoffs; request state prevents restart reminting."""

    def __init__(
        self,
        *,
        workflow: GatewayCredentialWorkflow,
        requests: GatewayRequestService,
        sessions: SessionRepository,
        transcript: GatewayTranscriptService,
    ) -> None:
        self._workflow = workflow
        self._requests = requests
        self._sessions = sessions
        self._transcript = transcript
        self._pending: dict[str, PendingCredentialAction] = {}
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger(__name__)

    def clear(self) -> None:
        """Discard bearer links when the gateway stops its broker pollers."""
        self._pending.clear()

    def pending_for_owner(self, owner: str) -> tuple[PendingCredentialAction, ...]:
        # Live-update reads call this from worker threads while the loop may
        # add forms, so the pending forms are copied before they are read
        # (website_handlers.off_loop).
        self._prune()
        return tuple(item for item in tuple(self._pending.values()) if item.owner == owner)

    def _prune(self) -> None:
        now = datetime.now(timezone.utc)
        for key, item in tuple(self._pending.items()):
            record = self._requests.get(item.correlation_id)
            if (
                item.expires_at <= now
                or record is None
                or record.status != REQUEST_STATUS_WAITING_FOR_CREDENTIAL
            ):
                self._pending.pop(key, None)

    async def try_present(self, envelope: MessageEnvelope) -> bool:
        content = envelope.msg_content
        if not isinstance(content, ToolSetupRequestV1):
            return False
        if not self._workflow.is_manager_sender(envelope):
            self._logger.warning(
                "event=gateway_credential_handoff_sender_rejected request_id=%s",
                content.setup_request_id,
            )
            return False
        # Serializing creation also makes concurrent redelivery idempotent.
        async with self._lock:
            return await self._present_tool_setup(envelope, content)

    async def _present_tool_setup(
        self, envelope: MessageEnvelope, content: ToolSetupRequestV1
    ) -> bool:
        meta = envelope.msg_meta
        if meta is None:
            return False
        correlation_id = str(meta.msg_correlation_id or "")
        record = self._requests.get(correlation_id)
        if record is None:
            record = self._admit_delegated_request(envelope, content)
            if record is None:
                return False
        session = self._validated_session(record, envelope, content)
        if session is None or not session.owner_canonical_user_id:
            self._logger.warning(
                "event=gateway_credential_handoff_lineage_rejected "
                "request_id=%s correlation_id=%s",
                content.setup_request_id,
                correlation_id,
            )
            return False
        self._prune()
        existing = self._pending.get(content.setup_request_id)
        if existing is not None:
            return existing.correlation_id == correlation_id
        # An already-issued form is not recreated after gateway restart. A
        # different request on the same still-waiting task is invalid, except
        # another setup page of a task that waits on its pages: set_up_tools
        # opens them all at once (owner's decision 2026-09-28).
        if record.status == REQUEST_STATUS_WAITING_FOR_CREDENTIAL and not self._workflow.tool_setups.is_new_sibling_page(
            content
        ):
            return False
        if (
            self._transcript.find_system_message(
                session_key=session.session_key,
                owner_canonical_user_id=session.owner_canonical_user_id,
                source_kind=CREDENTIAL_HANDOFF_SOURCE_KIND,
                source_id=content.setup_request_id,
            )
            is not None
        ):
            # A terminal handoff's safe receipt is also its replay tombstone.
            # Never reopen the form or send a second failure for redelivery.
            return True
        if record.status in TERMINAL_REQUEST_STATUSES:
            return False
        if len(self._pending) >= 256:
            self._logger.error("event=gateway_credential_handoff_capacity_reached")
            return False
        response = await self._workflow.tool_setups.start(
            request_content=content,
            request_envelope=envelope,
            correlation_id=correlation_id,
            conversation_id=record.conversation_id,
            session_key=session.session_key,
        )
        # tool_setups.start sends its own typed result when the page cannot
        # open, and returns the plain-language reason for the person.
        anchor = self._transcript.append_system_message(
            session=session,
            owner_canonical_user_id=session.owner_canonical_user_id,
            conversation_id=record.conversation_id,
            source_kind=CREDENTIAL_HANDOFF_SOURCE_KIND,
            source_id=content.setup_request_id,
            content=response.transcript_text or response.text,
            message_type=response.message_type,
        )
        action = response.transient_action
        if action is not None:
            expires_at = datetime.fromisoformat(action.expires_at_utc or "")
            self._pending[content.setup_request_id] = PendingCredentialAction(
                owner=session.owner_canonical_user_id,
                session_key=session.session_key,
                correlation_id=correlation_id,
                request_id=content.setup_request_id,
                anchor=anchor,
                expires_at=min(
                    expires_at, datetime.now(timezone.utc) + timedelta(minutes=31)
                ),
                action=action,
            )
            self._logger.info(
                "event=gateway_credential_handoff_presented request_id=%s "
                "correlation_id=%s session_key=%s",
                content.setup_request_id,
                correlation_id,
                session.session_key,
            )
        return True

    def _admit_delegated_request(
        self, envelope: MessageEnvelope, content: ToolSetupRequestV1
    ) -> RequestRecord | None:
        """Give delegated work its own wait without changing the chat task.

        The manager sent the request (checked by ``try_present``) for the
        owner of an active Web Chat session in the same conversation. No
        further approval is needed: the person still signs in or chooses the
        credential on the broker's page, and setting a tool up grants no access.
        """
        meta = envelope.msg_meta
        context = meta.gateway_context if meta is not None else None
        if (
            meta is None
            or context is None
            or not meta.msg_correlation_id
            or not context.session_key
        ):
            return None
        session = self._sessions.get_session(context.session_key)
        if (
            session is None
            or session.status != "active"
            or session.channel_name != "website"
            or session.chat_id != "web-chat"
            or not session.owner_canonical_user_id
            or context.canonical_user_id != session.owner_canonical_user_id
            or not meta.conversation_id
            or meta.conversation_id != session.conversation_id
        ):
            return None
        self._requests.record(
            correlation_id=meta.msg_correlation_id,
            session_key=session.session_key,
            request_kind=CREDENTIAL_HANDOFF_SOURCE_KIND,
            request_text=content.reason,
            conversation_id=meta.conversation_id,
            followup_to_task_id=content.parent_task_id,
            owner_canonical_user_id=session.owner_canonical_user_id,
        )
        self._logger.info(
            "event=gateway_delegated_credential_handoff_admitted request_id=%s task_id=%s correlation_id=%s",
            content.setup_request_id, content.parent_task_id, meta.msg_correlation_id,
        )
        return self._requests.get(meta.msg_correlation_id)

    def _validated_session(
        self,
        record: RequestRecord,
        envelope: MessageEnvelope,
        content: ToolSetupRequestV1,
    ) -> SessionRecord | None:
        session = self._sessions.get_session(record.session_key)
        meta = envelope.msg_meta
        if meta is None:
            return None
        context = meta.gateway_context
        # Persisted request identity is authoritative; envelope hints may only
        # agree with it.
        if (
            session is None
            or session.status != "active"
            or session.channel_name != "website"
            or session.chat_id != "web-chat"
            or not session.owner_canonical_user_id
            or record.owner_canonical_user_id != session.owner_canonical_user_id
            or record.conversation_id != meta.conversation_id
            or record.conversation_id != session.conversation_id
            or (record.active_task_id or record.followup_to_task_id)
            != content.parent_task_id
            or not content.parent_task_id
            or (context and context.session_key != session.session_key)
            or (
                context and context.canonical_user_id != session.owner_canonical_user_id
            )
        ):
            return None
        return session
