"""Real JetStream admission claims and late presence replay."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_agents.delegated_work_runtime import send_task_claim
from assistant_gateway.application.deferred_delivery import GatewayDeferredDeliveryApplication, GatewayDeferredDeliverySettings
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType, RoutingType
from custom_tools.comms_tools import TeamComms
from test_live_nats_replacement_semantics import _adapter, _subscribe_queue, _team

pytestmark = [pytest.mark.live, pytest.mark.integration, pytest.mark.nats, pytest.mark.asyncio]


async def test_delivery_receipt_waits_for_an_undiscovered_lead():
    team = _team("receipt_wait")
    adapter = _adapter(team)
    await adapter.connect(reconcile_topology=True)
    comms = TeamComms(team_id=team, nats_adapter=adapter)
    async def publish(envelope, *, routing_key):
        await comms.send_message(message=envelope, routing_key=routing_key)
    manager = SimpleNamespace(manager_id=None, settings=SimpleNamespace(team_id=team), publish_durable=publish)
    application = GatewayDeferredDeliveryApplication(settings=GatewayDeferredDeliverySettings(instance_name="example", response_route=f"{team}_gateway_response"), delivery=None, sessions=None, manager=manager, event_output=None, clock=None)
    try:
        envelope = MessageEnvelope(msg_content=Task(task_id="result", task_type=MessageMetaType.RESPONSE), msg_header=MessageHeader(from_agent_id="departed-lead"), msg_meta=MessageMeta(conversation_id="conversation", requester_type=RequesterType.TIMER_REQUESTER))
        kwargs = dict(envelope=envelope, delivery_state="delivered", delivery_via="digest", delivered_at=datetime.now(timezone.utc).isoformat())
        await application.send_receipt(**kwargs)
        await application.send_receipt(**kwargs)
        replies = await _subscribe_queue(adapter, name="replacement-lead", routing_key=team, durable_name="replacement_lead")
        delivery = await asyncio.wait_for(replies.get(), 3)
        assert delivery.envelope.msg_content.task_id == "result"
        # Re-publication uses the same key, so there is one stored receipt.
        info = await adapter._js.stream_info(adapter.mapper.work_stream_name())
        assert info.state.messages == 1
        await delivery.ack()
    finally:
        await adapter.close()


async def test_same_worker_reclaims_within_duplicate_window():
    team = _team("claim_reentry")
    adapter = _adapter(team)
    await adapter.connect(reconcile_topology=True)
    comms = TeamComms(team_id=team, nats_adapter=adapter)
    try:
        replies = await _subscribe_queue(adapter, name="claims", routing_key="lead", durable_name="claims")
        agent = SimpleNamespace(id="worker", name="Worker", team_id=team, named_agent_type="ResearchAgent", team_comms=comms, _logger=logging.getLogger(__name__))
        for worker in ("worker", "other-worker", "worker"):
            agent.id = worker
            assert await send_task_claim(agent, task_id="child", parent_task_id="root", header=MessageHeader(response_routing_key="lead"), meta=MessageMeta(requester_type=RequesterType.AGENT_REQUESTER, is_response_requested=True))
            delivery = await asyncio.wait_for(replies.get(), 3)
            assert delivery.envelope.msg_content.worker_agent_id == worker
            await delivery.ack()
    finally:
        await adapter.close()


async def test_presence_older_than_three_minutes_replays_within_retention():
    team = _team("presence_age")
    adapter = _adapter(team)
    await adapter.connect(reconcile_topology=True)
    comms = TeamComms(team_id=team, nats_adapter=adapter)
    try:
        envelope = MessageEnvelope(
            msg_content=AgentStatus(self_id="worker", team_id=team, named_agent_type="ResearchAgent", name="Worker", agent_role="Researcher", is_paused=False, is_busy=False, status_type=MessageMetaType.RESPONSE),
            msg_header=MessageHeader(from_agent_id="worker", to_routing_path=RoutingType.ALL_AGENTS_ROUTE, timestamp=(datetime.now(timezone.utc) - timedelta(seconds=240)).isoformat()),
            msg_meta=MessageMeta(),
        )
        await comms.broadcast_message(envelope, team)
        replay = await _subscribe_queue(adapter, name="late-presence", subject=f"{adapter.mapper.prefix}.presence.*", durable_name="late_presence", deliver_last_per_subject=True)
        delivery = await asyncio.wait_for(replay.get(), 3)
        assert delivery.envelope.msg_content.self_id == "worker"
        await delivery.ack()
    finally:
        await adapter.close()
