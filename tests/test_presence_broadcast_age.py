"""Expired control broadcasts do not reach the model; agent statuses are not applied from them."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_agents.pending_work_runtime import close_pending_work
from custom_messages.contracts.control_v1 import AgentStatus, ControlInstruction, Instruction
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.enums import ControlType, MessageMetaType, RoutingType
from tests.pending_work_support import Delivery, DrainAgent, use_simple_queue_checks


@pytest.mark.asyncio
@pytest.mark.parametrize("status_type", [MessageMetaType.REQUEST, MessageMetaType.RESPONSE, None])
async def test_expired_broadcast_only_dispatches_agent_status(monkeypatch, status_type):
    use_simple_queue_checks(monkeypatch)
    agent = DrainAgent()
    agent.max_broadcast_age_ms = 100
    sent_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    content = (
        AgentStatus(
            self_id="other_worker", named_agent_type="Worker", team_id=agent.team_id,
            status_type=status_type, info_type=ControlType.STATUS_REQUEST_BASIC,
            is_paused=False, name="Other worker", agent_role="worker", last_seen_at=sent_at,
        )
        if status_type is not None
        else ControlInstruction(type=ControlType.CONTROL_MSG, instruction=Instruction(instruction="pause"))
    )
    delivery = Delivery(
        MessageEnvelope(
            msg_content=content,
            msg_header=MessageHeader(
                from_agent_id="other_worker", to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                timestamp=sent_at,
            ),
        ),
        RoutingType.ALL_AGENTS_ROUTE.value,
    )
    try:
        await agent.on_nats_delivery(delivery)
        assert delivery.acked
        assert len(agent.dispatched) == (1 if status_type is not None else 0)
        if status_type is not None:
            assert delivery.envelope.msg_content.last_seen_at == sent_at
    finally:
        await close_pending_work(agent)
