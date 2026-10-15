"""Fenced dynamic-worker launch custody on SQLite and PostgreSQL.

One locked team row serializes capacity and rate decisions, including the first
reservation. A timeout never makes an unknown live process safe to replace.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from custom_messages.contracts.schedule_worker_v1 import (
    AUTOMATIC_WORKER_CAPACITY_LIMIT,
    TEAM_WORKER_CAPACITY_LIMIT,
    WORKER_LAUNCH_RATE_LIMIT,
    AgentLaunchState,
    AgentLaunchV1,
)
from dataclasses import dataclass
from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite
from storage.database_clock import database_now

ACTIVE_LAUNCH_STATES = frozenset(
    {
        AgentLaunchState.RESERVED,
        AgentLaunchState.STARTING,
        AgentLaunchState.READY,
        AgentLaunchState.UNKNOWN,
    }
)
# Starts a new request may share instead of starting another worker.
_JOINABLE_STATES = frozenset({AgentLaunchState.RESERVED, AgentLaunchState.STARTING})


# Starts trusted code makes on its own; each type's pool size applies to them.
# Manual starts (TeamArchitect's instantiate_agent) and takeover replacements
# count toward it but are not refused by it; the team limit applies to all.
AUTOMATIC_LAUNCH_SOURCES = frozenset({"schedule", "pool"})


@dataclass(frozen=True)
class LaunchCapacity:
    """The limits one reservation is checked against (read fresh by the caller)."""

    lease_seconds: float
    max_total_workers_per_team: int
    max_launches_per_type_per_hour: int
    # The effective pool size of the launch's type (custom_agents.worker_pool_limits).
    max_workers_of_type: int | None = None

    @classmethod
    def defaults(cls) -> "LaunchCapacity":
        from runtime_settings.schedule_workers import ScheduleWorkersConfig
        from runtime_settings.worker_pools import WorkerPoolsConfig

        pools = WorkerPoolsConfig()
        return cls(
            lease_seconds=ScheduleWorkersConfig().lease_seconds,
            max_total_workers_per_team=pools.max_total_workers_per_team,
            max_launches_per_type_per_hour=pools.max_launches_per_type_per_hour,
            max_workers_of_type=pools.default_max_workers_per_type,
        )


class LaunchConflict(RuntimeError):
    """The launch fence, identity, or capacity changed before a transition."""


class AgentLaunchRepository:
    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if not settings.postgres_dsn and not settings.sqlite_path:
            raise ValueError("Launch custody requires configured storage.")
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_launch_teams (team_id TEXT PRIMARY KEY)"
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS agent_launches (
                launch_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL UNIQUE,
                team_id TEXT NOT NULL, named_agent_type TEXT NOT NULL,
                state TEXT NOT NULL, fence INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS agent_launch_team_state ON agent_launches(team_id, state, created_at)"
            )
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    def _lock_team(self, conn: Any, team_id: str) -> None:
        p = self.dialect.bind
        conn.execute(
            f"INSERT INTO agent_launch_teams(team_id) VALUES ({p}) ON CONFLICT DO NOTHING",
            (team_id,),
        )
        suffix = " FOR UPDATE" if self.dialect == POSTGRES else ""
        conn.execute(
            f"SELECT team_id FROM agent_launch_teams WHERE team_id={p}{suffix}",
            (team_id,),
        ).fetchone()

    def _list(self, conn: Any, team_id: str) -> tuple[AgentLaunchV1, ...]:
        rows = conn.execute(
            f"SELECT payload_json FROM agent_launches WHERE team_id={self.dialect.bind} ORDER BY created_at,launch_id",
            (team_id,),
        ).fetchall()
        return tuple(AgentLaunchV1.model_validate_json(row[0]) for row in rows)

    def list(self, team_id: str) -> tuple[AgentLaunchV1, ...]:
        with self._connect() as conn:
            return self._list(conn, team_id)

    def get(self, launch_id: str, team_id: str) -> AgentLaunchV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM agent_launches WHERE launch_id={p} AND team_id={p}",
                (launch_id, team_id),
            ).fetchone()
            return AgentLaunchV1.model_validate_json(row[0]) if row else None

    def reserve(
        self,
        launch: AgentLaunchV1,
        limits: LaunchCapacity,
        *,
        join: bool,
        external_workers: tuple[tuple[str, str], ...] = (),
    ) -> tuple[AgentLaunchV1, bool]:
        """Reserve one worker start, or join a compatible start in progress.

        ``external_workers`` are (agent id, agent type) of running workers this
        process knows that may have no start record (startup agents); they count
        toward the team limit and their type's pool size.
        """
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            self._lock_team(conn, launch.team_id)
            now = database_now(conn)
            launch = launch.model_copy(
                update={
                    "created_at_utc": now,
                    "updated_at_utc": now,
                    "lease_expires_at_utc": now
                    + timedelta(seconds=limits.lease_seconds),
                }
            )
            records = self._list(conn, launch.team_id)
            existing = next(
                (item for item in records if item.launch_id == launch.launch_id), None
            )
            if existing is not None:
                if (
                    existing.named_agent_type,
                    existing.owner_canonical_user_id,
                    existing.source,
                ) != (
                    launch.named_agent_type,
                    launch.owner_canonical_user_id,
                    launch.source,
                ):
                    raise LaunchConflict("launch_identity_conflict")
                conn.commit()
                return existing, False
            active = [item for item in records if item.state in ACTIVE_LAUNCH_STATES]
            compatible = [
                item
                for item in active
                if (
                    item.named_agent_type,
                    item.owner_canonical_user_id,
                    item.definition_digest,
                    item.profile_id,
                )
                == (
                    launch.named_agent_type,
                    launch.owner_canonical_user_id,
                    launch.definition_digest,
                    launch.profile_id,
                )
            ]
            # Join only starts in progress. Ready-worker reuse also needs a live
            # capacity check by the runtime; an SQL row is not liveness evidence.
            # An unknown start is not in progress: joining it tied every later
            # run to a start that may never finish (2026-10-03), so it only
            # holds capacity until its process is proven gone.
            pending = next(
                (item for item in compatible if item.state in _JOINABLE_STATES),
                None,
            )
            if join and pending is not None:
                conn.commit()
                return pending, False
            external_ids = {agent_id for agent_id, _agent_type in external_workers}
            if (
                len({item.agent_id for item in active} | external_ids)
                >= limits.max_total_workers_per_team
            ):
                raise LaunchConflict(TEAM_WORKER_CAPACITY_LIMIT)
            if (
                launch.source in AUTOMATIC_LAUNCH_SOURCES
                and limits.max_workers_of_type is not None
            ):
                of_type = {
                    item.agent_id
                    for item in active
                    if item.named_agent_type == launch.named_agent_type
                } | {
                    agent_id
                    for agent_id, agent_type in external_workers
                    if agent_type == launch.named_agent_type
                }
                if len(of_type) >= limits.max_workers_of_type:
                    raise LaunchConflict(AUTOMATIC_WORKER_CAPACITY_LIMIT)
            cutoff = launch.created_at_utc - timedelta(hours=1)
            if (
                sum(
                    item.named_agent_type == launch.named_agent_type
                    and item.created_at_utc >= cutoff
                    for item in records
                )
                >= limits.max_launches_per_type_per_hour
            ):
                raise LaunchConflict(WORKER_LAUNCH_RATE_LIMIT)
            conn.execute(
                f"INSERT INTO agent_launches(launch_id,agent_id,team_id,named_agent_type,state,fence,created_at,updated_at,payload_json) VALUES ({','.join([p] * 9)})",
                (
                    launch.launch_id,
                    launch.agent_id,
                    launch.team_id,
                    launch.named_agent_type,
                    launch.state.value,
                    launch.fence,
                    launch.created_at_utc.isoformat(),
                    launch.updated_at_utc.isoformat(),
                    launch.model_dump_json(),
                ),
            )
            conn.commit()
            return launch, True

    def transition(
        self,
        record: AgentLaunchV1,
        *,
        expected_fence: int,
        expected_state: AgentLaunchState,
    ) -> AgentLaunchV1:
        p = self.dialect.bind
        if record.fence != expected_fence + 1:
            raise LaunchConflict("launch_fence_must_advance")
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE agent_launches SET state={p},fence={p},updated_at={p},payload_json={p} WHERE launch_id={p} AND team_id={p} AND agent_id={p} AND fence={p} AND state={p}",
                (
                    record.state.value,
                    record.fence,
                    record.updated_at_utc.isoformat(),
                    record.model_dump_json(),
                    record.launch_id,
                    record.team_id,
                    record.agent_id,
                    expected_fence,
                    expected_state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise LaunchConflict("stale_launch_fence")
            conn.commit()
        return record

    def update(
        self,
        record: AgentLaunchV1,
        *,
        state: AgentLaunchState,
        now: datetime | None = None,
        **changes: Any,
    ) -> AgentLaunchV1:
        now = now or datetime.now(timezone.utc)
        replacement = record.model_copy(
            update={
                **changes,
                "state": state,
                "fence": record.fence + 1,
                "updated_at_utc": now,
            }
        )
        return self.transition(
            replacement, expected_fence=record.fence, expected_state=record.state
        )

    def renew(self, record: AgentLaunchV1, *, lease_seconds: float) -> AgentLaunchV1:
        """Renew a live owner without changing the state-transition fence."""
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            suffix = " FOR UPDATE" if self.dialect == POSTGRES else ""
            row = conn.execute(
                f"SELECT payload_json FROM agent_launches WHERE launch_id={p} AND team_id={p}{suffix}",
                (record.launch_id, record.team_id),
            ).fetchone()
            now = database_now(conn)
            current = AgentLaunchV1.model_validate_json(row[0]) if row else None
            if (
                current is None
                or current.fence != record.fence
                or current.state != record.state
                or current.agent_id != record.agent_id
                or current.lease_expires_at_utc <= now
                or current.state
                not in {AgentLaunchState.STARTING, AgentLaunchState.READY}
            ):
                raise LaunchConflict("stale_launch_lease")
            replacement = current.model_copy(
                update={
                    "lease_expires_at_utc": now + timedelta(seconds=lease_seconds),
                    "updated_at_utc": now,
                }
            )
            cursor = conn.execute(
                f"UPDATE agent_launches SET updated_at={p},payload_json={p} WHERE launch_id={p} AND team_id={p} AND fence={p} AND state={p}",
                (
                    now.isoformat(),
                    replacement.model_dump_json(),
                    record.launch_id,
                    record.team_id,
                    record.fence,
                    record.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise LaunchConflict("stale_launch_fence")
            conn.commit()
            return replacement

    def prune(self, team_id: str, *, before: datetime) -> int:
        p = self.dialect.bind
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM agent_launches WHERE team_id={p} AND state IN ('failed','retired') AND updated_at < {p}",
                (team_id, before.isoformat()),
            )
            conn.commit()
            return int(cursor.rowcount)
