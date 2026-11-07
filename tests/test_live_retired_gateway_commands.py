"""A memory command a 0.9 gateway queued before the upgrade, answered over real NATS (plan 13B.10, 13B.12).

The 0.9 gateway's bytes (``tests/fixtures/upgrade/0.9-manager-memory-command.json``) wait
in the team lead's durable control queue before the 0.10 lead subscribes, as they do
across an in-place upgrade. The lead's consumer allows one delivery, so a message it
could not read would be set aside at once. The lead reads it, its dispatch tells the
person, and a real 0.10 gateway connected to the same server shows the message in the
person's chat. No model is needed.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from types import SimpleNamespace
from typing import Any

import pytest
from live_nats_client import LiveNatsClient, nats_transport_config_from_env, resolve_live_transport_settings

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from custom_agents.strands_agent import UniversalAgent
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import ControlType, MessageMetaType, RoutingType
from custom_transport.base import SubscriptionDelivery
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_transport.subjects import NatsSubjectMapper
from id_generation import new_id
from storage.pinned_memory_repository import create_pinned_memory_repository
from tests.broker_access_support import forget_task_context_keys, use_gateway_key
from tests.test_retired_gateway_commands import REFUSAL, _queued_0_9_command

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]


async def _until(condition, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await condition():
            return True
        await asyncio.sleep(0.1)
    return await condition()


@pytest.mark.asyncio
async def test_a_queued_0_9_memory_command_is_answered_in_the_persons_chat_over_nats(tmp_path, monkeypatch, caplog):
    use_gateway_key(monkeypatch)
    try:
        await _answered_over_nats(tmp_path, caplog)
    finally:
        forget_task_context_keys()


async def _answered_over_nats(tmp_path, caplog) -> None:
    team_id, lead_id = new_id("live_retired"), new_id("manager")
    config = GatewayInstanceConfig.create(
        instance_name="live_retired", instance_root=str(tmp_path / "instance"),
        team_id=team_id, timezone="UTC", display_name="Owner",
        manager_agent_id=lead_id, manager_storage_sqlite_path=str(tmp_path / "manager.db"),
        state_storage_sqlite_path=str(tmp_path / "gateway.db"), named_agent_type="TeamArchitect",
    )
    config.transport = resolve_live_transport_settings()
    config.delivery.quiet_hours.enabled = False
    delivered: list[Any] = []
    gateway = UserGateway(
        config, output_sink=None, event_sink=delivered.append,
        state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
    )
    # The lead's own connection: one delivery per message, so an unreadable one is set aside at once.
    lead_transport = nats_transport_config_from_env(transport=config.transport, max_deliver=1, name=new_id("live_lead"))
    lead_adapter = NatsTransportAdapter(
        config=lead_transport,
        mapper=NatsSubjectMapper(team_id=team_id, subject_prefix=lead_transport.subject_prefix),
        logger=logging.getLogger("test.live_retired.lead_transport"),
    )

    class LeadComms:
        async def send_message(self, *, message: MessageEnvelope, routing_key: str) -> None:
            await lead_client.publish(routing_key, message, durable=True)

    lead = SimpleNamespace(
        id=lead_id, name="TeamLead", is_team_lead=True, team_id=team_id, team_comms=LeadComms(),
        _pinned_memory_repository=create_pinned_memory_repository(config.manager_storage_settings),
        _logger=logging.getLogger("test.live_retired.lead"),
    )
    world = SimpleNamespace(config=config, lead=lead)

    async def lead_status(delivery) -> None:
        envelope = delivery.envelope
        if isinstance(envelope.msg_content, AgentStatus) and envelope.msg_content.status_type == MessageMetaType.REQUEST:
            reply = MessageEnvelope(
                msg_content=AgentStatus(
                    self_id=lead_id, team_id=team_id, named_agent_type="TeamArchitect", name=lead.name,
                    agent_role="manager", is_paused=False, is_executing=False, has_execution_capacity=True,
                    is_busy=False, status_type=MessageMetaType.RESPONSE, info_type=ControlType.STATUS_REQUEST_BASIC,
                ),
                msg_header=MessageHeader(from_agent_id=lead_id, to_routing_path=envelope.msg_header.response_routing_key),
                msg_meta=MessageMeta(msg_correlation_id=envelope.msg_meta.msg_correlation_id),
            )
            await lead_client.publish(RoutingType.ALL_AGENTS_ROUTE.value, reply, operational=True, durable=False)
        await delivery.ack()

    async def lead_control(delivery) -> None:
        # What the lead's mailbox does with a delivery it read (UniversalAgent.on_nats_delivery).
        envelope = delivery.envelope
        await UniversalAgent._dispatch_message(
            lead, msg_header=envelope.msg_header, msg_content=envelope.msg_content, msg_meta=envelope.msg_meta,
        )
        await delivery.ack()

    async with LiveNatsClient(team_id, transport=config.transport) as lead_client:
        await lead_adapter.connect(reconcile_topology=True)
        mapper = lead_adapter.mapper
        control_subject = mapper.agent_control_subject(lead_id)
        durable = mapper.durable_name(f"agent_{lead_id}_control")
        try:
            # Queued by the 0.9 gateway before the upgrade; nobody reads it yet.
            await lead_adapter._js.publish(control_subject, _queued_0_9_command(world))
            await lead_client.adapter.subscribe_subject(
                name="live-retired-lead-status", subject=lead_client.mapper.operational_subject(),
                handler=lead_status, delivery=SubscriptionDelivery.CORE,
                logical_route=RoutingType.ALL_AGENTS_ROUTE.value,
            )
            await gateway.connect(start_services=False)
            with caplog.at_level(logging.INFO):
                # The 0.10 lead starts and reads its control queue.
                await lead_adapter.subscribe_subject(
                    name="live-retired-lead-control", subject=control_subject, handler=lead_control,
                    durable_name=durable, logical_route=lead_id,
                )
                assert await _until(lambda: _delivered(delivered), timeout=15), "the person was never answered"

            (event,) = delivered
            assert event.text == f"Your memory command was not carried out\n\n{REFUSAL}"
            assert (event.channel_name, event.chat_id, event.session_key) == ("cli", "owner-chat", "cli:owner")
            info = await lead_adapter._js.consumer_info(mapper.work_stream_name(), durable)
            assert (info.num_pending, info.num_ack_pending) == (0, 0)
            dead_letters = await lead_adapter._js.stream_info(mapper.dead_letter_stream_name())
            assert dead_letters.state.messages == 0, "nothing was set aside"
            assert lead._pinned_memory_repository.list_unexpired_facts() == []
            assert not any("event=nats_message_undecodable" in r.getMessage() for r in caplog.records)
        finally:
            await gateway.close()
            await lead_adapter.close()
            for stream in (mapper.work_stream_name(), mapper.presence_stream_name(), mapper.dead_letter_stream_name()):
                try:
                    await lead_client.adapter._js.delete_stream(stream)
                except Exception:
                    pass

    # Memory stays the team services' and the lead's: the gateway opened no memory store.
    with sqlite3.connect(tmp_path / "gateway.db") as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {"pinned_memory_facts_active", "pinned_memory_fact_events"} & tables


async def _delivered(delivered: list[Any]) -> bool:
    return bool(delivered)
