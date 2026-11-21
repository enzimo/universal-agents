"""Hand a dead agent's unfinished work to a new agent of the same type.

Why: an agent's saved work (``active_task_runtime``) is adopted only by an agent
of the same team and type when that agent starts (``runtime_recovery``). On-demand
workers are not restarted after a crash or a replaced container, so their tasks
waited until some unrelated start of that type happened, often never (found
2026-10-04). On each scheduler pass the team lead now starts one replacement
worker per dead owner holding unfinished work; the replacement's own startup
takeover adopts the work through the existing transactional custody transfer
(``commit_task_runtime_recovery``), so two agents can never both adopt it.

When an owner counts as gone is decided by the one rule in
``custom_agents.agent_liveness`` (start record, then the owner's own process,
then its heartbeat after ``owner_gone_after_seconds``), the same rule the
replacement's startup takeover applies, so the lead never starts a replacement
that would then refuse the work, and a live startup agent keeps its work.

Retries: a replacement that fails before it is ready, or retires while the work
is still held, is followed by another one, after ``TAKEOVER_RETRY_DELAYS_SECONDS``
counted from the end of the previous one, up to ``MAX_TAKEOVER_ATTEMPTS``; then
the lead gives up and logs it once. Each attempt has its own start id,
``takeover:<owner>:<attempt>``. Until the review of 2026-11-20 the id was fixed,
so the start record of a replacement that crashed (for example on a work stream
at its consumer limit) was found again on every later pass and nothing was
started until that record was pruned, about 30 days later. The attempt number
comes from the highest-numbered attempt record present.

A give-up is not lasting. The lead starts no further replacement for that work
until the attempt records are pruned (``agent.schedule_workers.retention_days``
after the last attempt ended, 30 days by default), and then begins a new series
of five. An agent of the same type that starts before then for another reason
still adopts the work at its start.

One replacement serves every dead owner of its type: its startup takeover adopts
all their saved work, and starts for one type and person join (``reserve``), as
dead pool workers of one type do (they share the pool's owner). So the series
runs per type and person: one dead owner leads it (the one with the highest
attempt, else the first), and the others wait for its replacement. Run per
owner, four dead workers of one type started a replacement every minute.

A give-up covers the owners with no proof that they were alive after the
series' last attempt ended; each is logged once as given up with its own task
ids. The proof is the latest of the owner's last save of its work and its last
heartbeat (``AgentStatus.last_seen_at``): saves happen only when a task
changes, so a worker waiting on an approval may save nothing for days while it
heartbeats every 20 seconds. A start record's end is not proof, since the lead
ends the records of an earlier run after the fact. An owner proven alive later,
whose own series has not given up, leads a new series, and the given-up owners
wait for that replacement, which adopts their work too. Saves and heartbeats are
stored, so this holds across a lead restart. Without it, one given-up series
stopped replacements for every later dead worker of the type and person until
the records were pruned. An owner whose own series began after the give-up
keeps leading it even once the lead prunes its status row (about ten minutes
after it died, which removes its heartbeat): its attempt records are stored. The
lead also remembers the latest proof of life it read for each dead owner still
holding work, so one whose first tries were refused (which leaves no record)
keeps it after the prune too; like the refusal count, that memory is lost when
the lead restarts. A worker whose status row was pruned before the lead read it,
and that saved nothing after the series ended, is judged by its save alone.

- A refusal that leaves no start record (the type was removed or is denied, the
  team's worker limit or the hourly start limit is reached) is paced with the
  same delays and then tried hourly, never given up: a limit frees again, and a
  refusal starts no process. The count is kept in memory, is reset by a start,
  and a restarted lead starts it again.
- A start can still join another start of the same type and person that is
  starting (a pool start, say); that uses no attempt, and the next pass looks
  again.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Mapping, Sequence

from custom_messages.contracts.runtime_snapshot_v3 import decode_runtime_snapshot
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchV1
from custom_agents.agent_liveness import (
    LivenessPurpose,
    latest_launch,
    owner_liveness,
    parse_utc,
    retain_decisions,
)
from storage.agent_launch_repository import ACTIVE_LAUNCH_STATES, AgentLaunchRepository
from custom_agents.contract_drain_state import is_contract_draining

logger = logging.getLogger(__name__)

# How often the lead looks for dead owners holding unfinished work.
TAKEOVER_CHECK_SECONDS = 60.0
# How long the lead waits after replacement attempt n ended before attempt n+1,
# for n = 1 to 4; after the fifth it gives up. Refusals use the same delays, then
# the last one for good. Fixed: a work stream at its consumer limit or a
# provider's brief refusal clears within that span, a broken definition does
# not, and the 60-second check already paces passes.
TAKEOVER_RETRY_DELAYS_SECONDS = (60.0, 300.0, 900.0, 3600.0)
MAX_TAKEOVER_ATTEMPTS = len(TAKEOVER_RETRY_DELAYS_SECONDS) + 1
_TERMINAL_STATE_VALUES = frozenset({"completed", "failed", "cancelled", "abandoned"})

# The logged-once key of a dead owner whose person is not known.
SKIPPED_NO_PERSON = "skipped:no_person"

# What one pass does for one dead owner.
STEP_START = "start"
STEP_WAIT = "wait"
STEP_GIVE_UP = "give_up"


@dataclass(frozen=True)
class TakeoverStep:
    """The next step for one dead owner, from its replacement attempts' start records."""

    action: str
    attempt: int = 0
    # The newest attempt's record, if any (its reason is logged with the next one).
    previous: AgentLaunchV1 | None = None


@dataclass
class _OwnerMemory:
    """What the lead remembers per dead owner in this process (refusals leave no record)."""

    refusals: int = 0
    retry_at: float = 0.0
    logged: set[str] = field(default_factory=set)
    # The latest proof of life read for it (a heartbeat lasts only until the
    # lead prunes its status row, about ten minutes after it died).
    alive_at: datetime | None = None


def takeover_request_id(owner_agent_id: str, attempt: int) -> str:
    return f"takeover:{owner_agent_id}:{attempt}"


def takeover_step(attempts: Mapping[int, AgentLaunchV1], now: datetime) -> TakeoverStep:
    """Start the first attempt, wait while one runs or its delay lasts, start the next, or give up."""

    if not attempts:
        return TakeoverStep(STEP_START, 1)
    latest = max(attempts)
    record = attempts[latest]
    if record.state in ACTIVE_LAUNCH_STATES:
        return TakeoverStep(STEP_WAIT, latest, record)
    if latest >= MAX_TAKEOVER_ATTEMPTS:
        return TakeoverStep(STEP_GIVE_UP, latest, record)
    ended = parse_utc(record.updated_at_utc) or now
    if now < ended + timedelta(seconds=TAKEOVER_RETRY_DELAYS_SECONDS[latest - 1]):
        return TakeoverStep(STEP_WAIT, latest, record)
    return TakeoverStep(STEP_START, latest + 1, record)


def _unfinished_tasks(row: Any) -> list[Any]:
    """The owner's own unfinished tasks (not subtasks it delegated out); [] if unreadable."""
    try:
        snapshot = decode_runtime_snapshot(row.runtime_state_json)
    except ValueError:
        return []
    return [
        record
        for record in snapshot.tasks
        if record.state.value not in _TERMINAL_STATE_VALUES
        and record.task_id not in snapshot.subtask_to_parent_map
    ]


def _person_for(
    agent: Any, owner_agent_id: str, unfinished: list[Any], launches: Sequence[AgentLaunchV1]
) -> str | None:
    """Whom the replacement's start is recorded for: the dead start's owner, else a task's person."""
    from custom_agents.stuck_tasks import StuckTasks, stuck_task_settings

    launch = latest_launch(agent, owner_agent_id, launches)
    if launch is not None and launch.owner_canonical_user_id:
        return str(launch.owner_canonical_user_id)
    people = StuckTasks(agent, stuck_task_settings(agent))
    return next((person for person in (people.person_of(r.meta) for r in unfinished) if person), None)


def _attempt_records(
    agent: Any, owner_agent_id: str, person: str | None, by_id: Mapping[str, AgentLaunchV1]
) -> dict[int, AgentLaunchV1]:
    """The owner's replacement attempts that still have a start record, by attempt number."""
    from custom_agents.agent_instantiation_service import launch_id_for

    if not person:
        return {}
    found = {}
    for attempt in range(1, MAX_TAKEOVER_ATTEMPTS + 1):
        record = by_id.get(launch_id_for(agent.team_id, person, takeover_request_id(owner_agent_id, attempt)))
        if record is not None:
            found[attempt] = record
    return found


def _orphaned_owners(agent: Any, now: datetime) -> list[dict[str, Any]]:
    """Dead owners of the team that hold unfinished work, with their attempts (storage lane)."""

    launches = AgentLaunchRepository(agent.task_manager.storage_settings).list(agent.team_id)
    by_id = {record.launch_id: record for record in launches}
    found = []
    decided: list[str] = []
    for row in agent.task_manager.runtime_state_store.peek_task_runtime_by_team(agent.team_id):
        agent_type = row.owner_named_agent_type
        if row.owner_agent_id == agent.id or not agent_type or agent_type == TEAM_ARCHITECT_AGENT_TYPE:
            continue
        unfinished = _unfinished_tasks(row)
        if not unfinished:
            continue
        decided.append(row.owner_agent_id)
        liveness = owner_liveness(
            agent, row.owner_agent_id, purpose=LivenessPurpose.REPLACEMENT_PASS, now=now, launches=launches
        )
        if not liveness.gone:
            continue
        person = _person_for(agent, row.owner_agent_id, unfinished, launches)
        found.append(
            {
                "owner_agent_id": row.owner_agent_id,
                "agent_type": agent_type,
                "person": person,
                "tasks": len(unfinished),
                "task_ids": [record.task_id for record in unfinished],
                # Its latest proof of life: last save or last heartbeat (what a give-up covers).
                "alive_at": _latest(parse_utc(row.updated_at), liveness.last_seen),
                "attempts": _attempt_records(agent, row.owner_agent_id, person, by_id),
            }
        )
    # Owners with no unfinished work left are not decided again: forget them.
    retain_decisions(agent, LivenessPurpose.REPLACEMENT_PASS, decided)
    return found


def _launch_takeover(agent: Any, owner: dict[str, Any], attempt: int) -> Any:
    from custom_agents.agent_instantiation_service import AgentInstantiationService

    service = AgentInstantiationService(agent)
    return service.launch(
        agent_type=owner["agent_type"],
        owner_id=owner["person"],
        # One start per attempt; a repeated pass finds the same start.
        request_id=takeover_request_id(owner["owner_agent_id"], attempt),
        source="delegation",
        idle_ttl_seconds=service.default_idle_ttl(owner["agent_type"]),
        join_pending=True,
    )


def _owner_memory(agent: Any, owners: list[dict[str, Any]]) -> dict[str, _OwnerMemory]:
    """This process's memory per dead owner, kept only for owners still holding work."""
    memory: dict[str, _OwnerMemory] | None = getattr(agent, "_orphaned_work_takeovers", None)
    if memory is None:
        memory = {}
        agent._orphaned_work_takeovers = memory
    current = {owner["owner_agent_id"] for owner in owners}
    for gone in [owner_id for owner_id in memory if owner_id not in current]:
        del memory[gone]
    return memory


def _log_once(memory: _OwnerMemory, key: str, log: Any, message: str, *args: Any) -> None:
    if key not in memory.logged:
        memory.logged.add(key)
        log(message, *args)


def _give_up(agent: Any, owner: dict[str, Any], memory: _OwnerMemory, series: dict[str, Any], now: datetime) -> None:
    """One owner the given-up ``series`` covers; logged once per owner and series."""
    step = takeover_step(series["attempts"], now)
    _log_once(
        memory,
        f"{STEP_GIVE_UP}:{series['owner_agent_id']}",
        agent._logger.error,
        "Gave up starting replacements for a dead agent's unfinished work; the lead starts no further "
        "replacement for it until the attempt records are pruned (retention_days). "
        "event=orphaned_work_takeover_gave_up owner_agent_id=%s agent_type=%s task_ids=%s "
        "series_owner_agent_id=%s attempts=%s last_reason=%s",
        owner["owner_agent_id"],
        owner["agent_type"],
        owner["task_ids"],
        series["owner_agent_id"],
        step.attempt,
        step.previous.reason if step.previous is not None else None,
    )


def _series_end(owner: dict[str, Any], now: datetime) -> datetime | None:
    """When this owner's series gave up (its last attempt's end), or None."""
    step = takeover_step(owner["attempts"], now)
    if step.action != STEP_GIVE_UP or step.previous is None:
        return None
    return parse_utc(step.previous.updated_at_utc)


def _plan_group(group: list[dict[str, Any]], now: datetime) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The owner that runs the group's series, and the latest series that gave up (module docstring).

    No runner means the given-up series covers every owner of the group.
    """
    ended = [(end, owner) for owner in group if (end := _series_end(owner, now)) is not None]
    if not ended:
        return max(group, key=_highest_attempt), None
    gave_up_at, series = max(ended, key=lambda item: item[0])
    given_up = {id(owner) for _end, owner in ended}
    later = [
        owner
        for owner in group
        if id(owner) not in given_up and _alive_after(owner, gave_up_at)
    ]
    return (max(later, key=_highest_attempt) if later else None), series


def _alive_after(owner: dict[str, Any], moment: datetime) -> bool:
    """Proof of life after ``moment``, or a series of its own that began after it.

    The second keeps an owner leading its own series after the lead prunes its
    status row (about ten minutes after it died), which removes its heartbeat.
    """
    if owner["alive_at"] is not None and owner["alive_at"] > moment:
        return True
    return any((parse_utc(record.created_at_utc) or moment) > moment for record in owner["attempts"].values())


def _highest_attempt(owner: dict[str, Any]) -> int:
    return max(owner["attempts"], default=0)


def _latest(*moments: datetime | None) -> datetime | None:
    return max((moment for moment in moments if moment is not None), default=None)


def _refused(agent: Any, owner: dict[str, Any], memory: _OwnerMemory, reason: str | None, clock: float) -> None:
    """A refusal that left no start record: paced like an attempt, then hourly; never given up."""
    memory.refusals += 1
    delay = TAKEOVER_RETRY_DELAYS_SECONDS[min(memory.refusals, len(TAKEOVER_RETRY_DELAYS_SECONDS)) - 1]
    memory.retry_at = clock + delay
    _log_once(
        memory,
        f"refused:{reason}",
        agent._logger.error,
        "A replacement for a dead agent's unfinished work was refused; trying again later. "
        "event=orphaned_work_takeover_refused owner_agent_id=%s agent_type=%s task_ids=%s reason=%s "
        "refusals=%s retry_in_seconds=%s",
        owner["owner_agent_id"],
        owner["agent_type"],
        owner["task_ids"],
        reason,
        memory.refusals,
        delay,
    )


async def _take_over_one(
    agent: Any, owner: dict[str, Any], memory: _OwnerMemory, now: datetime, clock: float
) -> bool:
    """One pass for one dead owner; True when it requested a new start."""
    from custom_agents.agent_instantiation_service import launch_id_for

    step = takeover_step(owner["attempts"], now)
    # The runner's own series never gave up (_plan_group); it starts or waits.
    if step.action != STEP_START or clock < memory.retry_at:
        return False
    result = await agent.task_manager.operations_runtime.run(partial(_launch_takeover, agent, owner, step.attempt))
    if result.launch_id is None:
        _refused(agent, owner, memory, result.reason, clock)
        return False
    expected = launch_id_for(agent.team_id, owner["person"], takeover_request_id(owner["owner_agent_id"], step.attempt))
    if result.launch_id != expected:
        _log_once(
            memory,
            f"joined:{result.launch_id}",
            agent._logger.info,
            "A dead agent's unfinished work waits for a replacement of its type that is already starting. "
            "event=orphaned_work_takeover_joined owner_agent_id=%s agent_type=%s task_ids=%s launch_id=%s",
            owner["owner_agent_id"],
            owner["agent_type"],
            owner["task_ids"],
            result.launch_id,
        )
        return False
    # This attempt has a start record now; refusals before it count no longer.
    memory.refusals, memory.retry_at = 0, 0.0
    log = agent._logger.warning if result.accepted or result.status == "waiting" else agent._logger.error
    log(
        "A dead agent holds unfinished work; a replacement of its type takes it over. "
        "event=orphaned_work_takeover owner_agent_id=%s agent_type=%s tasks=%s task_ids=%s attempt=%s "
        "status=%s launch_id=%s created=%s reason=%s previous_reason=%s",
        owner["owner_agent_id"],
        owner["agent_type"],
        owner["tasks"],
        owner["task_ids"],
        step.attempt,
        result.status,
        result.launch_id,
        result.created,
        result.reason,
        step.previous.reason if step.previous is not None else None,
    )
    return bool(result.created)


async def take_over_orphaned_work(agent: Any, *, now: datetime | None = None, clock: float | None = None) -> int:
    """Start replacements for dead owners holding unfinished work; returns how many were requested."""

    if not getattr(agent, "is_team_lead", False) or getattr(agent, "_local_team_runtime", None) is None:
        return 0
    if not getattr(agent, "same_role_runtime_takeover_enabled", True):
        return 0
    if is_contract_draining(agent):
        # This process runs another message contract: it starts no replacement (custom_agents.contract_drain),
        # and asking at every pass would only be refused and logged each time.
        return 0
    moment = time.monotonic() if clock is None else clock
    if moment < getattr(agent, "_orphaned_work_check_after", 0.0):
        return 0
    agent._orphaned_work_check_after = moment + TAKEOVER_CHECK_SECONDS
    when = now or datetime.now(timezone.utc)
    owners = await agent.task_manager.operations_runtime.run(partial(_orphaned_owners, agent, when))
    memory = _owner_memory(agent, owners)
    requested = 0
    for group in _groups(agent, owners, memory).values():
        requested += await _take_over_group(agent, group, memory, when, moment)
    return requested


async def _take_over_group(
    agent: Any, group: list[dict[str, Any]], memory: dict[str, _OwnerMemory], now: datetime, clock: float
) -> int:
    """One pass for the dead owners one replacement serves; returns how many starts it requested."""
    remembered_by_owner = {
        owner["owner_agent_id"]: memory.setdefault(owner["owner_agent_id"], _OwnerMemory()) for owner in group
    }
    for owner in group:
        # A pruned status row takes the heartbeat with it; keep what was read.
        remembered = remembered_by_owner[owner["owner_agent_id"]]
        remembered.alive_at = owner["alive_at"] = _latest(remembered.alive_at, owner["alive_at"])
    runner, given_up = _plan_group(group, now)
    requested = 0
    for owner in group:
        remembered = remembered_by_owner[owner["owner_agent_id"]]
        if runner is None and given_up is not None:
            _give_up(agent, owner, remembered, given_up, now)
        elif owner is runner:
            requested += await _take_over_one(agent, owner, remembered, now, clock)
        elif runner is not None:
            _wait_for(agent, owner, remembered, runner)
    return requested


def _groups(
    agent: Any, owners: list[dict[str, Any]], memory: dict[str, _OwnerMemory]
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Dead owners by the agent type and person one replacement serves; skips owners without a person.

    A skipped owner is logged once while it holds work, not on every pass.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for owner in owners:
        if not owner["person"]:
            _log_once(
                memory.setdefault(owner["owner_agent_id"], _OwnerMemory()),
                SKIPPED_NO_PERSON,
                agent._logger.warning,
                "A dead agent holds unfinished work but nobody it works for is known, so no "
                "replacement was started. event=orphaned_work_takeover_skipped owner_agent_id=%s agent_type=%s "
                "task_ids=%s",
                owner["owner_agent_id"],
                owner["agent_type"],
                owner["task_ids"],
            )
            continue
        groups.setdefault((owner["agent_type"], owner["person"]), []).append(owner)
    return groups


def _wait_for(agent: Any, owner: dict[str, Any], memory: _OwnerMemory, leader: dict[str, Any]) -> None:
    _log_once(
        memory,
        f"waiting:{leader['owner_agent_id']}",
        agent._logger.info,
        "A dead agent's unfinished work waits for the replacement started for another dead agent of its "
        "type. event=orphaned_work_takeover_waiting owner_agent_id=%s agent_type=%s task_ids=%s "
        "for_owner_agent_id=%s",
        owner["owner_agent_id"],
        owner["agent_type"],
        owner["task_ids"],
        leader["owner_agent_id"],
    )
