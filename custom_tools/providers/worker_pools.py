"""TeamArchitect's view of worker pools and delegated work, and its controls over them.

Delegated tasks wait in their worker type's shared queue until a free worker
takes one (ADR-019); the worker pool controller starts workers while tasks
wait, up to each type's pool size (``custom_agents.worker_pool_controller``).
These tools let the team lead see that machinery, change pool sizes inside the
operator ceilings (owner's decision 2026-10-04; administrators get a notice of
every change) and see or stop the delegated tasks of its current request.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

from strands import tool

from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import get_agent_ref

_EXPLANATION = {
    "tasks_waiting": "Delegated tasks in the type's queue that no worker has taken yet.",
    "idle_workers_asking": "Workers of the type that are free and asking for a task.",
    "oldest_wait_seconds": "How long the oldest waiting task has waited.",
    "workers_running": "Workers of the type that are up (busy or free).",
    "workers_starting": "Workers the controller started that are not ready yet.",
    "workers_unknown": "Starts whose process cannot be checked; they hold a slot until proven gone.",
    "workers_draining": "Workers asked to leave (the pool is above its size); they finish their work first.",
    "workers_waiting_out_provider_limit": "Workers whose model provider refused calls; they retry later and take no new work, and no workers are added meanwhile.",
    "pool_size": "effective: in force now; source: temporary, lasting (both set by you) or default (configuration); ceiling: the operator's maximum.",
    "decision": "What the controller did on its last pass and why.",
    "open_problems": "Worker pool problems still open (capacity, provider_limited, start_failed); each gives you a review.",
}


def _agent() -> Any:
    agent = get_agent_ref()
    if agent is None:
        raise RuntimeError("Agent reference not set")
    return agent


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra})


async def _snapshot(agent: Any, settings: Any) -> dict[str, Any]:
    """The controller's latest view, or a fresh one when it is missing or old."""

    from custom_agents.worker_pool_controller import WorkerPoolController

    snapshot = dict(getattr(agent, "_worker_pool_snapshot", None) or {})
    observed = snapshot.get("observed_at")
    fresh = False
    if observed:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(observed)
        fresh = age < timedelta(seconds=60)
    if fresh or getattr(agent, "_local_team_runtime", None) is None:
        return snapshot
    observations, team_active, team = await WorkerPoolController(agent).observe(settings)
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "team": {"workers_active": team_active, "limit": team.as_dict()},
        "pools": {observation.agent_type: observation.as_dict() for observation in observations},
    }


async def _pool_history(agent: Any, settings: Any) -> dict[str, Any]:
    """Open problems, problems in the review window, provider-limit events and size changes."""

    from custom_agents.worker_pool_limits import worker_pool_repository

    repository = worker_pool_repository(agent)
    run = agent.task_manager.operations_runtime.run
    team_id = str(agent.team_id)
    now = datetime.now(timezone.utc)
    window = timedelta(hours=settings.recurring_incident_window_hours)
    open_problems = await run(repository.list_open_incidents, team_id)
    recent = await run(partial(repository.recent_incidents, team_id, since=now - window))
    events = await run(
        partial(
            repository.provider_limit_events,
            team_id,
            since=now - timedelta(minutes=settings.provider_limit.review_window_minutes),
        )
    )
    limits = await run(repository.list_limits, team_id)
    return {
        "open_problems": [
            {
                "scope": item.agent_type,
                "kind": item.kind,
                "open_since": item.opened_at_utc.isoformat(),
                "reviews": item.review_count,
                "details": item.details,
            }
            for item in open_problems
        ],
        "problems_in_window": [
            {
                "scope": item.agent_type,
                "kind": item.kind,
                "opened": item.opened_at_utc.isoformat(),
                "closed": item.closed_at_utc.isoformat() if item.closed_at_utc else None,
            }
            for item in recent
        ],
        "provider_limit_events_in_window": len(events),
        "pool_size_changes": [
            {
                "scope": item.scope,
                "recent_changes": [change.model_dump(mode="json") for change in item.history[-3:]],
            }
            for item in limits
        ],
    }


@tool
async def inspect_worker_pools(agent_type: str = "") -> str:
    """See the worker queues and pools: waiting tasks, free and busy workers, pool sizes, problems.

    Delegated tasks wait in their worker type's queue until a free worker takes
    one; the runtime starts workers while tasks wait, up to each type's pool
    size and the team limit. Use this before deciding on a worker pool review
    or when delegated work seems slow.

    Args:
        agent_type: One worker type to show; empty shows every type with waiting
            tasks or workers, and the team totals.
    """

    from custom_agents.worker_pool_settings import current_worker_pool_settings

    agent = _agent()
    try:
        settings = current_worker_pool_settings(agent)
    except ValueError as exc:
        return _error(f"The worker pool settings are not valid: {exc}")
    snapshot = await _snapshot(agent, settings)
    pools = snapshot.get("pools") or {}
    if agent_type:
        pools = {name: pool for name, pool in pools.items() if name == agent_type}
    else:
        pools = {
            name: pool
            for name, pool in pools.items()
            if pool.get("tasks_waiting") or pool.get("workers_running") or pool.get("workers_starting")
        }
    history = await _pool_history(agent, settings)
    return json.dumps(
        {
            "observed_at": snapshot.get("observed_at"),
            "team": snapshot.get("team"),
            "pools": pools,
            **history,
            "settings": {
                "default_max_workers_per_type": settings.default_max_workers_per_type,
                "max_workers_per_type_ceiling": settings.max_workers_per_type_ceiling,
                "max_total_workers_per_team": settings.max_total_workers_per_team,
                "scale_up_wait_seconds": settings.scale_up_wait_seconds,
                "capacity_warning_ratio": settings.capacity_warning_ratio,
            },
            "explanation": _EXPLANATION,
        },
        default=str,
    )


@tool
async def set_worker_pool_limit(
    agent_type: str, max_workers: int, reason: str, for_minutes: float = 0
) -> str:
    """Set how many workers of a type (or the whole team) may run, inside the operator ceilings.

    A worker pool review offers: A) a larger pool for a while (for_minutes > 0),
    B) a larger pool for good (for_minutes = 0), C) no change, D) fewer workers
    (a smaller size, for a while or for good). A smaller size makes surplus
    workers leave once they hold no work; running tasks are never interrupted.
    Administrators get a notice of every change with your reason.

    Args:
        agent_type: The worker type, or "team" for the team-wide limit.
        max_workers: The new size; at most the operator ceiling.
        reason: Why, in plain words; administrators read it.
        for_minutes: How long a temporary size lasts; 0 sets the lasting size.
    """

    from custom_agents.worker_pool_limits import PoolLimitRefused, set_pool_limit
    from custom_agents.worker_pool_settings import current_worker_pool_settings
    from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
    from storage.worker_pool_repository import TEAM_SCOPE

    agent = _agent()
    scope = TEAM_SCOPE if agent_type.strip().lower() in {"team", "*"} else agent_type.strip()
    runtime = getattr(agent, "_local_team_runtime", None)
    if scope != TEAM_SCOPE and (
        scope == TEAM_ARCHITECT_AGENT_TYPE
        or runtime is None
        or scope not in runtime.agent_types()
    ):
        return _error(f"{agent_type!r} is not a worker type of this team.")
    if for_minutes < 0:
        return _error("for_minutes cannot be negative.")
    try:
        settings = current_worker_pool_settings(agent)
        until = (
            datetime.now(timezone.utc) + timedelta(minutes=float(for_minutes)) if for_minutes else None
        )
        limit = await agent.task_manager.operations_runtime.run(
            partial(
                set_pool_limit,
                agent,
                scope=scope,
                max_workers=int(max_workers),
                until_utc=until,
                reason=reason,
                settings=settings,
                set_by=f"TeamArchitect ({agent.id})",
            )
        )
    except (PoolLimitRefused, ValueError) as exc:
        return _error(str(exc))
    await agent.task_manager.operations_runtime.run(
        partial(_notify_administrators, agent, scope=scope, limit=limit, reason=reason, until=until)
    )
    return json.dumps(
        {
            "scope": scope,
            "pool_size": limit.as_dict(),
            "next": (
                "The controller starts workers for waiting tasks within seconds, up to the new size."
                if limit.effective
                else "No workers of this type will be started; waiting tasks stay queued."
            ),
            "administrators_told": True,
        }
    )


def _notify_administrators(agent: Any, *, scope: str, limit: Any, reason: str, until: datetime | None) -> None:
    from access_control.people import PERSON_ROLE_ADMIN
    from custom_agents.access_runtime import send_runtime_notice_once
    from custom_messages.contracts.access_v2 import AccessNoticeKind

    label = "the team" if scope == "*" else f"{scope} workers"
    when = f" until {until.strftime('%Y-%m-%d %H:%M UTC')}" if until else " (lasting)"
    send_runtime_notice_once(
        agent,
        notice_id=f"worker_pool_limit:{agent.team_id}:{scope}:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}",
        kind=AccessNoticeKind.WORKER_POOL_CHANGED,
        title=f"TeamArchitect set the pool size for {label} to {limit.effective}{when}",
        summary=(
            f"Reason: {reason.strip()}\n"
            f"Operator ceiling: {limit.ceiling}; configured default: {limit.default}. "
            "Ceilings are in agent.worker_pools in the configuration."
        ),
        roles=(PERSON_ROLE_ADMIN,),
    )


async def _current_request(agent: Any) -> tuple[str | None, str | None]:
    """The current task's request (root task id), and an error when there is none."""

    manager = agent.task_manager
    current = manager.current_task_id
    if not current:
        return None, "There is no current task."
    root = await manager.operations_runtime.run(manager.resolve_root_task_id, current) or current
    return root, None


@tool
async def inspect_delegated_work(task_id: str = "") -> str:
    """See the delegated tasks of the current request: waiting, running, done or failed.

    Shows for each: its worker type, where it is (waiting for a free worker,
    running on a named worker), how often it moved to another worker after its
    worker was lost, and why it failed when trusted code knows
    (provider_limited, worker_lost).

    Args:
        task_id: One delegated task of the current request; empty shows them all.
    """

    from custom_agents.delegated_work_runtime import delegated_tasks
    from storage.delegated_task_repository import status_counts

    agent = _agent()
    root, error = await _current_request(agent)
    if error:
        return _error(error)
    manager = agent.task_manager
    run = manager.operations_runtime.run
    records = await run(delegated_tasks(agent).list_for_root, str(agent.team_id), root)
    if task_id:
        records = tuple(record for record in records if record.task_id == task_id)
        if not records:
            return _error("That task is not a delegated task of the current request.")
    rows = []
    for record in records:
        info = await run(manager.get_task_info, record.task_id)
        statement = str(getattr(getattr(info, "task", None), "problem_statement", "") or "")
        rows.append(
            {
                "task_id": record.task_id,
                "worker_type": record.named_agent_type,
                "status": record.status,
                "worker": record.holder_agent_name or record.holder_agent_id,
                "taken_at": record.claimed_at_utc.isoformat() if record.claimed_at_utc else None,
                "published_at": record.published_at_utc.isoformat(),
                "moved_to_another_worker": record.interruptions,
                "failure": record.failure_category,
                "task": statement[:300],
            }
        )
    return json.dumps({"request": root, "counts": status_counts(list(records)), "tasks": rows}, default=str)


@tool
async def cancel_delegated_task(task_id: str, reason: str) -> str:
    """Stop one delegated task of the current request, waiting or running.

    A waiting task is withdrawn from its queue; a running one is stopped on its
    worker. Its result no longer counts; stop the tasks you do not need before
    answering with what you have.

    Args:
        task_id: A delegated task of the current request (see inspect_delegated_work).
        reason: Why, in plain words.
    """

    from custom_agents.delegated_work_runtime import stop_delegated_task
    from custom_messages.enums import ProgressState

    agent = _agent()
    if not reason.strip():
        return _error("Give a reason.")
    root, error = await _current_request(agent)
    if error:
        return _error(error)
    manager = agent.task_manager
    run = manager.operations_runtime.run
    parent_id = await run(manager.get_parent_task_id, task_id)
    info = await run(manager.get_task_info, task_id)
    task_root = await run(manager.resolve_root_task_id, task_id) if info else None
    if info is None or parent_id is None or task_root != root:
        return _error("That task is not a delegated task of the current request.")
    if manager.is_terminal_state(info.state):
        return json.dumps({"task_id": task_id, "status": info.state.value, "note": "Already ended."})
    now = datetime.now(timezone.utc).isoformat()
    from custom_agents.access_runtime import verified_person_id

    await run(
        partial(
            manager.record_execution_graph_cancelled,
            root_task_id=root,
            task_ids=[task_id],
            canonical_user_id=verified_person_id(agent),
            command_id=f"cancel_delegated:{task_id}",
            reason=reason.strip(),
            cancelled_at=now,
        )
    )
    await run(
        partial(
            manager.try_transition_task_state,
            task_id,
            allowed_from={
                ProgressState.ACCEPTED,
                ProgressState.IN_PROGRESS,
                ProgressState.WAITING,
                ProgressState.PAUSED,
                ProgressState.PARTIAL,
                ProgressState.RETRYING,
                ProgressState.UNDEFINED,
            },
            to_state=ProgressState.CANCELLED,
            timestamp=now,
            reason=reason.strip(),
            source="cancel_delegated_task",
        )
    )
    await run(manager.bubble_subtask_result, parent_id, task_id)
    reached = await stop_delegated_task(agent, parent_task_id=parent_id, subtask_info=info, reason=reason.strip())
    return json.dumps({"task_id": task_id, "status": "cancelled", "worker_told": reached})


PROVIDER = ToolProvider(
    name="worker_pools",
    tools=(inspect_worker_pools, set_worker_pool_limit, inspect_delegated_work, cancel_delegated_task),
)
