"""Tests for pure timed-wait rules, the ``wait`` / ``long_wait`` tools, and A4/A5 checks."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from strands.interrupt import Interrupt, InterruptException, _InterruptState
from strands.types.tools import ToolContext

from access_control.identity import ENV_SCHEDULER_SIGNING_KEY, ISSUER_GATEWAY, ISSUER_SCHEDULER, ContextSigner
from custom_agents import timed_waits as timed_waits_rules
from custom_agents.schedule_store import ScheduleStore
from custom_agents.task_manager import TaskManager
from custom_agents.timed_waits import (
    LONG_WAIT_TOOL_ID,
    LONG_WAIT_TOOL_NAME,
    OVERDUE_GRACE_SECONDS,
    TIMED_WAIT_KIND,
    TIMED_WAIT_RESPONSE_KIND,
    TIMED_WAIT_STARTED_EVENT,
    WAIT_TOOL_NAME,
    WaitLedger,
    budget_refusal,
    is_due,
    next_run_refusal,
    resume_note,
    timed_wait_record,
    timer_running,
    validate_wait_args,
    wait_status_summary,
    wait_summary,
)
from custom_messages.contracts.access_v2 import TaskContextV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage, is_recurring_schedule
from custom_messages.contracts.task_v1 import BUBBLED_ENTRY_ID_KEY, Task
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    ScheduleManagementClass,
    TaskHistoryImportance,
    TaskHistoryType,
)
from custom_tools.providers.timed_waits import long_wait, wait
from custom_tools.runtime_context import set_agent_ref, task_work
from runtime_settings.timed_waits import TimedWaitLimits
from tests.broker_access_support import (
    TEST_TEAM,
    context_signer,
    forget_task_context_keys,
    use_scheduler_key,
)

BASE_TIME = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
TASK_ID = "task-timed-wait"
AGENT_ID = "agent-worker-1"


def _make_tool_context(
    tool_use_id: str,
    *,
    interrupt_state: _InterruptState | None = None,
    step_tracking_id: str | None = None,
    tool_name_str: str = "wait",
    messages: list[dict[str, Any]] | None = None,
) -> ToolContext:
    state = interrupt_state if interrupt_state is not None else _InterruptState()
    msg_list = list(messages) if messages is not None else []
    if step_tracking_id is not None:
        msg_list.append(
            {
                "role": "assistant",
                "tracking_id": step_tracking_id,
                "content": [{"toolUse": {"toolUseId": tool_use_id, "name": tool_name_str, "input": {}}}],
            }
        )
    fake_strands = SimpleNamespace(_interrupt_state=state, messages=msg_list)
    return ToolContext(
        tool_use={"toolUseId": tool_use_id, "name": tool_name_str, "input": {}},
        agent=fake_strands,  # type: ignore[arg-type]
        invocation_state={},
    )


def _record_started_wait(
    manager: TaskManager,
    task_id: str,
    *,
    tool_use_id: str,
    minutes: int,
    call_id: str | None = None,
    tool: str = WAIT_TOOL_NAME,
    requested_at: datetime = BASE_TIME,
    reason: str = "wait for deploy",
) -> None:
    resume_at = requested_at + timedelta(minutes=minutes)
    details: dict[str, Any] = {
        "event": TIMED_WAIT_STARTED_EVENT,
        "tool": tool,
        "tool_use_id": tool_use_id,
        "minutes": minutes,
        "requested_at": requested_at.isoformat(),
        "resume_at": resume_at.isoformat(),
        "reason": reason,
    }
    if call_id:
        details["call_id"] = call_id
    manager.append_history_entry(
        task_id,
        f"Waiting {minutes} minutes.",
        entry_type=TaskHistoryType.QUERY_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details=details,
    )


def _make_manager(db_path: Path, *, task_id: str = TASK_ID, meta: MessageMeta | None = None) -> TaskManager:
    mgr = TaskManager(db_path=str(db_path))
    mgr.set_owner_identity(AGENT_ID, "Worker", TEST_TEAM, "Worker")
    assert mgr.add_task(
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="run check"),
        MessageHeader(from_agent_id="gateway"),
        meta or MessageMeta(),
        owner_person_id=None,
    )
    return mgr


def _reload_manager(db_path: Path) -> TaskManager:
    mgr = TaskManager(db_path=str(db_path))
    mgr.set_owner_identity(AGENT_ID, "Worker", TEST_TEAM, "Worker")
    mgr.restore_runtime_state()
    return mgr


def _make_agent(
    manager: TaskManager,
    *,
    limits: TimedWaitLimits | None = None,
    auth_enabled: bool = True,
    tools: tuple[Any, ...] = (wait,),
    schedule_store: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=AGENT_ID,
        team_id=TEST_TEAM,
        named_agent_type="Worker",
        task_manager=manager,
        timed_wait_limits=limits or TimedWaitLimits(),
        authorization_config={"enabled": auth_enabled},
        tools=list(tools),
        _schedule_store=schedule_store,
    )


# -- Pure rules (Unit 10) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "reason"),
    [
        (0, "ok"),
        (-5, "ok"),
        (True, "ok"),
        (False, "ok"),
        (1.5, "ok"),
        (5, ""),
        (5, "   "),
        (5, "x" * 501),
    ],
)
def test_validate_wait_args_rejects_invalid_inputs(minutes: Any, reason: Any) -> None:
    with pytest.raises(ValueError):
        validate_wait_args(minutes, reason)


def test_validate_wait_args_accepts_whole_minutes_and_trims_reason() -> None:
    assert validate_wait_args(1, "  redeploy  ") == (1, "redeploy")
    assert validate_wait_args(1440, "a" * 500) == (1440, "a" * 500)


def test_wait_ledger_counts_both_tools_deduplicates_tool_use_id_and_survives_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "manager.db"
    mgr = _make_manager(db_path)
    _record_started_wait(mgr, TASK_ID, tool_use_id="tu-1", minutes=8, tool=WAIT_TOOL_NAME)
    _record_started_wait(mgr, TASK_ID, tool_use_id="tu-1", minutes=8, tool=WAIT_TOOL_NAME)  # duplicate
    _record_started_wait(mgr, TASK_ID, tool_use_id="tu-2", minutes=30, tool=LONG_WAIT_TOOL_NAME)
    mgr.append_history_entry(TASK_ID, "unrelated event", details={"event": "other", "minutes": 999})

    ledger = WaitLedger.from_history(mgr.get_task_info(TASK_ID))
    assert ledger.total_minutes == 38
    assert ledger.has_started("tu-1")
    assert ledger.has_started("tu-2")

    # Reload from SQLite in a brand-new TaskManager (restart).
    restarted = _reload_manager(db_path)
    restarted_ledger = WaitLedger.from_history(restarted.get_task_info(TASK_ID))
    assert restarted_ledger.total_minutes == 38
    assert restarted_ledger.entry_for("tu-2") is not None
    assert restarted_ledger.entry_for("tu-2").minutes == 30  # type: ignore[union-attr]


def test_budget_refusal_messages_match_auth_mode_and_limits() -> None:
    ledger = WaitLedger(entries_by_call_id={}, total_minutes=15)
    limits = TimedWaitLimits(without_approval=20, with_approval=1440)

    assert budget_refusal(ledger, 5, WAIT_TOOL_NAME, limits) is None

    # Auth on, without long_wait: mentions used/left, request_access, fn:long_wait, long_wait, 24 hours.
    msg_auth_on = budget_refusal(ledger, 6, WAIT_TOOL_NAME, limits, authorization_on=True, has_long_wait_tool=False)
    assert msg_auth_on is not None
    assert "15 minutes already used" in msg_auth_on
    assert "5 minutes left" in msg_auth_on
    assert "request_access" in msg_auth_on
    assert LONG_WAIT_TOOL_ID in msg_auth_on
    assert LONG_WAIT_TOOL_NAME in msg_auth_on
    assert "24 hours" in msg_auth_on

    # Auth on, agent already holds long_wait (C17): tells it directly to use long_wait.
    msg_auth_on_has = budget_refusal(ledger, 6, WAIT_TOOL_NAME, limits, authorization_on=True, has_long_wait_tool=True)
    assert msg_auth_on_has is not None
    assert "Use long_wait instead" in msg_auth_on_has
    assert "request_access" not in msg_auth_on_has

    # Auth off, agent has long_wait (C04).
    msg_auth_off_has = budget_refusal(ledger, 6, WAIT_TOOL_NAME, limits, authorization_on=False, has_long_wait_tool=True)
    assert msg_auth_off_has is not None
    assert "Use long_wait instead" in msg_auth_off_has
    assert "request_access" not in msg_auth_off_has

    # Auth off, agent lacks long_wait (C04).
    msg_auth_off_missing = budget_refusal(
        ledger, 6, WAIT_TOOL_NAME, limits, authorization_on=False, has_long_wait_tool=False
    )
    assert msg_auth_off_missing is not None
    assert "default_tools" in msg_auth_off_missing
    assert LONG_WAIT_TOOL_ID in msg_auth_off_missing
    assert "request_access" not in msg_auth_off_missing

    # long_wait ceiling.
    heavy_ledger = WaitLedger(entries_by_call_id={}, total_minutes=1430)
    assert budget_refusal(heavy_ledger, 10, LONG_WAIT_TOOL_NAME, limits) is None
    msg_ceiling = budget_refusal(heavy_ledger, 11, LONG_WAIT_TOOL_NAME, limits)
    assert msg_ceiling is not None
    assert "1430 minutes already used" in msg_ceiling
    assert "10 minutes left" in msg_ceiling
    assert "24 hours" in msg_ceiling


def test_next_run_refusal_rules_and_boundaries() -> None:
    next_fire = BASE_TIME + timedelta(minutes=10)
    recurring = ScheduledMessage(
        schedule_id="sched-rec",
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_id="t1", task_type=MessageMetaType.REQUEST, problem_statement="check"),
        interval_ms=10 * 60 * 1000,
        repeat_count=None,
        next_fire_at=next_fire.isoformat(),
        enabled=True,
    )
    assert is_recurring_schedule(recurring)

    # Exactly at next_fire_at -> refused, naming next fire time and longest fitting wait (9 min).
    at_boundary = next_run_refusal(recurring, next_fire, now=BASE_TIME)
    assert at_boundary is not None
    assert next_fire.isoformat() in at_boundary
    assert "9 minutes" in at_boundary

    # 1 second before next_fire_at -> allowed.
    assert next_run_refusal(recurring, next_fire - timedelta(seconds=1), now=BASE_TIME) is None

    # Finishing final run or disabled schedule -> allowed.
    assert next_run_refusal(recurring, next_fire, now=BASE_TIME, finishing=True) is None
    assert next_run_refusal(recurring.model_copy(update={"enabled": False}), next_fire, now=BASE_TIME) is None

    # Expired before next_fire -> allowed.
    expired = recurring.model_copy(update={"expires_at_utc": next_fire.isoformat()})
    assert next_run_refusal(expired, next_fire, now=BASE_TIME) is None

    # Paused until later -> effective next fire shifts to paused_until.
    paused = recurring.model_copy(update={"paused_until": (BASE_TIME + timedelta(minutes=25)).isoformat()})
    assert next_run_refusal(paused, BASE_TIME + timedelta(minutes=20), now=BASE_TIME) is None
    assert next_run_refusal(paused, BASE_TIME + timedelta(minutes=25), now=BASE_TIME) is not None

    # One-time schedule -> allowed.
    one_time = ScheduledMessage(
        schedule_id="sched-once",
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_id="t2", task_type=MessageMetaType.REQUEST, problem_statement="once"),
        repeat_count=1,
        next_fire_at=BASE_TIME.isoformat(),
        enabled=True,
    )
    assert not is_recurring_schedule(one_time)
    assert next_run_refusal(one_time, next_fire, now=BASE_TIME) is None


def test_timer_running_is_due_summary_and_resume_note() -> None:
    resume_at = BASE_TIME + timedelta(minutes=10)
    record = timed_wait_record(
        interrupt_id="i-1",
        tool=WAIT_TOOL_NAME,
        tool_use_id="tu-1",
        minutes=10,
        reason="Portainer redeploy",
        requested_at=BASE_TIME.isoformat(),
        resume_at=resume_at.isoformat(),
    )
    assert record["kind"] == TIMED_WAIT_KIND
    assert wait_summary(record) == "Waiting until 2026-11-26 12:10:00 UTC (10-minute wait: Portainer redeploy)."
    assert (
        wait_status_summary(record, now=BASE_TIME + timedelta(minutes=2), held=False)
        == "Waiting until 2026-11-26 12:10:00 UTC (10-minute wait: Portainer redeploy)."
    )
    assert (
        wait_status_summary(record, now=BASE_TIME + timedelta(minutes=50), held=True)
        == "The 10-minute wait (Portainer redeploy) ended at 2026-11-26 12:10:00 UTC; "
        "the task continues when the agent finishes its current work."
    )
    assert (
        wait_status_summary(record, now=BASE_TIME + timedelta(minutes=11), held=False)
        == "The 10-minute wait (Portainer redeploy) ended at 2026-11-26 12:10:00 UTC; "
        "waiting for the agent to resume the task."
    )

    assert not is_due(record, BASE_TIME + timedelta(minutes=9, seconds=59))
    assert timer_running(record, BASE_TIME + timedelta(minutes=9, seconds=59))

    assert is_due(record, resume_at)
    assert timer_running(record, resume_at + timedelta(seconds=OVERDUE_GRACE_SECONDS))
    assert not timer_running(record, resume_at + timedelta(seconds=OVERDUE_GRACE_SECONDS + 1))

    # Malformed record is not excused as a running timer.
    assert not timer_running({"kind": TIMED_WAIT_KIND, "resume_at": "not-a-date"}, BASE_TIME)
    assert not is_due({"kind": TIMED_WAIT_KIND}, BASE_TIME)

    resumed_at = BASE_TIME + timedelta(hours=3)
    note = resume_note(record, now=resumed_at)
    assert "10-minute wait (Portainer redeploy) ended" in note
    assert "the task was resumed at 2026-11-26 15:00:00 UTC" in note
    assert resumed_at.isoformat() in note
    assert "after 180 minutes" in note
    assert "2026-11-26 12:10:00 UTC" in note
    assert resume_at.isoformat() in note
    assert "current time" not in note.lower()
    assert resume_note(record, now=resumed_at) == note


# -- Tool tests: pause, resume, A4 budgets, C04 auth-off ------------------------------------


@pytest.mark.asyncio
async def test_wait_raises_interrupt_and_resumed_call_returns_waited_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])

    mgr = _make_manager(tmp_path / "manager.db")
    agent = _make_agent(mgr)
    state = _InterruptState()
    ctx = _make_tool_context("tu-pause-1", interrupt_state=state, step_tracking_id="step-1")

    set_agent_ref(agent)  # type: ignore[arg-type]
    try:
        with task_work(TASK_ID):
            with pytest.raises(InterruptException) as exc_info:
                await wait(10, "Portainer redeploy", ctx)
    finally:
        set_agent_ref(None)

    interrupt = exc_info.value.interrupt
    assert interrupt.name == TIMED_WAIT_KIND
    assert interrupt.reason["kind"] == TIMED_WAIT_KIND
    assert interrupt.reason["tool"] == WAIT_TOOL_NAME
    assert interrupt.reason["tool_use_id"] == "tu-pause-1"
    assert interrupt.reason["call_id"].startswith("tool_call:")
    assert interrupt.reason["minutes"] == 10
    assert interrupt.reason["reason"] == "Portainer redeploy"
    assert interrupt.reason["requested_at"] == BASE_TIME.isoformat()
    assert interrupt.reason["resume_at"] == (BASE_TIME + timedelta(minutes=10)).isoformat()

    # Simulate stage_timed_wait recording the start entry and the timer answering the interrupt 10m later.
    _record_started_wait(
        mgr,
        TASK_ID,
        tool_use_id="tu-pause-1",
        call_id=interrupt.reason["call_id"],
        minutes=10,
        requested_at=BASE_TIME,
    )
    interrupt.response = {"kind": TIMED_WAIT_RESPONSE_KIND, "interrupt_id": interrupt.id}
    now_box[0] = BASE_TIME + timedelta(minutes=10, seconds=6)

    set_agent_ref(agent)  # type: ignore[arg-type]
    try:
        with task_work(TASK_ID):
            raw = await wait(10, "Portainer redeploy", ctx)
    finally:
        set_agent_ref(None)

    result = json.loads(raw)
    assert result["status"] == "waited"
    assert result["minutes"] == 10
    assert result["waited_minutes"] == 10.1
    assert result["now_utc"] == now_box[0].isoformat()
    assert result["next_step"]


@pytest.mark.asyncio
async def test_repeated_tool_use_id_in_later_step_pauses_and_charges_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C14: a model server that repeats toolUseId in a later step gets a distinct call_id and charge."""

    now_box = [BASE_TIME]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])
    mgr = _make_manager(tmp_path / "manager.db")
    agent = _make_agent(mgr)

    # Step 1: wait(8, "first redeploy") with toolUseId="call_0".
    ctx_step1 = _make_tool_context("call_0", step_tracking_id="step-uuid-1")
    set_agent_ref(agent)  # type: ignore[arg-type]
    try:
        with task_work(TASK_ID), pytest.raises(InterruptException) as first_exc:
            await wait(8, "first redeploy", ctx_step1)
    finally:
        set_agent_ref(None)

    first_reason = first_exc.value.interrupt.reason
    _record_started_wait(
        mgr,
        TASK_ID,
        tool_use_id="call_0",
        call_id=first_reason["call_id"],
        minutes=8,
        requested_at=BASE_TIME,
        reason="first redeploy",
    )

    # Step 2 (2 hours later): server reuses toolUseId="call_0" for wait(12, "second redeploy").
    now_box[0] = BASE_TIME + timedelta(hours=2)
    ctx_step2 = _make_tool_context(
        "call_0",
        step_tracking_id="step-uuid-2",
        messages=ctx_step1.agent.messages,  # step 1 still in transcript, step 2 appended
    )
    set_agent_ref(agent)  # type: ignore[arg-type]
    try:
        with task_work(TASK_ID), pytest.raises(InterruptException) as second_exc:
            await wait(12, "second redeploy", ctx_step2)
    finally:
        set_agent_ref(None)

    second_reason = second_exc.value.interrupt.reason
    assert second_reason["call_id"] != first_reason["call_id"]
    assert second_reason["minutes"] == 12
    assert second_reason["reason"] == "second redeploy"
    assert second_reason["resume_at"] == (now_box[0] + timedelta(minutes=12)).isoformat()

    _record_started_wait(
        mgr,
        TASK_ID,
        tool_use_id="call_0",
        call_id=second_reason["call_id"],
        minutes=12,
        requested_at=now_box[0],
        reason="second redeploy",
    )
    ledger = WaitLedger.from_history(mgr.get_task_info(TASK_ID))
    assert ledger.total_minutes == 20
    assert ledger.entry_for(first_reason["call_id"]) is not None
    assert ledger.entry_for(second_reason["call_id"]) is not None


@pytest.mark.asyncio
async def test_wait_resumed_with_non_timer_answer_returns_ended_early_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now_box = [BASE_TIME + timedelta(minutes=3)]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])

    mgr = _make_manager(tmp_path / "manager.db")
    _record_started_wait(mgr, TASK_ID, tool_use_id="tu-early", minutes=10, requested_at=BASE_TIME)
    agent = _make_agent(mgr)
    state = _InterruptState()
    ctx = _make_tool_context("tu-early", interrupt_state=state)
    interrupt_id = ctx._interrupt_id(TIMED_WAIT_KIND)

    state.interrupts[interrupt_id] = Interrupt(interrupt_id, TIMED_WAIT_KIND, reason={}, response="human message")

    set_agent_ref(agent)  # type: ignore[arg-type]
    try:
        with task_work(TASK_ID), caplog.at_level(logging.WARNING):
            raw = await wait(10, "Portainer redeploy", ctx)
    finally:
        set_agent_ref(None)

    result = json.loads(raw)
    assert result["status"] == "ended_early"
    assert result["waited_minutes"] == 3.0
    assert "event=timed_wait_answered_by_other" in caplog.text


@pytest.mark.asyncio
async def test_wait_unexpected_store_error_logs_and_returns_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C18: unexpected failure inside wait logs structured identifiers and returns refused."""

    mgr = _make_manager(tmp_path / "manager.db")

    def boom(_task_id: str) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(mgr, "get_task_info", boom)
    agent = _make_agent(mgr)
    set_agent_ref(agent)  # type: ignore[arg-type]
    try:
        with task_work(TASK_ID), caplog.at_level(logging.WARNING):
            result = json.loads(await wait(5, "redeploy", _make_tool_context("tu-fail")))
    finally:
        set_agent_ref(None)

    assert result["status"] == "refused"
    assert "nothing was charged" in result["reason"]
    assert "event=timed_wait_failed" in caplog.text
    assert f"task_id={TASK_ID}" in caplog.text
    assert "tool=wait" in caplog.text
    assert "tool_use_id=tu-fail" in caplog.text
    assert "error_type=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_wait_and_long_wait_share_persisted_budget_across_restart_and_auth_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: BASE_TIME)
    db_path = tmp_path / "manager.db"
    mgr = _make_manager(db_path)
    _record_started_wait(mgr, TASK_ID, tool_use_id="tu-prior", minutes=12, tool=WAIT_TOOL_NAME)

    # Fresh TaskManager simulates restart.
    restarted = _reload_manager(db_path)
    agent_on = _make_agent(restarted, auth_enabled=True, tools=(wait,))

    set_agent_ref(agent_on)  # type: ignore[arg-type]
    try:
        with task_work(TASK_ID):
            # 12 + 9 = 21 > 20 -> refused with long_wait + request_access guidance.
            refused = json.loads(await wait(9, "too long", _make_tool_context("tu-over")))
            assert refused["status"] == "refused"
            assert "12 minutes already used" in refused["reason"]
            assert "8 minutes left" in refused["reason"]
            assert "long_wait" in refused["reason"]
            assert "request_access" in refused["reason"]

            # 12 + 8 = 20 -> allowed.
            with pytest.raises(InterruptException):
                await wait(8, "fits exactly", _make_tool_context("tu-fit"))

            # long_wait counts the 12 minutes already used by wait.
            refused_long = json.loads(await long_wait(1429, "over 24h", _make_tool_context("tu-long-over")))
            assert refused_long["status"] == "refused"
            assert "12 minutes already used" in refused_long["reason"]
            assert "1428 minutes left" in refused_long["reason"]

            with pytest.raises(InterruptException):
                await long_wait(1428, "fits 24h", _make_tool_context("tu-long-fit"))
    finally:
        set_agent_ref(None)

    # C17: auth-on agent that already holds long_wait in its active Strands registry gets direct guidance.
    agent_on_with_long = _make_agent(restarted, auth_enabled=True, tools=(wait,))
    agent_on_with_long._strands_agent = SimpleNamespace(
        tool_registry=SimpleNamespace(registry={"wait": wait}, dynamic_tools={"long_wait": long_wait})
    )
    set_agent_ref(agent_on_with_long)  # type: ignore[arg-type]
    try:
        with task_work(TASK_ID):
            refused_held = json.loads(await wait(9, "over", _make_tool_context("tu-on-held")))
            assert "Use long_wait instead" in refused_held["reason"]
            assert "request_access" not in refused_held["reason"]
    finally:
        set_agent_ref(None)

    # C04: auth-off refusal text depends on whether the agent holds long_wait.
    agent_off_no_long = _make_agent(restarted, auth_enabled=False, tools=(wait,))
    set_agent_ref(agent_off_no_long)  # type: ignore[arg-type]
    try:
        with task_work(TASK_ID):
            refused_no_long = json.loads(await wait(9, "over", _make_tool_context("tu-off-1")))
            assert "default_tools" in refused_no_long["reason"]
            assert LONG_WAIT_TOOL_ID in refused_no_long["reason"]
            assert "request_access" not in refused_no_long["reason"]
    finally:
        set_agent_ref(None)

    agent_off_with_long = _make_agent(restarted, auth_enabled=False, tools=(wait, long_wait))
    set_agent_ref(agent_off_with_long)  # type: ignore[arg-type]
    try:
        with task_work(TASK_ID):
            refused_with_long = json.loads(await wait(9, "over", _make_tool_context("tu-off-2")))
            assert "Use long_wait instead" in refused_with_long["reason"]
            assert "request_access" not in refused_with_long["reason"]
    finally:
        set_agent_ref(None)


# -- A5 + C06: recurring schedule next-run check with real ScheduleStore --------------------


def _scheduler_signed_meta(schedule_id: str) -> MessageMeta:
    import os

    now = datetime.now(timezone.utc)
    signer = ContextSigner.from_value(ISSUER_SCHEDULER, os.environ[ENV_SCHEDULER_SIGNING_KEY])
    signed = signer.sign(
        TaskContextV1(
            context_id=f"ctx-sched-{schedule_id}",
            issuer=ISSUER_SCHEDULER,
            team_id=TEST_TEAM,
            person_id="alice",
            person_roles=("member",),
            schedule_id=schedule_id,
            issued_at_utc=now,
            expires_at_utc=now + timedelta(hours=1),
        )
    )
    return MessageMeta(gateway_context=GatewayContext(task_context=signed))


def _gateway_signed_meta(schedule_id: str | None = None) -> MessageMeta:
    now = datetime.now(timezone.utc)
    signed = context_signer().sign(
        TaskContextV1(
            context_id=f"ctx-gw-{schedule_id or 'chat'}",
            issuer=ISSUER_GATEWAY,
            team_id=TEST_TEAM,
            person_id="alice",
            person_roles=("member",),
            schedule_id=schedule_id,
            issued_at_utc=now,
            expires_at_utc=now + timedelta(hours=1),
        )
    )
    return MessageMeta(gateway_context=GatewayContext(task_context=signed))


def _claim_and_advance(store: ScheduleStore, schedule_id: str) -> ScheduledMessage:
    claimed = store.claim_next_due(
        claimer_agent_id=AGENT_ID,
        claimer_named_agent_type="Worker",
        claimer_team_id=TEST_TEAM,
        allow_unscoped=True,
    )
    assert claimed is not None and claimed["schedule_id"] == schedule_id
    outcome = store.update_after_fire(
        schedule_id,
        claim_token=claimed["claim_token"],
        claimer_agent_id=AGENT_ID,
        due_at=claimed["next_fire_at"],
    )
    assert outcome.value in {"advanced", "finished", "finishing"}
    loaded = store.read_scheduled_message(schedule_id)
    assert loaded is not None
    return loaded


@pytest.mark.asyncio
async def test_recurring_interval_and_cron_schedules_refuse_overlapping_wait_via_real_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_scheduler_key(monkeypatch)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now)
    try:
        db_path = tmp_path / "team.db"
        store = ScheduleStore(db_path=str(db_path))

        # 1. Recurring interval schedule (every 10 minutes), claimed and advanced normally.
        interval_sched = ScheduledMessage(
            schedule_id="sched-interval",
            management_class=ScheduleManagementClass.USER,
            payload=Task(task_id="t-int", task_type=MessageMetaType.REQUEST, problem_statement="interval job"),
            interval_ms=10 * 60 * 1000,
            repeat_count=None,
            next_fire_at=(now - timedelta(seconds=5)).isoformat(),
            enabled=True,
        )
        store.add(interval_sched)
        advanced_interval = _claim_and_advance(store, "sched-interval")
        assert advanced_interval.next_fire_at is not None

        mgr = _make_manager(db_path, task_id="task-int", meta=_scheduler_signed_meta("sched-interval"))
        agent = _make_agent(mgr, schedule_store=store)

        set_agent_ref(agent)  # type: ignore[arg-type]
        try:
            with task_work("task-int"):
                # 11 minutes is definitely >= next_fire_at (~10 minutes away).
                refused = json.loads(await wait(11, "too long for interval", _make_tool_context("tu-int-over")))
                assert refused["status"] == "refused"
                assert advanced_interval.next_fire_at in refused["reason"]
                assert "next run" in refused["reason"]

                # 5 minutes fits well before the next fire.
                with pytest.raises(InterruptException):
                    await wait(5, "fits before next interval", _make_tool_context("tu-int-ok"))
        finally:
            set_agent_ref(None)

        # 2. Recurring cron schedule (every 15 minutes), claimed and advanced normally.
        cron_sched = ScheduledMessage(
            schedule_id="sched-cron",
            management_class=ScheduleManagementClass.USER,
            payload=Task(task_id="t-cron", task_type=MessageMetaType.REQUEST, problem_statement="cron job"),
            cron_expression="*/15 * * * *",
            cron_timezone="UTC",
            repeat_count=None,
            next_fire_at=(now - timedelta(seconds=5)).isoformat(),
            enabled=True,
        )
        store.add(cron_sched)
        advanced_cron = _claim_and_advance(store, "sched-cron")
        assert advanced_cron.next_fire_at is not None

        mgr.add_task(
            Task(task_id="task-cron", task_type=MessageMetaType.REQUEST, problem_statement="cron"),
            MessageHeader(from_agent_id="scheduler"),
            _scheduler_signed_meta("sched-cron"),
            owner_person_id=None,
        )
        set_agent_ref(agent)  # type: ignore[arg-type]
        try:
            with task_work("task-cron"):
                refused_cron = json.loads(await wait(16, "past next cron fire", _make_tool_context("tu-cron-over")))
                assert refused_cron["status"] == "refused"
                assert advanced_cron.next_fire_at in refused_cron["reason"]
        finally:
            set_agent_ref(None)
    finally:
        forget_task_context_keys()


@pytest.mark.asyncio
async def test_one_time_schedule_chat_and_missing_or_unreadable_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use_scheduler_key(monkeypatch)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now)
    try:
        db_path = tmp_path / "team.db"
        store = ScheduleStore(db_path=str(db_path))

        # One-time schedule kept in store.
        one_time = ScheduledMessage(
            schedule_id="sched-one-time",
            management_class=ScheduleManagementClass.USER,
            payload=Task(task_id="t-once", task_type=MessageMetaType.REQUEST, problem_statement="one-time"),
            repeat_count=1,
            next_fire_at=(now - timedelta(seconds=5)).isoformat(),
            enabled=True,
        )
        store.add(one_time)

        mgr = _make_manager(db_path, task_id="task-once", meta=_scheduler_signed_meta("sched-one-time"))
        mgr.add_task(
            Task(task_id="task-missing", task_type=MessageMetaType.REQUEST, problem_statement="deleted"),
            MessageHeader(from_agent_id="scheduler"),
            _scheduler_signed_meta("sched-does-not-exist"),
            owner_person_id=None,
        )
        mgr.add_task(
            Task(task_id="task-chat", task_type=MessageMetaType.REQUEST, problem_statement="chat"),
            MessageHeader(from_agent_id="gateway"),
            _gateway_signed_meta(schedule_id=None),
            owner_person_id=None,
        )
        mgr.add_task(
            Task(task_id="task-forged-sched", task_type=MessageMetaType.REQUEST, problem_statement="chat"),
            MessageHeader(from_agent_id="gateway"),
            _gateway_signed_meta(schedule_id="sched-one-time"),
            owner_person_id=None,
        )

        agent = _make_agent(mgr, schedule_store=store)
        set_agent_ref(agent)  # type: ignore[arg-type]
        try:
            # One-time schedule allows waiting.
            with task_work("task-once"), pytest.raises(InterruptException):
                await wait(15, "one-time wait", _make_tool_context("tu-once"))

            # Chat task allows waiting.
            with task_work("task-chat"), pytest.raises(InterruptException):
                await wait(15, "chat wait", _make_tool_context("tu-chat"))

            # Non-scheduler signed context naming a schedule_id is ignored.
            with task_work("task-forged-sched"), caplog.at_level(logging.WARNING), pytest.raises(InterruptException):
                await wait(15, "ignored schedule id", _make_tool_context("tu-forged"))
            assert "event=schedule_id_from_non_scheduler_ignored" in caplog.text

            # Missing schedule row allows waiting and logs schedule_id at INFO (C06).
            caplog.clear()
            with task_work("task-missing"), caplog.at_level(logging.INFO), pytest.raises(InterruptException):
                await wait(10, "deleted schedule", _make_tool_context("tu-missing"))
            assert "event=timed_wait_schedule_missing" in caplog.text
            assert "schedule_id=sched-does-not-exist" in caplog.text

            # Schedule read error refuses waiting with event=timed_wait_refused reason=schedule_unreadable (C06).
            def failing_read(_schedule_id: str) -> tuple[Any, bool]:
                raise RuntimeError("sqlite locked")

            monkeypatch.setattr(store, "read_next_fire_schedule", failing_read)
            caplog.clear()
            with task_work("task-once"), caplog.at_level(logging.WARNING):
                refused_err = json.loads(await wait(10, "db error", _make_tool_context("tu-err")))
            assert refused_err["status"] == "refused"
            assert "sched-one-time" in refused_err["reason"]
            assert "event=timed_wait_refused" in caplog.text
            assert "reason=schedule_unreadable" in caplog.text
        finally:
            set_agent_ref(None)
    finally:
        forget_task_context_keys()


def test_real_final_run_allows_wait_and_first_of_two_interval_runs_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C16: verify finishing flag on real claimed/admitted schedule runs without monkeypatching is_finishing."""

    from tests.scheduled_run_support import build_world
    from tests.test_schedule_final_runs import start_final_run

    try:
        # 1. Final run of a schedule with no interval and no repeat_count -> allowed.
        world_final = build_world(tmp_path / "final", monkeypatch)
        run_final = start_final_run(world_final, interval=None, count=None)
        agent_final = _make_agent(world_final.tasks, schedule_store=world_final.store)
        agent_final.team_id = world_final.worker.team_id

        async def _call_final() -> None:
            set_agent_ref(agent_final)  # type: ignore[arg-type]
            try:
                with task_work(run_final.task.task_id), pytest.raises(InterruptException):
                    await wait(15, "final run wait", _make_tool_context("tu-real-final"))
            finally:
                set_agent_ref(None)

        asyncio.run(_call_final())

        # 2. First of two 10-minute interval runs -> 11-minute wait refused, 5-minute wait allowed.
        world_two = build_world(tmp_path / "two", monkeypatch)
        run_first = start_final_run(world_two, interval=10 * 60 * 1000, count=2)
        agent_two = _make_agent(world_two.tasks, schedule_store=world_two.store)
        agent_two.team_id = world_two.worker.team_id

        async def _call_first_of_two() -> dict[str, Any]:
            set_agent_ref(agent_two)  # type: ignore[arg-type]
            try:
                with task_work(run_first.task.task_id):
                    refused = json.loads(await wait(11, "past second run", _make_tool_context("tu-two-over")))
                    with pytest.raises(InterruptException):
                        await wait(5, "fits before second run", _make_tool_context("tu-two-ok"))
                    return refused
            finally:
                set_agent_ref(None)

        refused_first = asyncio.run(_call_first_of_two())
        assert refused_first["status"] == "refused"
        assert "next run" in refused_first["reason"]
    finally:
        forget_task_context_keys()


@pytest.mark.asyncio
async def test_bubbled_child_wait_does_not_count_against_parent_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delegated subtask's bubbled timed_wait_started entry must not consume its parent's wait budget."""

    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: BASE_TIME)
    mgr = _make_manager(tmp_path / "manager.db", task_id="parent-task")
    subtask = Task(
        task_id="child-task",
        parent_task_id="parent-task",
        task_type=MessageMetaType.REQUEST,
        problem_statement="worker step",
    )
    assert (
        mgr.add_subtask(
            "parent-task",
            subtask,
            MessageHeader(from_agent_id="worker"),
            MessageMeta(),
        )
        is not None
    )
    _record_started_wait(mgr, "child-task", tool_use_id="tu-child", call_id="call-child", minutes=15)
    child_info = mgr.get_task_info("child-task")
    assert child_info is not None
    child_info.task.answer = "child done"
    mgr.update_main_task_state("child-task", ProgressState.COMPLETED, BASE_TIME.isoformat())
    mgr.bubble_subtask_result("parent-task", "child-task")

    parent_info = mgr.get_task_info("parent-task")
    assert parent_info is not None
    bubbled_waits = [
        entry
        for entry in parent_info.task.execution_history
        if entry.details
        and entry.details.get("event") == TIMED_WAIT_STARTED_EVENT
        and entry.details.get(BUBBLED_ENTRY_ID_KEY)
    ]
    assert len(bubbled_waits) == 1
    assert WaitLedger.from_history(parent_info).total_minutes == 0

    # Parent still has its full 20-minute without-approval wait budget.
    agent = _make_agent(mgr)
    set_agent_ref(agent)  # type: ignore[arg-type]
    try:
        with task_work("parent-task"), pytest.raises(InterruptException):
            await wait(10, "parent waits after child finished", _make_tool_context("tu-parent-wait"))
    finally:
        set_agent_ref(None)


@pytest.mark.asyncio
async def test_unverifiable_task_identity_refuses_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When signed identity material is present but cannot be verified, refuse the wait fail-safe."""

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now)

    # 1. Context signed with a key unknown to the runtime verifier (chat task with unverifiable gateway context).
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    unknown_signer = ContextSigner(ISSUER_GATEWAY, "unknown-key", Ed25519PrivateKey.generate())
    bad_signed = unknown_signer.sign(
        TaskContextV1(
            context_id="ctx-bad-sig",
            issuer=ISSUER_GATEWAY,
            team_id=TEST_TEAM,
            person_id="alice",
            person_roles=("member",),
            issued_at_utc=now,
            expires_at_utc=now + timedelta(hours=1),
        )
    )
    bad_meta = MessageMeta(gateway_context=GatewayContext(task_context=bad_signed))
    mgr = _make_manager(tmp_path / "manager.db", task_id="task-bad-id", meta=bad_meta)
    agent = _make_agent(mgr, schedule_store=ScheduleStore(db_path=str(tmp_path / "team.db")))

    use_scheduler_key(monkeypatch)
    try:
        set_agent_ref(agent)  # type: ignore[arg-type]
        try:
            with task_work("task-bad-id"), caplog.at_level(logging.WARNING):
                refused = json.loads(await wait(10, "wait with bad context", _make_tool_context("tu-bad-id")))
            assert refused["status"] == "refused"
            assert "signed identity could not be verified" in refused["reason"].lower()
            assert "schedule identity" not in refused["reason"].lower()
            assert "event=timed_wait_refused" in caplog.text
            assert "reason=identity_unverifiable" in caplog.text

            # 2. Verifier raises RuntimeError while identity material is present.
            from custom_agents import access_runtime

            def broken_verifier(_agent: Any, _access: Any = None) -> Any:
                raise RuntimeError("task storage unavailable")

            monkeypatch.setattr(access_runtime, "verified_task_context", broken_verifier)
            caplog.clear()
            with task_work("task-bad-id"), caplog.at_level(logging.WARNING):
                refused_err = json.loads(await wait(10, "wait with broken verifier", _make_tool_context("tu-err-id")))
            assert refused_err["status"] == "refused"
            assert "signed identity could not be verified" in refused_err["reason"].lower()
            assert "schedule identity" not in refused_err["reason"].lower()
            assert "event=timed_wait_refused" in caplog.text
            assert "reason=identity_unverifiable" in caplog.text
        finally:
            set_agent_ref(None)
    finally:
        forget_task_context_keys()
