"""Runtime message identity and task targeting are decided outside model output."""

from datetime import datetime, timezone
import logging
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.delegated_work_runtime import delegated_tasks, record_publication
from custom_agents.strands_agent import UniversalAgent
from custom_agents.structured_llm_messages import ModelTaskQueryPayload
from custom_agents.task_manager import TaskManager
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_agents.task_steering_runtime import _find_target_task_info
from custom_agents.task_steering_runtime import handle_task_steering_message
from custom_agents.task_message_runtime import _handle_task_response
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskSteering
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    RoutingType,
    TaskSteeringType,
    TaskSteeringStatus,
    TaskSteeringStatusCode,
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
@pytest.mark.parametrize("worker_finished", [False, True])
async def test_result_survives_lost_claim_with_real_worker_custody(tmp_path, worker_finished, caplog):
    path = str(tmp_path / "runtime.db")
    manager = TaskManager(db_path=path)
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    manager.add_task(Task(task_id="parent", task_type=MessageMetaType.REQUEST), MessageHeader(), MessageMeta(), owner_person_id=None)
    child = Task(task_id="child", parent_task_id="parent", task_type=MessageMetaType.REQUEST)
    manager.add_subtask("parent", child, MessageHeader(from_agent_id="lead"), MessageMeta(to_named_agent_type="Worker"))
    lead = SimpleNamespace(
        id="lead", name="Lead", team_id="team", task_manager=manager,
        _logger=logging.getLogger(__name__), _resume_parent_task_if_ready=AsyncMock(),
        _runtime_coordinator=lambda: SimpleNamespace(replay_event_id=None),
    )
    record_publication(lead, subtask_id="child", parent_task_id="parent", named_agent_type="Worker", target_agent_name=None, publish_sequence=1)
    worker = TaskManager(db_path=path)
    worker.set_owner_identity("worker", "Worker", "team", "Worker")
    assert worker.add_task(child.model_copy(deep=True), MessageHeader(from_agent_id="lead"), MessageMeta(), owner_person_id=None)
    if worker_finished:
        worker.complete_task("child", datetime.now(timezone.utc).isoformat())
        assert worker.get_task_from_db("child") is not None
    # No claim reached the lead. The worker's transactional lifecycle records
    # still prove which process may report this task's result.
    await _handle_task_response(
        lead, Task(task_id="child", parent_task_id="parent", task_type=MessageMetaType.RESPONSE, answer="Finished safely."),
        MessageHeader(from_agent_id="worker"), MessageMeta(), from_runtime_mailbox=True,
    )
    assert manager.get_task_info("child").state == ProgressState.COMPLETED
    assert manager.get_task_info("child").task.answer == "Finished safely."
    lead._resume_parent_task_if_ready.assert_awaited_once_with("parent")
    assert "event=delegated_task_claim_missing_at_result" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_finished", [False, True])
async def test_stale_claim_cannot_authorize_result_after_custody_moved(tmp_path, worker_finished):
    from custom_agents.delegated_work_runtime import handle_delegated_task_claimed, task_response_matches_holder
    from custom_messages.contracts.delegated_work_v1 import DelegatedTaskClaimedV1

    manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    manager.add_task(Task(task_id="parent", task_type=MessageMetaType.REQUEST), MessageHeader(), MessageMeta(), owner_person_id=None)
    child = Task(task_id="child", parent_task_id="parent", task_type=MessageMetaType.REQUEST)
    manager.add_subtask("parent", child, MessageHeader(), MessageMeta(to_named_agent_type="Worker"))
    lead = SimpleNamespace(id="lead", team_id="team", task_manager=manager, _logger=logging.getLogger(__name__))
    record_publication(lead, subtask_id="child", parent_task_id="parent", named_agent_type="Worker", target_agent_name=None, publish_sequence=1)
    delegated_tasks(lead).record_claim(team_id="team", task_id="child", worker_agent_id="old_worker", worker_agent_name="Old worker", claimed_at=datetime.now(timezone.utc))
    worker = TaskManager(db_path=manager._db_path)
    worker.set_owner_identity("current_worker", "Current worker", "team", "Worker")
    assert worker.add_task(child.model_copy(deep=True), MessageHeader(), MessageMeta(), owner_person_id=None)
    if worker_finished:
        worker.complete_task("child", datetime.now(timezone.utc).isoformat())
    await handle_delegated_task_claimed(
        lead,
        claim=DelegatedTaskClaimedV1(
            claim_id="late_claim", team_id="team", task_id="child", parent_task_id="parent",
            worker_agent_id="old_worker", claimed_at_utc=datetime.now(timezone.utc),
        ),
        msg_header=MessageHeader(from_agent_id="old_worker"), msg_meta=MessageMeta(),
    )
    assert not task_response_matches_holder(lead, manager.get_task_info("child"), child, "old_worker")
    assert task_response_matches_holder(lead, manager.get_task_info("child"), child, "current_worker")


@pytest.mark.asyncio
async def test_pre_admission_error_retries_its_claim_and_retains_out_of_order_result(tmp_path):
    from custom_agents.delegated_work_runtime import DelegatedTaskClaimPending, handle_delegated_task_claimed
    from custom_messages.enums import RequesterType

    manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    manager.add_task(Task(task_id="parent", task_type=MessageMetaType.REQUEST), MessageHeader(), MessageMeta(), owner_person_id=None)
    child = Task(task_id="child", parent_task_id="parent", task_type=MessageMetaType.REQUEST)
    manager.add_subtask("parent", child, MessageHeader(), MessageMeta(to_named_agent_type="Worker"))
    lead = SimpleNamespace(
        id="lead", name="Lead", team_id="team", task_manager=manager,
        _logger=logging.getLogger(__name__), _resume_parent_task_if_ready=AsyncMock(),
        _runtime_coordinator=lambda: SimpleNamespace(replay_event_id=None),
    )
    record_publication(lead, subtask_id="child", parent_task_id="parent", named_agent_type="Worker", target_agent_name=None, publish_sequence=1)
    published = []
    fail_claim = True

    async def publish(*, message, **kwargs):
        nonlocal fail_claim
        if fail_claim:
            fail_claim = False
            raise TimeoutError("Claim publication failed.")
        published.append(message)

    worker_manager = TaskManager(db_path=manager._db_path)
    worker_manager.set_owner_identity("worker", "Worker", "team", "Worker")
    worker = SimpleNamespace(
        id="worker", name="Worker", named_agent_type="Worker", team_id="team",
        _agent_id_by_name={}, named_agent_type_queue_map={}, _logger=logging.getLogger(__name__),
        _record_runtime_repair_anomaly=AsyncMock(), _extract_team_id_from_routing_value=lambda _: None,
        team_comms=SimpleNamespace(send_message=AsyncMock(side_effect=publish)),
        task_manager=worker_manager,
    )
    args = (child, MessageHeader(from_agent_id="lead"), MessageMeta(requester_type=RequesterType.AGENT_REQUESTER, is_response_requested=True), "The worker could not admit this task.")
    with pytest.raises(TimeoutError):
        await UniversalAgent._send_task_error_response(worker, *args)
    assert published == []
    await UniversalAgent._send_task_error_response(worker, *args)
    assert len(published) == 2
    claim, result = published
    assert claim.msg_header.to_routing_path == result.msg_header.to_routing_path == "lead"
    # A claim handler can retry after a later result has already arrived.
    with pytest.raises(DelegatedTaskClaimPending):
        await _handle_task_response(lead, result.msg_content, result.msg_header, result.msg_meta, from_runtime_mailbox=True)
    assert manager.get_task_info("child").state == ProgressState.ACCEPTED
    await handle_delegated_task_claimed(lead, claim=claim.msg_content, msg_header=claim.msg_header, msg_meta=claim.msg_meta)
    await _handle_task_response(lead, result.msg_content, result.msg_header, result.msg_meta, from_runtime_mailbox=True)
    assert manager.get_task_info("child").state == ProgressState.FAILED
    lead._resume_parent_task_if_ready.assert_awaited_once_with("parent")


@pytest.mark.asyncio
@pytest.mark.parametrize("finished", [False, True])
async def test_admitted_error_needs_no_claim_to_reach_requester(tmp_path, finished):
    from custom_messages.contracts.delegated_work_v1 import DelegatedTaskClaimedV1
    from custom_messages.enums import RequesterType

    manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    manager.set_owner_identity("worker", "Worker", "team", "Worker")
    task = Task(task_id="child", parent_task_id="parent", task_type=MessageMetaType.REQUEST)
    header = MessageHeader(from_agent_id="lead", response_routing_key="lead")
    meta = MessageMeta(requester_type=RequesterType.AGENT_REQUESTER, is_response_requested=True)
    assert manager.add_task(task, header, meta, owner_person_id=None)
    if finished:
        manager.fail_task("child", "The model failed.", datetime.now(timezone.utc).isoformat())
        assert manager.get_task_from_db("child")["status"] == "failed"
    published = []

    async def publish(*, message, **kwargs):
        if isinstance(message.msg_content, DelegatedTaskClaimedV1):
            raise TimeoutError("Claim transport unavailable.")
        published.append(message)

    worker = SimpleNamespace(
        id="worker", name="Worker", named_agent_type="Worker", team_id="team", task_manager=manager,
        _agent_id_by_name={}, named_agent_type_queue_map={}, _logger=logging.getLogger(__name__),
        _record_runtime_repair_anomaly=AsyncMock(), _extract_team_id_from_routing_value=lambda _: None,
        team_comms=SimpleNamespace(send_message=AsyncMock(side_effect=publish)),
    )
    await UniversalAgent._send_task_error_response(worker, task, header, meta, "The model failed.")
    assert len(published) == 1
    assert published[0].msg_content.errors[-1] == "The model failed."


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
async def test_addressed_manager_returns_stale_for_missing_steering_task():
    sent = AsyncMock()
    sibling = SimpleNamespace(task=SimpleNamespace(task_id="sibling"))
    agent = SimpleNamespace(
        id="lead", name="Lead", team_id="team", named_agent_type="TeamArchitect",
        task_manager=SimpleNamespace(
            operations_runtime=TaskOperationRuntime(),
            get_task_info=lambda _: None,
            find_task_info_by_correlation_id=lambda _: sibling,
        ),
        mid_turn_steering={"enabled": True},
        _message_targets_other_team=lambda _: False,
        _is_operational_content=UniversalAgent._is_operational_content,
        _agent_id_by_name={}, named_agent_type_queue_map={},
        team_comms=SimpleNamespace(send_message=sent), _logger=logging.getLogger(__name__),
    )
    agent._operational_target_matches = MethodType(UniversalAgent._operational_target_matches, agent)

    async def dispatch(*, msg_content, msg_header, msg_meta):
        await handle_task_steering_message(agent, msg_content, msg_header, msg_meta)

    agent._dispatch_message = dispatch
    delivery = SimpleNamespace(
        envelope=MessageEnvelope(
            msg_content=TaskSteering(
                steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
                target_task_id="finished", target_correlation_id="shared",
                target_agent_id="lead", guidance_text="Use the revised budget.",
            ),
            msg_header=MessageHeader(from_agent_id="gateway", to_routing_path="lead", response_routing_key="gateway"),
            msg_meta=MessageMeta(is_response_requested=True),
        ),
        ack=AsyncMock(),
    )
    await UniversalAgent.on_nats_operational_delivery(agent, delivery)
    sent.assert_awaited_once()
    response = sent.await_args.kwargs["message"].msg_content
    assert response.status == TaskSteeringStatus.STALE
    assert response.status_code == TaskSteeringStatusCode.TARGET_TASK_NOT_FOUND
    delivery.ack.assert_awaited_once()


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
