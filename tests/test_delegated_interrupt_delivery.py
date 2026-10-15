"""A worker's input request is refused by its parent, and the worker fails its task.

Only access requests pause an agent's turn, and they wait in the worker's own
process for a person's decision (``tests/test_access_request_tools.py``).
Agents no longer pass a worker's input requests on, so a parent agent refuses
any it receives; the refusal reaches the worker, whose normal failure path
returns the error to its parent instead of leaving the task waiting forever.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import (
    WORKER_INTERRUPT_REFUSED,
    handle_task_steering_message,
    send_interrupt_request_for_result,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    RequesterType,
    TaskSteeringStatus,
    TaskSteeringType,
)

TEAM = "team_relay"
PARENT_ID = "agent_lead"
WORKER_ID = "agent_worker"


def _agent(agent_id: str, tmp_path, **extra) -> SimpleNamespace:
    manager = TaskManager(db_path=str(tmp_path / f"{agent_id}.db"))
    manager.set_owner_identity(agent_id, agent_id, TEAM, extra.get("named_agent_type", "TeamArchitect"))
    return SimpleNamespace(
        id=agent_id,
        name=agent_id,
        team_id=TEAM,
        named_agent_type=extra.get("named_agent_type", "TeamArchitect"),
        task_manager=manager,
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _agent_id_by_name={},
        named_agent_type_queue_map={},
        _logger=logging.getLogger(f"tests.relay.{agent_id}"),
        _interrupted_turn_sessions={},
        _pending_interrupt_responses={},
        _processed_task_steering_ids=set(),
        _strands_agent_replacement_required=False,
        _send_task_error_response=AsyncMock(),
        mid_turn_steering={"enabled": True},
    )


@pytest.mark.asyncio
async def test_parent_refuses_a_worker_input_request_and_the_worker_fails_its_task(tmp_path, caplog) -> None:
    worker = _agent(WORKER_ID, tmp_path, named_agent_type="WebResearchAgent")
    parent = _agent(PARENT_ID, tmp_path)
    task = Task(task_id="child_task", task_type=MessageMetaType.REQUEST)
    header = MessageHeader(from_agent_id=PARENT_ID, to_routing_path=WORKER_ID, response_routing_key=PARENT_ID)
    meta = MessageMeta(
        msg_correlation_id="corr_child",
        conversation_id="conv_1",
        requester_type=RequesterType.AGENT_REQUESTER,
        is_response_requested=True,
    )
    worker.task_manager.add_task(task, header, meta, owner_person_id=None)

    # A tool other than an access request pauses the worker for input.
    steering = await send_interrupt_request_for_result(
        worker,
        result=SimpleNamespace(
            interrupts=[SimpleNamespace(id="ask_1", name="ask_person", reason={"kind": "question"})]
        ),
        task=task,
        msg_header=header,
        msg_meta=meta,
        strands_agent=object(),
        invoke_kwargs={},
        full_prompt="",
    )
    sent = worker.team_comms.send_message.await_args.kwargs
    assert sent["routing_key"] == PARENT_ID
    assert steering.steering_type == TaskSteeringType.INTERRUPT_REQUEST
    assert worker.task_manager.get_task_info("child_task").state == ProgressState.WAITING

    # The parent refuses it instead of passing it on.
    caplog.set_level(logging.WARNING)
    envelope = sent["message"]
    await handle_task_steering_message(parent, envelope.msg_content, envelope.msg_header, envelope.msg_meta)
    refusal = parent.team_comms.send_message.await_args.kwargs
    assert refusal["routing_key"] == WORKER_ID
    status = refusal["message"].msg_content
    assert isinstance(status, TaskSteering)
    assert status.steering_type == TaskSteeringType.STATUS
    assert status.status == TaskSteeringStatus.REJECTED
    assert status.status_reason == WORKER_INTERRUPT_REFUSED
    assert "event=worker_interrupt_refused" in caplog.text

    # The refusal fails the worker's task and returns the error to its parent.
    await handle_task_steering_message(
        worker, status, refusal["message"].msg_header, refusal["message"].msg_meta
    )
    worker._send_task_error_response.assert_awaited_once()
    assert WORKER_INTERRUPT_REFUSED in worker._send_task_error_response.await_args.args[3]
    assert worker.task_manager.get_task_info("child_task") is None
    assert worker.task_manager.get_task_from_db("child_task")["status"] == ProgressState.FAILED.value
    assert not worker._interrupted_turn_sessions
    assert not worker.task_manager.task_state.human_interaction_blockers
