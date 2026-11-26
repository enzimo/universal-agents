"""Tests for generalized local-wait blocker records and shared paused-wait resume helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents import paused_wait_resume
from custom_agents.task_human_interaction_blockers import (
    ACCESS_REQUEST_BLOCKER,
    ANSWER_HELD_AT_KEY,
    ANSWER_HELD_BY_KEY,
    SETUP_RUN_BLOCKER,
    TIMED_WAIT_BLOCKER,
    answer_held,
    clear_answer_marks,
    clear_answered_blocker,
    clear_wait_record,
    get_human_interaction_blocker,
    is_setup_run_blocker,
    is_timed_wait_blocker,
    is_wait_blocker,
    mark_answer_held,
    record_human_interaction_blocker,
    remove_answer_mark,
)
from custom_agents.task_manager import TaskManager
from custom_agents.timed_waits import TIMED_WAIT_RESPONSE_KIND
from custom_agents.tool_setup_runs import TOOL_SETUP_RUN_RESPONSE_KIND
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, ProgressState, TaskSteeringStatus, TaskSteeringType

TASK = "task-wait"
AGENT_ID = "agent-worker"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def manager(tmp_path: Path) -> TaskManager:
    mgr = TaskManager(db_path=str(tmp_path / "manager.db"))
    mgr.set_owner_identity(AGENT_ID, "Worker", "team", "Worker")
    assert mgr.add_task(
        Task(task_id=TASK, task_type=MessageMetaType.REQUEST),
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(),
        owner_person_id=None,
    )
    assert mgr.get_next_task_to_process() == TASK
    mgr.update_main_task_state(TASK, ProgressState.WAITING, _now())
    return mgr


def _timed_record(interrupt_id: str = "i-wait", *, held_by: str | None = None) -> dict:
    record = {
        "kind": TIMED_WAIT_BLOCKER,
        "interrupt_id": interrupt_id,
        "minutes": 10,
        "resume_at": "2026-11-26T06:00:00+00:00",
        "reason": "wait for redeploy",
    }
    if held_by is not None:
        record |= {ANSWER_HELD_AT_KEY: _now(), ANSWER_HELD_BY_KEY: held_by}
    return record


def test_wait_blocker_predicates_and_mark_lifecycle_for_timed_wait(manager: TaskManager) -> None:
    record = _timed_record("i-wait")
    assert is_timed_wait_blocker(record)
    assert is_timed_wait_blocker(record, "i-wait")
    assert not is_timed_wait_blocker(record, "i-other")
    assert not is_setup_run_blocker(record)
    assert not is_wait_blocker(record, ACCESS_REQUEST_BLOCKER)

    record_human_interaction_blocker(manager, TASK, record)
    assert not answer_held(get_human_interaction_blocker(manager, TASK))

    # Wrong kind or wrong wait id refuses to mark.
    assert not mark_answer_held(manager, TASK, SETUP_RUN_BLOCKER, "i-wait", holder_id=AGENT_ID, at=_now())
    assert not mark_answer_held(manager, TASK, TIMED_WAIT_BLOCKER, "i-other", holder_id=AGENT_ID, at=_now())

    # Compare-and-set marks once.
    assert mark_answer_held(manager, TASK, TIMED_WAIT_BLOCKER, "i-wait", holder_id=AGENT_ID, at=_now())
    assert not mark_answer_held(manager, TASK, TIMED_WAIT_BLOCKER, "i-wait", holder_id=AGENT_ID, at=_now())

    stored = get_human_interaction_blocker(manager, TASK)
    assert answer_held(stored)
    assert answer_held(stored, AGENT_ID)
    assert not answer_held(stored, "agent-other")

    # Removing the mark keeps the record unmarked.
    assert remove_answer_mark(manager, TASK, TIMED_WAIT_BLOCKER, "i-other") is None
    unmarked = remove_answer_mark(manager, TASK, TIMED_WAIT_BLOCKER, "i-wait")
    assert unmarked == record
    assert not answer_held(get_human_interaction_blocker(manager, TASK))

    # Clearing only matches the same kind and wait id.
    assert not clear_wait_record(manager, TASK, TIMED_WAIT_BLOCKER, "i-other")
    assert not clear_wait_record(manager, TASK, SETUP_RUN_BLOCKER, "i-wait")
    assert clear_wait_record(manager, TASK, TIMED_WAIT_BLOCKER, "i-wait")
    assert get_human_interaction_blocker(manager, TASK) is None


def test_clear_answer_marks_clears_both_setup_and_timed_wait_marks(manager: TaskManager) -> None:
    task_two = "task-setup"
    assert manager.add_task(
        Task(task_id=task_two, task_type=MessageMetaType.REQUEST),
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(),
        owner_person_id=None,
    )
    record_human_interaction_blocker(manager, TASK, _timed_record("i-wait", held_by=AGENT_ID))
    record_human_interaction_blocker(
        manager,
        task_two,
        {
            "kind": SETUP_RUN_BLOCKER,
            "run_id": "run-1",
            "interrupt_id": "i-setup",
            ANSWER_HELD_AT_KEY: _now(),
            ANSWER_HELD_BY_KEY: AGENT_ID,
        },
    )

    cleared = clear_answer_marks(manager)
    assert set(cleared) == {TASK, task_two}
    assert not answer_held(get_human_interaction_blocker(manager, TASK))
    assert not answer_held(get_human_interaction_blocker(manager, task_two))


@pytest.mark.parametrize(
    ("record", "answers", "resumed_ids", "answered_ids", "expected"),
    [
        # Unmarked timed wait cleared by any answer to its interrupt id (including stray text answers).
        (_timed_record("i1"), [], ["i1"], ["i1"], True),
        (_timed_record("i1"), [("i1", {"kind": TIMED_WAIT_RESPONSE_KIND})], [], [], True),
        # Unmarked timed wait NOT cleared when another interrupt in the same step is answered.
        (_timed_record("i1"), [("i2", {"access_request_id": "req-1"})], ["i1", "i2"], ["i2"], False),
        # Marked timed wait cleared when its session resumes, even if another answer replaced the held one (C01).
        (_timed_record("i1", held_by=AGENT_ID), [], ["i1", "i2"], ["i2"], True),
        # Marked timed wait NOT cleared if its interrupt is not in the resumed session.
        (_timed_record("i1", held_by=AGENT_ID), [], ["i2"], ["i2"], False),
        # Unmarked setup run is NOT cleared by answered_interrupt_ids alone (must match its run answer).
        ({"kind": SETUP_RUN_BLOCKER, "run_id": "run-1", "interrupt_id": "i1"}, [], ["i1"], ["i1"], False),
        (
            {"kind": SETUP_RUN_BLOCKER, "run_id": "run-1", "interrupt_id": "i1"},
            [("i1", {"kind": TOOL_SETUP_RUN_RESPONSE_KIND, "run_id": "run-1"})],
            ["i1"],
            ["i1"],
            True,
        ),
        # Access request is NOT cleared by answered_interrupt_ids alone.
        (
            {"kind": ACCESS_REQUEST_BLOCKER, "access_request_id": "req-1", "access_request_ids": ["req-1"], "interrupt_id": "i1"},
            [],
            ["i1"],
            ["i1"],
            False,
        ),
    ],
)
def test_clear_answered_blocker_timed_wait_and_existing_rules(
    manager: TaskManager,
    record: dict,
    answers: list,
    resumed_ids: list[str],
    answered_ids: list[str],
    expected: bool,
) -> None:
    record_human_interaction_blocker(manager, TASK, record)
    removed = clear_answered_blocker(
        manager,
        TASK,
        answers,
        resumed_interrupt_ids=resumed_ids,
        answered_interrupt_ids=answered_ids,
    )
    assert removed is expected
    assert (get_human_interaction_blocker(manager, TASK) is None) is expected


# -- shared paused_wait_resume helpers (Unit 9) ---------------------------------------------


def _fake_agent(manager: TaskManager, *, interrupt_id: str | None = "i-wait") -> SimpleNamespace:
    sessions = {}
    if interrupt_id is not None:
        sessions[TASK] = SimpleNamespace(
            task_id=TASK,
            interrupts=[SimpleNamespace(interrupt_id=interrupt_id)],
            strands=SimpleNamespace(replace_before_next_turn=False),
        )
    return SimpleNamespace(
        id=AGENT_ID,
        task_manager=manager,
        _interrupted_turn_sessions=sessions,
        _pending_interrupt_responses={},
    )


def _timed_steering(interrupt_id: str = "i-wait") -> TaskSteering:
    return TaskSteering(
        steering_id=f"timed_wait_resume_{interrupt_id}",
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id=TASK,
        target_agent_id=AGENT_ID,
        interrupt_responses=[{"interrupt_id": interrupt_id, "response": {"kind": TIMED_WAIT_RESPONSE_KIND}}],
        status=TaskSteeringStatus.PENDING,
    )


@pytest.mark.asyncio
async def test_hand_answer_to_paused_turn_refuses_to_overwrite_when_replace_pending_is_false(
    manager: TaskManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C01: the timed-wait path never replaces an answer already pending for the task."""
    woken: list = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)
    record_human_interaction_blocker(manager, TASK, _timed_record("i-wait"))
    agent = _fake_agent(manager, interrupt_id="i-wait")
    existing = TaskSteering(
        steering_id="existing-answer",
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id=TASK,
        guidance_text="already pending",
        status=TaskSteeringStatus.PENDING,
    )
    agent._pending_interrupt_responses[TASK] = existing

    held = await paused_wait_resume.hand_answer_to_paused_turn(
        agent,
        task_id=TASK,
        kind=TIMED_WAIT_BLOCKER,
        wait_id="i-wait",
        steering=_timed_steering("i-wait"),
        replace_pending=False,
    )

    assert held is False
    assert agent._pending_interrupt_responses[TASK] is existing
    assert not answer_held(get_human_interaction_blocker(manager, TASK))
    assert not woken


@pytest.mark.asyncio
async def test_hand_answer_to_paused_turn_unmarks_if_other_answer_arrives_during_save(
    manager: TaskManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C11: if another answer arrives while mark_answer_held is saving, undo the mark and leave it."""
    woken: list = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)
    record_human_interaction_blocker(manager, TASK, _timed_record("i-wait"))
    agent = _fake_agent(manager, interrupt_id="i-wait")
    arrived = TaskSteering(
        steering_id="person-answer-mid-save",
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id=TASK,
        guidance_text="arrived while marking",
        status=TaskSteeringStatus.PENDING,
    )
    original_run = manager.operations_runtime.run
    placed = False

    async def racing_run(fn, *args, **kwargs):
        nonlocal placed
        result = await original_run(fn, *args, **kwargs)
        if not placed and result is True:
            placed = True
            agent._pending_interrupt_responses[TASK] = arrived
        return result

    monkeypatch.setattr(manager.operations_runtime, "run", racing_run)

    held = await paused_wait_resume.hand_answer_to_paused_turn(
        agent,
        task_id=TASK,
        kind=TIMED_WAIT_BLOCKER,
        wait_id="i-wait",
        steering=_timed_steering("i-wait"),
        replace_pending=False,
    )

    assert placed is True
    assert held is False
    assert agent._pending_interrupt_responses[TASK] is arrived
    assert not answer_held(get_human_interaction_blocker(manager, TASK))
    assert not woken


@pytest.mark.asyncio
async def test_hand_answer_to_paused_turn_unmarks_if_turn_stops_during_save(
    manager: TaskManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the paused turn disappears while mark_answer_held is saving, remove the mark and leave no orphan answer."""

    woken: list = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)
    record_human_interaction_blocker(manager, TASK, _timed_record("i-wait"))
    agent = _fake_agent(manager, interrupt_id="i-wait")
    original_run = manager.operations_runtime.run
    stopped = False

    async def racing_stop(fn, *args, **kwargs):
        nonlocal stopped
        result = await original_run(fn, *args, **kwargs)
        if not stopped and result is True:
            stopped = True
            agent._interrupted_turn_sessions.pop(TASK, None)
        return result

    monkeypatch.setattr(manager.operations_runtime, "run", racing_stop)

    held = await paused_wait_resume.hand_answer_to_paused_turn(
        agent,
        task_id=TASK,
        kind=TIMED_WAIT_BLOCKER,
        wait_id="i-wait",
        steering=_timed_steering("i-wait"),
        interrupt_id="i-wait",
        replace_pending=False,
    )

    assert stopped is True
    assert held is False
    assert TASK not in agent._pending_interrupt_responses
    assert not answer_held(get_human_interaction_blocker(manager, TASK))
    assert not woken


@pytest.mark.asyncio
async def test_hand_answer_to_paused_turn_marks_and_wakes_for_timed_wait(
    manager: TaskManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    woken: list = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)
    record_human_interaction_blocker(manager, TASK, _timed_record("i-wait"))
    agent = _fake_agent(manager, interrupt_id="i-wait")
    steering = _timed_steering("i-wait")

    assert paused_wait_resume.paused_turn_waits(agent, TASK, "i-wait")
    assert not paused_wait_resume.paused_turn_waits(agent, TASK, "")
    assert not paused_wait_resume.paused_turn_waits(agent, TASK, "i-other")

    held = await paused_wait_resume.hand_answer_to_paused_turn(
        agent,
        task_id=TASK,
        kind=TIMED_WAIT_BLOCKER,
        wait_id="i-wait",
        steering=steering,
        replace_pending=False,
    )

    assert held is True
    assert agent._pending_interrupt_responses[TASK] is steering
    assert answer_held(get_human_interaction_blocker(manager, TASK), AGENT_ID)
    assert woken == [agent]


@pytest.mark.asyncio
async def test_drop_paused_turn_and_resume_from_note_for_timed_wait(
    manager: TaskManager, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    woken: list = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)
    log_spec = paused_wait_resume.WaitLogSpec(
        label="timed-wait",
        dropped_event="timed_wait_paused_turn_dropped",
    )
    record_human_interaction_blocker(manager, TASK, _timed_record("i-wait", held_by=AGENT_ID))
    agent = _fake_agent(manager, interrupt_id="i-wait")
    session = agent._interrupted_turn_sessions[TASK]

    with caplog.at_level(logging.WARNING):
        dropped = paused_wait_resume.drop_paused_turn(agent, TASK, "i-wait", why="paused_turn_gone", log=log_spec)
    assert dropped is True
    assert TASK not in agent._interrupted_turn_sessions
    assert session.strands.replace_before_next_turn is True
    assert "event=timed_wait_paused_turn_dropped" in caplog.text

    resumed = await paused_wait_resume.resume_from_note(
        agent,
        task_id=TASK,
        kind=TIMED_WAIT_BLOCKER,
        wait_id="i-wait",
        note="The 10-minute wait ended at 2026-11-26T06:00:00Z.",
        history="Timed wait ended; the task resumes.",
        history_details={"event": "timed_wait_ended", "interrupt_id": "i-wait", "how": "note"},
        wake=True,
    )

    assert resumed is True
    assert get_human_interaction_blocker(manager, TASK) is None
    assert manager.get_task_info(TASK).state == ProgressState.ACCEPTED
    assert list(manager.task_state.task_queue) == [TASK]
    assert "10-minute wait ended" in manager.read_runtime_resume_directive(TASK)
    assert woken == [agent]


@pytest.mark.parametrize(
    ("kind", "wait_id", "record", "parent_task_id"),
    [
        (TIMED_WAIT_BLOCKER, "i-top", _timed_record("i-top"), None),
        (TIMED_WAIT_BLOCKER, "i-del", _timed_record("i-del"), "parent-on-lead"),
        (
            SETUP_RUN_BLOCKER,
            "run-1",
            {"kind": SETUP_RUN_BLOCKER, "run_id": "run-1", "interrupt_id": "i-setup"},
            None,
        ),
    ],
    ids=["top_level_timed_wait", "delegated_timed_wait", "setup_run_wait"],
)
def test_c02_atomic_resume_wait_from_note_matches_multi_step_sequence(
    tmp_path: Path,
    kind: str,
    wait_id: str,
    record: dict,
    parent_task_id: str | None,
) -> None:
    """C02 differential test: single-write resume_wait_from_note produces the same persisted state as the old sequence."""
    from custom_agents.task_state_transitions import resume_wait_from_note
    from custom_messages.enums import TaskHistoryImportance, TaskHistoryType

    ts = "2026-11-26T06:10:00+00:00"
    note = "Wait ended; continue."
    history = "Wait ended; task resumed."
    details = {"event": "wait_ended", "id": wait_id}

    def _setup(name: str) -> TaskManager:
        mgr = TaskManager(db_path=str(tmp_path / f"{name}.db"))
        mgr.set_owner_identity(AGENT_ID, "Worker", "team", "Worker")
        assert mgr.add_task(
            Task(task_id=TASK, task_type=MessageMetaType.REQUEST, parent_task_id=parent_task_id),
            MessageHeader(from_agent_id="lead"),
            MessageMeta(),
            owner_person_id=None,
        )
        assert mgr.get_next_task_to_process() == TASK
        mgr.update_main_task_state(TASK, ProgressState.WAITING, "2026-11-26T06:00:00+00:00")
        record_human_interaction_blocker(mgr, TASK, record)
        return mgr

    old_mgr = _setup("old")
    old_mgr.append_runtime_resume_directive(TASK, note)
    assert clear_wait_record(old_mgr, TASK, kind, wait_id)
    old_mgr.append_history_entry(
        TASK,
        history,
        entry_type=TaskHistoryType.QUERY_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=ts,
        details=details,
    )
    old_mgr.update_main_task_state(TASK, ProgressState.ACCEPTED, ts)
    assert old_mgr.enqueue_task(TASK, front=True)

    new_mgr = _setup("new")
    persist_calls: list[int] = []
    orig_persist = new_mgr._persist_runtime_state

    def counting_persist(*args, **kwargs):
        persist_calls.append(1)
        return orig_persist(*args, **kwargs)

    new_mgr._persist_runtime_state = counting_persist  # type: ignore[method-assign]
    assert resume_wait_from_note(
        new_mgr,
        TASK,
        kind=kind,
        wait_id=wait_id,
        note=note,
        history=history,
        history_details=details,
        timestamp=ts,
        front=True,
    )
    assert len(persist_calls) == 1

    # Compare all durable fields (excluding generated history entry_ids).
    old_info, new_info = old_mgr.get_task_info(TASK), new_mgr.get_task_info(TASK)
    assert new_info.state == old_info.state == ProgressState.ACCEPTED
    assert [s for s, _ in new_info.task.task_state] == [s for s, _ in old_info.task.task_state]
    assert new_info.task.task_state[1:] == old_info.task.task_state[1:]
    assert list(new_mgr.task_state.task_queue) == list(old_mgr.task_state.task_queue) == [TASK]
    assert new_mgr.task_state.waiting_top_level_task_ids == old_mgr.task_state.waiting_top_level_task_ids
    assert new_mgr.task_state.current_in_progress_task_id == old_mgr.task_state.current_in_progress_task_id
    assert new_mgr.task_state.human_interaction_blockers == old_mgr.task_state.human_interaction_blockers == {}
    assert new_mgr.read_runtime_resume_directive(TASK) == old_mgr.read_runtime_resume_directive(TASK) == note
    old_hist = [(e.entry_type, e.importance, e.summary, e.details, e.timestamp) for e in old_info.task.execution_history]
    new_hist = [(e.entry_type, e.importance, e.summary, e.details, e.timestamp) for e in new_info.task.execution_history]
    assert [row[:-1] for row in new_hist] == [row[:-1] for row in old_hist]
    assert new_hist[-2:] == old_hist[-2:]
    reloaded = TaskManager(db_path=str(tmp_path / "new.db"))
    reloaded.set_owner_identity(AGENT_ID, "Worker", "team", "Worker")
    reloaded.restore_runtime_state()
    assert reloaded.get_task_info(TASK).state == ProgressState.ACCEPTED
    assert list(reloaded.task_state.task_queue) == [TASK]
    assert reloaded.read_runtime_resume_directive(TASK) == note
    assert get_human_interaction_blocker(reloaded, TASK) is None


def test_c02_resume_wait_from_note_rolls_back_when_task_not_ready(
    manager: TaskManager, caplog: pytest.LogCaptureFixture
) -> None:
    """C02: if another hold (such as a pending recoverable pause) blocks readiness, every mutation rolls back."""
    from custom_agents.task_state_transitions import resume_wait_from_note
    from custom_messages.enums import RecoverablePauseTrigger

    record_human_interaction_blocker(manager, TASK, _timed_record("i-wait"))
    manager.create_recoverable_pause(
        TASK,
        trigger=RecoverablePauseTrigger.TOOL_CALL_BUDGET_EXCEEDED,
        friendly_reason="Waiting for operator approval to continue.",
    )
    before = manager._export_runtime_state()

    with caplog.at_level(logging.WARNING):
        resumed = resume_wait_from_note(
            manager,
            TASK,
            kind=TIMED_WAIT_BLOCKER,
            wait_id="i-wait",
            note="Should roll back.",
            history="Wait ended.",
            history_details={"event": "timed_wait_ended"},
            timestamp=_now(),
        )

    assert resumed is False
    assert "event=wait_resume_not_ready" in caplog.text
    assert manager._export_runtime_state() == before
    assert get_human_interaction_blocker(manager, TASK) is not None
    assert manager.read_runtime_resume_directive(TASK) is None
    assert list(manager.task_state.task_queue) == []


def test_c03_resume_wait_from_note_handles_custody_loss_and_persist_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C03: persist error rolls back; TaskCustodyTakenOver returns False; RuntimeCustodyLost stays emptied."""
    from custom_agents.task_state_transitions import resume_wait_from_note
    from storage.runtime_custody import CustodyStore, RuntimeCustodyLost

    def _fresh(name: str) -> TaskManager:
        mgr = TaskManager(db_path=str(tmp_path / f"{name}.db"))
        mgr.set_owner_identity(AGENT_ID, "Worker", "team", "Worker")
        assert mgr.add_task(
            Task(task_id=TASK, task_type=MessageMetaType.REQUEST),
            MessageHeader(from_agent_id="gateway"),
            MessageMeta(),
            owner_person_id=None,
        )
        assert mgr.get_next_task_to_process() == TASK
        mgr.update_main_task_state(TASK, ProgressState.WAITING, _now())
        record_human_interaction_blocker(mgr, TASK, _timed_record("i-wait"))
        return mgr

    # 1. Transient DB error: rolls back completely.
    mgr_err = _fresh("err")
    before_err = mgr_err._export_runtime_state()
    monkeypatch.setattr(mgr_err, "_persist_runtime_state", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("locked")))
    with pytest.raises(RuntimeError, match="locked"):
        resume_wait_from_note(
            mgr_err, TASK, kind=TIMED_WAIT_BLOCKER, wait_id="i-wait",
            note="Note", history="Ended", history_details={}, timestamp=_now(),
        )
    assert mgr_err._export_runtime_state() == before_err

    # 2. TaskCustodyTakenOver: drops just this task and returns normally -> returns False.
    mgr_taken = _fresh("taken")
    monkeypatch.setattr(mgr_taken, "_persist_runtime_state", lambda *_a, **_k: mgr_taken._forget_task(TASK))
    assert (
        resume_wait_from_note(
            mgr_taken, TASK, kind=TIMED_WAIT_BLOCKER, wait_id="i-wait",
            note="Note", history="Ended", history_details={}, timestamp=_now(),
        )
        is False
    )
    assert mgr_taken.get_task_info(TASK) is None

    # 3. RuntimeCustodyLost: empties state and raises -> rollback does not re-populate emptied state.
    mgr_lost = _fresh("lost")

    def lose_all(*_a, **_k):
        lost = RuntimeCustodyLost(AGENT_ID, CustodyStore.TASK_RUNTIME)
        mgr_lost.runtime_snapshot_coordinator._release_lost_custody(lost)
        raise lost

    monkeypatch.setattr(mgr_lost, "_persist_runtime_state", lose_all)
    with pytest.raises(RuntimeCustodyLost):
        resume_wait_from_note(
            mgr_lost, TASK, kind=TIMED_WAIT_BLOCKER, wait_id="i-wait",
            note="Note", history="Ended", history_details={}, timestamp=_now(),
        )
    assert mgr_lost.task_state.tasks == {}
    assert mgr_lost.task_state.human_interaction_blockers == {}
    assert list(mgr_lost.task_state.task_queue) == []


def test_c01_clear_answered_blocker_with_resume_note_and_turn_start_notes_release(
    manager: TaskManager,
) -> None:
    """C01: clearing a timed-wait blocker saves its resume note atomically, and TurnStartNotes releases only it."""
    from custom_agents.task_runtime_state import TurnStartNotes
    from custom_agents.task_state_transitions import clear_answered_blocker_with_resume_note

    record_human_interaction_blocker(manager, TASK, _timed_record("i-wait", held_by=AGENT_ID))
    manager.append_runtime_resume_directive(TASK, "Earlier restart note.")
    manager.append_runtime_resume_directive(TASK, "Mid-pause correction.")

    cleared, note = clear_answered_blocker_with_resume_note(
        manager,
        TASK,
        answers=[("i-wait", {"kind": TIMED_WAIT_RESPONSE_KIND})],
        resumed_interrupt_ids=["i-wait"],
        answered_interrupt_ids=["i-wait"],
        now=datetime(2026, 11, 26, 6, 10, 0, tzinfo=timezone.utc),
    )
    assert cleared is True
    assert note is not None and "10-minute wait (wait for redeploy) ended" in note
    assert get_human_interaction_blocker(manager, TASK) is None
    stored = manager.read_runtime_resume_directive(TASK) or ""
    assert "Earlier restart note." in stored and "Mid-pause correction." in stored and note in stored

    # Releasing start notes removes what the turn read + the live-resumed timed-wait note, keeping the mid-pause note.
    start_notes = TurnStartNotes.with_trailing_directive(
        TurnStartNotes.read(TASK, "Earlier restart note.", None),
        TASK,
        note,
    )
    assert start_notes is not None
    manager.release_turn_start_notes(start_notes)
    assert manager.read_runtime_resume_directive(TASK) == "Mid-pause correction."
