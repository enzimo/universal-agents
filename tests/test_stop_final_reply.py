"""A stopped request gets one final reply even when its turn finishes cleanup late."""

import asyncio
import logging
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import control_runtime
from custom_agents.strands_agent import UniversalAgent
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from tests.task_work_support import TurnHost, register_turn


@pytest.fixture
def agent(tmp_path):
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    agent = TurnHost(
        id="lead", name="Lead", team_id="team", is_team_lead=True,
        task_manager=manager, team_comms=SimpleNamespace(send_message=AsyncMock()),
        _logger=logging.getLogger(__name__), _stop_reasons_by_task_id={},
        _finalize_conversation_turn=AsyncMock(), named_agent_type_queue_map={}, _agent_id_by_name={},
    )
    agent._consume_stop_reason = lambda task_id: agent._stop_reasons_by_task_id.pop(task_id, None)
    # The agent's real wrapper, not a stand-in that takes any keyword: the
    # handler's keywords must match what the agent accepts (#79's send_response).
    agent._cancel_task_tree_locally = MethodType(UniversalAgent._cancel_task_tree_locally, agent)
    assert manager.add_task(
        Task(task_id="request", task_type=MessageMetaType.REQUEST, problem_statement="work"),
        MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
        MessageMeta(gateway_context=GatewayContext(canonical_user_id="person")), owner_person_id="person",
    )
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("late", ["child", "handler", "handler_subtask", "neither"])
@pytest.mark.parametrize("send_response", [True, False])
async def test_explicit_stop_owns_the_reply_when_cleanup_is_late(agent, monkeypatch, late, send_response, caplog):
    started, release = asyncio.Event(), asyncio.Event()
    info = agent.task_manager.get_task_info("request")
    requested = info
    if late == "handler_subtask":
        assert agent.task_manager.add_task(
            Task(task_id="step", parent_task_id="request", task_type=MessageMetaType.REQUEST),
            MessageHeader(from_agent_id="lead", response_routing_key="different_route"),
            MessageMeta(), owner_person_id="person",
        )
        requested = agent.task_manager.get_task_info("step")

    async def slow_turn():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if late == "child":
                await release.wait()
            raise

    turn = asyncio.create_task(slow_turn())
    register_turn(agent, "request", turn)

    async def handling_owner():
        try:
            await turn
        except asyncio.CancelledError:
            if late.startswith("handler"):
                await release.wait()
            await control_runtime.handle_interrupted_turn(agent, task_id="request", task_info=info)

    owner = asyncio.create_task(handling_owner())
    try:
        await started.wait()
        monkeypatch.setattr(control_runtime, "ACTIVE_STOP_GRACE_SECONDS", 0.01)
        result = await control_runtime.stop_task_execution(
            agent, task_info=requested, reason="stop", cascade_to_subtasks=False, send_response=send_response,
        )
        assert result["durable_cancellation_recorded"] and result["active_execution_cancelled"]
        assert agent.task_manager.get_task_info("request") is None
        assert agent.task_manager.is_execution_cancelled("request")
        assert agent.team_comms.send_message.await_count == int(send_response)
        if late == "child":
            assert "event=active_turn_stop_timeout" in caplog.text
    finally:
        release.set()
        await asyncio.wait_for(owner, 5)
    assert agent.team_comms.send_message.await_count == int(send_response)
    agent._finalize_conversation_turn.assert_awaited_once()
    if send_response:
        message = agent.team_comms.send_message.call_args.kwargs["message"]
        assert message.msg_content.task_id == "request"
        assert message.msg_content.task_state[-1][0] == ProgressState.CANCELLED
        assert agent.team_comms.send_message.call_args.kwargs["routing_key"] == "gateway"


@pytest.mark.asyncio
@pytest.mark.parametrize("send_response", [True, False])
async def test_agent_cancel_wrapper_passes_the_reply_choice_on(agent, send_response):
    info = agent.task_manager.get_task_info("request")
    result = await agent._cancel_task_tree_locally(
        task_info=info, reason="stop", signal_pending_subtasks=False, send_response=send_response,
    )
    assert result["cancelled_task_count"] == 1
    assert agent.task_manager.get_task_info("request") is None
    assert agent.team_comms.send_message.await_count == int(send_response)


@pytest.mark.asyncio
async def test_interrupt_without_an_explicit_stop_still_cancels_and_replies(agent):
    info = agent.task_manager.get_task_info("request")
    await control_runtime.handle_interrupted_turn(agent, task_id="request", task_info=info)
    assert agent.task_manager.get_task_info("request") is None
    agent.team_comms.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_can_retry_after_failure(agent):
    info = agent.task_manager.get_task_info("request")
    agent._finalize_conversation_turn.side_effect = [RuntimeError("injected storage failure"), None]
    with pytest.raises(RuntimeError, match="injected storage failure"):
        await control_runtime.cancel_task_tree_locally(
            agent, task_info=info, reason="stop", signal_pending_subtasks=False,
        )
    assert agent.task_manager.get_task_info("request") is not None
    agent.team_comms.send_message.assert_not_awaited()
    await control_runtime.cancel_task_tree_locally(
        agent, task_info=info, reason="stop", signal_pending_subtasks=False,
    )
    assert agent.task_manager.get_task_info("request") is None
    agent.team_comms.send_message.assert_awaited_once()
