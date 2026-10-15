"""Queued schedules can arrive inside the first subscription await."""

import asyncio
import sqlite3

from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_agents.schedule_store import ScheduleStore
from custom_agents.strands_agent import UniversalAgent
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_transport.base import InboundDelivery, TransportPublishOptions
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_transport.subjects import NatsSubjectMapper
from id_generation import new_id
from live_nats_client import nats_transport_config_from_env
from storage.config import resolve_storage_settings
from test_team_lead_delivery import lead_delivery_probe


class SubscriptionProbeComplete(Exception):
    """Stop startup after exercising the subscription boundary under test."""


def startup_probe(tmp_path):
    agent = lead_delivery_probe()
    agent.task_manager = SimpleNamespace(
        _db_path=str(tmp_path / "runtime.db"),
        storage_settings=resolve_storage_settings(
            None, sqlite_fallback_path=str(tmp_path / "runtime.db")
        ),
        operations_runtime=TaskOperationRuntime(),
    )
    agent._schedule_store = None
    agent._scheduler_wake = None
    agent._activity_client = None
    agent.instance_config_path = "instance_config.yaml"
    agent.instance_timezone = "UTC"
    agent.model_fields_set = set()
    agent.authorization_config = {"enabled": False}
    agent.on_nats_operational_delivery = AsyncMock()
    agent._cleanup_nats_runtime_on_exit = AsyncMock()
    agent._nats_mapper = lambda: NatsSubjectMapper(team_id=agent.team_id)
    for method in (
        "_ensure_schedule_store",
        "_handle_scheduled_message",
        "_connect_nats",
    ):
        setattr(agent, method, MethodType(getattr(UniversalAgent, method), agent))
    return agent


def schedule_envelope(route):
    return MessageEnvelope(
        msg_content=ScheduledMessage(
            schedule_id="startup_ensure",
            schedule_name="Startup recurring work",
            install_mode="ensure",
            interval_ms=60_000,
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement="Run scheduled work later.",
            ),
            payload_header=MessageHeader(from_agent_id="scheduler"),
            payload_meta=MessageMeta(),
        ),
        msg_header=MessageHeader(from_agent_id="scheduler", to_routing_path=route),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route_kind", ["direct", "lead"])
@pytest.mark.parametrize("preopened_store", [False, True])
async def test_startup_persists_schedule_before_subscription_returns(
    tmp_path, route_kind, preopened_store, caplog
):
    agent = startup_probe(tmp_path)
    original_store = agent._ensure_schedule_store() if preopened_store else None
    route = agent.id if route_kind == "direct" else agent.team_id
    ack, nak = AsyncMock(), AsyncMock()
    wake_events = []

    async def subscribe_route(**kwargs):
        if kwargs["routing_key"] != route:
            return
        # NATS may run this callback before subscribe_route returns to startup.
        await kwargs["handler"](
            InboundDelivery(
                envelope=schedule_envelope(route),
                logical_route=route,
                transport_subject=agent._nats_mapper().route_subject(route),
                _ack=ack,
                _nak=nak,
            )
        )
        wake_events.append(agent._scheduler_wake)
        raise SubscriptionProbeComplete()

    adapter = SimpleNamespace(
        connect=AsyncMock(),
        subscribe_route=subscribe_route,
        subscribe_subject=AsyncMock(),
        keepalive_while=AsyncMock(),
    )
    agent._ensure_nats_transport_adapter = lambda: adapter
    with pytest.raises(SubscriptionProbeComplete):
        await agent._connect_nats()
    nak.assert_not_awaited()
    ack.assert_awaited_once()
    assert "Exception processing NATS delivery" not in caplog.text
    assert agent._schedule_store is not None
    assert wake_events[0] is not None and wake_events[0].is_set()
    if original_store is not None:
        assert agent._schedule_store is original_store
    reopened = ScheduleStore(db_path=agent.task_manager._db_path)
    stored = reopened.get_scheduled_message("startup_ensure")
    assert stored is not None
    assert stored.install_mode == "ensure"


@pytest.mark.asyncio
async def test_schedule_storage_failure_opens_no_inbound_subscription(tmp_path):
    agent = startup_probe(tmp_path)
    adapter = SimpleNamespace(
        connect=AsyncMock(), subscribe_route=AsyncMock(), subscribe_subject=AsyncMock()
    )
    agent._ensure_nats_transport_adapter = lambda: adapter
    agent._ensure_schedule_store = Mock(
        side_effect=sqlite3.OperationalError("unavailable")
    )
    with pytest.raises(sqlite3.OperationalError, match="unavailable"):
        await agent._connect_nats()
    adapter.subscribe_route.assert_not_awaited()
    adapter.subscribe_subject.assert_not_awaited()
    agent._cleanup_nats_runtime_on_exit.assert_awaited_once_with(adapter)


@pytest.mark.live
@pytest.mark.nats
@pytest.mark.asyncio
@pytest.mark.parametrize("route_kind", ["direct", "lead"])
async def test_live_backlogged_schedule_is_persisted_during_startup(
    tmp_path, route_kind, monkeypatch, caplog
):
    agent = startup_probe(tmp_path)
    route = agent.id if route_kind == "direct" else agent.team_id
    mapper = NatsSubjectMapper(
        team_id=agent.team_id, subject_prefix=new_id("startup_test")
    )
    # The live NATS address and credentials, resolved as every live test does.
    config = nats_transport_config_from_env()
    publisher = NatsTransportAdapter(config=config, mapper=mapper)
    receiver = NatsTransportAdapter(config=config, mapper=mapper)
    delivered = asyncio.Event()
    subscribe = receiver.subscribe_route

    async def subscribe_with_startup_delivery(**kwargs):
        if kwargs["routing_key"] != route:
            return await subscribe(**kwargs)
        handler = kwargs["handler"]

        async def receive(delivery):
            await handler(delivery)
            delivered.set()

        kwargs["handler"] = receive
        await subscribe(**kwargs)
        await asyncio.wait_for(delivered.wait(), timeout=5)
        raise SubscriptionProbeComplete()

    monkeypatch.setattr(receiver, "subscribe_route", subscribe_with_startup_delivery)
    agent._ensure_nats_transport_adapter = lambda: receiver
    agent._nats_mapper = lambda: mapper
    try:
        await publisher.connect(reconcile_topology=True)
        await publisher.publish(
            envelope=schedule_envelope(route),
            routing_key=route,
            options=TransportPublishOptions(),
        )
        with pytest.raises(SubscriptionProbeComplete):
            await agent._connect_nats()
        assert "Exception processing NATS delivery" not in caplog.text
        reopened = ScheduleStore(db_path=agent.task_manager._db_path)
        assert reopened.get_scheduled_message("startup_ensure") is not None
        assert agent._scheduler_wake.is_set()
    finally:
        await receiver.close()
        if publisher.connected:
            for info in await publisher._js.streams_info():
                if any(
                    str(subject).startswith(mapper.prefix + ".")
                    for subject in info.config.subjects or []
                ):
                    await publisher._js.delete_stream(info.config.name)
        await publisher.close()
