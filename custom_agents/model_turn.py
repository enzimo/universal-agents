"""Provider-independent boundary between model turns and workflow effects."""

from dataclasses import dataclass
from typing import Any, Protocol

from custom_agents.structured_llm_messages import StructuredAgentTurnDecision
from custom_messages.contracts.envelope_v1 import ContentType
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta


@dataclass(frozen=True)
class TurnDecisionOutcome:
    decision: StructuredAgentTurnDecision
    structured_output_details: dict[str, Any] | None = None


class ModelTurn(Protocol):
    async def process_llm_interaction(
        self, msg_content: ContentType, msg_header: MessageHeader, msg_meta: MessageMeta
    ) -> ContentType: ...
