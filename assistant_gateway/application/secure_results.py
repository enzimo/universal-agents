"""Send a secure broker page's result to the task waiting for it.

Credential forms (``credential_workflow``) and tool setup pages
(``tool_setup``) finish in the person's browser. When one ends, the gateway
records that the chat request now waits for the resumed task's reply and
wakes the team lead with a durable envelope that carries the secret-free
result. Both workflows use this one publisher, so the envelope, the request
record and the handling of a result that cannot be sent stay the same.

A result is addressed when it is sent, to the team lead known at that
moment, and its NATS message id names that lead
(``secure_result:<kind>:<result id>:<lead id>``): NATS drops a repeated id
for its duplicate window (600 seconds), so a result sent again to a
replacement lead must not reuse the id of the copy sent to the old one, while
a retry to the same lead still counts as a repeat. When no lead is known the
chat request is left as it was; when a lead is known the request is recorded
as waiting for the result before the publish (so a fast reply is never
overwritten), and a publish that then fails (NATS disconnected) leaves it
waiting for a result that is still kept. Either way the caller keeps the
result, and the chat request never gets a finished status from it: a
credential form's watcher retries it, and a setup page's record keeps it
until a lead is back (``tool_setup.GatewayToolSetups.resend_kept_results``).
Before, the publisher wrote ``manager_unavailable`` onto the chat request,
which counts as finished although its task still ran, so the other setup
pages' cards disappeared and later pages were refused (incident 2026-11-14
follow-up).

"Sent" means the result was accepted into the queue of the lead known then.
A lead that stops before the gateway notices (the liveness check) can still
lose it, as it can lose any message the gateway sends it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import DELIVERY_MODE_INTERACTIVE, GatewayContext, MessageHeader, MessageMeta
from custom_messages.enums import RequesterType

# The chat request waits for a page the person is filling in.
REQUEST_STATUS_WAITING_FOR_CREDENTIAL = "waiting_for_credential"
# The page ended; the chat request waits for the resumed task's reply.
REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT = "waiting_for_credential_result"
# How long to wait before sending a result again after a failed send, doubling
# up to the maximum (credential form watchers and kept setup page results).
SECURE_RESULT_RETRY_MIN_SECONDS = 1.0
SECURE_RESULT_RETRY_MAX_SECONDS = 30.0


class SendOutcome(StrEnum):
    """What one send of a result did (``event=gateway_secure_result_not_sent reason=...``)."""

    SENT = "sent"
    # No team lead is known, so there is nowhere to send it.
    NO_ROUTE = "no_route"
    # A lead is known but the publish failed (for example NATS is disconnected).
    PUBLISH_FAILED = "publish_failed"


@dataclass(frozen=True)
class GatewayCredentialWorkflowSettings:
    """Stable gateway identity and routing settings for broker workflows."""

    gateway_instance_name: str
    user_display_name: str
    assistant_display_name: str
    team_id: str
    response_route: str
    request_timezone: str
    poll_interval_seconds: float = 2.0


class SecureResultPublisher:
    """Wake the manager with one page result for the task that waits on it."""

    def __init__(
        self,
        *,
        settings: GatewayCredentialWorkflowSettings,
        manager_client: GatewayManagerClient,
        request_service: GatewayRequestService,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._manager_client = manager_client
        self._request_service = request_service
        self._logger = logger or logging.getLogger(__name__)

    @property
    def route_known(self) -> bool:
        """Whether a team lead is known to send results to now."""

        return self._manager_client.manager_id is not None

    async def publish(
        self,
        result: Any,
        *,
        session: SessionRecord,
        correlation_id: str,
        conversation_id: str | None,
        request_kind: str,
        request_text: str,
        parent_task_id: str | None,
    ) -> SendOutcome:
        """Send the result to the team lead known now (module docstring); a failure is logged, never raised.

        When a lead is known, the chat request is recorded as waiting for the
        resumed task's reply before the publish, so a fast reply cannot be
        overwritten by it; a failed publish leaves that waiting status (never a
        finished one) while the caller keeps the result.
        """

        manager_id = self._manager_client.manager_id
        if manager_id is None:
            self._log_not_sent(SendOutcome.NO_ROUTE, result, session, correlation_id, request_kind)
            return SendOutcome.NO_ROUTE
        envelope = self._envelope(
            result,
            session=session,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            request_kind=request_kind,
            manager_id=manager_id,
        )
        self._request_service.record(
            correlation_id=correlation_id,
            session_key=session.session_key,
            request_kind=request_kind,
            request_text=request_text,
            conversation_id=conversation_id,
            followup_to_task_id=parent_task_id,
            status=REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT,
        )
        try:
            await self._manager_client.publish_durable(envelope, routing_key=manager_id)
        except Exception as error:  # noqa: BLE001 - the caller keeps the result and sends it again
            self._log_not_sent(
                SendOutcome.PUBLISH_FAILED, result, session, correlation_id, request_kind,
                manager_id=manager_id, error=error,
            )
            return SendOutcome.PUBLISH_FAILED
        return SendOutcome.SENT

    def _envelope(
        self,
        result: Any,
        *,
        session: SessionRecord,
        correlation_id: str,
        conversation_id: str | None,
        request_kind: str,
        manager_id: str,
    ) -> MessageEnvelope:
        """The durable envelope for one send, addressed to ``manager_id`` under a message id naming it."""

        return MessageEnvelope(
            msg_content=result,
            msg_meta=MessageMeta(
                from_agent_name=self._settings.user_display_name,
                msg_correlation_id=correlation_id,
                conversation_id=conversation_id,
                request_timezone=self._settings.request_timezone,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=True,
                gateway_context=GatewayContext(
                    gateway_instance_name=self._settings.gateway_instance_name,
                    session_key=session.session_key,
                    channel_name=session.channel_name,
                    user_id=session.user_id,
                    canonical_user_id=session.owner_canonical_user_id,
                    chat_id=session.chat_id,
                    thread_id=session.thread_id,
                    delivery_mode=DELIVERY_MODE_INTERACTIVE,
                    schedule_owner_agent_id=manager_id,
                ),
            ),
            msg_header=MessageHeader(
                idempotency_key=f"secure_result:{request_kind}:{result.get_id()}:{manager_id}",
                from_agent_id=self._settings.response_route,
                to_routing_path=manager_id,
                response_routing_key=self._settings.response_route,
                agent_signature="",
            ),
        )

    def _log_not_sent(
        self,
        outcome: SendOutcome,
        result: Any,
        session: SessionRecord,
        correlation_id: str,
        request_kind: str,
        *,
        manager_id: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self._logger.warning(
            "A secure page result was not sent to the team lead; its owner keeps it and sends it again. "
            "event=gateway_secure_result_not_sent reason=%s correlation_id=%s session_key=%s request_kind=%s "
            "result_id=%s manager_id=%s error_type=%s",
            outcome.value,
            correlation_id,
            session.session_key,
            request_kind,
            result.get_id(),
            manager_id or "-",
            type(error).__name__ if error is not None else "-",
        )


__all__ = [
    "GatewayCredentialWorkflowSettings",
    "REQUEST_STATUS_WAITING_FOR_CREDENTIAL",
    "REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT",
    "SECURE_RESULT_RETRY_MAX_SECONDS",
    "SECURE_RESULT_RETRY_MIN_SECONDS",
    "SecureResultPublisher",
    "SendOutcome",
]
