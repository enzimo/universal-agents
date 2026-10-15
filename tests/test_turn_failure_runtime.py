"""A task never keeps the execution slot after its turn is gone.

Covers ``custom_agents.turn_failure_runtime``: the guard that ends an
IN_PROGRESS task holding the slot with no turn running (the owner's queue
stall of 2026-09-28), the RETRYING state it must leave alone, and a
resume-with-directive turn that raises (it used to keep the slot as well).
The sequence through a real agent is in ``test_live_setup_wait_queue``.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from custom_agents import task_resume_runtime
from custom_agents.turn_failure_runtime import ORPHANED_SLOT_MESSAGE
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from test_turn_action_outbox import _make_agent, _make_task_manager, _now


def _add(manager, task_id: str) -> Task:
    task = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=task_id)
    meta = MessageMeta(
        from_agent_name="person",
        requester_type=RequesterType.USER_REQUESTER,
        is_response_requested=True,
        msg_correlation_id=f"corr_{task_id}",
        conversation_id="conv_1",
    )
    assert manager.add_task(task, MessageHeader(from_agent_id="gateway", response_routing_key="chat"), meta, owner_person_id=None)
    return task


def _hold_slot(manager, task_id: str, state: ProgressState) -> None:
    """A task that took the slot for a turn that is no longer running."""

    assert manager.get_next_task_to_process() == task_id
    assert manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, _now())
    if state != ProgressState.IN_PROGRESS:
        assert manager.update_main_task_state(task_id, state, _now())


def _sent_errors(agent) -> dict[str, list[str]]:
    errors = {}
    for call in agent.team_comms.send_message.await_args_list:
        content = call.kwargs["message"].msg_content
        if isinstance(content, Task) and content.task_state[-1][0] == ProgressState.FAILED:
            errors[content.task_id] = list(content.errors or [])
    return errors


@pytest.mark.asyncio
async def test_an_orphaned_in_progress_task_is_ended_and_the_queue_moves(tmp_path, caplog) -> None:
    manager = _make_task_manager(tmp_path)
    _add(manager, "task_orphan")
    _hold_slot(manager, "task_orphan", ProgressState.IN_PROGRESS)
    _add(manager, "task_next")
    agent = _make_agent(tmp_path, task_manager=manager)
    ran = []

    async def answer(msg_content, msg_header, msg_meta):
        ran.append(msg_content.task_id)
        return msg_content.model_copy(update={"task_type": MessageMetaType.RESPONSE, "answer": "done"})

    object.__setattr__(agent, "process_llm_interaction", answer)
    object.__setattr__(agent, "_apply_task_processing_result", AsyncMock())
    assert not manager.can_start_new_task(), "the orphan blocks the queue before the guard"

    with caplog.at_level(logging.ERROR):
        assert await agent._check_and_process_next_task() is True

    assert ran == ["task_next"]
    assert manager.get_task_info("task_orphan") is None, "ended and archived"
    assert _sent_errors(agent)["task_orphan"] == [ORPHANED_SLOT_MESSAGE]
    assert any(
        "event=execution_slot_orphaned" in record.getMessage() and "task_orphan" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_a_task_waiting_for_an_outbox_retry_keeps_the_slot(tmp_path, caplog) -> None:
    manager = _make_task_manager(tmp_path)
    _add(manager, "task_retrying")
    _hold_slot(manager, "task_retrying", ProgressState.RETRYING)
    _add(manager, "task_next")
    agent = _make_agent(tmp_path, task_manager=manager)
    object.__setattr__(agent, "process_llm_interaction", AsyncMock())

    assert await agent._check_and_process_next_task() is False

    agent.process_llm_interaction.assert_not_awaited()
    assert manager.get_task_info("task_retrying").state == ProgressState.RETRYING
    assert not any("execution_slot_orphaned" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_a_resume_with_directive_that_raises_ends_the_task_and_frees_the_slot(tmp_path) -> None:
    manager = _make_task_manager(tmp_path)
    _add(manager, "task_resumed")
    _hold_slot(manager, "task_resumed", ProgressState.WAITING)
    agent = _make_agent(tmp_path, task_manager=manager)
    object.__setattr__(
        agent, "process_llm_interaction", AsyncMock(side_effect=RuntimeError("model call failed"))
    )

    await task_resume_runtime.resume_task_with_directive(
        agent, manager.get_task_info("task_resumed"), "The sign-in finished; continue."
    )

    assert manager.get_task_info("task_resumed") is None
    assert manager.can_start_new_task()
    assert "Runtime error: model call failed" in _sent_errors(agent)["task_resumed"]


@pytest.mark.asyncio
async def test_the_scheduler_pass_frees_an_orphaned_slot_without_new_work(tmp_path) -> None:
    """Before 2026-10-04 the check ran only when the next task was picked."""
    import asyncio

    from custom_agents.turn_failure_runtime import release_orphaned_slot_when_idle

    manager = _make_task_manager(tmp_path)
    _add(manager, "task_orphan")
    _hold_slot(manager, "task_orphan", ProgressState.IN_PROGRESS)
    agent = _make_agent(tmp_path, task_manager=manager)

    turn_started, finish_turn = asyncio.Event(), asyncio.Event()

    async def running_turn() -> None:
        async with agent._execution_gate.hold():
            turn_started.set()
            await finish_turn.wait()

    turn = asyncio.create_task(running_turn())
    await turn_started.wait()
    assert await release_orphaned_slot_when_idle(agent) is False, "a running turn owns the slot"
    assert manager.get_task_info("task_orphan") is not None
    finish_turn.set()
    await turn

    assert await release_orphaned_slot_when_idle(agent) is True
    assert manager.get_task_info("task_orphan") is None
    assert _sent_errors(agent)["task_orphan"] == [ORPHANED_SLOT_MESSAGE]
