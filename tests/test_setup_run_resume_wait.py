"""A task waiting to resume a finished setup run keeps its saved record until the turn resumes.

When a ``set_up_tools`` run ends while the task's paused turn is still in
memory, the answer waits for the task's lane. The task's saved record of the
wait (its ``tool_setup_run`` blocker) stays, marked, and only the answer the
resumed turn takes removes it, just before that turn runs
(``task_steering_runtime._resume_interrupted_task_turn``). Real TaskManager
and setup-run store (SQLite); the paused turn is a stand-in session and the
resumed turn a stub, so the tests see what is saved when the turn starts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import task_steering_runtime, tool_setup_run_runtime
from custom_agents.task_human_interaction_blockers import (
    ACCESS_REQUEST_BLOCKER,
    clear_answered_blocker,
    get_human_interaction_blocker,
    record_human_interaction_blocker,
    setup_answer_held,
)
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import resume_interrupted_task_turn
from custom_agents.tool_setup_run_runtime import run_repository
from custom_agents.tool_setup_runs import (
    DONE,
    RUN_FINISHED,
    TOOL_SETUP_RUN_KIND,
    TOOL_SETUP_RUN_RESPONSE_KIND,
    RunPieceV1,
    ToolSetupRunV1,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, ProgressState, TaskSteeringStatus, TaskSteeringType
from custom_tools.tool_stop import ToolStop

TASK = "task-lead"
AGENT_ID = "agent-lead"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def woken(monkeypatch: pytest.MonkeyPatch) -> list:
    """Wake-ups of the drain; none runs, as when the task's lane is busy."""

    calls: list = []
    monkeypatch.setattr(tool_setup_run_runtime, "schedule_pending_work", calls.append)
    return calls


@pytest.fixture
def lead(tmp_path: Path) -> SimpleNamespace:
    """TeamLead with one task that is waiting (its turn paused)."""

    manager = TaskManager(db_path=str(tmp_path / "manager.db"))
    manager.set_owner_identity(AGENT_ID, "TeamArchitect", "team", "TeamArchitect")
    assert manager.add_task(
        Task(task_id=TASK, task_type=MessageMetaType.REQUEST), MessageHeader(from_agent_id="gateway"), MessageMeta(),
        owner_person_id=None,
    )
    assert manager.get_next_task_to_process() == TASK
    manager.update_main_task_state(TASK, ProgressState.IN_PROGRESS, _now())
    manager.update_main_task_state(TASK, ProgressState.WAITING, _now())
    return SimpleNamespace(
        id=AGENT_ID,
        team_id="team",
        task_manager=manager,
        _interrupted_turn_sessions={},
        _pending_interrupt_responses={},
        _set_active_execution_context=lambda task_id: ToolStop(agent_id=AGENT_ID, task_id=task_id),
        _clear_active_execution_context=lambda: None,
        _refresh_self_agent_state=AsyncMock(),
        _logger=logging.getLogger("test.setup_run_resume_wait"),
    )


def pause_on_setup(agent: SimpleNamespace, run_id: str = "run-1", interrupt_id: str = "i1") -> None:
    """set_up_tools paused the turn: its run (every page done), its record and the live session."""

    record_human_interaction_blocker(
        agent.task_manager, TASK, {"kind": TOOL_SETUP_RUN_KIND, "run_id": run_id, "interrupt_id": interrupt_id}
    )
    run_repository(agent).create(
        ToolSetupRunV1(
            run_id=run_id, team_id="team", person_id="user-1", task_id=TASK, agent_id=agent.id,
            agent_type="TeamArchitect", tool_ids=("cli:github",), interrupt_id=interrupt_id,
            pieces=(RunPieceV1(piece_id="sign_in", tool_id="cli:github", kind="sign_in", status=DONE),),
        )
    )
    agent._interrupted_turn_sessions[TASK] = SimpleNamespace(
        task_id=TASK, interrupts=[SimpleNamespace(interrupt_id=interrupt_id)],
        strands=SimpleNamespace(replace_before_next_turn=True),
    )


def record_of(agent: SimpleNamespace) -> dict | None:
    return get_human_interaction_blocker(agent.task_manager, TASK)


def answer(interrupt_id: str, run_id: str) -> TaskSteering:
    return TaskSteering(
        steering_id=f"tool_setup_resume_{run_id}",
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id=TASK,
        interrupt_responses=[
            {"interrupt_id": interrupt_id, "response": {"kind": TOOL_SETUP_RUN_RESPONSE_KIND, "run_id": run_id}}
        ],
        status=TaskSteeringStatus.PENDING,
    )


def stub_turn(monkeypatch: pytest.MonkeyPatch, agent: SimpleNamespace, then=None) -> list:
    """The resumed turn: it records the task's record when it starts, then does ``then``."""

    seen: list = []

    async def run_resumed_turn(_agent, session, steering, task_info, response_blocks, tool_stop):
        seen.append(record_of(agent))
        if then is not None:
            then()
        return None

    monkeypatch.setattr(task_steering_runtime, "_run_resumed_turn", run_resumed_turn)
    return seen


# -- the record stays until the turn resumes ---------------------------------------------


@pytest.mark.asyncio
async def test_a_run_ending_with_a_live_turn_marks_the_record_and_holds_the_answer(lead, woken) -> None:
    pause_on_setup(lead)

    await tool_setup_run_runtime.advance(lead, "run-1")

    record = record_of(lead)
    assert record["run_id"] == "run-1" and setup_answer_held(record, AGENT_ID)
    [held] = lead._pending_interrupt_responses[TASK].interrupt_responses
    assert held == {"interrupt_id": "i1", "response": {"kind": TOOL_SETUP_RUN_RESPONSE_KIND, "run_id": "run-1"}}
    assert woken == [lead]
    assert lead.task_manager.get_task_info(TASK).state == ProgressState.WAITING
    assert TASK not in lead.task_manager.task_state.task_queue


@pytest.mark.asyncio
async def test_the_answered_record_is_removed_just_before_the_resumed_turn_runs(lead, woken, monkeypatch) -> None:
    pause_on_setup(lead)
    await tool_setup_run_runtime.advance(lead, "run-1")
    seen = stub_turn(monkeypatch, lead)

    await resume_interrupted_task_turn(lead, TASK)

    assert seen == [None]
    assert record_of(lead) is None


@pytest.mark.parametrize(("interrupt_id", "run_id"), [("i1", "run-2"), ("i2", "run-1")], ids=["another run", "another interrupt"])
@pytest.mark.asyncio
async def test_an_answer_for_another_wait_leaves_the_record(lead, woken, monkeypatch, interrupt_id, run_id) -> None:
    pause_on_setup(lead)
    lead._pending_interrupt_responses[TASK] = answer(interrupt_id, run_id)
    seen = stub_turn(monkeypatch, lead)

    await resume_interrupted_task_turn(lead, TASK)

    assert [item["run_id"] for item in seen] == ["run-1"]
    assert record_of(lead)["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_a_new_pause_in_the_resumed_turn_keeps_its_own_record(lead, woken, monkeypatch) -> None:
    """The resumed turn calls set_up_tools again; its new record must survive the old answer's cleanup."""
    pause_on_setup(lead)
    await tool_setup_run_runtime.advance(lead, "run-1")

    def pause_again() -> None:
        record_human_interaction_blocker(
            lead.task_manager, TASK, {"kind": TOOL_SETUP_RUN_KIND, "run_id": "run-2", "interrupt_id": "i2"}
        )

    seen = stub_turn(monkeypatch, lead, then=pause_again)

    await resume_interrupted_task_turn(lead, TASK)

    assert seen == [None]
    record = record_of(lead)
    assert record["run_id"] == "run-2" and not setup_answer_held(record)


ACCESS_RECORD = {"kind": ACCESS_REQUEST_BLOCKER, "access_request_id": "req-1", "access_request_ids": ["req-1"], "interrupt_id": "i1"}
SETUP_RECORD = {"kind": TOOL_SETUP_RUN_KIND, "run_id": "run-1", "interrupt_id": "i1"}


@pytest.mark.parametrize(
    ("record", "answers", "removed"),
    [
        (ACCESS_RECORD, [("i1", {"access_request_id": "req-1", "status": "granted"})], True),
        (ACCESS_RECORD, [("i1", {"access_request_id": "req-2"})], False),
        (ACCESS_RECORD, [("i2", {"access_request_id": "req-1"})], False),
        (SETUP_RECORD, [("i1", {"kind": TOOL_SETUP_RUN_RESPONSE_KIND, "run_id": "run-1"})], True),
        (SETUP_RECORD, [("i1", {"kind": TOOL_SETUP_RUN_RESPONSE_KIND, "run_id": "run-2"})], False),
        (SETUP_RECORD, [("i1", {"access_request_id": "req-1"})], False),
        (SETUP_RECORD, [], False),
    ],
    ids=[
        "access answered", "another request", "another interrupt",
        "setup answered", "another run", "an access answer", "no structured answer",
    ],
)
def test_only_the_answer_a_record_waits_for_removes_it(lead, record, answers, removed) -> None:
    record_human_interaction_blocker(lead.task_manager, TASK, record)

    assert clear_answered_blocker(lead.task_manager, TASK, answers) is removed
    assert (record_of(lead) is None) is removed


# -- once only -----------------------------------------------------------------------------


def ended(agent: SimpleNamespace, run_id: str = "run-1") -> ToolSetupRunV1:
    """The stored run, saved as finished (what ``advance`` saves before it resumes the task)."""

    repository = run_repository(agent)
    return repository.save(repository.get(run_id).model_copy(update={"status": RUN_FINISHED}))


@pytest.mark.asyncio
async def test_a_run_seen_ending_twice_at_once_answers_the_paused_turn_once(lead, woken) -> None:
    pause_on_setup(lead)
    run = ended(lead)

    results = await asyncio.gather(*(tool_setup_run_runtime._resume(lead, run, None, None) for _ in range(2)))

    assert sorted(results) == [False, True]
    assert woken == [lead]
    assert setup_answer_held(record_of(lead), AGENT_ID)


@pytest.mark.asyncio
async def test_without_a_live_turn_two_resumes_at_once_queue_the_task_once(lead, woken, monkeypatch) -> None:
    pause_on_setup(lead)
    lead._interrupted_turn_sessions.clear()  # the paused turn is gone
    monkeypatch.setattr(tool_setup_run_runtime, "outcome_for", lambda agent, run: {"status": "ready", "run": run.run_id})
    run = ended(lead)

    results = await asyncio.gather(*(tool_setup_run_runtime._resume(lead, run, None, None) for _ in range(2)))

    manager = lead.task_manager
    assert sorted(results) == [False, True]
    assert record_of(lead) is None
    assert list(manager.task_state.task_queue) == [TASK]
    assert manager.get_task_info(TASK).state == ProgressState.ACCEPTED
    note = manager.read_runtime_resume_directive(TASK)
    assert note.count('"run": "run-1"') == 1 and note.startswith(tool_setup_run_runtime.RUN_ENDED_NOTE)
    resumes = [entry for entry in manager.get_task_history_entries(TASK, limit=None)
               if (entry.details or {}).get("event") == "tool_setup_run_resume"]
    assert len(resumes) == 1
