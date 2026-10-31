"""A rejected request must reach the requester without needing a model turn."""

from datetime import datetime, timezone
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import task_message_runtime
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import Task
from custom_agents.task_queue_notifications import check_queue_pressure
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from test_turn_action_outbox import _make_agent
from test_gateway_manager_client import _client, _FakeManagerRuntime


def _request(task_id):
    return (
        Task(
            task_id=task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Do work",
        ),
        MessageHeader(
            from_agent_id="gateway", response_routing_key="test_team_gateway_response"
        ),
        MessageMeta(
            msg_correlation_id=f"corr_{task_id}",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                canonical_user_id="alice", session_key="session_alice"
            ),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True], ids=["busy", "paused"])
async def test_new_thread_is_visible_and_durable_before_the_active_turn_finishes(
    tmp_path, monkeypatch, paused,
):
    from custom_agents.manager_control_runtime import _runtime_work_roots
    from custom_agents.pending_work_runtime import close_pending_work
    from custom_messages.contracts.envelope_v1 import MessageEnvelope
    from tests.broker_access_support import context_signer
    from tests.change_approval_support import use_signed_contexts

    use_signed_contexts(monkeypatch)
    agent = _make_agent(tmp_path)
    agent.paused = paused
    monkeypatch.setattr(agent, "_refresh_self_agent_state", AsyncMock())
    task, header, meta = _request("new-thread")
    meta.gateway_context.task_context = context_signer().issue(
        team_id=agent.team_id, person_id="alice", person_roles=("member",),
    )
    header.to_routing_path = agent.id
    envelope = MessageEnvelope(msg_content=task, msg_header=header, msg_meta=meta)
    delivery = SimpleNamespace(
        envelope=envelope, logical_route=agent.id, source_queue_name=agent.id,
        transport_subject="test.requests", redelivery_count=0, max_deliver=5,
        ack=AsyncMock(), nak=AsyncMock(),
    )
    try:
        async with agent._execution_lanes.hold():
            await asyncio.wait_for(agent.on_nats_delivery(delivery), 5)
            roots = await _runtime_work_roots(agent, "alice")
            assert [root.task.task_id for root in roots] == [task.task_id]
            assert not await _runtime_work_roots(agent, "someone_else")
            assert not agent.deferred_inbox.messages
            assert agent.task_manager.current_task_id is None
            assert list(agent.task_manager.task_state.task_queue) == [task.task_id]
            assert agent.task_manager.runtime_state_store.load_task_runtime_by_owner(agent.id)
            # Redelivery must not create another queue entry while execution waits.
            await asyncio.wait_for(agent.on_nats_delivery(delivery), 5)
            assert list(agent.task_manager.task_state.task_queue) == [task.task_id]
            delivery.nak.assert_not_awaited()
            assert delivery.ack.await_count == 2
    finally:
        await close_pending_work(agent)


@pytest.mark.asyncio
async def test_full_queue_does_not_reject_duplicate_accepted_work(tmp_path):
    manager = TaskManager(
        db_path=str(tmp_path / "tasks.db"), max_queued_top_level_tasks=1
    )
    manager.set_owner_identity("lead", "Lead", "test_team", "TeamArchitect")
    agent = _make_agent(tmp_path, task_manager=manager)
    request = _request("existing")
    assert manager.add_task(*request, owner_person_id=None)
    await task_message_runtime._handle_task_request(agent, *request)
    agent.team_comms.send_message.assert_not_awaited()
    assert manager.get_tracked_top_level_task_count() == 1


@pytest.mark.asyncio
async def test_waiting_tasks_warn_at_90_percent_once_and_rearm_after_recovery(tmp_path):
    manager = TaskManager(
        db_path=str(tmp_path / "tasks.db"), max_queued_top_level_tasks=50
    )
    manager.set_owner_identity("lead", "Lead", "test_team", "TeamArchitect")
    agent = _make_agent(tmp_path, task_manager=manager)
    for index in range(44):
        assert manager.add_task(*_request(f"waiting_{index}"), owner_person_id=None)
        manager.update_main_task_state(
            f"waiting_{index}",
            ProgressState.WAITING,
            datetime.now(timezone.utc).isoformat(),
        )
    await check_queue_pressure(agent)
    agent.team_comms.send_message.assert_not_awaited()
    assert manager.add_task(*_request("threshold"), owner_person_id=None)
    await asyncio.gather(check_queue_pressure(agent), check_queue_pressure(agent))
    agent.team_comms.send_message.assert_awaited_once()
    warning = agent.team_comms.send_message.call_args.kwargs["message"]
    assert "45 of 50" in warning.msg_content.text
    assert "44 waiting" in warning.msg_content.text
    assert warning.msg_meta.gateway_context.canonical_user_id == "alice"
    assert warning.msg_meta.msg_correlation_id != "corr_threshold"
    assert warning.msg_content.source_task_id is None
    unsolicited = []
    client = _client(_FakeManagerRuntime(), unsolicited=unsolicited)
    pending = client.register_response_channel("corr_threshold")
    await client.handle_response(warning)
    assert unsolicited == [warning]
    assert pending.empty()  # The advisory must not finish the accepted chat request.
    assert manager.get_tracked_top_level_task_count() == 45
    manager.remove_task("threshold")
    await check_queue_pressure(agent)
    assert manager.add_task(*_request("threshold_again"), owner_person_id=None)
    await check_queue_pressure(agent)
    assert agent.team_comms.send_message.await_count == 2


@pytest.mark.asyncio
async def test_admission_capacity_race_reports_rejection(tmp_path, monkeypatch):
    from custom_agents.conversation_store import ConversationStore

    manager = TaskManager(
        db_path=str(tmp_path / "tasks.db"), max_queued_top_level_tasks=1
    )
    manager.set_owner_identity("lead", "Lead", "test_team", "TeamArchitect")
    agent = _make_agent(tmp_path, task_manager=manager)
    agent._conversation_store = ConversationStore(
        db_path=str(tmp_path / "conversations.db"), artifacts_dir=tmp_path / "conversation_artifacts"
    )
    add_task = manager.add_task

    def racing_add(*args, **kwargs):
        assert add_task(*_request("competing"), owner_person_id=None)
        return add_task(*args, **kwargs)

    monkeypatch.setattr(manager, "add_task", racing_add)
    await task_message_runtime._handle_task_request(agent, *_request("rejected"))
    response = agent.team_comms.send_message.call_args.kwargs["message"]
    assert "not queued" in response.msg_content.answer
    assert manager.get_task_info("rejected") is None
    assert (
        agent._conversation_store.get_turn_by_task_id("rejected")["task_status"]
        == "failed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["test_user", "another_user"])
async def test_internal_review_backlog_uses_only_its_owners_configured_notification_route(
    tmp_path, owner
):
    from test_notify_user_tool import _write_config
    from assistant_gateway.config import GatewayInstanceConfig

    path = _write_config(tmp_path)
    manager = TaskManager(
        db_path=str(tmp_path / "tasks.db"), max_queued_top_level_tasks=1
    )
    manager.set_owner_identity("lead", "Lead", "test_team", "TeamArchitect")
    agent = _make_agent(tmp_path, task_manager=manager)
    agent.instance_config_path = str(path)
    task, _, _ = _request("internal_review")
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id=agent.id),
        MessageMeta(
            requester_type=RequesterType.AGENT_REQUESTER,
            gateway_context=GatewayContext(canonical_user_id=owner),
        ),
        owner_person_id=owner,
    )
    await check_queue_pressure(agent)
    if owner == "another_user":
        agent.team_comms.send_message.assert_not_awaited()
        assert not agent._queue_pressure_notifications.notified_owners
        return
    sent = agent.team_comms.send_message.call_args.kwargs
    assert (
        sent["routing_key"]
        == GatewayInstanceConfig.load(path).manager.response_queue_name
    )
    notice = sent["message"]
    assert notice.msg_meta.gateway_context.canonical_user_id == owner
    assert notice.msg_meta.gateway_context.chat_id == "-100123"
    assert notice.msg_meta.gateway_context.session_key == "preferred_session"
    assert notice.msg_meta.gateway_context.scheduled_job_id is None
    assert notice.msg_meta.gateway_context.schedule_owner_agent_id is None
    assert notice.msg_meta.conversation_id is None
    assert notice.msg_content.source_task_id is None
