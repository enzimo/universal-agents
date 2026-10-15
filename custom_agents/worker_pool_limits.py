"""The effective pool size of a worker type and of the team, and changing them.

A pool size comes from, in order: TeamArchitect's temporary size while it
lasts, TeamArchitect's lasting size, then the configured default
(``agent.worker_pools.default_max_workers_per_type``; for the team,
``max_total_workers_per_team``). It never exceeds the operator ceiling
(``max_workers_per_type_ceiling``, or ``max_total_workers_per_team`` for the
team); a stored size above a ceiling an operator lowered is cut to the ceiling
and logged.

The owner decided on 2026-10-04 that TeamArchitect changes pool sizes itself
(options A, B and D of a worker pool review), inside the ceilings; every change
sends administrators an Inbox notice so they see who changed what and why.
Ceilings are operator-only configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from runtime_settings.worker_pools import WorkerPoolsConfig
from storage.worker_pool_repository import TEAM_SCOPE, WorkerPoolLimitV1, WorkerPoolRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolLimit:
    """How many workers a scope (one type, or the team) may run, and why."""

    scope: str
    effective: int
    default: int
    ceiling: int
    lasting: Optional[int] = None
    temporary: Optional[int] = None
    temporary_until_utc: Optional[datetime] = None
    # "temporary", "lasting" or "default": which value is in force.
    source: str = "default"

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "effective": self.effective,
            "source": self.source,
            "default": self.default,
            "ceiling": self.ceiling,
            "lasting": self.lasting,
            "temporary": self.temporary,
            "temporary_until_utc": self.temporary_until_utc.isoformat()
            if self.temporary_until_utc
            else None,
        }


def worker_pool_repository(agent: Any) -> WorkerPoolRepository:
    repository = getattr(agent, "_worker_pool_repository", None)
    if repository is None:
        repository = WorkerPoolRepository(agent.task_manager.storage_settings)
        agent._worker_pool_repository = repository
    return repository


def resolve_limit(
    record: Optional[WorkerPoolLimitV1],
    *,
    scope: str,
    default: int,
    ceiling: int,
    now: datetime,
) -> PoolLimit:
    """The size in force for one scope (pure; see the module docstring)."""

    lasting = record.lasting_max_workers if record else None
    temporary = record.temporary_max_workers if record and record.temporary_active(now) else None
    until = record.temporary_until_utc if temporary is not None and record else None
    if temporary is not None:
        value, source = temporary, "temporary"
    elif lasting is not None:
        value, source = lasting, "lasting"
    else:
        value, source = default, "default"
    if value > ceiling:
        logger.warning(
            "A worker pool size is above the operator ceiling; the ceiling applies. "
            "event=worker_pool_limit_above_ceiling scope=%s size=%s ceiling=%s source=%s",
            scope,
            value,
            ceiling,
            source,
        )
        value = ceiling
    return PoolLimit(
        scope=scope,
        effective=value,
        default=min(default, ceiling),
        ceiling=ceiling,
        lasting=lasting,
        temporary=temporary,
        temporary_until_utc=until,
        source=source,
    )


def type_limit(
    agent: Any, agent_type: str, settings: WorkerPoolsConfig, *, now: Optional[datetime] = None
) -> PoolLimit:
    record = worker_pool_repository(agent).get_limit(str(agent.team_id), agent_type)
    return resolve_limit(
        record,
        scope=agent_type,
        default=settings.default_max_workers_per_type,
        ceiling=settings.max_workers_per_type_ceiling,
        now=now or datetime.now(timezone.utc),
    )


def team_limit(agent: Any, settings: WorkerPoolsConfig, *, now: Optional[datetime] = None) -> PoolLimit:
    record = worker_pool_repository(agent).get_limit(str(agent.team_id), TEAM_SCOPE)
    return resolve_limit(
        record,
        scope=TEAM_SCOPE,
        default=settings.max_total_workers_per_team,
        ceiling=settings.max_total_workers_per_team,
        now=now or datetime.now(timezone.utc),
    )


class PoolLimitRefused(ValueError):
    """A requested pool size is outside what TeamArchitect may set."""


def set_pool_limit(
    agent: Any,
    *,
    scope: str,
    max_workers: int,
    until_utc: Optional[datetime],
    reason: str,
    settings: WorkerPoolsConfig,
    set_by: str,
) -> PoolLimit:
    """Store a lasting or temporary pool size inside the operator ceiling."""

    ceiling = (
        settings.max_total_workers_per_team
        if scope == TEAM_SCOPE
        else settings.max_workers_per_type_ceiling
    )
    if max_workers < 0:
        raise PoolLimitRefused("A pool size cannot be negative.")
    if max_workers > ceiling:
        raise PoolLimitRefused(
            f"{max_workers} is above the operator ceiling of {ceiling}; an administrator "
            "raises the ceiling in the configuration (agent.worker_pools)."
        )
    now = datetime.now(timezone.utc)
    if until_utc is not None and until_utc <= now:
        raise PoolLimitRefused("A temporary pool size needs an end time in the future.")
    if not reason.strip():
        raise PoolLimitRefused("Give a reason; administrators see it.")
    record = worker_pool_repository(agent).set_limit(
        team_id=str(agent.team_id),
        scope=scope,
        max_workers=max_workers,
        until_utc=until_utc,
        set_by=set_by,
        reason=reason.strip(),
    )
    logger.warning(
        "A worker pool size was changed. event=worker_pool_limit_set team_id=%s scope=%s "
        "max_workers=%s until=%s set_by=%s",
        agent.team_id,
        scope,
        max_workers,
        until_utc.isoformat() if until_utc else None,
        set_by,
    )
    default = (
        settings.max_total_workers_per_team
        if scope == TEAM_SCOPE
        else settings.default_max_workers_per_type
    )
    return resolve_limit(record, scope=scope, default=default, ceiling=ceiling, now=now)


__all__ = [
    "PoolLimit",
    "PoolLimitRefused",
    "TEAM_SCOPE",
    "resolve_limit",
    "set_pool_limit",
    "team_limit",
    "type_limit",
    "worker_pool_repository",
]
