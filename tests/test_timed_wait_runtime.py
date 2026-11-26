"""Runtime tests for timed waits: A2 live pause/resume, A3 restart recovery, C01/C15 multi-wait & fallbacks."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from strands import Agent, tool
from strands.models import Model
from strands.types.tools import ToolContext

from custom_agents import (
    paused_wait_resume,
    runtime_recovery,
    scheduled_runtime,
    task_steering_runtime,
    timed_wait_runtime,
    timed_waits as timed_waits_rules,
    tool_setup_run_runtime,
)
from custom_agents.pool_intake import may_take_pool_task
from custom_agents.schedule_store import ScheduleStore
from custom_agents.strands_parts import StrandsParts
from custom_agents.task_human_interaction_blockers import (
    ACCESS_REQUEST_BLOCKER,
    SETUP_RUN_BLOCKER,
    TIMED_WAIT_BLOCKER,
    answer_held,
    get_human_interaction_blocker,
    mark_answer_held,
    record_human_interaction_blocker,
)
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import (
    _build_interrupt_response_blocks,
    resume_interrupted_task_turn,
    send_interrupt_request_for_result,
)
from custom_agents.timed_wait_runtime import (
    finish_timed_wait,
    resume_due_timed_waits,
    resume_without_paused_turn,
    seconds_until_next_resume,
    stage_timed_wait,
)
from custom_agents.timed_waits import (
    TIMED_WAIT_ENDED_EVENT,
    TIMED_WAIT_KIND,
    TIMED_WAIT_RESPONSE_KIND,
    TIMED_WAIT_STARTED_EVENT,
    WAIT_TOOL_NAME,
    WaitLedger,
    timed_wait_record,
)
from custom_agents.tool_setup_runs import (
    TOOL_SETUP_RUN_KIND,
    TOOL_SETUP_RUN_RESPONSE_KIND,
    WAITING,
    RunPieceV1,
    ToolSetupRunV1,
)
from custom_messages.contracts.access_v2 import TaskContextV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import (
    ACCESS_REQUEST_ID_KEY,
    ACCESS_REQUEST_INTERRUPT_KIND,
    Task,
    TaskSteering,
)
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    ScheduleManagementClass,
    TaskSteeringStatus,
    TaskSteeringType,
)
from custom_tools.providers.timed_waits import wait
from custom_tools.runtime_context import set_agent_ref, task_work
from custom_tools.tool_stop import ToolStop
from runtime_settings.timed_waits import TimedWaitLimits
from tests.broker_access_support import (
    TEST_TEAM,
    forget_task_context_keys,
    use_scheduler_key,
)

BASE_TIME = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
AGENT_ID = "agent-worker-tw"
TASK_ID = "task-timed-wait-1"


class _ScriptedWaitModel(Model):
    """Drives a real Strands Agent through scripted tool-call steps, then echoes tool results in text."""

    def __init__(self, steps: list[list[tuple[str, str, dict[str, Any]]]]) -> None:
        self.steps = steps
        self.step = 0
        self.observed_tool_results: list[dict[str, Any]] = []

    def update_config(self, **config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted-timed-wait"}

    async def structured_output(self, *args: Any, **kwargs: Any):
        raise AssertionError("No structured_output call expected")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.step += 1
        # Capture any toolResult blocks visible to the model on this call.
        for msg in messages or ():
            if msg.get("role") != "user":
                continue
            for block in msg.get("content") or ():
                result = block.get("toolResult")
                if isinstance(result, dict):
                    for part in result.get("content") or ():
                        text = part.get("text")
                        if isinstance(text, str):
                            try:
                                parsed = json.loads(text)
                                if isinstance(parsed, dict) and parsed not in self.observed_tool_results:
                                    self.observed_tool_results.append(parsed)
                            except json.JSONDecodeError:
                                pass
        yield {"messageStart": {"role": "assistant"}}
        if self.step <= len(self.steps):
            for call_id, name, tool_input in self.steps[self.step - 1]:
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": call_id, "name": name}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        summary = json.dumps(self.observed_tool_results, sort_keys=True)
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": f"finished:{summary}"}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


class _FakeWake:
    def __init__(self) -> None:
        self.count = 0

    def set(self) -> None:
        self.count += 1


class _EmptyInbox:
    capacity = 16

    def __len__(self) -> int:
        return 0

    async def stored_team_id(self) -> str | None:
        return None

    async def clear(self) -> None:
        return None

    async def restore(self) -> bool:
        return False


def _make_runtime_agent(
    db_path: Path,
    *,
    task_id: str = TASK_ID,
    meta: MessageMeta | None = None,
    schedule_store: Any = None,
    add_initial_task: bool = True,
) -> SimpleNamespace:
    manager = TaskManager(db_path=str(db_path))
    manager.set_owner_identity(AGENT_ID, "Worker", TEST_TEAM, "Worker")
    if add_initial_task:
        assert manager.add_task(
            Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="redeploy check"),
            MessageHeader(from_agent_id="scheduler"),
            meta or MessageMeta(),
            owner_person_id=None,
        )
        assert manager.get_next_task_to_process() == task_id
        manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, BASE_TIME.isoformat())

    exit_event = asyncio.Event()
    wake = _FakeWake()
    fake_store = schedule_store or SimpleNamespace(get_earliest_fire_time=lambda **_k: None)
    agent = SimpleNamespace(
        id=AGENT_ID,
        name="Worker",
        team_id=TEST_TEAM,
        named_agent_type="Worker",
        is_team_lead=False,
        task_manager=manager,
        timed_wait_limits=TimedWaitLimits(),
        authorization_config={"enabled": True},
        tools=[wait],
        _interrupted_turn_sessions={},
        _pending_interrupt_responses={},
        _processed_task_steering_ids=set(),
        _pending_proactive_task_steering={},
        _scheduler_wake=wake,
        _schedule_store=fake_store,
        _worker_ready=True,
        _pool_draining=False,
        _provider_cooldown_until=None,
        paused=False,
        is_executing=False,
        same_role_runtime_takeover_enabled=False,
        deferred_inbox=_EmptyInbox(),
        get_exit_event=lambda: exit_event,
        subtask_watchdog_interval_seconds=60.0,
        _agent_id_by_name={},
        named_agent_type_queue_map={},
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _set_active_execution_context=lambda tid: ToolStop(agent_id=AGENT_ID, task_id=tid),
        _clear_active_execution_context=lambda: None,
        _refresh_self_agent_state=AsyncMock(),
        _recover_stalled_subtasks=AsyncMock(),
        _logger=logging.getLogger("test.timed_wait_runtime"),
    )
    return agent


async def _run_real_scheduler_pass(agent: Any, monkeypatch: pytest.MonkeyPatch) -> float | None:
    """Invoke scheduled_runtime._scheduler_pass with unrelated housekeeping stubbed out."""

    monkeypatch.setattr(scheduled_runtime, "observe_liveness", lambda _a: None)
    monkeypatch.setattr(scheduled_runtime, "check_queue_pressure", AsyncMock())
    monkeypatch.setattr(scheduled_runtime, "resume_decided_access_waits", AsyncMock(return_value=0))
    monkeypatch.setattr(scheduled_runtime, "resume_ended_setup_runs", AsyncMock(return_value=0))
    monkeypatch.setattr(scheduled_runtime, "process_pending_handbacks", AsyncMock())
    monkeypatch.setattr(scheduled_runtime, "take_over_orphaned_work", AsyncMock())
    monkeypatch.setattr(scheduled_runtime, "prune_departed_agent_rows_when_due", AsyncMock())
    monkeypatch.setattr(scheduled_runtime, "report_stuck_tasks_if_due", AsyncMock(), raising=False)
    monkeypatch.setattr(scheduled_runtime, "_run_scheduler_claim_cycle", AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(
        "custom_agents.agent_instantiation_service.renew_runtime_launch",
        lambda _a: None,
    )
    # Run the real _scheduler_pass until it finishes the wait checks and computes sleep_seconds.
    # Avoid invoking schedule claiming / stuck reports if unrelated fields are absent on our stub.
    monkeypatch.setattr(scheduled_runtime, "_prune_expired_schedules_if_due", AsyncMock(), raising=False)
    monkeypatch.setattr(scheduled_runtime, "_schedule_claim_scope", AsyncMock(return_value=(False, False)), raising=False)
    monkeypatch.setattr(scheduled_runtime, "_report_stuck_work_if_due", AsyncMock(), raising=False)
    monkeypatch.setattr(scheduled_runtime, "_emit_scheduled_telemetry_if_due", AsyncMock(), raising=False)
    monkeypatch.setattr(scheduled_runtime, "check_stuck_work", AsyncMock(), raising=False)
    # Ensure agent has _scheduler_last_prune_at / _worker_launch None so _scheduler_pass runs cleanly.
    if not hasattr(agent, "_worker_launch"):
        agent._worker_launch = None
    if not hasattr(agent, "is_heartbeat_enabled"):
        agent.is_heartbeat_enabled = False
    return await scheduled_runtime._scheduler_pass(agent, {}, AsyncMock())


async def _invoke_and_pause(
    agent: Any,
    strands_agent: Agent,
    *,
    task_id: str = TASK_ID,
    prompt: Any = "Wait 10 minutes for redeploy, then report time.",
    parts: StrandsParts | None = None,
) -> tuple[Any, StrandsParts]:
    """Run the real Strands Agent and pass any interrupt to send_interrupt_request_for_result."""

    use_parts = parts or StrandsParts(agent=strands_agent, loaded=SimpleNamespace(), sync=None)  # type: ignore[arg-type]
    task_info = agent.task_manager.get_task_info(task_id)
    set_agent_ref(agent)
    try:
        with task_work(task_id) as work:
            work.strands = use_parts
            result = await strands_agent.invoke_async(prompt)
            if getattr(result, "stop_reason", None) == "interrupt":
                await send_interrupt_request_for_result(
                    agent,
                    result=result,
                    task=task_info.task,
                    msg_header=task_info.header,
                    msg_meta=task_info.meta,
                    invoke_kwargs={},
                    callback_handler=lambda **_k: None,
                    full_prompt=str(prompt),
                )
    finally:
        set_agent_ref(None)
    return result, use_parts


async def _resume_live_turn(agent: Any, *, task_id: str = TASK_ID) -> Any:
    """Execute the queued interrupt response against the real paused Strands Agent."""

    session = agent._interrupted_turn_sessions[task_id]
    steering = agent._pending_interrupt_responses[task_id]
    blocks = _build_interrupt_response_blocks(session, steering)
    assert blocks

    captured_result: list[Any] = []

    async def run_real_strands(_agent, sess, _steering, task_info, response_blocks, _tool_stop):
        set_agent_ref(agent)
        try:
            res = await sess.strands.agent.invoke_async(response_blocks)
            captured_result.append(res)
            if getattr(res, "stop_reason", None) == "interrupt":
                await send_interrupt_request_for_result(
                    agent,
                    result=res,
                    task=task_info.task,
                    msg_header=task_info.header,
                    msg_meta=task_info.meta,
                    invoke_kwargs={},
                    callback_handler=lambda **_k: None,
                    full_prompt=sess.full_prompt,
                )
            else:
                agent._interrupted_turn_sessions.pop(task_id, None)
            return res
        finally:
            set_agent_ref(None)

    orig = task_steering_runtime._run_resumed_turn
    task_steering_runtime._run_resumed_turn = run_real_strands  # type: ignore[assignment]
    try:
        await resume_interrupted_task_turn(agent, task_id)
    finally:
        task_steering_runtime._run_resumed_turn = orig  # type: ignore[assignment]
    return captured_result[0] if captured_result else None


# -- A2: real Strands agent + real wait tool + real scheduler pass ---------------------------


@pytest.mark.asyncio
async def test_a2_wait_pauses_frees_slot_and_resumes_through_scheduler_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    woken: list[Any] = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)

    agent = _make_runtime_agent(tmp_path / "manager.db")
    model = _ScriptedWaitModel([[("tu-wait-1", "wait", {"minutes": 10, "reason": "Portainer redeploy"})]])
    strands_agent = Agent(model=model, tools=[wait], callback_handler=None)

    # 1. Model calls real wait(10, ...): pauses locally, frees worker slot, wakes scheduler, sends no upstream NATS msg.
    result, _parts = await _invoke_and_pause(agent, strands_agent)
    assert result.stop_reason == "interrupt"
    assert agent.team_comms.send_message.await_count == 0
    assert agent._scheduler_wake.count == 1

    mgr = agent.task_manager
    task_info = mgr.get_task_info(TASK_ID)
    assert task_info is not None
    assert task_info.state == ProgressState.WAITING
    assert mgr.task_state.current_in_progress_task_id is None
    assert may_take_pool_task(agent) is True

    blocker = get_human_interaction_blocker(mgr, TASK_ID)
    assert blocker is not None
    assert blocker["kind"] == TIMED_WAIT_KIND
    assert blocker["minutes"] == 10
    assert blocker["reason"] == "Portainer redeploy"
    assert blocker["resume_at"] == (BASE_TIME + timedelta(minutes=10)).isoformat()

    # Idle sleep caps to the remaining wait time when closer than watchdog/max sleep.
    now_box[0] = BASE_TIME + timedelta(minutes=9, seconds=55)
    sleep_s = await scheduled_runtime._idle_sleep_seconds(agent, can_claim_scheduled_work=False)
    assert 4.0 <= sleep_s <= 6.0

    # 2. Scheduler pass before due does nothing.
    now_box[0] = BASE_TIME + timedelta(minutes=9, seconds=59)
    assert await resume_due_timed_waits(agent) == 0
    assert TASK_ID not in agent._pending_interrupt_responses
    assert not answer_held(get_human_interaction_blocker(mgr, TASK_ID))

    # 3. Scheduler pass after due hands the answer and marks the record held.
    now_box[0] = BASE_TIME + timedelta(minutes=10, seconds=6)
    await _run_real_scheduler_pass(agent, monkeypatch)
    assert TASK_ID in agent._pending_interrupt_responses
    assert woken == [agent]
    held_blocker = get_human_interaction_blocker(mgr, TASK_ID)
    assert answer_held(held_blocker, AGENT_ID)
    # Held live answer does not force _idle_sleep_seconds into a tight loop while lane is busy.
    assert await seconds_until_next_resume(agent) is None

    # 4. Resuming the turn clears the blocker and re-runs the real wait tool, returning waited_minutes & now_utc.
    final = await _resume_live_turn(agent)
    assert final is not None
    assert final.stop_reason == "end_turn"
    assert get_human_interaction_blocker(mgr, TASK_ID) is None
    assert len(model.observed_tool_results) == 1
    tool_res = model.observed_tool_results[0]
    assert tool_res["status"] == "waited"
    assert tool_res["minutes"] == 10
    assert tool_res["waited_minutes"] == 10.1
    assert tool_res["now_utc"] == now_box[0].isoformat()

    # Both started and ended entries are in task history.
    events = [entry.details.get("event") for entry in mgr.get_task_info(TASK_ID).task.execution_history if entry.details]
    assert TIMED_WAIT_STARTED_EVENT in events
    assert TIMED_WAIT_ENDED_EVENT in events


# -- A3: simulated restart (no live session) -------------------------------------------------


@pytest.mark.asyncio
async def test_a3_restart_keeps_future_wait_and_resumes_due_wait_from_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    woken: list[Any] = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)

    db_path = tmp_path / "manager.db"
    # Before restart: one future wait (with a stale held mark) and one wait that will already be due on boot.
    before = _make_runtime_agent(db_path, task_id="task-future")
    before.task_manager.add_task(
        Task(task_id="task-already-due", task_type=MessageMetaType.REQUEST, problem_statement="due during downtime"),
        MessageHeader(from_agent_id="scheduler"),
        MessageMeta(),
        owner_person_id=None,
    )
    before.task_manager.update_main_task_state("task-future", ProgressState.WAITING, BASE_TIME.isoformat())
    before.task_manager.update_main_task_state("task-already-due", ProgressState.WAITING, BASE_TIME.isoformat())

    future_rec = timed_wait_record(
        interrupt_id="int-future",
        call_id="call-future",
        tool=WAIT_TOOL_NAME,
        tool_use_id="tu-future",
        minutes=10,
        reason="Portainer redeploy",
        requested_at=BASE_TIME.isoformat(),
        resume_at=(BASE_TIME + timedelta(minutes=10)).isoformat(),
    )
    due_rec = timed_wait_record(
        interrupt_id="int-due",
        call_id="call-due",
        tool=WAIT_TOOL_NAME,
        tool_use_id="tu-due",
        minutes=2,
        reason="short cooldown",
        requested_at=BASE_TIME.isoformat(),
        resume_at=(BASE_TIME + timedelta(minutes=2)).isoformat(),
    )
    record_human_interaction_blocker(before.task_manager, "task-future", future_rec)
    mark_answer_held(
        before.task_manager, "task-future", TIMED_WAIT_BLOCKER, "int-future", holder_id="old-agent", at=BASE_TIME.isoformat()
    )
    record_human_interaction_blocker(before.task_manager, "task-already-due", due_rec)

    # Simulate restart at +5m: brand-new TaskManager on same SQLite, empty _interrupted_turn_sessions.
    now_box[0] = BASE_TIME + timedelta(minutes=5)
    restarted_mgr = TaskManager(db_path=str(db_path))
    restarted_mgr.set_owner_identity(AGENT_ID, "Worker", TEST_TEAM, "Worker")
    restarted_mgr.restore_runtime_state()
    after = _make_runtime_agent(db_path, add_initial_task=False)
    after.task_manager = restarted_mgr

    with caplog.at_level(logging.INFO):
        await runtime_recovery.recover_runtime_after_restart(after)

    assert "event=wait_answer_marks_cleared" in caplog.text
    assert "event=tool_setup_answer_marks_cleared" not in caplog.text

    # task-already-due was resumed from a note during startup recovery (wake=False).
    assert restarted_mgr.get_task_info("task-already-due").state == ProgressState.ACCEPTED
    assert "task-already-due" in restarted_mgr.task_state.task_queue
    assert get_human_interaction_blocker(restarted_mgr, "task-already-due") is None
    due_note = restarted_mgr.read_runtime_resume_directive("task-already-due") or ""
    assert "2-minute wait (short cooldown) ended" in due_note

    # task-future stays WAITING (stale mark cleared, NOT re-queued early).
    assert restarted_mgr.get_task_info("task-future").state == ProgressState.WAITING
    assert "task-future" not in restarted_mgr.task_state.task_queue
    future_blocker = get_human_interaction_blocker(restarted_mgr, "task-future")
    assert future_blocker is not None and not answer_held(future_blocker)

    # Scheduler pass before +10m still leaves task-future WAITING.
    now_box[0] = BASE_TIME + timedelta(minutes=9)
    assert await resume_due_timed_waits(after) == 0
    assert restarted_mgr.get_task_info("task-future").state == ProgressState.WAITING

    # Clock passes +10m: scheduler pass resumes task-future from a note at front of queue.
    now_box[0] = BASE_TIME + timedelta(minutes=10, seconds=5)
    assert await resume_due_timed_waits(after) == 1
    assert restarted_mgr.get_task_info("task-future").state == ProgressState.ACCEPTED
    assert restarted_mgr.task_state.task_queue[0] == "task-future"
    assert get_human_interaction_blocker(restarted_mgr, "task-future") is None
    assert woken == [after]
    future_note = restarted_mgr.read_runtime_resume_directive("task-future") or ""
    assert "10-minute wait (Portainer redeploy) ended" in future_note
    ended_entries = [
        entry.details
        for entry in restarted_mgr.get_task_info("task-future").task.execution_history
        if entry.details and entry.details.get("event") == TIMED_WAIT_ENDED_EVENT
    ]
    assert len(ended_entries) == 1
    assert ended_entries[0]["how"] == "note"


# -- C01 & C15: same-step multi-interrupts with advancing clock ------------------------------


@pytest.mark.asyncio
async def test_c01_c15_two_waits_in_one_step_run_sequentially_from_actual_start_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    agent = _make_runtime_agent(tmp_path / "manager.db")
    model = _ScriptedWaitModel(
        [
            [
                ("tu-w1", "wait", {"minutes": 5, "reason": "first step"}),
                ("tu-w2", "wait", {"minutes": 10, "reason": "second step"}),
            ]
        ]
    )
    strands_agent = Agent(model=model, tools=[wait], callback_handler=None)

    with caplog.at_level(logging.INFO):
        res1, _parts = await _invoke_and_pause(agent, strands_agent)
    assert res1.stop_reason == "interrupt"
    assert "event=timed_wait_deferred" in caplog.text

    # Only first wait started (12:00 -> 12:05, 5m charged); second is deferred with 0 charge.
    mgr = agent.task_manager
    b1 = get_human_interaction_blocker(mgr, TASK_ID)
    assert b1 is not None and b1["minutes"] == 5
    assert b1["resume_at"] == (BASE_TIME + timedelta(minutes=5)).isoformat()
    assert WaitLedger.from_history(mgr.get_task_info(TASK_ID)).total_minutes == 5

    # Advance to 12:05 -> first wait ends; resuming re-runs second wait, which NOW starts 12:05 -> 12:15 (C15).
    now_box[0] = BASE_TIME + timedelta(minutes=5)
    assert await resume_due_timed_waits(agent) == 1
    res2 = await _resume_live_turn(agent)
    assert res2.stop_reason == "interrupt"

    b2 = get_human_interaction_blocker(mgr, TASK_ID)
    assert b2 is not None and b2["minutes"] == 10
    assert b2["requested_at"] == (BASE_TIME + timedelta(minutes=5)).isoformat()
    assert b2["resume_at"] == (BASE_TIME + timedelta(minutes=15)).isoformat()
    assert WaitLedger.from_history(mgr.get_task_info(TASK_ID)).total_minutes == 15

    # At 12:10 second wait is NOT due (would have been due if stale 12:00..12:10 were used).
    now_box[0] = BASE_TIME + timedelta(minutes=10)
    assert await resume_due_timed_waits(agent) == 0

    # At 12:15 second wait ends and reports waited_minutes=10.0.
    now_box[0] = BASE_TIME + timedelta(minutes=15)
    assert await resume_due_timed_waits(agent) == 1
    res3 = await _resume_live_turn(agent)
    assert res3.stop_reason == "end_turn"
    assert get_human_interaction_blocker(mgr, TASK_ID) is None
    assert any(r["minutes"] == 10 and r["waited_minutes"] == 10.0 for r in model.observed_tool_results)


@pytest.mark.asyncio
async def test_c01_c15_wait_deferred_behind_access_request_starts_at_approval_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    @tool(context=True)
    def ask_access(tool_context: ToolContext) -> str:
        """Stub access request tool raising a local access_request interrupt."""
        ans = tool_context.interrupt(
            ACCESS_REQUEST_INTERRUPT_KIND,
            reason={"kind": ACCESS_REQUEST_INTERRUPT_KIND, ACCESS_REQUEST_ID_KEY: "req-1", "summary": "Approve"},
        )
        return json.dumps({"approved": ans})

    agent = _make_runtime_agent(tmp_path / "manager.db")
    model = _ScriptedWaitModel(
        [
            [
                ("tu-acc", "ask_access", {}),
                ("tu-wait", "wait", {"minutes": 10, "reason": "after approval"}),
            ]
        ]
    )
    strands_agent = Agent(model=model, tools=[ask_access, wait], callback_handler=None)

    with caplog.at_level(logging.INFO):
        res1, _parts = await _invoke_and_pause(agent, strands_agent)
    assert res1.stop_reason == "interrupt"
    assert "event=timed_wait_deferred" in caplog.text

    mgr = agent.task_manager
    blocker = get_human_interaction_blocker(mgr, TASK_ID)
    assert blocker is not None and blocker["kind"] == ACCESS_REQUEST_BLOCKER
    assert WaitLedger.from_history(mgr.get_task_info(TASK_ID)).total_minutes == 0

    # 30 minutes later (12:30): person approves access request.
    now_box[0] = BASE_TIME + timedelta(minutes=30)
    access_int_id = blocker["interrupt_id"]
    agent._pending_interrupt_responses[TASK_ID] = TaskSteering(
        steering_id="acc-approved",
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id=TASK_ID,
        interrupt_responses=[{"interrupt_id": access_int_id, "response": {ACCESS_REQUEST_ID_KEY: "req-1", "ok": True}}],
        status=TaskSteeringStatus.PENDING,
    )
    res2 = await _resume_live_turn(agent)
    assert res2.stop_reason == "interrupt"

    # C15: wait(10) starts NOW (12:30 -> 12:40), not 12:00 -> 12:10.
    wait_blocker = get_human_interaction_blocker(mgr, TASK_ID)
    assert wait_blocker is not None and wait_blocker["kind"] == TIMED_WAIT_BLOCKER
    assert wait_blocker["requested_at"] == (BASE_TIME + timedelta(minutes=30)).isoformat()
    assert wait_blocker["resume_at"] == (BASE_TIME + timedelta(minutes=40)).isoformat()
    assert WaitLedger.from_history(mgr.get_task_info(TASK_ID)).total_minutes == 10

    # At 12:35 still waiting; at 12:40 resumes with waited_minutes=10.0.
    now_box[0] = BASE_TIME + timedelta(minutes=35)
    assert await resume_due_timed_waits(agent) == 0

    now_box[0] = BASE_TIME + timedelta(minutes=40)
    assert await resume_due_timed_waits(agent) == 1
    res3 = await _resume_live_turn(agent)
    assert res3.stop_reason == "end_turn"
    wait_results = [r for r in model.observed_tool_results if r.get("status") == "waited"]
    assert len(wait_results) == 1
    assert wait_results[0]["waited_minutes"] == 10.0
    assert wait_results[0]["now_utc"] == (BASE_TIME + timedelta(minutes=40)).isoformat()


@pytest.mark.asyncio
async def test_c01_c15_wait_deferred_behind_setup_run_starts_at_setup_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)
    monkeypatch.setattr(tool_setup_run_runtime, "advance", AsyncMock(return_value=False))

    @tool(context=True)
    def fake_setup(tool_context: ToolContext) -> str:
        """Stub set_up_tools raising a local tool_setup_run interrupt."""
        ans = tool_context.interrupt(
            TOOL_SETUP_RUN_KIND,
            reason={"kind": TOOL_SETUP_RUN_KIND, "run_id": "run-setup-1", "summary": "Setup"},
        )
        return json.dumps({"setup": ans})

    agent = _make_runtime_agent(tmp_path / "manager.db")
    tool_setup_run_runtime.run_repository(agent).create(
        ToolSetupRunV1(
            run_id="run-setup-1",
            team_id=TEST_TEAM,
            person_id="alice",
            task_id=TASK_ID,
            agent_id=AGENT_ID,
            agent_type="Worker",
            tool_ids=("fn:wait",),
            pieces=(RunPieceV1(piece_id="p1", tool_id="fn:wait", kind="sign_in", status=WAITING, attempt=1),),
        )
    )
    model = _ScriptedWaitModel(
        [
            [
                ("tu-setup", "fake_setup", {}),
                ("tu-wait", "wait", {"minutes": 10, "reason": "after setup"}),
            ]
        ]
    )
    strands_agent = Agent(model=model, tools=[fake_setup, wait], callback_handler=None)

    with caplog.at_level(logging.INFO):
        res1, _parts = await _invoke_and_pause(agent, strands_agent)
    assert res1.stop_reason == "interrupt"
    assert "event=timed_wait_deferred" in caplog.text

    mgr = agent.task_manager
    blocker = get_human_interaction_blocker(mgr, TASK_ID)
    assert blocker is not None and blocker["kind"] == SETUP_RUN_BLOCKER
    assert WaitLedger.from_history(mgr.get_task_info(TASK_ID)).total_minutes == 0

    # 30 minutes later (12:30): setup run finishes.
    now_box[0] = BASE_TIME + timedelta(minutes=30)
    setup_int_id = blocker["interrupt_id"]
    agent._pending_interrupt_responses[TASK_ID] = TaskSteering(
        steering_id="setup-done",
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id=TASK_ID,
        interrupt_responses=[
            {"interrupt_id": setup_int_id, "response": {"kind": TOOL_SETUP_RUN_RESPONSE_KIND, "run_id": "run-setup-1"}}
        ],
        status=TaskSteeringStatus.PENDING,
    )
    res2 = await _resume_live_turn(agent)
    assert res2.stop_reason == "interrupt"

    wait_blocker = get_human_interaction_blocker(mgr, TASK_ID)
    assert wait_blocker is not None and wait_blocker["kind"] == TIMED_WAIT_BLOCKER
    assert wait_blocker["requested_at"] == (BASE_TIME + timedelta(minutes=30)).isoformat()
    assert wait_blocker["resume_at"] == (BASE_TIME + timedelta(minutes=40)).isoformat()


@pytest.mark.asyncio
async def test_c15_stage_timed_wait_refuses_without_charging_when_next_run_no_longer_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from access_control.identity import ENV_SCHEDULER_SIGNING_KEY, ISSUER_SCHEDULER, ContextSigner

    use_scheduler_key(monkeypatch)
    now_box = [datetime.now(timezone.utc).replace(second=0, microsecond=0)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    woken: list[Any] = []
    monkeypatch.setattr(timed_wait_runtime, "schedule_pending_work", woken.append)
    try:
        db_path = tmp_path / "team.db"
        store = ScheduleStore(db_path=str(db_path))
        next_fire = now_box[0] + timedelta(minutes=11)
        store.add(
            ScheduledMessage(
                schedule_id="sched-tight",
                management_class=ScheduleManagementClass.USER,
                payload=Task(task_id="t-tight", task_type=MessageMetaType.REQUEST, problem_statement="tight"),
                interval_ms=11 * 60 * 1000,
                repeat_count=None,
                next_fire_at=next_fire.isoformat(),
                enabled=True,
            )
        )
        signer = ContextSigner.from_value(ISSUER_SCHEDULER, os.environ[ENV_SCHEDULER_SIGNING_KEY])
        signed = signer.sign(
            TaskContextV1(
                context_id="ctx-tight",
                issuer=ISSUER_SCHEDULER,
                team_id=TEST_TEAM,
                person_id="alice",
                person_roles=("member",),
                schedule_id="sched-tight",
                issued_at_utc=now_box[0],
                expires_at_utc=now_box[0] + timedelta(hours=1),
            )
        )
        meta = MessageMeta(gateway_context=GatewayContext(task_context=signed))
        agent = _make_runtime_agent(db_path, meta=meta, schedule_store=store)

        # Tool runs at t+0 (10m fits before next_fire at t+11m) and raises interrupt.
        model = _ScriptedWaitModel([[("tu-tight", "wait", {"minutes": 10, "reason": "tight fit"})]])
        strands_agent = Agent(model=model, tools=[wait], callback_handler=None)
        parts = StrandsParts(agent=strands_agent, loaded=SimpleNamespace(), sync=None)  # type: ignore[arg-type]
        set_agent_ref(agent)
        try:
            with task_work(TASK_ID) as work:
                work.strands = parts
                result = await strands_agent.invoke_async("wait 10m")
                assert result.stop_reason == "interrupt"
                # Clock advances 2 minutes before stage_timed_wait runs -> t+12m >= t+11m -> refused at start!
                now_box[0] = now_box[0] + timedelta(minutes=2)
                task_info = agent.task_manager.get_task_info(TASK_ID)
                await send_interrupt_request_for_result(
                    agent,
                    result=result,
                    task=task_info.task,
                    msg_header=task_info.header,
                    msg_meta=task_info.meta,
                    invoke_kwargs={},
                    callback_handler=lambda **_k: None,
                    full_prompt="wait 10m",
                )
        finally:
            set_agent_ref(None)

        # Nothing charged, no blocker left, refusal response queued for immediate resume.
        assert WaitLedger.from_history(agent.task_manager.get_task_info(TASK_ID)).total_minutes == 0
        assert get_human_interaction_blocker(agent.task_manager, TASK_ID) is None
        assert TASK_ID in agent._pending_interrupt_responses
        assert woken == [agent]

        resumed = await _resume_live_turn(agent)
        assert resumed.stop_reason == "end_turn"
        assert any(r.get("status") == "refused" and "next run" in r.get("reason", "") for r in model.observed_tool_results)
    finally:
        forget_task_context_keys()


# -- C01 edge cases & failure/fallback paths -------------------------------------------------


@pytest.mark.asyncio
async def test_c01_reraise_keeps_original_times_clears_held_mark_and_never_double_charges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])

    agent = _make_runtime_agent(tmp_path / "manager.db")
    agent.task_manager.update_main_task_state(TASK_ID, ProgressState.WAITING, BASE_TIME.isoformat())
    reason = {
        "kind": TIMED_WAIT_KIND,
        "call_id": "call-same",
        "tool": WAIT_TOOL_NAME,
        "tool_use_id": "tu-same",
        "minutes": 10,
        "reason": "redeploy",
    }
    first_decision = await stage_timed_wait(
        agent, task_id=TASK_ID, interrupt_id="int-1", reason=reason, msg_meta=MessageMeta()
    )
    assert first_decision.action == "record"
    assert await finish_timed_wait(agent, first_decision)
    mark_answer_held(agent.task_manager, TASK_ID, TIMED_WAIT_BLOCKER, "int-1", holder_id=AGENT_ID, at=BASE_TIME.isoformat())

    # 3 minutes later the same call_id re-raises -> no second charge, original resume_at kept, held mark cleared.
    now_box[0] = BASE_TIME + timedelta(minutes=3)
    second_decision = await stage_timed_wait(
        agent, task_id=TASK_ID, interrupt_id="int-1", reason=reason, msg_meta=MessageMeta()
    )
    assert second_decision.action == "restore"
    assert await finish_timed_wait(agent, second_decision)
    assert WaitLedger.from_history(agent.task_manager.get_task_info(TASK_ID)).total_minutes == 10
    blocker = get_human_interaction_blocker(agent.task_manager, TASK_ID)
    assert blocker is not None
    assert not answer_held(blocker)
    assert blocker["resume_at"] == (BASE_TIME + timedelta(minutes=10)).isoformat()


@pytest.mark.asyncio
async def test_c01_timed_wait_does_not_replace_pending_interrupt_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    agent = _make_runtime_agent(tmp_path / "manager.db")
    agent.task_manager.update_main_task_state(TASK_ID, ProgressState.WAITING, BASE_TIME.isoformat())
    rec = timed_wait_record(
        interrupt_id="int-tw",
        call_id="call-tw",
        tool=WAIT_TOOL_NAME,
        tool_use_id="tu-tw",
        minutes=5,
        reason="redeploy",
        requested_at=BASE_TIME.isoformat(),
        resume_at=(BASE_TIME + timedelta(minutes=5)).isoformat(),
    )
    record_human_interaction_blocker(agent.task_manager, TASK_ID, rec)
    agent._interrupted_turn_sessions[TASK_ID] = SimpleNamespace(
        task_id=TASK_ID,
        interrupts=[SimpleNamespace(interrupt_id="int-tw")],
        strands=SimpleNamespace(replace_before_next_turn=True),
    )
    other_steering = TaskSteering(
        steering_id="other-pending",
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id=TASK_ID,
        guidance_text="other answer",
        status=TaskSteeringStatus.PENDING,
    )
    agent._pending_interrupt_responses[TASK_ID] = other_steering

    now_box[0] = BASE_TIME + timedelta(minutes=6)
    assert await resume_due_timed_waits(agent) == 0
    assert agent._pending_interrupt_responses[TASK_ID] is other_steering
    assert not answer_held(get_human_interaction_blocker(agent.task_manager, TASK_ID))

    # Once the other pending answer is consumed, the next pass hands the timed-wait answer.
    agent._pending_interrupt_responses.pop(TASK_ID)
    assert await resume_due_timed_waits(agent) == 1
    assert answer_held(get_human_interaction_blocker(agent.task_manager, TASK_ID), AGENT_ID)


@pytest.mark.asyncio
async def test_resume_without_paused_turn_drops_turn_and_resumes_from_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now_box = [BASE_TIME + timedelta(minutes=5)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    woken: list[Any] = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)

    agent = _make_runtime_agent(tmp_path / "manager.db")
    agent.task_manager.update_main_task_state(TASK_ID, ProgressState.WAITING, BASE_TIME.isoformat())
    rec = timed_wait_record(
        interrupt_id="int-drop",
        call_id="call-drop",
        tool=WAIT_TOOL_NAME,
        tool_use_id="tu-drop",
        minutes=5,
        reason="redeploy",
        requested_at=BASE_TIME.isoformat(),
        resume_at=(BASE_TIME + timedelta(minutes=5)).isoformat(),
    )
    record_human_interaction_blocker(agent.task_manager, TASK_ID, rec)
    mark_answer_held(agent.task_manager, TASK_ID, TIMED_WAIT_BLOCKER, "int-drop", holder_id=AGENT_ID, at=now_box[0].isoformat())
    agent._interrupted_turn_sessions[TASK_ID] = SimpleNamespace(
        task_id=TASK_ID,
        interrupts=[SimpleNamespace(interrupt_id="int-drop")],
        strands=SimpleNamespace(replace_before_next_turn=False),
    )
    steering = TaskSteering(
        steering_id="timed_wait_resume_int-drop",
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id=TASK_ID,
        interrupt_responses=[{"interrupt_id": "int-drop", "response": {"kind": TIMED_WAIT_RESPONSE_KIND, "interrupt_id": "int-drop"}}],
        status=TaskSteeringStatus.PENDING,
    )

    with caplog.at_level(logging.WARNING):
        handled = await resume_without_paused_turn(agent, TASK_ID, steering, why="answer_not_converted")

    assert handled is True
    assert TASK_ID not in agent._interrupted_turn_sessions
    assert get_human_interaction_blocker(agent.task_manager, TASK_ID) is None
    assert agent.task_manager.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert TASK_ID in agent.task_manager.task_state.task_queue
    assert woken == [agent]
    assert "event=timed_wait_answer_not_taken" in caplog.text
    assert "event=timed_wait_paused_turn_dropped" in caplog.text


@pytest.mark.asyncio
async def test_executing_state_anomaly_and_per_task_failure_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now_box = [BASE_TIME + timedelta(minutes=10)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    agent = _make_runtime_agent(tmp_path / "manager.db", task_id="task-exec")
    # task-exec stays IN_PROGRESS with a due timed-wait blocker and no live session -> logged anomaly, untouched.
    record_human_interaction_blocker(
        agent.task_manager,
        "task-exec",
        timed_wait_record(
            interrupt_id="int-exec",
            call_id="call-exec",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-exec",
            minutes=5,
            reason="anomaly",
            requested_at=BASE_TIME.isoformat(),
            resume_at=(BASE_TIME + timedelta(minutes=5)).isoformat(),
        ),
    )

    # task-bad fails during resume, task-good still resumes in the same pass.
    for tid, iid in (("task-bad", "int-bad"), ("task-good", "int-good")):
        agent.task_manager.add_task(
            Task(task_id=tid, task_type=MessageMetaType.REQUEST, problem_statement=tid),
            MessageHeader(from_agent_id="scheduler"),
            MessageMeta(),
            owner_person_id=None,
        )
        agent.task_manager.update_main_task_state(tid, ProgressState.WAITING, BASE_TIME.isoformat())
        record_human_interaction_blocker(
            agent.task_manager,
            tid,
            timed_wait_record(
                interrupt_id=iid,
                call_id=f"call-{tid}",
                tool=WAIT_TOOL_NAME,
                tool_use_id=f"tu-{tid}",
                minutes=5,
                reason=tid,
                requested_at=BASE_TIME.isoformat(),
                resume_at=(BASE_TIME + timedelta(minutes=5)).isoformat(),
            ),
        )

    orig_resume = timed_wait_runtime._resume_task_from_note

    async def flaky_resume(a: Any, *, task_id: str, blocker: dict[str, Any], now: datetime, wake: bool) -> bool:
        if task_id == "task-bad":
            raise RuntimeError("transient sqlite error")
        return await orig_resume(a, task_id=task_id, blocker=blocker, now=now, wake=wake)

    monkeypatch.setattr(timed_wait_runtime, "_resume_task_from_note", flaky_resume)

    with caplog.at_level(logging.WARNING):
        resumed = await resume_due_timed_waits(agent)

    assert resumed == 1
    assert agent.task_manager.get_task_info("task-exec").state == ProgressState.IN_PROGRESS
    assert agent.task_manager.get_task_info("task-bad").state == ProgressState.WAITING
    assert agent.task_manager.get_task_info("task-good").state == ProgressState.ACCEPTED
    assert "event=timed_wait_executing_with_blocker" in caplog.text
    assert "event=timed_wait_resume_failed" in caplog.text


@pytest.mark.asyncio
async def test_c19_unresumable_overdue_wait_backs_off_idle_sleep_and_warns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    due_at = BASE_TIME + timedelta(minutes=5)
    now_box = [due_at]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])

    agent = _make_runtime_agent(tmp_path / "manager.db", task_id="task-exec")
    record_human_interaction_blocker(
        agent.task_manager,
        "task-exec",
        timed_wait_record(
            interrupt_id="int-exec",
            call_id="call-exec",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-exec",
            minutes=5,
            reason="anomaly",
            requested_at=BASE_TIME.isoformat(),
            resume_at=due_at.isoformat(),
        ),
    )

    sleeps: list[float] = []
    with caplog.at_level(logging.DEBUG, logger="custom_agents.timed_wait_runtime"):
        for overdue_seconds in (0, 20, 60, 180, 10800):
            now_box[0] = due_at + timedelta(seconds=overdue_seconds)
            assert await resume_due_timed_waits(agent) == 0
            sleeps.append(await scheduled_runtime._idle_sleep_seconds(agent, can_claim_scheduled_work=False))

    assert sleeps == [1.0, 10.0, 30.0, 60.0, 60.0]

    warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "event=timed_wait_executing_with_blocker" in rec.getMessage()
    ]
    debugs = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.DEBUG and "event=timed_wait_executing_with_blocker" in rec.getMessage()
    ]
    assert len(warnings) == 1
    assert len(debugs) == 4

    # An overdue unresumable wait must not mask a healthy upcoming wait that comes due sooner.
    agent.task_manager.add_task(
        Task(task_id="task-upcoming", task_type=MessageMetaType.REQUEST, problem_statement="healthy wait"),
        MessageHeader(from_agent_id="scheduler"),
        MessageMeta(),
        owner_person_id=None,
    )
    agent.task_manager.update_main_task_state("task-upcoming", ProgressState.WAITING, now_box[0].isoformat())
    record_human_interaction_blocker(
        agent.task_manager,
        "task-upcoming",
        timed_wait_record(
            interrupt_id="int-up",
            call_id="call-up",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-up",
            minutes=1,
            reason="healthy",
            requested_at=now_box[0].isoformat(),
            resume_at=(now_box[0] + timedelta(seconds=5)).isoformat(),
        ),
    )
    assert await scheduled_runtime._idle_sleep_seconds(agent, can_claim_scheduled_work=False) == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_c19_repeated_resume_failure_warns_once_and_prunes_after_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    due_at = BASE_TIME + timedelta(minutes=5)
    now_box = [due_at + timedelta(seconds=10)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    agent = _make_runtime_agent(tmp_path / "manager.db", task_id="task-flaky")
    agent.task_manager.update_main_task_state("task-flaky", ProgressState.WAITING, BASE_TIME.isoformat())
    record_human_interaction_blocker(
        agent.task_manager,
        "task-flaky",
        timed_wait_record(
            interrupt_id="int-flaky",
            call_id="call-flaky",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-flaky",
            minutes=5,
            reason="transient error",
            requested_at=BASE_TIME.isoformat(),
            resume_at=due_at.isoformat(),
        ),
    )

    fail = [True]
    orig_resume = timed_wait_runtime._resume_task_from_note

    async def sometimes_fails(a: Any, *, task_id: str, blocker: dict[str, Any], now: datetime, wake: bool) -> bool:
        if fail[0]:
            raise RuntimeError("sqlite busy")
        return await orig_resume(a, task_id=task_id, blocker=blocker, now=now, wake=wake)

    monkeypatch.setattr(timed_wait_runtime, "_resume_task_from_note", sometimes_fails)

    with caplog.at_level(logging.DEBUG, logger="custom_agents.timed_wait_runtime"):
        assert await resume_due_timed_waits(agent) == 0
        now_box[0] = due_at + timedelta(seconds=40)
        assert await resume_due_timed_waits(agent) == 0
        assert await seconds_until_next_resume(agent) == pytest.approx(20.0)

        fail[0] = False
        assert await resume_due_timed_waits(agent) == 1

    warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "event=timed_wait_resume_failed" in rec.getMessage()
    ]
    debugs = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.DEBUG and "event=timed_wait_resume_failed" in rec.getMessage()
    ]
    assert len(warnings) == 1
    assert len(debugs) == 1
    assert not getattr(agent, "_timed_wait_warned", set())


# -- A6: runtime work summary and stop/cancel cleanup ---------------------------------------


@pytest.mark.asyncio
async def test_a6_runtime_work_summary_describes_timed_wait_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_agents.manager_control_runtime import _runtime_work_root_summary
    from custom_agents.task_human_interaction_blockers import mark_answer_held, remove_answer_mark
    from custom_agents.timed_waits import TIMED_WAIT_KIND

    now_box = [BASE_TIME + timedelta(minutes=2)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])

    agent = _make_runtime_agent(tmp_path / "manager.db")
    agent.task_manager.update_main_task_state(TASK_ID, ProgressState.WAITING, BASE_TIME.isoformat())
    record_human_interaction_blocker(
        agent.task_manager,
        TASK_ID,
        timed_wait_record(
            interrupt_id="int-sum",
            call_id="call-sum",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-sum",
            minutes=10,
            reason="Portainer redeploy",
            requested_at=BASE_TIME.isoformat(),
            resume_at=(BASE_TIME + timedelta(minutes=10)).isoformat(),
        ),
    )
    root = agent.task_manager.get_task_info(TASK_ID)
    running = await _runtime_work_root_summary(agent, root)
    assert running.blocker == "Waiting until 2026-11-26 12:10:00 UTC (10-minute wait: Portainer redeploy)."

    # After the timer ends while the lane is busy with another task (answer held by this agent).
    now_box[0] = BASE_TIME + timedelta(minutes=50)
    assert mark_answer_held(
        agent.task_manager,
        TASK_ID,
        TIMED_WAIT_KIND,
        "int-sum",
        holder_id=agent.id,
        at=(BASE_TIME + timedelta(minutes=10)).isoformat(),
    )
    held_summary = await _runtime_work_root_summary(agent, root)
    assert (
        held_summary.blocker
        == "The 10-minute wait (Portainer redeploy) ended at 2026-11-26 12:10:00 UTC; "
        "the task continues when the agent finishes its current work."
    )

    # After the timer ends and no answer is held (waiting for the scheduler pass / recovery).
    remove_answer_mark(agent.task_manager, TASK_ID, TIMED_WAIT_KIND, "int-sum")
    overdue_summary = await _runtime_work_root_summary(agent, root)
    assert (
        overdue_summary.blocker
        == "The 10-minute wait (Portainer redeploy) ended at 2026-11-26 12:10:00 UTC; "
        "waiting for the agent to resume the task."
    )


@pytest.mark.asyncio
async def test_a6_stopping_a_waiting_task_clears_blocker_drops_turn_and_never_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    woken: list[Any] = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)

    agent = _make_runtime_agent(tmp_path / "manager.db")
    task_steering_runtime._register_terminal_task_steering_cleanup(agent)
    model = _ScriptedWaitModel([[("tu-stop", "wait", {"minutes": 10, "reason": "Portainer redeploy"})]])
    strands_agent = Agent(model=model, tools=[wait], callback_handler=None)

    result, parts = await _invoke_and_pause(agent, strands_agent)
    assert result.stop_reason == "interrupt"
    assert get_human_interaction_blocker(agent.task_manager, TASK_ID) is not None
    assert TASK_ID in agent._interrupted_turn_sessions

    # Stop/cancel the waiting task: terminal cleanup removes the blocker and drops the paused turn.
    now_box[0] = BASE_TIME + timedelta(minutes=2)
    cancelled = await agent.task_manager.operations_runtime.run(
        agent.task_manager.cancel_task,
        TASK_ID,
        "Stopped by person during timed wait.",
        now_box[0].isoformat(),
    )
    assert cancelled is True
    assert get_human_interaction_blocker(agent.task_manager, TASK_ID) is None
    assert TASK_ID not in agent._interrupted_turn_sessions
    assert TASK_ID not in agent._pending_interrupt_responses
    assert parts.replace_before_next_turn is True

    # Past the original resume time, nothing resumes or schedules a wake.
    now_box[0] = BASE_TIME + timedelta(minutes=15)
    assert await resume_due_timed_waits(agent) == 0
    assert await seconds_until_next_resume(agent) is None
    assert woken == []
    assert TASK_ID not in agent.task_manager.task_state.task_queue


@pytest.mark.asyncio
async def test_crash_between_record_and_waiting_recovers_waits_and_delivers_note_to_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Charge and blocker persist before WAITING; a crash in that window recovers to WAITING,
    waits until due, resumes from a durable note, and delivers that note to the next model turn."""

    from collections import deque
    import dataclasses

    from tests.test_runtime_owner_boundaries import build_turn_owner

    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    db_path = tmp_path / "manager.db"
    agent = _make_runtime_agent(db_path)
    model = _ScriptedWaitModel([[("tu-crash", "wait", {"minutes": 10, "reason": "Portainer redeploy"})]])
    strands_agent = Agent(model=model, tools=[wait], callback_handler=None)

    # Simulate process crash right before ProgressState.WAITING is saved.
    orig_update = agent.task_manager.update_main_task_state

    def crash_on_waiting(task_id: str, new_state: ProgressState, *args: Any, **kwargs: Any) -> bool:
        if new_state == ProgressState.WAITING:
            raise RuntimeError("simulated crash before WAITING")
        return orig_update(task_id, new_state, *args, **kwargs)

    monkeypatch.setattr(agent.task_manager, "update_main_task_state", crash_on_waiting)
    with pytest.raises(RuntimeError, match="simulated crash before WAITING"):
        await _invoke_and_pause(agent, strands_agent)

    # Reload from SQLite in a fresh process: state is still IN_PROGRESS, but charge and blocker are durable.
    restarted_mgr = TaskManager(db_path=str(db_path))
    restarted_mgr.set_owner_identity(AGENT_ID, "Worker", TEST_TEAM, "Worker")
    restarted_mgr.restore_runtime_state()
    pre_recovery = restarted_mgr.get_task_info(TASK_ID)
    assert pre_recovery is not None
    assert pre_recovery.state == ProgressState.IN_PROGRESS
    assert WaitLedger.from_history(pre_recovery).total_minutes == 10
    saved_blocker = get_human_interaction_blocker(restarted_mgr, TASK_ID)
    assert saved_blocker is not None and saved_blocker["kind"] == TIMED_WAIT_KIND

    # Restart 2 minutes later (even past a short executing-task grace period): recovery moves it to WAITING.
    now_box[0] = BASE_TIME + timedelta(minutes=2)
    restarted = _make_runtime_agent(db_path, add_initial_task=False)
    restarted.task_manager = restarted_mgr
    restarted._recovery_retry_deadline_seconds = lambda: 1.0
    await runtime_recovery.recover_runtime_after_restart(restarted)

    assert restarted_mgr.get_task_info(TASK_ID).state == ProgressState.WAITING
    assert list(restarted_mgr.task_state.task_queue) == []
    assert get_human_interaction_blocker(restarted_mgr, TASK_ID) is not None

    # When the 10-minute wait ends, resume_due_timed_waits queues the task at front with its durable note.
    now_box[0] = BASE_TIME + timedelta(minutes=10, seconds=5)
    assert await resume_due_timed_waits(restarted) == 1
    assert restarted_mgr.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert list(restarted_mgr.task_state.task_queue) == [TASK_ID]
    assert get_human_interaction_blocker(restarted_mgr, TASK_ID) is None
    expected_note = timed_waits_rules.resume_note(saved_blocker, now=now_box[0])
    assert restarted_mgr.read_runtime_resume_directive(TASK_ID) == expected_note

    # Drain the queue and run the real turn owner to prove the saved note reaches the model prompt.
    assert restarted_mgr.get_next_task_to_process() == TASK_ID
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
            "read_runtime_resume_directive": restarted_mgr.read_runtime_resume_directive,
            "read_partial_tool_call_limit": restarted_mgr.read_partial_tool_call_limit,
            "release_turn_start_notes": restarted_mgr.release_turn_start_notes,
        },
    )

    async def record_decision(*, incoming_content: Task, start_notes: Any = None, **_kwargs: Any) -> Task:
        assert restarted_mgr.create_turn_action_batch(incoming_content.task_id, [], release_notes=start_notes)
        return Task(task_id=incoming_content.task_id, task_type=MessageMetaType.RESPONSE, answer="done")

    executor.effects = dataclasses.replace(executor.effects, execute_turn_decision_outbox=record_decision)
    resumed_info = restarted_mgr.get_task_info(TASK_ID)
    assert resumed_info is not None
    response = await executor.process_llm_interaction(resumed_info.task, resumed_info.header, resumed_info.meta)

    assert response.answer == "done"
    assert len(captured_prompts) == 1
    assert expected_note in captured_prompts[0]
    assert "10-minute wait (Portainer redeploy) ended" in captured_prompts[0]
    assert restarted_mgr.read_runtime_resume_directive(TASK_ID) is None


@pytest.mark.asyncio
async def test_retried_resume_from_note_adds_only_one_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying a failed note-based resume at a later time keeps exactly one resume directive."""

    now_box = [BASE_TIME + timedelta(minutes=10, seconds=5)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    agent = _make_runtime_agent(tmp_path / "manager.db")
    agent.task_manager.update_main_task_state(TASK_ID, ProgressState.WAITING, BASE_TIME.isoformat())
    rec = timed_wait_record(
        interrupt_id="int-retry",
        call_id="call-retry",
        tool=WAIT_TOOL_NAME,
        tool_use_id="tu-retry",
        minutes=10,
        reason="Portainer redeploy",
        requested_at=BASE_TIME.isoformat(),
        resume_at=(BASE_TIME + timedelta(minutes=10)).isoformat(),
    )
    record_human_interaction_blocker(agent.task_manager, TASK_ID, rec)

    # First attempt fails during the single atomic persistence write: rolls back cleanly with no saved note.
    orig_persist = agent.task_manager._persist_runtime_state
    fail_once = [True]

    def flaky_persist(*args: Any, **kwargs: Any) -> None:
        if fail_once[0]:
            fail_once[0] = False
            raise RuntimeError("transient sqlite failure")
        orig_persist(*args, **kwargs)

    monkeypatch.setattr(agent.task_manager, "_persist_runtime_state", flaky_persist)
    assert await resume_due_timed_waits(agent) == 0
    assert get_human_interaction_blocker(agent.task_manager, TASK_ID) is not None
    assert agent.task_manager.read_runtime_resume_directive(TASK_ID) is None
    assert list(agent.task_manager.task_state.task_queue) == []

    # Retry 2 minutes later succeeds in one write with the actual resume time and elapsed minutes.
    now_box[0] = BASE_TIME + timedelta(minutes=12, seconds=5)
    assert await resume_due_timed_waits(agent) == 1
    assert get_human_interaction_blocker(agent.task_manager, TASK_ID) is None
    assert list(agent.task_manager.task_state.task_queue) == [TASK_ID]

    directive = agent.task_manager.read_runtime_resume_directive(TASK_ID) or ""
    assert directive == timed_waits_rules.resume_note(rec, now=now_box[0])
    assert directive.count("Portainer redeploy") == 1


# -- Contract drain (release/0.10): own due timed waits still resume; no takeover ------------


@pytest.mark.asyncio
async def test_draining_agent_scheduler_pass_resumes_due_timed_wait_and_caps_idle_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent draining for another message contract still wakes for and resumes its own due timed wait."""

    from custom_agents.contract_drain_state import ContractDrain

    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    woken: list[Any] = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)

    agent = _make_runtime_agent(tmp_path / "manager.db")
    model = _ScriptedWaitModel([[("tu-wait-drain", "wait", {"minutes": 10, "reason": "Portainer redeploy"})]])
    strands_agent = Agent(model=model, tools=[wait], callback_handler=None)

    result, _parts = await _invoke_and_pause(agent, strands_agent)
    assert result.stop_reason == "interrupt"

    agent._contract_drain = ContractDrain(reason="another message contract", started_at=BASE_TIME)

    now_box[0] = BASE_TIME + timedelta(minutes=9, seconds=55)
    sleep_s = await scheduled_runtime._idle_sleep_seconds(agent, can_claim_scheduled_work=False)
    assert 4.0 <= sleep_s <= 6.0

    now_box[0] = BASE_TIME + timedelta(minutes=10, seconds=6)
    await _run_real_scheduler_pass(agent, monkeypatch)
    assert TASK_ID in agent._pending_interrupt_responses
    assert woken == [agent]
    assert answer_held(get_human_interaction_blocker(agent.task_manager, TASK_ID), AGENT_ID)


def _save_due_timed_wait_for_owner(db_path: Path, owner_id: str, task_id: str, reason: str) -> dict[str, Any]:
    manager = TaskManager(db_path=str(db_path))
    manager.set_owner_identity(owner_id, owner_id, TEST_TEAM, "Worker")
    assert manager.add_task(
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=reason),
        MessageHeader(from_agent_id="scheduler"),
        MessageMeta(),
        owner_person_id=None,
    )
    assert manager.get_next_task_to_process() == task_id
    manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, BASE_TIME.isoformat())
    manager.update_main_task_state(task_id, ProgressState.WAITING, BASE_TIME.isoformat())
    rec = timed_wait_record(
        interrupt_id=f"int-{task_id}",
        call_id=f"call-{task_id}",
        tool=WAIT_TOOL_NAME,
        tool_use_id=f"tu-{task_id}",
        minutes=2,
        reason=reason,
        requested_at=BASE_TIME.isoformat(),
        resume_at=(BASE_TIME + timedelta(minutes=2)).isoformat(),
    )
    record_human_interaction_blocker(manager, task_id, rec)
    manager._persist_runtime_state()
    return rec


@pytest.mark.asyncio
@pytest.mark.parametrize("draining", [True, False], ids=["draining", "not_draining"])
async def test_draining_agent_start_recovery_resumes_own_due_timed_wait_and_adopts_no_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, draining: bool
) -> None:
    """At start, a draining agent resumes its own due timed wait from a note and takes over no gone agent's wait."""

    from custom_agents.contract_drain_state import ContractDrain
    from custom_agents.lease_loss_wind_down import may_start_next_task

    own_task, gone_task = "task-own-due", "task-gone-due"
    gone_owner = "agent-gone-worker"
    db_path = tmp_path / "manager.db"

    own_rec = _save_due_timed_wait_for_owner(db_path, AGENT_ID, own_task, "own redeploy wait")
    gone_rec = _save_due_timed_wait_for_owner(db_path, gone_owner, gone_task, "gone worker wait")

    now_box = [BASE_TIME + timedelta(minutes=5)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    woken: list[Any] = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)
    monkeypatch.setattr(
        runtime_recovery,
        "gone_owner_ids",
        AsyncMock(side_effect=lambda _agent, ids, **_kw: [owner for owner in ids if owner == gone_owner]),
    )
    monkeypatch.setattr(runtime_recovery, "tell_task_about_its_restarts", AsyncMock())

    agent = _make_runtime_agent(db_path, add_initial_task=False)
    agent.same_role_runtime_takeover_enabled = True
    agent._recovery_retry_deadline_seconds = lambda: None
    agent.deferred_inbox = SimpleNamespace(
        stored_team_id=AsyncMock(return_value=None),
        restore_role=AsyncMock(return_value=False),
        restore=AsyncMock(return_value=False),
    )
    if draining:
        agent._contract_drain = ContractDrain(
            reason="another message contract",
            started_at=now_box[0],
            holds_back_unstarted=True,
        )

    await runtime_recovery.recover_runtime_after_restart(agent)
    mgr = agent.task_manager

    if not draining:
        assert sorted(mgr.task_state.task_queue) == sorted([own_task, gone_task])
        assert mgr.read_runtime_resume_directive(own_task) == timed_waits_rules.resume_note(own_rec, now=now_box[0])
        assert mgr.read_runtime_resume_directive(gone_task) == timed_waits_rules.resume_note(gone_rec, now=now_box[0])
        return

    assert list(mgr.task_state.task_queue) == [own_task]
    assert get_human_interaction_blocker(mgr, own_task) is None
    assert mgr.get_task_info(own_task).state == ProgressState.ACCEPTED
    assert mgr.read_runtime_resume_directive(own_task) == timed_waits_rules.resume_note(own_rec, now=now_box[0])
    assert woken == []

    # The gone worker's saved timed wait is untouched and never adopted.
    assert mgr.get_task_info(gone_task) is None
    saved_owners = {row.owner_agent_id for row in mgr.runtime_state_store.peek_task_runtime_by_role(TEST_TEAM, "Worker")}
    assert gone_owner in saved_owners

    # Even a pool worker drain holding back unstarted tasks may start its own resumed wait.
    assert await may_start_next_task(agent) is True


@pytest.mark.asyncio
async def test_b2_charge_and_blocker_saved_in_one_write_and_failure_rolls_back_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#208 / B2 / C03: charge and blocker persist in one DB write; a failed write rolls back cleanly so a re-run charges once."""

    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    db_path = tmp_path / "manager.db"
    agent = _make_runtime_agent(db_path)
    manager = agent.task_manager
    before_snapshot = manager._export_runtime_state()

    persist_calls: list[int] = []
    history_calls: list[int] = []
    orig_persist = manager._persist_runtime_state
    orig_persist_history = manager.runtime_snapshot_coordinator.persist_task_history
    fail_once = [True]

    def counting_persist(*args: Any, **kwargs: Any) -> None:
        persist_calls.append(1)
        if fail_once[0]:
            fail_once[0] = False
            raise RuntimeError("simulated power loss during single write")
        orig_persist(*args, **kwargs)

    def counting_history(*args: Any, **kwargs: Any) -> bool:
        history_calls.append(1)
        return orig_persist_history(*args, **kwargs)

    monkeypatch.setattr(manager, "_persist_runtime_state", counting_persist)
    monkeypatch.setattr(manager.runtime_snapshot_coordinator, "persist_task_history", counting_history)

    reason = {
        "kind": TIMED_WAIT_KIND,
        "call_id": "call-first-attempt",
        "tool": WAIT_TOOL_NAME,
        "tool_use_id": "tu-1",
        "minutes": 10,
        "reason": "Portainer redeploy",
    }
    with pytest.raises(RuntimeError, match="simulated power loss during single write"):
        await stage_timed_wait(
            agent, task_id=TASK_ID, interrupt_id="int-1", reason=reason, msg_meta=MessageMeta()
        )

    # Exactly one write attempted (no separate history write), full in-memory rollback, nothing charged on disk.
    assert len(persist_calls) == 1
    assert history_calls == []
    assert manager._export_runtime_state() == before_snapshot
    assert WaitLedger.from_history(manager.get_task_info(TASK_ID)).total_minutes == 0
    assert get_human_interaction_blocker(manager, TASK_ID) is None

    reloaded = TaskManager(db_path=str(db_path))
    reloaded.set_owner_identity(AGENT_ID, "Worker", TEST_TEAM, "Worker")
    reloaded.restore_runtime_state()
    assert WaitLedger.from_history(reloaded.get_task_info(TASK_ID)).total_minutes == 0
    assert get_human_interaction_blocker(reloaded, TASK_ID) is None

    # Re-run after crash gets a brand new call_id and persists charge + blocker in one write: charged once (10 min).
    persist_calls.clear()
    reason_retry = {**reason, "call_id": "call-after-restart", "tool_use_id": "tu-2"}
    decision = await stage_timed_wait(
        agent, task_id=TASK_ID, interrupt_id="int-2", reason=reason_retry, msg_meta=MessageMeta()
    )
    assert decision.action == "record"
    assert len(persist_calls) == 1
    assert history_calls == []
    assert WaitLedger.from_history(manager.get_task_info(TASK_ID)).total_minutes == 10
    saved_blocker = get_human_interaction_blocker(manager, TASK_ID)
    assert saved_blocker is not None and saved_blocker["call_id"] == "call-after-restart"

    # C11: prove both the budget charge and the blocker record arrived on disk together.
    reloaded_after = TaskManager(db_path=str(db_path))
    reloaded_after.set_owner_identity(AGENT_ID, "Worker", TEST_TEAM, "Worker")
    reloaded_after.restore_runtime_state()
    assert WaitLedger.from_history(reloaded_after.get_task_info(TASK_ID)).total_minutes == 10
    reloaded_blocker = get_human_interaction_blocker(reloaded_after, TASK_ID)
    assert reloaded_blocker is not None and reloaded_blocker["call_id"] == "call-after-restart"


@pytest.mark.asyncio
async def test_b2_custody_loss_during_stage_timed_wait_guards_rollback_and_skips_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C03: TaskCustodyTakenOver returns skip without start log/wake; RuntimeCustodyLost does not restore into emptied state."""

    from storage.runtime_custody import CustodyStore, RuntimeCustodyLost

    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    reason = {
        "kind": TIMED_WAIT_KIND,
        "call_id": "call-custody",
        "tool": WAIT_TOOL_NAME,
        "tool_use_id": "tu-c",
        "minutes": 5,
        "reason": "wait for build",
    }

    # Case A: TaskCustodyTakenOver drops just this task and returns normally.
    agent_a = _make_runtime_agent(tmp_path / "taken.db")
    wake_a = asyncio.Event()
    agent_a._scheduler_wake = wake_a

    def taken_over(*_args: Any, **_kwargs: Any) -> None:
        agent_a.task_manager._forget_task(TASK_ID)

    monkeypatch.setattr(agent_a.task_manager, "_persist_runtime_state", taken_over)
    with caplog.at_level(logging.INFO):
        decision_a = await stage_timed_wait(
            agent_a, task_id=TASK_ID, interrupt_id="int-a", reason=reason, msg_meta=MessageMeta()
        )
        assert await finish_timed_wait(agent_a, decision_a) is False
    assert decision_a.action == "skip"
    assert not wake_a.is_set()
    assert "event=timed_wait_started" not in caplog.text
    assert get_human_interaction_blocker(agent_a.task_manager, TASK_ID) is None

    # Case B: RuntimeCustodyLost empties the whole state and raises; rollback must not re-populate emptied state.
    agent_b = _make_runtime_agent(tmp_path / "lost.db")

    def whole_row_lost(*_args: Any, **_kwargs: Any) -> None:
        lost = RuntimeCustodyLost(AGENT_ID, CustodyStore.TASK_RUNTIME)
        agent_b.task_manager.runtime_snapshot_coordinator._release_lost_custody(lost)
        raise lost

    monkeypatch.setattr(agent_b.task_manager, "_persist_runtime_state", whole_row_lost)
    with pytest.raises(RuntimeCustodyLost):
        await stage_timed_wait(
            agent_b, task_id=TASK_ID, interrupt_id="int-b", reason=reason, msg_meta=MessageMeta()
        )
    assert agent_b.task_manager.task_state.tasks == {}
    assert agent_b.task_manager.task_state.human_interaction_blockers == {}


@pytest.mark.asyncio
async def test_f6_held_live_answer_then_crash_writes_single_timed_wait_ended_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: timed_wait_ended is written on live consumption (not when held), so crash+restart leaves exactly one entry."""

    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)
    db_path = tmp_path / "f6_single_end.db"
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])

    agent = _make_runtime_agent(db_path)
    model = _ScriptedWaitModel([[("tu-f6", "wait", {"minutes": 10, "reason": "f6 check"})]])
    res, _ = await _invoke_and_pause(agent, Agent(model=model, tools=[wait], callback_handler=None))
    assert res.stop_reason == "interrupt"

    # Timer comes due and answer is held for the live paused turn: NO timed_wait_ended yet.
    now_box[0] = BASE_TIME + timedelta(minutes=10, seconds=5)
    assert await resume_due_timed_waits(agent) == 1
    info_before_crash = agent.task_manager.get_task_info(TASK_ID)
    ended_while_held = [
        e for e in info_before_crash.task.execution_history
        if (e.details or {}).get("event") == TIMED_WAIT_ENDED_EVENT
    ]
    assert ended_while_held == []

    # Crash before the lane consumes the live answer; restart recovers via note-resume.
    restarted_mgr = TaskManager(db_path=str(db_path))
    restarted_mgr.set_owner_identity(AGENT_ID, "Worker", TEST_TEAM, "Worker")
    restarted_mgr.restore_runtime_state()
    restarted = _make_runtime_agent(db_path, add_initial_task=False)
    restarted.task_manager = restarted_mgr

    now_box[0] = BASE_TIME + timedelta(minutes=11)
    await runtime_recovery.recover_runtime_after_restart(restarted)

    info_after = restarted_mgr.get_task_info(TASK_ID)
    ended_after = [
        e.details for e in info_after.task.execution_history
        if (e.details or {}).get("event") == TIMED_WAIT_ENDED_EVENT
    ]
    assert len(ended_after) == 1
    assert ended_after[0]["how"] == "note"
    assert WaitLedger.from_history(info_after).total_minutes == 10


@pytest.mark.asyncio
async def test_f3_resumed_live_turn_releases_live_wait_note_when_decision_keeps_task_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: resuming a live paused turn through the real _run_resumed_turn + outbox releases the live-wait note."""

    from unittest.mock import patch

    from custom_agents.strands_agent import UniversalAgent
    from custom_agents.structured_llm_messages import ModelAgentTurnDecision, ModelNoOpAction
    from custom_agents.task_steering_runtime import resume_interrupted_task_turn
    from storage.config import resolve_storage_settings

    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda _a: None)

    class _WaitThenKeepOpenModel(Model):
        def __init__(self) -> None:
            self.waited = False

        def update_config(self, **config: Any) -> None:
            pass

        def get_config(self) -> dict[str, Any]:
            return {"model_id": "scripted-f3"}

        async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
            yield {
                "output": ModelAgentTurnDecision(
                    actions=[ModelNoOpAction(reason="Wait ended; keeping task open for follow-up")]
                )
            }

        async def stream(
            self,
            messages,
            tool_specs=None,
            system_prompt=None,
            *,
            structured_output_model=None,
            **kwargs,
        ):
            if not self.waited:
                self.waited = True
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "tu_f3", "name": "wait"}}}}
                yield {
                    "contentBlockDelta": {
                        "delta": {"toolUse": {"input": json.dumps({"minutes": 5, "reason": "redeploy"})}}
                    }
                }
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}
                return
            decision = ModelAgentTurnDecision(
                actions=[ModelNoOpAction(reason="Wait ended; keeping task open for follow-up")]
            )
            decision_tool = next(
                (
                    spec["name"]
                    for spec in (tool_specs or ())
                    if isinstance(spec, dict) and str(spec.get("name", "")).endswith("TurnDecision")
                ),
                None,
            )
            yield {"messageStart": {"role": "assistant"}}
            if decision_tool:
                yield {
                    "contentBlockStart": {
                        "start": {"toolUse": {"toolUseId": "tu_f3_dec", "name": decision_tool}}
                    }
                }
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": decision.model_dump_json()}}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}
                return
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": decision.model_dump_json()}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}

    db_path = tmp_path / "f3_release.db"
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    storage_settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db_path)}})
    model = _WaitThenKeepOpenModel()

    with patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=model):
        agent = UniversalAgent(
            id=AGENT_ID,
            name="Worker",
            named_agent_type="Worker",
            team_id=TEST_TEAM,
            is_team_lead=False,
            task_manager=TaskManager(db_path=str(db_path), storage_settings=storage_settings),
            workspace_dir=str(workspace),
            instance_root=str(workspace),
            memory_graph_enabled=False,
            activity_collector_base_url=None,
            subtask_watchdog_interval_seconds=3600,
            model_provider="openai",
            model_id="scripted-f3",
        )
        agent.authorization_config = {"enabled": False}
        agent.tools = [wait]
        agent.team_comms = SimpleNamespace(send_message=AsyncMock())
        agent.task_manager.set_owner_identity(AGENT_ID, "Worker", TEST_TEAM, "Worker")

        task = Task(task_id=TASK_ID, task_type=MessageMetaType.REQUEST, problem_statement="Wait then stay open")
        header = MessageHeader(from_agent_id="gateway", response_routing_key="gateway_chat")
        meta = MessageMeta(conversation_id="conv-f3", msg_correlation_id="corr-f3")
        assert agent.task_manager.add_task(task, header, meta, owner_person_id=None)
        assert agent.task_manager.get_next_task_to_process() == TASK_ID
        agent.task_manager.update_main_task_state(TASK_ID, ProgressState.IN_PROGRESS, BASE_TIME.isoformat())

        # Step 1: Run a real turn that calls wait() and pauses in WAITING with a live session.
        set_agent_ref(agent)
        try:
            with task_work(TASK_ID) as work:
                strands_agent = Agent(model=model, tools=[wait], callback_handler=None)
                agent._strands_parts.agent = strands_agent
                work.strands = agent._strands_parts
                res = await strands_agent.invoke_async(
                    "Wait 5 minutes for redeploy.",
                    structured_output_model=ModelAgentTurnDecision,
                )
                assert res.stop_reason == "interrupt"
                await send_interrupt_request_for_result(
                    agent,
                    result=res,
                    task=task,
                    msg_header=header,
                    msg_meta=meta,
                    invoke_kwargs={"structured_output_model": ModelAgentTurnDecision},
                    callback_handler=lambda **_k: None,
                    full_prompt="Wait 5 minutes for redeploy.",
                )
        finally:
            set_agent_ref(None)

        assert agent.task_manager.get_task_info(TASK_ID).state == ProgressState.WAITING
        assert TASK_ID in agent._interrupted_turn_sessions

        # Step 2: Advance clock past resume_at and hold the live answer.
        now_box[0] = BASE_TIME + timedelta(minutes=5, seconds=5)
        assert await resume_due_timed_waits(agent) == 1
        assert TASK_ID in agent._pending_interrupt_responses

        # Step 3: Resume through the REAL resume_interrupted_task_turn -> _run_resumed_turn ->
        # _execute_turn_decision_outbox path with a NoOp decision that keeps the task open.
        await resume_interrupted_task_turn(agent, TASK_ID)

    # Task remains tracked/open (not archived), the live-wait note was released, and exactly 1 live_turn end entry exists.
    info = agent.task_manager.get_task_info(TASK_ID)
    assert info is not None
    assert not agent.task_manager.is_terminal_state(info.state)
    assert get_human_interaction_blocker(agent.task_manager, TASK_ID) is None
    assert agent.task_manager.read_runtime_resume_directive(TASK_ID) is None
    ended_events = [
        e.details for e in info.task.execution_history
        if (e.details or {}).get("event") == TIMED_WAIT_ENDED_EVENT
    ]
    assert len(ended_events) == 1
    assert ended_events[0]["how"] == "live_turn"


@pytest.mark.asyncio
async def test_c07_timed_wait_pause_flushes_deferred_sqlite_commits_and_keeps_pause_on_flush_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C07 (0.10): with flush deferral enabled, finish_timed_wait flushes charge+record+WAITING; flush error logs and keeps pause."""

    from custom_agents import commit_flushes
    from custom_transport import send_barrier
    from storage import sqlite_connection

    monkeypatch.setattr(send_barrier, "_BEFORE_SEND", None)
    monkeypatch.setattr(sqlite_connection, "_FLUSHES_DEFERRED", False)
    monkeypatch.setattr(sqlite_connection, "_UNFLUSHED", {})
    monkeypatch.setattr(commit_flushes, "_FAILED", None)
    commit_flushes.flush_commits_before_sending()

    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    agent = _make_runtime_agent(tmp_path / "c07_wait.db")
    sqlite_connection.flush_unflushed_commits()
    assert not sqlite_connection.has_unflushed_commits()

    reason = {
        "kind": TIMED_WAIT_KIND,
        "call_id": "call-c07-1",
        "tool": WAIT_TOOL_NAME,
        "tool_use_id": "tu-c07-1",
        "minutes": 10,
        "reason": "power-loss durability check",
    }
    decision = await stage_timed_wait(
        agent, task_id=TASK_ID, interrupt_id="int-c07-1", reason=reason, msg_meta=MessageMeta()
    )
    agent.task_manager.update_main_task_state(TASK_ID, ProgressState.WAITING, BASE_TIME.isoformat())
    # Before finish_timed_wait, the deferred runtime-state commits are still unflushed.
    assert sqlite_connection.has_unflushed_commits()

    assert await finish_timed_wait(agent, decision) is True
    assert not sqlite_connection.has_unflushed_commits()
    assert agent.task_manager.get_task_info(TASK_ID).state == ProgressState.WAITING
    assert get_human_interaction_blocker(agent.task_manager, TASK_ID) is not None

    # Failure case: a failing disk flush logs with identifiers, does NOT undo the pause, and stops future sends.
    agent2 = _make_runtime_agent(tmp_path / "c07_wait_fail.db")
    sqlite_connection.flush_unflushed_commits()

    def failing_flush(**_kwargs: Any) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(sqlite_connection, "flush_unflushed_commits", failing_flush)
    decision2 = await stage_timed_wait(
        agent2,
        task_id=TASK_ID,
        interrupt_id="int-c07-2",
        reason={**reason, "call_id": "call-c07-2", "tool_use_id": "tu-c07-2"},
        msg_meta=MessageMeta(),
    )
    agent2.task_manager.update_main_task_state(TASK_ID, ProgressState.WAITING, BASE_TIME.isoformat())
    assert sqlite_connection.has_unflushed_commits()

    with caplog.at_level(logging.WARNING):
        assert await finish_timed_wait(agent2, decision2) is True
    assert "event=local_pause_commit_flush_failed" in caplog.text
    assert f"agent_id={AGENT_ID}" in caplog.text
    assert f"task_id={TASK_ID}" in caplog.text
    assert "pause_kind=timed_wait" in caplog.text
    assert agent2.task_manager.get_task_info(TASK_ID).state == ProgressState.WAITING
    assert get_human_interaction_blocker(agent2.task_manager, TASK_ID) is not None
    with pytest.raises(commit_flushes.CommitFlushFailed):
        await send_barrier.before_send()
