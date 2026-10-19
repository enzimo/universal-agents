"""Real NATS fanout with sibling workers sharing a request correlation id."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from live_nats_client import LiveNatsClient

from custom_agents.strands_agent import UniversalAgent
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import handle_task_steering_message
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, RoutingType, TaskSteeringType
from custom_transport.base import SubscriptionDelivery
from id_generation import new_id


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.nats,
]


async def test_live_targeted_steering_is_accepted_only_by_worker_holding_subtask(
    tmp_path,
):
    team_id = new_id("steering_team")
    workers = []
    received = [asyncio.Event(), asyncio.Event()]
    for index in range(2):
        task_id = f"child_{index}"
        manager = TaskManager(db_path=str(tmp_path / f"worker_{index}.db"))
        manager.set_owner_identity(
            f"worker_{index}", f"Worker {index}", team_id, "CodeWritingAgent"
        )
        manager.add_task(
            Task(
                task_id=task_id,
                parent_task_id="parent",
                task_type=MessageMetaType.REQUEST,
                problem_statement="Research one part",
            ),
            MessageHeader(from_agent_id="lead"),
            MessageMeta(msg_correlation_id="shared_request"),
            owner_person_id=None,
        )
        workers.append(
            SimpleNamespace(
                id=f"worker_{index}",
                name=f"Worker {index}",
                named_agent_type="CodeWritingAgent",
                team_id=team_id,
                task_manager=manager,
                mid_turn_steering={"enabled": True},
                _pending_proactive_task_steering={},
                _processed_task_steering_ids=set(),
                _logger=logging.getLogger(__name__),
            )
        )

    async with LiveNatsClient(team_id) as client:
        for index, worker in enumerate(workers):

            async def handle(delivery, worker=worker, event=received[index]):
                envelope = delivery.envelope
                try:
                    if UniversalAgent._operational_target_matches(
                        worker,
                        msg_content=envelope.msg_content,
                        msg_header=envelope.msg_header,
                        msg_meta=envelope.msg_meta,
                    ):
                        await handle_task_steering_message(
                            worker,
                            envelope.msg_content,
                            envelope.msg_header,
                            envelope.msg_meta,
                        )
                    await delivery.ack()
                finally:
                    event.set()

            await client.adapter.subscribe_subject(
                name=worker.id,
                subject=client.mapper.operational_subject(),
                handler=handle,
                delivery=SubscriptionDelivery.CORE,
            )
        guidance = TaskSteering(
            steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
            target_task_id="child_0",
            target_correlation_id="shared_request",
            target_named_agent_type="CodeWritingAgent",
            guidance_text="Use the staging account.",
        )
        await client.publish(
            RoutingType.ALL_AGENTS_ROUTE.value,
            MessageEnvelope(
                msg_content=guidance,
                msg_header=MessageHeader(
                    from_agent_id="lead", to_routing_path=RoutingType.ALL_AGENTS_ROUTE
                ),
                msg_meta=MessageMeta(
                    to_named_agent_type="CodeWritingAgent", is_response_requested=False
                ),
            ),
            operational=True,
            durable=False,
        )
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in received)), timeout=10
        )

    assert (
        workers[0]._pending_proactive_task_steering["child_0"][0].guidance_text
        == guidance.guidance_text
    )
    assert workers[1]._pending_proactive_task_steering == {}
    assert (
        guidance.guidance_text
        not in workers[1].task_manager.get_task_info("child_1").task.model_dump_json()
    )
