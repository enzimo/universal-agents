"""A contract drain wins before or during a shared work subscription."""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_agents.contract_drain import ContractDrain, drain_agent
from custom_agents.strands_agent import UniversalAgent
from custom_transport.base import FetchPolicy
from custom_transport.nats_adapter import NAMED_WORKER_CONSUMER_ROLE


@pytest.mark.asyncio
@pytest.mark.parametrize("during_subscribe", [False, True])
async def test_a_starting_agent_cannot_join_shared_work_after_its_drain(during_subscribe):
    active, subscribed = [], []

    async def leave(role):
        count = active.count(role)
        active[:] = [item for item in active if item != role]
        return count

    async def subscribe(**kwargs):
        subscribed.append(kwargs)
        assert kwargs["ready"]()
        await drain_agent(agent, "Another message contract")
        assert not kwargs["ready"]()
        # Simulate a subscription whose setup completes after the first drain.
        active.append(kwargs["consumer_role"])

    adapter = SimpleNamespace(subscribe_subject=subscribe, unsubscribe_consumer_role=leave,
                              worker_queue_durable_name=lambda name: "worker_" + name)
    mapper = SimpleNamespace(named_type_work_subject=lambda name: "team.type." + name)
    agent = SimpleNamespace(id="worker", team_id="team", named_agent_type="Researcher",
                            named_agent_type_queue_map={}, on_nats_delivery=lambda delivery: None,
                            _nats_mapper=lambda: mapper, _ensure_nats_transport_adapter=lambda: adapter,
                            _logger=logging.getLogger(__name__), _should_exit=asyncio.Event())
    if not during_subscribe:
        agent._contract_drain = ContractDrain("Another message contract", datetime.now(timezone.utc))
    await UniversalAgent._subscribe_type_queue(agent, adapter, policy=FetchPolicy.WHEN_READY, ready=lambda: True)
    assert not active
    assert len(subscribed) == int(during_subscribe)
    if during_subscribe:
        assert agent._contract_drain.left_queues == 1
        assert subscribed[0]["consumer_role"] == NAMED_WORKER_CONSUMER_ROLE
