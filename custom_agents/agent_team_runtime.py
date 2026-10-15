"""Process-local composition contract between an agent and its team factory.

The team module owns catalog hydration and concrete agent construction.  The
agent runtime needs only the resulting type names and a callable for an
authorized team lead to request another local worker.  Keeping that small
contract here prevents the runtime from importing its composition root.

This is deliberately not a distributed-agent protocol.  Cross-runtime agent
creation continues to require an independently designed, structured NATS
contract and authorization flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from weakref import WeakValueDictionary
from typing import Any, Callable, Protocol
from custom_messages.contracts.schedule_worker_v1 import (
    AgentLaunchResultV1,
    AgentLaunchV1,
)

_LOCAL_WORKERS: WeakValueDictionary[str, Any] = WeakValueDictionary()
_LOCAL_WORKERS_LOCK = RLock()


@dataclass(frozen=True)
class LocalWorkerStatus:
    self_id: str
    named_agent_type: str
    has_execution_capacity: bool
    # Holds no task of its own at all (no running, queued or waiting task).
    idle: bool = False
    # Asked by the worker pool controller to leave when it holds no work.
    draining: bool = False
    # Waiting out a model provider's limit; takes no new work meanwhile.
    cooling_down: bool = False
    # Started by a recorded launch (False for startup agents of the roster).
    launched: bool = False


def register_local_worker(agent: Any) -> None:
    with _LOCAL_WORKERS_LOCK:
        _LOCAL_WORKERS[agent.id] = agent


def unregister_local_worker(agent_id: str) -> None:
    with _LOCAL_WORKERS_LOCK:
        _LOCAL_WORKERS.pop(agent_id, None)


def local_worker_statuses(team_id: str) -> tuple[LocalWorkerStatus, ...]:
    """Read local readiness only. Remote NATS announcements cannot populate this registry."""
    with _LOCAL_WORKERS_LOCK:
        workers = list(_LOCAL_WORKERS.values())
    from custom_agents.pool_intake import provider_cooldown_active

    return tuple(
        LocalWorkerStatus(
            agent.id,
            agent.named_agent_type,
            not agent.paused and not agent.is_executing,
            idle=_holds_no_task(agent),
            draining=bool(getattr(agent, "_pool_draining", False)),
            cooling_down=provider_cooldown_active(agent),
            launched=getattr(agent, "_worker_launch", None) is not None,
        )
        for agent in workers
        if agent.team_id == team_id and not agent.is_team_lead
        and agent._worker_ready and not agent.get_exit_event().is_set()
    )


def _holds_no_task(agent: Any) -> bool:
    """A cheap in-memory read; the worker's own idle timer checks properly."""
    if agent.is_executing:
        return False
    manager = agent.task_manager
    delegated_out = manager.task_state.subtask_to_parent_map
    return not any(
        task_id not in delegated_out and not manager.is_terminal_state(info.state)
        for task_id, info in list(manager.task_state.tasks.items())
    )


def drain_local_worker(agent_id: str) -> bool:
    """Ask one local worker to stop taking work and leave once it holds none."""
    with _LOCAL_WORKERS_LOCK:
        agent = _LOCAL_WORKERS.get(agent_id)
    if agent is None:
        return False
    agent._pool_draining = True
    return True


class LocalAgentSpawner(Protocol):
    """Construct and start one agent in the current runtime process."""

    def __call__(
        self,
        named_agent_type: str,
        team_id: str,
        is_team_lead: bool = False,
        mcp_profile_names: list[str] | None = None,
        idle_ttl_seconds: float | None = None,
        *,
        launch: AgentLaunchV1,
        runtime_config: Any,
    ) -> AgentLaunchResultV1: ...


class LocalAgentShutdown(Protocol):
    """Stop and join spawned agents owned by one local team runtime."""

    def __call__(
        self,
        team_id: str,
        owner_agent_id: str,
        timeout_seconds: float,
    ) -> None: ...


@dataclass(frozen=True)
class LocalAgentTeamRuntime:
    """Minimal team-owned capabilities consumed by ``UniversalAgent``."""

    # The team's agent types, read from the current definition catalog on each
    # call: TeamArchitect can create or remove a specialist type while the team
    # runs (custom_agents.generated_agent_provisioning), and delegation, the
    # worker pool controller and pool sizes must see it without a restart.
    agent_types: Callable[[], tuple[str, ...]]
    spawn_agent: LocalAgentSpawner
    shutdown_agents: LocalAgentShutdown
    resolve_definition: Callable[[str], dict[str, Any] | None]
    inspect_launch: Callable[[AgentLaunchV1], str]
    stop_launch: Callable[[AgentLaunchV1], bool]
    local_workers: Callable[[str], tuple[LocalWorkerStatus, ...]] = local_worker_statuses
    drain_worker: Callable[[str], bool] = drain_local_worker


