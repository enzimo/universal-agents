"""Messages still reach an agent whose model call is stuck mid-stream.

A person reported that an agent "sits there" while its model call hangs and
that new messages seem not to reach it. This test holds a real Strands model
call open (the provider stream never finishes), then sends, over real NATS:

- a new task on the agent's work route, which must be admitted to the durable
  deferred inbox and acknowledged, not left waiting behind the running turn;
- same-conversation guidance as ``TaskSteering`` on the operational subject,
  which must be accepted and answered while the turn is still running.

It also measures event-loop lag throughout, because a model call that blocked
the loop would stall every subscription at once. When the stuck call finally
ends, the queued task must run next.
"""

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from strands import Agent
from strands.models.model import Model

from custom_agents import task_message_runtime
from custom_agents.conversation_store import ConversationStore
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    RoutingType,
    TaskSteeringStatus,
    TaskSteeringType,
)
from custom_tools.comms_tools import TeamComms
from custom_transport.base import SubscriptionDelivery, TransportPublishOptions
from test_live_nats_replacement_semantics import _adapter, _subscribe_queue, _team
from test_turn_action_outbox import _make_agent


class StuckStreamModel(Model):
    """A provider whose stream starts, then sends nothing until released."""

    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.second_call = asyncio.Event()
        self.release = asyncio.Event()

    def update_config(self, **config):
        pass

    def get_config(self):
        return {"model_id": "local-stuck-provider"}

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("No structured output expected")
        yield

    async def stream(self, *args, **kwargs):
        self.calls += 1
        (self.entered if self.calls == 1 else self.second_call).set()
        yield {"messageStart": {"role": "assistant"}}
        if self.calls == 1:
            await self.release.wait()
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "done"}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


async def _loop_lag_monitor(samples: list[float], stop: asyncio.Event) -> None:
    interval = 0.02
    while not stop.is_set():
        started = time.monotonic()
        await asyncio.sleep(interval)
        samples.append(time.monotonic() - started - interval)


async def _wait_until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Condition was not reached in time.")
        await asyncio.sleep(0.02)


def _chat_task(task_id: str, reply: str, agent_id: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(
            task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=task_id
        ),
        msg_header=MessageHeader(
            from_agent_id="gateway", to_routing_path=agent_id, response_routing_key=reply
        ),
        msg_meta=MessageMeta(
            msg_correlation_id=f"corr_{task_id}",
            requester_type=RequesterType.USER_REQUESTER,
            is_response_requested=True,
            gateway_context=GatewayContext(canonical_user_id="owner"),
        ),
    )


@pytest.mark.live
@pytest.mark.integration
@pytest.mark.nats
@pytest.mark.asyncio
async def test_new_messages_reach_agent_while_model_stream_is_stuck(tmp_path, monkeypatch):
    team = _team("stuck")
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"), max_queued_top_level_tasks=10)
    manager.set_owner_identity("lead", "Lead", team, "TeamArchitect")
    agent = _make_agent(tmp_path, task_manager=manager)
    agent.team_id = team
    agent.task_manager.set_owner_identity(agent.id, "Lead", team, "TeamArchitect")
    adapter = _adapter(team)
    provider = StuckStreamModel()
    sdk = Agent(model=provider, callback_handler=None)
    completed: list[str] = []

    async def real_sdk_turn(content, header, meta):
        agent._set_active_execution_context(content.task_id)
        try:
            sdk.messages.clear()
            await sdk.invoke_async(content.problem_statement)
            return content.model_copy(
                update={"task_type": MessageMetaType.RESPONSE, "answer": "done"}
            )
        finally:
            agent._clear_active_execution_context()

    async def complete(_agent, task_id, _task_info, _result):
        completed.append(task_id)
        await manager.operations_runtime.run(
            manager.complete_task, task_id, datetime.now(timezone.utc).isoformat()
        )

    agent._llm_turn_executor = SimpleNamespace(process_llm_interaction=real_sdk_turn)
    agent._strands_agent = sdk
    agent._conversation_store = ConversationStore(db_path=str(tmp_path / "conversations.db"), artifacts_dir=tmp_path / "conversation_artifacts")
    agent._conversation_summary_generator = SimpleNamespace(summarize_turn=Mock())
    agent._conversation_compactor = SimpleNamespace(compact_if_needed=Mock())
    for name in ("_refresh_self_agent_state", "_notify_context_after_turn"):
        object.__setattr__(agent, name, AsyncMock())
    monkeypatch.setattr(task_message_runtime, "_apply_task_processing_result", complete)
    object.__setattr__(agent, "_drain_due_turn_action_outbox", AsyncMock(return_value=False))
    object.__setattr__(agent, "_runtime_coordinator", lambda: SimpleNamespace(
        drain_all_deferred_events=AsyncMock(return_value=False)))
    agent._nats_transport_adapter = adapter
    reply = f"{team}_gateway_response"
    lag_samples: list[float] = []
    stop_monitor = asyncio.Event()
    monitor = None
    try:
        await adapter.connect(reconcile_topology=True)
        # The agent's own delivery handlers on its real work and operational subjects.
        await adapter.subscribe_route(
            name=f"{agent.id}:direct", routing_key=agent.id, handler=agent.on_nats_delivery,
            durable_name=adapter.mapper.durable_name(f"agent_{agent.id}"),
        )
        await adapter.subscribe_subject(
            name=f"{agent.id}:operational", subject=adapter.mapper.operational_subject(),
            handler=agent.on_nats_operational_delivery,
            logical_route=RoutingType.ALL_AGENTS_ROUTE.value, delivery=SubscriptionDelivery.CORE,
        )
        responses = await _subscribe_queue(
            adapter, name="responses", routing_key=reply,
            durable_name=adapter.mapper.durable_name("responses"),
        )
        agent.team_comms = TeamComms(team_id=team, nats_adapter=adapter)

        await agent.team_comms.send_message(
            message=_chat_task("first", reply, agent.id), routing_key=agent.id
        )
        await asyncio.wait_for(provider.entered.wait(), 5)
        monitor = asyncio.create_task(_loop_lag_monitor(lag_samples, stop_monitor))
        assert agent.is_executing

        # A message in another conversation: durably queued and acknowledged.
        await agent.team_comms.send_message(
            message=_chat_task("second", reply, agent.id), routing_key=agent.id
        )
        await _wait_until(
            lambda: any(
                item.msg_content.task_id == "second" for item in agent.deferred_inbox.messages
            ),
            3,
        )

        # A message in the same conversation: accepted as guidance while stuck.
        steering = TaskSteering(
            steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
            target_task_id="first",
            target_agent_id=agent.id,
            guidance_text="Draft the email instead of sending it.",
            idempotency_key="steer_first",
        )
        await adapter.publish(
            envelope=MessageEnvelope(
                msg_content=steering,
                msg_header=MessageHeader(
                    from_agent_id=reply, to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                    response_routing_key=reply, idempotency_key="steer_first",
                ),
                msg_meta=MessageMeta(
                    msg_correlation_id="corr_steer",
                    requester_type=RequesterType.USER_REQUESTER,
                    is_response_requested=True,
                ),
            ),
            routing_key=RoutingType.ALL_AGENTS_ROUTE.value,
            options=TransportPublishOptions(operational=True, durable=False),
        )
        status = await asyncio.wait_for(responses.get(), 3)
        await status.ack()
        assert isinstance(status.envelope.msg_content, TaskSteering)
        assert status.envelope.msg_content.status == TaskSteeringStatus.ACCEPTED
        assert agent._pending_proactive_task_steering["first"]

        # The model call is still stuck, and nothing blocked the event loop.
        assert not provider.release.is_set() and provider.calls == 1
        assert agent.is_executing and not completed
        assert max(lag_samples) < 0.25

        # When the stuck call ends, the queued task runs next.
        provider.release.set()
        await asyncio.wait_for(provider.second_call.wait(), 5)
        await _wait_until(lambda: completed == ["first", "second"], 5)
    finally:
        stop_monitor.set()
        if monitor is not None:
            await asyncio.gather(monitor, return_exceptions=True)
        agent._should_exit.set()
        provider.release.set()
        pending = getattr(agent, "_pending_work_task", None)
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await adapter.close()
