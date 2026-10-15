"""Delegated work stops when the request it belongs to ends some other way than /stop.

Real TaskManager and SQLite cancellation barrier; stop messages captured at
``team_comms``. Before 2026-10-04 only explicit stops cascaded: a parent that
failed or completed left its children running on workers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.control_runtime import request_subtask_stop
from custom_agents.delegated_work_runtime import delegated_tasks, record_publication
from custom_agents.orphaned_subtasks import (
    PARENT_ENDED_REASON,
    register_parent_end_stops,
    stop_report_for_ended_parent,
)
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.control_v1 import ControlInstruction
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def lead(tmp_path):
    manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    manager.set_owner_identity("lead", "lead", "team", "TeamArchitect")
    context = GatewayContext(canonical_user_id="jordan", session_key="s")
    manager.add_task(
        Task(task_id="root", task_type=MessageMetaType.REQUEST, problem_statement="Research"),
        MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
        MessageMeta(gateway_context=context),
        owner_person_id="jordan",
    )
    manager.update_main_task_state("root", ProgressState.IN_PROGRESS, now())
    manager.add_subtask(
        "root",
        Task(task_id="child", parent_task_id="root", task_type=MessageMetaType.REQUEST, problem_statement="Fetch"),
        MessageHeader(from_agent_id="lead", to_routing_path="team_WebResearchAgent"),
        MessageMeta(to_named_agent_type="WebResearchAgent", gateway_context=context),
    )
    agent = SimpleNamespace(
        id="lead",
        name="lead",
        team_id="team",
        task_manager=manager,
        team_comms=SimpleNamespace(send_message=AsyncMock(), delete_work_message=AsyncMock(return_value=True)),
        named_agent_type_queue_map={"WebResearchAgent": "team_WebResearchAgent"},
        _logger=logging.getLogger("orphaned-subtasks-test"),
    )
    agent._request_subtask_stop = partial(request_subtask_stop, agent)
    # The child was published to the shared queue and taken by worker-3.
    record_publication(
        agent,
        subtask_id="child",
        parent_task_id="root",
        named_agent_type="WebResearchAgent",
        target_agent_name=None,
        publish_sequence=11,
    )
    delegated_tasks(agent).record_claim(
        team_id="team",
        task_id="child",
        worker_agent_id="worker-3",
        worker_agent_name="worker-3",
        claimed_at=datetime.now(timezone.utc),
    )
    register_parent_end_stops(agent)
    return agent


async def settle() -> None:
    """Let the stop messages scheduled on the loop go out.

    A stop first reads the delegated-task record on the task lane (a worker
    thread), so a few bare loop turns are not enough.
    """
    for _ in range(50):
        await asyncio.sleep(0.01)


def stops(agent):
    return [
        call.kwargs
        for call in agent.team_comms.send_message.await_args_list
        if isinstance(call.kwargs["message"].msg_content, ControlInstruction)
    ]


@pytest.mark.asyncio
async def test_a_failed_request_stops_its_delegated_work(lead, caplog):
    with caplog.at_level(logging.WARNING):
        assert lead.task_manager.fail_task("root", "The model call failed.", now())
    await settle()
    assert lead.task_manager.is_execution_cancelled("child")  # barrier for workers sharing the database
    (sent,) = stops(lead)
    instruction = sent["message"].msg_content
    assert instruction.target_task_id == "child" and instruction.cascade_to_subtasks
    assert instruction.reason == PARENT_ENDED_REASON
    assert sent["routing_key"] == "worker-3"  # the worker holding it, never the shared queue
    assert "event=parent_end_subtasks_stopping" in caplog.text


@pytest.mark.asyncio
async def test_a_request_without_pending_work_sends_nothing(lead):
    lead.task_manager.update_main_task_state("child", ProgressState.COMPLETED, now())
    lead.task_manager.fail_task("root", "Done differently.", now())
    await settle()
    assert not stops(lead)


@pytest.mark.asyncio
async def test_a_worker_reporting_on_work_of_an_ended_request_is_told_to_stop(lead):
    lead.task_manager.fail_task("root", "The model call failed.", now())
    await settle()
    lead.team_comms.send_message.reset_mock()
    sent = await stop_report_for_ended_parent(
        lead, parent_task_id="root", child_task_id="child", msg_header=MessageHeader(from_agent_id="worker-7")
    )
    assert sent
    (stop,) = stops(lead)
    assert stop["routing_key"] == "worker-7"  # straight to the worker that holds it
    assert stop["message"].msg_content.target_task_id == "child"


@pytest.mark.asyncio
async def test_a_report_for_a_parent_never_seen_is_only_logged(lead):
    sent = await stop_report_for_ended_parent(
        lead, parent_task_id="unknown", child_task_id="unknown-child", msg_header=MessageHeader(from_agent_id="worker-7")
    )
    assert not sent and not stops(lead)
