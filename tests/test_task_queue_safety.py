"""Execution readiness must survive an access-request resume and final review."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import logging

import pytest

from custom_agents.task_manager import TaskManager
from custom_agents.task_human_interaction_blockers import (
    record_human_interaction_blocker,
)
from custom_agents.task_steering_runtime import (
    StrandsInterruptedTurnSession,
    resume_interrupted_task_turn,
)
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import (
    Task,
    TaskSteering,
    TaskSteeringInterruptRecord,
)
from custom_messages.enums import (
    ExecutionPlanItemStatus,
    MessageMetaType,
    ProgressState,
    TaskSteeringType,
)
from custom_tools.tool_stop import ToolStop


@pytest.fixture
def manager(tmp_path):
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("manager", "Manager", "team", "TeamArchitect")
    return manager


def add_task(manager, task_id="audit"):
    task = Task(
        task_id=task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Audit schedules",
    )
    manager.add_task(
        task,
        MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
        MessageMeta(
            gateway_context=GatewayContext(
                canonical_user_id="alice", session_key="session_alice"
            )
        ),
        owner_person_id="alice",
    )
    return task


@pytest.mark.parametrize(
    "state",
    [
        ProgressState.WAITING,
        ProgressState.PAUSED,
        ProgressState.COMPLETED,
        ProgressState.FAILED,
        ProgressState.CANCELLED,
    ],
)
def test_nonrunnable_transition_removes_queue_entry_and_enqueue_refuses_it(
    manager, state
):
    task = add_task(manager)
    manager.update_main_task_state(
        task.task_id, state, datetime.now(timezone.utc).isoformat()
    )
    assert task.task_id not in manager.task_state.task_queue
    assert not manager.enqueue_task(task.task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_again", [False, True])
async def test_access_resume_clears_old_blocker_before_final_review(
    manager, monkeypatch, interrupt_again
):
    task = add_task(manager)
    manager.get_next_task_to_process()
    now = datetime.now(timezone.utc).isoformat()
    manager.update_main_task_state(task.task_id, ProgressState.WAITING, now)
    manager.upsert_execution_plan_item(
        task.task_id,
        title="Review remaining anomaly",
        rationale="Need truthful caveats",
        status=ExecutionPlanItemStatus.PLANNED,
    )
    record_human_interaction_blocker(
        manager,
        task.task_id,
        {
            "kind": "access_request",
            "access_request_id": "areq_old",
            "interrupt_id": "interrupt_old",
        },
    )

    async def invoke(*args, **kwargs):
        assert task.task_id not in manager.task_state.human_interaction_blockers
        return SimpleNamespace(
            stop_reason="interrupt" if interrupt_again else "end_turn"
        )

    async def next_interrupt(*args, **kwargs):
        record_human_interaction_blocker(
            manager,
            task.task_id,
            {
                "kind": "access_request",
                "access_request_id": "areq_new",
                "interrupt_id": "interrupt_new",
            },
        )
        manager.update_main_task_state(task.task_id, ProgressState.WAITING, now)

    monkeypatch.setattr(
        "custom_agents.task_steering_runtime.send_interrupt_request_for_result",
        next_interrupt,
    )

    sdk = SimpleNamespace(invoke_async=invoke)
    session = StrandsInterruptedTurnSession(
        task.task_id,
        sdk,
        {},
        lambda **_: None,
        "prompt",
        task,
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(),
        now,
        [
            TaskSteeringInterruptRecord(
                interrupt_id="interrupt_old",
                reason={"access_request_id": "areq_old"},
            )
        ],
    )
    steering = TaskSteering(
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id=task.task_id,
        interrupt_responses=[
            {
                "interrupt_id": "interrupt_old",
                "response": {
                    "access_request_id": "areq_old",
                    "status": "granted",
                },
            }
        ],
    )

    async def dispatch(**kwargs):
        assert manager.begin_final_review_if_needed(
            task.task_id, attempted_answer="Audit complete with caveats"
        )
        manager.project_task_state_after_batch(
            task.task_id, batch_id="batch_review", reason="Resumed output committed"
        )
        return Task(
            task_id=task.task_id,
            task_type=MessageMetaType.RESPONSE,
            answer="Audit complete with caveats",
        )

    agent = SimpleNamespace(
        id="manager",
        name="Manager",
        task_manager=manager,
        _interrupted_turn_sessions={task.task_id: session},
        _pending_interrupt_responses={task.task_id: steering},
        _strands_agent=sdk,
        _strands_agent_replacement_required=True,
        _set_active_execution_context=lambda task_id: ToolStop(agent_id="manager", task_id=task_id),
        _clear_active_execution_context=lambda: None,
        _logger=logging.getLogger(__name__),
        _refresh_self_agent_state=AsyncMock(),
        _apply_task_processing_result=AsyncMock(),
        _execute_turn_decision_outbox=dispatch,
        turn_executor=SimpleNamespace(
            _apply_agent_result_transformer=lambda _, result: result,
            prepare_turn_decision=AsyncMock(),
            decode_result=lambda **_: SimpleNamespace(
                decision=None, structured_output_details=None
            ),
        ),
    )
    await resume_interrupted_task_turn(agent, task.task_id)
    if interrupt_again:
        agent.turn_executor.prepare_turn_decision.assert_not_awaited()
        assert (
            manager.task_state.human_interaction_blockers[task.task_id][
                "access_request_id"
            ]
            == "areq_new"
        )
        assert manager.get_next_task_to_process() is None
        return
    agent.turn_executor.prepare_turn_decision.assert_awaited_once_with(
        msg_content=task,
        msg_header=session.msg_header,
        msg_meta=session.msg_meta,
        task_id=task.task_id,
        decision=None,
        strands_agent=sdk,
        decision_tool_name="",
    )
    assert manager.get_task_info(task.task_id).state == ProgressState.PARTIAL
    assert manager.get_next_task_to_process() == task.task_id
    manager.update_main_task_state(task.task_id, ProgressState.IN_PROGRESS, now)
    assert manager.begin_final_review_if_needed(task.task_id) is None


def test_preempted_task_stays_paused_outside_ready_queue(manager):
    first = add_task(manager)
    second = add_task(manager, "second")
    now = datetime.now(timezone.utc).isoformat()
    manager.update_main_task_state(first.task_id, ProgressState.IN_PROGRESS, now)
    manager.update_main_task_state(second.task_id, ProgressState.IN_PROGRESS, now)
    assert manager.get_task_info(first.task_id).state == ProgressState.PAUSED
    assert first.task_id not in manager.task_state.task_queue
    manager.update_main_task_state(second.task_id, ProgressState.COMPLETED, now)
    assert manager.get_next_task_to_process() is None


def test_failed_dequeue_persistence_keeps_ready_task(manager, monkeypatch):
    task = add_task(manager)

    def unavailable():
        raise OSError("Database temporarily unavailable")

    monkeypatch.setattr(manager, "_persist_runtime_state", unavailable)
    with pytest.raises(OSError):
        manager.get_next_task_to_process()
    assert list(manager.task_state.task_queue) == [task.task_id]


def notification_agent(manager):
    from custom_agents.task_queue_notifications import QueuePressureNotifications

    return SimpleNamespace(
        task_manager=manager,
        id="manager",
        name="Manager",
        is_team_lead=True,
        _queue_pressure_notifications=QueuePressureNotifications(),
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _logger=logging.getLogger(__name__),
    )


@pytest.mark.live
@pytest.mark.integration
@pytest.mark.nats
@pytest.mark.asyncio
async def test_queue_safety_notice_reaches_requester_through_jetstream(manager):
    from custom_agents.task_queue_notifications import check_queue_pressure
    from custom_tools.comms_tools import TeamComms
    from test_live_nats_replacement_semantics import _adapter, _subscribe_queue, _team

    team = _team("queue_safety")
    adapter = _adapter(team)
    route = f"{team}_gateway_response"
    try:
        await adapter.connect(reconcile_topology=True)
        responses = await _subscribe_queue(
            adapter,
            name="responses",
            routing_key=route,
            durable_name=adapter.mapper.durable_name("responses"),
        )
        task = add_task(manager)
        info = manager.get_task_info(task.task_id)
        info.header.response_routing_key = route
        manager.update_main_task_state(
            task.task_id, ProgressState.WAITING, datetime.now(timezone.utc).isoformat()
        )
        blocker = {"kind": "access_request", "access_request_id": "pending"}
        record_human_interaction_blocker(manager, task.task_id, blocker)
        manager.task_state.task_queue.appendleft(task.task_id)
        agent = notification_agent(manager)
        agent.team_comms = TeamComms(team_id=team, nats_adapter=adapter)
        await check_queue_pressure(agent)
        delivered = await asyncio.wait_for(responses.get(), timeout=3)
        envelope = delivered.envelope
        assert envelope.msg_content.reason == "task_queue_safety"
        assert envelope.msg_content.source_task_id == task.task_id
        assert envelope.msg_meta.gateway_context.canonical_user_id == "alice"
        assert envelope.msg_meta.msg_correlation_id != info.meta.msg_correlation_id
        await delivered.ack()
        assert manager.task_state.human_interaction_blockers[task.task_id] == blocker
        assert not manager.task_state.task_queue
    finally:
        await adapter.close()
