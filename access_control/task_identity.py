"""Who an agent task works for: the one check every enforcement point uses.

A task proves whom it works for in one of two ways, carried in its
``GatewayContext``:

- a protocol 1 root-task authority token (``task_context``, verified by
  ``access_control.authority_verifier.AuthorityTokenVerifier``): the gateway
  signs one with its host certificate when it admits a person's task, and the
  team services' scheduler signs one for a scheduled run;
- a schedule run identity (``schedule_run``, Step 5 on the old wrapper): a
  scheduled run started with its schedule's standing scheduler-signed permit
  token plus the run's own unsigned fields (a fresh ``schedule_run_<hex>``
  context id, the conversation and the start time). Bare permit tokens
  (``schedule_permit:*`` / ``schedule_owner_permit:*``) are refused if
  presented directly in ``task_context``.

``resolve_task_identity`` (and its async counterpart
``resolve_task_identity_async``, which fetches a missing issuer certificate or
stale revocations record once on demand before verifying) reads only verified
protocol 1 tokens and returns the uniform ``TaskContextV1`` shape carrying the
signing certificate's verified ``issuer_roles``.

For each permit-backed run identity this checks, live, that the schedule still
exists, is turned on and is still owned by the permit's person, and reads that
person's current standing and roles from the team's people copy
(``access_control.people_directory``). A disabled or removed person, a deleted
or turned-off schedule, or a changed owner refuses the run's next tool call.
The live facts are cached for ``LIVE_CHECK_SECONDS`` (30 seconds) per schedule
and permit token, bounded to ``LIVE_CHECK_CACHE_SIZE`` entries.

Effective lifetime of a permit-backed run (F3): a standing permit lasts at most
7 days and is renewed by ``team_services.scheduler`` when fewer than 2 days
remain, while the run itself lasts ``DEFAULT_RUN_LIFETIME`` (7 days) from
``started_at_utc``. When a long or paused run outlives the frozen permit it
started with, ``_run_context`` verifies the frozen permit's signature and
binding without expiry and falls back to the schedule's current stored permit
(same schedule, same person, same binding kind, verified now). If renewal was
refused or no matching current permit is stored, the run stops.

Signed "no roles" marker (F2): any token carrying the ``unvouched_run_`` request
id prefix or ``schedule_unvouched_permit:<schedule_id>`` binding resolves with
``person_roles=()`` whoever issued it, with no live lookup.

A permit for no person (system work) is accepted only for a turned-on,
runtime-managed schedule whose kind the system schedule table marks
``system_work`` (``custom_messages.system_schedules``).

Steps 6–7 handoff note: once queued work moves onto protocol 1 ``task.request``
frames whose ``verify_frame`` compares ``/body/work/root_task_id`` with
``token.bound_to.id``, Steps 6–7 must replace the ``ScheduleRunIdentityV1``
wrapper path with per-run tokens or scheduler-dispatched frames.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from access_control.authority_verifier import (
    AuthorityTokenVerifier,
    FETCHABLE_REFUSAL_CODES,
    IdentityRejected,
    IssuerRequirement,
    REFUSAL_BAD_AUTHORITY,
    REFUSAL_EXPIRED,
    REFUSAL_INVALID_PERMIT,
    REFUSAL_MALFORMED,
    REFUSAL_MISSING,
    VerifiedTaskToken,
)
from access_control.people_directory import PeopleDirectory, PeopleStore, PersonNow
from custom_messages.contracts.access_v2 import (
    ScheduleRunIdentityV1,
    TaskContextV1,
)
from custom_messages.enums import ScheduleManagementClass, SystemScheduleKind
from custom_messages.system_schedules import is_system_work_kind
from custom_messages.wire.authority_bindings import (
    AUTHORITY_TOKEN_MAX_LIFETIME_SECONDS,
    AuthorityTokenRecord,
    GATEWAY_ISSUER_ROLE,
    SCHEDULER_ISSUER_ROLE,
    SCHEDULE_OWNER_PERMIT_PREFIX,
    SCHEDULE_RUN_CONTEXT_PREFIX,
)
from storage.config import StorageSettings

logger = logging.getLogger(__name__)

LIVE_CHECK_SECONDS = 30.0
LIVE_CHECK_CACHE_SIZE = 1024
DEFAULT_RUN_LIFETIME = timedelta(seconds=AUTHORITY_TOKEN_MAX_LIFETIME_SECONDS)
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
    # The schedule's current standing permits as stored now (used by F3 fallback when a frozen permit expires).
    run_permit_json: str | None = None
    owner_permit_json: str | None = None

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
        run_permit_json = str(row[21]) if len(row) > 21 and row[21] else None
        owner_permit_json = str(row[23]) if len(row) > 23 and row[23] else None
        return ScheduleFacts(
            schedule_id=schedule_id,
            enabled=bool(row[9]),
            owner_id=owner or None,
            system_managed=system_managed,
            system_kind=str(getattr(kind, "value", kind) or SystemScheduleKind.UNCLASSIFIED.value)
            if system_managed
            else None,
            runner_type=runner or None,
            run_permit_json=run_permit_json,
            owner_permit_json=owner_permit_json,
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

    def facts(self, permit: VerifiedTaskToken) -> tuple[ScheduleFacts | None, PersonNow | None]:
        """The schedule row and the permit's person as recorded now (cached per team, schedule and token)."""

        schedule_id = str(permit.schedule_id or "")

        def read() -> _LiveFacts:
            schedule = self.schedules.schedule_facts(schedule_id)
            person = (
                PeopleDirectory(self.people, permit.team_id).person(permit.person_id) if permit.person_id else None
            )
            return _LiveFacts(schedule, person, self.clock())

        found = self._cached((permit.team_id, schedule_id, permit.token_id), read)
        return found.schedule, found.person

    def schedule(self, schedule_id: str) -> ScheduleFacts | None:
        """The schedule row as recorded now, cached like ``facts``."""

        return self._cached(
            ("", schedule_id, ""), lambda: _LiveFacts(self.schedules.schedule_facts(schedule_id), None, self.clock())
        ).schedule

    def refresh_schedule(self, schedule_id: str) -> ScheduleFacts | None:
        """Re-read a schedule row directly from storage and update the cache."""

        found = _LiveFacts(self.schedules.schedule_facts(schedule_id), None, self.clock())
        key = ("", schedule_id, "")
        with self._lock:
            self._cache[key] = found
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
        return found.schedule

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


def verified_token_to_task_context(
    verified: VerifiedTaskToken,
    *,
    conversation_id: str | None = None,
) -> TaskContextV1:
    """Convert a verified protocol 1 root-task token into the uniform ``TaskContextV1`` view."""

    issuer_kind = verified.issuer_kind
    if issuer_kind not in (GATEWAY_ISSUER_ROLE, SCHEDULER_ISSUER_ROLE):
        raise IdentityRejected(
            "The authority token's issuer certificate holds neither the gateway nor the scheduler role.",
            code=REFUSAL_BAD_AUTHORITY,
        )
    return TaskContextV1(
        context_id=verified.request_id,
        issuer=issuer_kind,
        issuer_roles=tuple(sorted(verified.issuer_roles)),
        team_id=verified.team_id,
        person_id=verified.person_id,
        person_roles=() if verified.is_unvouched else verified.person_roles,
        root_task_id=verified.root_task_id,
        conversation_id=conversation_id,
        schedule_id=verified.schedule_id,
        issued_at_utc=verified.issued_at,
        expires_at_utc=verified.expires_at,
    )


def _live_problem(permit: VerifiedTaskToken, schedule: ScheduleFacts | None, person: PersonNow | None) -> str | None:
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


def _run_times(run: ScheduleRunIdentityV1, check_moment: datetime) -> tuple[datetime, datetime]:
    """The run's start and end, as a task context would state them; raises when outside them."""

    started = run.started_at_utc
    if started > check_moment + RUN_START_SKEW:
        raise IdentityRejected("The scheduled run says it starts in the future.", code=REFUSAL_EXPIRED)
    expires = started + DEFAULT_RUN_LIFETIME
    if check_moment >= expires:
        raise IdentityRejected("The scheduled run's identity has expired.", code=REFUSAL_EXPIRED)
    return started, expires


def _parse_schedule_run(run_value: ScheduleRunIdentityV1 | Mapping[str, Any]) -> ScheduleRunIdentityV1:
    try:
        run = (
            run_value
            if isinstance(run_value, ScheduleRunIdentityV1)
            else ScheduleRunIdentityV1.model_validate(run_value)
        )
    except Exception as exc:
        raise IdentityRejected(f"The scheduled run identity is malformed: {exc}", code=REFUSAL_MALFORMED) from exc
    if not run.context_id.startswith(SCHEDULE_RUN_CONTEXT_PREFIX):
        raise IdentityRejected(
            "A permit-backed scheduled run identity must use a schedule_run_ request id.",
            code=REFUSAL_INVALID_PERMIT,
        )
    return run


def _people_override(verifier: AuthorityTokenVerifier, schedule_runs: ScheduleRunLookup | None) -> PeopleStore | None:
    if schedule_runs is not None and not verifier.has_people:
        return schedule_runs.people
    return None


def _current_permit_json(facts: ScheduleFacts | None, bound_id: str) -> str | None:
    if facts is None:
        return None
    if bound_id.startswith(SCHEDULE_OWNER_PERMIT_PREFIX):
        return facts.owner_permit_json
    return facts.run_permit_json


def _fallback_current_permit(
    run: ScheduleRunIdentityV1,
    *,
    frozen_exc: IdentityRejected,
    verifier: AuthorityTokenVerifier,
    team_id: str,
    schedule_runs: ScheduleRunLookup | None,
    moment: datetime,
    valid_at: datetime | None,
    reached_at: datetime | None,
    people_override: PeopleStore | None,
) -> VerifiedTaskToken:
    """When a run's frozen permit expired, verify and adopt the schedule's current renewed permit (F3).

    Verifies the frozen permit's signature, issuer, revocations and binding without expiry
    first, then reads the schedule's current stored permit matching the same schedule, person
    and binding kind (regular, unvouched, or owner permit) verified at the check moment.
    """

    frozen = verifier.verify_schedule_permit(
        run.permit,
        team_id=team_id,
        now=moment,
        check_expiry=False,
        require_active_person=False,
        people=people_override,
    )
    if schedule_runs is None or frozen.schedule_id is None:
        raise frozen_exc
    facts = schedule_runs.schedule(frozen.schedule_id)
    raw_current = _current_permit_json(facts, frozen.root_task_id)
    if not raw_current or frozen.token_id in raw_current:
        facts = schedule_runs.refresh_schedule(frozen.schedule_id)
        raw_current = _current_permit_json(facts, frozen.root_task_id)
    if not raw_current:
        raise frozen_exc
    try:
        candidate = AuthorityTokenRecord.model_validate_json(raw_current)
    except ValueError as exc:
        raise frozen_exc from exc
    if candidate.token_id == frozen.token_id:
        raise frozen_exc
    current = verifier.verify_schedule_permit(
        candidate,
        team_id=team_id,
        now=moment,
        valid_at=valid_at,
        reached_at=reached_at,
        people=people_override,
    )
    if (
        current.schedule_id != frozen.schedule_id
        or current.person_id != frozen.person_id
        or current.root_task_id != frozen.root_task_id
    ):
        raise IdentityRejected(
            "The schedule's current permit does not match this run.",
            code=REFUSAL_INVALID_PERMIT,
        )
    return current


def _run_context(
    run_value: ScheduleRunIdentityV1 | Mapping[str, Any],
    *,
    verifier: AuthorityTokenVerifier,
    team_id: str,
    schedule_runs: ScheduleRunLookup | None,
    moment: datetime,
    valid_at: datetime | None,
    reached_at: datetime | None,
    require_issuer: IssuerRequirement,
) -> TaskContextV1:
    if require_issuer == GATEWAY_ISSUER_ROLE:
        raise IdentityRejected(
            "Only a gateway-signed authority token is accepted here.",
            code=REFUSAL_BAD_AUTHORITY,
        )
    run = _parse_schedule_run(run_value)
    people_override = _people_override(verifier, schedule_runs)
    try:
        permit = verifier.verify_schedule_permit(
            run.permit,
            team_id=team_id,
            now=moment,
            valid_at=valid_at,
            reached_at=reached_at,
            people=people_override,
        )
    except IdentityRejected as frozen_exc:
        if frozen_exc.code != REFUSAL_EXPIRED:
            raise
        permit = _fallback_current_permit(
            run,
            frozen_exc=frozen_exc,
            verifier=verifier,
            team_id=team_id,
            schedule_runs=schedule_runs,
            moment=moment,
            valid_at=valid_at,
            reached_at=reached_at,
            people_override=people_override,
        )
    check_moment = valid_at or reached_at or moment
    started, expires = _run_times(run, check_moment)
    if schedule_runs is None:
        raise IdentityRejected(
            "This process cannot check scheduled runs (it has no access to the schedules).",
            code=REFUSAL_INVALID_PERMIT,
        )
    schedule, person = schedule_runs.facts(permit)
    problem = _live_problem(permit, schedule, person)
    if problem is not None:
        raise IdentityRejected(problem, code=REFUSAL_INVALID_PERMIT)
    return TaskContextV1(
        context_id=run.context_id,
        issuer=SCHEDULER_ISSUER_ROLE,
        issuer_roles=tuple(sorted(permit.issuer_roles)),
        team_id=permit.team_id,
        person_id=permit.person_id,
        person_roles=() if (person is None or permit.is_unvouched) else tuple(sorted(set(person.roles))),
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
    (``ScheduleFacts.worker_job``). Only the scheduler says a task is a
    schedule's run: a token signed by a certificate holding the ``scheduler``
    role, or a schedule run identity checked against a scheduler-signed permit.
    """

    if context.schedule_id is None:
        return None
    scheduler_signed = context.issuer == SCHEDULER_ISSUER_ROLE and (
        not context.issuer_roles or SCHEDULER_ISSUER_ROLE in context.issuer_roles
    )
    if not scheduler_signed:
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
    verifier: AuthorityTokenVerifier,
    team_id: str,
    task_context: AuthorityTokenRecord | Mapping[str, Any] | Any | None,
    schedule_run: ScheduleRunIdentityV1 | Mapping[str, Any] | None,
    schedule_runs: ScheduleRunLookup | None,
    now: datetime | None = None,
    valid_at: datetime | None = None,
    reached_at: datetime | None = None,
    require_issuer: IssuerRequirement = "any",
) -> TaskContextV1:
    """Who the task works for, from its verified protocol 1 token or schedule run identity.

    Raises ``IdentityRejected`` when neither proves it. A task carries one or
    the other, never both. Pass ``valid_at`` / ``reached_at`` from
    ``InboundDelivery.stored_at`` when admitting queued JetStream messages
    (Decision 52).
    """

    moment = now if now is not None else verifier.now()
    if schedule_run is None:
        verified = verifier.verify_root_task(
            task_context,
            team_id=team_id,
            now=moment,
            valid_at=valid_at,
            reached_at=reached_at,
            require_issuer=require_issuer,
            people=_people_override(verifier, schedule_runs),
        )
        if verified.person_id is None and verified.schedule_id is None:
            raise IdentityRejected(
                "An authority token must name either a person or a schedule.",
                code=REFUSAL_BAD_AUTHORITY,
            )
        return verified_token_to_task_context(verified)
    if task_context is not None:
        raise IdentityRejected(
            "The task carries both an authority token and a scheduled run identity.",
            code=REFUSAL_MALFORMED,
        )
    return _run_context(
        schedule_run,
        verifier=verifier,
        team_id=team_id,
        schedule_runs=schedule_runs,
        moment=moment,
        valid_at=valid_at,
        reached_at=reached_at,
        require_issuer=require_issuer,
    )


async def resolve_task_identity_async(
    *,
    verifier: AuthorityTokenVerifier,
    team_id: str,
    task_context: AuthorityTokenRecord | Mapping[str, Any] | Any | None,
    schedule_run: ScheduleRunIdentityV1 | Mapping[str, Any] | None,
    schedule_runs: ScheduleRunLookup | None,
    now: datetime | None = None,
    valid_at: datetime | None = None,
    reached_at: datetime | None = None,
    require_issuer: IssuerRequirement = "any",
) -> TaskContextV1:
    """Async resolver: fetches a missing issuer certificate or stale revocations once before verifying (C12)."""

    if schedule_run is not None and task_context is not None:
        raise IdentityRejected(
            "The task carries both an authority token and a scheduled run identity.",
            code=REFUSAL_MALFORMED,
        )
    raw = schedule_run if schedule_run is not None else task_context
    if raw is None:
        raise IdentityRejected(
            "The task has no authority token, so nobody vouches for who it works for.",
            code=REFUSAL_MISSING,
        )
    await verifier.ensure_issuer_known(raw, now=now)
    try:
        return resolve_task_identity(
            verifier=verifier,
            team_id=team_id,
            task_context=task_context,
            schedule_run=schedule_run,
            schedule_runs=schedule_runs,
            now=now,
            valid_at=valid_at,
            reached_at=reached_at,
            require_issuer=require_issuer,
        )
    except IdentityRejected as refused:
        if schedule_run is None or schedule_runs is None or refused.code not in FETCHABLE_REFUSAL_CODES:
            raise
        # A renewed current permit (F3 fallback) may carry a newer scheduler certificate epoch.
        run = _parse_schedule_run(schedule_run)
        schedule_id = getattr(run.permit, "schedule_id", None)
        bound_id = str(getattr(getattr(run.permit, "bound_to", None), "id", "") or "")
        if not isinstance(schedule_id, str) or not schedule_id:
            raise
        raw_current = _current_permit_json(schedule_runs.schedule(schedule_id), bound_id)
        if not raw_current:
            raise
        try:
            candidate = AuthorityTokenRecord.model_validate_json(raw_current)
        except ValueError:
            raise refused from None
        if not await verifier.ensure_issuer_known(candidate, now=now):
            raise
        return resolve_task_identity(
            verifier=verifier,
            team_id=team_id,
            task_context=task_context,
            schedule_run=schedule_run,
            schedule_runs=schedule_runs,
            now=now,
            valid_at=valid_at,
            reached_at=reached_at,
            require_issuer=require_issuer,
        )


def reported_task_person(
    *,
    verifier: AuthorityTokenVerifier,
    team_id: str,
    task_context: AuthorityTokenRecord | Mapping[str, Any] | Any | None,
    schedule_run: ScheduleRunIdentityV1 | Mapping[str, Any] | None,
) -> str | None:
    """Whose completed work is reported, from the original signature only.

    Historical attribution, never authority to execute. Checks signature,
    certificate, team, installation, and current revocations without rejecting
    on token expiry or a person who has since been disabled or removed.
    """

    if schedule_run is None:
        return verifier.verify_root_task(
            task_context,
            team_id=team_id,
            check_expiry=False,
            require_active_person=False,
        ).person_id
    if task_context is not None:
        raise IdentityRejected(
            "The task carries both an authority token and a scheduled run identity.",
            code=REFUSAL_MALFORMED,
        )
    run = _parse_schedule_run(schedule_run)
    return verifier.verify_schedule_permit(
        run.permit,
        team_id=team_id,
        check_expiry=False,
        require_active_person=False,
    ).person_id


@dataclass(frozen=True)
class ReportedScheduleRun:
    """Which schedule a finished run belonged to, whom it worked for, and when it started."""

    schedule_id: str
    person_id: str | None
    started_at_utc: datetime


def reported_schedule_run(
    *,
    verifier: AuthorityTokenVerifier,
    team_id: str,
    task_context: AuthorityTokenRecord | Mapping[str, Any] | Any | None,
    schedule_run: ScheduleRunIdentityV1 | Mapping[str, Any] | None,
) -> ReportedScheduleRun | None:
    """The schedule and person of a finished scheduled run, from its original signature only."""

    if schedule_run is None:
        verified = verifier.verify_root_task(
            task_context,
            team_id=team_id,
            check_expiry=False,
            require_active_person=False,
        )
        if not verified.is_scheduler_issued or not verified.schedule_id:
            return None
        return ReportedScheduleRun(verified.schedule_id, verified.person_id, verified.issued_at)
    if task_context is not None:
        raise IdentityRejected(
            "The task carries both an authority token and a scheduled run identity.",
            code=REFUSAL_MALFORMED,
        )
    run = _parse_schedule_run(schedule_run)
    permit = verifier.verify_schedule_permit(
        run.permit,
        team_id=team_id,
        check_expiry=False,
        require_active_person=False,
    )
    assert permit.schedule_id is not None
    return ReportedScheduleRun(permit.schedule_id, permit.person_id, run.started_at_utc)


__all__ = [
    "DEFAULT_RUN_LIFETIME",
    "LIVE_CHECK_CACHE_SIZE",
    "LIVE_CHECK_SECONDS",
    "ReportedScheduleRun",
    "ScheduleFacts",
    "ScheduleFactsSource",
    "ScheduleRunLookup",
    "StoredScheduleFacts",
    "job_schedule_id",
    "reported_schedule_run",
    "reported_task_person",
    "resolve_task_identity",
    "resolve_task_identity_async",
    "verified_token_to_task_context",
]
