"""The ``schedules`` section: schedules, where they deliver, the heartbeat list and worker setup.

What travels, and why:

- **Schedules** (``ScheduledMessage``, each as its own document with its
  remaining run count). They keep their ids, on/off state and pause (owner's
  decision). Recurring schedules arrive without a next run time, so the store
  computes it from the import time and runs missed during the move are
  skipped. A one-time schedule keeps its time; if that time passed during the
  move it arrives turned off and is listed. Claims, run permits, run identity
  and the run notes' ledger and state are runtime data and stay behind; the
  manager issues new permits once the gateway has recorded the people.
- **Which schedules.** People's schedules travel when their owner is
  imported. Schedules the runtime manages travel when they are configuration:
  memory reviews, internal-improvement maintenance, Objective work sessions
  (when their objective is imported) and unclassified ones (kept protected as
  they were). The heartbeat launcher is replaced at every gateway start (its
  on/off is the heartbeat setting the ``settings`` section carries), pickup
  checks are obsolete, and after-action reviews, provider-overload retries and
  recovery checkpoints are one-time follow-ups of work that does not move;
  these are listed as not imported. A team maintenance schedule (team memory
  review, internal improvement) whose person is not imported still travels:
  the gateway names a current person at its next start.
- **Who a person's schedule works for.** The gateway signed the context saved
  with a person's schedule, and every run checks that signature
  (``task_context_runtime.gateway_vouches``) before giving the run its
  owner's roles. The new instance has its own signing key, so the import signs
  the context again for the owner, with their imported roles. Only a context
  the old instance's gateway key vouched for is signed again (checked at
  export with the old instance's key), so the move never vouches for something
  nobody vouched for. Re-signing does not change the schedule's execution
  digest (the signed context is not part of it). Contexts saved with
  system-managed schedules are not used to vouch (trusted code set their
  owner) and the gateway refreshes them at start, so they travel unchanged.
- **Sessions** (routing records only). A person's schedule delivers through a
  chat session, which no other section carries. This section carries the
  sessions carried schedules and delivery targets use, and each imported
  person's current session per chat, with their names and chat routes but no
  conversation: the session's conversation and last task are history and stay
  behind. Whether a session is its chat's current one travels too.
- **Delivery targets** (``gateway_scheduled_targets``). Their conversation id
  is kept: it is the id the schedule's own runs use (saved in the schedule's
  task meta), so replies of the first run after the move still find their
  schedule. No conversation content moves with it.
- **Heartbeat list** (source and items). Same instance name, same source id.
  Run times (``last_*``) are runtime data and stay behind.
- **Worker bindings** of people's schedules (``ScheduleWorkerBindingV1``),
  written through the repository's own guarded ``configure`` as revision 1.
  Their saved definition records are review evidence and stay behind (the
  runtime records the current definition again). A binding that was waiting
  for review is imported unblocked and listed: the new instance checks it
  again at its next run. Bindings of system-managed schedules need a schedule
  permit and are left for TeamLead to set up again.
- **Worker pool sizes** TeamArchitect set: the lasting size, and a temporary
  size only while it lasts.
- **Automation jobs and managed services** depend on code revisions, builds
  and artifact files this version of the transfer does not carry yet. They are
  listed so the operator sets them up again; SSH sessions are live leases and
  are not listed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Iterable, Optional

from pydantic import BaseModel

from team_transfer.schedule_records import (
    CarriedScheduleV2,
    ExecutionRecordV1,
    HeartbeatItemV1,
    HeartbeatSourceV1,
    ScheduledTargetV1,
    ScheduleRecordKind,
    ScheduleTransferRecordV2,
    SessionRouteV1,
    gateway_signing_key,
    is_one_time,
    record_key,
    run_time_passed,
)
from team_transfer.schedule_writers import ScheduleWriter
from team_transfer.sections import (
    SECTION_UPGRADES,
    ExportSource,
    ImportContext,
    PlannedRecord,
    RecordOutcome,
    SectionExport,
    SectionFiles,
    SectionRecords,
)

if TYPE_CHECKING:
    from access_control.identity import ContextVerifier
    from custom_messages.contracts.schedule_v1 import ScheduledMessage

logger = logging.getLogger(__name__)

_ACTIVE_SESSION = "active"
_TEAM_SCOPE = "*"
_NOT_CARRIED_YET = (
    "not carried by the team transfer yet; set it up again in the new instance"
)


def _record(kind: ScheduleRecordKind, record_id: str, **payload: Any) -> dict[str, Any]:
    return ScheduleTransferRecordV2(
        kind=kind, record_id=record_id, **payload
    ).model_dump(mode="json")


# -- which schedules travel ----------------------------------------------------------


def _kind_reasons() -> dict[str, str]:
    """System schedule kinds that do not travel, with the reason listed for each."""

    from custom_messages.enums import SystemScheduleKind

    one_time = "a one-time follow-up of work that does not move"
    return {
        SystemScheduleKind.HEARTBEAT_LAUNCHER.value: (
            "the gateway sets it up again at every start; whether it runs is the heartbeat setting, which is carried"
        ),
        SystemScheduleKind.DELEGATION_ASSURANCE.value: "an obsolete pickup check; this version no longer runs it",
        SystemScheduleKind.AFTER_ACTION_REPORT_REVIEW.value: one_time,
        SystemScheduleKind.PROVIDER_OVERLOAD_RETRY.value: one_time,
        SystemScheduleKind.ISOLATED_RECOVERY_CHECKPOINT.value: one_time,
    }


def _team_kinds() -> frozenset[str]:
    """System schedules for the whole team (``scope`` ``TEAM`` in ``custom_messages.system_schedules``).

    They travel without their person: the gateway names their current person
    at each start. Team kinds that do not travel at all (the heartbeat
    launcher, after-action report reviews) are turned away first by
    ``_kind_reasons``.
    """

    from custom_messages.system_schedules import SystemScheduleScope, system_schedule_specs

    return frozenset(spec.kind.value for spec in system_schedule_specs() if spec.scope is SystemScheduleScope.TEAM)


def _expired(schedule: "ScheduledMessage", now: datetime) -> bool:
    if not schedule.expires_at_utc:
        return False
    end = datetime.fromisoformat(schedule.expires_at_utc.replace("Z", "+00:00"))
    return (end if end.tzinfo else end.replace(tzinfo=timezone.utc)) <= now


def _old_verifier(source: ExportSource) -> Optional["ContextVerifier"]:
    from access_control.identity import (
        ISSUER_GATEWAY,
        ContextVerifier,
        public_entry,
        verify_keys_value,
    )

    value = gateway_signing_key(source.config, source.layout)
    if value is None:
        logger.warning(
            "The exported instance has no gateway signing key, so no person's schedule counts as vouched for. "
            "event=team_transfer_schedules_no_old_key"
        )
        return None
    return ContextVerifier.from_value(
        verify_keys_value([public_entry(ISSUER_GATEWAY, value)])
    )


def _vouched(
    verifier: Optional["ContextVerifier"],
    schedule: "ScheduledMessage",
    owner: Optional[str],
) -> bool:
    """``gateway_vouches`` under the old instance's own key (the export runs where that key is)."""

    from access_control.identity import ISSUER_GATEWAY, IdentityRejected

    gateway_context = getattr(schedule.payload_meta, "gateway_context", None)
    signed = getattr(gateway_context, "task_context", None)
    if verifier is None or signed is None or not owner:
        return False
    try:
        context = verifier.verify(signed, check_expiry=False)
    except IdentityRejected:
        return False
    return context.issuer == ISSUER_GATEWAY and context.person_id == owner


# -- export ---------------------------------------------------------------------------


def _schedule_store(config: Any) -> Any:
    from custom_agents.schedule_store import ScheduleStore

    return ScheduleStore(
        storage_settings=config.manager_storage_settings,
        default_cron_timezone=config.user.timezone,
    )


def _gateway_repository(config: Any) -> Any:
    from storage.gateway_state_repository import create_gateway_state_repository

    return create_gateway_state_repository(config.state_storage_settings)


def _without_runtime_data(schedule: "ScheduledMessage") -> "ScheduledMessage":
    """The schedule as configuration: no run identity, no run ledger or state (module docstring)."""

    from custom_messages.contracts.schedule_v1 import (
        ScheduleMemoryLedger,
        ScheduleMemoryState,
    )

    copy = schedule.model_copy(deep=True)
    gateway_context = getattr(copy.payload_meta, "gateway_context", None)
    if gateway_context is not None:
        gateway_context.schedule_run = None
    if copy.schedule_memory is not None:
        copy.schedule_memory = copy.schedule_memory.model_copy(
            update={"ledger": ScheduleMemoryLedger(), "state": ScheduleMemoryState()}
        )
    return copy


@dataclass
class _Export:
    """Collects the section's records while reading the old instance."""

    source: ExportSource
    records: list[dict[str, Any]] = field(default_factory=list)
    session_keys: set[str] = field(default_factory=set)

    def schedules(self) -> list[str]:
        from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
        from team_transfer.errors import TransferError

        store = _schedule_store(self.source.config)
        verifier = _old_verifier(self.source)
        ids = []
        for row in store.all_rows():
            schedule_id = str(row["schedule_id"])
            schedule = store.get_scheduled_message(schedule_id)
            if schedule is None:
                raise TransferError(
                    f"The schedule {schedule_id} cannot be read, so the export stops."
                )
            owner = owner_canonical_user_id_for_schedule(schedule)
            from team_transfer.schedule_markdown import render_schedule

            entry = CarriedScheduleV2(
                remaining_count=row.get("remaining_count"),
                owner_vouched=_vouched(verifier, schedule, owner),
                markdown=render_schedule(
                    _without_runtime_data(schedule).model_dump(mode="json")
                ),
            )
            self.records.append(
                _record(ScheduleRecordKind.SCHEDULE, schedule_id, schedule=entry)
            )
            self._uses_session(
                getattr(
                    getattr(schedule.payload_meta, "gateway_context", None),
                    "session_key",
                    None,
                )
            )
            ids.append(schedule_id)
        return ids

    def targets(self, schedule_ids: list[str]) -> None:
        repository = _gateway_repository(self.source.config)
        for schedule_id in schedule_ids:
            target = repository.get_scheduled_target(schedule_id)
            if target is None:
                continue
            carried = ScheduledTargetV1(
                schedule_id=schedule_id,
                session_key=target.session_key,
                conversation_id=target.conversation_id,
                owner_person_id=target.owner_canonical_user_id,
            )
            self.records.append(
                _record(
                    ScheduleRecordKind.SCHEDULED_TARGET, schedule_id, target=carried
                )
            )
            self._uses_session(target.session_key)

    def sessions(self) -> None:
        repository = _gateway_repository(self.source.config)
        current = _current_sessions(
            repository, _team_people(repository, self.source.team_id)
        )
        sessions = []
        for session_key in sorted(self.session_keys | set(current)):
            session = repository.get_session(session_key)
            if session is None:
                logger.warning(
                    "A schedule names a chat session that does not exist; it is not carried. "
                    "event=team_transfer_session_missing session_key=%s",
                    session_key,
                )
                continue
            route = _session_route(session, channel_current=session_key in current)
            sessions.append(
                _record(ScheduleRecordKind.SESSION, session_key, session=route)
            )
        # Sessions first: schedules and delivery targets use them.
        self.records[:0] = sessions

    def heartbeat(self) -> None:
        from custom_agents.heartbeat_store import HeartbeatStore

        source_id = self.source.config.heartbeat.source_id
        store = HeartbeatStore(
            storage_settings=self.source.config.manager_storage_settings
        )
        found = store.get_source(source_id) if source_id else None
        if found is None:
            return
        source = HeartbeatSourceV1(
            source_id=found.source_id, title=found.title, enabled=found.enabled
        )
        self.records.append(
            _record(
                ScheduleRecordKind.HEARTBEAT_SOURCE,
                found.source_id,
                heartbeat_source=source,
            )
        )
        for item in store.list_items(found.source_id, enabled_only=False):
            carried = HeartbeatItemV1(
                heartbeat_item_id=item.heartbeat_item_id,
                source_id=item.source_id,
                title=item.title,
                instruction=item.instruction,
                conversation_id=item.conversation_id,
                target_agent_type=item.target_agent_type,
                objective_id=item.objective_id,
                enabled=item.enabled,
                source_key=item.source_key,
            )
            self.records.append(
                _record(
                    ScheduleRecordKind.HEARTBEAT_ITEM,
                    item.heartbeat_item_id,
                    heartbeat_item=carried,
                )
            )

    def workers(self) -> None:
        from storage.schedule_worker_repository import ScheduleWorkerRepository
        from storage.worker_pool_repository import WorkerPoolRepository

        storage = self.source.config.manager_storage_settings
        for binding in ScheduleWorkerRepository(storage).list_bindings(
            self.source.team_id
        ):
            document = binding.model_dump(mode="json")
            self.records.append(
                _record(
                    ScheduleRecordKind.WORKER_BINDING,
                    binding.schedule_id,
                    document=document,
                )
            )
        for limit in WorkerPoolRepository(storage).list_limits(self.source.team_id):
            self.records.append(
                _record(
                    ScheduleRecordKind.POOL_LIMIT,
                    limit.scope,
                    document=limit.model_dump(mode="json"),
                )
            )

    def execution(self) -> None:
        from storage.read_only import repositories_are_read_only

        if repositories_are_read_only():
            # Execution records are refused on import; they cannot be a
            # dependency of any accepted record in a later batch.
            return
        from storage.execution_control_repository import (
            create_execution_control_repository,
        )

        repository = create_execution_control_repository(
            self.source.config.manager_storage_settings
        )
        for kind, records, id_field in (
            (
                ScheduleRecordKind.EXECUTION_JOB,
                repository.list_jobs(team_id=self.source.team_id),
                "job_id",
            ),
            (
                ScheduleRecordKind.MANAGED_SERVICE,
                repository.list_managed_services(team_id=self.source.team_id),
                "service_id",
            ),
        ):
            for record in records:
                record_id = getattr(record, id_field)
                listed = ExecutionRecordV1(
                    record_id=record_id,
                    name=record.name,
                    owner_kind=record.owner.scope_kind.value,
                    owner_id=record.owner.owner_id,
                )
                self.records.append(_record(kind, record_id, execution=listed))

    def _uses_session(self, session_key: Optional[str]) -> None:
        if session_key:
            self.session_keys.add(session_key)


def _team_people(repository: Any, team_id: str) -> list[str]:
    from access_control.people import MEMBERSHIP_STATUS_REMOVED

    return sorted(
        member.canonical_user_id
        for member in repository.list_team_people(team_id)
        if member.membership_status != MEMBERSHIP_STATUS_REMOVED
    )


def _current_sessions(repository: Any, people: Iterable[str]) -> set[str]:
    """Each person's sessions that are their chat's current one."""

    current: set[str] = set()
    for person in people:
        count = repository.count_sessions(
            status=_ACTIVE_SESSION, owner_canonical_user_id=person
        )
        if not count:
            continue
        sessions = repository.list_sessions(
            limit=count, status=_ACTIVE_SESSION, owner_canonical_user_id=person
        )
        current.update(
            session.session_key
            for session in sessions
            if _chat_current(repository, session)
        )
    return current


def _chat_current(repository: Any, session: Any) -> bool:
    if not (session.channel_name and session.user_id and session.chat_id):
        return False
    found = repository.get_active_session_for_channel(
        channel_name=session.channel_name,
        user_id=session.user_id,
        chat_id=session.chat_id,
        thread_id=session.thread_id,
    )
    return found is not None and found.session_key == session.session_key


def _session_route(session: Any, *, channel_current: bool) -> SessionRouteV1:
    return SessionRouteV1(
        session_key=session.session_key,
        session_alias=session.session_alias,
        manager_agent_id=session.manager_agent_id,
        channel_name=session.channel_name,
        user_id=session.user_id,
        chat_id=session.chat_id,
        thread_id=session.thread_id,
        owner_person_id=session.owner_canonical_user_id,
        status=session.status,
        channel_current=channel_current,
    )


# -- planning ---------------------------------------------------------------------------

_PLAN_ORDER = tuple(ScheduleRecordKind)


@dataclass
class _Known:
    """What the new instance will have, from this version and the sections planned before this one."""

    team_id: str
    people: set[str]
    objectives: set[str]
    agent_types: set[str]
    heartbeat_source_id: Optional[str]
    signing_key: bool
    now: datetime
    default_cron_timezone: str
    accepted: set[str] = field(default_factory=set)
    schedules: dict[str, "ScheduledMessage"] = field(default_factory=dict)

    @classmethod
    def build(cls, context: ImportContext) -> "_Known":
        from team_transfer.access import known_agent_types
        from team_transfer.settings import planned_config

        config = planned_config(context)
        objectives = {
            key.split("/", 1)[1]
            for key in context.accepted("objectives")
            if key.startswith("objective/")
        }
        known = cls(
            team_id=context.reader.manifest.team_id,
            people=context.accepted("people"),
            objectives=objectives,
            agent_types=known_agent_types(context),
            heartbeat_source_id=config.heartbeat.source_id,
            # An env-file instance gets its keys when the settings are imported.
            signing_key=(
                not config.launch.secrets_from_environment
                or gateway_signing_key(config, context.layout) is not None
            ),
            now=datetime.now(timezone.utc),
            default_cron_timezone=config.user.timezone,
        )
        from custom_messages.contracts.schedule_v1 import ScheduledMessage

        known.accepted.update(context.baseline_keys.get("schedules", ()))
        for raw in context.baseline.get("schedules", ()):
            record = ScheduleTransferRecordV2.model_validate(raw)
            if record.kind is ScheduleRecordKind.SCHEDULE:
                schedule = ScheduledMessage.model_validate(record.schedule.document)
                known.schedules[schedule.schedule_id] = schedule
        return known

    def person_problem(self, person_id: Optional[str]) -> str:
        if person_id and person_id not in self.people:
            return f"it belongs to {person_id}, who is not imported"
        return ""


_Outcome = tuple[RecordOutcome, str]
_IMPORT: _Outcome = (RecordOutcome.IMPORT, "")


def _refused(reason: str) -> _Outcome:
    return RecordOutcome.REFUSED, reason


def _attention(reasons: list[str]) -> _Outcome:
    return (RecordOutcome.ATTENTION, "; ".join(reasons)) if reasons else _IMPORT


def _plan_session(record: ScheduleTransferRecordV2, known: _Known) -> _Outcome:
    problem = known.person_problem(
        record.session.owner_person_id if record.session else None
    )
    return _refused(problem) if problem else _IMPORT


def _schedule_problem(schedule: "ScheduledMessage", known: _Known) -> str:
    """Why the schedule does not travel ("" when it does)."""

    from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
    from custom_messages.enums import SystemScheduleKind

    kind = schedule.effective_system_schedule_kind
    owner = owner_canonical_user_id_for_schedule(schedule)
    if kind is not None and kind.value in _kind_reasons():
        return _kind_reasons()[kind.value]
    if _expired(schedule, known.now):
        return "it expired before the import"
    if kind is None and not owner:
        return "it is a person's schedule that names no person"
    if (
        kind is SystemScheduleKind.OBJECTIVE_WORK_SESSION
        and schedule.objective_id not in known.objectives
    ):
        return f"its objective {schedule.objective_id} is not imported"
    if kind is not None and kind.value in _team_kinds():
        return ""
    return known.person_problem(owner)


def _schedule_attention(
    schedule: "ScheduledMessage", entry: CarriedScheduleV2, known: _Known
) -> list[str]:
    from custom_agents.schedule_store import owner_canonical_user_id_for_schedule

    reasons = []
    owner = owner_canonical_user_id_for_schedule(schedule)
    if schedule.enabled and run_time_passed(schedule, known.now):
        reasons.append("its one run was due during the move; it is turned off")
    if schedule.effective_system_schedule_kind is not None:
        if owner and owner not in known.people:
            reasons.append(
                f"its person {owner} is not imported; the gateway names a current person at its next start"
            )
    elif not entry.owner_vouched:
        reasons.append(
            "nobody vouched for its owner in the old instance either, so it runs without the owner's roles"
        )
    elif not known.signing_key:
        reasons.append(
            "runs without its owner's roles until signed again: the new instance has no gateway signing key"
        )
    agent_type = schedule.claim_named_agent_type
    if agent_type and agent_type not in known.agent_types:
        reasons.append(
            f"no agent type {agent_type} exists in the new instance, so nothing picks it up"
        )
    return reasons


def _plan_schedule(record: ScheduleTransferRecordV2, known: _Known) -> _Outcome:
    from custom_messages.contracts.schedule_v1 import ScheduledMessage

    entry: CarriedScheduleV2 = record.payload
    schedule = ScheduledMessage.model_validate(entry.document)
    if schedule.schedule_id != record.record_id:
        return _refused("its schedule ID does not match the transfer record")
    if is_one_time(schedule) and not schedule.next_fire_at:
        return _refused("it is a one-time schedule with no run time")
    timing_problem = _next_run_problem(schedule, known)
    if timing_problem:
        return _refused(timing_problem)
    problem = _schedule_problem(schedule, known)
    if problem:
        return _refused(problem)
    context = getattr(schedule.payload_meta, "gateway_context", None)
    session_key = getattr(context, "session_key", None)
    if (
        session_key
        and record_key(ScheduleRecordKind.SESSION, session_key) not in known.accepted
    ):
        return _refused(
            f"its chat session {session_key} is not selected or present in the target"
        )
    known.schedules[schedule.schedule_id] = schedule
    return _attention(_schedule_attention(schedule, entry, known))


def _next_run_problem(schedule: "ScheduledMessage", known: _Known) -> str:
    """Check the same recurrence the writer will compute after dropping old runs."""
    if is_one_time(schedule):
        return ""
    from zoneinfo import ZoneInfoNotFoundError
    from custom_agents.schedule_store import compute_next_fire_at, fill_cron_timezone

    candidate = schedule.model_copy(deep=True)
    try:
        fill_cron_timezone(candidate, known.default_cron_timezone)
        compute_next_fire_at(
            interval_ms=candidate.interval_ms,
            cron_expression=candidate.cron_expression,
            cron_timezone=candidate.cron_timezone,
            from_time=known.now,
        )
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        return "its timing cannot produce a next run; correct the recurrence and time zone"
    return ""


def _plan_target(record: ScheduleTransferRecordV2, known: _Known) -> _Outcome:
    target: ScheduledTargetV1 = record.payload
    if (
        record_key(ScheduleRecordKind.SCHEDULE, target.schedule_id)
        not in known.accepted
    ):
        return _refused(f"its schedule {target.schedule_id} is not imported")
    if record_key(ScheduleRecordKind.SESSION, target.session_key) not in known.accepted:
        return _refused(f"its chat session {target.session_key} is not imported")
    problem = known.person_problem(target.owner_person_id)
    return _refused(problem) if problem else _IMPORT


def _plan_heartbeat_source(record: ScheduleTransferRecordV2, known: _Known) -> _Outcome:
    source: HeartbeatSourceV1 = record.payload
    if source.source_id != known.heartbeat_source_id:
        return _refused(
            f"the new instance's heartbeat list is {known.heartbeat_source_id}"
        )
    return _IMPORT


def _plan_heartbeat_item(record: ScheduleTransferRecordV2, known: _Known) -> _Outcome:
    item: HeartbeatItemV1 = record.payload
    if (
        record_key(ScheduleRecordKind.HEARTBEAT_SOURCE, item.source_id)
        not in known.accepted
    ):
        return _refused(f"its heartbeat list {item.source_id} is not imported")
    if item.objective_id and item.objective_id not in known.objectives:
        return _refused(f"its objective {item.objective_id} is not imported")
    if item.target_agent_type and item.target_agent_type not in known.agent_types:
        return _refused(
            f"the agent type {item.target_agent_type} does not exist in the new instance"
        )
    return _IMPORT


def _plan_binding(record: ScheduleTransferRecordV2, known: _Known) -> _Outcome:
    from custom_agents.schedule_execution_identity import execution_digest
    from custom_agents.schedule_management import is_system_managed_schedule
    from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
    from custom_messages.contracts.schedule_worker_v1 import ScheduleWorkerBindingV1

    binding = ScheduleWorkerBindingV1.model_validate(record.document)
    schedule = known.schedules.get(binding.schedule_id)
    if schedule is None:
        return _refused(f"its schedule {binding.schedule_id} is not imported")
    if is_system_managed_schedule(schedule):
        return _refused(
            "the schedule is one the runtime manages; TeamLead sets up its worker again"
        )
    if (binding.owner_canonical_user_id, binding.team_id) != (
        owner_canonical_user_id_for_schedule(schedule),
        schedule.claim_team_id,
    ):
        return _refused("it names a different owner or team than its schedule")
    if binding.target_agent_type not in known.agent_types:
        return _refused(
            f"the agent type {binding.target_agent_type} does not exist in the new instance"
        )
    if binding.target_agent_type != schedule.claim_named_agent_type:
        return _refused(
            "its worker type differs from the edited schedule's worker type; set up its worker again in the destination"
        )
    reasons = []
    if binding.blocked_reason:
        reasons.append(
            f"it was waiting for review ({binding.blocked_reason}); the new instance checks it again"
        )
    if (
        execution_digest(schedule, binding.definition_digest)
        != binding.execution_digest
    ):
        reasons.append(
            "its schedule changed since the worker was set up, so the schedule waits for review"
        )
    return _attention(reasons)


def _plan_pool_limit(record: ScheduleTransferRecordV2, known: _Known) -> _Outcome:
    from storage.worker_pool_repository import WorkerPoolLimitV1

    limit = WorkerPoolLimitV1.model_validate(record.document)
    if limit.scope != _TEAM_SCOPE and limit.scope not in known.agent_types:
        return _refused(
            f"the agent type {limit.scope} does not exist in the new instance"
        )
    if limit.lasting_max_workers is None and not limit.temporary_active(known.now):
        return _refused("it held only a temporary size, which has ended")
    return _IMPORT


def _plan_execution(record: ScheduleTransferRecordV2, known: _Known) -> _Outcome:
    return _refused(_NOT_CARRIED_YET)


_PLANNERS: dict[
    ScheduleRecordKind, Callable[[ScheduleTransferRecordV2, _Known], _Outcome]
] = {
    ScheduleRecordKind.SESSION: _plan_session,
    ScheduleRecordKind.SCHEDULE: _plan_schedule,
    ScheduleRecordKind.SCHEDULED_TARGET: _plan_target,
    ScheduleRecordKind.HEARTBEAT_SOURCE: _plan_heartbeat_source,
    ScheduleRecordKind.HEARTBEAT_ITEM: _plan_heartbeat_item,
    ScheduleRecordKind.WORKER_BINDING: _plan_binding,
    ScheduleRecordKind.POOL_LIMIT: _plan_pool_limit,
    ScheduleRecordKind.EXECUTION_JOB: _plan_execution,
    ScheduleRecordKind.MANAGED_SERVICE: _plan_execution,
}


def _in_plan_order(records: SectionRecords) -> list[ScheduleTransferRecordV2]:
    parsed = [ScheduleTransferRecordV2.model_validate(raw) for raw in records]
    return sorted(parsed, key=lambda record: _PLAN_ORDER.index(record.kind))


def _missing_schedule_setup(schedule, known, source):
    from custom_agents.schedule_management import is_system_managed_schedule

    required = [ScheduleRecordKind.SCHEDULED_TARGET]
    if not is_system_managed_schedule(schedule):
        required.append(ScheduleRecordKind.WORKER_BINDING)
    return [
        record_key(kind, schedule.schedule_id)
        for kind in required
        if record_key(kind, schedule.schedule_id) in source
        and record_key(kind, schedule.schedule_id) not in known.accepted
    ]


def _require_related_records(records, planned, known, context):
    """A selected schedule must retain its saved worker and delivery setup."""
    source = context.scratch.get("schedules.source_keys", set())
    blocked = {}
    for decision in planned:
        if (
            not decision.key.startswith("schedule/")
            or decision.outcome is RecordOutcome.REFUSED
        ):
            continue
        schedule_id = decision.key.split("/", 1)[1]
        missing = _missing_schedule_setup(known.schedules[schedule_id], known, source)
        if missing:
            blocked[schedule_id] = (
                "its saved setup is not selected, present, or accepted: "
                + ", ".join(missing)
            )
    if not blocked:
        return planned
    parsed = {record.key: record for record in _in_plan_order(records)}
    checked = []
    for decision in planned:
        record = parsed[decision.key]
        if (
            record.kind
            in {
                ScheduleRecordKind.SCHEDULE,
                ScheduleRecordKind.SCHEDULED_TARGET,
                ScheduleRecordKind.WORKER_BINDING,
            }
            and record.record_id in blocked
        ):
            reason = (
                blocked[record.record_id]
                if record.kind is ScheduleRecordKind.SCHEDULE
                else f"its schedule {record.record_id} is refused"
            )
            checked.append(
                PlannedRecord("schedules", decision.key, RecordOutcome.REFUSED, reason)
            )
        else:
            checked.append(decision)
    return checked


class SchedulesSection:
    name: ClassVar[str] = "schedules"
    # 3: two new system schedule kinds (knowledge curation) in ScheduledMessage.
    # 4: the payload Task and TaskSteering can carry the chat message a person
    #    replied to (Task.replied_message).
    # 5: ScheduledMessage carries its store-owned timing_revision.
    schema_version: ClassVar[int] = 5
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = ScheduleTransferRecordV2

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        exported = _Export(source)
        exported.targets(exported.schedules())
        exported.sessions()
        exported.heartbeat()
        exported.workers()
        exported.execution()
        return SectionExport(records=exported.records)

    def plan(
        self, records: SectionRecords, context: ImportContext
    ) -> list[PlannedRecord]:
        known = _Known.build(context)
        planned = []
        for record in _in_plan_order(records):
            try:
                outcome, reason = _PLANNERS[record.kind](record, known)
            except ValueError as exc:  # this version's model refuses the document
                outcome, reason = _refused(f"this version refuses it: {exc}")
            if outcome is not RecordOutcome.REFUSED:
                known.accepted.add(record.key)
            planned.append(PlannedRecord(self.name, record.key, outcome, reason))
        return _require_related_records(records, planned, known, context)

    def apply(
        self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext
    ) -> None:
        accepted = {
            record.key for record in plan if record.outcome is not RecordOutcome.REFUSED
        }
        writer = ScheduleWriter.open(context)
        for record in _in_plan_order(records):
            if record.key in accepted:
                writer.write(record)
        logger.info(
            "Team schedules imported. event=team_transfer_schedules_imported records=%s refused=%s",
            len(accepted),
            len(plan) - len(accepted),
        )


__all__ = ["SchedulesSection"]


# V4 added optional Task.replied_message and V5 adds timing_revision, whose
# initial value is zero. Already-supported V3 payloads carry neither field;
# their records are otherwise unchanged and the current model fills both.
SECTION_UPGRADES[(SchedulesSection.name, 3)] = lambda records: records

# V4 exports have no timing_revision. ScheduledMessage reads their timing as
# revision zero; the first explicit timing edit increments it. Keep the V3
# conversion above for the already-supported frozen transfer fixture too.
SECTION_UPGRADES[(SchedulesSection.name, 4)] = lambda records: records
