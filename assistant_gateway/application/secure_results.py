"""Send a secure broker page's result to the task waiting for it.

Credential forms (``credential_workflow``) and tool setup pages
(``tool_setup``) finish in the person's browser. When one ends, the gateway
records that the chat request now waits for the resumed task's reply and
wakes the manager with a durable envelope that carries the secret-free
result. Both workflows use this one publisher, so the envelope, the request
record and the "manager unavailable" handling stay the same.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.enums import RequesterType

# The chat request waits for a page the person is filling in.
REQUEST_STATUS_WAITING_FOR_CREDENTIAL = "waiting_for_credential"
# The page ended; the chat request waits for the resumed task's reply.
REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT = "waiting_for_credential_result"
REQUEST_STATUS_MANAGER_UNAVAILABLE = "manager_unavailable"


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
    ) -> bool:
        """Record the result wait, then send the envelope; False without a manager (logged)."""

        manager_id = self._manager_client.manager_id
        if manager_id is None:
            self._request_service.update_status(correlation_id, REQUEST_STATUS_MANAGER_UNAVAILABLE)
            self._logger.warning(
                "A secure page result could not be sent because the manager route is unavailable. "
                "event=gateway_secure_result_manager_unavailable correlation_id=%s session_key=%s "
                "request_kind=%s result_id=%s",
                correlation_id,
                session.session_key,
                request_kind,
                result.get_id(),
            )
            return False
        envelope = MessageEnvelope(
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
                    delivery_mode="interactive",
                    schedule_owner_agent_id=manager_id,
                ),
            ),
            msg_header=MessageHeader(
                from_agent_id=self._settings.response_route,
                to_routing_path=manager_id,
                response_routing_key=self._settings.response_route,
                agent_signature="",
            ),
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
        await self._manager_client.publish_durable(envelope, routing_key=manager_id)
        return True


__all__ = [
    "GatewayCredentialWorkflowSettings",
    "REQUEST_STATUS_MANAGER_UNAVAILABLE",
    "REQUEST_STATUS_WAITING_FOR_CREDENTIAL",
    "REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT",
    "SecureResultPublisher",
]
