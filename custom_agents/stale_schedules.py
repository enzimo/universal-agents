"""Find schedules nobody needs any more, before each maintenance run (trusted manager code).

Why trusted code: InternalImprovementAgent's maintenance run is system work
for no person, so it cannot see people's schedules, which are private (access
plan 7c). Instead, before each maintenance run, the manager checks every
schedule with fixed rules, the same way it cleans scratch folders
(``internal_improvement_runtime.prune_scratch_for_maintenance``). No model
sees anyone's schedules; the run's instructions get counts only.

Already handled elsewhere: the schedule store deletes one-time schedules once
their last run ends and schedules past their end date, and a disabled or
removed person's schedules do not run (``task_context_runtime.inactive_owner``).

First, ``check_schedules`` ends the work of everyone disabled or removed
(``custom_agents.person_departure_runtime.settle_departed_people``): their
own schedules and their per-person system schedules are deleted, and the
runtime's team schedules they owned move to another active administrator.
Then the rules, using the gateway's copy of the team's people
(``access_control.people_directory``) and ``agent.schedule_maintenance.
stale_schedules`` settings:

- The owner is disabled or removed, and their schedule is still here: ending
  it failed, or no administrator could take over a team schedule. Reported to
  administrators.
- The owner is not known to the gateway: reported to administrators.
- For a person's own schedules with an active owner, reported to that owner:
  turned off for at least ``turned_off_days`` (counted from the time it would
  next have run, which stays fixed while it is off, so it is never reported
  early); paused until more than ``paused_beyond_days`` from now; it runs an
  agent type that no longer exists; its objective is closed.

System-managed schedules are otherwise left alone: pausing or turning them off
is an operator's deliberate choice.

Reports are Inbox notices stored once per content (``send_runtime_notice_once``),
so a person is told again only when their list changes. Nothing is reported,
and no departed person's work is ended, until the gateway has recorded
everyone at least once.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping

import asyncio

from access_control.people import PERSON_ROLE_ADMIN
from access_control.people_directory import PersonNow
from custom_agents.access_runtime import send_runtime_notice_once
from custom_agents.schedule_management import is_system_managed_schedule
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from custom_agents.task_context_runtime import people_directory
from custom_messages.contracts.access_v2 import AccessNoticeKind, PersonStanding
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.enums import ObjectiveStatus
from runtime_settings.stale_schedules import StaleScheduleSettings

logger = logging.getLogger(__name__)

STALE_SCHEDULE_NOTICE_PREFIX = "stale_schedules"
# Objectives in these states still want their schedules.
_OPEN_OBJECTIVE_STATES = frozenset(
    status.value
    for status in (ObjectiveStatus.PROPOSED, ObjectiveStatus.ACTIVE, ObjectiveStatus.PAUSED, ObjectiveStatus.BLOCKED)
)


class StaleAction(StrEnum):
    TELL_OWNER = "tell_owner"
    TELL_ADMINISTRATORS = "tell_administrators"


@dataclass(frozen=True)
class ScheduleFacts:
    """What the rules look at, taken from one stored schedule."""

    schedule_id: str
    name: str
    owner_id: str | None
    system_managed: bool
    enabled: bool
    next_fire_at: datetime | None
    paused_until: datetime | None
    target_agent_type: str | None
    objective_id: str | None


@dataclass(frozen=True)
class StaleFinding:
    schedule: ScheduleFacts
    action: StaleAction
    reason: str

    def line(self) -> str:
        return f"- {self.schedule.name} ({self.schedule.schedule_id}): {self.reason}"


@dataclass(frozen=True)
class StaleCheckInputs:
    people: Mapping[str, PersonNow]
    now: datetime
    settings: StaleScheduleSettings
    missing_agent_types: frozenset[str] | None  # None when the agent definitions could not be read
    closed_objectives: frozenset[str] = frozenset()


@dataclass
class StaleScheduleReport:
    for_owners: dict[str, list[StaleFinding]] = field(default_factory=dict)
    for_administrators: list[StaleFinding] = field(default_factory=list)
    skipped_reason: str | None = None

    def instruction_sentence(self) -> str:
        """What the maintenance run is told: counts only, never anyone's schedules."""

        if self.skipped_reason:
            return f"The stale-schedule check did not run before this review: {self.skipped_reason}."
        told = sum(len(findings) for findings in self.for_owners.values())
        return (
            f"Trusted code checked every schedule before this review: it told {len(self.for_owners)} "
            f"person(s) about {told} of their schedules that look unused, and told administrators about "
            f"{len(self.for_administrators)}. Schedules are handled there, so do not look for or report them."
        )


# -- the rules -------------------------------------------------------------------


def _owner_finding(facts: ScheduleFacts, inputs: StaleCheckInputs) -> StaleFinding | None:
    if not facts.owner_id:
        return None
    person = inputs.people.get(facts.owner_id)
    if person is None:
        return StaleFinding(facts, StaleAction.TELL_ADMINISTRATORS, "its owner is not known to the gateway")
    if person.standing in (PersonStanding.DISABLED, PersonStanding.REMOVED):
        # Ending a departed person's work deletes or moves every schedule they
        # own (person_departure_runtime); one still here was not handled.
        return StaleFinding(
            facts,
            StaleAction.TELL_ADMINISTRATORS,
            f"its owner is {person.standing.value} and it could not be deleted or moved to an administrator "
            "(manager log event=person_departure_schedule_delete_failed or team_schedule_owner_unavailable)",
        )
    return None


def _unused_reason(facts: ScheduleFacts, inputs: StaleCheckInputs) -> str | None:
    settings, now = inputs.settings, inputs.now
    if (
        not facts.enabled
        and facts.next_fire_at is not None
        and facts.next_fire_at <= now - timedelta(days=settings.turned_off_days)
    ):
        return f"turned off since at least {facts.next_fire_at:%Y-%m-%d}"
    if facts.paused_until is not None and facts.paused_until > now + timedelta(days=settings.paused_beyond_days):
        return f"paused until {facts.paused_until:%Y-%m-%d}"
    if inputs.missing_agent_types and facts.target_agent_type in inputs.missing_agent_types:
        return f"it runs {facts.target_agent_type}, which no longer exists"
    if facts.objective_id and facts.objective_id in inputs.closed_objectives:
        return f"its objective {facts.objective_id} is closed"
    return None


def classify(facts: ScheduleFacts, inputs: StaleCheckInputs) -> StaleFinding | None:
    """What to do about one schedule, or None when it looks in use (module docstring)."""

    found = _owner_finding(facts, inputs)
    if found is not None or facts.system_managed:
        return found
    reason = _unused_reason(facts, inputs)
    return StaleFinding(facts, StaleAction.TELL_OWNER, reason) if reason else None


# -- reading the schedules ----------------------------------------------------------


def _utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def schedule_facts(schedule: ScheduledMessage) -> ScheduleFacts:
    meta = schedule.payload_meta
    return ScheduleFacts(
        schedule_id=schedule.schedule_id,
        name=str(schedule.schedule_name or schedule.schedule_id),
        owner_id=owner_canonical_user_id_for_schedule(schedule),
        system_managed=is_system_managed_schedule(schedule),
        enabled=bool(schedule.enabled),
        next_fire_at=_utc(schedule.next_fire_at),
        paused_until=_utc(schedule.paused_until),
        target_agent_type=str(getattr(meta, "to_named_agent_type", "") or "") or None,
        objective_id=schedule.objective_id or None,
    )


def _stored_schedules(agent: Any) -> list[ScheduleFacts]:
    store = agent._schedule_store
    schedules = (store.get_scheduled_message(str(row.get("schedule_id") or "")) for row in store.all_rows())
    return [schedule_facts(schedule) for schedule in schedules if schedule is not None]


def _missing_agent_types(agent: Any, schedules: Iterable[ScheduleFacts]) -> frozenset[str] | None:
    """The agent types people's schedules run that have no definition now; None when that cannot be told.

    Uses the definition lookup the team's composition gives the manager
    (``LocalAgentTeamRuntime.resolve_definition``, which reads the current
    catalog), so this module does not import the agent catalog.
    """

    runtime = getattr(agent, "_local_team_runtime", None)
    wanted = {facts.target_agent_type for facts in schedules if facts.target_agent_type and not facts.system_managed}
    if not wanted:
        return frozenset()
    try:
        return frozenset(agent_type for agent_type in wanted if runtime.resolve_definition(agent_type) is None)
    except Exception:  # noqa: BLE001 - that one rule is skipped; the others still apply
        logger.exception(
            "Could not read the agent definitions; the stale-schedule check skips its agent type rule. "
            "event=stale_schedule_agent_types_unreadable"
        )
        return None


def _closed_objectives(agent: Any, schedules: Iterable[ScheduleFacts]) -> frozenset[str]:
    closed: set[str] = set()
    for objective_id in {facts.objective_id for facts in schedules if facts.objective_id and not facts.system_managed}:
        objective = agent.task_manager.get_objective(objective_id)
        status = getattr(objective, "status", None)
        if objective is None or str(getattr(status, "value", status)) not in _OPEN_OBJECTIVE_STATES:
            closed.add(objective_id)
    return frozenset(closed)


def stale_schedule_settings(agent: Any) -> StaleScheduleSettings:
    return StaleScheduleSettings.model_validate(
        (getattr(agent, "schedule_maintenance", {}) or {}).get("stale_schedules") or {}
    )


# -- acting and telling people ----------------------------------------------------


def _act(agent: Any, findings: Iterable[StaleFinding]) -> StaleScheduleReport:
    report = StaleScheduleReport()
    for finding in findings:
        if finding.action is StaleAction.TELL_OWNER and finding.schedule.owner_id:
            report.for_owners.setdefault(finding.schedule.owner_id, []).append(finding)
        else:
            report.for_administrators.append(finding)
    return report


def _notice_id(audience: str, findings: Iterable[StaleFinding]) -> str:
    content = "\n".join(sorted(f"{finding.action.value}:{finding.line()}" for finding in findings))
    digest = hashlib.sha256(f"{audience}\n{content}".encode("utf-8")).hexdigest()[:32]
    return f"{STALE_SCHEDULE_NOTICE_PREFIX}:{digest}"


def _tell_owner(agent: Any, owner_id: str, findings: list[StaleFinding]) -> None:
    lines = "\n".join(finding.line() for finding in findings)
    send_runtime_notice_once(
        agent,
        notice_id=_notice_id(f"person:{owner_id}", findings),
        kind=AccessNoticeKind.SCHEDULES_TO_REVIEW,
        person_id=owner_id,
        title=f"{len(findings)} of your schedules may no longer be needed",
        summary=(
            f"These schedules of yours look unused:\n{lines}\n\n"
            "To remove one, use /delete schedule <schedule_id> or ask TeamLead. To keep a turned-off or "
            "paused one and run it again, use /resume schedule <schedule_id>. Nothing changes if you do "
            "nothing, and you are told again only if this list changes."
        ),
    )


# The schedules administrators should check, from the latest run, by team:
# the Status page shows them (``administrator_findings``) until a later run no
# longer finds them.
_LATEST_FOR_ADMINISTRATORS: dict[str, tuple[StaleFinding, ...]] = {}


def administrator_findings(team_id: str) -> tuple[StaleFinding, ...]:
    """The schedules administrators should check, from the latest check of ``team_id``."""

    return _LATEST_FOR_ADMINISTRATORS.get(team_id, ())


def _tell_administrators(agent: Any, report: StaleScheduleReport) -> None:
    """One notice per list of schedules for administrators to check."""

    _LATEST_FOR_ADMINISTRATORS[str(getattr(agent, "team_id", "") or "")] = tuple(report.for_administrators)

    if report.for_administrators:
        send_runtime_notice_once(
            agent,
            notice_id=_notice_id("administrators:check", report.for_administrators),
            kind=AccessNoticeKind.SCHEDULES_TO_REVIEW,
            roles=(PERSON_ROLE_ADMIN,),
            title=f"{len(report.for_administrators)} schedule(s) to check",
            summary="To check:\n"
            + "\n".join(
                f"{finding.line()} (owner {finding.schedule.owner_id or 'none'})" for finding in report.for_administrators
            )
            + "\n\nDisabling or removing a person (/user disable, /user remove) deletes their own schedules "
            "and moves the team's schedules they owned to another administrator; a schedule listed here "
            "needs a person to delete it (/delete schedule <schedule_id>) or to find out what failed.",
        )


async def check_schedules(agent: Any) -> str:
    """Before each maintenance run: end departed people's work, then check the rest; a sentence of counts.

    The departure step deletes disabled and removed people's schedules and
    moves the team's schedules they owned (module docstring).
    """

    from custom_agents.history_cleanup import resume_history_cleanups
    from custom_agents.person_departure_runtime import settle_departed_people_safely

    departures = await settle_departed_people_safely(agent, when="maintenance")
    # History clean-ups a stopped run left part way (custom_agents.history_cleanup).
    await resume_history_cleanups(agent)
    report = await asyncio.to_thread(check_stale_schedules, agent)
    return f"{departures} {report.instruction_sentence()}"


def check_stale_schedules(agent: Any, *, now: datetime | None = None) -> StaleScheduleReport:
    """Check every schedule and tell owners and administrators (module docstring).

    Reads and writes stores, so async callers run it in a thread (``asyncio.to_thread``).
    """

    settings = stale_schedule_settings(agent)
    if not settings.enabled:
        return StaleScheduleReport(skipped_reason="it is turned off in the settings")
    directory = people_directory(agent)
    if not directory.synced():
        logger.warning(
            "The stale-schedule check waits until the gateway has recorded the team's people. "
            "event=stale_schedule_check_waiting_for_people team_id=%s",
            directory.team_id,
        )
        return StaleScheduleReport(skipped_reason="the gateway has not recorded the team's people yet")
    schedules = _stored_schedules(agent)
    inputs = StaleCheckInputs(
        people=directory.people(),
        now=now or datetime.now(timezone.utc),
        settings=settings,
        missing_agent_types=_missing_agent_types(agent, schedules),
        closed_objectives=_closed_objectives(agent, schedules),
    )
    report = _act(agent, (finding for facts in schedules if (finding := classify(facts, inputs)) is not None))
    for owner_id, findings in report.for_owners.items():
        _tell_owner(agent, owner_id, findings)
    _tell_administrators(agent, report)
    logger.info(
        "Checked schedules for ones nobody needs. event=stale_schedule_check_done schedules=%s "
        "owners_told=%s for_administrators=%s",
        len(schedules),
        len(report.for_owners),
        len(report.for_administrators),
    )
    return report


__all__ = [
    "STALE_SCHEDULE_NOTICE_PREFIX",
    "ScheduleFacts",
    "StaleAction",
    "StaleCheckInputs",
    "StaleFinding",
    "StaleScheduleReport",
    "check_schedules",
    "check_stale_schedules",
    "classify",
    "schedule_facts",
    "stale_schedule_settings",
]
