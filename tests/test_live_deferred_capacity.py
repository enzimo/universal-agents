"""A full paused inbox retains a JetStream message for later admission."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.deferred_message_runtime import requires_execution_slot
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_messages.enums import MessageMetaType
from custom_agents.strands_agent import UniversalAgent
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_transport.subjects import NatsSubjectMapper
from id_generation import new_id
from live_nats_client import nats_transport_config_from_env
from tests.test_deferred_message_inbox import _envelope, inbox as inbox


def _paused_worker(inbox, team_id, state_refreshes):
    async def refresh_self_agent_state(*, is_exiting: bool = False) -> None:
        state_refreshes.append(is_exiting)

    # on_nats_delivery republishes agent state and wakes pending work after
    # every delivery, and admission consults the task queue before the inbox.
    return SimpleNamespace(
        id="worker",
        team_id=team_id,
        named_agent_type_queue_map={},
        paused=True,
        is_executing=False,
        deferred_inbox=inbox,
        _logger=inbox.logger,
        _message_targets_other_team=lambda _: False,
        _requires_execution_slot=requires_execution_slot,
        _refresh_self_agent_state=refresh_self_agent_state,
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _send_task_error_response=AsyncMock(),
        nats_max_deliver=5,
        task_manager=SimpleNamespace(
            operations_runtime=TaskOperationRuntime(),
            identity=SimpleNamespace(owner_agent_id="worker"),
            is_buffer_full=lambda: False,
            get_task_info=lambda _task_id: None,
            get_task_from_db=lambda _task_id, owner_agent_id: None,
            get_max_queued_top_level_tasks=lambda: 10,
        ),
    )


def _result_envelope(task_id):
    envelope = _envelope(task_id)
    envelope.msg_content.task_type = MessageMetaType.RESPONSE
    return envelope


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.nats
async def test_full_inbox_naks_result_then_admits_redelivery_after_capacity_is_released(
    inbox,
):
    """Results keep broker custody while the paused inbox is full."""
    inbox.capacity = 1
    await inbox.admit(_envelope("already-owned"))
    mapper = NatsSubjectMapper(team_id=new_id("capacity"))
    adapter = NatsTransportAdapter(
        config=nats_transport_config_from_env(), mapper=mapper
    )
    state_refreshes = []
    agent = _paused_worker(inbox, mapper.team_id, state_refreshes)
    rejected, admitted = asyncio.Event(), asyncio.Event()
    deliveries = []

    async def receive(delivery):
        deliveries.append(delivery.redelivery_count)
        await UniversalAgent.on_nats_delivery(agent, delivery)
        if len(deliveries) == 1:
            assert [item.msg_content.task_id for item in inbox.messages] == [
                "already-owned"
            ]
            rejected.set()
        elif any(item.msg_content.task_id == "broker-owned" for item in inbox.messages):
            admitted.set()

    await adapter.connect(reconcile_topology=True)
    try:
        await adapter.subscribe_route(
            name="capacity",
            routing_key="worker",
            durable_name="capacity",
            handler=receive,
        )
        await adapter.publish(
            envelope=_result_envelope("broker-owned"), routing_key="worker"
        )
        await asyncio.wait_for(rejected.wait(), timeout=5)
        await inbox.clear()
        await asyncio.wait_for(admitted.wait(), timeout=8)
        assert len(deliveries) >= 2
        assert [item.msg_content.task_id for item in inbox.messages] == ["broker-owned"]
        assert len(state_refreshes) >= len(deliveries)
        assert not any(state_refreshes)
        agent._send_task_error_response.assert_not_awaited()
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.nats
async def test_full_inbox_rejects_new_request_once_without_redelivery(inbox):
    """A new request to a full inbox is answered as not queued and acknowledged."""
    inbox.capacity = 1
    await inbox.admit(_envelope("already-owned"))
    mapper = NatsSubjectMapper(team_id=new_id("capacity"))
    adapter = NatsTransportAdapter(
        config=nats_transport_config_from_env(), mapper=mapper
    )
    agent = _paused_worker(inbox, mapper.team_id, [])
    handled = asyncio.Event()
    deliveries = []

    async def receive(delivery):
        deliveries.append(delivery.redelivery_count)
        await UniversalAgent.on_nats_delivery(agent, delivery)
        handled.set()

    await adapter.connect(reconcile_topology=True)
    try:
        await adapter.subscribe_route(
            name="capacity",
            routing_key="worker",
            durable_name="capacity",
            handler=receive,
        )
        await adapter.publish(envelope=_envelope("rejected-request"), routing_key="worker")
        await asyncio.wait_for(handled.wait(), timeout=5)
        # A refused (NAK) delivery is redelivered promptly; none may arrive.
        await asyncio.sleep(3)
        assert len(deliveries) == 1
        assert [item.msg_content.task_id for item in inbox.messages] == [
            "already-owned"
        ]
        agent._send_task_error_response.assert_awaited_once()
        rejected_task, _header, _meta, message = (
            agent._send_task_error_response.await_args.args
        )
        assert rejected_task.task_id == "rejected-request"
        assert "incoming message queue is full" in message
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.nats
async def test_transport_close_joins_inflight_durable_admission(inbox, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = inbox.repository.add_deferred_message

    def blocked_commit(**kwargs):
        started.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(inbox.repository, "add_deferred_message", blocked_commit)
    mapper = NatsSubjectMapper(team_id=new_id("close_custody"))
    adapter = NatsTransportAdapter(
        config=nats_transport_config_from_env(), mapper=mapper
    )
    await adapter.connect(reconcile_topology=True)

    async def receive(delivery):
        await inbox.admit(delivery.envelope)
        await delivery.ack()

    close = None
    try:
        await adapter.subscribe_route(
            name="custody",
            routing_key="worker",
            durable_name="custody",
            handler=receive,
        )
        await adapter.publish(envelope=_envelope("inflight"), routing_key="worker")
        assert await asyncio.to_thread(started.wait, 2)
        close = asyncio.create_task(adapter.close())
        await asyncio.sleep(0.05)
        assert not close.done(), (
            "transport close released its storage callback before commit"
        )
    finally:
        release.set()
        await asyncio.wait_for(close if close else adapter.close(), 3)
    assert len(inbox) == 1
    assert inbox.repository.load_deferred_messages_by_owner("worker")
