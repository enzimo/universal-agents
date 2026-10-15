"""Who an agent task works for: the one check every enforcement point uses.

A task proves whom it works for in one of two ways, carried in its
``GatewayContext``:

- a signed task context (``task_context``, ``access_control.identity``): the
  gateway signs one for each person's request, and the scheduler signs one
  for a scheduled run started in the manager process, which holds the key;
- a schedule run identity (``schedule_run``): a scheduled run started by a
  worker that holds no scheduler key carries its schedule's permit, which the
  manager signed once for the schedule (``custom_agents.schedule_permits``),
  plus the run's own unsigned fields (a fresh context id, the conversation and
  the start time).

``resolve_task_identity`` accepts either and returns the same
``TaskContextV1`` shape, so the access hook, the broker's gate and the
schedule tools decide exactly as before; there is no second decision path.

Why permits (owner's decision 2026-09-27): only the manager holds the
scheduler key, and asking the manager to sign every run of every worker does
not scale. So the manager signs once per schedule and workers never contact it
for a normal run. A permit names no roles, so it cannot carry stale ones: for
each run identity this checks, live, that the schedule still exists, is
turned on and is still owned by the permit's person, and reads that person's
current standing and roles from the gateway's copy of the team's people
(``access_control.people_directory``). A disabled or removed person, a
deleted or turned-off schedule, or a changed owner refuses the run's next
tool call. The live facts are cached for ``LIVE_CHECK_SECONDS`` (30 seconds)
per schedule and permit, bounded to ``LIVE_CHECK_CACHE_SIZE`` entries, so a
busy run does not read the database for every tool call; a change therefore
applies within 30 seconds.

A permit for no person (system work) is accepted only for a turned-on,
runtime-managed schedule whose kind the system schedule table marks
``system_work`` (``custom_messages.system_schedules``; today the
internal-improvement maintenance of a built-in system agent).

This module reads the schedule row straight from storage
(``StoredScheduleFacts``) so the broker, which runs no agents, can check run
identities too.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from access_control.identity import (
    DEFAULT_CONTEXT_LIFETIME,
    ISSUER_SCHEDULER,
    ContextVerifier,
    IdentityRejected,
)
from access_control.people_directory import PeopleDirectory, PeopleStore, PersonNow
from custom_messages.contracts.access_v2 import (
    SchedulePermitV1,
    ScheduleRunIdentityV1,
    SignedTaskContextV1,
    TaskContextV1,
)
from custom_messages.enums import ScheduleManagementClass, SystemScheduleKind
from custom_messages.system_schedules import is_system_work_kind
from storage.config import StorageSettings

logger = logging.getLogger(__name__)

LIVE_CHECK_SECONDS = 30.0
LIVE_CHECK_CACHE_SIZE = 1024
# A run may say it started at most this far in the future (clocks differ a little).
RUN_START_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class ScheduleFacts:
    """What a run check needs from a schedule row, as stored now."""

    schedule_id: str
    enabled: bool
    owner_id: str | None
    system_managed: bool
    system_kind: str | None
    # The agent type that claims and runs the schedule (claim_named_agent_type).
    runner_type: str | None = None

    @property
    def system_work(self) -> bool:
        """Whether the schedule's runs work for no person (its kind is ``system_work`` in the table)."""

        return self.system_managed and is_system_work_kind(self.system_kind)

    def worker_job(self, lead_agent_type: str) -> bool:
        """Whether this is a person's scheduled job a worker type runs (``job_schedule_id``)."""

        return not self.system_managed and self.runner_type not in (None, "", lead_agent_type)


class ScheduleFactsSource(Protocol):
    def schedule_facts(self, schedule_id: str) -> ScheduleFacts | None: ...


class StoredScheduleFacts:
    """Reads schedule rows from the team's schedule table (any process with the manager storage).

    Opens the table on first use: opening prepares the schema, and most
    processes never check a scheduled run.
    """

    def __init__(self, storage: StorageSettings) -> None:
        self._storage = storage
        self._repository: Any = None

    def _rows(self) -> Any:
        if self._repository is None:
            from storage.schedule_repository import create_schedule_repository

            self._repository = create_schedule_repository(self._storage, logger=logger)
        return self._repository

    def schedule_facts(self, schedule_id: str) -> ScheduleFacts | None:
        from custom_messages.contracts.schedule_v1 import ScheduledMessage

        row = self._rows().get_schedule_row(schedule_id)
        if not row:
            return None
        schedule = ScheduledMessage.model_validate_json(row[0]) if row[0] else None
        management = getattr(schedule, "management_class", None)
        kind = getattr(schedule, "system_schedule_kind", None)
        system_managed = management == ScheduleManagementClass.SYSTEM
        # The row's owner column is authoritative (schedule_store.apply_persisted_schedule_owner).
        owner = str(row[20] or "").strip() if len(row) > 20 else ""
        runner = str(row[13] or "").strip() if len(row) > 13 else ""
        return ScheduleFacts(
            schedule_id=schedule_id,
            enabled=bool(row[9]),
            owner_id=owner or None,
            system_managed=system_managed,
            system_kind=str(getattr(kind, "value", kind) or SystemScheduleKind.UNCLASSIFIED.value)
            if system_managed
            else None,
            runner_type=runner or None,
        )


class _StoredPeople:
    """The team's people from the access store, opened on first use (``StoredScheduleFacts``)."""

    def __init__(self, storage: StorageSettings) -> None:
        self._storage = storage
        self._store: Any = None

    def __getattr__(self, name: str) -> Any:
        if self._store is None:
            from storage.access_repository import AccessRepository

            self._store = AccessRepository(self._storage)
        return getattr(self._store, name)


@dataclass(frozen=True)
class _LiveFacts:
    schedule: ScheduleFacts | None
    person: PersonNow | None
    read_at: float


class ScheduleRunLookup:
    """The live facts behind schedule run identities, cached briefly (module docstring).

    ``clock`` is a monotonic clock in seconds; tests replace it to expire the cache.
    """

    def __init__(
        self,
        schedules: ScheduleFactsSource,
        people: PeopleStore,
        *,
        ttl_seconds: float = LIVE_CHECK_SECONDS,
        max_entries: int = LIVE_CHECK_CACHE_SIZE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.schedules = schedules
        self.people = people
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.clock = clock
        self._cache: OrderedDict[tuple[str, str, str], _LiveFacts] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_storage(cls, storage: StorageSettings) -> "ScheduleRunLookup":
        return cls(StoredScheduleFacts(storage), _StoredPeople(storage))

    def facts(self, permit: SchedulePermitV1) -> tuple[ScheduleFacts | None, PersonNow | None]:
        """The schedule row and the permit's person as recorded now (cached per team, schedule and permit)."""

        def read() -> _LiveFacts:
            schedule = self.schedules.schedule_facts(permit.schedule_id)
            person = (
                PeopleDirectory(self.people, permit.team_id).person(permit.person_id) if permit.person_id else None
            )
            return _LiveFacts(schedule, person, self.clock())

        found = self._cached((permit.team_id, permit.schedule_id, permit.permit_id), read)
        return found.schedule, found.person

    def schedule(self, schedule_id: str) -> ScheduleFacts | None:
        """The schedule row as recorded now, cached like ``facts``."""

        return self._cached(
            ("", schedule_id, ""), lambda: _LiveFacts(self.schedules.schedule_facts(schedule_id), None, self.clock())
        ).schedule

    def _cached(self, key: tuple[str, str, str], read: Callable[[], _LiveFacts]) -> _LiveFacts:
        now = self.clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached.read_at < self.ttl_seconds:
                self._cache.move_to_end(key)
                return cached
        found = read()
        with self._lock:
            self._cache[key] = found
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
        return found


def _live_problem(permit: SchedulePermitV1, schedule: ScheduleFacts | None, person: PersonNow | None) -> str | None:
    """Why the permit's schedule or person no longer allows its runs, or None."""

    if schedule is None:
        return "The scheduled run's schedule no longer exists."
    if not schedule.enabled:
        return "The scheduled run's schedule is turned off."
    if permit.person_id is None:
        if not schedule.system_work:
            return "A permit for no person is valid only for the runtime's own maintenance schedule."
        return None
    if schedule.owner_id != permit.person_id:
        return "The scheduled run's schedule now belongs to someone else."
    if person is None or not person.active:
        standing = person.describe() if person is not None else "unknown"
        return f"The person the scheduled run works for is not active ({standing})."
    return None


def _run_times(run: ScheduleRunIdentityV1, moment: datetime) -> tuple[datetime, datetime]:
    """The run's start and end, as a task context would state them; raises when outside them."""

    started = run.started_at_utc
    if started > moment + RUN_START_SKEW:
        raise IdentityRejected("The scheduled run says it starts in the future.")
    expires = started + DEFAULT_CONTEXT_LIFETIME
    if moment >= expires:
        raise IdentityRejected("The scheduled run's identity has expired.")
    return started, expires


def _run_context(
    run_value: ScheduleRunIdentityV1 | Mapping[str, Any],
    *,
    verifier: ContextVerifier,
    team_id: str,
    schedule_runs: ScheduleRunLookup | None,
    moment: datetime,
) -> TaskContextV1:
    run = run_value if isinstance(run_value, ScheduleRunIdentityV1) else ScheduleRunIdentityV1.model_validate(run_value)
    permit = verifier.verify_permit(run.permit, team_id=team_id, now=moment)
    started, expires = _run_times(run, moment)
    if schedule_runs is None:
        raise IdentityRejected("This process cannot check scheduled runs (it has no access to the schedules).")
    schedule, person = schedule_runs.facts(permit)
    problem = _live_problem(permit, schedule, person)
    if problem is not None:
        raise IdentityRejected(problem)
    return TaskContextV1(
        context_id=run.context_id,
        issuer=ISSUER_SCHEDULER,
        team_id=permit.team_id,
        person_id=permit.person_id,
        person_roles=tuple(sorted(set(person.roles))) if person is not None else (),
        conversation_id=run.conversation_id,
        schedule_id=permit.schedule_id,
        issued_at_utc=started,
        expires_at_utc=expires,
    )


def job_schedule_id(
    context: TaskContextV1, schedule_runs: ScheduleRunLookup | None, *, lead_agent_type: str
) -> str | None:
    """The scheduled job a verified task is a run of, or None ("access follows the work").

    A run is a job run only when a worker type runs a person's own schedule
    (``ScheduleFacts.worker_job``). There every agent is in the schedule, so
    the schedule's access counts, and its workers draw on nothing else: they
    work for the schedule. Every other scheduled run is treated like a
    person's request, so the agents inherit the access of whom they work for:
    a schedule the team lead runs, and the runtime's own timers (heartbeat
    items, objective work sessions, after-action reviews; their contexts name
    a system-managed schedule or a heartbeat item, which has no schedule row).
    An after-action review's schedule is owned by the person whose work the
    reports describe, or by nobody for system work
    (``custom_agents.after_action_report_identity``).

    Only the scheduler says a task is a schedule's run: a context it signed,
    or a schedule run identity checked against the schedule's permit (both
    have issuer ``scheduler``). A schedule id in a context anyone else signed
    is ignored and logged, so a schedule's access (``SubjectKind.SCHEDULE``)
    never reaches a person's own tasks. A process that cannot read the
    schedules keeps the schedule id (the stricter reading) and logs it. A job
    deleted during a run is, for the rest of that run, read like the person's
    request (its own access already ended with it, ``ScheduleStore.remove``):
    heartbeat item runs also name an id with no row, so a missing row cannot
    mean "job".
    """

    if context.schedule_id is None:
        return None
    if context.issuer != ISSUER_SCHEDULER:
        logger.warning(
            "A task context names a schedule but was not signed by the scheduler; ignoring it. "
            "event=schedule_id_from_non_scheduler_ignored issuer=%s schedule_id=%s context_id=%s",
            context.issuer,
            context.schedule_id,
            context.context_id,
        )
        return None
    if schedule_runs is None:
        logger.warning(
            "This process cannot read the schedules, so a scheduled run counts as a job run. "
            "event=schedule_kind_unknown schedule_id=%s context_id=%s",
            context.schedule_id,
            context.context_id,
        )
        return context.schedule_id
    facts = schedule_runs.schedule(context.schedule_id)
    if facts is None or not facts.worker_job(lead_agent_type):
        return None
    return context.schedule_id


def resolve_task_identity(
    *,
    verifier: ContextVerifier,
    team_id: str,
    task_context: SignedTaskContextV1 | Mapping[str, Any] | None,
    schedule_run: ScheduleRunIdentityV1 | Mapping[str, Any] | None,
    schedule_runs: ScheduleRunLookup | None,
    now: datetime | None = None,
) -> TaskContextV1:
    """Who the task works for, from its signed task context or its schedule run identity (module docstring).

    Raises ``IdentityRejected`` (or ``ValueError`` for a malformed value)
    when neither proves it. A task carries one or the other, never both.
    """

    moment = now or datetime.now(timezone.utc)
    if schedule_run is None:
        return verifier.verify(task_context, team_id=team_id, now=moment)
    if task_context is not None:
        raise IdentityRejected("The task carries both a signed task context and a scheduled run identity.")
    return _run_context(schedule_run, verifier=verifier, team_id=team_id, schedule_runs=schedule_runs, moment=moment)


def reported_task_person(
    *,
    verifier: ContextVerifier,
    team_id: str,
    task_context: SignedTaskContextV1 | Mapping[str, Any] | None,
    schedule_run: ScheduleRunIdentityV1 | Mapping[str, Any] | None,
) -> str | None:
    """Whose completed work is reported, from the original signature only.

    This is historical attribution, never authority to execute. A request
    may outlive its context, and a finished schedule may already be deleted.
    Both still prove the original person. Return only their id, never roles
    or a context that a caller could use to authorize work. Review runs get
    fresh identity and access decisions through ``resolve_task_identity``.
    """

    if schedule_run is None:
        return verifier.verify(task_context, team_id=team_id, check_expiry=False).person_id
    if task_context is not None:
        raise IdentityRejected("The task carries both a signed task context and a scheduled run identity.")
    run = (
        schedule_run
        if isinstance(schedule_run, ScheduleRunIdentityV1)
        else ScheduleRunIdentityV1.model_validate(schedule_run)
    )
    return verifier.verify_permit(run.permit, team_id=team_id, check_expiry=False).person_id


__all__ = [
    "LIVE_CHECK_CACHE_SIZE",
    "LIVE_CHECK_SECONDS",
    "ScheduleFacts",
    "ScheduleFactsSource",
    "ScheduleRunLookup",
    "StoredScheduleFacts",
    "job_schedule_id",
    "resolve_task_identity",
    "reported_task_person",
]
