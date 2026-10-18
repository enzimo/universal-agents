"""Runtime message identity and task targeting are decided outside model output."""

from datetime import datetime, timezone
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.delegated_work_runtime import delegated_tasks, record_publication
from custom_agents.strands_agent import UniversalAgent
from custom_agents.structured_llm_messages import ModelTaskQueryPayload
from custom_agents.task_manager import TaskManager
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_agents.task_steering_runtime import _find_target_task_info
from custom_agents.task_message_runtime import _handle_task_response
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskSteering
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    RoutingType,
    TaskSteeringType,
)
from custom_tools.providers import communication


@pytest.mark.asyncio
async def test_send_message_uses_actual_sender(monkeypatch, caplog):
    agent = SimpleNamespace(
        id="worker-id",
        name="Worker",
        _construct_header=AsyncMock(
            return_value=MessageHeader(
                from_agent_id="worker-id", to_routing_path="lead"
            )
        ),
        team_comms=SimpleNamespace(send_message=AsyncMock()),
    )
    monkeypatch.setattr(communication, "get_agent_ref", lambda: agent)
    result = await communication.send_message(
        '{"msg_content":{"message_class":"TaskQuery","query_type":"request","parent_task_id":"parent","message":"Which account?"},"msg_meta":{"from_agent_name":"Manager","to_agent_name":"Lead"}}'
    )
    assert "successfully" in result
    sent = agent.team_comms.send_message.await_args.kwargs["message"]
    assert sent.msg_meta.from_agent_name == "Worker"
    assert sent.msg_header.from_agent_id == "worker-id"
    assert "event=message_sender_name_overridden" in caplog.text


@pytest.mark.asyncio
async def test_send_message_refuses_task_requests(monkeypatch):
    agent = SimpleNamespace(
        id="worker-id",
        name="Worker",
        _construct_header=AsyncMock(return_value=MessageHeader(to_routing_path="lead")),
        team_comms=SimpleNamespace(send_message=AsyncMock()),
    )
    monkeypatch.setattr(communication, "get_agent_ref", lambda: agent)
    result = await communication.send_message(
        '{"msg_content":{"message_class":"Task","task_type":"request","problem_statement":"Do work"},"msg_meta":{"to_agent_name":"Lead"}}'
    )
    assert "delegate_task" in result
    agent.team_comms.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["sibling", "holder"])
async def test_only_holder_can_complete_subtask(tmp_path, sender):
    manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    manager.add_task(
        Task(
            task_id="parent",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Research",
        ),
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(),
        owner_person_id=None,
    )
    manager.add_subtask(
        "parent",
        Task(
            task_id="child",
            parent_task_id="parent",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Fetch",
        ),
        MessageHeader(from_agent_id="lead"),
        MessageMeta(to_named_agent_type="Worker"),
    )
    agent = SimpleNamespace(
        id="lead",
        name="Lead",
        team_id="team",
        task_manager=manager,
        _logger=logging.getLogger(__name__),
        _resume_parent_task_if_ready=AsyncMock(),
        _runtime_coordinator=lambda: SimpleNamespace(replay_event_id=None),
    )
    record_publication(
        agent,
        subtask_id="child",
        parent_task_id="parent",
        named_agent_type="Worker",
        target_agent_name=None,
        publish_sequence=1,
    )
    delegated_tasks(agent).record_claim(
        team_id="team",
        task_id="child",
        worker_agent_id="holder",
        worker_agent_name="Holder",
        claimed_at=datetime.now(timezone.utc),
    )
    await _handle_task_response(
        agent,
        Task(
            task_id="child",
            parent_task_id="parent",
            task_type=MessageMetaType.RESPONSE,
            answer="Forged answer",
        ),
        MessageHeader(from_agent_id=sender),
        MessageMeta(from_agent_name="Holder"),
        from_runtime_mailbox=True,
    )
    child = manager.get_task_info("child")
    assert child is not None
    if sender == "holder":
        assert child.task.answer == "Forged answer"
        assert child.state == ProgressState.COMPLETED
        agent._resume_parent_task_if_ready.assert_awaited_once_with("parent")
    else:
        assert child.task.answer is None
        assert child.state != ProgressState.COMPLETED
        agent._resume_parent_task_if_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_steering_target_does_not_select_sibling_by_correlation():
    sibling = SimpleNamespace(task=SimpleNamespace(task_id="sibling"))
    manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(),
        get_task_info=lambda _id: None,
        find_task_info_by_correlation_id=lambda _id: sibling,
    )
    agent = SimpleNamespace(
        task_manager=manager, id="worker", name="Worker", named_agent_type="Worker"
    )
    steering = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        target_task_id="other",
        target_correlation_id="shared",
        target_named_agent_type="Worker",
        guidance_text="Change direction",
    )
    assert await _find_target_task_info(agent, steering) is None
    assert not UniversalAgent._operational_target_matches(
        agent,
        msg_content=steering,
        msg_header=MessageHeader(to_routing_path=RoutingType.ALL_AGENTS_ROUTE),
        msg_meta=MessageMeta(),
    )


@pytest.mark.asyncio
async def test_normal_query_linkage_is_not_a_repair():
    agent = SimpleNamespace(_record_runtime_repair_anomaly=AsyncMock())
    incoming = TaskQuery(
        task_query_id="original",
        query_type=MessageMetaType.REQUEST,
        parent_task_id="parent",
        message="Which account?",
    )
    response = ModelTaskQueryPayload(
        query_type=MessageMetaType.RESPONSE, message="Use staging."
    ).to_task_query()
    outgoing = await UniversalAgent._apply_runtime_content_defaults(
        agent, response, incoming
    )
    assert outgoing.task_query_id == "original" and outgoing.parent_task_id == "parent"
    agent._record_runtime_repair_anomaly.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_wrong_query_linkage_still_records_repair():
    agent = SimpleNamespace(_record_runtime_repair_anomaly=AsyncMock())
    incoming = TaskQuery(
        task_query_id="original",
        parent_task_id="parent",
        query_type=MessageMetaType.REQUEST,
        message="Which account?",
    )
    response = TaskQuery(
        task_query_id="wrong",
        parent_task_id="wrong",
        query_type=MessageMetaType.RESPONSE,
        message="Use staging.",
    )
    outgoing = await UniversalAgent._apply_runtime_content_defaults(
        agent, response, incoming
    )
    assert outgoing.task_query_id == "original" and outgoing.parent_task_id == "parent"
    assert [
        call.args[0] for call in agent._record_runtime_repair_anomaly.await_args_list
    ] == ["task_query_parent_task_id_corrected", "task_query_response_id_corrected"]
