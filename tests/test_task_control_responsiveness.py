"""Exercise controls through the real subscription while a model turn is held."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from assistant_gateway.gateway import MANAGER_CONTROL_TIMEOUT_SECONDS
from custom_agents.conversation_runtime import ensure_task_conversation_context
from custom_agents.conversation_store import ConversationStore
from custom_agents.strands_agent import _subscribe_manager_control_route
from custom_agents.task_manager import TaskManager
from custom_agents.task_queue_notifications import check_queue_pressure
from custom_messages.contracts.control_v1 import ManagerRuntimeWorkCommand
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import (
    GatewayContext, MessageHeader, MessageMeta,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.comms_tools import TeamComms
from test_live_nats_replacement_semantics import _adapter, _subscribe_queue, _team
from test_turn_action_outbox import _make_agent
from tests.pending_work_support import idle_runtime_coordinator
from tests.broker_access_support import context_signer, context_verify_keys, forget_task_context_keys


@pytest.fixture
def gateway_signed_commands(monkeypatch):
    """The manager takes a command's person only from its gateway-signed context."""

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


@pytest.mark.live
@pytest.mark.integration
@pytest.mark.nats
@pytest.mark.asyncio
@pytest.mark.parametrize("is_team_lead", [True, False])
async def test_controls_remain_available_during_model_work_and_after_stop(tmp_path, is_team_lead, gateway_signed_commands):
    team = _team("controls")
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"), max_queued_top_level_tasks=50)
    manager.set_owner_identity("lead", "Lead", team, "TeamArchitect")
    agent = _make_agent(tmp_path, task_manager=manager)
    agent.team_id = team
    agent.is_team_lead = is_team_lead
    agent.task_manager.set_owner_identity(agent.id, "Lead", team, "TeamArchitect")
    adapter = _adapter(team)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_turn(content, header, meta):
        agent._set_active_execution_context(content.task_id)
        entered.set()
        try:
            await release.wait()
            return content.model_copy(update={"task_type": MessageMetaType.RESPONSE})
        finally:
            agent._clear_active_execution_context()

    agent._llm_turn_executor = SimpleNamespace(process_llm_interaction=held_turn)
    agent._strands_agent = SimpleNamespace(cancel=lambda: None, messages=[])
    agent._conversation_store = ConversationStore(db_path=str(tmp_path / "conversations.db"), artifacts_dir=tmp_path / "conversation_artifacts")
    agent._conversation_summary_generator = SimpleNamespace(
        summarize_turn=Mock(side_effect=AssertionError("Control cleanup must not invoke a model")))
    agent._conversation_compactor = SimpleNamespace(
        compact_if_needed=Mock(side_effect=AssertionError("Control cleanup must not compact")))
    for name in ("_refresh_self_agent_state", "_notify_context_after_turn"):
        object.__setattr__(agent, name, AsyncMock())
    object.__setattr__(agent, "_drain_due_turn_action_outbox", AsyncMock(return_value=False))
    # No runtime mailbox events: every lane's drain finds none.
    object.__setattr__(agent, "_runtime_coordinator", idle_runtime_coordinator)
    agent._nats_transport_adapter = adapter
    reply = f"{team}_gateway_response"
    try:
        await adapter.connect(reconcile_topology=True)
        await _subscribe_manager_control_route(agent, adapter)
        responses = await _subscribe_queue(
            adapter, name="responses", routing_key=reply,
            durable_name=adapter.mapper.durable_name("responses"),
        )
        agent.team_comms = TeamComms(team_id=team, nats_adapter=adapter)
        for task_id in ["held", *(f"queued_{index}" for index in range(49))]:
            task = Task(task_id=task_id, problem_statement=task_id, task_type=MessageMetaType.REQUEST)
            header = MessageHeader(from_agent_id="gateway", response_routing_key=reply)
            meta = ensure_task_conversation_context(
                conversation_store=agent._conversation_store, team_id=team,
                owner_agent_id=agent.id, owner_agent_name="Lead", task_request=task,
                msg_header=header, msg_meta=MessageMeta(gateway_context=GatewayContext(
                    canonical_user_id="owner",
                    task_context=context_signer().issue(team_id=team, person_id="owner", person_roles=("member",)),
                )),
                owner_person_id="owner",
            )
            # Admission records the verified task person independently of the
            # message fields; user-scoped controls read that recorded owner.
            assert agent.task_manager.add_task(
                task, header, meta,
                owner_person_id="owner",
            )
            await check_queue_pressure(agent)

        if is_team_lead:
            warning = await asyncio.wait_for(responses.get(), 3)
            assert warning.envelope.msg_content.reason == "task_queue_near_capacity"
            assert "45 of 50" in warning.envelope.msg_content.text
            await warning.ack()

        cancelled: list[str] = []  # the last exchange's cancellation replies

        async def exchange(action, **kwargs):
            command = ManagerRuntimeWorkCommand(
                action=action, scope="user", **kwargs,
            )
            await agent.team_comms.send_message(
                message=MessageEnvelope(
                    msg_content=command,
                    msg_header=MessageHeader(from_agent_id="gateway", to_routing_path=agent.id,
                                             response_routing_key=reply),
                    msg_meta=MessageMeta(gateway_context=GatewayContext(
                        canonical_user_id="owner",
                        task_context=context_signer().issue(team_id=team, person_id="owner", person_roles=("member",)),
                    )),
                ), routing_key=agent.id,
            )
            # Each stopped request also gets its final reply, its cancellation,
            # before the command's answer (control_runtime.stop_task_execution):
            # current work from its stopped turn, queued work from the stop.
            cancelled.clear()
            while True:
                received = await asyncio.wait_for(responses.get(), MANAGER_CONTROL_TIMEOUT_SECONDS)
                await received.ack()
                content = received.envelope.msg_content
                if not isinstance(content, Task):
                    break
                assert action in {"cancel", "remove_task", "purge_tasks"}
                assert content.task_state[-1][0].value == "cancelled"
                cancelled.append(content.task_id)
            assert content.command_id == command.command_id
            return content

        await exchange("list_tasks")
        await asyncio.wait_for(entered.wait(), 3)
        if is_team_lead:
            # Real durable chat delivery must receive a rejection while the model
            # still owns execution, without first entering the deferred inbox.
            requests = await _subscribe_queue(
                adapter, name="full-queue-chat", routing_key=agent.id,
                durable_name=adapter.mapper.durable_name("full_queue_chat"),
            )
            await agent.team_comms.send_message(
                message=MessageEnvelope(
                    msg_content=Task(task_id="rejected_chat", task_type=MessageMetaType.REQUEST,
                                     problem_statement="Please answer me"),
                    msg_header=MessageHeader(from_agent_id="gateway", to_routing_path=agent.id,
                                             response_routing_key=reply),
                    msg_meta=MessageMeta(msg_correlation_id="corr_rejected_chat",
                                         gateway_context=GatewayContext(
                                             canonical_user_id="owner",
                                             task_context=context_signer().issue(
                                                 team_id=team, person_id="owner", person_roles=("member",),
                                             ),
                                         )),
                ), routing_key=agent.id,
            )
            delivery = await asyncio.wait_for(requests.get(), 3)
            await asyncio.wait_for(agent.on_nats_delivery(delivery), 3)
            rejection = await asyncio.wait_for(responses.get(), 3)
            assert rejection.envelope.msg_meta.msg_correlation_id == "corr_rejected_chat"
            assert "not queued" in rejection.envelope.msg_content.answer
            assert not release.is_set()
            assert not agent.deferred_inbox.messages
            await rejection.ack()
        view = await exchange("list_tasks")
        assert view.current_task_count == 1
        purged = await exchange("purge_tasks", inventory_digest=view.inventory_digest)
        assert purged.success and purged.task_total == 1
        assert len(purged.cancellation_receipts) == 49
        # Each purged request's requester is told, so a waiting chat thread is freed.
        assert sorted(cancelled) == sorted(f"queued_{index}" for index in range(49))
        assert not release.is_set()
        stopped = (await exchange("cancel") if is_team_lead else
                   await exchange("remove_task", target_task_id="held", inventory_digest=purged.inventory_digest))
        assert stopped.success
        assert cancelled == ["held"], "one final reply, from the stopped turn"
        assert agent.task_manager.is_execution_cancelled("held")
        assert agent._conversation_store.get_turn_by_task_id("held")["task_status"] == "cancelled"
        agent._conversation_summary_generator.summarize_turn.assert_not_called()
        agent._conversation_compactor.compact_if_needed.assert_not_called()
        assert (await exchange("list_tasks")).task_total == 0
        if agent._pending_work_task is not None:
            await asyncio.wait_for(asyncio.shield(agent._pending_work_task), 3)
            assert not agent._pending_work_task.cancelled()
        assert adapter.connected
    finally:
        agent._should_exit.set()
        release.set()
        pending = getattr(agent, "_pending_work_task", None)
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await adapter.close()
