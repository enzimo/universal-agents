"""Live NATS/JetStream integration tests for structured agent messaging."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from unittest.mock import MagicMock, patch

import pytest

from custom_agents.task_manager import TaskManager

from custom_agents.structured_llm_messages import (
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TaskResponseAction,
)
from custom_messages.contracts.access_v2 import AccessRequestResolvedV1
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from custom_transport.base import SubscriptionDelivery
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_transport.subjects import NatsSubjectMapper
from live_nats_client import (
    LiveNatsClient,
    nats_transport_config_from_env,
    resolve_live_transport_settings,
)
from runtime_settings.projection import _project_transport
from id_generation import new_id, new_id_suffix


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.nats,
]


class FakeAgentResult:
    def __init__(self, structured_output: StructuredAgentTurnDecision):
        self.structured_output = structured_output

    def __str__(self) -> str:
        return self.structured_output.model_dump_json()


class FakeStrandsAgent:
    def __init__(self, *responses: StructuredAgentTurnDecision):
        self._responses = list(responses)
        self.messages = []

    async def invoke_async(self, prompt: str, **_: object) -> FakeAgentResult:
        if not self._responses:
            raise AssertionError("FakeStrandsAgent received more invocations than expected.")
        return FakeAgentResult(self._responses.pop(0))


def _response(answer: str) -> StructuredAgentTurnDecision:
    return StructuredAgentTurnDecision(
        actions=[
            TaskResponseAction(
                task=StructuredTaskPayload(task_type=MessageMetaType.RESPONSE, answer=answer),
            )
        ],
    )


async def test_can_connect_to_nats_server() -> None:
    team_id = new_id("nats_conn")
    adapter = NatsTransportAdapter(
        config=nats_transport_config_from_env(),
        mapper=NatsSubjectMapper(team_id=team_id),
    )
    await adapter.connect(reconcile_topology=True)
    try:
        assert adapter.connected is True
    finally:
        await adapter.close()


async def test_can_publish_and_consume_direct_task_message() -> None:
    team_id = new_id("nats_direct")
    route = new_id("agent")
    async with LiveNatsClient(team_id) as client:
        queue = await client.subscribe_route(route)
        envelope = MessageEnvelope(
            msg_content=Task(task_type=MessageMetaType.REQUEST, problem_statement="Integration test task"),
            msg_meta=MessageMeta(from_agent_name="TestAgent"),
            msg_header=MessageHeader(from_agent_id="test_agent_001", to_routing_path=route, agent_signature=""),
        )

        await client.publish(route, envelope)
        delivered = await queue.get(timeout=5)

        assert isinstance(delivered.msg_content, Task)
        assert delivered.msg_content.problem_statement == "Integration test task"
        assert delivered.msg_header.to_routing_path == route


async def test_access_decision_wakes_the_requester_durably() -> None:
    """The gateway's "request decided" message reaches the waiting agent's route."""

    team_id = new_id("nats_awake")
    requester = new_id("agent")
    async with LiveNatsClient(team_id) as client:
        queue = await client.subscribe_route(requester)
        resolved = AccessRequestResolvedV1(
            message_id=new_id("access_resolved"),
            team_id=team_id,
            request_id=new_id("access_request"),
        )
        envelope = MessageEnvelope(
            msg_content=resolved,
            msg_meta=MessageMeta(
                from_agent_name="gateway",
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=False,
                msg_correlation_id=new_id("corr"),
            ),
            msg_header=MessageHeader(
                from_agent_id="gateway_queue",
                to_routing_path=requester,
                idempotency_key=resolved.message_id,
                agent_signature="",
            ),
        )

        await client.publish(requester, envelope, durable=True)
        delivered = await queue.get(timeout=5)

        assert isinstance(delivered.msg_content, AccessRequestResolvedV1)
        assert delivered.msg_content.request_id == resolved.request_id
        assert delivered.msg_header.to_routing_path == requester


async def test_nats_object_store_round_trips_attachment_bytes(tmp_path) -> None:
    team_id = new_id("nats_object")
    bucket = new_id("ua_attach_live")
    object_name = f"{uuid.uuid4().hex}/sample.txt"
    source = tmp_path / "sample.txt"
    destination = tmp_path / "downloaded.txt"
    source.write_text("live object store attachment bytes\n", encoding="utf-8")
    adapter = NatsTransportAdapter(
        config=nats_transport_config_from_env(),
        mapper=NatsSubjectMapper(team_id=team_id),
    )
    await adapter.connect(reconcile_topology=True)
    try:
        await adapter.put_object(
            bucket=bucket,
            object_name=object_name,
            path=str(source),
            ttl_seconds=60,
            max_bytes=1024 * 1024,
            headers={"attachment_id": "live-test-attachment"},
        )
        await adapter.get_object(bucket=bucket, object_name=object_name, destination_path=str(destination))

        assert destination.read_bytes() == source.read_bytes()
    finally:
        if getattr(adapter, "_js", None) is not None:
            with contextlib.suppress(Exception):
                await adapter._js.delete_object_store(bucket)
        await adapter.close()


async def test_core_operational_fanout_is_not_replayed() -> None:
    team_id = new_id("nats_ops")
    mapper = NatsSubjectMapper(team_id=team_id)
    async with LiveNatsClient(team_id) as client:
        envelope = MessageEnvelope(
            msg_content=Task(task_type=MessageMetaType.REQUEST, problem_statement="live only"),
            msg_meta=MessageMeta(from_agent_name="TestAgent"),
            msg_header=MessageHeader(
                from_agent_id="test_agent_001",
                to_routing_path="__all_agents__",
                agent_signature="",
            ),
        )
        await client.publish("__all_agents__", envelope, operational=True, durable=False)

        received = asyncio.Queue()

        async def handler(delivery) -> None:
            await received.put(delivery.envelope)
            await delivery.ack()

        await client.adapter.subscribe_subject(
            name="late-operational-subscriber",
            subject=mapper.operational_subject(),
            handler=handler,
            delivery=SubscriptionDelivery.CORE,
        )

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(received.get(), timeout=0.5)


async def test_named_type_work_is_buffered_until_worker_subscription() -> None:
    team_id = new_id("nats_named")
    named_queue = f"{team_id}_CodeWritingAgent"
    async with LiveNatsClient(team_id) as client:
        client.mapper = NatsSubjectMapper(
            team_id=team_id,
            named_agent_type_queue_map={"CodeWritingAgent": named_queue},
        )
        client.adapter.mapper = client.mapper
        envelope = MessageEnvelope(
            msg_content=Task(task_type=MessageMetaType.REQUEST, problem_statement="buffered work"),
            msg_meta=MessageMeta(from_agent_name="TestAgent", to_named_agent_type="CodeWritingAgent"),
            msg_header=MessageHeader(from_agent_id="test_agent_001", to_routing_path=named_queue, agent_signature=""),
        )

        await client.publish(named_queue, envelope)
        queue = await client.subscribe_route(named_queue)
        delivered = await queue.get(timeout=5)

        assert isinstance(delivered.msg_content, Task)
        assert delivered.msg_content.problem_statement == "buffered work"


async def test_universal_agent_receives_nats_task_and_replies(tmp_path) -> None:
    from custom_agents.strands_agent import UniversalAgent

    team_id = new_id("nats_agent")
    response_route = f"{team_id}_gateway_user_{new_id_suffix()}_response"
    fake_model = FakeStrandsAgent(_response("nats agent ok"))
    agent_task = None
    transport = resolve_live_transport_settings()

    async with LiveNatsClient(team_id, transport=transport) as client:
        response_queue = await client.subscribe_route(response_route)
        with patch("custom_agents.strands_agent.Agent", return_value=fake_model), patch(
            "custom_agents.strands_agent.UniversalAgent._configure_model",
            return_value=MagicMock(),
        ):
            agent = UniversalAgent(
                **_project_transport(transport_cfg=transport.model_dump()),
                role="NATS integration agent",
                tools=[],
                team_id=team_id,
                named_agent_type="TeamArchitect",
                is_team_lead=True,
                skills_enabled=False,
                task_manager=TaskManager(db_path=str(tmp_path / "nats-agent.db")),
            )
            try:
                assert agent.task_manager._db_path == str(tmp_path / "nats-agent.db")
                agent_task = asyncio.create_task(agent.connect())
                deadline = asyncio.get_running_loop().time() + 10
                while asyncio.get_running_loop().time() < deadline:
                    if getattr(agent, "_operational_exchange_name", None):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("Agent did not finish NATS setup.")

                envelope = MessageEnvelope(
                    msg_content=Task(
                        task_id=new_id("task"),
                        task_type=MessageMetaType.REQUEST,
                        problem_statement="Reply with nats agent ok.",
                    ),
                    msg_meta=MessageMeta(
                        from_agent_name="live-nats-test",
                        requester_type=RequesterType.USER_REQUESTER,
                        is_response_requested=True,
                        msg_correlation_id=new_id("corr"),
                    ),
                    msg_header=MessageHeader(
                        from_agent_id="live-nats-test",
                        to_routing_path=agent.id,
                        response_routing_key=response_route,
                        agent_signature="",
                    ),
                )
                await client.publish(agent.id, envelope, durable=True)
                response = await response_queue.get(timeout=20)

                assert isinstance(response.msg_content, Task)
                assert response.msg_content.task_type == MessageMetaType.RESPONSE
                assert response.msg_content.answer == "nats agent ok"
            finally:
                if agent_task is not None:
                    agent.set_exit_flag()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(agent_task, timeout=10)
