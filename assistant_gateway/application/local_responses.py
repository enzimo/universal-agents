"""Factory for gateway-local user-facing responses."""

from __future__ import annotations

from dataclasses import dataclass

from assistant_gateway.application.ports import GatewayIdGenerator
from assistant_gateway.application.response_models import GatewayResponse
from custom_messages.contracts.control_v1 import SystemStatusMessage
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import RequesterType


@dataclass(frozen=True)
class GatewayLocalResponseSettings:
    """Stable origin settings for locally generated responses."""

    instance_name: str
    response_route: str


class GatewayLocalResponseFactory:
    """Create deterministic responses without contacting the manager."""

    def __init__(
        self,
        *,
        settings: GatewayLocalResponseSettings,
        id_generator: GatewayIdGenerator,
    ) -> None:
        self._settings = settings
        self._id_generator = id_generator

    def create(
        self,
        *,
        text: str,
        message_type: str = "system_status",
        correlation_id: str | None = None,
        conversation_id: str | None = None,
    ) -> GatewayResponse:
        resolved_correlation_id = (
            correlation_id or self._id_generator.new_local_correlation_id()
        )
        envelope = MessageEnvelope(
            msg_content=SystemStatusMessage(system_status=text),
            msg_meta=MessageMeta(
                from_agent_name=f"{self._settings.instance_name}_gateway",
                msg_correlation_id=resolved_correlation_id,
                conversation_id=conversation_id,
                requester_type=RequesterType.SELF_REQUESTER,
                is_response_requested=False,
            ),
            msg_header=MessageHeader(
                from_agent_id=self._settings.response_route,
                to_routing_path=self._settings.response_route,
                response_routing_key=self._settings.response_route,
                agent_signature="",
            ),
        )
        return GatewayResponse(
            correlation_id=resolved_correlation_id,
            conversation_id=conversation_id,
            task_id=None,
            message_type=message_type,
            text=text,
            raw_envelope=envelope,
        )
