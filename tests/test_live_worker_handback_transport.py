"""Live NATS guarantees the lease-loss wind-down relies on.

A winding-down worker leaves its named-type queue group without deleting the
shared consumer, so the remaining workers keep receiving delegated work. A
handback published twice with the same id reaches the lead once.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.contracts.worker_handback_v1 import (
    HandedBackTaskV1,
    WorkerLeaseLossEvidenceV1,
    WorkerTaskHandbackV1,
    handback_identity,
)
from custom_messages.enums import MessageMetaType
from custom_transport.base import TransportPublishOptions
from custom_transport.base import FetchPolicy
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_transport.subjects import NatsSubjectMapper
from id_generation import new_id
from live_nats_client import nats_transport_config_from_env

pytestmark = [pytest.mark.asyncio, pytest.mark.live, pytest.mark.nats]

WORKER_TYPE = "WebResearchAgent"


def _work(task_id: str, route: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(task_id=task_id, task_type=MessageMetaType.REQUEST),
        msg_header=MessageHeader(from_agent_id="lead", to_routing_path=route),
        msg_meta=MessageMeta(),
    )


async def _subscribe_worker(adapter, mapper, name, route, received):
    async def handler(delivery):
        received.append(delivery.envelope.msg_content.task_id)
        await delivery.ack()

    await adapter.subscribe_route(
        name=name,
        routing_key=route,
        handler=handler,
        durable_name=mapper.durable_name(f"workers_{WORKER_TYPE}"),
        consumer_role="named_worker",
        owner_agent_id=name,
        fetch_policy=FetchPolicy.WHEN_READY,
        ready=lambda: True,
    )


async def _wait_until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.05)


async def test_leaving_the_worker_queue_keeps_the_shared_consumer_for_other_workers():
    mapper = NatsSubjectMapper(team_id=new_id("handback"))
    route = f"{mapper.team_id}_{WORKER_TYPE}"
    config = nats_transport_config_from_env()
    leaving = NatsTransportAdapter(config=config, mapper=mapper)
    staying = NatsTransportAdapter(config=config, mapper=mapper)
    publisher = NatsTransportAdapter(config=config, mapper=mapper)
    leaving_received: list[str] = []
    staying_received: list[str] = []
    await leaving.connect(reconcile_topology=True)
    await staying.connect(reconcile_topology=True)
    await publisher.connect(reconcile_topology=True)
    try:
        await _subscribe_worker(leaving, mapper, "worker_a", route, leaving_received)
        await _subscribe_worker(staying, mapper, "worker_b", route, staying_received)

        assert await leaving.unsubscribe_consumer_role("named_worker") == 1
        for index in range(5):
            await publisher.publish(envelope=_work(f"after_{index}", route), routing_key=route)
        await _wait_until(lambda: len(staying_received) == 5)

        assert leaving_received == []
        assert sorted(staying_received) == [f"after_{index}" for index in range(5)]
    finally:
        await publisher.close()
        await staying.close()
        await leaving.close()


async def test_a_resent_handback_reaches_the_lead_once():
    mapper = NatsSubjectMapper(team_id=new_id("handback"))
    lead_route = f"{mapper.team_id}_TeamArchitect"
    config = nats_transport_config_from_env()
    lead = NatsTransportAdapter(config=config, mapper=mapper)
    worker = NatsTransportAdapter(config=config, mapper=mapper)
    received: list[WorkerTaskHandbackV1] = []

    async def lead_handler(delivery):
        received.append(delivery.envelope.msg_content)
        await delivery.ack()

    handback_id = handback_identity(worker_agent_id="worker_a", launch_id="launch_a", task_id="t1")
    handback = WorkerTaskHandbackV1(
        handback_id=handback_id,
        team_id=mapper.team_id,
        worker_agent_id="worker_a",
        worker_named_agent_type=WORKER_TYPE,
        evidence=WorkerLeaseLossEvidenceV1(
            launch_id="launch_a",
            lease_lost_at_utc=datetime.now(timezone.utc),
            reason="stale_launch_lease",
        ),
        handed_back=HandedBackTaskV1(
            task_id="t1",
            disposition="interrupted",
            prior_state="in_progress",
            task=Task(task_id="t1", task_type=MessageMetaType.REQUEST),
            header=MessageHeader(),
            meta=MessageMeta(),
        ),
    )
    await lead.connect(reconcile_topology=True)
    await worker.connect(reconcile_topology=True)
    try:
        await lead.subscribe_route(
            name="lead",
            routing_key=lead_route,
            handler=lead_handler,
            durable_name=mapper.durable_name("type_TeamArchitect"),
        )
        for _attempt in range(2):
            # Each retry builds a new envelope (new message id); only the
            # handback id is stable, as in the wind-down publish loop.
            envelope = MessageEnvelope(
                msg_content=handback,
                msg_header=MessageHeader(
                    from_agent_id="worker_a",
                    to_routing_path=lead_route,
                    idempotency_key=handback_id,
                ),
                msg_meta=MessageMeta(),
            )
            await worker.publish(
                envelope=envelope,
                routing_key=lead_route,
                options=TransportPublishOptions(durable=True, idempotency_key=handback_id),
            )
        await _wait_until(lambda: len(received) == 1)
        await asyncio.sleep(1.0)

        assert [item.handback_id for item in received] == [handback_id]
    finally:
        await worker.close()
        await lead.close()


async def test_returned_task_reaches_another_worker_with_its_notice(tmp_path, monkeypatch):
    """Lead handler -> shared type queue -> a different worker, over real NATS."""

    import logging
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import custom_agents.worker_handback_runtime as runtime
    from custom_agents.task_manager import TaskManager
    from custom_messages.contracts.redispatch_v1 import DispatchedActionNoteV1

    team_id = new_id("handback")
    route = f"{team_id}_{WORKER_TYPE}"
    # Like the runtime, both sides know which route is the shared type queue.
    mapper = NatsSubjectMapper(
        team_id=team_id, named_agent_type_queue_map={WORKER_TYPE: route}
    )
    # JetStream dedupes Nats-Msg-Id per stream, so identities must be unique per run.
    worker_a = new_id("agent")
    launch_a = new_id("launch")
    config = nats_transport_config_from_env()
    lead_adapter = NatsTransportAdapter(config=config, mapper=mapper)
    worker_adapter = NatsTransportAdapter(config=config, mapper=mapper)
    received: list[MessageEnvelope] = []

    async def worker_handler(delivery):
        received.append(delivery.envelope)
        await delivery.ack()

    class _LeadComms:
        async def send_message(self, *, message, routing_key):
            await lead_adapter.publish(
                envelope=message,
                routing_key=routing_key,
                options=TransportPublishOptions(
                    durable=True, idempotency_key=message.msg_header.idempotency_key
                ),
            )

    manager = TaskManager(db_path=str(tmp_path / "lead.db"))
    manager.set_owner_identity("lead", "Lead", mapper.team_id, "TeamArchitect")
    monkeypatch.setattr(
        runtime,
        "AgentLaunchRepository",
        lambda _settings: SimpleNamespace(
            get=lambda launch_id, team_id: SimpleNamespace(
                agent_id=worker_a, owner_canonical_user_id="alice"
            )
        ),
    )
    monkeypatch.setattr(runtime, "interruption_limit", lambda agent: 3)
    lead = SimpleNamespace(
        id="lead",
        team_id=mapper.team_id,
        is_team_lead=True,
        task_manager=manager,
        team_comms=_LeadComms(),
        named_agent_type_queue_map={WORKER_TYPE: route},
        _logger=logging.getLogger("live.redispatch"),
        # A replacement worker is already consuming the queue.
        _probe_live_named_agent_type=AsyncMock(return_value=object()),
        _schedule_store=None,
    )
    note = DispatchedActionNoteV1(
        action_id="a1", action_type="notify_user", status="dispatched", summary="Sent draft"
    )
    handback = WorkerTaskHandbackV1(
        handback_id=handback_identity(worker_agent_id=worker_a, launch_id=launch_a, task_id="t1"),
        team_id=mapper.team_id,
        worker_agent_id=worker_a,
        worker_named_agent_type=WORKER_TYPE,
        evidence=WorkerLeaseLossEvidenceV1(
            launch_id=launch_a,
            lease_lost_at_utc=datetime.now(timezone.utc),
            reason="stale_launch_lease",
        ),
        handed_back=HandedBackTaskV1(
            task_id="t1",
            disposition="interrupted",
            prior_state="in_progress",
            task=Task(task_id="t1", task_type=MessageMetaType.REQUEST, problem_statement="report"),
            header=MessageHeader(
                from_agent_id="gateway", to_routing_path=worker_a, response_routing_key="gateway"
            ),
            meta=MessageMeta(),
            dispatched_actions=(note,),
        ),
    )
    await lead_adapter.connect(reconcile_topology=True)
    await worker_adapter.connect(reconcile_topology=True)
    try:
        await worker_adapter.subscribe_route(
            name="worker_b",
            routing_key=route,
            handler=worker_handler,
            durable_name=mapper.durable_name(f"workers_{WORKER_TYPE}"),
            consumer_role="named_worker",
            fetch_policy=FetchPolicy.WHEN_READY,
            ready=lambda: True,
        )
        assert await runtime.handle_worker_task_handback(
            lead,
            handback=handback,
            msg_header=MessageHeader(from_agent_id=worker_a),
            msg_meta=MessageMeta(),
        )
        # A redelivered handback must not send the task out a second time.
        await runtime.handle_worker_task_handback(
            lead,
            handback=handback,
            msg_header=MessageHeader(from_agent_id=worker_a),
            msg_meta=MessageMeta(),
        )
        await _wait_until(lambda: len(received) == 1)
        await asyncio.sleep(1.0)

        [envelope] = received
        assert envelope.msg_content.task_id == "t1"
        assert envelope.msg_header.response_routing_key == "gateway"
        notice = envelope.msg_meta.redispatch_notice
        assert notice.prior_disposition == "interrupted"
        assert notice.dispatched_actions == (note,)
    finally:
        await worker_adapter.close()
        await lead_adapter.close()
