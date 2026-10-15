"""Core NATS status replies must remain available throughout an approval resume."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    RoutingType,
    TaskSteeringType,
)
from custom_transport.base import SubscriptionDelivery, TransportPublishOptions
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_transport.subjects import NatsSubjectMapper
from tests.live_nats_client import nats_transport_config_from_env
from tests.test_turn_action_outbox import _make_agent, _make_task_manager


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.integration
async def test_status_reply_while_approval_resumes_a_long_turn(tmp_path):
    team = "team_" + uuid4().hex
    agents = []
    adapters = []
    release = asyncio.Event()
    entered = asyncio.Event()
    try:
        for name in ("requester", "worker"):
            manager = _make_task_manager(tmp_path, owner_id=name)
            agent = _make_agent(tmp_path, task_manager=manager)
            agent.team_id = team
            agent.name = name
            agent.subtask_status_probe_timeout_seconds = 0.5
            adapter = NatsTransportAdapter(
                config=nats_transport_config_from_env(),
                mapper=NatsSubjectMapper(team_id=team),
            )
            await adapter.connect(reconcile_topology=True)
            adapters.append(adapter)
            object.__setattr__(
                agent, "_ensure_nats_transport_adapter", lambda adapter=adapter: adapter
            )
            object.__setattr__(
                agent,
                "_operational_exchange_name",
                adapter.mapper.operational_subject(),
            )
            await adapter.subscribe_subject(
                name=name,
                subject=adapter.mapper.operational_subject(),
                handler=agent.on_nats_operational_delivery,
                delivery=SubscriptionDelivery.CORE,
                logical_route=RoutingType.ALL_AGENTS_ROUTE.value,
            )
            agents.append(agent)
        requester, worker = agents
        task = Task(task_id="delegated_task", task_type=MessageMetaType.REQUEST)
        worker.task_manager.add_task(
            task, MessageHeader(from_agent_id=requester.id), MessageMeta(),
            owner_person_id=None,
        )
        worker.task_manager.update_main_task_state(
            task.task_id, ProgressState.WAITING, datetime.now(timezone.utc).isoformat()
        )
        worker._interrupted_turn_sessions[task.task_id] = SimpleNamespace()

        async def held_resume(task_id):
            worker._pending_interrupt_responses.pop(task_id, None)
            entered.set()
            await release.wait()

        object.__setattr__(worker, "_resume_interrupted_task_turn", held_resume)
        root = Task(task_id="root", task_type=MessageMetaType.REQUEST)
        requester.task_manager.add_task(
            root, MessageHeader(from_agent_id="gateway"), MessageMeta(),
            owner_person_id=None,
        )
        child = requester.task_manager.add_subtask(
            root.task_id,
            task,
            MessageHeader(to_routing_path=worker.id),
            MessageMeta(
                to_agent_name=worker.name, to_named_agent_type=worker.named_agent_type
            ),
        )
        before = await requester._probe_subtask_task_status(subtask_info=child)
        assert before is not None and before.is_known_task
        await adapters[0].publish(
            envelope=MessageEnvelope(
                msg_content=TaskSteering(
                    steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
                    target_task_id=task.task_id,
                    target_agent_id=worker.id,
                    interrupt_responses=[
                        {"interrupt_id": "approval", "response": "approved"}
                    ],
                ),
                msg_header=MessageHeader(
                    from_agent_id=requester.id, to_routing_path=worker.id
                ),
                msg_meta=MessageMeta(),
            ),
            routing_key=worker.id,
            options=TransportPublishOptions(durable=False, operational=True),
        )
        await asyncio.wait_for(entered.wait(), 3)
        after = await requester._probe_subtask_task_status(subtask_info=child)
        assert after is not None, "Approval resume blocked the operational subscription"
        assert after.is_known_task and after.responder_agent_id == worker.id
    finally:
        release.set()
        for agent in agents:
            continuation = getattr(agent, "_pending_work_task", None)
            if continuation:
                await asyncio.wait_for(continuation, 3)
        for adapter in reversed(adapters):
            await adapter.close()
