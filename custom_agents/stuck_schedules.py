"""Which schedules are stuck, what their people are told, and missed one-time runs.

Replaced the self-addressed TeamLead review task (removed 2026-10-04; see
``custom_agents.stuck_work`` for why). Runs on every pass of the team lead's
scheduler loop (``scheduled_runtime._scheduler_pass``).

A schedule is stuck when it is enabled, overdue by more than the claim window,
not paused or expired, and nobody holds a live claim on it. Its reason is the
block the scheduled-worker controller recorded, or
``schedule_overdue_unclaimed`` when nothing was recorded (no worker policy, or
no worker picked it up). The incident is that due time: once the schedule
runs, the incident closes and its people hear it is working again. People are
told after ``agent.schedule_workers.review_interval_seconds``.

Who is told: the schedule's owner (Inbox and chat) and, for a runtime-managed
schedule or a block only an administrator can fix
(``schedule_repair_guidance.administrators_act``), administrators (Inbox).

A final run (a one-time schedule, or the last of a counted series) that is more
than ``agent.schedule_workers.missed_one_time_after_seconds`` late is turned
off instead, and its owner is told it was missed: a reminder delivered days
late is worse than none (owner's decision, 2026-10-04). Runtime-managed
schedules are left to their own lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

from custom_agents.access_runtime import send_runtime_notice_once
from custom_agents.instance_delivery_context import configured_owner_route
from custom_agents.schedule_management import is_system_managed_schedule
from custom_agents.schedule_repair_guidance import (
    SCHEDULE_OVERDUE_UNCLAIMED,
    administrators_act,
    person_explanation,
)
from custom_agents.schedule_store import (
    DEFAULT_SCHEDULE_CLAIM_TTL_SECONDS,
    owner_canonical_user_id_for_schedule,
)
from custom_agents.stuck_work import (
    MISSED_RUN_NOTICE_REASON,
    ChatRoute,
    Recovery,
    StuckFinding,
    StuckMessage,
    report_stuck_work,
    send_chat_notice,
)
from custom_messages.contracts.access_v2 import AccessNoticeKind
from storage.stuck_work_repository import StuckSubjectKind, StuckWorkIncidentV1
from time_presentation import format_user_timestamp

SCHEDULE_SUBJECT: StuckSubjectKind = "schedule"


def _utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def overdue_unclaimed(row: dict[str, Any], *, team_id: str, now: datetime) -> bool:
    """Enabled, past due beyond the claim window, not paused or expired, unclaimed."""
    if row.get("claim_team_id") != team_id or not row.get("enabled"):
        return False
    due = _utc(row.get("next_fire_at"))
    if due is None or due > now - timedelta(seconds=DEFAULT_SCHEDULE_CLAIM_TTL_SECONDS):
        return False
    pause = _utc(row.get("paused_until"))
    expiry = _utc(row.get("expires_at_utc"))
    if (pause and pause > now) or (expiry and expiry <= now):
        return False
    lease = _utc(row.get("claim_expires_at"))
    return not (row.get("claimed_by_agent_id") and lease and lease > now)


def schedule_block_reason(store: Any, schedule: Any) -> str:
    """The recorded worker block, or ``schedule_overdue_unclaimed`` when none is recorded."""
    team_id = schedule.claim_team_id
    binding = (
        store.worker_records.get_binding(schedule.schedule_id, team_id=team_id, active=True)
        if team_id
        else None
    )
    if binding is not None and binding.blocked_reason:
        return str(binding.blocked_reason)
    return SCHEDULE_OVERDUE_UNCLAIMED


def not_running(store: Any, schedule: Any, *, now: datetime) -> tuple[str, str] | None:
    """Since when and why a schedule is not running (its reason code), or None.

    The one rule behind both the notices and the Schedules card in Settings
    (``manager_control_runtime``), so they always agree.
    """
    row = store.get_row(schedule.schedule_id)
    if not row or not overdue_unclaimed(row, team_id=str(schedule.claim_team_id or ""), now=now):
        return None
    return str(row["next_fire_at"]), schedule_block_reason(store, schedule)


def _final_run(schedule: Any, row: dict[str, Any]) -> bool:
    remaining = row.get("remaining_count")
    recurring = bool(schedule.interval_ms or schedule.cron_expression)
    return remaining == 1 or (not recurring and remaining is None)


def _label(schedule: Any) -> str:
    return str(schedule.schedule_name or schedule.schedule_id)


@dataclass(frozen=True)
class MissedRun:
    """A final run too late to deliver, and what its owner is told."""

    schedule_id: str
    due: str
    owner: str | None
    chat_route: ChatRoute | None
    message: StuckMessage


class StuckSchedules:
    """The schedule source for ``report_stuck_work``."""

    subject_kind: StuckSubjectKind = SCHEDULE_SUBJECT

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.store = agent._schedule_store

    def when(self, schedule: Any, value: Any) -> str:
        """A time in the schedule's own time zone, for people."""
        zone = schedule.cron_timezone or getattr(self.store, "default_cron_timezone", None)
        return format_user_timestamp(value, timezone_name=zone)

    def chat_route(self, schedule: Any) -> ChatRoute | None:
        """The owner's chat: the schedule's own route, else the instance's configured user."""
        header, meta = schedule.payload_header, schedule.payload_meta
        context = getattr(meta, "gateway_context", None)
        routing_key = getattr(header, "response_routing_key", None)
        if context is not None and routing_key and routing_key != self.agent.id:
            return ChatRoute(routing_key, context)
        owner = owner_canonical_user_id_for_schedule(schedule)
        configured = (
            configured_owner_route(getattr(self.agent, "instance_config_path", None), owner)
            if owner
            else None
        )
        return ChatRoute(*configured) if configured else None

    def findings(self, now: datetime) -> list[StuckFinding]:
        found: list[StuckFinding] = []
        for row in self.store.get_all_enabled():
            if not overdue_unclaimed(row, team_id=self.agent.team_id, now=now):
                continue
            schedule = self.store.get_scheduled_message(row["schedule_id"])
            if schedule is None:
                continue
            reason = schedule_block_reason(self.store, schedule)
            system = is_system_managed_schedule(schedule)
            found.append(
                StuckFinding(
                    subject_kind=SCHEDULE_SUBJECT,
                    subject_id=schedule.schedule_id,
                    incident_key=str(row["next_fire_at"]),
                    reason=reason,
                    since=_utc(row["next_fire_at"]) or now,
                    person_id=owner_canonical_user_id_for_schedule(schedule),
                    tell_administrators=system or administrators_act(reason),
                    message=StuckMessage(
                        title=f'Schedule "{_label(schedule)}" is not running',
                        text=(
                            f"It was due {self.when(schedule, row['next_fire_at'])} and has "
                            f"not run since. {person_explanation(reason)}"
                        ),
                    ),
                    chat_route=self.chat_route(schedule),
                )
            )
        return found

    def recovery(self, incident: StuckWorkIncidentV1) -> Recovery:
        schedule = self.store.get_scheduled_message(incident.subject_id)
        if schedule is None:
            return Recovery(
                StuckMessage(
                    title=f'Schedule "{incident.subject_id}" is no longer waiting',
                    text="It was removed, so nothing is waiting for it any more.",
                ),
                None,
            )
        if not schedule.enabled:
            return Recovery(
                StuckMessage(
                    title=f'Schedule "{_label(schedule)}" is no longer waiting',
                    text="It was turned off, so it will not run until it is turned on again.",
                ),
                self.chat_route(schedule),
            )
        return Recovery(
            StuckMessage(
                title=f'Schedule "{_label(schedule)}" is running again',
                text=(
                    "The problem reported earlier is resolved and the schedule ran. "
                    f"Its next run is due {self.when(schedule, schedule.next_fire_at)}."
                ),
            ),
            self.chat_route(schedule),
        )

    def missed_final_runs(self, now: datetime, late_after: timedelta) -> list[MissedRun]:
        """Final runs more than ``late_after`` overdue, with what to tell (storage lane)."""
        missed: list[MissedRun] = []
        for row in self.store.get_all_enabled():
            if not overdue_unclaimed(row, team_id=self.agent.team_id, now=now):
                continue
            due = _utc(row.get("next_fire_at"))
            if due is None or now - due < late_after:
                continue
            schedule = self.store.get_scheduled_message(row["schedule_id"])
            if (
                schedule is None
                or is_system_managed_schedule(schedule)
                or not _final_run(schedule, row)
            ):
                continue
            due_text = str(row["next_fire_at"])
            reason = schedule_block_reason(self.store, schedule)
            missed.append(
                MissedRun(
                    schedule_id=schedule.schedule_id,
                    due=due_text,
                    owner=owner_canonical_user_id_for_schedule(schedule),
                    chat_route=self.chat_route(schedule),
                    message=StuckMessage(
                        title=f'Schedule "{_label(schedule)}" was missed',
                        text=(
                            f"Its last run was due {self.when(schedule, due_text)} but could "
                            f"not run on time. {person_explanation(reason)} It has been "
                            "turned off so it does not arrive late. Ask your assistant to "
                            "set it up again if you still need it."
                        ),
                    ),
                )
            )
        return missed


def _turn_off(store: Any, missed: MissedRun, now: datetime) -> bool:
    """Turn the schedule off only if its missed run is still waiting, unclaimed (one conditional write).

    The rows were read in an earlier step; a worker may have claimed and
    started the run since. Then nothing changes and nobody is told it was missed.
    """
    return bool(store.turn_off_missed_run(missed.schedule_id, due_at=missed.due, now=now))


async def _turn_off_missed_final_runs(agent: Any, source: StuckSchedules, now: datetime, late_after: timedelta) -> None:
    runtime = agent.task_manager.operations_runtime
    for missed in await runtime.run(partial(source.missed_final_runs, now, late_after)):
        if not await runtime.run(_turn_off, source.store, missed, now):
            agent._logger.info(
                "A missed final run was not turned off: it is running or changed meanwhile. "
                "event=schedule_final_run_turn_off_skipped schedule_id=%s due=%s",
                missed.schedule_id,
                missed.due,
            )
            continue
        owner, message = missed.owner, missed.message
        notice_id = f"missed_run:{missed.schedule_id}:{missed.due}"
        if owner:
            await runtime.run(
                partial(
                    send_runtime_notice_once,
                    agent,
                    notice_id=notice_id,
                    kind=AccessNoticeKind.WORK_STUCK,
                    title=message.title,
                    summary=message.text,
                    person_id=owner,
                )
            )
        sent = await send_chat_notice(
            agent,
            missed.chat_route,
            message,
            reason=MISSED_RUN_NOTICE_REASON,
            correlation_id=notice_id,
            source_task_id=None,
        )
        agent._logger.warning(
            "Turned off a schedule whose last run was missed, and told its owner. "
            "event=schedule_final_run_missed schedule_id=%s due=%s owner=%s chat=%s",
            missed.schedule_id,
            missed.due,
            owner,
            sent,
        )


async def report_stuck_schedules(agent: Any, *, now: datetime | None = None) -> None:
    """One pass: turn off missed final runs, then report stuck schedules and recoveries."""
    if not getattr(agent, "is_team_lead", False) or not agent.team_id:
        return
    from custom_agents.agent_instantiation_service import current_worker_limits

    now = now or datetime.now(timezone.utc)
    try:
        limits = current_worker_limits(agent)
        source = StuckSchedules(agent)
        await _turn_off_missed_final_runs(
            agent, source, now, timedelta(seconds=limits.missed_one_time_after_seconds)
        )
        await report_stuck_work(
            agent,
            source,
            notice_after=timedelta(seconds=limits.review_interval_seconds),
            now=now,
        )
    except Exception:
        agent._logger.exception(
            "Could not check for stuck schedules; retrying on the next pass. "
            "event=stuck_schedule_scan_failed team_id=%s",
            agent.team_id,
        )
