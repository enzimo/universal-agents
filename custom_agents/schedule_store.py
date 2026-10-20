"""
Persistent storage and scheduling logic for ScheduledMessage objects.

Schedules are ordered by next_fire_at (earliest first) and can use
fixed intervals (interval_ms) or cron expressions for recurring execution.

Every write that depends on a schedule's current values reads, decides and
writes in one write transaction (``ScheduleRepository.write_locked``), so a
change never overwrites a newer one (2026-10-17). Before, saves replaced the
whole row with a copy read earlier: a run recording its notes brought back a
schedule its person had just deleted, undid a pause or a turn-off, and cleared
another worker's claim, and an edit racing a reminder's last run recreated the
reminder so that it could never finish. Now:

- ``add`` installs a schedule: it creates the row, or replaces the definition
  of an existing one for installers that hold that authority (gateway
  installs, Objective work schedules). It keeps the claim, and the remaining
  count unless the repeat count changed. With ``create_only`` (a model's
  ``schedule_message`` action) it only creates and never changes a stored row.
- ``update`` saves a change made to a copy read earlier. It is refused when
  the schedule is gone or another change reached it meanwhile
  (``ScheduleWriteConflict``), and it keeps the next run time, the remaining
  count and the run notes unless the change itself changed them.
- ``edit_schedule_memory`` changes only the run notes.
- Turning on and off, pausing, resuming and removing change only their own
  fields, and never clear a claim. A run that has not started is still
  stopped: whoever starts or signs it checks the schedule again
  (``schedule_stopped_reason``), and typed root jobs again at admission
  (``ScheduleWorkerRepository.commit_admission``). The advance after a run
  (``update_after_fire``) writes only the run time, the count and the claim.
- Every deletion (``remove``, the advance that finishes a schedule,
  ``prune_expired``) ends the schedule's access grants.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import TYPE_CHECKING, Any, Callable, List, Mapping, Optional, TypedDict

from storage.backends import STORAGE_BACKEND_SQLITE
from custom_agents.schedule_fields import schedule_memory_now, trim_schedule_memory_ledger
from storage.runtime_custody import DeferredMessageClaim
from custom_agents.schedule_management import (
    ScheduleMutationOperation,
    ScheduleMutationPermit,
    SystemScheduleMutationDenied,
    is_system_managed_schedule,
    require_schedule_mutation_permit,
    schedule_control_state_digest,
    schedule_reviewed_state_digest,
)
from custom_messages.enums import ChangeRefusal, ScheduleManagementClass
from custom_messages.contracts.routing_v1 import PERSON_GATEWAY_FIELDS
from default_values import STORAGE_DEFAULTS
from id_generation import new_id
from storage.config import StorageSettings, resolve_storage_settings
from storage.schedule_repository import RowWrite, create_schedule_repository

if TYPE_CHECKING:
    from custom_messages.contracts.access_v2 import SignedSchedulePermitV1
    from custom_messages.contracts.schedule_v1 import ScheduledMessage, ScheduleTaskMemory

logger = logging.getLogger(__name__)

# Why a schedule write was refused (``ScheduleWriteConflict.reason_code``).
# A schedule that changed since the caller read it uses the shared name for a
# stale read (``ChangeRefusal``), as other stores do.
SCHEDULE_CHANGED = ChangeRefusal.CHANGED_SINCE_READ.value
SCHEDULE_GONE = "schedule_not_found"
SCHEDULE_OWNER_CHANGED = "schedule_owner_changed"


class ScheduleWriteConflict(RuntimeError):
    """A schedule write was refused: the schedule is gone, or changed since the caller read it."""

    def __init__(self, reason_code: str, schedule_id: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.schedule_id = schedule_id


class AdvanceOutcome(StrEnum):
    """What ``update_after_fire`` did after a run was admitted."""

    ADVANCED = "advanced"  # the next run time and remaining count moved on
    FINISHED = "finished"  # that was the schedule's last run; it was removed
    NOT_FOUND = "not_found"  # the schedule was removed during the run
    CLAIM_LOST = "claim_lost"  # another agent holds the schedule's claim now
    DUE_CHANGED = "due_changed"  # its next run time changed during the run; that change stands
    FAILED = "failed"  # storage or timing error (logged with its traceback)

    @property
    def applied(self) -> bool:
        return self in (AdvanceOutcome.ADVANCED, AdvanceOutcome.FINISHED)


@dataclass(frozen=True)
class OwnerGuard:
    """A run-notes edit applies only while the schedule's owner is still ``owner``."""

    owner: Optional[str]


class ScheduleTelemetrySummary(TypedDict):
    """Stable aggregate shape returned by schedule telemetry."""

    total: int
    enabled: int
    disabled: int
    paused: int
    due: int
    recurring: int
    one_shot: int
    with_interval: int
    with_cron: int
    load_errors: int
    next_fire_at: str | None

DEFAULT_SCHEDULE_CLAIM_TTL_SECONDS = 120
_UNSET_REMAINING_COUNT = object()


def _parse_utc_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _preview_text(value: Optional[str], *, limit: int = 180) -> Optional[str]:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


class ScheduleClaimScope(TypedDict):
    """Only the three claim fields inferred from an agent's identity."""

    claim_team_id: str | None
    claim_named_agent_type: str | None
    claim_agent_id: str | None


def schedule_claim_scope_for_agent(agent: Any, *, task_local: bool = False) -> ScheduleClaimScope:
    """Allow replaceable type ownership unless firing requires this agent's task memory."""
    named_agent_type = getattr(agent, "named_agent_type", None)
    return {
        "claim_team_id": getattr(agent, "team_id", None),
        "claim_named_agent_type": named_agent_type,
        "claim_agent_id": getattr(agent, "id", None) if task_local or not named_agent_type else None,
    }


def owner_canonical_user_id_for_schedule(msg: "ScheduledMessage") -> Optional[str]:
    meta = getattr(msg, "payload_meta", None)
    gateway_context = getattr(meta, "gateway_context", None) if meta is not None else None
    value = getattr(gateway_context, "canonical_user_id", None) if gateway_context is not None else None
    return str(value or "").strip() or None


def apply_persisted_schedule_owner(
    msg: "ScheduledMessage",
    owner_canonical_user_id: Optional[str],
    *,
    logger_: logging.Logger,
) -> "ScheduledMessage":
    """Project the row owner, including no owner, into executable task metadata.

    Old rows can retain a person's signed context and chat route in their JSON
    after the owner column is added with NULL. Loading must never turn that
    stale context into a person-owned run or a private conversation.
    """

    owner = str(owner_canonical_user_id or "").strip()
    if not owner:
        meta = msg.payload_meta
        if meta is None or meta.gateway_context is None:
            return msg
        gateway_context = meta.gateway_context
        cleared_fields = tuple(
            name for name in PERSON_GATEWAY_FIELDS if getattr(gateway_context, name, None) is not None
        )
        if not cleared_fields:
            return msg
        embedded_owner = gateway_context.canonical_user_id
        for name in cleared_fields:
            setattr(gateway_context, name, None)
        # A saved personal conversation must not supply history to work for no
        # person. The runtime opens the run's own conversation after loading.
        meta.conversation_id = None
        meta.followup_to_task_id = None
        logger_.warning(
            "Cleared stale person context from a schedule with no recorded owner. "
            "event=schedule_owner_context_reconciled schedule_id=%s row_owner_canonical_user_id=%s "
            "embedded_owner_canonical_user_id=%s cleared_fields=%s",
            msg.schedule_id,
            None,
            embedded_owner,
            ",".join(cleared_fields),
        )
        return msg

    from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta

    if msg.payload_meta is None:
        msg.payload_meta = MessageMeta()
    if msg.payload_meta.gateway_context is None:
        msg.payload_meta.gateway_context = GatewayContext()
    embedded_owner = str(msg.payload_meta.gateway_context.canonical_user_id or "").strip()
    if embedded_owner != owner:
        logger_.warning(
            "Reconciled scheduled payload owner from authoritative repository row. "
            "event=schedule_owner_context_reconciled schedule_id=%s row_owner_canonical_user_id=%s "
            "embedded_owner_canonical_user_id=%s",
            msg.schedule_id,
            owner,
            embedded_owner or None,
        )
        msg.payload_meta.gateway_context.canonical_user_id = owner
    return msg


def compute_next_fire_at(
    interval_ms: Optional[int] = None,
    cron_expression: Optional[str] = None,
    from_time: Optional[datetime] = None,
    cron_timezone: Optional[str] = None,
    previous_fire_at: Optional[datetime] = None,
) -> str:
    """
    Compute the next fire time as an ISO 8601 UTC string.

    A cron expression is read in ``cron_timezone``: "0 9 * * *" is 9:00 there,
    also across daylight saving changes (croniter works in the zone of its
    start time). A cron expression without a time zone is refused; the store
    fills one when a schedule is added (``prepare_schedule_for_add``).

    ``previous_fire_at`` is the run just made. When clocks go back, the
    repeated hour's local times come twice. A schedule at fixed times of day
    (``_fixed_time_of_day``: "30 1 * * *", "0,30 1 * * *") skips results whose
    local time is not later than that run's, so it runs once per time that
    night, as classic cron does. A schedule whose minute or hour starts with
    a wildcard ("0 * * * *", "*/30 * * * *") runs every real hour,
    the repeated one included. When clocks go forward, a time in the skipped
    hour runs at the first time after it (02:30 runs at 03:00).
    """
    if from_time is None:
        from_time = datetime.now(timezone.utc)

    if interval_ms is not None:
        next_time = from_time + timedelta(milliseconds=interval_ms)
        return next_time.isoformat()

    if cron_expression is not None:
        if not cron_timezone:
            raise ValueError(f"The cron schedule {cron_expression!r} has no time zone.")
        if from_time.tzinfo is None:
            from_time = from_time.replace(tzinfo=timezone.utc)
        return _next_cron_run(cron_expression, from_time, ZoneInfo(cron_timezone), previous_fire_at)

    raise ValueError("Either interval_ms or cron_expression must be provided.")


def _next_cron_run(
    cron_expression: str, from_time: datetime, zone: ZoneInfo, previous_fire_at: Optional[datetime]
) -> str:
    """The next cron run after ``from_time`` in ``zone``, by the classic rule for daylight saving changes."""

    from croniter import croniter

    runs = croniter(cron_expression, from_time.astimezone(zone))
    next_time = runs.get_next(datetime)
    if previous_fire_at is not None and _fixed_time_of_day(cron_expression):
        previous_local = _local_wall_time(previous_fire_at, zone)
        # Some zones repeat more than an hour, and cron can include seconds.
        # Never return a repeated local time just because a fixed count ran out.
        while _local_wall_time(next_time, zone) <= previous_local:
            next_time = runs.get_next(datetime)
    return next_time.astimezone(timezone.utc).isoformat()


# Cron shorthands at fixed times of day, and the one that is not.
_FIXED_TIME_SHORTHANDS = frozenset({"@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight"})


def _fixed_time_of_day(cron_expression: str) -> bool:
    """Whether a cron expression runs only at fixed local times of day.

    As in classic cron: neither its minute nor its hour field starts with
    ``*`` (``*``, ``*/15``). Numbers, lists and ranges (``1-2``) are fixed.
    Such a job runs once per time when clocks go back; a wildcard job runs
    every real hour.
    """

    text = cron_expression.strip().lower()
    if text.startswith("@"):
        return text in _FIXED_TIME_SHORTHANDS
    fields = text.split()
    return len(fields) >= 2 and not any(field.startswith("*") for field in fields[:2])


def _local_wall_time(moment: datetime, zone: ZoneInfo) -> datetime:
    """The local date and wall-clock time of a moment in ``zone``, without its offset."""

    return moment.astimezone(zone).replace(tzinfo=None, fold=0)


def _valid_zone(name: Any) -> Optional[str]:
    try:
        return ZoneInfo(str(name)).key if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def fill_cron_timezone(msg: "ScheduledMessage", default_cron_timezone: Optional[str]) -> Optional[str]:
    """Give a cron schedule without a time zone its owner's, else the instance's; return the one set.

    The owner's time zone is the request time zone of the task that made the
    schedule (``payload_meta.request_timezone``, set by the gateway from the
    person's profile). System schedules without one use the instance time
    zone (``user.timezone``, or ``DEFAULT_REQUEST_TIMEZONE``).
    """

    if not msg.cron_expression or msg.cron_timezone:
        return None
    owner_zone = _valid_zone(getattr(msg.payload_meta, "request_timezone", None))
    zone = owner_zone or _valid_zone(default_cron_timezone)
    if zone is None:
        raise ValueError(
            f"The cron schedule {msg.schedule_id} has no time zone, and neither its owner nor the instance has one."
        )
    msg.cron_timezone = zone
    return zone


def with_cron_start(msg: "ScheduledMessage", default_cron_timezone: Optional[str]) -> "ScheduledMessage":
    """Fill a cron schedule's time zone (``fill_cron_timezone``) and compute its first run; return it.

    For callers that bind a permit to the schedule before saving it, so the
    store's own defaults change nothing afterwards.
    """

    fill_cron_timezone(msg, default_cron_timezone)
    msg.next_fire_at = compute_next_fire_at(cron_expression=msg.cron_expression, cron_timezone=msg.cron_timezone)
    return msg


def _drop_run_identity(msg: "ScheduledMessage") -> None:
    """A scheduled run's identity belongs to that run and is never saved on a schedule (logged).

    It is set on one run's task (``custom_agents.schedule_permits``); a
    schedule made from inside such a run copies the run's meta, so the
    saving path drops it. Each run of the saved schedule gets its own.
    """

    gateway_context = getattr(getattr(msg, "payload_meta", None), "gateway_context", None)
    if getattr(gateway_context, "schedule_run", None) is None:
        return
    logger.warning(
        "Dropped a scheduled run's identity from a schedule being saved; each run gets its own. "
        "event=schedule_run_identity_not_saved schedule_id=%s",
        msg.schedule_id,
    )
    gateway_context.schedule_run = None


def prepare_schedule_for_add(
    msg: "ScheduledMessage",
    *,
    claim_team_id: Optional[str] = None,
    claim_named_agent_type: Optional[str] = None,
    claim_agent_id: Optional[str] = None,
    default_cron_timezone: Optional[str] = None,
) -> "ScheduledMessage":
    """Apply store-owned defaults once before permit derivation or persistence."""

    _drop_run_identity(msg)
    fill_cron_timezone(msg, default_cron_timezone)
    if not msg.next_fire_at:
        msg.next_fire_at = compute_next_fire_at(
            interval_ms=msg.interval_ms,
            cron_expression=msg.cron_expression,
            cron_timezone=msg.cron_timezone,
        )
    if claim_team_id is not None and msg.claim_team_id is None:
        msg.claim_team_id = claim_team_id
    if claim_named_agent_type is not None and msg.claim_named_agent_type is None:
        msg.claim_named_agent_type = claim_named_agent_type
    if claim_agent_id is not None and msg.claim_agent_id is None:
        msg.claim_agent_id = claim_agent_id
    return msg


# -- rows -----------------------------------------------------------------------------

# The claim columns; only claiming, releasing and the advance after a run write them.
_CLEARED_CLAIM: dict[str, Any] = {
    "claimed_by_agent_id": None,
    "claim_token": None,
    "claim_expires_at": None,
    "claimed_at": None,
}


def schedule_record_digest(schedule: "ScheduledMessage") -> str:
    """The digest of a schedule as listed to people (the gateway's ``expected_record_digest``).

    Leaves out what runs change by themselves (``schedule_listing_digest``).
    """

    from custom_messages.contracts.schedule_v1 import schedule_listing_digest

    return schedule_listing_digest(schedule)


def _stored_message(row: Mapping[str, Any]) -> "ScheduledMessage":
    """The schedule as saved in the row's JSON, without the row's columns laid over it.

    Writes that change one part of the saved schedule start from this, so they
    never save the owner reconciliation of ``apply_persisted_schedule_owner``.
    A row saved before ``schedule_json`` existed is rebuilt from its columns.
    """

    from custom_messages.contracts.schedule_v1 import ScheduledMessage
    from custom_messages.contracts.task_v1 import Task

    if row.get("schedule_json"):
        return ScheduledMessage.model_validate_json(row["schedule_json"])
    return ScheduledMessage(
        schedule_id=row["schedule_id"],
        payload=Task.model_validate_json(row["payload_json"]),
        schedule_name=row.get("schedule_name"),
        interval_ms=row.get("interval_ms"),
        cron_expression=row.get("cron_expression"),
        repeat_count=row.get("repeat_count"),
        next_fire_at=row.get("next_fire_at"),
        paused_until=row.get("paused_until"),
        expires_at_utc=row.get("expires_at_utc"),
        created_at=row["created_at"],
        enabled=bool(row.get("enabled")),
        objective_id=row.get("objective_id"),
        claim_team_id=row.get("claim_team_id"),
        claim_named_agent_type=row.get("claim_named_agent_type"),
        claim_agent_id=row.get("claim_agent_id"),
    )


def _row_message(row: Mapping[str, Any], logger_: logging.Logger) -> "ScheduledMessage":
    """The schedule as the runtime reads it: the saved schedule, the row's columns, the row's owner."""

    schedule = _stored_message(row).model_copy(
        update={
            "schedule_name": row.get("schedule_name"),
            "interval_ms": row.get("interval_ms"),
            "cron_expression": row.get("cron_expression"),
            "repeat_count": row.get("repeat_count"),
            "next_fire_at": row.get("next_fire_at"),
            "paused_until": row.get("paused_until"),
            "created_at": row["created_at"],
            "enabled": bool(row.get("enabled")),
            "objective_id": row.get("objective_id"),
            "claim_team_id": row.get("claim_team_id"),
            "claim_named_agent_type": row.get("claim_named_agent_type"),
            "claim_agent_id": row.get("claim_agent_id"),
            "expires_at_utc": row.get("expires_at_utc"),
        }
    )
    return apply_persisted_schedule_owner(schedule, row.get("owner_canonical_user_id"), logger_=logger_)


def _definition_columns(msg: "ScheduledMessage", *, remaining_count: Optional[int]) -> dict[str, Any]:
    """The columns a schedule's definition fills; never the claim or the run permit."""

    return {
        "payload_json": msg.payload.model_dump_json(),
        "schedule_json": msg.model_dump_json(),
        "schedule_name": msg.schedule_name,
        "objective_id": msg.objective_id,
        "interval_ms": msg.interval_ms,
        "cron_expression": msg.cron_expression,
        "repeat_count": msg.repeat_count,
        "remaining_count": remaining_count,
        "next_fire_at": msg.next_fire_at,
        "paused_until": msg.paused_until,
        "expires_at_utc": msg.expires_at_utc,
        "created_at": msg.created_at,
        "enabled": 1 if msg.enabled else 0,
        "claim_team_id": msg.claim_team_id,
        "claim_named_agent_type": msg.claim_named_agent_type,
        "claim_agent_id": msg.claim_agent_id,
        "owner_canonical_user_id": owner_canonical_user_id_for_schedule(msg),
    }


def _next_run_changed(candidate: "ScheduledMessage", base: "ScheduledMessage") -> bool:
    """Whether a change sets a new next run time: new timing, or a new time itself."""

    fields = ("interval_ms", "cron_expression", "cron_timezone", "next_fire_at")
    return any(getattr(candidate, name) != getattr(base, name) for name in fields)


def _memory_json(memory: Optional["ScheduleTaskMemory"]) -> Optional[dict[str, Any]]:
    return memory.model_dump(mode="json") if memory is not None else None


def _mark_memory_changed(memory: "ScheduleTaskMemory") -> None:
    memory.updated_at = schedule_memory_now()
    trim_schedule_memory_ledger(memory)


def _edited_memory(
    stored: Optional["ScheduleTaskMemory"], edit: Callable[["ScheduleTaskMemory"], Any]
) -> "ScheduleTaskMemory":
    """A copy of the stored run notes (or new ones) with ``edit`` applied."""

    from custom_messages.contracts.schedule_v1 import ScheduleTaskMemory

    memory = stored.model_copy(deep=True) if stored is not None else ScheduleTaskMemory()
    edit(memory)
    _mark_memory_changed(memory)
    return memory


# Why a claimed schedule must not start its run (``schedule_stopped_reason``).
SCHEDULE_TURNED_OFF = "turned_off"
SCHEDULE_PAUSED = "paused"
SCHEDULE_ENDED = "ended"


def schedule_stopped_reason(schedule: "ScheduledMessage", now: datetime) -> Optional[str]:
    """Why a claimed schedule must not start a run now (turned off, paused or past its end), or None.

    Claiming checks the same, but control changes keep the claim, so a person
    can pause or turn off a schedule after it was claimed and before its run
    starts. Whoever starts or signs the run checks again with the schedule
    as it is now (``scheduled_runtime._fire_claimed_schedule``,
    ``scheduled_run_contexts``); typed root jobs are also checked at admission
    (``ScheduleWorkerRepository.commit_admission``).
    """

    if not schedule.enabled:
        return SCHEDULE_TURNED_OFF
    pause = _parse_utc_datetime(schedule.paused_until)
    if pause and pause > now:
        return SCHEDULE_PAUSED
    end = _parse_utc_datetime(schedule.expires_at_utc)
    if end and end <= now:
        return SCHEDULE_ENDED
    return None


def _claim_live(row: Mapping[str, Any], now: datetime) -> bool:
    expires = _parse_utc_datetime(row.get("claim_expires_at"))
    return bool(row.get("claimed_by_agent_id")) and expires is not None and expires > now


@dataclass(frozen=True)
class _AfterRun:
    """What follows a schedule's run: its next run time (None when it is finished) and count."""

    next_fire_at: Optional[str]
    remaining_count: Optional[int]
    reason: str


# A schedule with neither an interval nor a cron expression has no next run time.
NO_REPEAT_TIMING = "no_repeat_timing"


def _after_run(row: Mapping[str, Any], previous_due: Optional[str], now: datetime) -> _AfterRun:
    """The run time and count after one run of the row's schedule (``update_after_fire``).

    The next run is computed from the later of now and the run's due time, so
    runs missed while nothing ran are not made up.
    """

    expires = _parse_utc_datetime(row.get("expires_at_utc"))
    if expires and expires <= now:
        return _AfterRun(None, None, "expired")
    remaining = row.get("remaining_count")
    if remaining is not None:
        remaining -= 1
        if remaining <= 0:
            return _AfterRun(None, None, "last_run")
    if row.get("interval_ms") is None and not row.get("cron_expression"):
        return _AfterRun(None, None, NO_REPEAT_TIMING)
    previous = _parse_utc_datetime(previous_due)
    next_fire = compute_next_fire_at(
        interval_ms=row.get("interval_ms"),
        cron_expression=row.get("cron_expression"),
        from_time=max(now, previous) if previous else now,
        cron_timezone=_stored_message(row).cron_timezone,
        previous_fire_at=previous,
    )
    next_dt = _parse_utc_datetime(next_fire)
    if expires and next_dt and next_dt >= expires:
        return _AfterRun(None, None, "next_run_after_expiry")
    return _AfterRun(next_fire, remaining, "advanced")


def schedule_changed_since_read(schedule_id: str) -> ScheduleWriteConflict:
    """The refusal for a change based on a version of the schedule that is no longer current."""

    return ScheduleWriteConflict(
        SCHEDULE_CHANGED,
        schedule_id,
        f"Schedule {schedule_id} changed after it was read; nothing was changed. "
        "Read it again with list_scheduled_jobs and decide again from what it says now.",
    )


def _require_record_digest(current: "ScheduledMessage", expected: Optional[str]) -> None:
    """A change applies only to the version of the schedule the caller read (``schedule_record_digest``).

    People's controls in the gateway and the agent schedule tools pass the
    version they listed; trusted callers that pass none are not checked.
    """

    if expected and schedule_record_digest(current) != expected:
        raise schedule_changed_since_read(current.schedule_id)


# What ``ScheduleStore.add`` did (its return value and log).
SCHEDULE_INSTALLED = "installed"
SCHEDULE_REPLACED = "replaced"
SCHEDULE_KEPT = "kept"  # create_only found a stored schedule with the id and left it as it is


# A person set a new run time for a one-time schedule while its run was under way.
RETIMED_FINAL_RUN_KEPT = "retimed_final_run_kept"


def _after_retimed_run(row: Mapping[str, Any], *, release_claim: bool) -> RowWrite:
    """The advance after a run during which someone set a new next run time.

    The new run time stands and the claim is released. A one-time schedule
    (no interval or cron) stays at the new time with its one run: the
    person's latest instruction wins, as for a reminder moved to tomorrow
    (owner's decision 2026-10-17). A counted repeating series counts the run
    and finishes after its last one even when it was re-timed, so a series
    whose runs move their own next run time cannot run forever.
    """

    remaining = row.get("remaining_count")
    one_time = row.get("interval_ms") is None and not row.get("cron_expression")
    columns: dict[str, Any] = dict(_CLEARED_CLAIM) if release_claim else {}
    if remaining is not None and remaining - 1 <= 0:
        if not one_time:
            return RowWrite(delete=True, result=(AdvanceOutcome.FINISHED, _AfterRun(None, None, "last_run")))
        columns["remaining_count"] = 1
        kept = _AfterRun(row.get("next_fire_at"), 1, RETIMED_FINAL_RUN_KEPT)
        return RowWrite(columns=columns, result=(AdvanceOutcome.DUE_CHANGED, kept))
    if remaining is not None:
        columns["remaining_count"] = remaining - 1
    return RowWrite(columns=columns, result=(AdvanceOutcome.DUE_CHANGED, None))


def is_final_run(row: Mapping[str, Any]) -> bool:
    """Whether the row's next run is its last: one run left, or a one-time schedule."""

    remaining = row.get("remaining_count")
    recurring = row.get("interval_ms") is not None or bool(row.get("cron_expression"))
    return remaining == 1 or (not recurring and remaining is None)


class ScheduleStore:
    """Runtime-facing scheduled-message store backed by the selected repository."""

    def __init__(
        self,
        db_path: str = STORAGE_DEFAULTS.task_history_db_path.value,
        logger: logging.Logger = None,
        storage_settings: Optional[StorageSettings] = None,
        default_cron_timezone: Optional[str] = None,
    ):
        # The instance time zone, for cron schedules whose owner has none.
        self.default_cron_timezone = default_cron_timezone
        self._db_path = storage_settings.sqlite_path if storage_settings and storage_settings.sqlite_path else db_path
        self._logger = logger or logging.getLogger(__name__)
        self._storage_settings = storage_settings or resolve_storage_settings(
            {"backend": STORAGE_DEFAULTS.backend.value, STORAGE_BACKEND_SQLITE: {"path": db_path}}
        )
        self._repository = create_schedule_repository(
            self._storage_settings,
            logger=self._logger,
        )
        from storage.schedule_worker_repository import ScheduleWorkerRepository

        self.worker_records = ScheduleWorkerRepository(self._storage_settings)

    def add(
        self,
        msg: "ScheduledMessage",
        *,
        claim_team_id: Optional[str] = None,
        claim_named_agent_type: Optional[str] = None,
        claim_agent_id: Optional[str] = None,
        remaining_count_override: Any = _UNSET_REMAINING_COUNT,
        system_mutation_permit: Optional[ScheduleMutationPermit] = None,
        system_mutation_operation: ScheduleMutationOperation = (
            ScheduleMutationOperation.UPDATE
        ),
        system_mutation_request_binding: Optional[dict[str, Any]] = None,
        deferred_message: DeferredMessageClaim | None = None,
        create_only: bool = False,
    ) -> str:
        """Install a schedule: create it, or replace the stored definition of one with its id.

        For installers that own the whole definition (a new schedule, the
        gateway's installs, an Objective's work schedule). The stored claim is
        kept, and so is the remaining count unless the repeat count changed or
        ``remaining_count_override`` is given. A change to a copy of a schedule
        read earlier goes through ``update`` instead, which never recreates a
        deleted schedule. A stored system-managed schedule needs the exact
        permit, checked in the same transaction as the write.

        ``create_only`` creates the schedule or, when one with the id is
        stored, leaves it exactly as it is (``SCHEDULE_KEPT``). A model's
        ``schedule_message`` action uses it: the action may create a schedule
        but never replace one (``schedule_installation.create_schedule_from_turn_action``).
        Returns what was done: ``SCHEDULE_INSTALLED``, ``SCHEDULE_REPLACED``
        or ``SCHEDULE_KEPT``.
        """

        prepare_schedule_for_add(
            msg,
            claim_team_id=claim_team_id,
            claim_named_agent_type=claim_named_agent_type,
            claim_agent_id=claim_agent_id,
            default_cron_timezone=self.default_cron_timezone,
        )

        def decide(row: Optional[dict[str, Any]]) -> RowWrite:
            if row is None:
                remaining = msg.repeat_count if remaining_count_override is _UNSET_REMAINING_COUNT else remaining_count_override
                return RowWrite(columns=_definition_columns(msg, remaining_count=remaining), insert=True, result=SCHEDULE_INSTALLED)
            if create_only:
                return RowWrite(result=SCHEDULE_KEPT)
            current = _row_message(row, self._logger)
            self._require_replace_authority(
                current,
                msg,
                permit=system_mutation_permit,
                operation=system_mutation_operation,
                request_binding=system_mutation_request_binding,
            )
            if remaining_count_override is not _UNSET_REMAINING_COUNT:
                remaining = remaining_count_override
            elif msg.repeat_count != row.get("repeat_count"):
                remaining = msg.repeat_count
            else:
                remaining = row.get("remaining_count")
            written = msg.model_copy(deep=True)
            written.timing_revision = current.timing_revision + int(_next_run_changed(msg, current))
            return RowWrite(columns=_definition_columns(written, remaining_count=remaining), result=SCHEDULE_REPLACED)

        try:
            write = self._repository.write_locked(
                msg.schedule_id, decide, deferred_message=deferred_message,
            )
        except SystemScheduleMutationDenied:
            raise
        except Exception as e:
            self._logger.error(f"Failed to add schedule {msg.schedule_id}: {e}")
            raise
        outcome: str = write.result
        self._logger.info(
            "Schedule %s %s, next fire at %s", msg.schedule_id, outcome, msg.next_fire_at
        )
        return outcome

    @staticmethod
    def _require_replace_authority(
        current: "ScheduledMessage",
        candidate: "ScheduledMessage",
        *,
        permit: Optional[ScheduleMutationPermit],
        operation: ScheduleMutationOperation,
        request_binding: Optional[dict[str, Any]],
    ) -> None:
        """A stored system-managed schedule stays system-managed and changes only with its exact permit."""

        if not is_system_managed_schedule(current):
            return
        if candidate.management_class != ScheduleManagementClass.SYSTEM:
            raise SystemScheduleMutationDenied(
                "system_schedule_provenance_downgrade_denied",
                "A system-managed schedule cannot be replaced as user-managed.",
            )
        if schedule_control_state_digest(current) != schedule_control_state_digest(candidate):
            require_schedule_mutation_permit(
                current,
                operation=operation,
                permit=permit,
                candidate=candidate,
                request_binding=request_binding,
            )

    def update(
        self,
        candidate: "ScheduledMessage",
        *,
        base: "ScheduledMessage",
        remaining_count: Any = _UNSET_REMAINING_COUNT,
        memory_edit: Optional[Callable[["ScheduleTaskMemory"], Any]] = None,
        system_mutation_permit: Optional[ScheduleMutationPermit] = None,
        system_mutation_operation: ScheduleMutationOperation = ScheduleMutationOperation.UPDATE,
        system_mutation_request_binding: Optional[dict[str, Any]] = None,
    ) -> None:
        """Save a change made to ``base``, a copy of the schedule read earlier, without overwriting anything newer.

        Refused with ``ScheduleWriteConflict`` when the schedule is gone (it
        is never recreated) or when another change reached what a person
        reviews (``schedule_reviewed_state_digest``) since ``base`` was read.
        Runs writing notes or moving a repeating run time do not invalidate
        unrelated edits. An explicit retiming requires the run time still
        read by the caller, so it cannot overwrite another retiming. Kept
        from the stored row unless the change itself changed
        them: the next run time (new timing or a new time), the remaining
        count (a new repeat count, or ``remaining_count`` when the change
        sets the count again) and the run notes. A policy change to the run
        notes is given as ``memory_edit`` and applied to the notes as stored
        (no I/O). The claim is always kept. A system-managed schedule needs
        the exact permit for its current state, checked in the same
        transaction.
        """

        prepare_schedule_for_add(candidate, default_cron_timezone=self.default_cron_timezone)
        schedule_id = candidate.schedule_id

        def decide(row: Optional[dict[str, Any]]) -> RowWrite:
            if row is None:
                raise ScheduleWriteConflict(
                    SCHEDULE_GONE, schedule_id, f"Schedule {schedule_id} no longer exists; nothing was saved."
                )
            current = _row_message(row, self._logger)
            if schedule_reviewed_state_digest(current) != schedule_reviewed_state_digest(base) or (
                _next_run_changed(candidate, base) and current.next_fire_at != base.next_fire_at
            ):
                raise schedule_changed_since_read(schedule_id)
            self._require_replace_authority(
                current,
                candidate,
                permit=system_mutation_permit,
                operation=system_mutation_operation,
                request_binding=system_mutation_request_binding,
            )
            written = candidate.model_copy(deep=True)
            # A repeating run advances next_fire_at without changing this
            # revision. Explicit retimes change it, so the listing digest
            # distinguishes them even though it excludes the live run time.
            written.timing_revision = current.timing_revision + int(_next_run_changed(candidate, base))
            if not _next_run_changed(candidate, base):
                written.next_fire_at = row["next_fire_at"]
            if memory_edit is not None:
                written.schedule_memory = _edited_memory(_stored_message(row).schedule_memory, memory_edit)
            elif _memory_json(candidate.schedule_memory) == _memory_json(base.schedule_memory):
                written.schedule_memory = _stored_message(row).schedule_memory
            if remaining_count is not _UNSET_REMAINING_COUNT:
                remaining = remaining_count
            elif candidate.repeat_count != base.repeat_count:
                remaining = candidate.repeat_count
            else:
                remaining = row.get("remaining_count")
            return RowWrite(columns=_definition_columns(written, remaining_count=remaining), result=written.next_fire_at)

        write = self._repository.write_locked(schedule_id, decide)
        self._logger.info("Schedule %s changed, next fire at %s", schedule_id, write.result)

    def add_if_absent(
        self,
        msg: "ScheduledMessage",
        *,
        claim_team_id: Optional[str] = None,
        claim_named_agent_type: Optional[str] = None,
        claim_agent_id: Optional[str] = None,
        remaining_count_override: Any = _UNSET_REMAINING_COUNT,
    ) -> Optional[bool]:
        """Atomically install a schedule once while preserving an existing row verbatim."""

        prepare_schedule_for_add(
            msg,
            claim_team_id=claim_team_id,
            claim_named_agent_type=claim_named_agent_type,
            claim_agent_id=claim_agent_id,
            default_cron_timezone=self.default_cron_timezone,
        )

        remaining = (
            msg.repeat_count
            if remaining_count_override is _UNSET_REMAINING_COUNT
            else remaining_count_override
        )
        try:
            created = self._repository.add_schedule_row_if_absent(
                schedule_id=msg.schedule_id,
                payload_json=msg.payload.model_dump_json(),
                schedule_json=msg.model_dump_json(),
                schedule_name=msg.schedule_name,
                objective_id=getattr(msg, "objective_id", None),
                interval_ms=msg.interval_ms,
                cron_expression=msg.cron_expression,
                repeat_count=msg.repeat_count,
                remaining_count=remaining,
                next_fire_at=msg.next_fire_at,
                paused_until=msg.paused_until,
                expires_at_utc=getattr(msg, "expires_at_utc", None),
                created_at=msg.created_at,
                enabled=msg.enabled,
                claim_team_id=getattr(msg, "claim_team_id", None),
                claim_named_agent_type=getattr(msg, "claim_named_agent_type", None),
                claim_agent_id=getattr(msg, "claim_agent_id", None),
                owner_canonical_user_id=owner_canonical_user_id_for_schedule(msg),
            )
            if created:
                self._logger.info(
                    "Schedule %s installed once, next fire at %s",
                    msg.schedule_id,
                    msg.next_fire_at,
                )
            else:
                self._logger.info(
                    "Schedule %s already exists; preserved persisted timing and state.",
                    msg.schedule_id,
                )
            return created
        except Exception as exc:
            self._logger.error(
                "Failed to ensure schedule %s without replacement: %s",
                msg.schedule_id,
                exc,
            )
            return None

    def remove(
        self,
        schedule_id: str,
        *,
        system_mutation_permit: Optional[ScheduleMutationPermit] = None,
        system_mutation_request_binding: Optional[dict[str, Any]] = None,
        expected_record_digest: Optional[str] = None,
    ) -> bool:
        """Delete a schedule; False when there is none (or storage failed, logged).

        The permit and ``expected_record_digest`` (the listed schedule a person
        acted on, ``ScheduleWriteConflict`` when it changed) are checked in the
        same transaction as the delete.
        """

        def decide(row: Optional[dict[str, Any]]) -> RowWrite:
            if row is None:
                return RowWrite(result=False)
            current = _row_message(row, self._logger)
            _require_record_digest(current, expected_record_digest)
            require_schedule_mutation_permit(
                current,
                operation=ScheduleMutationOperation.DELETE,
                permit=system_mutation_permit,
                request_binding=system_mutation_request_binding,
            )
            return RowWrite(delete=True, result=True)

        deleted = self._write_or_log(schedule_id, decide, action="remove")
        if deleted:
            self._logger.info(f"Schedule {schedule_id} removed.")
            self._end_schedule_access(schedule_id)
        else:
            self._logger.warning(f"Schedule {schedule_id} not found for removal.")
        return deleted

    def _write_or_log(self, schedule_id: str, decide: Callable[[Optional[dict[str, Any]]], RowWrite], *, action: str) -> bool:
        """Run a control change; refusals propagate, a storage failure is logged and reads as not changed."""

        try:
            return bool(self._repository.write_locked(schedule_id, decide).result)
        except (SystemScheduleMutationDenied, ScheduleWriteConflict):
            raise
        except Exception:
            self._logger.exception(
                "A schedule change failed in storage. event=schedule_write_failed schedule_id=%s action=%s",
                schedule_id,
                action,
            )
            return False

    def _end_schedule_access(self, schedule_id: str) -> None:
        """End the access a deleted schedule holds (``SubjectKind.SCHEDULE`` grants).

        Every deletion calls it: ``remove``, the advance that removes a
        finished schedule (``update_after_fire``) and ``prune_expired``. A
        failure is logged; the grants stay listed but no run can use them.
        """

        from storage.access_repository import AccessRepository

        try:
            ended = AccessRepository(self._storage_settings).end_schedule_grants(
                schedule_id, at=datetime.now(timezone.utc)
            )
        except Exception:  # noqa: BLE001 - the schedule is deleted; only its listing lags
            self._logger.exception(
                "Could not end the access of a deleted schedule. event=schedule_grants_end_failed schedule_id=%s",
                schedule_id,
            )
            return
        if ended:
            self._logger.info(
                "Access tied to a deleted schedule ended. event=schedule_grants_ended schedule_id=%s grant_ids=%s",
                schedule_id,
                ",".join(ended),
            )

    def update_after_fire(
        self,
        schedule_id: str,
        *,
        claim_token: Optional[str] = None,
        claimer_agent_id: Optional[str] = None,
        due_at: Optional[str] = None,
    ) -> AdvanceOutcome:
        """Move a schedule on after a run was admitted: its next run time and count, or remove it.

        In one write transaction, and only while this agent still holds the
        claim (when given) and the next run time is still ``due_at``, the run
        that was claimed. An edit that set a new run time during the run wins
        and the claim is released: the run counts against a counted series
        (which finishes after its last run), and a one-time schedule stays at
        the new time (``_after_retimed_run``). Writes only the run time, the
        remaining count and the claim, so a pause or turn-off made during the
        run stays. The next run is computed from the later of now and
        ``due_at``. The caller logs an outcome that is not ``applied``.
        """

        now = datetime.now(timezone.utc)

        def decide(row: Optional[dict[str, Any]]) -> RowWrite:
            if row is None:
                return RowWrite(result=(AdvanceOutcome.NOT_FOUND, None))
            if claim_token and claimer_agent_id and (
                row.get("claim_token") != claim_token or row.get("claimed_by_agent_id") != claimer_agent_id
            ):
                return RowWrite(result=(AdvanceOutcome.CLAIM_LOST, None))
            if due_at is not None and row.get("next_fire_at") != due_at:
                return _after_retimed_run(row, release_claim=bool(claim_token))
            after = _after_run(row, due_at or row.get("next_fire_at"), now)
            if after.next_fire_at is None:
                return RowWrite(delete=True, result=(AdvanceOutcome.FINISHED, after))
            columns = {"next_fire_at": after.next_fire_at, "remaining_count": after.remaining_count, **_CLEARED_CLAIM}
            return RowWrite(columns=columns, result=(AdvanceOutcome.ADVANCED, after))

        try:
            result: tuple[AdvanceOutcome, Optional[_AfterRun]] = self._repository.write_locked(schedule_id, decide).result
        except Exception:
            self._logger.exception(
                "Could not move a schedule on after its run. event=schedule_advance_failed "
                "schedule_id=%s due_at=%s agent_id=%s",
                schedule_id,
                due_at,
                claimer_agent_id,
            )
            return AdvanceOutcome.FAILED
        outcome, after = result
        self._log_advance(schedule_id, outcome, after, due_at=due_at, agent_id=claimer_agent_id)
        if outcome is AdvanceOutcome.FINISHED:
            self._end_schedule_access(schedule_id)
        return outcome

    def _log_advance(
        self,
        schedule_id: str,
        outcome: AdvanceOutcome,
        after: Optional[_AfterRun],
        *,
        due_at: Optional[str],
        agent_id: Optional[str],
    ) -> None:
        """Log an advance that applied, and a re-timed one-time schedule that was kept.

        The caller logs every outcome that is not ``applied``
        (``scheduled_runtime``).
        """

        if after is not None and after.reason == RETIMED_FINAL_RUN_KEPT:
            self._logger.info(
                "A one-time schedule's run time was changed while its run was under way; it stays at the new "
                "time. event=schedule_retimed_during_final_run_kept schedule_id=%s due_at=%s next_fire_at=%s "
                "agent_id=%s",
                schedule_id,
                due_at,
                after.next_fire_at,
                agent_id,
            )
        elif outcome is AdvanceOutcome.ADVANCED and after is not None:
            remaining = "infinite" if after.remaining_count is None else str(after.remaining_count)
            self._logger.info(
                "Schedule %s updated: next fire at %s, remaining: %s", schedule_id, after.next_fire_at, remaining
            )
        elif outcome is AdvanceOutcome.FINISHED and after is not None and after.reason == NO_REPEAT_TIMING:
            self._logger.warning(
                "A schedule with no interval or cron expression still had runs left; it runs only once, "
                "so it was removed after this run. event=schedule_one_time_count_ignored schedule_id=%s",
                schedule_id,
            )
        elif outcome is AdvanceOutcome.FINISHED and after is not None:
            self._logger.info("Schedule %s finished after its run (%s); removed.", schedule_id, after.reason)

    def get_all_enabled(self) -> List[dict]:
        try:
            self.prune_expired()
            return self._repository.get_all_enabled()
        except Exception as e:
            self._logger.error(f"Failed to query enabled schedules: {e}")
            return []

    def get_all(self) -> List[dict]:
        try:
            self.prune_expired()
            return self._repository.get_all()
        except Exception as e:
            self._logger.error(f"Failed to query schedules: {e}")
            return []

    def all_rows(self) -> List[dict]:
        """Every schedule row, enabled or not; storage errors propagate.

        Unlike get_all, a failure raises, so callers deciding whether to rerun
        or delete work never mistake an unreadable store for an empty one.
        """
        return self._repository.get_all()

    def fill_missing_cron_timezones(self) -> list[tuple[str, str]]:
        """Give each cron schedule saved without a time zone its owner's, once (owner's decision 2026-09-27).

        Cron schedules used to run in UTC. The first manager start after the
        upgrade sets each one's ``cron_timezone`` as a new schedule would get
        it (``fill_cron_timezone``: the owner's request time zone, else the
        instance's) and moves its next run to that local time. This is the
        scheduler's own maintenance, like after-run bookkeeping, so it needs
        no schedule permit; each change is logged. Returns (schedule id, zone).
        """

        filled: list[tuple[str, str]] = []
        for row in self.all_rows():
            if not row.get("cron_expression") or not row.get("schedule_json"):
                continue
            write = self._repository.write_locked(str(row["schedule_id"]), self._fill_cron_timezone)
            if write.result is None:
                continue
            schedule_id, zone, next_fire_at, cron = write.result
            self._logger.warning(
                "Gave a cron schedule saved without a time zone its owner's time zone. "
                "event=schedule_cron_timezone_filled schedule_id=%s cron=%r timezone=%s next_fire_at=%s",
                schedule_id,
                cron,
                zone,
                next_fire_at,
            )
            filled.append((schedule_id, zone))
        return filled

    def _fill_cron_timezone(self, row: Optional[dict[str, Any]]) -> RowWrite:
        """One row of ``fill_missing_cron_timezones``, decided on the row as it is now."""

        if row is None or not row.get("cron_expression") or not row.get("schedule_json"):
            return RowWrite()
        schedule = _stored_message(row)
        zone = fill_cron_timezone(schedule, self.default_cron_timezone)
        if zone is None:
            return RowWrite()
        schedule.next_fire_at = compute_next_fire_at(cron_expression=schedule.cron_expression, cron_timezone=zone)
        return RowWrite(
            columns={"schedule_json": schedule.model_dump_json(), "next_fire_at": schedule.next_fire_at},
            result=(schedule.schedule_id, zone, schedule.next_fire_at, schedule.cron_expression),
        )

    def get_row(self, schedule_id: str) -> Optional[dict]:
        """One schedule row, enabled or not; storage errors propagate (``all_rows``)."""
        return next(
            (row for row in self.all_rows() if row.get("schedule_id") == schedule_id),
            None,
        )

    def prune_expired(self, *, now: Optional[datetime] = None) -> int:
        """Remove schedules whose explicit expiration timestamp has passed."""
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            pruned = self._repository.prune_expired(now_iso=now_dt.isoformat())
        except Exception as e:
            self._logger.error(f"Failed to prune expired schedules: {e}")
            return 0
        if pruned:
            self._logger.warning(
                "Pruned %s expired scheduled message%s at %s. schedule_ids=%s",
                len(pruned),
                "" if len(pruned) == 1 else "s",
                now_dt.isoformat(),
                ",".join(pruned),
            )
        for schedule_id in pruned:
            self._end_schedule_access(schedule_id)
        return len(pruned)

    def build_telemetry_snapshot(
        self,
        *,
        include_schedules: bool = True,
        now: Optional[datetime] = None,
    ) -> dict:
        """Return operator-facing schedule telemetry from persisted schedule state."""
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        rows = self.get_all()
        summary: ScheduleTelemetrySummary = {
            "total": len(rows),
            "enabled": 0,
            "disabled": 0,
            "paused": 0,
            "due": 0,
            "recurring": 0,
            "one_shot": 0,
            "with_interval": 0,
            "with_cron": 0,
            "load_errors": 0,
            "next_fire_at": None,
        }
        schedules: list[dict] = []

        for row in rows:
            schedule_id = row.get("schedule_id")
            enabled = bool(row.get("enabled"))
            next_fire_dt = _parse_utc_datetime(row.get("next_fire_at"))
            paused_until_dt = _parse_utc_datetime(row.get("paused_until"))
            is_paused = bool(paused_until_dt and paused_until_dt > now_dt)
            is_due = bool(enabled and not is_paused and next_fire_dt and next_fire_dt <= now_dt)
            interval_ms = row.get("interval_ms")
            cron_expression = row.get("cron_expression")
            repeat_count = row.get("repeat_count")

            summary["enabled" if enabled else "disabled"] += 1
            if is_paused:
                summary["paused"] += 1
            if is_due:
                summary["due"] += 1
            if interval_ms is not None:
                summary["with_interval"] += 1
            if cron_expression:
                summary["with_cron"] += 1
            if repeat_count is None or interval_ms is not None or cron_expression:
                summary["recurring"] += 1
            else:
                summary["one_shot"] += 1
            if enabled and not is_paused and next_fire_dt:
                if summary["next_fire_at"] is None or next_fire_dt.isoformat() < summary["next_fire_at"]:
                    summary["next_fire_at"] = next_fire_dt.isoformat()

            if not include_schedules:
                continue

            scheduled_message = self.get_scheduled_message(schedule_id) if schedule_id else None
            if scheduled_message is None:
                summary["load_errors"] += 1
                schedules.append(
                    {
                        "schedule_id": schedule_id,
                        "load_error": True,
                        "enabled": enabled,
                        "next_fire_at": row.get("next_fire_at"),
                    }
                )
                continue

            header = scheduled_message.payload_header
            meta = scheduled_message.payload_meta
            gateway_context = getattr(meta, "gateway_context", None) if meta is not None else None
            payload = scheduled_message.payload
            schedules.append(
                {
                    "schedule_id": scheduled_message.schedule_id,
                    "schedule_name": scheduled_message.schedule_name,
                    "management_class": scheduled_message.management_class.value,
                    "system_schedule_kind": (
                        scheduled_message.effective_system_schedule_kind.value
                        if scheduled_message.effective_system_schedule_kind is not None
                        else None
                    ),
                    "enabled": scheduled_message.enabled,
                    "paused": is_paused,
                    "due": is_due,
                    "created_at": scheduled_message.created_at,
                    "next_fire_at": scheduled_message.next_fire_at,
                    "paused_until": scheduled_message.paused_until,
                    "expires_at_utc": scheduled_message.expires_at_utc,
                    "interval_ms": scheduled_message.interval_ms,
                    "interval_seconds": scheduled_message.interval_ms / 1000
                    if scheduled_message.interval_ms is not None
                    else None,
                    "cron_expression": scheduled_message.cron_expression,
                    "repeat_count": scheduled_message.repeat_count,
                    "remaining_count": row.get("remaining_count"),
                    "objective_id": scheduled_message.objective_id,
                    "claim_scope": {
                        "team_id": scheduled_message.claim_team_id,
                        "named_agent_type": scheduled_message.claim_named_agent_type,
                        "agent_id": scheduled_message.claim_agent_id,
                    },
                    "claim_state": {
                        "claimed_by_agent_id": row.get("claimed_by_agent_id"),
                        "claim_expires_at": row.get("claim_expires_at"),
                        "claimed_at": row.get("claimed_at"),
                    },
                    "payload_task_id": getattr(payload, "task_id", None),
                    "payload_parent_task_id": getattr(payload, "parent_task_id", None),
                    "problem_preview": _preview_text(getattr(payload, "problem_statement", None)),
                    "schedule_memory": scheduled_message.schedule_memory.model_dump(mode="json")
                    if getattr(scheduled_message, "schedule_memory", None) is not None
                    else None,
                    "scheduled_by": {
                        "from_agent_id": getattr(header, "from_agent_id", None) if header is not None else None,
                        "from_agent_name": getattr(meta, "from_agent_name", None) if meta is not None else None,
                        "requester_type": _enum_value(getattr(meta, "requester_type", None))
                        if meta is not None
                        else None,
                        "conversation_id": getattr(meta, "conversation_id", None) if meta is not None else None,
                        "followup_to_task_id": getattr(meta, "followup_to_task_id", None) if meta is not None else None,
                    },
                    "target": {
                        "to_routing_path": _enum_value(getattr(header, "to_routing_path", None))
                        if header is not None
                        else None,
                        "to_agent_name": getattr(meta, "to_agent_name", None) if meta is not None else None,
                        "to_named_agent_type": getattr(meta, "to_named_agent_type", None) if meta is not None else None,
                        "response_routing_key": getattr(header, "response_routing_key", None)
                        if header is not None
                        else None,
                    },
                    "gateway_context": {
                        "gateway_instance_name": getattr(gateway_context, "gateway_instance_name", None),
                        "session_key": getattr(gateway_context, "session_key", None),
                        "channel_name": getattr(gateway_context, "channel_name", None),
                        "thread_id": getattr(gateway_context, "thread_id", None),
                        "scheduled_job_id": getattr(gateway_context, "scheduled_job_id", None),
                        "heartbeat_source_id": getattr(gateway_context, "heartbeat_source_id", None),
                    }
                    if gateway_context is not None
                    else None,
                }
            )

        return {
            "generated_at": now_dt.isoformat(),
            "summary": summary,
            "schedules": schedules if include_schedules else None,
        }

    def get_scheduled_message(self, schedule_id: str) -> Optional["ScheduledMessage"]:
        try:
            row = self._row(schedule_id)
            return _row_message(row, self._logger) if row is not None else None
        except Exception as e:
            self._logger.error(f"Failed to load scheduled message {schedule_id}: {e}")
            return None

    def _row(self, schedule_id: str) -> Optional[dict[str, Any]]:
        """One schedule row by its column names (``get_schedule_row`` returns them in ``SCHEDULE_ROW_FIELDS`` order)."""

        from storage.schedule_repository import SCHEDULE_ROW_FIELDS

        row = self._repository.get_schedule_row(schedule_id)
        return {"schedule_id": schedule_id, **dict(zip(SCHEDULE_ROW_FIELDS, row))} if row else None

    def get_next_due(self) -> Optional[dict]:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            self.prune_expired()
            return self._repository.get_next_due(now_iso=now_iso)
        except Exception as e:
            self._logger.error(f"Failed to query next due schedule: {e}")
            return None

    def claim_next_due(
        self,
        *,
        claimer_agent_id: str,
        claimer_named_agent_type: Optional[str],
        claimer_team_id: Optional[str],
        claim_ttl_seconds: int = DEFAULT_SCHEDULE_CLAIM_TTL_SECONDS,
        allow_unscoped: bool = False,
        exclude_schedule_ids: tuple[str, ...] = (),
    ) -> Optional[dict]:
        now_dt = datetime.now(timezone.utc)
        claim_expires_at = (
            now_dt + timedelta(seconds=max(1, int(claim_ttl_seconds)))
        ).isoformat()
        claim_token = new_id("sched_claim")
        try:
            self.prune_expired(now=now_dt)
            return self._repository.claim_next_due(
                now_iso=now_dt.isoformat(),
                claim_expires_at=claim_expires_at,
                claimer_agent_id=claimer_agent_id,
                claimer_named_agent_type=claimer_named_agent_type,
                claimer_team_id=claimer_team_id,
                claim_token=claim_token,
                allow_unscoped=allow_unscoped,
                exclude_schedule_ids=exclude_schedule_ids,
            )
        except Exception as e:
            self._logger.error(f"Failed to claim next due schedule: {e}")
            return None

    def release_claim(
        self,
        schedule_id: str,
        *,
        claim_token: str,
        claimer_agent_id: str,
    ) -> bool:
        try:
            released = self._repository.release_claim(
                schedule_id=schedule_id,
                claim_token=claim_token,
                claimer_agent_id=claimer_agent_id,
            )
            if released:
                self._logger.debug(f"Schedule {schedule_id} claim released.")
            return released
        except Exception as e:
            self._logger.error(f"Failed to release schedule {schedule_id} claim: {e}")
            return False

    def holds_claim(
        self,
        schedule_id: str,
        *,
        claim_token: str,
        claimer_agent_id: str,
        now: Optional[datetime] = None,
    ) -> bool:
        """Whether this agent holds this unexpired claim on the schedule row.

        The manager checks a worker's claim before it signs the run's task
        context (``custom_agents.scheduled_run_contexts``). Storage errors
        propagate so the caller refuses and logs rather than guessing.
        """

        row = self._row(schedule_id)
        if not row or not claim_token or not claimer_agent_id:
            return False
        expires_at = _parse_utc_datetime(row.get("claim_expires_at"))
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return (
            row.get("claim_token") == claim_token
            and row.get("claimed_by_agent_id") == claimer_agent_id
            and expires_at is not None
            and expires_at > moment
        )

    def get_next_wake_time(self) -> Optional[str]:
        try:
            self.prune_expired()
            return self._repository.get_next_wake_time()
        except Exception as e:
            self._logger.error(f"Failed to query next wake time: {e}")
            return None

    def get_earliest_fire_time(self) -> Optional[str]:
        return self.get_next_wake_time()

    def set_enabled(
        self,
        schedule_id: str,
        enabled: bool,
        *,
        operation: Optional[ScheduleMutationOperation] = None,
        system_mutation_permit: Optional[ScheduleMutationPermit] = None,
        system_mutation_request_binding: Optional[dict[str, Any]] = None,
        expected_record_digest: Optional[str] = None,
    ) -> bool:
        """Turn a schedule on or off (``_change_controls``)."""

        return self._change_controls(
            schedule_id,
            {"enabled": enabled},
            operation=operation or (ScheduleMutationOperation.ENABLE if enabled else ScheduleMutationOperation.DISABLE),
            permit=system_mutation_permit,
            request_binding=system_mutation_request_binding,
            expected_record_digest=expected_record_digest,
        )

    def set_paused_until(
        self,
        schedule_id: str,
        paused_until: Optional[str],
        *,
        operation: Optional[ScheduleMutationOperation] = None,
        system_mutation_permit: Optional[ScheduleMutationPermit] = None,
        system_mutation_request_binding: Optional[dict[str, Any]] = None,
        expected_record_digest: Optional[str] = None,
    ) -> bool:
        """Pause a schedule until a time, or clear its pause (``_change_controls``)."""

        return self._change_controls(
            schedule_id,
            {"paused_until": paused_until},
            operation=operation or (ScheduleMutationOperation.PAUSE if paused_until else ScheduleMutationOperation.RESUME),
            permit=system_mutation_permit,
            request_binding=system_mutation_request_binding,
            expected_record_digest=expected_record_digest,
        )

    def resume(
        self,
        schedule_id: str,
        *,
        system_mutation_permit: Optional[ScheduleMutationPermit] = None,
        system_mutation_request_binding: Optional[dict[str, Any]] = None,
        expected_record_digest: Optional[str] = None,
    ) -> bool:
        """Clear a schedule's pause and turn it on, as one change with one permit (``_change_controls``)."""

        return self._change_controls(
            schedule_id,
            {"paused_until": None, "enabled": True},
            operation=ScheduleMutationOperation.RESUME,
            permit=system_mutation_permit,
            request_binding=system_mutation_request_binding,
            expected_record_digest=expected_record_digest,
        )

    def _change_controls(
        self,
        schedule_id: str,
        changes: dict[str, Any],
        *,
        operation: ScheduleMutationOperation,
        permit: Optional[ScheduleMutationPermit],
        request_binding: Optional[dict[str, Any]],
        expected_record_digest: Optional[str],
    ) -> bool:
        """Change ``enabled`` and ``paused_until`` on the schedule as it is now; False when there is none.

        One write transaction checks ``expected_record_digest`` (the listed
        schedule a person acted on; ``ScheduleWriteConflict`` when it changed)
        and the permit against the current row, then writes only these fields.
        The claim is kept: a run that has not started is stopped where it
        starts (``schedule_stopped_reason``), and the advance after a run
        writes only the run time, count and claim.
        """

        def decide(row: Optional[dict[str, Any]]) -> RowWrite:
            if row is None:
                return RowWrite(result=False)
            current = _row_message(row, self._logger)
            _require_record_digest(current, expected_record_digest)
            require_schedule_mutation_permit(
                current,
                operation=operation,
                permit=permit,
                candidate=current.model_copy(deep=True, update=changes),
                request_binding=request_binding,
            )
            stored = _stored_message(row).model_copy(update=changes)
            columns = {**changes, "schedule_json": stored.model_dump_json()}
            if "enabled" in columns:
                columns["enabled"] = 1 if columns["enabled"] else 0
            return RowWrite(columns=columns, result=True)

        changed = self._write_or_log(schedule_id, decide, action=operation.value)
        if changed:
            self._logger.info("Schedule %s %s: %s", schedule_id, operation.value, changes)
        else:
            self._logger.warning("Schedule %s not found for %s.", schedule_id, operation.value)
        return changed

    def turn_off_missed_run(self, schedule_id: str, *, due_at: str, now: datetime) -> bool:
        """Turn off a schedule whose final run is too late, only if that run is still waiting.

        One conditional write: the schedule is still on, not paused and not
        ended, its next run time is still ``due_at``, that run is still its
        last (``is_final_run``: nobody raised the count meanwhile) and nobody
        holds a live claim (a worker may have just claimed and started it).
        System-managed schedules are never turned off here (no permit). False
        when nothing was changed.
        """

        def decide(row: Optional[dict[str, Any]]) -> RowWrite:
            if row is None or row.get("next_fire_at") != due_at or not is_final_run(row):
                return RowWrite(result=False)
            current = _row_message(row, self._logger)
            if schedule_stopped_reason(current, now) is not None or _claim_live(row, now):
                return RowWrite(result=False)
            require_schedule_mutation_permit(
                current,
                operation=ScheduleMutationOperation.DISABLE,
                permit=None,
                candidate=current.model_copy(deep=True, update={"enabled": False}),
            )
            stored = _stored_message(row).model_copy(update={"enabled": False})
            return RowWrite(columns={"enabled": 0, "schedule_json": stored.model_dump_json()}, result=True)

        return self._write_or_log(schedule_id, decide, action="turn_off_missed_run")

    def edit_schedule_memory(
        self,
        schedule_id: str,
        edit: Callable[["ScheduleTaskMemory"], Any],
        *,
        owner_guard: Optional[OwnerGuard] = None,
    ) -> Any:
        """Change only a schedule's run notes, on the notes as they are now; the edit's result, or None when gone.

        ``edit`` changes the given notes in place (an empty set when the
        schedule has none) and returns a result, which must not be None. It
        runs inside the write transaction, so it must do no I/O. Nothing is
        written when it changes nothing. With ``owner_guard``, the edit is
        refused (``ScheduleWriteConflict``) when the schedule's owner changed.
        """

        from custom_messages.contracts.schedule_v1 import ScheduleTaskMemory

        def decide(row: Optional[dict[str, Any]]) -> RowWrite:
            if row is None:
                return RowWrite(result=None)
            if owner_guard is not None and (row.get("owner_canonical_user_id") or None) != owner_guard.owner:
                raise ScheduleWriteConflict(
                    SCHEDULE_OWNER_CHANGED,
                    schedule_id,
                    f"Schedule {schedule_id} now belongs to someone else; its run notes were not changed.",
                )
            stored = _stored_message(row)
            memory = stored.schedule_memory.model_copy(deep=True) if stored.schedule_memory else ScheduleTaskMemory()
            before = _memory_json(memory)
            result = edit(memory)
            if _memory_json(memory) == before:
                return RowWrite(result=result)
            _mark_memory_changed(memory)
            stored.schedule_memory = memory
            return RowWrite(columns={"schedule_json": stored.model_dump_json()}, result=result)

        return self._repository.write_locked(schedule_id, decide).result

    def count_enabled(self) -> int:
        try:
            self.prune_expired()
            return self._repository.count_enabled()
        except Exception as e:
            self._logger.error(f"Failed to count enabled schedules: {e}")
            return 0

    # -- run permits (custom_agents.schedule_permits) ---------------------------------
    # Runtime bookkeeping kept on the row beside next_fire_at: never part of
    # schedule_json or the schedule's control state, so setting it needs no
    # schedule permit. Storage errors propagate: callers decide what a
    # missing answer means (the manager retries; a worker asks the manager).

    def run_permit(self, schedule_id: str) -> Optional["SignedSchedulePermitV1"]:
        """The schedule's stored run permit as the manager signed it, or None.

        Not verified here; whoever uses it verifies it. A stored value that is
        not a permit is logged and treated as none.
        """

        from custom_messages.contracts.access_v2 import SignedSchedulePermitV1

        row = self._row(schedule_id)
        raw = row.get("run_permit_json") if row else None
        if not raw:
            return None
        try:
            return SignedSchedulePermitV1.model_validate_json(raw)
        except ValueError:
            self._logger.warning(
                "A schedule's stored run permit is not readable; treating it as none. "
                "event=schedule_permit_unreadable schedule_id=%s",
                schedule_id,
            )
            return None

    def rows_needing_run_permit(self, *, now: datetime, limit: int) -> List[str]:
        """Schedule ids whose run permit the manager should issue, renew or re-check (one query)."""

        return self._repository.rows_needing_run_permit(now_iso=now.astimezone(timezone.utc).isoformat(), limit=limit)

    def set_run_permit(
        self,
        schedule_id: str,
        *,
        permit: Optional["SignedSchedulePermitV1"],
        checked_owner: Optional[str],
        recheck_at: datetime,
    ) -> bool:
        """Record the manager's check: the permit it issued (or None) and when to look again.

        False when the row is gone or its owner is no longer ``checked_owner``.
        """

        return self._repository.set_run_permit(
            schedule_id,
            permit_json=permit.model_dump_json() if permit is not None else None,
            checked_owner=checked_owner,
            recheck_at=recheck_at.astimezone(timezone.utc).isoformat(),
        )
