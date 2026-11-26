"""Deterministic restart and crash matrix tests (a)-(f) for timed waits (B4).

Covers every crash/restart point from ``wait`` through resumed task completion
using real ``recover_runtime_after_restart``, real ``take_over_orphaned_work``,
real ``ScheduleStore`` / ``_coalesce_scheduled_fire_if_active``, real
``scheduled_runtime._scheduler_pass``, and real ``TurnExecutor`` prompt/outbox
handling with a controllable clock (``timed_waits.utc_now`` plus past-dated
saved timestamps where recovery/schedule storage uses wall-clock UTC).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from strands import Agent

from custom_agents import (
    paused_wait_resume,
    runtime_recovery,
    scheduled_runtime,
    task_steering_runtime,
    timed_waits as timed_waits_rules,
)
from custom_agents.orphaned_work_takeover import take_over_orphaned_work
from custom_agents.schedule_store import ScheduleStore
from custom_agents.strands_team import worker_holds_work
from custom_agents.task_human_interaction_blockers import (
    TIMED_WAIT_BLOCKER,
    answer_held,
    get_human_interaction_blocker,
    mark_answer_held,
    record_human_interaction_blocker,
)
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import resume_interrupted_task_turn
from custom_agents.timed_wait_runtime import resume_due_timed_waits, seconds_until_next_resume
from custom_agents.timed_waits import (
    TIMED_WAIT_STARTED_EVENT,
    WAIT_TOOL_NAME,
    WaitLedger,
    timed_wait_record,
)
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduleManagementClass, ScheduledMessage
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, TaskHistoryImportance, TaskHistoryType
from custom_tools.providers.timed_waits import wait
from storage.agent_launch_repository import AgentLaunchRepository, LaunchCapacity
from tests.test_runtime_owner_boundaries import build_turn_owner
from tests.test_schedule_worker_service import lead as _lead_fixture  # noqa: F401
from tests.test_schedule_worker_transactions import make_launch
from tests.test_timed_wait_runtime import (
    AGENT_ID,
    BASE_TIME,
    TASK_ID,
    TEST_TEAM,
    _ScriptedWaitModel,
    _invoke_and_pause,
    _make_runtime_agent,
    _run_real_scheduler_pass,
)

# Expose imported pytest fixture under its expected parameter name.
lead = _lead_fixture


def _new_manager(
    db_path: Path,
    *,
    agent_id: str = AGENT_ID,
    team_id: str = TEST_TEAM,
    agent_type: str = "Worker",
) -> TaskManager:
    mgr = TaskManager(db_path=str(db_path))
    mgr.set_owner_identity(agent_id, agent_type, team_id, agent_type)
    return mgr


def _restarted_agent(
    db_path: Path,
    *,
    agent_id: str = AGENT_ID,
    team_id: str = TEST_TEAM,
    agent_type: str = "Worker",
    same_role_takeover: bool = False,
    grace_seconds: float = 900.0,
) -> SimpleNamespace:
    agent = _make_runtime_agent(db_path, add_initial_task=False)
    mgr = _new_manager(db_path, agent_id=agent_id, team_id=team_id, agent_type=agent_type)
    agent.id = agent_id
    agent.name = agent_type
    agent.team_id = team_id
    agent.named_agent_type = agent_type
    agent.task_manager = mgr
    agent.same_role_runtime_takeover_enabled = same_role_takeover
    agent._recovery_retry_deadline_seconds = lambda: grace_seconds
    agent._parse_iso_timestamp = lambda v: datetime.fromisoformat(v) if v else None
    agent._finalize_conversation_turn = AsyncMock()
    agent._send_task_error_response = AsyncMock()
    agent._agent_state_store = None
    agent.deferred_inbox.restore_role = AsyncMock(return_value=False)
    return agent


async def _run_queued_task_to_completion(
    manager: TaskManager,
    *,
    task_id: str = TASK_ID,
    answer: str = "redeploy verified healthy",
    now_iso: str,
) -> tuple[Task, list[str]]:
    """Dequeue ``task_id``, run the real TurnExecutor, record its decision batch, and complete it."""
    assert manager.get_next_task_to_process() == task_id
    manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, now_iso)
    captured_prompts: list[str] = []

    async def capture_model_call(prompt: str, **_kwargs: Any) -> Any:
        captured_prompts.append(prompt)
        return turn_result

    executor, turn_result = build_turn_owner(
        invoke=capture_model_call,
        guidance=deque(["", ""]),
        committed=[],
        cleared=[],
        task_operations={
            "read_runtime_resume_directive": manager.read_runtime_resume_directive,
            "read_partial_tool_call_limit": manager.read_partial_tool_call_limit,
            "release_turn_start_notes": manager.release_turn_start_notes,
        },
    )

    async def record_decision(*, incoming_content: Task, start_notes: Any = None, **_kwargs: Any) -> Task:
        assert manager.create_turn_action_batch(incoming_content.task_id, [], release_notes=start_notes)
        return Task(task_id=incoming_content.task_id, task_type=MessageMetaType.RESPONSE, answer=answer)

    executor.effects = dataclasses.replace(executor.effects, execute_turn_decision_outbox=record_decision)
    info = manager.get_task_info(task_id)
    assert info is not None
    response = await executor.process_llm_interaction(info.task, info.header, info.meta)
    manager.complete_task(task_id, now_iso)
    archived = manager.get_task_from_db(task_id)
    assert archived is not None and archived["status"] == ProgressState.COMPLETED.value
    return response, captured_prompts


def _seed_waiting_timed_wait(
    db_path: Path,
    *,
    agent_id: str = AGENT_ID,
    team_id: str = TEST_TEAM,
    agent_type: str = "Worker",
    task_id: str = TASK_ID,
    minutes: int = 10,
    reason: str = "Portainer redeploy",
    started_at: datetime,
    meta: MessageMeta | None = None,
    owner_person_id: str | None = None,
    interrupt_id: str = "int-matrix",
    call_id: str = "call-matrix",
) -> dict[str, Any]:
    """Persist a WAITING task with one charged timed-wait history entry and blocker record."""
    mgr = _new_manager(db_path, agent_id=agent_id, team_id=team_id, agent_type=agent_type)
    assert mgr.add_task(
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=f"Run {task_id}"),
        MessageHeader(from_agent_id="scheduler", response_routing_key="gateway"),
        meta or MessageMeta(),
        owner_person_id=owner_person_id,
    )
    assert mgr.get_next_task_to_process() == task_id
    mgr.update_main_task_state(task_id, ProgressState.IN_PROGRESS, started_at.isoformat())
    resume_at = started_at + timedelta(minutes=minutes)
    record = timed_wait_record(
        interrupt_id=interrupt_id,
        call_id=call_id,
        tool=WAIT_TOOL_NAME,
        tool_use_id="tu-matrix",
        minutes=minutes,
        reason=reason,
        requested_at=started_at.isoformat(),
        resume_at=resume_at.isoformat(),
    )
    mgr.append_history_entry(
        task_id,
        record["summary"],
        entry_type=TaskHistoryType.QUERY_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=started_at.isoformat(),
        details={
            "event": TIMED_WAIT_STARTED_EVENT,
            "call_id": call_id,
            "tool": WAIT_TOOL_NAME,
            "tool_use_id": "tu-matrix",
            "interrupt_id": interrupt_id,
            "minutes": minutes,
            "reason": reason,
            "requested_at": started_at.isoformat(),
            "resume_at": resume_at.isoformat(),
        },
    )
    record_human_interaction_blocker(mgr, task_id, record)
    mgr.update_main_task_state(task_id, ProgressState.WAITING, started_at.isoformat())
    return record


# -- (a) restart before resume_at ------------------------------------------------------------


@pytest.mark.asyncio
async def test_matrix_a_restart_before_resume_at_waits_and_resumes_once_at_due_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix (a): restart before resume_at keeps task WAITING, never resumes early, resumes once at due time."""
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    db_path = tmp_path / "matrix_a.db"
    first = _make_runtime_agent(db_path)
    model = _ScriptedWaitModel([[("tu-a", "wait", {"minutes": 10, "reason": "Portainer redeploy"})]])
    strands_agent = Agent(model=model, tools=[wait], callback_handler=None)
    result, _parts = await _invoke_and_pause(first, strands_agent)
    assert result.stop_reason == "interrupt"
    saved_blocker = get_human_interaction_blocker(first.task_manager, TASK_ID)
    assert saved_blocker is not None

    # Restart 3 minutes later (7 minutes before resume_at).
    now_box[0] = BASE_TIME + timedelta(minutes=3)
    restarted = _restarted_agent(db_path)
    await runtime_recovery.recover_runtime_after_restart(restarted)

    mgr = restarted.task_manager
    assert mgr.get_task_info(TASK_ID).state == ProgressState.WAITING
    assert list(mgr.task_state.task_queue) == []
    assert get_human_interaction_blocker(mgr, TASK_ID) is not None
    assert mgr.read_runtime_resume_directive(TASK_ID) is None

    # Scheduler pass before due time does not resume early; next resume is ~420s away (sleep capped at 60s).
    assert await seconds_until_next_resume(restarted) == pytest.approx(420.0)
    sleep_before = await _run_real_scheduler_pass(restarted, monkeypatch)
    assert sleep_before == pytest.approx(60.0)
    assert mgr.get_task_info(TASK_ID).state == ProgressState.WAITING
    assert list(mgr.task_state.task_queue) == []

    # Advance clock past resume_at: real scheduler pass resumes the task once from durable note.
    now_box[0] = BASE_TIME + timedelta(minutes=10, seconds=6)
    await _run_real_scheduler_pass(restarted, monkeypatch)
    assert mgr.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert list(mgr.task_state.task_queue) == [TASK_ID]
    assert get_human_interaction_blocker(mgr, TASK_ID) is None

    expected_note = timed_waits_rules.resume_note(saved_blocker, now=now_box[0])
    assert mgr.read_runtime_resume_directive(TASK_ID) == expected_note
    assert "after 10.1 minutes" in expected_note
    assert f"the task was resumed at {timed_waits_rules.format_utc_display(now_box[0])}" in expected_note

    # Second scheduler pass does not re-queue or duplicate the note.
    await _run_real_scheduler_pass(restarted, monkeypatch)
    assert list(mgr.task_state.task_queue) == [TASK_ID]
    assert mgr.read_runtime_resume_directive(TASK_ID) == expected_note

    # Run the resumed task through TurnExecutor to completion; budget stays charged once (10 min).
    assert WaitLedger.from_history(mgr.get_task_info(TASK_ID)).total_minutes == 10
    response, prompts = await _run_queued_task_to_completion(
        mgr, answer="redeploy complete after wait", now_iso=now_box[0].isoformat()
    )
    assert response.answer == "redeploy complete after wait"
    assert len(prompts) == 1 and expected_note in prompts[0]
    assert mgr.read_runtime_resume_directive(TASK_ID) is None
    archived = mgr.get_task_from_db(TASK_ID)
    assert archived is not None and archived["status"] == ProgressState.COMPLETED.value


# -- (b) restart hours late + recurring schedule overlap ------------------------------------


@pytest.mark.asyncio
async def test_matrix_b_restart_hours_late_resumes_promptly_and_coalesces_recurring_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix (b): restart 3 hours late resumes once with real waited time, coalesces the overlapping
    recurring schedule fire, and advances next_fire_at past now without burst-replaying missed runs."""
    db_path = tmp_path / "matrix_b.db"
    # Anchor to real wall-clock UTC minus 3 hours so ScheduleStore.claim_due_schedules (which reads real UTC)
    # and timed_waits.utc_now both agree that 3 hours elapsed across the outage (C06).
    restart_now = datetime.now(timezone.utc).replace(microsecond=0)
    started_at = restart_now - timedelta(hours=3)
    now_box = [restart_now]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    schedule_id = "sched-recurring-b"
    original_next_fire = started_at + timedelta(minutes=30)
    store = ScheduleStore(db_path=str(db_path))
    store.add(
        ScheduledMessage(
            schedule_id=schedule_id,
            management_class=ScheduleManagementClass.USER,
            payload=Task(task_id="template", task_type=MessageMetaType.REQUEST, problem_statement="Check health"),
            interval_ms=30 * 60 * 1000,
            repeat_count=None,
            next_fire_at=original_next_fire.isoformat(),
            enabled=True,
        )
    )

    meta = MessageMeta(
        gateway_context=GatewayContext(
            canonical_user_id="alice",
            session_key="sess-alice",
            scheduled_job_id=schedule_id,
        )
    )
    saved_blocker = _seed_waiting_timed_wait(
        db_path,
        minutes=10,
        reason="check stack",
        started_at=started_at,
        meta=meta,
        owner_person_id="alice",
    )

    # Restart 3 hours later: recover_runtime_after_restart resumes the overdue timed wait immediately.
    restarted = _restarted_agent(db_path)
    restarted._schedule_store = store
    await runtime_recovery.recover_runtime_after_restart(restarted)

    mgr = restarted.task_manager
    assert mgr.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert list(mgr.task_state.task_queue) == [TASK_ID]
    assert get_human_interaction_blocker(mgr, TASK_ID) is None
    note = mgr.read_runtime_resume_directive(TASK_ID) or ""
    assert note == timed_waits_rules.resume_note(saved_blocker, now=restart_now)
    assert "after 180 minutes" in note
    assert timed_waits_rules.format_utc_display(restart_now) in note
    assert "current time" not in note.lower()

    # The overdue recurring schedule claims its fire while the resumed task is still active:
    # _coalesce_scheduled_fire_if_active coalesces it (no duplicate run), and update_after_fire
    # advances next_fire_at past now so the 5 missed 30-minute slots are NOT burst-replayed.
    monkeypatch.setattr(scheduled_runtime, "_notify_user_of_coalesced_scheduled_fire", AsyncMock())
    claimed = store.claim_next_due(
        claimer_agent_id=AGENT_ID,
        claimer_named_agent_type="Worker",
        claimer_team_id=TEST_TEAM,
        allow_unscoped=True,
    )
    assert claimed is not None and claimed["schedule_id"] == schedule_id
    coalesced = await scheduled_runtime._coalesce_scheduled_fire_if_active(
        restarted,
        schedule_id,
        schedule_id=schedule_id,
        schedule_name="Health check",
    )
    assert coalesced is True
    store.update_after_fire(
        schedule_id,
        claim_token=claimed["claim_token"],
        claimer_agent_id=AGENT_ID,
        due_at=original_next_fire.isoformat(),
    )
    updated_schedule = store.get_scheduled_message(schedule_id)
    assert updated_schedule is not None and updated_schedule.next_fire_at is not None
    next_due = timed_waits_rules.parse_utc(updated_schedule.next_fire_at)
    assert next_due is not None and next_due > restart_now
    assert (
        store.claim_next_due(
            claimer_agent_id=AGENT_ID,
            claimer_named_agent_type="Worker",
            claimer_team_id=TEST_TEAM,
            allow_unscoped=True,
        )
        is None
    )

    # Only the original resumed task exists and runs to completion once; budget stays 10 minutes.
    assert list(mgr.task_state.task_queue) == [TASK_ID]
    assert WaitLedger.from_history(mgr.get_task_info(TASK_ID)).total_minutes == 10
    response, prompts = await _run_queued_task_to_completion(
        mgr, answer="late restart recovered cleanly", now_iso=restart_now.isoformat()
    )
    assert response.answer == "late restart recovered cleanly"
    assert len(prompts) == 1 and note in prompts[0]
    archived = mgr.get_task_from_db(TASK_ID)
    assert archived is not None and archived["status"] == ProgressState.COMPLETED.value


# -- (c) crash during resume hand-over (3 windows) ------------------------------------------


@pytest.mark.asyncio
async def test_matrix_c_crash_during_resume_handover_all_three_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix (c) & C01: crashes (1) after live-answer clear before IN_PROGRESS, (2) during note-resume write,
    and (3) after queued ACCEPTED before turn runs all resume exactly once without double-charging."""
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    # Window (c1): live answer cleared the blocker (atomically writing the resume note) then crashed before IN_PROGRESS.
    db_c1 = tmp_path / "matrix_c1.db"
    now_c1 = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_c1[0])
    agent_c1 = _make_runtime_agent(db_c1)
    model_c1 = _ScriptedWaitModel([[("tu-c1", "wait", {"minutes": 10, "reason": "window c1"})]])
    strands_c1 = Agent(model=model_c1, tools=[wait], callback_handler=None)
    res_c1, _ = await _invoke_and_pause(agent_c1, strands_c1)
    assert res_c1.stop_reason == "interrupt"
    blocker_c1 = get_human_interaction_blocker(agent_c1.task_manager, TASK_ID)
    assert blocker_c1 is not None

    now_c1[0] = BASE_TIME + timedelta(minutes=10, seconds=5)
    assert await resume_due_timed_waits(agent_c1) == 1
    assert TASK_ID in agent_c1._pending_interrupt_responses

    # Inject crash right when resume_interrupted_task_turn tries to persist ProgressState.IN_PROGRESS.
    orig_update = agent_c1.task_manager.update_main_task_state
    orig_run = agent_c1.task_manager.operations_runtime.run

    async def crash_on_in_progress(fn: Any, *args: Any, **kwargs: Any) -> Any:
        if fn == orig_update and len(args) >= 2 and args[1] == ProgressState.IN_PROGRESS:
            raise asyncio.CancelledError("simulated kill after clear_answered_blocker before IN_PROGRESS")
        return await orig_run(fn, *args, **kwargs)

    monkeypatch.setattr(agent_c1.task_manager.operations_runtime, "run", crash_on_in_progress)
    with pytest.raises(asyncio.CancelledError):
        await resume_interrupted_task_turn(agent_c1, TASK_ID)

    # Restart: disk has WAITING, no blocker, AND the durable resume note saved atomically at clear time!
    restarted_c1 = _restarted_agent(db_c1)
    await runtime_recovery.recover_runtime_after_restart(restarted_c1)
    mgr_c1 = restarted_c1.task_manager
    assert mgr_c1.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert list(mgr_c1.task_state.task_queue) == [TASK_ID]
    note_c1 = mgr_c1.read_runtime_resume_directive(TASK_ID) or ""
    assert note_c1 == timed_waits_rules.resume_note(blocker_c1, now=now_c1[0])
    assert WaitLedger.from_history(mgr_c1.get_task_info(TASK_ID)).total_minutes == 10
    resp_c1, prompts_c1 = await _run_queued_task_to_completion(
        mgr_c1, answer="c1 done", now_iso=now_c1[0].isoformat()
    )
    assert resp_c1.answer == "c1 done" and note_c1 in prompts_c1[0]
    assert mgr_c1.read_runtime_resume_directive(TASK_ID) is None

    # Window (c2): crash during the atomic note-resume persistence write -> full rollback, restart resumes once.
    db_c2 = tmp_path / "matrix_c2.db"
    now_c2 = [BASE_TIME + timedelta(minutes=11)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_c2[0])
    blocker_c2 = _seed_waiting_timed_wait(db_c2, minutes=10, reason="window c2", started_at=BASE_TIME)
    agent_c2 = _restarted_agent(db_c2)
    agent_c2.task_manager.restore_runtime_state()
    monkeypatch.setattr(
        agent_c2.task_manager,
        "_persist_runtime_state",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("power loss mid-write")),
    )
    assert await resume_due_timed_waits(agent_c2) == 0
    assert get_human_interaction_blocker(agent_c2.task_manager, TASK_ID) is not None
    assert agent_c2.task_manager.read_runtime_resume_directive(TASK_ID) is None

    restarted_c2 = _restarted_agent(db_c2)
    await runtime_recovery.recover_runtime_after_restart(restarted_c2)
    mgr_c2 = restarted_c2.task_manager
    assert mgr_c2.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert list(mgr_c2.task_state.task_queue) == [TASK_ID]
    note_c2 = mgr_c2.read_runtime_resume_directive(TASK_ID) or ""
    assert note_c2 == timed_waits_rules.resume_note(blocker_c2, now=now_c2[0])
    assert WaitLedger.from_history(mgr_c2.get_task_info(TASK_ID)).total_minutes == 10
    resp_c2, prompts_c2 = await _run_queued_task_to_completion(
        mgr_c2, answer="c2 done", now_iso=now_c2[0].isoformat()
    )
    assert resp_c2.answer == "c2 done" and note_c2 in prompts_c2[0]

    # Window (c3): crash AFTER task was queued as ACCEPTED with durable note, BEFORE the resumed turn ran.
    db_c3 = tmp_path / "matrix_c3.db"
    now_c3 = [BASE_TIME + timedelta(minutes=11)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_c3[0])
    blocker_c3 = _seed_waiting_timed_wait(db_c3, minutes=10, reason="window c3", started_at=BASE_TIME)
    agent_c3 = _restarted_agent(db_c3)
    await runtime_recovery.recover_runtime_after_restart(agent_c3)
    assert agent_c3.task_manager.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert list(agent_c3.task_manager.task_state.task_queue) == [TASK_ID]
    # Process dies before draining the queue; a second restart restores ACCEPTED + single note.
    now_c3[0] = BASE_TIME + timedelta(minutes=12)
    restarted_c3 = _restarted_agent(db_c3)
    await runtime_recovery.recover_runtime_after_restart(restarted_c3)
    mgr_c3 = restarted_c3.task_manager
    assert mgr_c3.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert list(mgr_c3.task_state.task_queue) == [TASK_ID]
    note_c3 = mgr_c3.read_runtime_resume_directive(TASK_ID) or ""
    assert note_c3 == timed_waits_rules.resume_note(blocker_c3, now=BASE_TIME + timedelta(minutes=11))
    assert note_c3.count("window c3") == 1
    assert WaitLedger.from_history(mgr_c3.get_task_info(TASK_ID)).total_minutes == 10
    resp_c3, prompts_c3 = await _run_queued_task_to_completion(
        mgr_c3, answer="c3 done", now_iso=now_c3[0].isoformat()
    )
    assert resp_c3.answer == "c3 done" and note_c3 in prompts_c3[0]


# -- (d) crash after resumed turn started running -------------------------------------------


@pytest.mark.asyncio
async def test_matrix_d_crash_during_resumed_turn_within_and_past_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix (d) & C01: crash while resumed turn is IN_PROGRESS recovers with its resume note within grace
    without double-charging, and fails cleanly past restart_executing_task_grace_seconds."""
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    async def _crash_mid_resumed_turn(db_path: Path, now_box: list[datetime]) -> dict[str, Any]:
        agent = _make_runtime_agent(db_path)
        model = _ScriptedWaitModel([[("tu-d", "wait", {"minutes": 10, "reason": "window d"})]])
        strands_agent = Agent(model=model, tools=[wait], callback_handler=None)
        res, _ = await _invoke_and_pause(agent, strands_agent)
        assert res.stop_reason == "interrupt"
        blocker = get_human_interaction_blocker(agent.task_manager, TASK_ID)
        assert blocker is not None

        now_box[0] = now_box[0] + timedelta(minutes=10, seconds=5)
        assert await resume_due_timed_waits(agent) == 1

        # Crash inside _run_resumed_turn AFTER IN_PROGRESS was saved and BEFORE decision batch recorded.
        async def crash_mid_turn(*_args: Any, **_kwargs: Any) -> Any:
            raise asyncio.CancelledError("simulated power loss mid resumed model turn")

        monkeypatch.setattr(task_steering_runtime, "_run_resumed_turn", crash_mid_turn)
        with pytest.raises(asyncio.CancelledError):
            await resume_interrupted_task_turn(agent, TASK_ID)
        assert agent.task_manager.get_task_info(TASK_ID).state == ProgressState.IN_PROGRESS
        return blocker

    # Subcase (d1): restart within grace period -> re-queued with durable resume note, completes once, budget 10m.
    db_d1 = tmp_path / "matrix_d1.db"
    now_d1 = [datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10, seconds=10)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_d1[0])
    blocker_d1 = await _crash_mid_resumed_turn(db_d1, now_d1)
    expected_note = timed_waits_rules.resume_note(blocker_d1, now=now_d1[0])

    restarted_d1 = _restarted_agent(db_d1, grace_seconds=900.0)
    await runtime_recovery.recover_runtime_after_restart(restarted_d1)
    mgr_d1 = restarted_d1.task_manager
    assert mgr_d1.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert list(mgr_d1.task_state.task_queue) == [TASK_ID]
    assert mgr_d1.read_runtime_resume_directive(TASK_ID) == expected_note
    assert WaitLedger.from_history(mgr_d1.get_task_info(TASK_ID)).total_minutes == 10
    resp_d1, prompts_d1 = await _run_queued_task_to_completion(
        mgr_d1, answer="d1 recovered", now_iso=now_d1[0].isoformat()
    )
    assert resp_d1.answer == "d1 recovered" and expected_note in prompts_d1[0]
    restarted_d1._send_task_error_response.assert_not_awaited()

    # Subcase (d2): outage longer than grace period -> stale IN_PROGRESS fails cleanly instead of re-running.
    db_d2 = tmp_path / "matrix_d2.db"
    now_d2 = [datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_d2[0])
    await _crash_mid_resumed_turn(db_d2, now_d2)

    # Backdate the persisted snapshot's updated_at by 2 hours to simulate a long outage past 60s grace.
    import sqlite3

    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(db_d2) as conn:
        conn.execute("UPDATE active_task_runtime SET updated_at = ?", (stale_ts,))
        conn.commit()

    restarted_d2 = _restarted_agent(db_d2, grace_seconds=60.0)
    await runtime_recovery.recover_runtime_after_restart(restarted_d2)
    archived = restarted_d2.task_manager.get_task_from_db(TASK_ID)
    assert archived is not None and archived["status"] == ProgressState.FAILED.value
    assert list(restarted_d2.task_manager.task_state.task_queue) == []
    restarted_d2._send_task_error_response.assert_awaited_once()
    assert "stale" in restarted_d2._send_task_error_response.await_args.args[3].lower()


# -- (e) dead pool worker replaced by lead takeover + new instance resumes ------------------


@pytest.mark.asyncio
async def test_matrix_e_dead_pool_worker_replaced_by_takeover_and_resumed_at_due_time(
    lead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix (e): when a pool worker holding a WAITING timed wait dies with no worker of its type running,
    TeamLead's take_over_orphaned_work starts a replacement, which adopts the task, stays alive across
    worker_holds_work, and resumes it once at resume_at."""
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    db_path = Path(lead.task_manager.storage_settings.sqlite.path)
    dead_owner = "agent_pool_dead"
    replacement_id = "agent_pool_replacement"
    pool_owner = "worker_pool:team"

    # Record a FAILED pool worker launch for dead_owner so owner_liveness proves it is gone.
    launches = AgentLaunchRepository(lead.task_manager.storage_settings)
    reserved, _ = launches.reserve(
        make_launch(
            agent_id=dead_owner,
            team_id="team",
            named_agent_type="Worker",
            owner_canonical_user_id=pool_owner,
            source="delegation",
        ),
        LaunchCapacity.defaults(),
        join=False,
    )
    starting = launches.update(reserved, state=AgentLaunchState.STARTING)
    launches.update(starting, state=AgentLaunchState.FAILED)

    # Persist dead_owner's WAITING timed-wait task in the shared SQLite runtime store.
    saved_blocker = _seed_waiting_timed_wait(
        db_path,
        agent_id=dead_owner,
        team_id="team",
        agent_type="Worker",
        minutes=10,
        reason="wait for build",
        started_at=BASE_TIME,
        owner_person_id="owner",
    )

    # TeamLead runs its real orphaned-work takeover pass: starts one replacement for the dead pool owner.
    lead.is_team_lead = True
    lead.schedule_maintenance = {}
    started_count = await take_over_orphaned_work(lead, now=BASE_TIME + timedelta(minutes=1), clock=1.0)
    assert started_count == 1
    assert len(lead.starts) == 1
    assert lead.starts[0].named_agent_type == "Worker"
    assert lead.starts[0].owner_canonical_user_id == pool_owner

    # Replacement worker starts with a brand new agent_id and runs real recover_runtime_after_restart.
    now_box[0] = BASE_TIME + timedelta(minutes=2)
    replacement = _restarted_agent(
        db_path,
        agent_id=replacement_id,
        team_id="team",
        agent_type="Worker",
        same_role_takeover=True,
    )
    await runtime_recovery.recover_runtime_after_restart(replacement)

    # Adopted task stays WAITING before due time, dead owner's row is gone, and worker_holds_work keeps it alive.
    mgr = replacement.task_manager
    assert mgr.get_task_info(TASK_ID) is not None
    assert mgr.get_task_info(TASK_ID).state == ProgressState.WAITING
    assert list(mgr.task_state.task_queue) == []
    assert await worker_holds_work(replacement) is True
    remaining_rows = [
        row.owner_agent_id
        for row in mgr.runtime_state_store.peek_task_runtime_by_role("team", "Worker")
    ]
    assert dead_owner not in remaining_rows and replacement_id in remaining_rows

    # Scheduler pass before resume_at does NOT resume early (next resume 480s away, sleep capped at 60s).
    assert await seconds_until_next_resume(replacement) == pytest.approx(480.0)
    sleep_before = await _run_real_scheduler_pass(replacement, monkeypatch)
    assert sleep_before == pytest.approx(60.0)
    assert mgr.get_task_info(TASK_ID).state == ProgressState.WAITING

    # At resume_at, replacement's scheduler pass resumes the adopted task once and finishes it.
    now_box[0] = BASE_TIME + timedelta(minutes=10, seconds=3)
    await _run_real_scheduler_pass(replacement, monkeypatch)
    assert mgr.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert list(mgr.task_state.task_queue) == [TASK_ID]
    expected_note = timed_waits_rules.resume_note(saved_blocker, now=now_box[0])
    assert mgr.read_runtime_resume_directive(TASK_ID) == expected_note
    assert WaitLedger.from_history(mgr.get_task_info(TASK_ID)).total_minutes == 10

    response, prompts = await _run_queued_task_to_completion(
        mgr, answer="adopted pool task finished", now_iso=now_box[0].isoformat()
    )
    assert response.answer == "adopted pool task finished"
    assert len(prompts) == 1 and expected_note in prompts[0]
    assert await worker_holds_work(replacement) is False


# -- (f) answer_held marks cleared across same-id and new-id restarts -----------------------


@pytest.mark.parametrize(
    ("same_id", "due_on_restart"),
    [
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ],
    ids=["same_id_due", "same_id_not_due", "new_id_due", "new_id_not_due"],
)
@pytest.mark.asyncio
async def test_matrix_f_answer_held_marks_cleared_on_restart_and_task_resumes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    same_id: bool,
    due_on_restart: bool,
) -> None:
    """Matrix (f): answer_held marks left on timed-wait blockers are cleared on startup for both same-id
    restarts and new-id takeovers, whether due on restart or still in the future."""
    db_path = tmp_path / "matrix_f.db"
    old_id = "worker-old"
    new_id = old_id if same_id else "worker-new"
    saved_blocker = _seed_waiting_timed_wait(
        db_path,
        agent_id=old_id,
        minutes=10,
        reason="held before crash",
        started_at=BASE_TIME,
        interrupt_id="int-held",
    )
    old_mgr = _new_manager(db_path, agent_id=old_id)
    old_mgr.restore_runtime_state()
    assert mark_answer_held(
        old_mgr,
        TASK_ID,
        TIMED_WAIT_BLOCKER,
        "int-held",
        holder_id=old_id,
        at=(BASE_TIME + timedelta(minutes=10)).isoformat(),
    )
    assert answer_held(get_human_interaction_blocker(old_mgr, TASK_ID), old_id)

    if not same_id:
        # Mark old_id as gone via its launch record so startup takeover adopts its row.
        launches = AgentLaunchRepository(old_mgr.storage_settings)
        reserved, _ = launches.reserve(
            make_launch(agent_id=old_id, team_id=TEST_TEAM, named_agent_type="Worker"),
            LaunchCapacity.defaults(),
            join=False,
        )
        starting = launches.update(reserved, state=AgentLaunchState.STARTING)
        launches.update(starting, state=AgentLaunchState.FAILED)

    restart_time = BASE_TIME + (timedelta(minutes=15) if due_on_restart else timedelta(minutes=4))
    now_box = [restart_time]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    restarted = _restarted_agent(
        db_path,
        agent_id=new_id,
        same_role_takeover=not same_id,
    )
    with caplog.at_level(logging.INFO):
        await runtime_recovery.recover_runtime_after_restart(restarted)

    assert "event=wait_answer_marks_cleared" in caplog.text
    mgr = restarted.task_manager

    if due_on_restart:
        assert mgr.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
        assert list(mgr.task_state.task_queue) == [TASK_ID]
        assert get_human_interaction_blocker(mgr, TASK_ID) is None
        resumed_at = restart_time
    else:
        assert mgr.get_task_info(TASK_ID).state == ProgressState.WAITING
        assert list(mgr.task_state.task_queue) == []
        blocker_after = get_human_interaction_blocker(mgr, TASK_ID)
        assert blocker_after is not None and not answer_held(blocker_after)
        # Advance past resume_at and let the scheduler pass resume it once.
        resumed_at = BASE_TIME + timedelta(minutes=10, seconds=5)
        now_box[0] = resumed_at
        await _run_real_scheduler_pass(restarted, monkeypatch)
        assert mgr.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
        assert list(mgr.task_state.task_queue) == [TASK_ID]
        assert get_human_interaction_blocker(mgr, TASK_ID) is None

    expected_note = timed_waits_rules.resume_note(saved_blocker, now=resumed_at)
    assert mgr.read_runtime_resume_directive(TASK_ID) == expected_note
    assert WaitLedger.from_history(mgr.get_task_info(TASK_ID)).total_minutes == 10
    response, prompts = await _run_queued_task_to_completion(
        mgr, answer="matrix f complete", now_iso=resumed_at.isoformat()
    )
    assert response.answer == "matrix f complete"
    assert len(prompts) == 1 and expected_note in prompts[0]
