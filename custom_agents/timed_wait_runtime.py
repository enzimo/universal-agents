"""Runtime lifecycle for timed task pauses (``wait`` and ``long_wait``).

Both tools pause the active turn with a local Strands interrupt
(``TIMED_WAIT_KIND``). This module owns the trusted runtime side:

- **Begin** (``decide_timed_wait`` / ``write_timed_wait`` / ``stage_timed_wait``
  and ``finish_timed_wait``, called around the ``WAITING`` transition in
  ``task_steering_runtime.send_interrupt_request_for_result``): under a
  per-task lock, ``decide_timed_wait`` defers the wait (no charge, no record)
  if another wait is already active on the task (same-step ``request_access``,
  ``set_up_tools``, upstream question, or an earlier ``wait``). When starting a
  new wait, it computes ``requested_at`` and ``resume_at`` from
  ``timed_waits.utc_now`` so the blocker record and ``timed_wait_started``
  history entry are the single source of truth (including for a wait deferred
  behind an earlier pause whose reused Strands ``Interrupt`` object still holds
  its first call's payload) and re-checks the recurring-schedule next-run rule
  against that ``resume_at``. ``write_timed_wait`` writes the
  ``timed_wait_started`` history entry (the budget charge) and saves the
  blocker record *before* ``ProgressState.WAITING`` is persisted, so a crash
  after pausing can never leave a ``WAITING`` task without its blocker. A
  re-raise of an already-charged ``call_id`` adds no second charge and restores
  an unmarked blocker with the original times. After ``WAITING`` is saved,
  ``finish_timed_wait`` wakes the scheduler loop (or, if the wait was refused
  at start time, answers the paused turn immediately without charging).
- **Resume** (``resume_due_timed_waits``, run on every scheduler pass, at
  startup recovery, and during lease-loss wind-down): for each non-terminal
  task of this agent whose timed-wait record is due, acquires the task lock,
  re-reads the record, and either hands the timer answer to the live paused
  turn (``paused_wait_resume.hand_answer_to_paused_turn`` with
  ``replace_pending=False`` so an already-pending answer is never clobbered) or
  resumes the task from a durable note (``paused_wait_resume.resume_from_note``)
  after a restart, dropped turn, or takeover.
- **Answers a paused turn cannot take** (``resume_without_paused_turn``): when
  a held timer answer finds its paused turn gone or unconvertible, drops the
  paused turn, removes the held-answer mark, and resumes the task from a note.
- **Scheduler wake cadence** (``seconds_until_next_resume``): reports the delay
  until the earliest unheld timed wait so the scheduler loop wakes near the
  resume time without busy-looping while a held answer waits on a busy lane,
  and backs off overdue unresumable waits up to ``MAX_SCHEDULER_SLEEP_SECONDS``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Literal

from access_control.identity import ISSUER_SCHEDULER
from custom_agents import timed_waits as timed_waits_rules
from custom_agents.liveness_observation import MAX_SCHEDULER_SLEEP_SECONDS
from custom_agents.paused_wait_resume import (
    WaitLogSpec,
    append_wait_history,
    drop_paused_turn,
    hand_answer_to_paused_turn,
    paused_turn_waits,
    resume_from_note,
)
from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.task_human_interaction_blockers import (
    TIMED_WAIT_BLOCKER,
    answer_held,
    get_human_interaction_blocker,
    is_timed_wait_blocker,
    record_human_interaction_blocker,
    remove_answer_mark,
)
from custom_agents.timed_waits import (
    TIMED_WAIT_ENDED_EVENT,
    TIMED_WAIT_REFUSED_RESPONSE_KIND,
    TIMED_WAIT_RESPONSE_KIND,
    TIMED_WAIT_STARTED_EVENT,
    WAIT_TOOL_NAME,
    WaitLedger,
    is_due,
    next_run_refusal,
    resume_at_of,
    resume_note,
    timed_wait_record,
    waited_minutes,
)
from custom_messages.contracts.task_v1 import TaskSteering
from custom_messages.enums import ProgressState, TaskSteeringStatus, TaskSteeringType
from custom_tools.runtime_context import active_schedule_run, active_signed_task_context

logger = logging.getLogger(__name__)

_TIMED_WAIT_LOG = WaitLogSpec(
    label="timed-wait",
    dropped_event="timed_wait_paused_turn_dropped",
    restored_event="timed_wait_record_restored",
    restore_failed_event="timed_wait_record_restore_failed",
)


# -- locking & scheduler wake ----------------------------------------------------------------


def _lock(agent: Any, task_id: str) -> asyncio.Lock:
    """One lock per task so begin, due-resume, and fallback paths never race on a record."""

    locks: dict[str, asyncio.Lock] | None = getattr(agent, "_timed_wait_locks", None)
    if locks is None:
        locks = {}
        agent._timed_wait_locks = locks
    return locks.setdefault(task_id, asyncio.Lock())


def _wake_scheduler(agent: Any) -> None:
    wake = getattr(agent, "_scheduler_wake", None)
    if wake is not None:
        wake.set()


def _warned_anomalies(agent: Any) -> set[tuple[str, str, str, str]]:
    """Per-record anomaly keys already logged at WARNING so repeated passes log at DEBUG."""

    warned: set[tuple[str, str, str, str]] | None = getattr(agent, "_timed_wait_warned", None)
    if warned is None:
        warned = set()
        agent._timed_wait_warned = warned
    return warned


def _anomaly_log_level(
    agent: Any,
    *,
    task_id: str,
    interrupt_id: str,
    resume_at: str,
    anomaly: str,
) -> int:
    """WARNING the first time an anomaly is seen for one blocker record, DEBUG afterwards."""

    warned = _warned_anomalies(agent)
    key = (task_id, interrupt_id, resume_at, anomaly)
    if key in warned:
        return logging.DEBUG
    warned.add(key)
    return logging.WARNING


def _prune_warned_anomalies(agent: Any, active_records: set[tuple[str, str, str]]) -> None:
    """Forget deduplication state for records that resumed, finished, or changed."""

    warned: set[tuple[str, str, str, str]] | None = getattr(agent, "_timed_wait_warned", None)
    if warned:
        warned.intersection_update({key for key in warned if key[:3] in active_records})


# -- recurring-schedule next-run check (shared by tools and begin_timed_wait) ----------------


def _verified_scheduler_schedule_id(
    agent: Any, task_id: str, *, meta: Any = None
) -> tuple[str | None, str | None]:
    """Return ``(schedule_id, refusal)`` for the active task or ``meta``.

    When signed identity material is present but cannot be verified, returns a
    fail-safe refusal with a structured warning instead of skipping the
    recurring-schedule next-run guard.
    """

    from custom_agents.access_runtime import resolve_meta_identity, verified_task_context

    has_active_identity = (
        active_signed_task_context(agent) is not None or active_schedule_run(agent) is not None
    )
    has_meta_identity = (
        getattr(getattr(meta, "gateway_context", None), "task_context", None) is not None
        or getattr(getattr(meta, "gateway_context", None), "schedule_run", None) is not None
    )
    if not has_active_identity and not has_meta_identity:
        return None, None

    context = None
    error_detail = "verification_failed"
    if has_active_identity:
        try:
            context = verified_task_context(agent)
        except RuntimeError as exc:
            error_detail = type(exc).__name__
            if not has_meta_identity:
                logger.warning(
                    "Timed wait refused because the active task's schedule identity could not be verified. "
                    "event=timed_wait_refused reason=identity_unverifiable task_id=%s error=%s",
                    task_id,
                    exc,
                )
                return (
                    None,
                    "Could not verify this task's schedule identity, so this wait was refused to avoid "
                    "overlapping a recurring schedule's next run.",
                )
    if context is None and has_meta_identity:
        context = resolve_meta_identity(agent, meta)
    if context is None:
        logger.warning(
            "Timed wait refused because the task's identity could not be verified. "
            "event=timed_wait_refused reason=identity_unverifiable task_id=%s detail=%s",
            task_id,
            error_detail,
        )
        return (
            None,
            "Could not verify this task's schedule identity, so this wait was refused to avoid "
            "overlapping a recurring schedule's next run.",
        )
    if not context.schedule_id:
        return None, None
    if context.issuer != ISSUER_SCHEDULER:
        logger.warning(
            "Ignoring schedule_id from a non-scheduler task context during timed wait check. "
            "event=schedule_id_from_non_scheduler_ignored issuer=%s schedule_id=%s task_id=%s",
            context.issuer,
            context.schedule_id,
            task_id,
        )
        return None, None
    return context.schedule_id, None


def _schedule_store(agent: Any) -> Any | None:
    store = getattr(agent, "_schedule_store", None)
    if store is None and callable(getattr(agent, "_ensure_schedule_store", None)):
        store = agent._ensure_schedule_store()
    return store


async def check_schedule_next_run(
    agent: Any,
    task_id: str,
    *,
    now: datetime,
    resume_at: datetime,
    meta: Any = None,
) -> str | None:
    """Refuse a wait in a recurring scheduled run that would reach or pass its next fire time."""

    schedule_id, identity_refusal = _verified_scheduler_schedule_id(agent, task_id, meta=meta)
    if identity_refusal is not None:
        return identity_refusal
    if not schedule_id:
        return None
    store = _schedule_store(agent)
    if store is None:
        logger.warning(
            "Timed wait refused because this agent has no schedule store to read the next run time. "
            "event=timed_wait_refused reason=schedule_unreadable task_id=%s schedule_id=%s",
            task_id,
            schedule_id,
        )
        return (
            f"Could not check schedule {schedule_id}'s next run time (no schedule store), "
            "so this wait was refused to avoid overlapping the next run."
        )
    try:
        schedule, finishing = await asyncio.to_thread(store.read_next_fire_schedule, schedule_id)
    except Exception as exc:  # noqa: BLE001 - fail safe on any storage or parse error
        logger.warning(
            "Timed wait refused because the schedule could not be read. "
            "event=timed_wait_refused reason=schedule_unreadable task_id=%s schedule_id=%s error_type=%s",
            task_id,
            schedule_id,
            type(exc).__name__,
        )
        return (
            f"Could not check schedule {schedule_id}'s next run time ({type(exc).__name__}), "
            "so this wait was refused to avoid overlapping the next run."
        )
    if schedule is None:
        logger.info(
            "Scheduled run's schedule row was not found; allowing timed wait. "
            "event=timed_wait_schedule_missing task_id=%s schedule_id=%s",
            task_id,
            schedule_id,
        )
        return None
    refusal = next_run_refusal(schedule, resume_at, now=now, finishing=finishing)
    if refusal is not None:
        logger.info(
            "Timed wait refused because it would overlap the schedule's next run. "
            "event=timed_wait_refused reason=next_run_overlap task_id=%s schedule_id=%s "
            "resume_at=%s next_fire_at=%s",
            task_id,
            schedule_id,
            resume_at.isoformat(),
            schedule.next_fire_at,
        )
    return refusal


# -- begin -----------------------------------------------------------------------------------


async def _refuse_at_start(agent: Any, *, task_id: str, interrupt_id: str, refusal: str) -> None:
    """End a timed wait that no longer fits at start time without charging the task's budget."""

    logger.info(
        "Timed wait refused at start time; ending pause without charging budget. "
        "event=timed_wait_refused reason=start_next_run_overlap task_id=%s interrupt_id=%s agent_id=%s",
        task_id,
        interrupt_id,
        getattr(agent, "id", "-"),
    )
    if paused_turn_waits(agent, task_id, interrupt_id):
        if task_id in agent._pending_interrupt_responses:
            logger.warning(
                "Could not hand a start-time wait refusal because another interrupt answer is pending. "
                "event=timed_wait_refusal_deferred task_id=%s interrupt_id=%s",
                task_id,
                interrupt_id,
            )
            return
        agent._pending_interrupt_responses[task_id] = TaskSteering(
            steering_id=f"timed_wait_refused_{interrupt_id}",
            steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
            target_task_id=task_id,
            target_agent_id=agent.id,
            interrupt_responses=[
                {
                    "interrupt_id": interrupt_id,
                    "response": {
                        "kind": TIMED_WAIT_REFUSED_RESPONSE_KIND,
                        "interrupt_id": interrupt_id,
                        "reason": refusal,
                    },
                }
            ],
            status=TaskSteeringStatus.PENDING,
            idempotency_key=f"timed_wait_refused:{interrupt_id}",
        )
        schedule_pending_work(agent)
        return

    # Defensive fallback if no live session exists: re-queue with a note so the task is never stranded.
    manager = agent.task_manager
    run_ops = manager.operations_runtime.run
    await run_ops(
        manager.append_runtime_resume_directive,
        task_id,
        f"Your requested timed wait was refused at start time ({refusal}). Continue the task without waiting.",
    )
    await run_ops(
        manager.update_main_task_state,
        task_id,
        ProgressState.ACCEPTED,
        timed_waits_rules.utc_now().isoformat(),
    )
    await run_ops(manager.enqueue_task, task_id, front=True)
    schedule_pending_work(agent)


@dataclass(frozen=True)
class TimedWaitDecision:
    """Decision for one timed-wait interrupt, separated from the write and post-WAITING effects."""

    action: Literal["record", "restore", "defer", "refuse", "skip"]
    task_id: str
    interrupt_id: str
    record: dict[str, Any] | None = None
    history_details: dict[str, Any] | None = None
    refusal: str | None = None


async def decide_timed_wait(
    agent: Any,
    *,
    task_id: str,
    interrupt_id: str,
    reason: dict[str, Any],
    msg_meta: Any,
    defer_for_other_wait: bool = False,
    deferred_by: str = "upstream_interrupt",
) -> TimedWaitDecision:
    """Decide whether to record, restore, defer, or refuse a timed wait without mutating state.

    Caller holds ``_lock(agent, task_id)``.
    """

    if defer_for_other_wait:
        logger.info(
            "Deferring timed wait because another interrupt is active in the same step. "
            "event=timed_wait_deferred task_id=%s interrupt_id=%s active_kind=%s",
            task_id,
            interrupt_id,
            deferred_by,
        )
        return TimedWaitDecision(action="defer", task_id=task_id, interrupt_id=interrupt_id)

    manager = agent.task_manager
    run_ops = manager.operations_runtime.run
    existing_blocker = await run_ops(partial(get_human_interaction_blocker, manager), task_id)
    if existing_blocker is not None and not is_timed_wait_blocker(existing_blocker, interrupt_id):
        logger.info(
            "Deferring timed wait because another wait is already active on this task. "
            "event=timed_wait_deferred task_id=%s interrupt_id=%s active_kind=%s",
            task_id,
            interrupt_id,
            existing_blocker.get("kind"),
        )
        return TimedWaitDecision(action="defer", task_id=task_id, interrupt_id=interrupt_id)

    task_info = await run_ops(manager.get_task_info, task_id)
    if task_info is None or await run_ops(manager.is_terminal_state, task_info.state):
        return TimedWaitDecision(action="skip", task_id=task_id, interrupt_id=interrupt_id)

    tool = str(reason.get("tool") or WAIT_TOOL_NAME)
    tool_use_id = str(reason.get("tool_use_id") or "").strip()
    call_id = str(reason.get("call_id") or tool_use_id or interrupt_id).strip()
    raw_minutes = reason.get("minutes")
    minutes = raw_minutes if isinstance(raw_minutes, int) and not isinstance(raw_minutes, bool) else 0
    wait_reason = str(reason.get("reason") or "").strip() or "timed wait"

    ledger = WaitLedger.from_history(task_info)
    existing_entry = ledger.entry_for(call_id)
    if existing_entry is not None:
        # Re-raise of an already-charged call: no second charge, keep original times, clear held mark.
        record = timed_wait_record(
            interrupt_id=interrupt_id,
            call_id=existing_entry.call_id,
            tool=existing_entry.tool,
            tool_use_id=existing_entry.tool_use_id,
            minutes=existing_entry.minutes,
            reason=existing_entry.reason,
            requested_at=existing_entry.requested_at,
            resume_at=existing_entry.resume_at,
        )
        return TimedWaitDecision(action="restore", task_id=task_id, interrupt_id=interrupt_id, record=record)

    if minutes < 1:
        logger.warning(
            "Timed wait interrupt had invalid minutes; refusing at start. "
            "event=timed_wait_refused reason=invalid_minutes task_id=%s interrupt_id=%s minutes=%r",
            task_id,
            interrupt_id,
            raw_minutes,
        )
        return TimedWaitDecision(
            action="refuse",
            task_id=task_id,
            interrupt_id=interrupt_id,
            refusal="minutes must be a whole number of at least 1.",
        )

    # The runtime clock is the single source of truth for start/resume times when recording a new wait.
    now = timed_waits_rules.utc_now()
    resume_at = now + timedelta(minutes=minutes)
    refusal = await check_schedule_next_run(agent, task_id, now=now, resume_at=resume_at, meta=msg_meta)
    if refusal is not None:
        return TimedWaitDecision(action="refuse", task_id=task_id, interrupt_id=interrupt_id, refusal=refusal)

    requested_at_iso = now.isoformat()
    resume_at_iso = resume_at.isoformat()
    record = timed_wait_record(
        interrupt_id=interrupt_id,
        call_id=call_id,
        tool=tool,
        tool_use_id=tool_use_id,
        minutes=minutes,
        reason=wait_reason,
        requested_at=requested_at_iso,
        resume_at=resume_at_iso,
    )
    history_details = {
        "event": TIMED_WAIT_STARTED_EVENT,
        "call_id": call_id,
        "tool": tool,
        "tool_use_id": tool_use_id,
        "interrupt_id": interrupt_id,
        "minutes": minutes,
        "reason": wait_reason,
        "requested_at": requested_at_iso,
        "resume_at": resume_at_iso,
    }
    return TimedWaitDecision(
        action="record",
        task_id=task_id,
        interrupt_id=interrupt_id,
        record=record,
        history_details=history_details,
    )


async def write_timed_wait(agent: Any, decision: TimedWaitDecision) -> bool:
    """Persist the charge and blocker record for a decided timed wait (before WAITING is saved).

    Caller holds ``_lock(agent, decision.task_id)``. Returns True when a record was written.
    """

    if decision.action not in {"record", "restore"} or decision.record is None:
        return False
    manager = agent.task_manager
    run_ops = manager.operations_runtime.run
    if decision.action == "record":
        assert decision.history_details is not None
        # Charge (history) first, then save the blocker record.
        await append_wait_history(
            agent,
            decision.task_id,
            decision.record["summary"],
            decision.history_details,
        )
        await run_ops(partial(record_human_interaction_blocker, manager), decision.task_id, decision.record)
        logger.info(
            "A task paused on a timed wait. event=timed_wait_started task_id=%s interrupt_id=%s "
            "tool=%s minutes=%s resume_at=%s agent_id=%s",
            decision.task_id,
            decision.interrupt_id,
            decision.record.get("tool"),
            decision.record.get("minutes"),
            decision.record.get("resume_at"),
            getattr(agent, "id", "-"),
        )
        return True
    await run_ops(partial(record_human_interaction_blocker, manager), decision.task_id, decision.record)
    return True


async def stage_timed_wait(
    agent: Any,
    *,
    task_id: str,
    interrupt_id: str,
    reason: dict[str, Any],
    msg_meta: Any,
    defer_for_other_wait: bool = False,
    deferred_by: str = "upstream_interrupt",
) -> TimedWaitDecision:
    """Decide and persist a timed wait's charge and blocker before ``ProgressState.WAITING`` is saved."""

    async with _lock(agent, task_id):
        decision = await decide_timed_wait(
            agent,
            task_id=task_id,
            interrupt_id=interrupt_id,
            reason=reason,
            msg_meta=msg_meta,
            defer_for_other_wait=defer_for_other_wait,
            deferred_by=deferred_by,
        )
        if decision.action in {"record", "restore"}:
            await write_timed_wait(agent, decision)
        return decision


async def finish_timed_wait(agent: Any, decision: TimedWaitDecision) -> bool:
    """Run post-``WAITING`` effects for a staged timed wait (wake scheduler or hand start-time refusal)."""

    if decision.action in {"record", "restore"}:
        _wake_scheduler(agent)
        return True
    if decision.action == "refuse":
        async with _lock(agent, decision.task_id):
            await _refuse_at_start(
                agent,
                task_id=decision.task_id,
                interrupt_id=decision.interrupt_id,
                refusal=decision.refusal or "The timed wait was refused at start time.",
            )
    return False


async def begin_timed_wait(
    agent: Any,
    *,
    task_id: str,
    interrupt_id: str,
    reason: dict[str, Any],
    msg_meta: Any,
    defer_for_other_wait: bool = False,
    deferred_by: str = "upstream_interrupt",
) -> bool:
    """Stage and finish a timed wait in one call (convenience wrapper; True when a record was saved)."""

    decision = await stage_timed_wait(
        agent,
        task_id=task_id,
        interrupt_id=interrupt_id,
        reason=reason,
        msg_meta=msg_meta,
        defer_for_other_wait=defer_for_other_wait,
        deferred_by=deferred_by,
    )
    return await finish_timed_wait(agent, decision)


# -- resume ----------------------------------------------------------------------------------


def _timed_wait_tasks(agent: Any) -> list[tuple[str, dict[str, Any]]]:
    """This agent's non-terminal tasks with a timed-wait blocker (an operations-lane read)."""

    manager = agent.task_manager
    waits: list[tuple[str, dict[str, Any]]] = []
    for task_id, blocker in list(manager.task_state.human_interaction_blockers.items()):
        if not is_timed_wait_blocker(blocker):
            continue
        info = manager.get_task_info(task_id)
        if info is not None and not manager.is_terminal_state(info.state):
            waits.append((task_id, dict(blocker)))
    return waits


async def _answer_paused_turn(agent: Any, *, task_id: str, blocker: dict[str, Any], now: datetime) -> bool:
    """Mark the record and hand the timer answer to the live paused turn without clobbering another answer."""

    interrupt_id = str(blocker.get("interrupt_id") or "")
    now_iso = now.isoformat()
    steering = TaskSteering(
        steering_id=f"timed_wait_resume_{interrupt_id}",
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id=task_id,
        target_agent_id=agent.id,
        interrupt_responses=[
            {
                "interrupt_id": interrupt_id,
                "response": {
                    "kind": TIMED_WAIT_RESPONSE_KIND,
                    "interrupt_id": interrupt_id,
                    "resumed_at": now_iso,
                },
            }
        ],
        status=TaskSteeringStatus.PENDING,
        idempotency_key=f"timed_wait_resume:{interrupt_id}",
    )
    held = await hand_answer_to_paused_turn(
        agent,
        task_id=task_id,
        kind=TIMED_WAIT_BLOCKER,
        wait_id=interrupt_id,
        steering=steering,
        interrupt_id=interrupt_id,
        replace_pending=False,
        at=now_iso,
    )
    if not held:
        return False

    raw_minutes = blocker.get("minutes")
    fallback_minutes = raw_minutes if isinstance(raw_minutes, int) and not isinstance(raw_minutes, bool) else 0
    elapsed = waited_minutes(blocker.get("requested_at"), now, fallback_minutes)
    try:
        await append_wait_history(
            agent,
            task_id,
            f"Timed wait ended after {elapsed} minutes; resuming paused turn.",
            {
                "event": TIMED_WAIT_ENDED_EVENT,
                "interrupt_id": interrupt_id,
                "call_id": blocker.get("call_id"),
                "tool": blocker.get("tool"),
                "tool_use_id": blocker.get("tool_use_id"),
                "minutes": blocker.get("minutes"),
                "waited_minutes": elapsed,
                "resumed_at": now_iso,
                "how": "live_turn",
            },
        )
    except Exception as exc:  # noqa: BLE001 - answer is already held and waking the drain
        logger.warning(
            "Timed wait answer was held, but writing its history entry failed. "
            "event=timed_wait_history_failed task_id=%s interrupt_id=%s agent_id=%s error_type=%s",
            task_id,
            interrupt_id,
            getattr(agent, "id", "-"),
            type(exc).__name__,
        )
    logger.info(
        "Timed wait ended; answer waits for the task's lane. "
        "event=timed_wait_answer_held task_id=%s interrupt_id=%s agent_id=%s waited_minutes=%s",
        task_id,
        interrupt_id,
        getattr(agent, "id", "-"),
        elapsed,
    )
    return True


async def _resume_task_from_note(
    agent: Any,
    *,
    task_id: str,
    blocker: dict[str, Any],
    now: datetime,
    wake: bool,
) -> bool:
    """Resume a timed wait with no live paused turn via a durable resume note."""

    interrupt_id = str(blocker.get("interrupt_id") or "")
    now_iso = now.isoformat()
    raw_minutes = blocker.get("minutes")
    fallback_minutes = raw_minutes if isinstance(raw_minutes, int) and not isinstance(raw_minutes, bool) else 0
    elapsed = waited_minutes(blocker.get("requested_at"), now, fallback_minutes)
    resumed = await resume_from_note(
        agent,
        task_id=task_id,
        kind=TIMED_WAIT_BLOCKER,
        wait_id=interrupt_id,
        note=resume_note(blocker),
        history=f"Timed wait ended after {elapsed} minutes; task resumed from saved note.",
        history_details={
            "event": TIMED_WAIT_ENDED_EVENT,
            "interrupt_id": interrupt_id,
            "call_id": blocker.get("call_id"),
            "tool": blocker.get("tool"),
            "tool_use_id": blocker.get("tool_use_id"),
            "minutes": blocker.get("minutes"),
            "waited_minutes": elapsed,
            "resumed_at": now_iso,
            "how": "note",
        },
        log=_TIMED_WAIT_LOG,
        wake=wake,
        at=now_iso,
    )
    if resumed:
        logger.info(
            "Timed wait ended; task resumed from saved note. "
            "event=timed_wait_resumed task_id=%s interrupt_id=%s agent_id=%s waited_minutes=%s how=note",
            task_id,
            interrupt_id,
            getattr(agent, "id", "-"),
            elapsed,
        )
    return resumed


async def _resume_one_due_task(agent: Any, task_id: str, moment: datetime, *, wake: bool) -> bool:
    manager = agent.task_manager
    run_ops = manager.operations_runtime.run
    async with _lock(agent, task_id):
        task_info = await run_ops(manager.get_task_info, task_id)
        if task_info is None or await run_ops(manager.is_terminal_state, task_info.state):
            return False
        blocker = await run_ops(partial(get_human_interaction_blocker, manager), task_id)
        if blocker is None or not is_timed_wait_blocker(blocker) or not is_due(blocker, moment):
            return False
        interrupt_id = str(blocker.get("interrupt_id") or "")
        resume_at_raw = str(blocker.get("resume_at") or "")
        if not interrupt_id:
            level = _anomaly_log_level(
                agent,
                task_id=task_id,
                interrupt_id="",
                resume_at=resume_at_raw,
                anomaly="missing_interrupt_id",
            )
            logger.log(
                level,
                "Timed wait record has no interrupt_id; cannot resume. "
                "event=timed_wait_resume_failed task_id=%s interrupt_id=- agent_id=%s reason=missing_interrupt_id",
                task_id,
                getattr(agent, "id", "-"),
            )
            return False
        if paused_turn_waits(agent, task_id, interrupt_id):
            if answer_held(blocker, str(agent.id)):
                return False
            return await _answer_paused_turn(agent, task_id=task_id, blocker=blocker, now=moment)
        if await run_ops(manager.is_executing_state, task_info.state):
            level = _anomaly_log_level(
                agent,
                task_id=task_id,
                interrupt_id=interrupt_id,
                resume_at=resume_at_raw,
                anomaly=f"executing_with_blocker:{task_info.state.value}",
            )
            logger.log(
                level,
                "Task is in an executing state with a due timed-wait record and no paused turn; leaving for next pass. "
                "event=timed_wait_executing_with_blocker task_id=%s interrupt_id=%s state=%s agent_id=%s",
                task_id,
                interrupt_id,
                task_info.state.value,
                getattr(agent, "id", "-"),
            )
            return False
        return await _resume_task_from_note(agent, task_id=task_id, blocker=blocker, now=moment, wake=wake)


async def resume_due_timed_waits(
    agent: Any,
    *,
    now: datetime | None = None,
    at_start: bool = False,
) -> int:
    """Resume every non-terminal task of this agent whose timed wait is due (module docstring)."""

    manager = getattr(agent, "task_manager", None)
    if manager is None:
        return 0
    moment = (now or timed_waits_rules.utc_now()).astimezone(timezone.utc)
    candidates = await manager.operations_runtime.run(partial(_timed_wait_tasks, agent))
    resumed = 0
    wake = not at_start
    still_active: set[tuple[str, str, str]] = set()
    for task_id, blocker in candidates:
        interrupt_id = str(blocker.get("interrupt_id") or "")
        resume_at_raw = str(blocker.get("resume_at") or "")
        record_key = (task_id, interrupt_id, resume_at_raw)
        if not is_due(blocker, moment):
            still_active.add(record_key)
            continue
        try:
            if await _resume_one_due_task(agent, task_id, moment, wake=wake):
                resumed += 1
            else:
                still_active.add(record_key)
        except Exception as exc:  # noqa: BLE001 - one task must not stop other due waits
            still_active.add(record_key)
            level = _anomaly_log_level(
                agent,
                task_id=task_id,
                interrupt_id=interrupt_id,
                resume_at=resume_at_raw,
                anomaly=f"resume_failed:{type(exc).__name__}",
            )
            logger.log(
                level,
                "Could not resume a due timed wait; the next pass will try again. "
                "event=timed_wait_resume_failed task_id=%s interrupt_id=%s agent_id=%s error_type=%s",
                task_id,
                interrupt_id or "-",
                getattr(agent, "id", "-"),
                type(exc).__name__,
                exc_info=exc if level >= logging.WARNING else None,
            )
    _prune_warned_anomalies(agent, still_active)
    return resumed


# -- answers a paused turn cannot take -------------------------------------------------------


def _timed_wait_answer(steering: TaskSteering) -> tuple[str, str, dict[str, Any]] | None:
    """Extract ``(kind, interrupt_id, response)`` for a timed-wait answer, or None for any other."""

    for item in steering.interrupt_responses or []:
        response = item.get("response")
        if not isinstance(response, dict):
            continue
        kind = response.get("kind")
        if kind in {TIMED_WAIT_RESPONSE_KIND, TIMED_WAIT_REFUSED_RESPONSE_KIND}:
            interrupt_id = str(item.get("interrupt_id") or response.get("interrupt_id") or "")
            return str(kind), interrupt_id, response
    return None


async def resume_without_paused_turn(agent: Any, task_id: str, steering: TaskSteering, *, why: str) -> bool:
    """A timed-wait answer its paused turn could not take: drop the turn and resume from a note."""

    answer = _timed_wait_answer(steering)
    if answer is None:
        return False
    kind, interrupt_id, response = answer
    logger.warning(
        "A paused turn could not take its timed-wait answer; the task resumes from a note. "
        "event=timed_wait_answer_not_taken task_id=%s interrupt_id=%s why=%s",
        task_id,
        interrupt_id or "-",
        why,
    )
    drop_paused_turn(agent, task_id, interrupt_id, why=why, log=_TIMED_WAIT_LOG)
    manager = agent.task_manager
    run_ops = manager.operations_runtime.run
    try:
        if kind == TIMED_WAIT_REFUSED_RESPONSE_KIND:
            refusal = str(response.get("reason") or "The timed wait was refused at start time.")
            await run_ops(
                manager.append_runtime_resume_directive,
                task_id,
                f"Your requested timed wait was refused at start time ({refusal}). Continue the task without waiting.",
            )
            await run_ops(
                manager.update_main_task_state,
                task_id,
                ProgressState.ACCEPTED,
                timed_waits_rules.utc_now().isoformat(),
            )
            await run_ops(manager.enqueue_task, task_id, front=True)
            schedule_pending_work(agent)
            return True

        async with _lock(agent, task_id):
            blocker = await run_ops(partial(remove_answer_mark, manager), task_id, TIMED_WAIT_BLOCKER, interrupt_id)
            if blocker is None:
                return False
            return await _resume_task_from_note(
                agent,
                task_id=task_id,
                blocker=blocker,
                now=timed_waits_rules.utc_now(),
                wake=True,
            )
    except Exception as exc:  # noqa: BLE001 - scheduler pass retries from the unmarked record
        logger.warning(
            "Could not resume a task whose paused turn could not take its timed-wait answer; next pass will retry. "
            "event=timed_wait_answer_not_taken_failed task_id=%s interrupt_id=%s why=%s error_type=%s",
            task_id,
            interrupt_id or "-",
            why,
            type(exc).__name__,
            exc_info=exc,
        )
        return False


# -- scheduler idle sleep helper -------------------------------------------------------------


def _overdue_retry_delay(overdue_seconds: float) -> float:
    """Back off an already-due wait that could not be resumed, up to the normal scheduler cadence."""

    return min(MAX_SCHEDULER_SLEEP_SECONDS, max(1.0, overdue_seconds / 2.0))


def _next_resume_delay_sync(agent: Any, moment: datetime) -> float | None:
    """Seconds until the earliest unheld timed wait, or None if no unheld timed wait is active.

    An already-due wait that is still present after the pass could not be
    resumed (anomalous state, transient failure, or another answer pending);
    its retry delay grows with how overdue it is up to
    ``MAX_SCHEDULER_SLEEP_SECONDS`` so it does not wake the loop every second
    while still allowing a healthy upcoming wait to wake on time.
    """

    manager = getattr(agent, "task_manager", None)
    state = getattr(manager, "task_state", None)
    blockers = getattr(state, "human_interaction_blockers", None)
    if manager is None or not blockers:
        return None
    holder_id = str(getattr(agent, "id", ""))
    earliest_delay: float | None = None
    for task_id, blocker in list(blockers.items()):
        if not is_timed_wait_blocker(blocker):
            continue
        info = manager.get_task_info(task_id)
        if info is None or manager.is_terminal_state(info.state):
            continue
        interrupt_id = str(blocker.get("interrupt_id") or "")
        # Skip waits whose answer is already held for a live paused turn so a busy lane does not spin.
        if answer_held(blocker, holder_id) and paused_turn_waits(agent, task_id, interrupt_id):
            continue
        target = resume_at_of(blocker)
        if target is None:
            continue
        raw_delay = (target - moment).total_seconds()
        delay = _overdue_retry_delay(-raw_delay) if raw_delay <= 0.0 else raw_delay
        if earliest_delay is None or delay < earliest_delay:
            earliest_delay = delay
    return earliest_delay


async def seconds_until_next_resume(agent: Any, *, now: datetime | None = None) -> float | None:
    """Seconds until the next unheld timed wait comes due, for capping the scheduler's idle sleep."""

    manager = getattr(agent, "task_manager", None)
    ops = getattr(manager, "operations_runtime", None)
    if manager is None or ops is None:
        return None
    moment = (now or timed_waits_rules.utc_now()).astimezone(timezone.utc)
    result: float | None = await ops.run(partial(_next_resume_delay_sync, agent, moment))
    return result


__all__ = [
    "TimedWaitDecision",
    "begin_timed_wait",
    "check_schedule_next_run",
    "decide_timed_wait",
    "finish_timed_wait",
    "resume_due_timed_waits",
    "resume_without_paused_turn",
    "seconds_until_next_resume",
    "stage_timed_wait",
    "write_timed_wait",
]
