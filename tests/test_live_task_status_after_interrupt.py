"""A worker keeps answering while an approval resume holds its turn (nats-server 2.15.1).

A person's approval reaches the worker as a ``TaskSteering`` interrupt response on the
operational subject, which resumes the turn the approval interrupted. That resume once ran
inside the subscription's handler and blocked it, so nothing more on the subject was
answered until the turn ended. While the resume is held here:

- the requester's ``task.probe`` (custom_agents.live_controls), sent through its process's
  host and signed with its session, is answered on the worker's own address;
- a ``TaskStatus`` request on the operational subject, as the gateway's parent-task probe
  still sends one until phase 3 (plan decision 68), is answered there too.

Running the resumed turn inline in the steering handler makes the second check fail.
"""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from custom_agents import agent_requests
from custom_agents.agent_host import (
    LEAD_HOST_RUNTIME,
    AgentProcessHost,
    definition_sha256,
    lead_host_profile,
    startup_agent_host_profile,
)
from custom_agents.live_controls import LIVE_CONTROL_HANDLERS
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskStatus, TaskSteering
from custom_messages.enums import MessageMetaType, ProgressState, RoutingType, TaskSteeringType
from custom_transport.base import SubscriptionDelivery, TransportPublishOptions
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.subjects import NatsSubjectMapper
from custom_transport.protocol1.host_runtime import MessageHost
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from tests.live_nats_support import flush_adapter_fully
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401 - broker is a fixture
from tests.test_turn_action_outbox import _make_agent, _make_task_manager

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

WORKER_RUNTIME = "agent-WebResearchAgent"
DEFINITION = definition_sha256("ab" * 32)


async def _until(condition, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_status_reply_while_approval_resumes_a_long_turn(
    broker: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811 - the shared live NATS fixture
) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect", "WebResearchAgent"), 2)
    worker_profile = startup_agent_host_profile(TEAM, WORKER_RUNTIME, "WebResearchAgent")
    release, entered = asyncio.Event(), asyncio.Event()
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), lead_profile, worker_profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))

        requester = _make_agent(tmp_path / "requester", task_manager=_make_task_manager(tmp_path, owner_id="requester"))
        worker = _make_agent(tmp_path / "worker", task_manager=_make_task_manager(tmp_path, owner_id="worker"))
        for agent, name, agent_type in ((requester, "requester", "TeamArchitect"), (worker, "worker", "WebResearchAgent")):
            agent.team_id, agent.name, agent.named_agent_type = TEAM, name, agent_type
        requester.subtask_status_probe_timeout_seconds = 2.0
        requester_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
        worker_host = AgentProcessHost(layout, worker_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
        # The requester's process: ask_agent and team_view go through its host.
        monkeypatch.setattr(agent_requests, "_PROCESS_HOST", requester_host)
        requester_host.attach(requester, agent_type="TeamArchitect", definition=DEFINITION,
                              nc=await connect(stack, broker, LEAD_HOST_RUNTIME), state=("busy", True))
        worker_host.attach(worker, agent_type="WebResearchAgent", definition=DEFINITION,
                           nc=await connect(stack, broker, WORKER_RUNTIME), state=("busy", True))
        await _until(lambda: agent_requests.team_view() is not None
                     and agent_requests.team_view().agent(str(worker.id)) is not None)

        task = Task(task_id="delegated_task", task_type=MessageMetaType.REQUEST)
        worker.task_manager.add_task(task, MessageHeader(from_agent_id=requester.id), MessageMeta(),
                                     owner_person_id=None)
        worker.task_manager.update_main_task_state(task.task_id, ProgressState.WAITING,
                                                   datetime.now(timezone.utc).isoformat())
        worker._interrupted_turn_sessions[task.task_id] = SimpleNamespace()

        async def held_resume(task_id):
            worker._pending_interrupt_responses.pop(task_id, None)
            entered.set()
            await release.wait()

        object.__setattr__(worker, "_resume_interrupted_task_turn", held_resume)
        # The operational subject: the worker reads it as an agent does; the gateway stand-in sends
        # on it and keeps the worker's answers. On this test's own server, like the rest: the
        # team id is fixed, so on the shared server every run of this test would share its streams.
        operational: list[NatsTransportAdapter] = []
        for name in ("worker", "gateway"):
            adapter = NatsTransportAdapter(config=NatsTransportConfig(servers=[broker], name=f"operational-{name}"),
                                           mapper=NatsSubjectMapper(team_id=TEAM))
            await adapter.connect(reconcile_topology=True)
            stack.push_async_callback(adapter.close)
            operational.append(adapter)
        worker_adapter, gateway = operational
        object.__setattr__(worker, "_ensure_nats_transport_adapter", lambda: worker_adapter)
        object.__setattr__(worker, "_operational_exchange_name", worker_adapter.mapper.operational_subject())
        await worker_adapter.subscribe_subject(
            name="worker", subject=worker_adapter.mapper.operational_subject(),
            handler=worker.on_nats_operational_delivery, delivery=SubscriptionDelivery.CORE,
            logical_route=RoutingType.ALL_AGENTS_ROUTE.value)
        answers: list[TaskStatus] = []

        async def collect(delivery) -> None:
            content = delivery.envelope.msg_content
            if isinstance(content, TaskStatus) and content.status_type == MessageMetaType.RESPONSE:
                answers.append(content)
            await delivery.ack()

        await gateway.subscribe_subject(
            name="gateway", subject=gateway.mapper.operational_subject(), handler=collect,
            delivery=SubscriptionDelivery.CORE, logical_route=RoutingType.ALL_AGENTS_ROUTE.value)
        for adapter in operational:
            await flush_adapter_fully(adapter)

        async def publish(content) -> None:
            await gateway.publish(
                envelope=MessageEnvelope(
                    msg_content=content,
                    msg_header=MessageHeader(from_agent_id="gateway", to_routing_path=worker.id,
                                             response_routing_key="gateway"),
                    msg_meta=MessageMeta(),
                ),
                routing_key=worker.id,
                options=TransportPublishOptions(durable=False, operational=True),
            )
        root = Task(task_id="root", task_type=MessageMetaType.REQUEST)
        requester.task_manager.add_task(root, MessageHeader(from_agent_id="gateway"), MessageMeta(),
                                        owner_person_id=None)
        child = requester.task_manager.add_subtask(
            root.task_id, task, MessageHeader(to_routing_path=worker.id),
            MessageMeta(to_agent_name=worker.name, to_named_agent_type=worker.named_agent_type),
        )
        try:
            before = await requester._probe_subtask_task_status(subtask_info=child)
            assert before is not None and before.is_known_task and before.responder_agent_id == worker.id
            # The person's approval arrives as an interrupt response; it resumes the held turn.
            await publish(TaskSteering(
                steering_type=TaskSteeringType.INTERRUPT_RESPONSE, target_task_id=task.task_id,
                target_agent_id=worker.id, interrupt_responses=[{"interrupt_id": "approval", "response": "approved"}]))
            await asyncio.wait_for(entered.wait(), 5)
            # Its address still answers the requester's task probe...
            after = await requester._probe_subtask_task_status(subtask_info=child)
            assert after is not None, "Approval resume blocked the worker's task probes"
            assert after.is_known_task and after.responder_agent_id == worker.id
            # ...and its operational subscription still answers the gateway's parent-task probe.
            asked = TaskStatus(status_type=MessageMetaType.REQUEST, task_id=task.task_id, target_agent_id=worker.id,
                               correlation_id=uuid4().hex)
            await publish(asked)
            await _until(lambda: any(answer.task_status_id == asked.task_status_id for answer in answers), timeout=5)
            [answer] = [answer for answer in answers if answer.task_status_id == asked.task_status_id]
            assert answer.is_known_task and answer.responder_agent_id == worker.id, (
                "Approval resume blocked the operational subscription")
        finally:
            release.set()
            continuation = getattr(worker, "_pending_work_task", None)
            if continuation is not None:
                await asyncio.wait_for(asyncio.gather(continuation, return_exceptions=True), 5)
            await worker_host.stop()
            await requester_host.stop()
            stop.set()
            await asyncio.wait_for(serving, timeout=10)
