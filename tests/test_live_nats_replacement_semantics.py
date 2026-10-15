"""Live NATS/JetStream replacement-semantics checks.

These tests are intentionally deterministic and avoid LLM calls. They exercise
the broker contracts that the runtime relies on: durable work, queue
groups, redelivery, duplicate suppression, replayable presence, and reconnect
behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

from custom_messages.contracts.control_v1 import (
    AgentStatus,
    ControlInstruction,
    Instruction,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import ControlType, MessageMetaType, RoutingType
from custom_messages.enums import TaskSteeringStatus, TaskSteeringType
from custom_messages.skill_catalog import ManagedSkillCatalogChangedV1
from custom_transport.base import (
    ConsumerScope,
    FetchPolicy,
    InboundDelivery,
    SubscriptionDelivery,
    TransportPublishOptions,
)
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_transport.subjects import NatsSubjectMapper
from live_nats_client import nats_transport_config_from_env
from id_generation import new_id, new_id_suffix
from scripts.reconcile_nats_consumers import reconcile_nats_consumers


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.nats,
]


def _team(prefix: str) -> str:
    return new_id(prefix)


def _task(
    route: str, text: str, *, task_id: str | None = None, named_type: str | None = None
) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(
            task_id=task_id or new_id("task"),
            task_type=MessageMetaType.REQUEST,
            problem_statement=text,
        ),
        msg_meta=MessageMeta(
            from_agent_name="live-nats-semantics",
            to_named_agent_type=named_type,
            msg_correlation_id=new_id("corr"),
            is_response_requested=True,
        ),
        msg_header=MessageHeader(
            from_agent_id="live-nats-semantics",
            to_routing_path=route,
            response_routing_key=f"{route}_response",
            agent_signature="",
        ),
    )


def _control(route: str, instruction: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=ControlInstruction(
            type=ControlType.CONTROL_MSG,
            instruction=Instruction(instruction=instruction),
        ),
        msg_meta=MessageMeta(from_agent_name="live-nats-semantics"),
        msg_header=MessageHeader(
            from_agent_id="live-nats-semantics",
            to_routing_path=route,
            agent_signature="",
        ),
    )


def _status(agent_id: str, team_id: str, *, exiting: bool = False) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=AgentStatus(
            self_id=agent_id,
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
            team_id=team_id,
            named_agent_type="TeamArchitect",
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=exiting,
            name=agent_id,
            agent_role="manager",
        ),
        msg_meta=MessageMeta(from_agent_name=agent_id),
        msg_header=MessageHeader(
            from_agent_id=agent_id,
            to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
            agent_signature="",
        ),
    )


def _steering(
    route: str,
    steering_type: TaskSteeringType,
    *,
    text: str,
    task_id: str = "task_live_steering",
) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=TaskSteering(
            steering_type=steering_type,
            target_task_id=task_id,
            target_correlation_id="corr_live_steering",
            target_agent_id=route,
            guidance_text=text,
            interrupt_responses=(
                [{"interrupt_id": "interrupt_1", "response": text}]
                if steering_type == TaskSteeringType.INTERRUPT_RESPONSE
                else []
            ),
            status=TaskSteeringStatus.PENDING,
            idempotency_key=f"steering_{steering_type.value}_{task_id}",
        ),
        msg_meta=MessageMeta(
            from_agent_name="live-nats-semantics",
            msg_correlation_id=new_id("corr"),
        ),
        msg_header=MessageHeader(
            from_agent_id="gateway",
            to_routing_path=route,
            response_routing_key=f"{route}_response",
            agent_signature="",
        ),
    )


def _adapter(
    team_id: str,
    *,
    mapper: NatsSubjectMapper | None = None,
    ack_wait_seconds: float = 2.0,
    max_deliver: int = 5,
    duplicate_window_seconds: float = 30.0,
    work_max_age_seconds: float | None = 60.0,
    process_consumer_inactive_threshold_seconds: float = 300.0,
    name: str | None = None,
) -> NatsTransportAdapter:
    return NatsTransportAdapter(
        config=nats_transport_config_from_env(
            name=name or f"live-nats-semantics-{new_id_suffix()}",
            ack_wait_seconds=ack_wait_seconds,
            # Worker queues use their own wait; keep both short in these tests.
            pool_ack_wait_seconds=ack_wait_seconds,
            max_deliver=max_deliver,
            duplicate_window_seconds=duplicate_window_seconds,
            work_max_age_seconds=work_max_age_seconds,
            process_consumer_inactive_threshold_seconds=(
                process_consumer_inactive_threshold_seconds
            ),
        ),
        mapper=mapper or NatsSubjectMapper(team_id=team_id),
    )


async def _subscribe_queue(
    adapter: NatsTransportAdapter,
    *,
    name: str,
    routing_key: str | None = None,
    subject: str | None = None,
    durable_name: str | None = None,
    worker_queue: bool = False,
    durable: bool = True,
    consumer_scope: ConsumerScope = ConsumerScope.ROUTE,
    consumer_role: str = "test",
    owner_agent_id: str | None = None,
    deliver_last_per_subject: bool = False,
) -> asyncio.Queue[InboundDelivery]:
    queue: asyncio.Queue[InboundDelivery] = asyncio.Queue()

    async def handler(delivery: InboundDelivery) -> None:
        await queue.put(delivery)

    if subject is not None:
        await adapter.subscribe_subject(
            name=name,
            subject=subject,
            handler=handler,
            durable_name=durable_name,
            delivery=(
                SubscriptionDelivery.JETSTREAM_DURABLE
                if durable
                else SubscriptionDelivery.CORE
            ),
            consumer_scope=consumer_scope,
            consumer_role=consumer_role,
            owner_agent_id=owner_agent_id,
            deliver_last_per_subject=deliver_last_per_subject,
        )
    elif routing_key is not None:
        await adapter.subscribe_route(
            name=name,
            routing_key=routing_key,
            handler=handler,
            durable_name=durable_name,
            consumer_scope=consumer_scope,
            fetch_policy=FetchPolicy.WHEN_READY if worker_queue else FetchPolicy.CONTINUOUS,
            ready=(lambda: True) if worker_queue else None,
            consumer_role=consumer_role,
            owner_agent_id=owner_agent_id,
            deliver_last_per_subject=deliver_last_per_subject,
        )
    else:  # pragma: no cover - helper misuse
        raise ValueError("routing_key or subject is required")
    return queue


async def _publish(
    adapter: NatsTransportAdapter,
    routing_key: str,
    envelope: MessageEnvelope,
    *,
    durable: bool = True,
    broadcast: bool = False,
    presence: bool = False,
    operational: bool = False,
    idempotency_key: str | None = None,
) -> None:
    await adapter.publish(
        envelope=envelope,
        routing_key=routing_key,
        options=TransportPublishOptions(
            durable=durable,
            broadcast=broadcast,
            presence=presence,
            operational=operational,
            idempotency_key=idempotency_key,
        ),
    )


async def _wait_no_delivery(
    queue: asyncio.Queue[InboundDelivery], *, timeout: float = 0.5
) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=timeout)


async def _flush(adapter: NatsTransportAdapter) -> None:
    if adapter._nc is not None:
        await adapter._nc.flush()


async def test_live_redelivery_moves_unacked_pool_work_to_another_worker() -> None:
    team_id = _team("nats_redeliver")
    named_type = "CodeWritingAgent"
    named_queue = f"{team_id}_{named_type}"
    mapper = NatsSubjectMapper(
        team_id=team_id, named_agent_type_queue_map={named_type: named_queue}
    )
    queue_group = mapper.durable_name(f"workers_{named_type}")
    first = _adapter(
        team_id,
        mapper=mapper,
        ack_wait_seconds=0.75,
        max_deliver=3,
        name="redelivery-first",
    )
    publisher = _adapter(
        team_id,
        mapper=mapper,
        ack_wait_seconds=0.75,
        max_deliver=3,
        name="redelivery-publisher",
    )
    try:
        await first.connect(reconcile_topology=True)
        first_queue = await _subscribe_queue(
            first,
            name="first-worker",
            routing_key=named_queue,
            durable_name=queue_group,
            worker_queue=True,
        )
        await publisher.connect()
        await _publish(
            publisher,
            named_queue,
            _task(named_queue, "redeliver me", named_type=named_type),
        )
        first_delivery = await asyncio.wait_for(first_queue.get(), timeout=5)
        assert first_delivery.redelivery_count == 0
        await first.close()

        second = _adapter(
            team_id,
            mapper=mapper,
            ack_wait_seconds=0.75,
            max_deliver=3,
            name="redelivery-second",
        )
        try:
            await second.connect()
            second_queue = await _subscribe_queue(
                second,
                name="second-worker",
                routing_key=named_queue,
                durable_name=queue_group,
                worker_queue=True,
            )
            second_delivery = await asyncio.wait_for(second_queue.get(), timeout=5)
            await second_delivery.ack()
            assert (
                second_delivery.redelivery_count
                and second_delivery.redelivery_count >= 1
            )
            assert (
                second_delivery.envelope.msg_content.problem_statement == "redeliver me"
            )
        finally:
            await second.close()
    finally:
        await publisher.close()
        with contextlib.suppress(Exception):
            await first.close()


async def test_live_in_progress_keepalive_prevents_ack_wait_redelivery() -> None:
    team_id = _team("nats_keepalive")
    route = new_id("agent")
    adapter = _adapter(team_id, ack_wait_seconds=0.75, max_deliver=3)
    deliveries: list[int | None] = []
    done = asyncio.Event()

    async def handler(delivery: InboundDelivery) -> None:
        deliveries.append(delivery.redelivery_count)
        for _ in range(4):
            await asyncio.sleep(0.3)
            await delivery.in_progress()
        await delivery.ack()
        done.set()

    try:
        await adapter.connect(reconcile_topology=True)
        await adapter.subscribe_route(
            name="keepalive-worker",
            routing_key=route,
            handler=handler,
            durable_name=adapter.mapper.durable_name(f"agent_{route}"),
        )
        await _publish(adapter, route, _task(route, "long but alive"))
        await asyncio.wait_for(done.wait(), timeout=5)
        await asyncio.sleep(1.0)
        assert deliveries == [0]
    finally:
        await adapter.close()


async def test_live_duplicate_nats_message_id_suppresses_duplicate_work() -> None:
    team_id = _team("nats_dedupe")
    route = new_id("agent")
    adapter = _adapter(team_id, duplicate_window_seconds=20.0)
    try:
        await adapter.connect(reconcile_topology=True)
        queue = await _subscribe_queue(
            adapter,
            name="dedupe-worker",
            routing_key=route,
            durable_name=adapter.mapper.durable_name(f"agent_{route}"),
        )
        envelope = _task(route, "dedupe me")
        idempotency_key = f"dedupe_{uuid.uuid4().hex}"
        await _publish(adapter, route, envelope, idempotency_key=idempotency_key)
        await _publish(adapter, route, envelope, idempotency_key=idempotency_key)
        delivered = await asyncio.wait_for(queue.get(), timeout=5)
        await delivered.ack()
        assert delivered.envelope.msg_content.problem_statement == "dedupe me"
        await _wait_no_delivery(queue, timeout=1.0)
    finally:
        await adapter.close()


async def test_live_gateway_response_route_replays_after_late_subscribe() -> None:
    team_id = _team("nats_gw_replay")
    response_route = f"{team_id}_gateway_user_{new_id_suffix()}_response"
    adapter = _adapter(team_id)
    try:
        await adapter.connect(reconcile_topology=True)
        response = MessageEnvelope(
            msg_content=Task(
                task_type=MessageMetaType.RESPONSE, answer="offline gateway replay ok"
            ),
            msg_meta=MessageMeta(
                from_agent_name="manager", msg_correlation_id=new_id("corr")
            ),
            msg_header=MessageHeader(
                from_agent_id="manager",
                to_routing_path=response_route,
                agent_signature="",
            ),
        )
        await _publish(adapter, response_route, response)
        queue = await _subscribe_queue(
            adapter,
            name="late-gateway",
            routing_key=response_route,
            durable_name=adapter.mapper.durable_name(f"gateway_{response_route}"),
        )
        delivered = await asyncio.wait_for(queue.get(), timeout=5)
        await delivered.ack()
        assert delivered.envelope.msg_content.answer == "offline gateway replay ok"
    finally:
        await adapter.close()


async def test_live_poison_message_reaches_max_deliver_and_can_be_terminated() -> None:
    team_id = _team("nats_poison")
    route = new_id("agent")
    adapter = _adapter(team_id, ack_wait_seconds=0.4, max_deliver=3)
    counts: list[int | None] = []
    done = asyncio.Event()

    async def handler(delivery: InboundDelivery) -> None:
        counts.append(delivery.redelivery_count)
        if delivery.redelivery_count is not None and delivery.redelivery_count >= 2:
            await delivery.term("intentional poison-message termination")
            done.set()
        else:
            await delivery.nak(delay_seconds=0.05)

    try:
        await adapter.connect(reconcile_topology=True)
        await adapter.subscribe_route(
            name="poison-worker",
            routing_key=route,
            handler=handler,
            durable_name=adapter.mapper.durable_name(f"agent_{route}"),
        )
        await _publish(adapter, route, _task(route, "poison"))
        await asyncio.wait_for(done.wait(), timeout=8)
        assert counts == [0, 1, 2]
    finally:
        await adapter.close()


async def test_live_work_max_age_expires_undelivered_work() -> None:
    team_id = _team("nats_expiry")
    route = new_id("agent")
    adapter = _adapter(team_id, work_max_age_seconds=1.0, duplicate_window_seconds=0.5)
    try:
        await adapter.connect(reconcile_topology=True)
        await _publish(adapter, route, _task(route, "expire before consumer"))
        await asyncio.sleep(2.0)
        queue = await _subscribe_queue(
            adapter,
            name="expired-worker",
            routing_key=route,
            durable_name=adapter.mapper.durable_name(f"agent_{route}"),
        )
        await _wait_no_delivery(queue, timeout=1.0)
    finally:
        await adapter.close()


async def test_live_broadcast_control_reaches_all_current_subscribers() -> None:
    team_id = _team("nats_broadcast")
    mapper = NatsSubjectMapper(team_id=team_id)
    adapters = [
        _adapter(team_id, mapper=mapper, name=f"broadcast-{idx}") for idx in range(3)
    ]
    try:
        queues = []
        for idx, adapter in enumerate(adapters):
            await adapter.connect(reconcile_topology=(idx == 0))
            queues.append(
                await _subscribe_queue(
                    adapter,
                    name=f"broadcast-sub-{idx}",
                    subject=mapper.broadcast_subject(),
                    durable=False,
                )
            )
        for adapter in adapters:
            await _flush(adapter)
        await _publish(
            adapters[0],
            RoutingType.ALL_AGENTS_ROUTE.value,
            _control(RoutingType.ALL_AGENTS_ROUTE.value, "pause"),
            durable=False,
            broadcast=True,
        )
        deliveries = [
            await asyncio.wait_for(queue.get(), timeout=5) for queue in queues
        ]
        for delivery in deliveries:
            await delivery.ack()
            assert isinstance(delivery.envelope.msg_content, ControlInstruction)
            assert delivery.envelope.msg_content.instruction.instruction == "pause"
    finally:
        for adapter in adapters:
            await adapter.close()


async def test_live_managed_skill_change_reaches_all_operational_subscribers() -> None:
    team_id = _team("nats_skill_cat")
    mapper = NatsSubjectMapper(team_id=team_id)
    adapters = [
        _adapter(team_id, mapper=mapper, name=f"skill-catalog-{idx}")
        for idx in range(3)
    ]
    try:
        queues = []
        for idx, adapter in enumerate(adapters):
            await adapter.connect(reconcile_topology=(idx == 0))
            queues.append(
                await _subscribe_queue(
                    adapter,
                    name=f"skill-catalog-sub-{idx}",
                    subject=mapper.operational_subject(),
                    durable=False,
                )
            )
        for adapter in adapters:
            await _flush(adapter)

        notification = ManagedSkillCatalogChangedV1(
            team_id=team_id,
            source_agent_id="agent_team_architect",
            operation="install",
            change_id="skill_install_0123456789ab",
            skill_name="grill-me",
            installed_tree_sha256="a" * 64,
        )
        envelope = MessageEnvelope(
            msg_content=notification,
            msg_meta=MessageMeta(from_agent_name="TeamArchitect-0001"),
            msg_header=MessageHeader(
                from_agent_id=notification.source_agent_id,
                to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                idempotency_key=notification.notification_id,
            ),
        )
        await _publish(
            adapters[0],
            RoutingType.ALL_AGENTS_ROUTE.value,
            envelope,
            durable=False,
            operational=True,
        )

        deliveries = [
            await asyncio.wait_for(queue.get(), timeout=5) for queue in queues
        ]
        for delivery in deliveries:
            await delivery.ack()
            received = delivery.envelope.msg_content
            assert isinstance(received, ManagedSkillCatalogChangedV1)
            assert received.notification_id == notification.notification_id
            assert received.skill_name == "grill-me"
    finally:
        for adapter in adapters:
            await adapter.close()


async def test_live_worker_pool_single_delivery_and_broadcast_fanout() -> None:
    team_id = _team("nats_pool")
    named_type = "WebResearchAgent"
    named_queue = f"{team_id}_{named_type}"
    mapper = NatsSubjectMapper(
        team_id=team_id, named_agent_type_queue_map={named_type: named_queue}
    )
    queue_group = mapper.durable_name(f"workers_{named_type}")
    adapters = [
        _adapter(team_id, mapper=mapper, name=f"pool-{idx}") for idx in range(2)
    ]
    try:
        work_queues = []
        broadcast_queues = []
        for idx, adapter in enumerate(adapters):
            await adapter.connect(reconcile_topology=(idx == 0))
            work_queues.append(
                await _subscribe_queue(
                    adapter,
                    name=f"pool-worker-{idx}",
                    routing_key=named_queue,
                    durable_name=queue_group,
                    worker_queue=True,
                )
            )
            broadcast_queues.append(
                await _subscribe_queue(
                    adapter,
                    name=f"pool-broadcast-{idx}",
                    subject=mapper.broadcast_subject(),
                    durable=False,
                )
            )
        for adapter in adapters:
            await _flush(adapter)

        await _publish(
            adapters[0],
            named_queue,
            _task(named_queue, "one worker only", named_type=named_type),
        )
        done, pending = await asyncio.wait(
            [asyncio.create_task(queue.get()) for queue in work_queues],
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        assert len(done) == 1
        work_delivery = next(iter(done)).result()
        await work_delivery.ack()
        await asyncio.sleep(0.5)
        assert sum(queue.qsize() for queue in work_queues) == 0

        await _publish(
            adapters[0],
            RoutingType.ALL_AGENTS_ROUTE.value,
            _control(RoutingType.ALL_AGENTS_ROUTE.value, "status"),
            durable=False,
            broadcast=True,
        )
        broadcast_deliveries = [
            await asyncio.wait_for(queue.get(), timeout=5) for queue in broadcast_queues
        ]
        for delivery in broadcast_deliveries:
            await delivery.ack()
            assert delivery.envelope.msg_content.instruction.instruction == "status"
    finally:
        for adapter in adapters:
            await adapter.close()


async def test_live_presence_replays_last_status_to_late_joiner_but_core_broadcast_does_not() -> (
    None
):
    team_id = _team("nats_late_join")
    mapper = NatsSubjectMapper(team_id=team_id)
    adapter = _adapter(team_id, mapper=mapper)
    try:
        await adapter.connect(reconcile_topology=True)
        await _publish(
            adapter,
            RoutingType.ALL_AGENTS_ROUTE.value,
            _status("manager_a", team_id),
            presence=True,
        )
        await _publish(
            adapter,
            RoutingType.ALL_AGENTS_ROUTE.value,
            _status("manager_a", team_id, exiting=True),
            presence=True,
        )

        presence_queue = await _subscribe_queue(
            adapter,
            name="late-presence",
            subject=f"{mapper.prefix}.presence.*",
            durable_name=mapper.durable_name(new_id("presence_late")),
            deliver_last_per_subject=True,
        )
        presence = await asyncio.wait_for(presence_queue.get(), timeout=5)
        await presence.ack()
        assert isinstance(presence.envelope.msg_content, AgentStatus)
        assert presence.envelope.msg_content.self_id == "manager_a"
        assert presence.envelope.msg_content.is_exiting is True

        await _publish(
            adapter,
            RoutingType.ALL_AGENTS_ROUTE.value,
            _control(RoutingType.ALL_AGENTS_ROUTE.value, "resume"),
            durable=False,
            broadcast=True,
        )
        late_broadcast = await _subscribe_queue(
            adapter,
            name="late-core-broadcast",
            subject=mapper.broadcast_subject(),
            durable=False,
        )
        await _wait_no_delivery(late_broadcast, timeout=0.5)
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    ("steering_type", "guidance"),
    [
        (TaskSteeringType.PROACTIVE_GUIDANCE, "apply this new constraint"),
        (TaskSteeringType.INTERRUPT_RESPONSE, "approved"),
    ],
)
async def test_live_task_steering_routes_to_active_agent(
    steering_type: TaskSteeringType, guidance: str
) -> None:
    team_id = _team("nats_steering")
    route = new_id("manager")
    adapter = _adapter(team_id)
    try:
        await adapter.connect(reconcile_topology=True)
        queue = await _subscribe_queue(
            adapter,
            name="steering-target",
            routing_key=route,
            durable_name=adapter.mapper.durable_name(f"agent_{route}"),
        )
        await _publish(
            adapter,
            route,
            _steering(route, steering_type, text=guidance),
            idempotency_key=f"steering_{steering_type.value}_{uuid.uuid4().hex}",
        )
        delivered = await asyncio.wait_for(queue.get(), timeout=5)
        await delivered.ack()
        assert isinstance(delivered.envelope.msg_content, TaskSteering)
        assert delivered.envelope.msg_content.steering_type == steering_type
        assert delivered.envelope.msg_content.guidance_text == guidance
        if steering_type == TaskSteeringType.INTERRUPT_RESPONSE:
            assert (
                delivered.envelope.msg_content.interrupt_responses[0]["response"]
                == guidance
            )
    finally:
        await adapter.close()


async def test_live_multi_team_subjects_are_isolated_on_same_nats_server() -> None:
    team_a = _team("nats_iso_a")
    team_b = _team("nats_iso_b")
    route = "shared_manager_route"
    adapter_a = _adapter(team_a)
    adapter_b = _adapter(team_b)
    try:
        await adapter_a.connect(reconcile_topology=True)
        await adapter_b.connect(reconcile_topology=True)
        queue_a = await _subscribe_queue(
            adapter_a,
            name="team-a-route",
            routing_key=route,
            durable_name=adapter_a.mapper.durable_name(f"agent_{route}"),
        )
        queue_b = await _subscribe_queue(
            adapter_b,
            name="team-b-route",
            routing_key=route,
            durable_name=adapter_b.mapper.durable_name(f"agent_{route}"),
        )
        await _publish(adapter_a, route, _task(route, "team a only"))
        delivered_a = await asyncio.wait_for(queue_a.get(), timeout=5)
        await delivered_a.ack()
        assert delivered_a.envelope.msg_content.problem_statement == "team a only"
        await _wait_no_delivery(queue_b, timeout=0.5)
    finally:
        await adapter_a.close()
        await adapter_b.close()


async def _run_docker(
    *args: str, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    def _call() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    return await asyncio.to_thread(_call)


async def _restart_nats_container() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is not available for NATS restart test")
    configured = os.getenv("NATS_DOCKER_CONTAINER", "team-nats-live")
    ps = await _run_docker("ps", "--format", "{{.Names}}")
    names = set(ps.stdout.splitlines())
    if configured not in names:
        pytest.skip(f"NATS docker container {configured!r} is not running")
    restarted = await _run_docker("restart", configured, timeout=60.0)
    if restarted.returncode != 0:
        raise AssertionError(f"Failed to restart {configured}: {restarted.stderr}")


async def _wait_for_nats(
    team_id: str, *, work_max_age_seconds: float, timeout: float = 30.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        # Readiness validates the existing stream too, so use the same retention
        # contract as the adapters that created it before the restart.
        adapter = _adapter(
            team_id,
            name=f"restart-probe-{new_id_suffix()}",
            work_max_age_seconds=work_max_age_seconds,
        )
        try:
            await adapter.connect()
            await adapter.close()
            return
        except Exception as exc:  # pragma: no cover - depends on live restart timing
            last_error = exc
            with contextlib.suppress(Exception):
                await adapter.close()
            await asyncio.sleep(0.5)
    raise AssertionError(f"NATS did not become available after restart: {last_error}")


@pytest.mark.docker
async def test_live_unacked_work_redelivers_after_nats_server_restart() -> None:
    team_id = _team("nats_restart")
    route = new_id("agent")
    work_max_age_seconds = 120.0
    first = _adapter(
        team_id, ack_wait_seconds=0.75, max_deliver=4,
        work_max_age_seconds=work_max_age_seconds,
    )
    try:
        await first.connect(reconcile_topology=True)
        queue = await _subscribe_queue(
            first,
            name="restart-first",
            routing_key=route,
            durable_name=first.mapper.durable_name(f"agent_{route}"),
        )
        await _publish(first, route, _task(route, "survive nats restart"))
        first_delivery = await asyncio.wait_for(queue.get(), timeout=5)
        assert first_delivery.redelivery_count == 0

        await _restart_nats_container()
        with contextlib.suppress(Exception):
            await first.close()
        await _wait_for_nats(team_id, work_max_age_seconds=work_max_age_seconds)

        second = _adapter(
            team_id, ack_wait_seconds=0.75, max_deliver=4,
            work_max_age_seconds=work_max_age_seconds,
        )
        try:
            await second.connect()
            redelivery_queue = await _subscribe_queue(
                second,
                name="restart-second",
                routing_key=route,
                durable_name=second.mapper.durable_name(f"agent_{route}"),
            )
            redelivered = await asyncio.wait_for(redelivery_queue.get(), timeout=10)
            await redelivered.ack()
            assert redelivered.redelivery_count and redelivered.redelivery_count >= 1
            assert (
                redelivered.envelope.msg_content.problem_statement
                == "survive nats restart"
            )
        finally:
            await second.close()
    finally:
        with contextlib.suppress(Exception):
            await first.close()


async def _wait_for_consumer_deletion(
    adapter: NatsTransportAdapter,
    *,
    stream_name: str,
    consumer_name: str,
    timeout_seconds: float = 10.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        try:
            await adapter._js.consumer_info(stream_name, consumer_name)
        except Exception as exc:
            if "not found" in str(exc).lower():
                return
            raise
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"consumer {stream_name}/{consumer_name} was not deleted"
            )
        await asyncio.sleep(0.2)


async def test_live_process_consumer_is_deleted_on_graceful_close() -> None:
    team_id = _team("nats_pclose")
    mapper = NatsSubjectMapper(team_id=team_id)
    agent_id = new_id("agent_id")
    consumer_name = mapper.durable_name(f"agent_{agent_id}")
    adapter = _adapter(team_id, mapper=mapper)
    inspector = _adapter(team_id, mapper=mapper)
    try:
        await adapter.connect(reconcile_topology=True)
        await _subscribe_queue(
            adapter,
            name="process-direct",
            routing_key=agent_id,
            durable_name=consumer_name,
            consumer_scope=ConsumerScope.PROCESS,
            consumer_role="agent_direct",
            owner_agent_id=agent_id,
        )

        await adapter.close()
        await inspector.connect()
        await _wait_for_consumer_deletion(
            inspector,
            stream_name=mapper.work_stream_name(),
            consumer_name=consumer_name,
        )
    finally:
        await inspector.close()
        await adapter.close()


async def test_live_process_consumer_expires_after_connection_loss() -> None:
    team_id = _team("nats_pexpire")
    mapper = NatsSubjectMapper(team_id=team_id)
    agent_id = new_id("agent_id")
    consumer_name = mapper.durable_name(f"agent_{agent_id}")
    crashed = _adapter(
        team_id,
        mapper=mapper,
        process_consumer_inactive_threshold_seconds=1.0,
    )
    inspector = _adapter(team_id, mapper=mapper)
    try:
        await crashed.connect(reconcile_topology=True)
        queue: asyncio.Queue[InboundDelivery] = asyncio.Queue()

        async def handler(delivery: InboundDelivery) -> None:
            await queue.put(delivery)

        await crashed.subscribe_route(
            name="crashed-process-direct",
            routing_key=agent_id,
            handler=handler,
            durable_name=consumer_name,
            consumer_scope=ConsumerScope.PROCESS,
            consumer_role="agent_direct",
            owner_agent_id=agent_id,
        )
        await crashed._nc.close()

        await inspector.connect()
        await _wait_for_consumer_deletion(
            inspector,
            stream_name=mapper.work_stream_name(),
            consumer_name=consumer_name,
        )
    finally:
        await inspector.close()
        with contextlib.suppress(Exception):
            await crashed._nc.close()


async def test_live_reconciler_uses_normalized_default_team_id(monkeypatch) -> None:
    from nats.js.api import AckPolicy, ConsumerConfig

    team_id = _team("nats_rscope")
    mapper = NatsSubjectMapper(team_id=team_id)
    adapter = _adapter(team_id, mapper=mapper)
    agent_id = new_id("agent_id")
    consumer_name = mapper.durable_name(f"agent_{agent_id}")
    monkeypatch.setenv("DEFAULT_TEAM_ID", team_id)

    try:
        await adapter.connect(reconcile_topology=True)
        await adapter._js.add_consumer(
            mapper.work_stream_name(),
            ConsumerConfig(
                durable_name=consumer_name,
                ack_policy=AckPolicy.EXPLICIT,
                filter_subject=mapper.agent_work_subject(agent_id),
                deliver_subject=f"_INBOX.{new_id_suffix()}",
            ),
        )

        result = await reconcile_nats_consumers(
            config_path=Path(__file__).resolve().parents[1] / "config.example.yaml",
            apply=False,
            minimum_age_seconds=0,
        )

        assert result.scanned_count == 1
        assert result.candidate_count == 1, result.decisions
    finally:
        if adapter._js is not None:
            with contextlib.suppress(Exception):
                await adapter._js.delete_stream(mapper.work_stream_name())
        await adapter.close()
