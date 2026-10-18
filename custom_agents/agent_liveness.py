"""Whether an agent that holds saved work is gone: one rule for every decision.

Five places ask this question: a starting agent taking over saved work of its
type (``runtime_recovery``, for tasks and for deferred messages), a scheduled
fire deciding whether another worker's unfinished run is still live
(``scheduled_runtime``), the team lead starting a replacement for a dead owner
(``orphaned_work_takeover``) and the stuck-task notices (``stuck_tasks``); the
lead's pruning of status rows asks it too. All of them call ``owner_liveness``
or ``gone_owner_ids`` here, so they cannot disagree.

The rule, strongest evidence first:

1. Start record. An agent started inside another process (a pool, scheduled,
   replacement or manual worker) has a launch record. A record that ended
   (``retired``/``failed``) or whose process is provably gone means gone; any
   other record means the worker may still run, however old its heartbeat.
2. Process. An agent that is its own process (a startup agent or the team lead)
   reports that process in ``AgentStatus.process_identity``. A process that is
   provably gone (``runtime_hosting.process_run``: another run of the runtime,
   or the same process number with another start time or none at all) means
   gone. A live process means the agent still runs, even while its heartbeat
   is old or it announced its exit. A process that cannot be inspected from
   here (another host, an unproven run, no ``/proc``) proves nothing either
   way, so rule 3 decides, and ``event=owner_liveness_unknown`` is logged.
3. Heartbeat, for agents that report no process and for processes that cannot
   be inspected: an announced exit or no heartbeat at all means gone; otherwise
   silence longer than
   ``agent.schedule_maintenance.stuck_tasks.owner_gone_after_seconds``. While
   the task monitor catches up after a pause (``liveness_observation``) an old
   heartbeat proves nothing.

Why: until 2026-10-17 owners without a start record were judged by heartbeat
age alone, with three different limits (30 seconds when an agent started, 600
seconds for the lead's replacement pass, a time-since rule for notices). A live
startup agent whose heartbeat was 30 seconds old, after host sleep or a stalled
event loop, or whose status row the exiting team lead had deleted, lost its work
to any newly started agent of its type, kept running it, and saved it again, so
the task ran twice. Its process is the proof; its heartbeat was only a guess.

A process says nothing about other agents inside it, so workers started inside
a process publish no process (their start record decides), and a status that
names the deciding process itself is judged by its heartbeat.

An unknown process used to keep its work for good. On 2026-10-17 review found
the gap: with an unproven run and a recreated container, a crashed startup
agent's identity names the old host, so nothing replaced it, no notice went
out, and schedule coalescing folded every new fire into its dead run. Falling
back to the heartbeat is safe because a previous owner that is in fact still
running cannot save the work again: its task saves are refused once its task
row moved, and a deferred message leaves its deferred row only in the same
transaction as the task save that records its handling (``RuntimeCustodyLost``,
``custom_agents.custody_loss``, ``deferred_message_runtime.DeferredHandoff``).

Assumption: every agent process of a team is a child of one supervisor run on
one host (``runtime_hosting.process_run``; the run lock refuses a second
runtime for the instance folder). A process from another run counts as gone
whatever host it names, so agent processes of another runtime sharing the
database would be judged gone at once; that deployment is not supported.

Each observer logs an owner's decision, per asking purpose and scope, when it
first makes it and whenever it changes (``event=owner_liveness_decided``), not
on every pass. The record behind that is bounded: each pass or call keeps only
the owners it decided about within its own scope (``retain_decisions``), so
owners whose saved work or status row is gone drop out. The lead's passes look
at the whole team (no scope); schedule coalescing asks per schedule, so one
schedule's fire does not forget another schedule's owners.

It depends only on stores and settings, so every runtime module may import it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import partial
from typing import Any, Callable, Iterable, Optional

from custom_agents.agent_state_store import AgentStateStore
from custom_agents.liveness_observation import heartbeat_checks_deferred
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState, AgentLaunchV1
from runtime_hosting.process_run import (
    RecordedProcessState,
    current_process_identity,
    current_run_proven,
    recorded_process_state,
)
from runtime_settings.stuck_tasks import StuckTaskSettings
from storage.agent_launch_repository import ACTIVE_LAUNCH_STATES, AgentLaunchRepository

_ENDED_LAUNCH_STATES = frozenset({AgentLaunchState.RETIRED, AgentLaunchState.FAILED})
# A sender's own time may run this far ahead of the lead's clock before it is logged.
STATUS_CLOCK_TOLERANCE = timedelta(seconds=5)
# How often the team lead removes status rows of agents that are gone.
AGENT_ROW_PRUNE_SECONDS = 60.0
# What LocalAgentTeamRuntime.inspect_launch answers for a start whose process is gone.
_LAUNCH_ABSENT = "absent"


class LivenessPurpose(StrEnum):
    """Which decision asked; logged with it."""

    STARTUP_TAKEOVER = "startup_takeover"
    DEFERRED_TAKEOVER = "deferred_takeover"
    SCHEDULE_COALESCING = "schedule_coalescing"
    REPLACEMENT_PASS = "replacement_pass"
    STUCK_TASK_NOTICE = "stuck_task_notice"
    STATUS_ROW_PRUNE = "status_row_prune"


class OwnerEvidence(StrEnum):
    """What decided whether an owner is gone; logged with the decision."""

    LAUNCH_ENDED = "launch_ended"
    LAUNCH_PROCESS_ABSENT = "launch_process_absent"
    LAUNCH_ACTIVE = "launch_active"
    PROCESS_GONE = "process_gone"
    PROCESS_ALIVE = "process_alive"
    PROCESS_UNKNOWN = "process_unknown"
    EXITING = "exiting"
    NO_HEARTBEAT = "no_heartbeat"
    HEARTBEAT_DEFERRED = "heartbeat_deferred"
    HEARTBEAT_STALE = "heartbeat_stale"
    HEARTBEAT_FRESH = "heartbeat_fresh"


_GONE_EVIDENCE = frozenset(
    {
        OwnerEvidence.LAUNCH_ENDED,
        OwnerEvidence.LAUNCH_PROCESS_ABSENT,
        OwnerEvidence.PROCESS_GONE,
        OwnerEvidence.EXITING,
        OwnerEvidence.NO_HEARTBEAT,
        OwnerEvidence.HEARTBEAT_STALE,
    }
)
_LAUNCH_EVIDENCE = frozenset(
    {OwnerEvidence.LAUNCH_ENDED, OwnerEvidence.LAUNCH_PROCESS_ABSENT, OwnerEvidence.LAUNCH_ACTIVE}
)


@dataclass(frozen=True)
class OwnerLiveness:
    """One decision about one owner, with the records it was made from."""

    owner_agent_id: str
    evidence: OwnerEvidence
    status: Optional[AgentStatus]
    launch: Optional[AgentLaunchV1]
    # The process the owner reported, when it could not be inspected (rule 2).
    unchecked_process: Optional[str] = None

    @property
    def gone(self) -> bool:
        return self.evidence in _GONE_EVIDENCE

    @property
    def last_seen(self) -> datetime | None:
        return None if self.status is None else parse_utc(self.status.last_seen_at)

    def gone_since(self, fallback: datetime | None) -> datetime | None:
        """When the owner stopped: its start record's last change, else its last heartbeat."""
        if self.evidence in _LAUNCH_EVIDENCE and self.launch is not None:
            return parse_utc(self.launch.updated_at_utc) or fallback
        return self.last_seen or fallback


def parse_utc(value: Any) -> datetime | None:
    """A stored time as an aware UTC datetime, or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def owner_gone_after(agent: Any) -> timedelta:
    """The one heartbeat threshold (module docstring, rule 3)."""
    settings = StuckTaskSettings.from_schedule_maintenance(getattr(agent, "schedule_maintenance", None))
    return timedelta(seconds=settings.owner_gone_after_seconds)


def own_process_identity(agent: Any) -> str | None:
    """The process an agent publishes in its status, or None for a worker started inside another process."""
    if getattr(agent, "_worker_launch", None) is not None:
        return None
    return current_process_identity().text()


def _state_store(agent: Any) -> AgentStateStore:
    return getattr(agent, "_agent_state_store", None) or AgentStateStore(
        db_path=agent.task_manager._db_path,
        logger=agent._logger,
        storage_settings=agent.task_manager.storage_settings,
    )


def lookup_owner_status(agent: Any, owner_agent_id: str) -> Optional[AgentStatus]:
    """The owner's last stored status (heartbeat), or None."""
    if not owner_agent_id:
        return None
    return _state_store(agent).get_by_id(owner_agent_id)


def latest_launch(agent: Any, owner_agent_id: str) -> AgentLaunchV1 | None:
    records = [
        record
        for record in AgentLaunchRepository(agent.task_manager.storage_settings).list(agent.team_id)
        if record.agent_id == owner_agent_id
    ]
    return max(records, key=lambda record: record.created_at_utc) if records else None


def _launch_evidence(agent: Any, launch: AgentLaunchV1) -> OwnerEvidence:
    if launch.state in _ENDED_LAUNCH_STATES:
        return OwnerEvidence.LAUNCH_ENDED
    runtime = getattr(agent, "_local_team_runtime", None)
    if launch.state in ACTIVE_LAUNCH_STATES and runtime is not None:
        if str(runtime.inspect_launch(launch)) == _LAUNCH_ABSENT:
            return OwnerEvidence.LAUNCH_PROCESS_ABSENT
    return OwnerEvidence.LAUNCH_ACTIVE


def _process_evidence(status: Optional[AgentStatus]) -> OwnerEvidence | None:
    """The owner's own process, or None when its status names no other process."""
    identity = None if status is None else status.process_identity
    current = current_process_identity()
    if not identity or identity == current.text():
        return None
    state = recorded_process_state(identity, current=current, run_proven=current_run_proven())
    if state is RecordedProcessState.GONE:
        return OwnerEvidence.PROCESS_GONE
    if state is RecordedProcessState.ALIVE:
        return OwnerEvidence.PROCESS_ALIVE
    return OwnerEvidence.PROCESS_UNKNOWN


def _heartbeat_evidence(agent: Any, status: Optional[AgentStatus], now: datetime) -> OwnerEvidence:
    if status is not None and status.is_exiting:
        return OwnerEvidence.EXITING
    if heartbeat_checks_deferred(agent):
        return OwnerEvidence.HEARTBEAT_DEFERRED
    last_seen = None if status is None else parse_utc(status.last_seen_at)
    if last_seen is None:
        return OwnerEvidence.NO_HEARTBEAT
    if now - last_seen >= owner_gone_after(agent):
        return OwnerEvidence.HEARTBEAT_STALE
    return OwnerEvidence.HEARTBEAT_FRESH


def _decide(agent: Any, owner_agent_id: str, status: Optional[AgentStatus], now: datetime) -> OwnerLiveness:
    launch = latest_launch(agent, owner_agent_id)
    if launch is not None:
        return OwnerLiveness(owner_agent_id, _launch_evidence(agent, launch), status, launch)
    process = _process_evidence(status)
    if process in (OwnerEvidence.PROCESS_GONE, OwnerEvidence.PROCESS_ALIVE):
        return OwnerLiveness(owner_agent_id, process, status, None)
    unchecked = status.process_identity if process is OwnerEvidence.PROCESS_UNKNOWN and status else None
    return OwnerLiveness(owner_agent_id, _heartbeat_evidence(agent, status, now), status, None, unchecked)


def _observer_memory(agent: Any, name: str, empty: Callable[[], Any]) -> Any:
    """Per-observer state kept on the agent (``_liveness_decisions``, ``_status_clock_ahead``)."""
    memory = getattr(agent, name, None)
    if memory is None:
        memory = empty()
        setattr(agent, name, memory)
    return memory


# Decision records: (purpose, scope, owner) -> (evidence, unchecked process).
_DecisionRecords = dict[tuple[LivenessPurpose, str, str], tuple[Any, ...]]


def _note_decision(agent: Any, decision: OwnerLiveness, purpose: LivenessPurpose, scope: str) -> None:
    """Log an owner's decision the first time and whenever it changes, not on every pass."""
    memory: _DecisionRecords = _observer_memory(agent, "_liveness_decisions", dict)
    key = (purpose, scope, decision.owner_agent_id)
    current = (decision.evidence, decision.unchecked_process)
    previous = memory.get(key)
    if previous == current:
        return
    memory[key] = current
    last_seen = None if decision.status is None else decision.status.last_seen_at
    if decision.unchecked_process and (previous is None or previous[1] != decision.unchecked_process):
        agent._logger.warning(
            "Could not check whether an agent's process still runs; judging it by its heartbeat. "
            "event=owner_liveness_unknown purpose=%s owner_agent_id=%s process_identity=%s "
            "gone=%s evidence=%s last_seen_at=%s",
            purpose.value,
            decision.owner_agent_id,
            decision.unchecked_process,
            decision.gone,
            decision.evidence.value,
            last_seen,
        )
    agent._logger.info(
        "Decided whether an agent holding saved work is gone. event=owner_liveness_decided "
        "purpose=%s owner_agent_id=%s gone=%s evidence=%s previous_evidence=%s last_seen_at=%s",
        purpose.value,
        decision.owner_agent_id,
        decision.gone,
        decision.evidence.value,
        None if previous is None else previous[0].value,
        last_seen,
    )


def retain_decisions(
    agent: Any, purpose: LivenessPurpose, owner_agent_ids: Iterable[str], *, scope: str = ""
) -> None:
    """Forget decisions of this purpose and scope about owners the latest pass or call did not see."""
    memory: _DecisionRecords = _observer_memory(agent, "_liveness_decisions", dict)
    seen = set(owner_agent_ids)
    for key in [key for key in memory if key[0] is purpose and key[1] == scope and key[2] not in seen]:
        del memory[key]


def owner_liveness(
    agent: Any,
    owner_agent_id: str,
    *,
    purpose: LivenessPurpose,
    scope: str = "",
    now: datetime | None = None,
    status: Optional[AgentStatus] = None,
) -> OwnerLiveness:
    """Decide whether one owner is gone (module docstring); ``status`` skips a second read.

    ``scope`` names what the asking call covers (a schedule id for coalescing,
    nothing for the lead's whole-team passes); it only groups decision records.
    """
    moment = now or datetime.now(timezone.utc)
    known_status = status if status is not None else lookup_owner_status(agent, owner_agent_id)
    decision = _decide(agent, owner_agent_id, known_status, moment)
    _note_decision(agent, decision, purpose, scope)
    return decision


async def gone_owner_ids(
    agent: Any, owner_agent_ids: Iterable[str], *, purpose: LivenessPurpose, scope: str = ""
) -> list[str]:
    """The owners among these that are gone, never this agent; ``scope`` as in ``owner_liveness``."""
    moment = datetime.now(timezone.utc)
    decided = [owner for owner in dict.fromkeys(owner_agent_ids) if owner and owner != agent.id]
    gone = [
        owner
        for owner in decided
        if owner_liveness(agent, owner, purpose=purpose, scope=scope, now=moment).gone
    ]
    retain_decisions(agent, purpose, decided, scope=scope)
    return gone


def prune_departed_agent_rows(agent: Any, now: datetime | None = None) -> int:
    """Delete status rows of agents that are gone and unseen for ``owner_gone_after_seconds``.

    The team lead runs this from its scheduler pass instead of deleting every
    row of the team when it exits, which made live startup agents look gone
    after a manager-only restart. A row is deleted only if it still has the
    heartbeat that was read, so an agent that reports in meanwhile keeps it.
    Nothing is deleted while the task monitor catches up after a pause.
    """
    if heartbeat_checks_deferred(agent):
        return 0
    moment = now or datetime.now(timezone.utc)
    gone_after = owner_gone_after(agent)
    store = _state_store(agent)
    removed = 0
    decided: list[str] = []
    for status in store.query({"team_id": agent.team_id}):
        agent_id = status.self_id
        if not agent_id or agent_id == agent.id:
            continue
        last_seen = parse_utc(status.last_seen_at)
        if last_seen is not None and moment - last_seen < gone_after:
            continue
        decision = owner_liveness(
            agent, agent_id, purpose=LivenessPurpose.STATUS_ROW_PRUNE, now=moment, status=status
        )
        decided.append(agent_id)
        if not decision.gone or not store.remove_unseen(agent_id, status.last_seen_at):
            continue
        removed += 1
        agent._logger.info(
            "Removed the status row of an agent that is gone. event=agent_status_row_pruned "
            "agent_id=%s named_agent_type=%s evidence=%s last_seen_at=%s",
            agent_id,
            status.named_agent_type,
            decision.evidence.value,
            status.last_seen_at,
        )
    retain_decisions(agent, LivenessPurpose.STATUS_ROW_PRUNE, decided)
    return removed


async def prune_departed_agent_rows_when_due(agent: Any, *, clock: float | None = None) -> int:
    """The team lead's pass: prune at most every ``AGENT_ROW_PRUNE_SECONDS`` (storage lane)."""
    if not getattr(agent, "is_team_lead", False) or not getattr(agent, "team_id", None):
        return 0
    moment = time.monotonic() if clock is None else clock
    if moment < getattr(agent, "_agent_row_prune_after", 0.0):
        return 0
    agent._agent_row_prune_after = moment + AGENT_ROW_PRUNE_SECONDS
    removed: int = await agent.task_manager.operations_runtime.run(partial(prune_departed_agent_rows, agent))
    return removed


def received_status_time(agent: Any, status: AgentStatus, *, now: datetime | None = None) -> str:
    """The time the lead stores for a received status: the sender's own, never later than now.

    Presence is replayed when the lead subscribes, so the arrival time would
    make an agent that just crashed look alive. The sender's time is used
    instead, but a sender whose clock runs ahead must not stay fresh (and
    unpruned) on that clock, so the earlier of the two is kept. Being ahead
    by more than ``STATUS_CLOCK_TOLERANCE`` is logged when it starts and ends.
    """
    arrived = now or datetime.now(timezone.utc)
    agent_id = status.self_id or ""
    sent = parse_utc(status.last_seen_at)
    if sent is None:
        agent._logger.warning(
            "An agent status arrived without the time it was sent; storing the time it "
            "arrived. event=agent_status_without_sent_time agent_id=%s",
            agent_id,
        )
        return arrived.isoformat()
    ahead: set[str] = _observer_memory(agent, "_status_clock_ahead", set)
    if sent - arrived > STATUS_CLOCK_TOLERANCE:
        if agent_id not in ahead:
            ahead.add(agent_id)
            agent._logger.warning(
                "An agent's clock runs ahead of the team lead's; storing the arrival time. "
                "event=agent_status_clock_ahead agent_id=%s sent_at=%s arrived_at=%s ahead_seconds=%.1f",
                agent_id,
                sent.isoformat(),
                arrived.isoformat(),
                (sent - arrived).total_seconds(),
            )
    elif agent_id in ahead:
        ahead.discard(agent_id)
        agent._logger.info(
            "An agent's clock no longer runs ahead of the team lead's. "
            "event=agent_status_clock_ahead_cleared agent_id=%s",
            agent_id,
        )
    return min(sent, arrived).isoformat()
