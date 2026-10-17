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
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from functools import partial
from typing import Any

from custom_messages.contracts.runtime_snapshot_v3 import decode_runtime_snapshot
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_agents.agent_liveness import latest_launch, owner_liveness

logger = logging.getLogger(__name__)

# How often the lead looks for dead owners holding unfinished work.
TAKEOVER_CHECK_SECONDS = 60.0
_TERMINAL_STATE_VALUES = frozenset({"completed", "failed", "cancelled", "abandoned"})


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


def _person_for(agent: Any, owner_agent_id: str, unfinished: list[Any]) -> str | None:
    """Whom the replacement's start is recorded for: the dead start's owner, else a task's person."""
    from custom_agents.stuck_tasks import StuckTasks, stuck_task_settings

    launch = latest_launch(agent, owner_agent_id)
    if launch is not None and launch.owner_canonical_user_id:
        return str(launch.owner_canonical_user_id)
    people = StuckTasks(agent, stuck_task_settings(agent))
    return next((person for person in (people.person_of(r.meta) for r in unfinished) if person), None)


def _orphaned_owners(agent: Any, now: datetime) -> list[dict[str, Any]]:
    """Dead owners of the team that hold unfinished work (storage lane)."""

    found = []
    for row in agent.task_manager.runtime_state_store.peek_task_runtime_by_team(agent.team_id):
        agent_type = row.owner_named_agent_type
        if row.owner_agent_id == agent.id or not agent_type or agent_type == TEAM_ARCHITECT_AGENT_TYPE:
            continue
        unfinished = _unfinished_tasks(row)
        if not unfinished or not owner_liveness(agent, row.owner_agent_id, now=now).gone:
            continue
        found.append(
            {
                "owner_agent_id": row.owner_agent_id,
                "agent_type": agent_type,
                "person": _person_for(agent, row.owner_agent_id, unfinished),
                "tasks": len(unfinished),
            }
        )
    return found


def _launch_takeover(agent: Any, owner: dict[str, Any]) -> Any:
    from custom_agents.agent_instantiation_service import AgentInstantiationService

    service = AgentInstantiationService(agent)
    return service.launch(
        agent_type=owner["agent_type"],
        owner_id=owner["person"],
        # One replacement per dead owner; a repeated pass finds the same start.
        request_id=f"takeover:{owner['owner_agent_id']}",
        source="delegation",
        idle_ttl_seconds=service.default_idle_ttl(owner["agent_type"]),
        join_pending=True,
    )


async def take_over_orphaned_work(agent: Any, *, now: datetime | None = None, clock: float | None = None) -> int:
    """Start replacements for dead owners holding unfinished work; returns how many were requested."""

    if not getattr(agent, "is_team_lead", False) or getattr(agent, "_local_team_runtime", None) is None:
        return 0
    if not getattr(agent, "same_role_runtime_takeover_enabled", True):
        return 0
    moment = time.monotonic() if clock is None else clock
    if moment < getattr(agent, "_orphaned_work_check_after", 0.0):
        return 0
    agent._orphaned_work_check_after = moment + TAKEOVER_CHECK_SECONDS
    run = agent.task_manager.operations_runtime.run
    owners = await run(partial(_orphaned_owners, agent, now or datetime.now(timezone.utc)))
    requested = 0
    for owner in owners:
        if not owner["person"]:
            agent._logger.warning(
                "A dead agent holds unfinished work but nobody it works for is known, so no "
                "replacement was started. event=orphaned_work_takeover_skipped owner_agent_id=%s agent_type=%s",
                owner["owner_agent_id"],
                owner["agent_type"],
            )
            continue
        result = await run(partial(_launch_takeover, agent, owner))
        if result.created:
            requested += 1
        log = agent._logger.warning if result.accepted or result.status == "waiting" else agent._logger.error
        log(
            "A dead agent holds unfinished work; a replacement of its type takes it over. "
            "event=orphaned_work_takeover owner_agent_id=%s agent_type=%s tasks=%s status=%s "
            "launch_id=%s created=%s reason=%s",
            owner["owner_agent_id"],
            owner["agent_type"],
            owner["tasks"],
            result.status,
            result.launch_id,
            result.created,
            result.reason,
        )
    return requested
