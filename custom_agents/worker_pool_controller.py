"""Start workers while delegated tasks wait; tell TeamArchitect when limits bite.

Delegated tasks wait in their worker type's shared queue until a free worker
takes one (ADR-019). This controller is trusted code, not a model. It runs in
the team host's startup loop (``schedule_worker_controller.local_schedule_runtime_loop``),
independent of TeamArchitect's model, current task and paused state. Every pass,
for each registered worker type, it reads:

- from NATS, the type's queue: tasks waiting, idle workers asking for work,
  tasks handed out but not yet admitted, and how long the oldest task waited;
- from launch records and this process's registry: workers running, starting,
  of unknown state (they hold capacity), draining or waiting out a model
  provider's limit;
- the type's pool size and the team's (``custom_agents.worker_pool_limits``).

and decides (``decide``):

- tasks waiting and no worker of the type: start one at once;
- tasks waiting, no idle worker asking, the oldest waited
  ``scale_up_wait_seconds``: start up to the pool size, the team limit and
  ``max_starts_per_pass``;
- a worker of the type is waiting out a provider limit: start none;
- more workers than the pool size (TeamArchitect lowered it): idle ones leave
  now, busy ones stop asking for work and leave when they hold none.

Problems become incidents (``storage.worker_pool_repository``) that
``custom_agents.worker_pool_reviews`` turns into reviews for TeamArchitect and,
when they keep coming back, notices for administrators: a pool or the team at
``capacity_warning_ratio`` of its limit while tasks wait (``capacity``), a type
whose provider keeps refusing calls (``provider_limited``), and a worker that
could not be started (``start_failed``).

Pool workers serve every person's tasks; their starts are recorded for the
team's pool service identity (``pool_launch_owner``), and access stays decided
per call for each task's person.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Optional

from custom_agents.worker_pool_limits import PoolLimit, team_limit, type_limit, worker_pool_repository
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.schedule_worker_v1 import (
    AUTOMATIC_WORKER_CAPACITY_LIMIT,
    TEAM_WORKER_CAPACITY_LIMIT,
    AgentLaunchState,
)
from id_generation import new_id
from runtime_settings.worker_pools import WorkerPoolsConfig
from storage.agent_launch_repository import ACTIVE_LAUNCH_STATES, AgentLaunchRepository
from storage.worker_pool_repository import (
    INCIDENT_CAPACITY,
    INCIDENT_PROVIDER_LIMITED,
    INCIDENT_START_FAILED,
    TEAM_SCOPE,
)

logger = logging.getLogger(__name__)

POOL_LAUNCH_OWNER_PREFIX = "worker_pool:"
# Start refusals that only mean "the limit was reached meanwhile", not a fault.
_CAPACITY_REFUSALS = frozenset({AUTOMATIC_WORKER_CAPACITY_LIMIT, TEAM_WORKER_CAPACITY_LIMIT})


def pool_launch_owner(team_id: str) -> str:
    """Whom a pool worker's start is recorded for: the team's pool, not a person."""

    return f"{POOL_LAUNCH_OWNER_PREFIX}{team_id}"


@dataclass(frozen=True)
class PoolObservation:
    """One worker type's queue and workers at one moment."""

    agent_type: str
    waiting: int
    asking: int
    unacknowledged: int
    oldest_wait_seconds: Optional[float]
    running: int
    starting: int
    unknown: int
    draining: int
    cooling_down: int
    limit: PoolLimit
    observed_at: datetime

    @property
    def active(self) -> int:
        return self.running + self.starting + self.unknown

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_type": self.agent_type,
            "tasks_waiting": self.waiting,
            "idle_workers_asking": self.asking,
            "tasks_handed_out_not_admitted": self.unacknowledged,
            "oldest_wait_seconds": None
            if self.oldest_wait_seconds is None
            else round(self.oldest_wait_seconds, 1),
            "workers_running": self.running,
            "workers_starting": self.starting,
            "workers_unknown": self.unknown,
            "workers_draining": self.draining,
            "workers_waiting_out_provider_limit": self.cooling_down,
            "pool_size": self.limit.as_dict(),
            "observed_at": self.observed_at.isoformat(),
        }


@dataclass(frozen=True)
class PoolDecision:
    """What one pass does for one worker type, and why (plain words)."""

    starts: int
    reason: str
    at_capacity: bool = False
    provider_blocked: bool = False
    surplus: int = 0


def capacity_threshold(limit: int, ratio: float) -> int:
    """Workers at which a limit counts as reached (95% of 4 is 4; of 32 is 31)."""

    return math.ceil(ratio * limit) if limit > 0 else 0


def decide(
    observation: PoolObservation,
    *,
    team_active: int,
    team_limit_value: int,
    settings: WorkerPoolsConfig,
) -> PoolDecision:
    """Pure decision for one worker type (see the module docstring)."""

    limit = observation.limit.effective
    surplus = max(0, observation.running - observation.draining - limit)
    if observation.waiting <= 0:
        return PoolDecision(0, "no tasks waiting", surplus=surplus)
    long_wait = (observation.oldest_wait_seconds or 0.0) >= settings.scale_up_wait_seconds
    at_capacity = long_wait and observation.active >= capacity_threshold(
        limit, settings.capacity_warning_ratio
    )
    if observation.cooling_down:
        return PoolDecision(
            0,
            "a worker of this type is waiting out the model provider's limit; more workers would make it worse",
            at_capacity=at_capacity,
            provider_blocked=True,
            surplus=surplus,
        )
    if not settings.enabled:
        return PoolDecision(0, "worker pools are turned off", at_capacity=at_capacity, surplus=surplus)
    room = min(limit - observation.active, team_limit_value - team_active)
    if room <= 0:
        reason = (
            "the team is at its worker limit"
            if team_limit_value - team_active <= 0
            else "the pool is at its size"
        )
        return PoolDecision(0, reason, at_capacity=long_wait, surplus=surplus)
    if observation.active == 0:
        return PoolDecision(1, "tasks wait and no worker of this type runs", at_capacity=at_capacity)
    if observation.asking == 0 and long_wait:
        wanted = max(0, observation.waiting - observation.starting)
        starts = min(wanted, room, settings.max_starts_per_pass)
        return PoolDecision(
            starts,
            "tasks waited with no free worker",
            at_capacity=at_capacity,
            surplus=surplus,
        )
    return PoolDecision(0, "workers are free or starting", at_capacity=at_capacity, surplus=surplus)


@dataclass
class _Workers:
    running: set[str] = field(default_factory=set)
    starting: set[str] = field(default_factory=set)
    unknown: set[str] = field(default_factory=set)
    draining: set[str] = field(default_factory=set)
    cooling_down: set[str] = field(default_factory=set)


def _workers_by_type(launches: Any, local: Any) -> dict[str, _Workers]:
    found: dict[str, _Workers] = {}
    for record in launches:
        if record.state not in ACTIVE_LAUNCH_STATES:
            continue
        bucket = found.setdefault(record.named_agent_type, _Workers())
        if record.state == AgentLaunchState.READY:
            bucket.running.add(record.agent_id)
        elif record.state == AgentLaunchState.UNKNOWN:
            bucket.unknown.add(record.agent_id)
        else:
            bucket.starting.add(record.agent_id)
    for status in local:
        bucket = found.setdefault(status.named_agent_type, _Workers())
        bucket.starting.discard(status.self_id)
        bucket.unknown.discard(status.self_id)
        bucket.running.add(status.self_id)
        if status.draining:
            bucket.draining.add(status.self_id)
        if status.cooling_down:
            bucket.cooling_down.add(status.self_id)
    return found


class WorkerPoolController:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.runtime = agent.task_manager.operations_runtime
        self.team_id = str(agent.team_id)

    def _adapter(self) -> Any:
        return self.agent._ensure_nats_transport_adapter()

    def _worker_types(self) -> list[str]:
        from custom_agents.agent_instantiation_service import current_worker_limits

        local_runtime = self.agent._local_team_runtime
        denied = set(current_worker_limits(self.agent).denied_agent_types)
        return sorted(
            agent_type
            for agent_type in local_runtime.agent_types()
            if agent_type != TEAM_ARCHITECT_AGENT_TYPE and agent_type not in denied
        )

    async def _ensure_queue(self, agent_type: str) -> bool:
        ensured: set[str] = self.agent._ensured_worker_queues
        if agent_type in ensured:
            return True
        try:
            await self._adapter().ensure_worker_queue(agent_type)
        except Exception:
            logger.warning(
                "Could not create a worker type's queue; retrying next pass. "
                "event=worker_pool_queue_create_failed team_id=%s agent_type=%s",
                self.team_id,
                agent_type,
                exc_info=True,
            )
            return False
        ensured.add(agent_type)
        return True

    async def observe(self, settings: WorkerPoolsConfig) -> tuple[list[PoolObservation], int, PoolLimit]:
        """Every registered worker type's queue and workers, plus the team's totals."""

        now = datetime.now(timezone.utc)
        launches = await self.runtime.run(AgentLaunchRepository(self.agent.task_manager.storage_settings).list, self.team_id)
        local = self.agent._local_team_runtime.local_workers(self.team_id)
        workers = _workers_by_type(launches, local)
        team_active = sum(
            len(bucket.running | bucket.starting | bucket.unknown) for bucket in workers.values()
        )
        team = await self.runtime.run(partial(team_limit, self.agent, settings, now=now))
        observations = []
        adapter = self._adapter()
        for agent_type in self._worker_types():
            if not await self._ensure_queue(agent_type):
                continue
            state = await adapter.worker_queue_state(agent_type)
            if state is None:
                self.agent._ensured_worker_queues.discard(agent_type)
                continue
            oldest_at = (
                await adapter.oldest_worker_queue_message_at(agent_type)
                if state.waiting or state.unacknowledged
                else None
            )
            bucket = workers.get(agent_type, _Workers())
            observations.append(
                PoolObservation(
                    agent_type=agent_type,
                    waiting=state.waiting,
                    asking=state.asking,
                    unacknowledged=state.unacknowledged,
                    oldest_wait_seconds=(now - oldest_at).total_seconds() if oldest_at else None,
                    running=len(bucket.running),
                    starting=len(bucket.starting),
                    unknown=len(bucket.unknown),
                    draining=len(bucket.draining),
                    cooling_down=len(bucket.cooling_down),
                    limit=await self.runtime.run(
                        partial(type_limit, self.agent, agent_type, settings, now=now)
                    ),
                    observed_at=now,
                )
            )
        return observations, team_active, team

    async def reconcile(self) -> None:
        from custom_agents.worker_pool_reviews import WorkerPoolIncidents
        from custom_agents.worker_pool_settings import current_worker_pool_settings

        try:
            settings = current_worker_pool_settings(self.agent)
        except ValueError:
            logger.exception(
                "The worker pool settings are not valid; no workers are started until they "
                "are fixed. event=worker_pool_settings_invalid team_id=%s",
                self.team_id,
            )
            return
        observations, team_active, team = await self.observe(settings)
        incidents = WorkerPoolIncidents(self.agent, settings)
        self.agent._worker_pool_snapshot = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "team": {"workers_active": team_active, "limit": team.as_dict()},
            "pools": {observation.agent_type: observation.as_dict() for observation in observations},
        }
        for observation in observations:
            decision = decide(
                observation,
                team_active=team_active,
                team_limit_value=team.effective,
                settings=settings,
            )
            self.agent._worker_pool_snapshot["pools"][observation.agent_type]["decision"] = asdict(decision)
            started = await self._start(observation, decision, incidents)
            team_active += started
            if decision.surplus:
                await self._drain(observation.agent_type, decision.surplus)
            await incidents.observe_type(observation, decision)
        await incidents.observe_team(observations, team_active=team_active, team=team)
        await incidents.observe_provider_limits(observations)
        await incidents.settle()
        await self.runtime.run(
            partial(
                worker_pool_repository(self.agent).prune,
                self.team_id,
                before=datetime.now(timezone.utc)
                - timedelta(hours=max(48.0, settings.recurring_incident_window_hours * 2)),
            )
        )

    async def _start(self, observation: PoolObservation, decision: PoolDecision, incidents: Any) -> int:
        started = 0
        for _ in range(decision.starts):
            result = await self.runtime.run(partial(self._launch, observation.agent_type))
            if result.accepted:
                started += 1
                incidents.start_succeeded(observation.agent_type)
                logger.info(
                    "Started a worker for waiting delegated tasks. event=worker_pool_start "
                    "team_id=%s agent_type=%s launch_id=%s agent_id=%s waiting=%s reason=%s",
                    self.team_id,
                    observation.agent_type,
                    result.launch_id,
                    result.agent_id,
                    observation.waiting,
                    decision.reason,
                )
                continue
            if result.reason in _CAPACITY_REFUSALS:
                logger.info(
                    "A pool start found its limit reached. event=worker_pool_start_at_limit "
                    "team_id=%s agent_type=%s reason=%s",
                    self.team_id,
                    observation.agent_type,
                    result.reason,
                )
                break
            logger.error(
                "Could not start a worker for waiting delegated tasks. "
                "event=worker_pool_start_failed team_id=%s agent_type=%s status=%s reason=%s waiting=%s",
                self.team_id,
                observation.agent_type,
                result.status,
                result.reason,
                observation.waiting,
            )
            incidents.start_failed(observation, str(result.reason or result.status))
            break
        return started

    def _launch(self, agent_type: str) -> Any:
        from custom_agents.agent_instantiation_service import AgentInstantiationService

        service = AgentInstantiationService(self.agent)
        return service.launch(
            agent_type=agent_type,
            owner_id=pool_launch_owner(self.team_id),
            request_id=f"pool:{agent_type}:{new_id('pool_start')}",
            source="pool",
            idle_ttl_seconds=service.default_idle_ttl(agent_type),
            join_pending=False,
        )

    async def _drain(self, agent_type: str, surplus: int) -> None:
        runtime = self.agent._local_team_runtime
        candidates = [
            status
            for status in runtime.local_workers(self.team_id)
            if status.named_agent_type == agent_type and status.launched and not status.draining
        ]
        # Idle workers leave first; busy ones finish their work and then leave.
        candidates.sort(key=lambda status: (not status.idle, status.self_id))
        for status in candidates[:surplus]:
            if runtime.drain_worker(status.self_id):
                logger.warning(
                    "Asked a worker to leave because its pool is above its size. "
                    "event=worker_pool_drain_requested team_id=%s agent_type=%s agent_id=%s idle=%s",
                    self.team_id,
                    agent_type,
                    status.self_id,
                    status.idle,
                )


async def reconcile_worker_pools(agent: Any) -> None:
    """One controller pass; failures are logged and never stop the host loop."""

    if not getattr(agent, "is_team_lead", False) or agent._local_team_runtime is None:
        return
    try:
        await WorkerPoolController(agent).reconcile()
    except Exception:
        logger.exception(
            "Worker pool controller pass failed; retrying next pass. "
            "event=worker_pool_pass_failed team_id=%s",
            agent.team_id,
        )


__all__ = [
    "INCIDENT_CAPACITY",
    "INCIDENT_PROVIDER_LIMITED",
    "INCIDENT_START_FAILED",
    "PoolDecision",
    "PoolObservation",
    "TEAM_SCOPE",
    "WorkerPoolController",
    "capacity_threshold",
    "decide",
    "pool_launch_owner",
    "reconcile_worker_pools",
]
