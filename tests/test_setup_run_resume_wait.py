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


def pause_on_setup(
    agent: SimpleNamespace, run_id: str = "run-1", interrupt_id: str = "i1", *, task_id: str = TASK, page: str = DONE
) -> None:
    """set_up_tools paused the turn: its run (one sign-in page, ``page``), its record and the live session."""

    record_human_interaction_blocker(
        agent.task_manager, task_id, {"kind": TOOL_SETUP_RUN_KIND, "run_id": run_id, "interrupt_id": interrupt_id}
    )
    run_repository(agent).create(
        ToolSetupRunV1(
            run_id=run_id, team_id="team", person_id="user-1", task_id=task_id, agent_id=agent.id,
            agent_type="TeamArchitect", tool_ids=("cli:github",), interrupt_id=interrupt_id,
            pieces=(RunPieceV1(piece_id="sign_in", tool_id="cli:github", kind="sign_in", status=page, attempt=1),),
        )
    )
    agent._interrupted_turn_sessions[task_id] = SimpleNamespace(
        task_id=task_id, interrupts=[SimpleNamespace(interrupt_id=interrupt_id)],
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


# -- answers a paused turn cannot take (A3) ---------------------------------------------------

RESULT = {"status": "ready", "tools": [{"tool_id": "cli:github", "ready": True}]}


def another_waiting_task(agent: SimpleNamespace, task_id: str) -> None:
    """A second task of the agent whose turn paused too."""

    manager = agent.task_manager
    assert manager.add_task(
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST), MessageHeader(from_agent_id="gateway"), MessageMeta(),
        owner_person_id=None,
    )
    assert manager.get_next_task_to_process() == task_id
    manager.update_main_task_state(task_id, ProgressState.WAITING, _now())


def queue_another_task(agent: SimpleNamespace) -> None:
    """Other work already waits in the queue, so 'at the front' means something."""

    assert agent.task_manager.add_task(
        Task(task_id="other", task_type=MessageMetaType.REQUEST), MessageHeader(from_agent_id="gateway"), MessageMeta(),
        owner_person_id=None,
    )
    assert list(agent.task_manager.task_state.task_queue) == ["other"]


def resumed_from_note(agent: SimpleNamespace, note_start: str) -> None:
    """The no-live-turn path: note, runnable, at the front of the queue, record removed, once."""

    manager = agent.task_manager
    assert record_of(agent) is None
    assert manager.get_task_info(TASK).state == ProgressState.ACCEPTED
    assert list(manager.task_state.task_queue) == [TASK, "other"]
    assert manager.read_runtime_resume_directive(TASK).startswith(note_start)


@pytest.mark.asyncio
async def test_a_held_answer_whose_paused_turn_is_gone_resumes_the_task_from_a_note(lead, woken, monkeypatch, caplog) -> None:
    monkeypatch.setattr(tool_setup_run_runtime, "outcome_for", lambda agent, run: RESULT)
    pause_on_setup(lead)
    await tool_setup_run_runtime.advance(lead, "run-1")
    queue_another_task(lead)
    lead._interrupted_turn_sessions.clear()  # the paused turn ended after its answer was held
    seen = stub_turn(monkeypatch, lead)

    with caplog.at_level(logging.WARNING):
        assert await resume_interrupted_task_turn(lead, TASK) is None

    assert seen == [], "no turn to resume"
    resumed_from_note(lead, tool_setup_run_runtime.RUN_ENDED_NOTE)
    assert '"status": "ready"' in lead.task_manager.read_runtime_resume_directive(TASK)
    assert "event=tool_setup_answer_not_taken" in caplog.text and "why=paused_turn_gone" in caplog.text


@pytest.mark.asyncio
async def test_an_answer_the_sdk_cannot_take_resumes_the_task_from_a_note_and_drops_the_turn(
    lead, woken, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(tool_setup_run_runtime, "outcome_for", lambda agent, run: RESULT)
    monkeypatch.setattr(task_steering_runtime, "_build_interrupt_response_blocks", lambda session, steering: [])
    pause_on_setup(lead)
    await tool_setup_run_runtime.advance(lead, "run-1")
    queue_another_task(lead)
    session = lead._interrupted_turn_sessions[TASK]
    seen = stub_turn(monkeypatch, lead)

    with caplog.at_level(logging.WARNING):
        assert await resume_interrupted_task_turn(lead, TASK) is None

    assert seen == []
    resumed_from_note(lead, tool_setup_run_runtime.RUN_ENDED_NOTE)
    assert TASK not in lead._interrupted_turn_sessions and session.strands.replace_before_next_turn
    assert "why=answer_not_converted" in caplog.text and "event=tool_setup_paused_turn_dropped" in caplog.text
    events = [(entry.details or {}).get("event") for entry in lead.task_manager.get_task_history_entries(TASK, limit=None)]
    assert "task_steering_interrupt_response_empty" in events and "tool_setup_run_resume" in events


@pytest.mark.asyncio
async def test_a_held_answer_for_a_run_that_is_not_stored_resumes_saying_so(lead, woken, monkeypatch, caplog) -> None:
    pause_on_setup(lead)
    await tool_setup_run_runtime.advance(lead, "run-1")
    queue_another_task(lead)
    # The answer names a run the store does not have (its row is gone).
    record_human_interaction_blocker(lead.task_manager, TASK, {**record_of(lead), "run_id": "run-gone"})
    lead._pending_interrupt_responses[TASK] = answer("i1", "run-gone")
    lead._interrupted_turn_sessions.clear()

    with caplog.at_level(logging.WARNING):
        await resume_interrupted_task_turn(lead, TASK)

    resumed_from_note(lead, tool_setup_run_runtime.RUN_NOT_FOUND_NOTE)
    assert "event=tool_setup_run_not_found" in caplog.text and "run_id=run-gone" in caplog.text


def text_answer() -> TaskSteering:
    return TaskSteering(
        steering_id="answer-1", steering_type=TaskSteeringType.INTERRUPT_RESPONSE, target_task_id=TASK,
        guidance_text="Use the work account.", status=TaskSteeringStatus.PENDING,
    )


@pytest.mark.parametrize("branch", ["paused_turn_gone", "answer_not_converted"])
@pytest.mark.asyncio
async def test_other_answers_keep_their_handling_in_those_branches(lead, woken, monkeypatch, branch) -> None:
    access = {"kind": ACCESS_REQUEST_BLOCKER, "access_request_id": "req-1", "interrupt_id": "i1"}
    record_human_interaction_blocker(lead.task_manager, TASK, access)
    lead._pending_interrupt_responses[TASK] = text_answer()
    if branch == "answer_not_converted":
        lead._interrupted_turn_sessions[TASK] = SimpleNamespace(
            task_id=TASK, interrupts=[SimpleNamespace(interrupt_id="i1")], strands=SimpleNamespace(replace_before_next_turn=True)
        )
        monkeypatch.setattr(task_steering_runtime, "_build_interrupt_response_blocks", lambda session, steering: [])

    await resume_interrupted_task_turn(lead, TASK)

    assert record_of(lead) == access
    assert lead.task_manager.get_task_info(TASK).state == ProgressState.WAITING
    assert not lead.task_manager.task_state.task_queue and not woken


@pytest.mark.asyncio
async def test_a_failure_while_resuming_from_a_note_leaves_the_record_for_recovery(lead, woken, monkeypatch, caplog) -> None:
    """Review C06: the handoff runs in the drain; raising there would hold back every other task."""
    pause_on_setup(lead)
    await tool_setup_run_runtime.advance(lead, "run-1")
    lead._interrupted_turn_sessions.clear()

    async def unreadable(agent, run_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(tool_setup_run_runtime, "_load", unreadable)

    with caplog.at_level(logging.WARNING):
        assert await resume_interrupted_task_turn(lead, TASK) is None

    # The record stays for recovery, without its mark: no paused turn waits for
    # the answer any more (review F1/F3), so the stuck check may report the task.
    record = record_of(lead)
    assert record["run_id"] == "run-1" and not setup_answer_held(record)
    assert not lead.task_manager.task_state.task_queue
    assert "event=tool_setup_answer_not_taken_failed" in caplog.text and "error_type=RuntimeError" in caplog.text


# -- recovery at start and on each scheduler pass (A4) ----------------------------------------

PASS = 100.0  # a scheduler pass's monotonic time, past any earlier pacing


@pytest.fixture(autouse=True)
def settled_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs saved by a test count as settled; the grace for runs between two steps has its own test."""

    monkeypatch.setattr(tool_setup_run_runtime, "RECENT_RUN_SECONDS", 0.0)


@pytest.fixture
def results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_setup_run_runtime, "outcome_for", lambda agent, run: RESULT)


def count_reads(monkeypatch: pytest.MonkeyPatch) -> list:
    """Calls of the recovery's read of the agent's setup-run records, so a test sees that a pass looked."""

    reads: list = []
    original = tool_setup_run_runtime._setup_run_waits
    monkeypatch.setattr(tool_setup_run_runtime, "_setup_run_waits", lambda agent: reads.append(1) or original(agent))
    return reads


@pytest.mark.asyncio
async def test_a_pass_answers_a_live_turn_whose_run_ended_without_resuming_it(lead, woken, caplog) -> None:
    pause_on_setup(lead)
    ended(lead)  # saved as ended, but the wake-up was lost

    with caplog.at_level(logging.WARNING):
        assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 1

    assert setup_answer_held(record_of(lead), AGENT_ID) and TASK in lead._pending_interrupt_responses
    assert woken == [lead]
    assert "event=tool_setup_run_recovered" in caplog.text and "why=not_resumed" in caplog.text


@pytest.mark.asyncio
async def test_a_pass_resumes_a_task_with_no_live_turn_from_a_note(lead, woken, results) -> None:
    pause_on_setup(lead)
    ended(lead)
    queue_another_task(lead)
    lead._interrupted_turn_sessions.clear()

    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 1

    resumed_from_note(lead, tool_setup_run_runtime.RUN_ENDED_NOTE)
    assert woken == [lead]


@pytest.mark.asyncio
async def test_a_pass_resumes_a_task_whose_run_is_not_stored_and_drops_its_paused_turn(lead, woken, caplog) -> None:
    """begin_run records the wait before it finds the run; a missing run never ends on its own."""
    pause_on_setup(lead)
    queue_another_task(lead)
    record_human_interaction_blocker(lead.task_manager, TASK, {**record_of(lead), "run_id": "run-gone"})

    with caplog.at_level(logging.WARNING):
        assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 1

    resumed_from_note(lead, tool_setup_run_runtime.RUN_NOT_FOUND_NOTE)
    assert TASK not in lead._interrupted_turn_sessions
    assert "event=tool_setup_run_not_found" in caplog.text and "run_id=run-gone" in caplog.text


@pytest.mark.asyncio
async def test_a_pass_leaves_a_run_that_still_waits_for_its_page_alone(lead, woken, monkeypatch) -> None:
    pause_on_setup(lead, page="waiting")

    reads = count_reads(monkeypatch)
    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 0

    assert reads == [1], "the pass looked"
    assert record_of(lead) == {"kind": TOOL_SETUP_RUN_KIND, "run_id": "run-1", "interrupt_id": "i1"}
    assert run_repository(lead).get("run-1").status == "waiting"
    assert not woken and not lead.task_manager.task_state.task_queue


@pytest.mark.asyncio
async def test_a_pass_leaves_an_answer_held_for_a_busy_lane_alone(lead, woken) -> None:
    """The companion of the stuck-task test: the per-pass recovery must not resume a held answer twice."""
    pause_on_setup(lead)
    await tool_setup_run_runtime.advance(lead, "run-1")
    woken.clear()

    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 0

    assert setup_answer_held(record_of(lead), AGENT_ID) and TASK in lead._pending_interrupt_responses
    assert not woken and not lead.task_manager.task_state.task_queue


@pytest.mark.asyncio
async def test_a_pass_finishes_a_run_whose_advance_was_lost(lead, woken, caplog) -> None:
    """Review C04: the last page's result was saved, but the run was never settled."""
    pause_on_setup(lead)  # its one page is done; the run is still saved as waiting

    with caplog.at_level(logging.WARNING):
        assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 1

    assert run_repository(lead).get("run-1").status == RUN_FINISHED
    assert setup_answer_held(record_of(lead), AGENT_ID) and woken == [lead]
    assert "event=tool_setup_run_advanced_on_recovery" in caplog.text


@pytest.mark.asyncio
async def test_a_pass_opens_a_page_whose_advance_was_lost(lead, woken, monkeypatch) -> None:
    """Review C04: an expired page was saved to be offered again, but no new page was opened."""
    pause_on_setup(lead, page="pending")
    opened: list = []

    async def open_page(agent, run, piece, task_info):
        opened.append(piece.piece_id)
        return await tool_setup_run_runtime._save(agent, run.with_piece(piece.model_copy(update={"status": "waiting"})))

    monkeypatch.setattr(tool_setup_run_runtime, "_open", open_page)

    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 0

    assert opened == ["sign_in"]
    assert run_repository(lead).get("run-1").status == "waiting"
    assert record_of(lead)["run_id"] == "run-1" and not lead.task_manager.task_state.task_queue


@pytest.mark.asyncio
async def test_a_pass_never_touches_another_agents_run(lead, woken) -> None:
    """Only this agent's tasks are read; a run of a task it does not hold is left to its holder."""
    run_repository(lead).create(
        ToolSetupRunV1(
            run_id="run-elsewhere", team_id="team", person_id="user-1", task_id="task-of-another-agent",
            agent_id="agent-other", agent_type="TeamArchitect", tool_ids=("cli:github",), interrupt_id="i9",
            status=RUN_FINISHED,
        )
    )

    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 0

    assert run_repository(lead).get("run-elsewhere").status == RUN_FINISHED
    assert not lead.task_manager.task_state.task_queue and not woken


@pytest.mark.asyncio
async def test_a_pass_runs_at_most_once_a_minute(lead, woken, monkeypatch) -> None:
    reads = count_reads(monkeypatch)
    for moment in (PASS, PASS + 30, PASS + tool_setup_run_runtime.SETUP_RUN_RECOVERY_SECONDS + 1):
        await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=moment)
    assert reads == [1, 1]


@pytest.mark.asyncio
async def test_one_task_that_fails_does_not_stop_recovery_for_the_others(lead, woken, results, monkeypatch, caplog) -> None:
    """Review C02."""
    another_waiting_task(lead, "task-two")
    for task_id, run_id in ((TASK, "run-1"), ("task-two", "run-2")):
        pause_on_setup(lead, run_id, f"i-{run_id}", task_id=task_id)
        ended(lead, run_id)
    lead._interrupted_turn_sessions.clear()
    original = tool_setup_run_runtime._resume

    async def fails_for_the_first(agent, run, header, meta, **kwargs):
        if run.task_id == TASK:
            raise RuntimeError("database is locked")
        return await original(agent, run, header, meta, **kwargs)

    monkeypatch.setattr(tool_setup_run_runtime, "_resume", fails_for_the_first)

    with caplog.at_level(logging.WARNING):
        assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 1

    assert lead.task_manager.task_state.task_queue[0] == "task-two"
    assert record_of(lead)["run_id"] == "run-1", "the failed task keeps its record for the next pass"
    failure = next(line for line in caplog.text.splitlines() if "event=tool_setup_run_recovery_failed" in line)
    assert f"task_id={TASK}" in failure and "stage=resume" in failure and "error_type=RuntimeError" in failure


@pytest.mark.asyncio
async def test_a_stop_between_saving_the_note_and_removing_the_record_resumes_once(
    lead, woken, results, monkeypatch, caplog
) -> None:
    """Review C05: the note is saved, then the agent stops (here: the removal fails) before the record goes."""
    pause_on_setup(lead)
    ended(lead)
    lead._interrupted_turn_sessions.clear()
    original = tool_setup_run_runtime.clear_setup_run_blocker
    failures = [RuntimeError("the agent stopped")]

    def stops_once(host, task_id, run_id):
        if failures:
            raise failures.pop()
        return original(host, task_id, run_id)

    monkeypatch.setattr(tool_setup_run_runtime, "clear_setup_run_blocker", stops_once)

    with caplog.at_level(logging.WARNING):
        assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 0
    manager = lead.task_manager
    assert record_of(lead) is not None and manager.read_runtime_resume_directive(TASK)
    assert "event=tool_setup_run_recovery_failed" in caplog.text

    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS + 61) == 1

    assert record_of(lead) is None
    assert list(manager.task_state.task_queue) == [TASK]
    assert manager.read_runtime_resume_directive(TASK).count(tool_setup_run_runtime.RUN_ENDED_NOTE) == 1
    resumes = [entry for entry in manager.get_task_history_entries(TASK, limit=None)
               if (entry.details or {}).get("event") == "tool_setup_run_resume"]
    assert len(resumes) == 1


# -- where recovery runs ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_recovery_queues_without_waking_and_a_failure_does_not_stop_the_start(monkeypatch, caplog) -> None:
    from custom_agents import runtime_recovery

    calls: list = []

    async def recover(agent, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(tool_setup_run_runtime, "resume_ended_setup_runs", recover)
    agent = SimpleNamespace(id="agent-lead", _logger=logging.getLogger("test.setup_run_resume_wait"))

    with caplog.at_level(logging.ERROR):
        await runtime_recovery._resume_ended_setup_runs(agent)

    assert calls == [{"at_start": True}]
    assert "event=setup_run_recovery_failed" in caplog.text


@pytest.mark.parametrize("fails", [False, True], ids=["runs", "fails"])
@pytest.mark.asyncio
async def test_each_scheduler_pass_runs_the_recovery_and_goes_on_after_a_failure(monkeypatch, caplog, fails) -> None:
    from custom_agents import scheduled_runtime
    from tests.test_schedule_tools import _LoopAgent

    agent = _LoopAgent(can_start=False)
    calls: list = []
    alerts: list = []
    order: list = []

    async def recover(item):
        calls.append(item)
        order.append("recovery")
        if fails:
            raise RuntimeError("database is locked")
        return 0

    async def nothing(_agent):
        return None

    async def report(_agent):
        alerts.append(_agent)
        order.append("stuck check")

    monkeypatch.setattr(scheduled_runtime, "resume_ended_setup_runs", recover)
    monkeypatch.setattr(scheduled_runtime, "resume_decided_access_waits", nothing)
    monkeypatch.setattr(scheduled_runtime, "take_over_orphaned_work", nothing)
    monkeypatch.setattr(scheduled_runtime, "report_stuck_tasks", report)

    with caplog.at_level(logging.ERROR):
        await scheduled_runtime._scheduler_pass(agent, {}, asyncio.sleep)

    assert calls == [agent] and alerts == [agent]
    # Review F8 (EVD-03): recovery resumes what it can before the stuck check reports.
    assert order == ["recovery", "stuck check"]
    assert ("event=setup_run_recovery_failed" in caplog.text) is fails


@pytest.mark.asyncio
async def test_a_pass_leaves_a_run_another_step_is_working_on(lead, woken, monkeypatch, caplog) -> None:
    """Review C07: a run in the middle of its own step is not a lost wake-up, so no warning names it."""
    pause_on_setup(lead)  # the page is done, but the run is not settled yet: still_waits is false

    lock = tool_setup_run_runtime._lock(lead, "run-1")
    with caplog.at_level(logging.WARNING):
        async with lock:  # advance, begin_run or _resume holds it
            assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 0
        monkeypatch.setattr(tool_setup_run_runtime, "RECENT_RUN_SECONDS", 30.0)
        assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS + 61) == 0  # just saved
    assert run_repository(lead).get("run-1").status == "waiting"
    assert "event=tool_setup_run_" not in caplog.text and not woken

    monkeypatch.setattr(tool_setup_run_runtime, "RECENT_RUN_SECONDS", 0.0)  # later: the run has settled
    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS + 122) == 1
    assert run_repository(lead).get("run-1").status == RUN_FINISHED


@pytest.mark.asyncio
async def test_start_recovery_does_not_wait_for_a_recently_saved_run(lead, woken, results, monkeypatch) -> None:
    """At start no step of the new process can be part-way through a run: a run the old one just saved is recovered."""
    monkeypatch.setattr(tool_setup_run_runtime, "RECENT_RUN_SECONDS", 30.0)
    pause_on_setup(lead)
    ended(lead)
    lead._interrupted_turn_sessions.clear()

    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS, at_start=True) == 1

    assert record_of(lead) is None and list(lead.task_manager.task_state.task_queue) == [TASK]
    assert not woken, "startup wakes the drain itself"


# -- project review fixes: a mark only while its answer is held; a failed resume is retried ----


def persist_fails_once(monkeypatch: pytest.MonkeyPatch, manager: TaskManager) -> None:
    """The next runtime-state save raises, as when the database is locked; later saves work."""

    original = manager._persist_runtime_state
    failures = [RuntimeError("database is locked")]

    def persist(*args, **kwargs):
        if failures:
            raise failures.pop()
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_persist_runtime_state", persist)


@pytest.mark.asyncio
async def test_a_failed_save_of_the_mark_leaves_the_record_unmarked_for_recovery(lead, woken, monkeypatch) -> None:
    """Review F1 (GEN-1): a mark saved without its answer would hide a stranded task from the stuck check."""
    pause_on_setup(lead)
    persist_fails_once(monkeypatch, lead.task_manager)

    with pytest.raises(RuntimeError):
        await tool_setup_run_runtime.advance(lead, "run-1")

    assert record_of(lead) == {"kind": TOOL_SETUP_RUN_KIND, "run_id": "run-1", "interrupt_id": "i1"}
    assert TASK not in lead._pending_interrupt_responses and not woken
    # The next pass answers the paused turn, once.
    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 1
    assert setup_answer_held(record_of(lead), AGENT_ID) and TASK in lead._pending_interrupt_responses
    assert woken == [lead]
    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS + 61) == 0


@pytest.mark.parametrize("step", ["the removal's save", "the history save", "the state change"])
@pytest.mark.asyncio
async def test_a_failure_after_the_record_is_removed_puts_it_back_and_the_next_pass_resumes_once(
    lead, woken, results, monkeypatch, caplog, step
) -> None:
    """Review F2 (FAIL-02): the record is what lets recovery try again."""
    pause_on_setup(lead)
    ended(lead)
    lead._interrupted_turn_sessions.clear()
    manager = lead.task_manager
    failures = [RuntimeError("database is locked")]
    if step == "the removal's save":
        original = tool_setup_run_runtime.clear_setup_run_blocker

        def removal(host, task_id, run_id):
            if failures:
                persist_fails_once(monkeypatch, host)
                failures.pop()
            return original(host, task_id, run_id)

        monkeypatch.setattr(tool_setup_run_runtime, "clear_setup_run_blocker", removal)
    elif step == "the history save":
        original_history = tool_setup_run_runtime._history

        async def history(*args, **kwargs):
            if failures:
                raise failures.pop()
            return await original_history(*args, **kwargs)

        monkeypatch.setattr(tool_setup_run_runtime, "_history", history)
    else:
        original_state = manager.update_main_task_state

        def update_state(*args, **kwargs):
            if failures:
                raise failures.pop()
            return original_state(*args, **kwargs)

        monkeypatch.setattr(manager, "update_main_task_state", update_state)

    with caplog.at_level(logging.WARNING):
        assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 0

    assert record_of(lead) == {"kind": TOOL_SETUP_RUN_KIND, "run_id": "run-1", "interrupt_id": "i1"}, "put back"
    assert not manager.task_state.task_queue
    assert "event=tool_setup_run_recovery_failed" in caplog.text and "event=tool_setup_run_record_restored" in caplog.text

    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS + 61) == 1

    assert record_of(lead) is None and list(manager.task_state.task_queue) == [TASK]
    assert manager.get_task_info(TASK).state == ProgressState.ACCEPTED
    assert manager.read_runtime_resume_directive(TASK).count(tool_setup_run_runtime.RUN_ENDED_NOTE) == 1


@pytest.mark.asyncio
async def test_a_dropped_turn_takes_the_mark_with_it_even_when_the_resume_then_fails(lead, woken, monkeypatch) -> None:
    """Review F3 (CRIT-1): no paused turn and no answer, so nothing may claim an answer is held."""
    pause_on_setup(lead)
    await tool_setup_run_runtime.advance(lead, "run-1")
    lead._interrupted_turn_sessions.clear()  # the paused turn ended after its answer was held

    def unreadable(agent, run):
        raise RuntimeError("the catalog could not be read")

    monkeypatch.setattr(tool_setup_run_runtime, "outcome_for", unreadable)

    assert await resume_interrupted_task_turn(lead, TASK) is None

    record = record_of(lead)
    assert record["run_id"] == "run-1" and not setup_answer_held(record)


@pytest.mark.asyncio
async def test_start_recovery_removes_every_mark_even_when_it_cannot_resume_the_task_yet(lead, woken, monkeypatch, caplog) -> None:
    """Review F4 (GEN-2): an agent whose id is fixed in its configuration keeps that id across a restart."""
    pause_on_setup(lead)
    await tool_setup_run_runtime.advance(lead, "run-1")
    lead._interrupted_turn_sessions.clear()
    lead._pending_interrupt_responses.clear()  # the restart: same agent id, nothing in memory

    async def unreadable(agent, run_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(tool_setup_run_runtime, "_load", unreadable)

    with caplog.at_level(logging.INFO):
        assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS, at_start=True) == 0

    assert not setup_answer_held(record_of(lead)), "the stuck check can report it if recovery keeps failing"
    assert "event=tool_setup_answer_marks_cleared" in caplog.text


@pytest.mark.parametrize("marked", [True, False], ids=["marked record", "unmarked record"])
@pytest.mark.asyncio
async def test_a_resumed_turn_holding_the_setup_interrupt_ends_only_a_marked_record(lead, woken, monkeypatch, marked) -> None:
    """Review F5 (CONC-2): another answer replaced the held setup answer, but the run has ended.

    ``set_up_tools`` re-reads the run, so the turn still gets its result. An
    unmarked record's run still waits; that record must stay to resume the
    task later.
    """
    pause_on_setup(lead, page="done" if marked else "waiting")
    if marked:
        await tool_setup_run_runtime.advance(lead, "run-1")
    lead._pending_interrupt_responses[TASK] = text_answer()  # replaces the held answer, if any
    seen = stub_turn(monkeypatch, lead)

    await resume_interrupted_task_turn(lead, TASK)

    if marked:
        assert seen == [None] and record_of(lead) is None
    else:
        assert [item["run_id"] for item in seen] == ["run-1"] and record_of(lead)["run_id"] == "run-1"


def test_an_answer_replacing_a_held_setup_answer_is_logged(lead, caplog) -> None:
    """Review F5 (CONC-2)."""
    lead._pending_interrupt_responses[TASK] = answer("i1", "run-1")

    with caplog.at_level(logging.WARNING):
        task_steering_runtime._note_replaced_setup_answer(lead, TASK, text_answer())
        task_steering_runtime._note_replaced_setup_answer(lead, "task-without-a-held-answer", text_answer())

    assert caplog.text.count("event=tool_setup_answer_replaced") == 1
    assert f"task_id={TASK}" in caplog.text and "replaced_steering_id=tool_setup_resume_run-1" in caplog.text


@pytest.mark.asyncio
async def test_a_pass_skips_a_finished_task_that_still_has_a_record(lead, woken, monkeypatch) -> None:
    """Review F9 (EVD-04): a finished task never resumes; ending a task normally removes its record too."""
    pause_on_setup(lead)
    ended(lead)
    lead._interrupted_turn_sessions.clear()
    lead.task_manager.update_main_task_state(TASK, ProgressState.COMPLETED, _now())
    reads = count_reads(monkeypatch)

    assert await tool_setup_run_runtime.resume_ended_setup_runs(lead, now=PASS) == 0

    assert reads == [1]
    assert record_of(lead) is not None and not lead.task_manager.task_state.task_queue and not woken
