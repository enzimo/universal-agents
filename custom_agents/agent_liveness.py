"""Whether an agent that holds saved work is gone: one rule for takeover, replacements and notices.

An agent's launch record decides when it has one. A record that ended
(``retired``/``failed``, written after the worker's thread ended) or whose
process is provably gone (``inspect_launch`` "absent", backed by
``runtime_hosting.process_run``) means gone; any other record means the worker
may still run, however stale its heartbeat. Owners without a launch record
(startup agents, pruned records) fall back to their heartbeat
(``agent_states``). Before 2026-10-04 takeover trusted a 30-second heartbeat
alone, so a slow but live worker could lose its work.

Used by ``runtime_recovery`` (startup takeover), ``orphaned_work_takeover``
(the lead's replacements) and ``stuck_tasks`` (notices). It depends only on
stores, so every runtime module may import it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from custom_agents.agent_state_store import AgentStateStore
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState, AgentLaunchV1
from storage.agent_launch_repository import ACTIVE_LAUNCH_STATES, AgentLaunchRepository

_ENDED_LAUNCH_STATES = frozenset({AgentLaunchState.RETIRED, AgentLaunchState.FAILED})


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


def lookup_owner_status(agent: Any, owner_agent_id: str) -> Optional[AgentStatus]:
    """The owner's last published status (heartbeat), or None."""
    if not owner_agent_id:
        return None
    store = getattr(agent, "_agent_state_store", None) or AgentStateStore(
        db_path=agent.task_manager._db_path,
        logger=agent._logger,
        storage_settings=agent.task_manager.storage_settings,
    )
    return store.get_by_id(owner_agent_id)


def latest_launch(agent: Any, owner_agent_id: str) -> AgentLaunchV1 | None:
    records = [
        record
        for record in AgentLaunchRepository(agent.task_manager.storage_settings).list(agent.team_id)
        if record.agent_id == owner_agent_id
    ]
    return max(records, key=lambda record: record.created_at_utc) if records else None


def owner_gone_by_launch(agent: Any, owner_agent_id: str) -> bool | None:
    """Whether an owner is gone by its launch record; None when it has none (module docstring)."""
    record = latest_launch(agent, owner_agent_id)
    if record is None:
        return None
    if record.state in _ENDED_LAUNCH_STATES:
        return True
    runtime = getattr(agent, "_local_team_runtime", None)
    if record.state in ACTIVE_LAUNCH_STATES and runtime is not None:
        return str(runtime.inspect_launch(record)) == "absent"
    return False
