"""Pure rules for timed task pauses (``wait`` and ``long_wait``).

A task may pause itself for a fixed number of whole minutes and continue in the
same task once the timer ends. This module holds the shared constants, clock,
per-task budget ledger (read from persisted task history), recurring-schedule
next-run check, and blocker/summary formatting. It performs no I/O so the
blocker store, stuck-task checks, tool provider, and runtime can all import it
without cycles.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from custom_messages.contracts.schedule_v1 import ScheduledMessage, is_recurring_schedule
from runtime_settings.timed_waits import TimedWaitLimits

# Interrupt kind, blocker record kind, and interrupt-response kinds (defined once).
TIMED_WAIT_KIND = "timed_wait"
TIMED_WAIT_RESPONSE_KIND = "timed_wait_resumed"
TIMED_WAIT_REFUSED_RESPONSE_KIND = "timed_wait_refused"

# Tool names and catalog tool ids.
WAIT_TOOL_NAME = "wait"
LONG_WAIT_TOOL_NAME = "long_wait"
WAIT_TOOL_ID = "fn:wait"
LONG_WAIT_TOOL_ID = "fn:long_wait"

# Task history event names (stored in TaskHistoryEntry.details["event"]).
TIMED_WAIT_STARTED_EVENT = "timed_wait_started"
TIMED_WAIT_ENDED_EVENT = "timed_wait_ended"

# Grace window past resume_at before an unheld timed wait is reported as stuck.
OVERDUE_GRACE_SECONDS = 300
MAX_REASON_CHARS = 500


def utc_now() -> datetime:
    """Current UTC time (the single clock for timed waits; patched in tests)."""

    return datetime.now(timezone.utc)


def parse_utc(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp to an aware UTC datetime, or None if missing/invalid."""

    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def format_utc_display(moment: datetime) -> str:
    """Human-readable UTC timestamp for summaries, notes, and refusal messages."""

    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def validate_wait_args(minutes: Any, reason: Any) -> tuple[int, str]:
    """Validate and normalize ``minutes`` (whole int >= 1) and ``reason`` (1..500 chars)."""

    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
        raise ValueError("minutes must be a whole number of at least 1.")
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise ValueError("Give a short reason explaining what you are waiting for.")
    if len(cleaned_reason) > MAX_REASON_CHARS:
        raise ValueError(
            f"reason must be at most {MAX_REASON_CHARS} characters (got {len(cleaned_reason)})."
        )
    return minutes, cleaned_reason


@dataclass(frozen=True)
class WaitEntry:
    """One timed wait charged against a task's budget, reconstructed from history."""

    call_id: str
    tool_use_id: str
    tool: str
    minutes: int
    requested_at: str
    resume_at: str
    reason: str
    interrupt_id: str | None = None


@dataclass(frozen=True)
class WaitLedger:
    """Per-task timed-wait budget reconstructed from persisted task history.

    Resumed-call lookup is keyed by ``call_id`` (agent id + model step
    ``tracking_id`` + ``tool_use_id`` via ``access_runtime.step_tool_call_id``),
    so a later model step whose server reuses a ``tool_use_id`` is never
    mistaken for an earlier paused call. Every distinct started wait entry
    counts toward ``total_minutes``. Reading the budget from task history means
    it survives restarts without a new database table or snapshot schema field.
    """

    entries_by_call_id: Mapping[str, WaitEntry]
    total_minutes: int

    @classmethod
    def from_history(cls, task: Any) -> WaitLedger:
        history = _extract_history(task)
        entries: dict[str, WaitEntry] = {}
        total = 0
        for idx, item in enumerate(history):
            details = _entry_details(item)
            if not details or details.get("event") != TIMED_WAIT_STARTED_EVENT:
                continue
            raw_minutes = details.get("minutes")
            if not isinstance(raw_minutes, int) or isinstance(raw_minutes, bool) or raw_minutes < 1:
                continue
            tool_use_id = str(details.get("tool_use_id") or "").strip()
            call_id = str(details.get("call_id") or "").strip() or tool_use_id
            key = call_id or f"history:{idx}"
            if key in entries:
                continue
            entries[key] = WaitEntry(
                call_id=key,
                tool_use_id=tool_use_id,
                tool=str(details.get("tool") or WAIT_TOOL_NAME),
                minutes=raw_minutes,
                requested_at=str(details.get("requested_at") or ""),
                resume_at=str(details.get("resume_at") or ""),
                reason=str(details.get("reason") or ""),
                interrupt_id=str(details.get("interrupt_id") or "") or None,
            )
            total += raw_minutes
        return cls(
            entries_by_call_id=entries,
            total_minutes=total,
        )

    def entry_for(self, call_id: str) -> WaitEntry | None:
        return self.entries_by_call_id.get(call_id)

    def has_started(self, call_id: str) -> bool:
        return call_id in self.entries_by_call_id


def _extract_history(task: Any) -> Iterable[Any]:
    history = getattr(task, "execution_history", None)
    if isinstance(history, Iterable) and not isinstance(history, (str, bytes, Mapping)):
        return history
    inner = getattr(task, "task", None)
    history = getattr(inner, "execution_history", None)
    if isinstance(history, Iterable) and not isinstance(history, (str, bytes, Mapping)):
        return history
    if isinstance(task, Iterable) and not isinstance(task, (str, bytes, Mapping)):
        return task
    return ()


def _entry_details(item: Any) -> Mapping[str, Any] | None:
    if isinstance(item, Mapping):
        details = item.get("details")
        return details if isinstance(details, Mapping) else None
    details = getattr(item, "details", None)
    return details if isinstance(details, Mapping) else None


def _plural_minutes(count: int) -> str:
    return "1 minute" if count == 1 else f"{count} minutes"


def _max_total_phrase(limit_minutes: int) -> str:
    if limit_minutes == 24 * 60:
        return f"{limit_minutes} minutes (24 hours)"
    return _plural_minutes(limit_minutes)


def budget_refusal(
    ledger: WaitLedger,
    minutes: int,
    tool: str,
    limits: TimedWaitLimits,
    *,
    authorization_on: bool = True,
    has_long_wait_tool: bool = False,
) -> str | None:
    """Return a plain-language refusal if ``minutes`` exceeds the task's budget for ``tool``, else None."""

    used = ledger.total_minutes
    projected = used + minutes
    if tool == WAIT_TOOL_NAME:
        limit = limits.without_approval
        if projected <= limit:
            return None
        remaining = max(0, limit - used)
        header = (
            f"Waiting {_plural_minutes(minutes)} would bring this task's total waited time to "
            f"{_plural_minutes(projected)}, exceeding the {_plural_minutes(limit)} limit for {WAIT_TOOL_NAME} "
            f"({_plural_minutes(used)} already used, {_plural_minutes(remaining)} left)."
        )
        max_phrase = _max_total_phrase(limits.with_approval)
        if limits.with_approval <= limits.without_approval:
            return (
                f"{header} {LONG_WAIT_TOOL_NAME} has the same {max_phrase} team limit here; "
                "finish what you can or report where things stand."
            )
        if has_long_wait_tool:
            return f"{header} Use {LONG_WAIT_TOOL_NAME} instead (up to {max_phrase} in total for this task)."
        if authorization_on:
            return (
                f"{header} For longer waits (up to {max_phrase} in total for this task), ask a person for "
                f"approval with request_access(tool_id={LONG_WAIT_TOOL_ID!r}, level='write', reason='...') "
                f"and then call {LONG_WAIT_TOOL_NAME}."
            )
        return (
            f"{header} Longer waits need {LONG_WAIT_TOOL_NAME} (up to {max_phrase} in total); with authorization "
            f"off, an operator must add {LONG_WAIT_TOOL_ID!r} to this agent type's default_tools."
        )
    if tool == LONG_WAIT_TOOL_NAME:
        limit = limits.with_approval
        if projected <= limit:
            return None
        remaining = max(0, limit - used)
        max_phrase = _max_total_phrase(limit)
        return (
            f"Waiting {_plural_minutes(minutes)} would bring this task's total waited time to "
            f"{_plural_minutes(projected)}, exceeding the {max_phrase} maximum for a task "
            f"({_plural_minutes(used)} already used, {_plural_minutes(remaining)} left). "
            "Finish what you can or report where things stand."
        )
    raise ValueError(f"Unknown timed wait tool: {tool!r}")


def next_run_refusal(
    schedule: ScheduledMessage,
    resume_at: datetime,
    *,
    now: datetime | None = None,
    finishing: bool = False,
) -> str | None:
    """Refuse a wait in a recurring scheduled run that would reach or pass its next run time."""

    if finishing or not schedule.enabled or not is_recurring_schedule(schedule):
        return None
    next_fire = parse_utc(schedule.next_fire_at)
    if next_fire is None:
        return None
    paused_until = parse_utc(schedule.paused_until)
    if paused_until is not None and paused_until > next_fire:
        next_fire = paused_until
    expires_at = parse_utc(schedule.expires_at_utc)
    if expires_at is not None and next_fire >= expires_at:
        return None
    target = resume_at.astimezone(timezone.utc)
    if target < next_fire:
        return None
    fit_clause = ""
    if now is not None:
        gap_seconds = (next_fire - now.astimezone(timezone.utc)).total_seconds()
        max_fit = max(0, math.ceil(gap_seconds / 60) - 1) if gap_seconds > 0 else 0
        if max_fit >= 1:
            fit_clause = f" The longest wait that fits before the next run is {_plural_minutes(max_fit)}."
        else:
            fit_clause = " No whole-minute wait fits before the next run."
    return (
        f"Cannot wait until {format_utc_display(target)}: this recurring schedule's next run is at "
        f"{format_utc_display(next_fire)} ({next_fire.isoformat()}), so waiting that long would overlap "
        f"and skip the next run.{fit_clause}"
    )


def wait_summary_text(*, minutes: int, resume_at: str, reason: str) -> str:
    """One-line summary shown in task history, blocker records, and runtime work status."""

    parsed = parse_utc(resume_at)
    until = format_utc_display(parsed) if parsed is not None else (resume_at or "unknown time")
    return f"Waiting until {until} ({minutes}-minute wait: {reason})."


def wait_summary(record: Mapping[str, Any]) -> str:
    """Human-readable summary for a saved timed-wait blocker record."""

    minutes = record.get("minutes")
    resume_at = str(record.get("resume_at") or "")
    reason = str(record.get("reason") or "").strip() or "timed wait"
    if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 1:
        return wait_summary_text(minutes=minutes, resume_at=resume_at, reason=reason)
    existing = str(record.get("summary") or "").strip()
    return existing or "Waiting for a timed pause to end."


def wait_status_summary(
    record: Mapping[str, Any],
    *,
    now: datetime | None = None,
    held: bool = False,
) -> str:
    """Live runtime-work summary for a timed-wait blocker, distinguishing running vs ended waits."""

    moment = (now or utc_now()).astimezone(timezone.utc)
    if not held and not is_due(record, moment):
        return wait_summary(record)
    minutes = record.get("minutes")
    duration = (
        f"{minutes}-minute"
        if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 1
        else "timed"
    )
    reason = str(record.get("reason") or "").strip()
    reason_clause = f" ({reason})" if reason else ""
    scheduled = resume_at_of(record)
    ended_at = format_utc_display(scheduled) if scheduled is not None else "its scheduled time"
    if held:
        return (
            f"The {duration} wait{reason_clause} ended at {ended_at}; "
            "the task continues when the agent finishes its current work."
        )
    return f"The {duration} wait{reason_clause} ended at {ended_at}; waiting for the agent to resume the task."


def timed_wait_record(
    *,
    interrupt_id: str,
    tool: str,
    tool_use_id: str,
    minutes: int,
    reason: str,
    requested_at: str,
    resume_at: str,
    call_id: str | None = None,
) -> dict[str, Any]:
    """Build the saved blocker record stored in ``TaskRuntimeState.human_interaction_blockers``."""

    record: dict[str, Any] = {
        "kind": TIMED_WAIT_KIND,
        "interrupt_id": interrupt_id,
        "tool": tool,
        "tool_use_id": tool_use_id,
        "minutes": int(minutes),
        "reason": reason,
        "requested_at": requested_at,
        "resume_at": resume_at,
        "summary": wait_summary_text(minutes=minutes, resume_at=resume_at, reason=reason),
    }
    if call_id:
        record["call_id"] = call_id
    return record


def resume_at_of(record: Mapping[str, Any] | None) -> datetime | None:
    """Parsed UTC resume time of a timed-wait blocker record, or None if missing/malformed."""

    if not record:
        return None
    return parse_utc(record.get("resume_at"))


def is_due(record: Mapping[str, Any] | None, now: datetime | None = None) -> bool:
    """Whether a timed-wait record's resume time has arrived."""

    target = resume_at_of(record)
    if target is None:
        return False
    moment = (now or utc_now()).astimezone(timezone.utc)
    return moment >= target


def timer_running(record: Mapping[str, Any] | None, now: datetime | None = None) -> bool:
    """Whether a timed-wait record is still within its timer + overdue grace period.

    Returns False once ``resume_at + OVERDUE_GRACE_SECONDS`` has passed, or when
    ``resume_at`` is missing/invalid, so the stuck-task check reports the task.
    """

    target = resume_at_of(record)
    if target is None:
        return False
    moment = (now or utc_now()).astimezone(timezone.utc)
    return moment <= target + timedelta(seconds=OVERDUE_GRACE_SECONDS)


def waited_minutes(requested_at: str | None, now: datetime, fallback_minutes: int) -> float:
    """Elapsed minutes since ``requested_at``, rounded to one decimal place."""

    started = parse_utc(requested_at)
    if started is None:
        return float(fallback_minutes)
    elapsed_seconds = max(0.0, (now.astimezone(timezone.utc) - started).total_seconds())
    return round(elapsed_seconds / 60.0, 1)


def wait_result(
    *,
    minutes: int,
    requested_at: str | None,
    now: datetime,
    status: str = "waited",
    next_step: str | None = None,
) -> dict[str, Any]:
    """Structured tool result returned when a paused ``wait`` / ``long_wait`` resumes."""

    moment = now.astimezone(timezone.utc)
    return {
        "status": status,
        "minutes": minutes,
        "waited_minutes": waited_minutes(requested_at, moment, minutes),
        "now_utc": moment.isoformat(),
        "next_step": next_step or "Continue the task from where you paused.",
    }


def resume_note(record: Mapping[str, Any], now: datetime | None = None) -> str:
    """Durable resume directive saved when a timed wait resumes without a live paused turn."""

    moment = (now or utc_now()).astimezone(timezone.utc)
    minutes = record.get("minutes")
    duration = (
        f"{minutes}-minute"
        if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 1
        else "timed"
    )
    reason = str(record.get("reason") or "").strip()
    reason_clause = f" ({reason})" if reason else ""
    scheduled = resume_at_of(record)
    scheduled_clause = f"; scheduled for {format_utc_display(scheduled)}" if scheduled is not None else ""
    return (
        f"Your {duration} wait{reason_clause} ended at {format_utc_display(moment)} "
        f"({moment.isoformat()}{scheduled_clause}). Continue the task from where you left off."
    )


__all__ = [
    "LONG_WAIT_TOOL_ID",
    "LONG_WAIT_TOOL_NAME",
    "MAX_REASON_CHARS",
    "OVERDUE_GRACE_SECONDS",
    "TIMED_WAIT_ENDED_EVENT",
    "TIMED_WAIT_KIND",
    "TIMED_WAIT_REFUSED_RESPONSE_KIND",
    "TIMED_WAIT_RESPONSE_KIND",
    "TIMED_WAIT_STARTED_EVENT",
    "WAIT_TOOL_ID",
    "WAIT_TOOL_NAME",
    "WaitEntry",
    "WaitLedger",
    "budget_refusal",
    "format_utc_display",
    "is_due",
    "next_run_refusal",
    "parse_utc",
    "resume_at_of",
    "resume_note",
    "timed_wait_record",
    "timer_running",
    "utc_now",
    "validate_wait_args",
    "wait_result",
    "wait_status_summary",
    "wait_summary",
    "wait_summary_text",
    "waited_minutes",
]
