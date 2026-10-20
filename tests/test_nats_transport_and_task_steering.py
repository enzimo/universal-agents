from __future__ import annotations

from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_agents.task_runtime_state import TaskRuntimeState

from datetime import datetime
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_agents import control_runtime, task_message_runtime
from custom_agents.task_manager import TaskInfo
from custom_agents.task_steering_runtime import (
    handle_task_steering_message,
    StrandsInterruptedTurnSession,
    _build_interrupt_response_blocks,
    _send_steering_status_response,
    ensure_fresh_strands_agent_for_new_turn,
    send_interrupt_request_for_result,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import (
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.replied_message_v1 import RepliedMessage
from custom_messages.contracts.task_v1 import (
    Task,
    TaskSteering,
    TaskSteeringInterruptRecord,
)
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    RequesterType,
    RoutingType,
    TaskSteeringStatus,
    TaskSteeringStatusCode,
    TaskSteeringType,
)
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.subjects import NatsSubjectMapper


@pytest.mark.asyncio
async def test_steering_status_retries_keep_effect_identity_and_publish_changed_outcomes() -> None:
    agent = SimpleNamespace(
        id="manager_1",
        name="Manager",
        team_id="team_1",
        _agent_id_by_name={},
        named_agent_type_queue_map={},
        _logger=MagicMock(),
        team_comms=SimpleNamespace(send_message=AsyncMock()),
    )
    guidance = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        target_task_id="task_report",
        target_correlation_id="corr_report",
        guidance_text="Use the June report",
        idempotency_key="guidance_1",
    )
    for status, code in [
        (TaskSteeringStatus.STALE, TaskSteeringStatusCode.TARGET_TASK_NOT_FOUND),
        (TaskSteeringStatus.STALE, TaskSteeringStatusCode.TARGET_TASK_NOT_FOUND),
        (TaskSteeringStatus.ACCEPTED, None),
        (TaskSteeringStatus.ACCEPTED, None),
        (TaskSteeringStatus.STALE, TaskSteeringStatusCode.TARGET_TASK_TERMINAL),
    ]:
        await _send_steering_status_response(
            agent,
            guidance,
            MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
            MessageMeta(msg_correlation_id="guidance_1", is_response_requested=True),
            status=status,
            status_code=code,
            reason="Synthetic status",
        )
    messages = [call.kwargs["message"] for call in agent.team_comms.send_message.await_args_list]
    keys = [message.msg_header.idempotency_key for message in messages]
    assert keys[0] == keys[1]
    assert keys[2] == keys[3]
    assert len({keys[0], keys[2], keys[4]}) == 3
    assert {message.msg_content.idempotency_key for message in messages} == {"guidance_1"}
    assert {message.msg_meta.msg_correlation_id for message in messages} == {"guidance_1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_route", ["gateway", "agent_requester", None])
async def test_input_interrupt_needs_an_external_requester_route(reply_route) -> None:
    """A turn paused for input (not an access request) asks its requester, if it has one."""

    task = Task(
        task_id="task_input_interrupt",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Ask which account to use.",
    )
    task_manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(),
        _persist_runtime_state=lambda: None,
        append_history_entry=MagicMock(),
        update_main_task_state=MagicMock(),
        task_state=TaskRuntimeState(
            tasks={task.task_id: object()}, human_interaction_blockers={}
        ),
    )
    agent = SimpleNamespace(
        id="agent_requester",
        name="Requester",
        named_agent_type="TeamArchitect",
        team_id="team_1",
        task_manager=task_manager,
        _interrupted_turn_sessions={},
        _strands_agent_replacement_required=False,
        _agent_id_by_name={},
        named_agent_type_queue_map={},
        _logger=MagicMock(),
        team_comms=SimpleNamespace(send_message=AsyncMock()),
    )
    result = SimpleNamespace(
        interrupts=[
            SimpleNamespace(id="interrupt_input", name="ask_person", reason={"kind": "question"})
        ]
    )

    invocation = send_interrupt_request_for_result(
        agent,
        result=result,
        task=task,
        msg_header=MessageHeader(
            from_agent_id=reply_route,
            response_routing_key=reply_route,
        ),
        msg_meta=MessageMeta(
            msg_correlation_id="corr_input",
            conversation_id="conversation_input",
        ),
        strands_agent=object(),
        invoke_kwargs={},
        callback_handler=lambda **_: None,
        full_prompt="prompt",
    )

    if reply_route != "gateway":
        with pytest.raises(RuntimeError, match="No input request was delivered"):
            await invocation
        assert not agent._interrupted_turn_sessions
        assert not task_manager.task_state.human_interaction_blockers
        task_manager.update_main_task_state.assert_not_called()
        agent.team_comms.send_message.assert_not_awaited()
        return
    steering = await invocation

    # Only an access request records a blocker; this one waits on the requester.
    assert not task_manager.task_state.human_interaction_blockers
    sent = agent.team_comms.send_message.await_args.kwargs
    assert sent["routing_key"] == "gateway"
    assert sent["message"].msg_content.steering_id == steering.steering_id
    assert task.task_id in agent._interrupted_turn_sessions


def test_new_turn_rotates_away_from_live_interrupted_strands_agent() -> None:
    interrupted_agent = object()
    replacement_agent = object()
    session = StrandsInterruptedTurnSession(
        task_id="task_waiting",
        strands_agent=interrupted_agent,
        invoke_kwargs={},
        callback_handler=lambda **_: None,
        full_prompt="prompt",
        msg_content=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="wait for permission",
        ),
        msg_header=MessageHeader(from_agent_id="gateway"),
        msg_meta=MessageMeta(msg_correlation_id="corr_waiting"),
        created_at="2026-08-19T00:00:00+00:00",
        interrupts=[TaskSteeringInterruptRecord(interrupt_id="interrupt_waiting")],
    )
    agent = SimpleNamespace(
        id="manager",
        _strands_agent=interrupted_agent,
        _interrupted_turn_sessions={"task_waiting": session},
        _strands_agent_replacement_required=True,
        _logger=MagicMock(),
    )

    def create_replacement() -> None:
        agent._strands_agent = replacement_agent

    agent._create_strands_agent = create_replacement

    assert ensure_fresh_strands_agent_for_new_turn(
        agent,
        task_id="task_new",
    )
    assert agent._strands_agent is replacement_agent
    assert agent._interrupted_turn_sessions["task_waiting"].strands_agent is (
        interrupted_agent
    )
    assert agent._strands_agent_replacement_required is False


@pytest.mark.asyncio
async def test_stop_cancellation_discards_live_interrupt_session(monkeypatch) -> None:
    task_id = "task_stopped"
    task_info = TaskInfo(
        Task(
            task_id=task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="stop this task",
        ),
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(msg_correlation_id="corr_stopped"),
        owner_person_id=None,
    )
    task_info.update_state(ProgressState.WAITING, "2026-08-19T00:00:00+00:00")
    interrupted_agent = object()
    session = StrandsInterruptedTurnSession(
        task_id=task_id,
        strands_agent=interrupted_agent,
        invoke_kwargs={},
        callback_handler=lambda **_: None,
        full_prompt="prompt",
        msg_content=task_info.task,
        msg_header=task_info.header,
        msg_meta=task_info.meta,
        created_at="2026-08-19T00:00:00+00:00",
        interrupts=[TaskSteeringInterruptRecord(interrupt_id="interrupt_stopped")],
    )
    task_manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(),
        _persist_runtime_state=lambda: None,
        resolve_root_task_id=lambda candidate: candidate,
        get_execution_graph_task_infos=lambda candidate: (
            [task_info] if candidate == task_id else []
        ),
        get_parent_task_id=lambda _task_id: None,
        is_terminal_state=lambda state: (
            state
            in {ProgressState.COMPLETED, ProgressState.FAILED, ProgressState.CANCELLED}
        ),
        get_pending_subtasks=lambda _task_id: [],
        get_subtasks=lambda _task_id: [],
        get_task_info=lambda candidate: task_info if candidate == task_id else None,
        append_history_entry=MagicMock(),
        cancel_task=MagicMock(),
        task_state=TaskRuntimeState(human_interaction_blockers={}),
    )
    agent = SimpleNamespace(
        id="manager",
        task_manager=task_manager,
        _strands_agent=interrupted_agent,
        _interrupted_turn_sessions={task_id: session},
        _pending_interrupt_responses={},
        _pending_proactive_task_steering={},
        _strands_agent_replacement_required=False,
        _finalize_conversation_turn=AsyncMock(),
        _logger=MagicMock(),
    )
    send_cancel = AsyncMock()
    monkeypatch.setattr(control_runtime, "send_task_cancel_response", send_cancel)

    result = await control_runtime.cancel_task_tree_locally(
        agent,
        task_info=task_info,
        reason="Stop requested by user.",
        signal_pending_subtasks=True,
    )

    assert result["cancelled_task_count"] == 1
    assert agent._interrupted_turn_sessions == {}
    assert agent._strands_agent_replacement_required is True
    task_manager.cancel_task.assert_called_once()
    assert task_manager.cancel_task.call_args.args[:2] == (
        task_id,
        "Stop requested by user.",
    )
    datetime.fromisoformat(task_manager.cancel_task.call_args.args[2])
    send_cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_nats_transport_close_suppresses_expected_shutdown_connection_loss(
    caplog,
) -> None:
    class FakeNatsConnection:
        is_closed = False
        is_reconnecting = False
        is_connecting = False

        def __init__(self) -> None:
            self.options = {"allow_reconnect": True}
            self.close_called = False

        async def drain(self) -> None:
            raise ConnectionResetError("Connection lost")

        async def close(self) -> None:
            self.close_called = True

    logger = logging.getLogger("tests.nats.shutdown")
    adapter = NatsTransportAdapter(
        config=NatsTransportConfig(),
        mapper=NatsSubjectMapper(team_id="team_303"),
        logger=logger,
    )
    connection = FakeNatsConnection()
    adapter._nc = connection

    with caplog.at_level(logging.WARNING, logger=logger.name):
        await adapter.close()

    assert connection.close_called is True
    assert connection.options["allow_reconnect"] is False
    assert adapter.connected is False
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]


@pytest.mark.asyncio
async def test_nats_transport_error_callback_logs_connection_loss_outside_shutdown(
    caplog,
) -> None:
    logger = logging.getLogger("tests.nats.runtime")
    adapter = NatsTransportAdapter(
        config=NatsTransportConfig(),
        mapper=NatsSubjectMapper(team_id="team_303"),
        logger=logger,
    )

    with caplog.at_level(logging.ERROR, logger=logger.name):
        await adapter._on_nats_error(ConnectionRefusedError("NATS unavailable"))

    assert "NATS client error" in caplog.text


@pytest.mark.asyncio
async def test_nats_transport_error_callback_suppresses_connection_loss_during_shutdown(
    caplog,
) -> None:
    logger = logging.getLogger("tests.nats.callback_shutdown")
    adapter = NatsTransportAdapter(
        config=NatsTransportConfig(),
        mapper=NatsSubjectMapper(team_id="team_303"),
        logger=logger,
    )
    adapter._closing = True

    with caplog.at_level(logging.WARNING, logger=logger.name):
        await adapter._on_nats_error(
            ConnectionRefusedError("NATS unavailable during shutdown")
        )

    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]


@pytest.mark.asyncio
async def test_missing_task_steering_returns_structured_stale_status() -> None:
    send_message = AsyncMock()

    agent = SimpleNamespace(
        id="manager",
        name="manager-name",
        team_id="team_303",
        mid_turn_steering={"enabled": True},
        task_manager=SimpleNamespace(
            operations_runtime=TaskOperationRuntime(),
            get_task_info=lambda _task_id: None,
            find_task_info_by_correlation_id=lambda _correlation_id: None,
        ),
        _agent_id_by_name={},
        named_agent_type_queue_map={},
        team_comms=SimpleNamespace(send_message=send_message),
        _logger=logging.getLogger("tests.task_steering.missing"),
    )

    await handle_task_steering_message(
        agent,
        TaskSteering(
            steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
            target_task_id="task_already_archived",
            target_correlation_id="corr_already_closed",
            guidance_text="restart yourself",
        ),
        MessageHeader(
            from_agent_id="gateway",
            response_routing_key="gateway_response",
        ),
        MessageMeta(
            msg_correlation_id="corr_stale_steering",
            requester_type=RequesterType.USER_REQUESTER,
            is_response_requested=True,
        ),
    )

    send_message.assert_awaited_once()
    status = send_message.await_args.kwargs["message"].msg_content
    assert isinstance(status, TaskSteering)
    assert status.status == TaskSteeringStatus.STALE
    assert status.status_code == TaskSteeringStatusCode.TARGET_TASK_NOT_FOUND
    assert status.status_reason == "Target task was not found."


def test_interrupt_response_blocks_fill_missing_interrupt_ids_from_live_session() -> (
    None
):
    interrupt = TaskSteeringInterruptRecord(interrupt_id="intr_1", name="approval")
    session = StrandsInterruptedTurnSession(
        task_id="task_123",
        strands_agent=object(),
        invoke_kwargs={},
        callback_handler=lambda **_: None,
        full_prompt="prompt",
        msg_content=Task(
            task_type=MessageMetaType.REQUEST, problem_statement="do work"
        ),
        msg_header=MessageHeader(from_agent_id="agent_1", to_routing_path="agent_2"),
        msg_meta=MessageMeta(msg_correlation_id="corr_123"),
        created_at="2026-05-28T00:00:00+00:00",
        interrupts=[interrupt],
    )
    steering = TaskSteering(
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id="task_123",
        guidance_text="approved",
        interrupt_responses=[{"response": "approved"}],
    )

    blocks = _build_interrupt_response_blocks(session, steering)

    assert blocks == [
        {"interruptResponse": {"response": "approved", "interruptId": "intr_1"}}
    ]


@pytest.mark.asyncio
async def test_proactive_steering_for_waiting_parent_forwards_to_pending_subtask() -> (
    None
):
    parent_task_id = "task_parent"
    child_task_id = "task_child"
    parent_info = TaskInfo(
        Task(
            task_id=parent_task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Find soup recipes.",
        ),
        MessageHeader(from_agent_id="user", to_routing_path="manager"),
        MessageMeta(msg_correlation_id="corr_parent", conversation_id="conversation_1"),
        owner_person_id=None,
    )
    parent_info.update_state(ProgressState.WAITING, "2026-06-01T00:00:00+00:00")
    child_info = TaskInfo(
        Task(
            task_id=child_task_id,
            parent_task_id=parent_task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Research soup recipes.",
        ),
        MessageHeader(
            from_agent_id="manager", to_routing_path="team_303_WebResearchAgent"
        ),
        MessageMeta(
            msg_correlation_id="corr_parent",
            conversation_id="conversation_1",
            to_named_agent_type="WebResearchAgent",
        ),
        owner_person_id=None,
    )
    child_info.update_state(ProgressState.IN_PROGRESS, "2026-06-01T00:00:01+00:00")
    sent: list[MessageEnvelope] = []
    task_manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(),
        get_task_info=lambda task_id: (
            parent_info if task_id == parent_task_id else None
        ),
        find_task_info_by_correlation_id=lambda correlation_id: (
            parent_info if correlation_id == "corr_parent" else None
        ),
        is_terminal_state=lambda state: False,
        get_pending_subtasks=lambda task_id: (
            [child_info] if task_id == parent_task_id else []
        ),
        append_history_entry=MagicMock(),
    )

    async def send_operational_message(envelope: MessageEnvelope) -> None:
        sent.append(envelope)

    agent = SimpleNamespace(
        id="manager",
        name="manager-name",
        named_agent_type="TeamArchitect",
        team_id="team_303",
        mid_turn_steering={"enabled": True},
        task_manager=task_manager,
        _pending_proactive_task_steering={},
        _processed_task_steering_ids=set(),
        _agent_id_by_name={},
        named_agent_type_queue_map={"WebResearchAgent": "team_303_WebResearchAgent"},
        _send_operational_message=send_operational_message,
        _logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )
    steering = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        target_task_id=parent_task_id,
        target_correlation_id="corr_parent",
        conversation_id="conversation_1",
        guidance_text="Actually, find meatloaf recipes.",
        idempotency_key="corr_steering",
        # Sent as a reply to an earlier chat message: workers get it too.
        replied_message=RepliedMessage(author="assistant", text="Soup recipes found: 3."),
    )

    await handle_task_steering_message(
        agent,
        steering,
        MessageHeader(
            from_agent_id="gateway", to_routing_path=RoutingType.ALL_AGENTS_ROUTE
        ),
        MessageMeta(
            msg_correlation_id="corr_steering",
            conversation_id="conversation_1",
            requester_type=RequesterType.USER_REQUESTER,
            is_response_requested=False,
        ),
    )

    assert agent._pending_proactive_task_steering[parent_task_id] == [steering]
    assert len(sent) == 1
    forwarded = sent[0]
    assert isinstance(forwarded.msg_content, TaskSteering)
    assert forwarded.msg_content.target_task_id == child_task_id
    assert forwarded.msg_content.target_named_agent_type == "WebResearchAgent"
    assert forwarded.msg_content.guidance_text == "Actually, find meatloaf recipes."
    assert forwarded.msg_content.replied_message == steering.replied_message
    assert forwarded.msg_meta.to_named_agent_type == "WebResearchAgent"
    assert forwarded.msg_meta.is_response_requested is False
    assert forwarded.msg_header.to_routing_path == RoutingType.ALL_AGENTS_ROUTE


@pytest.mark.asyncio
async def test_terminal_task_response_is_deferred_when_pending_guidance_exists() -> (
    None
):
    task_id = "task_steer_late"
    task_manager = MagicMock()
    task_manager.operations_runtime = TaskOperationRuntime()
    task_manager.append_history_entry.return_value = True
    task_manager.append_runtime_resume_directive.return_value = None
    task_manager.update_main_task_state.return_value = None
    task_manager.enqueue_task.return_value = None
    task_manager.get_task_info.return_value = SimpleNamespace(task=Task(task_id=task_id, task_type=MessageMetaType.REQUEST))
    agent = SimpleNamespace(
        _pending_proactive_task_steering={
            task_id: [
                TaskSteering(
                    steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
                    target_task_id=task_id,
                    guidance_text="Use shrimp instead of crawfish.",
                    status=TaskSteeringStatus.ACCEPTED,
                )
            ]
        },
        task_manager=task_manager,
    )
    task_result = Task(
        task_type=MessageMetaType.RESPONSE, problem_statement="draft answer"
    )

    await task_message_runtime._apply_task_processing_result(
        agent,
        task_id,
        SimpleNamespace(
            task=Task(task_type=MessageMetaType.REQUEST, problem_statement="recipes")
        ),
        task_result,
    )

    task_manager.append_runtime_resume_directive.assert_called_once()
    directive = task_manager.append_runtime_resume_directive.call_args.args[1]
    assert "Use shrimp instead of crawfish." in directive
    task_manager.update_main_task_state.assert_called_once()
    assert (
        task_manager.update_main_task_state.call_args.args[1] == ProgressState.ACCEPTED
    )
    task_manager.enqueue_task.assert_called_once_with(task_id, front=True)
    assert agent._pending_proactive_task_steering == {}
