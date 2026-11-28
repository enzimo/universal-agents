"""A pause that waits only locally is flushed to disk once WAITING is saved (decision 82).

An agent process defers SQLite flushes until it sends something to NATS
(``storage.sqlite_connection``, "Flushing before sending"). A pause whose every
interrupt waits here (an access request, a staged tool setup run or a timed
wait) sends no requester message, so ``send_interrupt_request_for_result``
flushes once itself (``custom_agents.commit_flushes.flush_local_pause_commits``).
Otherwise a power loss during an hours-long wait could undo the pause and bring
the task back as a running turn.

Each test drives the real pause function with deferral switched on, as
``strands_main`` sets it, and checks that nothing is left unflushed and nothing
was sent. A failed flush is logged with identifiers, keeps the pause and stops
later sends.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_agents import commit_flushes, task_steering_runtime
from custom_agents import timed_waits as timed_waits_rules
from custom_agents.task_human_interaction_blockers import (
    ACCESS_REQUEST_BLOCKER,
    TIMED_WAIT_BLOCKER,
    get_human_interaction_blocker,
)
from custom_agents.task_manager import TaskManager
from custom_agents.timed_waits import TIMED_WAIT_KIND, WAIT_TOOL_NAME
from custom_agents.tool_setup_run_runtime import run_repository
from custom_agents.tool_setup_runs import TOOL_SETUP_RUN_KIND, RunPieceV1, ToolSetupRunV1
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import ACCESS_REQUEST_ID_KEY, ACCESS_REQUEST_INTERRUPT_KIND, Task
from custom_messages.enums import MessageMetaType, ProgressState
from custom_tools.runtime_context import task_work
from custom_transport import send_barrier
from storage import sqlite_connection
from tests.commit_flush_support import defer_flushes_like_an_agent_process
from tests.test_timed_wait_runtime import AGENT_ID, BASE_TIME, TASK_ID, _make_runtime_agent

TIMED_WAIT_REASON = {
    "kind": TIMED_WAIT_KIND,
    "call_id": "call-pause-flush",
    "tool": WAIT_TOOL_NAME,
    "tool_use_id": "tu-pause-flush",
    "minutes": 10,
    "reason": "power-loss durability check",
}
ACCESS_REQUEST_REASON = {
    "kind": ACCESS_REQUEST_INTERRUPT_KIND,
    ACCESS_REQUEST_ID_KEY: "req-pause-flush",
    "summary": "Use the schedules tool",
}


@pytest.fixture
def agent_process(monkeypatch: pytest.MonkeyPatch) -> None:
    defer_flushes_like_an_agent_process(monkeypatch)
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: BASE_TIME)


def _interrupt(kind: str, reason: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(id=f"int-{kind}", name=kind, reason=reason)


async def _pause(agent: Any, task_id: str, interrupt: SimpleNamespace) -> None:
    """What the runtime does when a turn stops on ``interrupt`` (the real pause function)."""

    info = agent.task_manager.get_task_info(task_id)
    with task_work(task_id) as work:
        work.strands = SimpleNamespace(replace_before_next_turn=False)
        await task_steering_runtime.send_interrupt_request_for_result(
            agent,
            result=SimpleNamespace(interrupts=[interrupt]),
            task=info.task,
            msg_header=info.header,
            msg_meta=info.meta,
            invoke_kwargs={},
            callback_handler=lambda **_kw: None,
            full_prompt="",
        )


def _flushed_worker(db_path: Path) -> Any:
    agent = _make_runtime_agent(db_path)
    sqlite_connection.flush_unflushed_commits()
    assert not sqlite_connection.has_unflushed_commits()
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "reason", "blocker_kind"),
    [
        (TIMED_WAIT_KIND, TIMED_WAIT_REASON, TIMED_WAIT_BLOCKER),
        (ACCESS_REQUEST_INTERRUPT_KIND, ACCESS_REQUEST_REASON, ACCESS_REQUEST_BLOCKER),
    ],
)
async def test_a_local_pause_is_on_disk_without_sending_anything(
    agent_process: None, tmp_path: Path, kind: str, reason: dict[str, Any], blocker_kind: str
) -> None:
    agent = _flushed_worker(tmp_path / f"{kind}.db")

    await _pause(agent, TASK_ID, _interrupt(kind, reason))

    assert not sqlite_connection.has_unflushed_commits()
    agent.team_comms.send_message.assert_not_awaited()
    assert agent.task_manager.get_task_info(TASK_ID).state == ProgressState.WAITING
    assert get_human_interaction_blocker(agent.task_manager, TASK_ID)["kind"] == blocker_kind


@pytest.mark.asyncio
async def test_a_setup_run_pause_is_on_disk_without_sending_anything(agent_process: None, tmp_path: Path) -> None:
    task_id = "task-lead"
    manager = TaskManager(db_path=str(tmp_path / "setup.db"))
    manager.set_owner_identity("agent-lead", "TeamArchitect", "team", "TeamArchitect")
    meta = MessageMeta(conversation_id="conv-1", msg_correlation_id="corr-1")
    task = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Set up GitHub")
    assert manager.add_task(task, MessageHeader(from_agent_id="gateway"), meta, owner_person_id="user-1")
    assert manager.get_next_task_to_process() == task_id
    manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, BASE_TIME.isoformat())
    agent = SimpleNamespace(
        id="agent-lead",
        name="TeamArchitect",
        team_id="team",
        named_agent_type="TeamArchitect",
        task_manager=manager,
        _interrupted_turn_sessions={},
        _pending_interrupt_responses={},
        _logger=logging.getLogger("test.local_pause_flush"),
        team_comms=SimpleNamespace(send_message=AsyncMock()),
    )
    # A piece already waiting and no workflow: starting the run sends nothing.
    run_repository(agent).create(
        ToolSetupRunV1(
            run_id="run-pause-flush",
            team_id="team",
            person_id="user-1",
            task_id=task_id,
            agent_id="agent-lead",
            agent_type="TeamArchitect",
            tool_ids=("cli:github",),
            workflow_id=None,
            interrupt_id="",
            pieces=(RunPieceV1(piece_id="access", tool_id="cli:github", kind="access", status="waiting", attempt=1),),
        )
    )
    sqlite_connection.flush_unflushed_commits()
    assert not sqlite_connection.has_unflushed_commits()

    reason = {"kind": TOOL_SETUP_RUN_KIND, "run_id": "run-pause-flush", "summary": "Set up GitHub"}
    await _pause(agent, task_id, _interrupt(TOOL_SETUP_RUN_KIND, reason))

    assert not sqlite_connection.has_unflushed_commits()
    agent.team_comms.send_message.assert_not_awaited()
    assert manager.get_task_info(task_id).state == ProgressState.WAITING
    assert get_human_interaction_blocker(manager, task_id)["kind"] == TOOL_SETUP_RUN_KIND


@pytest.mark.asyncio
async def test_a_failed_pause_flush_is_logged_keeps_the_pause_and_stops_later_sends(
    agent_process: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _flushed_worker(tmp_path / "failing.db")

    def failing_flush(**_kwargs: Any) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(sqlite_connection, "flush_unflushed_commits", failing_flush)
    with caplog.at_level(logging.WARNING):
        await _pause(agent, TASK_ID, _interrupt(ACCESS_REQUEST_INTERRUPT_KIND, ACCESS_REQUEST_REASON))

    assert "event=local_pause_commit_flush_failed" in caplog.text
    assert f"agent_id={AGENT_ID}" in caplog.text
    assert f"task_id={TASK_ID}" in caplog.text
    assert f"pause_kind={ACCESS_REQUEST_INTERRUPT_KIND}" in caplog.text
    assert agent.task_manager.get_task_info(TASK_ID).state == ProgressState.WAITING
    assert get_human_interaction_blocker(agent.task_manager, TASK_ID)["kind"] == ACCESS_REQUEST_BLOCKER
    with pytest.raises(commit_flushes.CommitFlushFailed):
        await send_barrier.before_send()
