"""
Persistent storage and scheduling logic for ScheduledMessage objects.

Schedules are ordered by next_fire_at (earliest first) and can use
fixed intervals (interval_ms) or cron expressions for recurring execution.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import TYPE_CHECKING, Any, List, Optional, TypedDict

from storage.backends import STORAGE_BACKEND_SQLITE
from storage.runtime_custody import DeferredMessageClaim
from custom_agents.schedule_management import (
    ScheduleMutationOperation,
    ScheduleMutationPermit,
    SystemScheduleMutationDenied,
    is_system_managed_schedule,
    require_schedule_mutation_permit,
    schedule_control_state_digest,
)
from custom_messages.enums import ScheduleManagementClass
from custom_messages.contracts.routing_v1 import PERSON_GATEWAY_FIELDS
from default_values import STORAGE_DEFAULTS
from id_generation import new_id
from storage.config import StorageSettings, resolve_storage_settings
from storage.schedule_repository import create_schedule_repository

if TYPE_CHECKING:
    from custom_messages.contracts.access_v2 import SignedSchedulePermitV1
    from custom_messages.contracts.schedule_v1 import ScheduledMessage

logger = logging.getLogger(__name__)


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


def schedule_claim_scope_for_agent(agent: Any) -> dict[str, Optional[str]]:
    """Return the default persisted claim scope for schedules owned by an agent."""
    named_agent_type = getattr(agent, "named_agent_type", None)
    return {
        "claim_team_id": getattr(agent, "team_id", None),
        "claim_named_agent_type": named_agent_type,
        "claim_agent_id": None if named_agent_type else getattr(agent, "id", None),
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
) -> str:
    """
    Compute the next fire time as an ISO 8601 UTC string.

    A cron expression is read in ``cron_timezone``: "0 9 * * *" is 9:00 there,
    also across daylight saving changes (croniter works in the zone of its
    start time). A cron expression without a time zone is refused; the store
    fills one when a schedule is added (``prepare_schedule_for_add``).
    """
    if from_time is None:
        from_time = datetime.now(timezone.utc)

    if interval_ms is not None:
        next_time = from_time + timedelta(milliseconds=interval_ms)
        return next_time.isoformat()

    if cron_expression is not None:
        if not cron_timezone:
            raise ValueError(f"The cron schedule {cron_expression!r} has no time zone.")
        from croniter import croniter

        if from_time.tzinfo is None:
            from_time = from_time.replace(tzinfo=timezone.utc)
        local_start = from_time.astimezone(ZoneInfo(cron_timezone))
        next_time = croniter(cron_expression, local_start).get_next(datetime)
        return next_time.astimezone(timezone.utc).isoformat()

    raise ValueError("Either interval_ms or cron_expression must be provided.")


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
    ) -> None:
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
        existing = self.get_scheduled_message(msg.schedule_id)
        if existing is not None and is_system_managed_schedule(existing):
            if msg.management_class != ScheduleManagementClass.SYSTEM:
                raise SystemScheduleMutationDenied(
                    "system_schedule_provenance_downgrade_denied",
                    "A system-managed schedule cannot be replaced as user-managed.",
                )
            if schedule_control_state_digest(existing) != schedule_control_state_digest(
                msg
            ):
                require_schedule_mutation_permit(
                    existing,
                    operation=system_mutation_operation,
                    permit=system_mutation_permit,
                    candidate=msg,
                    request_binding=system_mutation_request_binding,
                )

        try:
            self._repository.add_schedule_row(
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
                deferred_message=deferred_message,
            )
            self._logger.info(
                f"Schedule {msg.schedule_id} added, next fire at {msg.next_fire_at}"
            )
        except Exception as e:
            self._logger.error(f"Failed to add schedule {msg.schedule_id}: {e}")
            raise

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
    ) -> bool:
        scheduled_message = self.get_scheduled_message(schedule_id)
        if scheduled_message is not None:
            require_schedule_mutation_permit(
                scheduled_message,
                operation=ScheduleMutationOperation.DELETE,
                permit=system_mutation_permit,
                request_binding=system_mutation_request_binding,
            )
        try:
            deleted = self._repository.remove(schedule_id)
            if deleted:
                self._logger.info(f"Schedule {schedule_id} removed.")
            else:
                self._logger.warning(f"Schedule {schedule_id} not found for removal.")
        except Exception as e:
            self._logger.error(f"Failed to remove schedule {schedule_id}: {e}")
            return False
        if deleted:
            self._end_schedule_access(schedule_id)
        return deleted

    def _end_schedule_access(self, schedule_id: str) -> None:
        """End the access a deleted schedule holds (``SubjectKind.SCHEDULE`` grants).

        Every deletion comes through ``remove``, so this is the one place. A
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
    ) -> bool:
        try:
            row = self._repository.get_schedule_row(schedule_id)
            if not row:
                self._logger.warning(f"Schedule {schedule_id} not found for update.")
                return False

            interval_ms = row[3]
            cron_expression = row[4]
            cron_timezone = json.loads(row[0]).get("cron_timezone") if row[0] else None
            remaining_count = row[10]
            expires_at_utc = row[19] if len(row) > 19 else None
            now_dt = datetime.now(timezone.utc)
            expires_at_dt = _parse_utc_datetime(expires_at_utc)

            if expires_at_dt and expires_at_dt <= now_dt:
                self._repository.update_after_fire(
                    schedule_id=schedule_id,
                    next_fire_at=None,
                    remaining_count=None,
                    claim_token=claim_token,
                    claimer_agent_id=claimer_agent_id,
                )
                self._logger.info(
                    f"Schedule {schedule_id} expired at {expires_at_dt.isoformat()} after fire. Removed."
                )
                return False

            if remaining_count is not None:
                remaining_count -= 1
                if remaining_count <= 0:
                    self._repository.update_after_fire(
                        schedule_id=schedule_id,
                        next_fire_at=None,
                        remaining_count=None,
                        claim_token=claim_token,
                        claimer_agent_id=claimer_agent_id,
                    )
                    self._logger.info(
                        f"Schedule {schedule_id} exhausted after final fire. Removed."
                    )
                    return False

            next_fire = compute_next_fire_at(
                interval_ms=interval_ms,
                cron_expression=cron_expression,
                cron_timezone=cron_timezone,
            )
            next_fire_dt = _parse_utc_datetime(next_fire)
            if expires_at_dt and next_fire_dt and next_fire_dt >= expires_at_dt:
                self._repository.update_after_fire(
                    schedule_id=schedule_id,
                    next_fire_at=None,
                    remaining_count=None,
                    claim_token=claim_token,
                    claimer_agent_id=claimer_agent_id,
                )
                self._logger.info(
                    f"Schedule {schedule_id} next fire {next_fire_dt.isoformat()} is at or after "
                    f"expiration {expires_at_dt.isoformat()}. Removed."
                )
                return False
            updated = self._repository.update_after_fire(
                schedule_id=schedule_id,
                next_fire_at=next_fire,
                remaining_count=remaining_count,
                claim_token=claim_token,
                claimer_agent_id=claimer_agent_id,
            )
            if not updated:
                self._logger.warning(f"Schedule {schedule_id} disappeared before reschedule update.")
                return False

            remaining_str = "infinite" if remaining_count is None else str(remaining_count)
            self._logger.info(
                f"Schedule {schedule_id} updated: next fire at {next_fire}, "
                f"remaining: {remaining_str}"
            )
            return True
        except Exception as e:
            self._logger.error(f"Failed to update schedule {schedule_id}: {e}")
            return False

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

        from custom_messages.contracts.schedule_v1 import ScheduledMessage

        filled: list[tuple[str, str]] = []
        for row in self.all_rows():
            if not row.get("cron_expression") or not row.get("schedule_json"):
                continue
            schedule = ScheduledMessage.model_validate_json(row["schedule_json"])
            zone = fill_cron_timezone(schedule, self.default_cron_timezone)
            if zone is None:
                continue
            schedule.next_fire_at = compute_next_fire_at(
                cron_expression=schedule.cron_expression, cron_timezone=zone
            )
            self._repository.set_timing(
                schedule.schedule_id, schedule_json=schedule.model_dump_json(), next_fire_at=schedule.next_fire_at
            )
            self._logger.warning(
                "Gave a cron schedule saved without a time zone its owner's time zone. "
                "event=schedule_cron_timezone_filled schedule_id=%s cron=%r timezone=%s next_fire_at=%s",
                schedule.schedule_id,
                schedule.cron_expression,
                zone,
                schedule.next_fire_at,
            )
            filled.append((schedule.schedule_id, zone))
        return filled

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
            if pruned:
                self._logger.warning(
                    "Pruned %s expired scheduled message%s at %s.",
                    pruned,
                    "" if pruned == 1 else "s",
                    now_dt.isoformat(),
                )
            return pruned
        except Exception as e:
            self._logger.error(f"Failed to prune expired schedules: {e}")
            return 0

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
            from custom_messages.contracts.schedule_v1 import ScheduledMessage
            from custom_messages.contracts.task_v1 import Task

            row = self._repository.get_schedule_row(schedule_id)
            if not row:
                return None
            schedule_json = row[0]
            if schedule_json:
                scheduled_message = ScheduledMessage.model_validate_json(schedule_json)
                scheduled_message.schedule_name = row[2]
                scheduled_message.interval_ms = row[3]
                scheduled_message.cron_expression = row[4]
                scheduled_message.repeat_count = row[5]
                scheduled_message.next_fire_at = row[6]
                scheduled_message.paused_until = row[7]
                scheduled_message.created_at = row[8]
                scheduled_message.enabled = bool(row[9])
                scheduled_message.objective_id = row[11] if len(row) > 11 else scheduled_message.objective_id
                scheduled_message.claim_team_id = row[12] if len(row) > 12 else scheduled_message.claim_team_id
                scheduled_message.claim_named_agent_type = (
                    row[13] if len(row) > 13 else scheduled_message.claim_named_agent_type
                )
                scheduled_message.claim_agent_id = row[14] if len(row) > 14 else scheduled_message.claim_agent_id
                scheduled_message.expires_at_utc = row[19] if len(row) > 19 else scheduled_message.expires_at_utc
                return apply_persisted_schedule_owner(
                    scheduled_message,
                    row[20] if len(row) > 20 else None,
                    logger_=self._logger,
                )
            scheduled_message = ScheduledMessage(
                schedule_id=schedule_id,
                payload=Task.model_validate_json(row[1]),
                schedule_name=row[2],
                interval_ms=row[3],
                cron_expression=row[4],
                repeat_count=row[5],
                next_fire_at=row[6],
                paused_until=row[7],
                expires_at_utc=row[19] if len(row) > 19 else None,
                created_at=row[8],
                enabled=bool(row[9]),
                objective_id=row[11] if len(row) > 11 else None,
                claim_team_id=row[12] if len(row) > 12 else None,
                claim_named_agent_type=row[13] if len(row) > 13 else None,
                claim_agent_id=row[14] if len(row) > 14 else None,
            )
            return apply_persisted_schedule_owner(
                scheduled_message,
                row[20] if len(row) > 20 else None,
                logger_=self._logger,
            )
        except Exception as e:
            self._logger.error(f"Failed to load scheduled message {schedule_id}: {e}")
            return None

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

        row = self._repository.get_schedule_row(schedule_id)
        if not row or len(row) < 18 or not claim_token or not claimer_agent_id:
            return False
        claimed_by, token, expires_at = row[15], row[16], _parse_utc_datetime(row[17])
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return (
            token == claim_token
            and claimed_by == claimer_agent_id
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
    ) -> bool:
        scheduled_message = self.get_scheduled_message(schedule_id)
        candidate = None
        normalized_operation = operation or (
            ScheduleMutationOperation.ENABLE
            if enabled
            else ScheduleMutationOperation.DISABLE
        )
        if scheduled_message is not None:
            candidate = scheduled_message.model_copy(deep=True)
            candidate.enabled = enabled
            require_schedule_mutation_permit(
                scheduled_message,
                operation=normalized_operation,
                permit=system_mutation_permit,
                candidate=candidate,
                request_binding=system_mutation_request_binding,
            )
        try:
            schedule_json = None
            if candidate is not None:
                schedule_json = candidate.model_dump_json()
            updated = self._repository.set_enabled(schedule_id, enabled, schedule_json)
            if updated:
                state = "enabled" if enabled else "disabled"
                self._logger.info(f"Schedule {schedule_id} {state}.")
            else:
                self._logger.warning(f"Schedule {schedule_id} not found for enable/disable.")
            return updated
        except Exception as e:
            self._logger.error(f"Failed to set enabled for schedule {schedule_id}: {e}")
            return False

    def set_paused_until(
        self,
        schedule_id: str,
        paused_until: Optional[str],
        *,
        operation: Optional[ScheduleMutationOperation] = None,
        system_mutation_permit: Optional[ScheduleMutationPermit] = None,
        system_mutation_request_binding: Optional[dict[str, Any]] = None,
    ) -> bool:
        scheduled_message = self.get_scheduled_message(schedule_id)
        normalized_operation = operation or (
            ScheduleMutationOperation.PAUSE
            if paused_until
            else ScheduleMutationOperation.RESUME
        )
        candidate = None
        if scheduled_message is not None:
            candidate = scheduled_message.model_copy(deep=True)
            candidate.paused_until = paused_until
            require_schedule_mutation_permit(
                scheduled_message,
                operation=normalized_operation,
                permit=system_mutation_permit,
                candidate=candidate,
                request_binding=system_mutation_request_binding,
            )
        try:
            if scheduled_message is None:
                self._logger.warning(f"Schedule {schedule_id} not found for pause/resume.")
                return False
            updated = self._repository.set_paused_until(
                schedule_id,
                paused_until,
                candidate.model_dump_json(),
            )
            if updated:
                if paused_until:
                    self._logger.info(f"Schedule {schedule_id} paused until {paused_until}.")
                else:
                    self._logger.info(f"Schedule {schedule_id} resumed.")
            return updated
        except Exception as e:
            self._logger.error(f"Failed to set paused_until for schedule {schedule_id}: {e}")
            return False

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

        row = self._repository.get_schedule_row(schedule_id)
        raw = row[21] if row and len(row) > 21 else None
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
