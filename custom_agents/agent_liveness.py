"""Whether an agent that holds saved work is gone: one rule for every decision.

Five places ask this question: a starting agent taking over saved work of its
type (``runtime_recovery``, for tasks and for deferred messages), a scheduled
fire deciding whether another worker's unfinished run is still live
(``scheduled_runtime``), the team lead starting a replacement for a dead owner
(``orphaned_work_takeover``) and the stuck-task notices (``stuck_tasks``). All of
them call ``owner_liveness`` or ``gone_owner_ids`` here, so they cannot disagree.

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
   is old or it announced its exit. When the process cannot be inspected the
   agent keeps its work, as with a start record.
3. Heartbeat, only for agents with neither: an announced exit or no heartbeat
   at all means gone; otherwise silence longer than
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

It depends only on stores and settings, so every runtime module may import it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import partial
from typing import Any, Iterable, Optional

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
# How often the team lead removes status rows of agents that are gone.
AGENT_ROW_PRUNE_SECONDS = 60.0
# What LocalAgentTeamRuntime.inspect_launch answers for a start whose process is gone.
_LAUNCH_ABSENT = "absent"


class OwnerEvidence(StrEnum):
    """What decided whether an owner is gone; logged with every decision."""

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


def owner_liveness(
    agent: Any,
    owner_agent_id: str,
    *,
    now: datetime | None = None,
    status: Optional[AgentStatus] = None,
) -> OwnerLiveness:
    """Decide whether one owner is gone (module docstring); ``status`` skips a second read."""
    moment = now or datetime.now(timezone.utc)
    launch = latest_launch(agent, owner_agent_id)
    known_status = status if status is not None else lookup_owner_status(agent, owner_agent_id)
    if launch is not None:
        evidence = _launch_evidence(agent, launch)
    else:
        evidence = _process_evidence(known_status) or _heartbeat_evidence(agent, known_status, moment)
    return OwnerLiveness(owner_agent_id, evidence, known_status, launch)


async def gone_owner_ids(agent: Any, owner_agent_ids: Iterable[str], *, purpose: str) -> list[str]:
    """The owners among these that are gone, never this agent; each decision is logged."""
    moment = datetime.now(timezone.utc)
    gone: list[str] = []
    for owner_agent_id in dict.fromkeys(owner_agent_ids):
        if not owner_agent_id or owner_agent_id == agent.id:
            continue
        decision = owner_liveness(agent, owner_agent_id, now=moment)
        agent._logger.info(
            "Decided whether an agent holding saved work is gone. event=owner_liveness_decided "
            "purpose=%s owner_agent_id=%s gone=%s evidence=%s last_seen_at=%s",
            purpose,
            owner_agent_id,
            decision.gone,
            decision.evidence.value,
            None if decision.status is None else decision.status.last_seen_at,
        )
        if decision.gone:
            gone.append(owner_agent_id)
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
    for status in store.query({"team_id": agent.team_id}):
        agent_id = status.self_id
        if not agent_id or agent_id == agent.id:
            continue
        last_seen = parse_utc(status.last_seen_at)
        if last_seen is not None and moment - last_seen < gone_after:
            continue
        decision = owner_liveness(agent, agent_id, now=moment, status=status)
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
