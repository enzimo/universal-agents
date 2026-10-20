"""A task that keeps waiting is stopped at MAX_WAITS_PER_TASK (custom_agents.task_wait_limit).

Real TaskManager notes; the turn executor's check and the failed-turn path are
exercised directly, with the transport captured.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.sdk.strands.turn import LLMTurnExecutor, _TurnState
from custom_agents.sdk.strands.turn_ports import TurnRuntimeEffects, TurnTaskOperations
from custom_agents.structured_llm_messages import (
    DelegateTaskAction,
    NoOpAction,
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TaskResponseAction,
)
from custom_agents.task_manager import TaskManager
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_agents.task_wait_limit import (
    MAX_WAITS_PER_TASK,
    WAIT_LIMIT_NOTICE_REASON,
    TaskWaitLimitReached,
    wait_limit_message,
)
from custom_agents.turn_failure_runtime import fail_task_after_turn_error
from custom_agents.turn_work_notes import ENDED_BY_OPEN, ENDED_BY_WAIT, note_details
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.routing_v1 import GatewayContext, ManagerEscalationContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType

LIMIT = 5


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _manager(tmp_path, *, parent_task_id: str | None = None, timer: bool = True) -> TaskManager:
    manager = TaskManager(db_path=str(tmp_path / "worker.db"))
    manager.set_owner_identity("worker_1", "worker", "team", "KnowledgeCurator")
    manager.add_task(
        Task(task_id="run_1", parent_task_id=parent_task_id, task_type=MessageMetaType.REQUEST, problem_statement="Audit."),
        MessageHeader(from_agent_id="scheduler", response_routing_key="gateway_route"),
        MessageMeta(
            requester_type=RequesterType.TIMER_REQUESTER if timer else RequesterType.AGENT_REQUESTER,
            gateway_context=GatewayContext(canonical_user_id="person_1", scheduled_job_id="schedule_audit"),
        ),
        owner_person_id="person_1",
    )
    manager.update_main_task_state("run_1", ProgressState.IN_PROGRESS, _now())
    return manager


def _record_waits(manager: TaskManager, count: int, *, started_wait: bool = True) -> None:
    for _ in range(count):
        manager.record_turn_work(
            "run_1",
            note_details(
                tool_calls=[{"tool": "knowledge_write", "input": "", "status": "success"}],
                sent=["manager escalation (needs_user_input) to TeamArchitect: confirm IDs"],
                started_wait=started_wait,
                ended_by=ENDED_BY_WAIT if started_wait else ENDED_BY_OPEN,
            ),
        )


def _executor(manager: TaskManager, limit: int | None) -> LLMTurnExecutor:
    def unexpected(name):
        def fail(*args, **kwargs):
            raise AssertionError(f"Undeclared test interaction: {name}")

        return fail

    tasks = {item.name: unexpected(item.name) for item in fields(TurnTaskOperations)}
    tasks.update(runtime=TaskOperationRuntime(), objective_work_coordinator=None, count_started_waits=manager.count_started_waits)
    effects = {item.name: unexpected(item.name) for item in fields(TurnRuntimeEffects)}
    return LLMTurnExecutor(
        settings=lambda: SimpleNamespace(id="worker_1", name="worker", max_waits_per_task=limit),
        model=lambda: SimpleNamespace(messages=[]),
        context=lambda: None,
        debug_dump=lambda: None,
        logger=logging.getLogger("task-wait-limit-test"),
        tasks=TurnTaskOperations(**tasks),
        effects=TurnRuntimeEffects(**effects),
    )


def _turn(manager: TaskManager) -> _TurnState:
    info = manager.get_task_info("run_1")
    turn = _TurnState(msg_content=info.task, msg_header=info.header, msg_meta=info.meta)
    turn.active_task_id = "run_1"
    return turn


ESCALATE = StructuredAgentTurnDecision(
    actions=[
        DelegateTaskAction(
            task=StructuredTaskPayload(task_type=MessageMetaType.REQUEST, problem_statement="Confirm two IDs."),
            manager_escalation=ManagerEscalationContext(escalation_type="needs_user_input"),
        )
    ]
)
REPORT_AND_FINISH = StructuredAgentTurnDecision(
    actions=[
        DelegateTaskAction(
            task=StructuredTaskPayload(task_type=MessageMetaType.REQUEST, problem_statement="Changed two pages."),
            manager_escalation=ManagerEscalationContext(escalation_type="worker_result"),
        ),
        TaskResponseAction(task=StructuredTaskPayload(task_type=MessageMetaType.RESPONSE, answer="done")),
    ]
)


def test_the_limit_is_five():
    assert MAX_WAITS_PER_TASK == LIMIT


def test_the_sixth_wait_is_refused_before_anything_is_sent(tmp_path, caplog):
    manager = _manager(tmp_path)
    _record_waits(manager, LIMIT)

    with caplog.at_level(logging.WARNING, logger="task-wait-limit-test"):
        with pytest.raises(TaskWaitLimitReached) as stopped:
            asyncio.run(_executor(manager, LIMIT)._stop_at_wait_limit(_turn(manager), ESCALATE))

    assert (stopped.value.task_id, stopped.value.waits, stopped.value.limit) == ("run_1", LIMIT, LIMIT)
    assert "event=task_wait_limit_reached" in caplog.text


@pytest.mark.parametrize(
    ("waits", "kept_open", "decision", "limit"),
    [
        (LIMIT - 1, 0, ESCALATE, LIMIT),  # the fifth wait is allowed
        (LIMIT - 1, 3, ESCALATE, LIMIT),  # turns that only kept waiting do not count
        (LIMIT, 0, REPORT_AND_FINISH, LIMIT),  # a report waits for nothing
        (LIMIT, 0, StructuredAgentTurnDecision(actions=[NoOpAction()]), LIMIT),
        (LIMIT, 0, ESCALATE, None),  # turn settings without a limit (test doubles)
    ],
)
def test_other_turns_go_on(tmp_path, waits, kept_open, decision, limit):
    manager = _manager(tmp_path)
    _record_waits(manager, waits)
    _record_waits(manager, kept_open, started_wait=False)

    asyncio.run(_executor(manager, limit)._stop_at_wait_limit(_turn(manager), decision))

    assert manager.count_started_waits("run_1") == waits


def _failing_agent(manager: TaskManager) -> SimpleNamespace:
    return SimpleNamespace(
        id="worker_1",
        name="worker",
        team_id="team",
        named_agent_type="KnowledgeCurator",
        task_manager=manager,
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _send_task_error_response=AsyncMock(),
        _notify_context_after_turn=AsyncMock(),
        _conversation_store=None,
        _conversation_compactor=None,
        _conversation_artifact_offloader=None,
        _conversation_summary_generator=None,
        _logger=logging.getLogger("task-wait-limit-test"),
        _strands_agent=None,
        _custody_release=None,
    )


def test_a_stopped_scheduled_run_fails_and_the_person_gets_a_chat_message(tmp_path):
    manager = _manager(tmp_path)
    agent = _failing_agent(manager)
    task_info = manager.get_task_info("run_1")
    error = TaskWaitLimitReached(task_id="run_1", waits=LIMIT, limit=LIMIT)

    asyncio.run(fail_task_after_turn_error(agent, "run_1", task_info, error))

    # The requester reads the plain explanation, not a "Runtime error".
    message = wait_limit_message(waits=LIMIT, limit=LIMIT)
    assert agent._send_task_error_response.await_args.args[3] == message
    assert manager.get_task_info("run_1") is None or manager.get_task_info("run_1").state == ProgressState.FAILED
    (call,) = agent.team_comms.send_message.await_args_list
    envelope = call.kwargs["message"]
    assert call.kwargs["routing_key"] == "gateway_route"
    assert isinstance(envelope.msg_content, UserNotification)
    assert envelope.msg_content.reason == WAIT_LIMIT_NOTICE_REASON
    assert message in envelope.msg_content.text
    # A standalone notice, not the run's result.
    assert envelope.msg_meta.gateway_context.scheduled_job_id is None
    assert envelope.msg_meta.gateway_context.canonical_user_id == "person_1"


@pytest.mark.parametrize(("parent_task_id", "timer"), [("lead_task", False), (None, False)])
def test_other_requesters_are_told_through_the_failure_only(tmp_path, parent_task_id, timer):
    """A subtask's requester is an agent that answers its own requester; a person's request gets the failure."""

    manager = _manager(tmp_path, parent_task_id=parent_task_id, timer=timer)
    agent = _failing_agent(manager)

    asyncio.run(
        fail_task_after_turn_error(
            agent, "run_1", manager.get_task_info("run_1"), TaskWaitLimitReached(task_id="run_1", waits=LIMIT, limit=LIMIT)
        )
    )

    agent._send_task_error_response.assert_awaited_once()
    agent.team_comms.send_message.assert_not_awaited()
