"""Pool sizes TeamArchitect set, worker pool problems, and provider-limit events.

``worker_pool_limits``: one row per worker type (and ``*`` for the team) with a
lasting size and an optional temporary size until a time, both set by
TeamArchitect with ``set_worker_pool_limit`` inside the operator ceilings
(``runtime_settings.worker_pools``). Without a row the configured default
applies.

``worker_pool_incidents``: one open incident per worker type and kind
(``capacity``, ``provider_limited``, ``start_failed``) while the condition
lasts; the worker pool controller opens, refreshes and closes them and records
when TeamArchitect was last given a review. Closed incidents stay for the
recurrence window, so a problem that keeps coming back is recognized.

``worker_pool_events``: provider-limit events reported by workers when a model
provider refuses their calls; the controller counts them per type in a window.
"""

from __future__ import annotations

from storage.read_only import repositories_are_read_only

import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite

TEAM_SCOPE = "*"
IncidentKind = Literal["capacity", "provider_limited", "start_failed"]
INCIDENT_CAPACITY: IncidentKind = "capacity"
INCIDENT_PROVIDER_LIMITED: IncidentKind = "provider_limited"
INCIDENT_START_FAILED: IncidentKind = "start_failed"
# The newest changes kept on a limit row, for inspection.
_LIMIT_HISTORY = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkerPoolLimitChangeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at_utc: datetime
    max_workers: int
    until_utc: Optional[datetime] = None
    set_by: str
    reason: str


class WorkerPoolLimitV1(BaseModel):
    """TeamArchitect's sizes for one worker type, or the team (``scope='*'``)."""

    model_config = ConfigDict(extra="forbid")

    team_id: str
    scope: str
    lasting_max_workers: Optional[int] = Field(default=None, ge=0)
    temporary_max_workers: Optional[int] = Field(default=None, ge=0)
    temporary_until_utc: Optional[datetime] = None
    updated_at_utc: datetime
    history: list[WorkerPoolLimitChangeV1] = Field(default_factory=list)

    def temporary_active(self, now: datetime) -> bool:
        return (
            self.temporary_max_workers is not None
            and self.temporary_until_utc is not None
            and self.temporary_until_utc > now
        )


class WorkerPoolIncidentV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    team_id: str
    agent_type: str
    kind: IncidentKind
    opened_at_utc: datetime
    last_seen_at_utc: datetime
    closed_at_utc: Optional[datetime] = None
    reviewed_at_utc: Optional[datetime] = None
    review_count: int = 0
    last_review_task_id: Optional[str] = None
    administrators_told: bool = False
    # The latest evidence, for the review and inspection (no private task text).
    details: dict[str, Any] = Field(default_factory=dict)


class ProviderLimitEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: str
    agent_type: str
    agent_id: str
    task_id: Optional[str] = None
    provider: Optional[str] = None
    model_id: Optional[str] = None
    status_code: Optional[int] = None
    at_utc: datetime


class WorkerPoolRepository:
    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if repositories_are_read_only():
            return
        if not settings.postgres_dsn and not settings.sqlite_path:
            raise ValueError("Worker pool records require configured storage.")
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS worker_pool_limits (
                team_id TEXT NOT NULL, scope TEXT NOT NULL,
                updated_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY (team_id, scope)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS worker_pool_incidents (
                incident_id TEXT PRIMARY KEY, team_id TEXT NOT NULL,
                agent_type TEXT NOT NULL, kind TEXT NOT NULL,
                opened_at TEXT NOT NULL, closed_at TEXT,
                payload_json TEXT NOT NULL
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS worker_pool_incidents_open "
                "ON worker_pool_incidents(team_id, agent_type, kind, closed_at)"
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS worker_pool_events (
                team_id TEXT NOT NULL, agent_type TEXT NOT NULL,
                at TEXT NOT NULL, payload_json TEXT NOT NULL
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS worker_pool_events_time "
                "ON worker_pool_events(team_id, agent_type, at)"
            )
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    # -- limits -----------------------------------------------------------------

    def _read_limit(self, conn: Any, team_id: str, scope: str) -> Optional[WorkerPoolLimitV1]:
        p = self.dialect.bind
        row = conn.execute(
            f"SELECT payload_json FROM worker_pool_limits WHERE team_id={p} AND scope={p}",
            (team_id, scope),
        ).fetchone()
        return WorkerPoolLimitV1.model_validate_json(row[0]) if row else None

    def get_limit(self, team_id: str, scope: str) -> Optional[WorkerPoolLimitV1]:
        with self._connect() as conn:
            return self._read_limit(conn, team_id, scope)

    def list_limits(self, team_id: str) -> tuple[WorkerPoolLimitV1, ...]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM worker_pool_limits WHERE team_id={p} ORDER BY scope",
                (team_id,),
            ).fetchall()
        return tuple(WorkerPoolLimitV1.model_validate_json(row[0]) for row in rows)

    def set_limit(
        self,
        *,
        team_id: str,
        scope: str,
        max_workers: int,
        until_utc: Optional[datetime],
        set_by: str,
        reason: str,
    ) -> WorkerPoolLimitV1:
        """Set a lasting size (``until_utc`` None) or a temporary one until a time."""

        now = _now()
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            current = self._read_limit(conn, team_id, scope) or WorkerPoolLimitV1(
                team_id=team_id, scope=scope, updated_at_utc=now
            )
            change = WorkerPoolLimitChangeV1(
                at_utc=now, max_workers=max_workers, until_utc=until_utc, set_by=set_by, reason=reason
            )
            updates: dict[str, Any] = {
                "updated_at_utc": now,
                "history": [*current.history, change][-_LIMIT_HISTORY:],
            }
            if until_utc is None:
                # A lasting size replaces any temporary one.
                updates.update(
                    lasting_max_workers=max_workers,
                    temporary_max_workers=None,
                    temporary_until_utc=None,
                )
            else:
                updates.update(temporary_max_workers=max_workers, temporary_until_utc=until_utc)
            record = current.model_copy(update=updates)
            conn.execute(
                "INSERT INTO worker_pool_limits(team_id,scope,updated_at,payload_json) "
                f"VALUES ({p},{p},{p},{p}) ON CONFLICT (team_id, scope) DO UPDATE SET "
                "updated_at=excluded.updated_at, payload_json=excluded.payload_json",
                (team_id, scope, now.isoformat(), record.model_dump_json()),
            )
            conn.commit()
            return record

    # -- incidents --------------------------------------------------------------

    def _write_incident(self, conn: Any, incident: WorkerPoolIncidentV1) -> None:
        p = self.dialect.bind
        conn.execute(
            "INSERT INTO worker_pool_incidents(incident_id,team_id,agent_type,kind,opened_at,"
            f"closed_at,payload_json) VALUES ({','.join([p] * 7)}) "
            "ON CONFLICT (incident_id) DO UPDATE SET closed_at=excluded.closed_at, "
            "payload_json=excluded.payload_json",
            (
                incident.incident_id,
                incident.team_id,
                incident.agent_type,
                incident.kind,
                incident.opened_at_utc.isoformat(),
                incident.closed_at_utc.isoformat() if incident.closed_at_utc else None,
                incident.model_dump_json(),
            ),
        )

    def open_incident(self, team_id: str, agent_type: str, kind: IncidentKind) -> Optional[WorkerPoolIncidentV1]:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM worker_pool_incidents WHERE team_id={p} AND "
                f"agent_type={p} AND kind={p} AND closed_at IS NULL "
                "ORDER BY opened_at DESC",
                (team_id, agent_type, kind),
            ).fetchone()
        return WorkerPoolIncidentV1.model_validate_json(row[0]) if row else None

    def list_open_incidents(self, team_id: str) -> tuple[WorkerPoolIncidentV1, ...]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM worker_pool_incidents WHERE team_id={p} "
                "AND closed_at IS NULL ORDER BY opened_at",
                (team_id,),
            ).fetchall()
        return tuple(WorkerPoolIncidentV1.model_validate_json(row[0]) for row in rows)

    def recent_incidents(
        self, team_id: str, *, since: datetime, agent_type: Optional[str] = None
    ) -> tuple[WorkerPoolIncidentV1, ...]:
        """Incidents opened since ``since`` (open or closed), newest first."""

        p = self.dialect.bind
        query = (
            f"SELECT payload_json FROM worker_pool_incidents WHERE team_id={p} AND opened_at>={p}"
        )
        params: list[Any] = [team_id, since.isoformat()]
        if agent_type is not None:
            query += f" AND agent_type={p}"
            params.append(agent_type)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY opened_at DESC", tuple(params)).fetchall()
        return tuple(WorkerPoolIncidentV1.model_validate_json(row[0]) for row in rows)

    def record_incident(
        self,
        *,
        team_id: str,
        agent_type: str,
        kind: IncidentKind,
        details: dict[str, Any],
        now: Optional[datetime] = None,
    ) -> tuple[WorkerPoolIncidentV1, bool]:
        """Open an incident or refresh the open one; True when newly opened."""

        moment = now or _now()
        current = self.open_incident(team_id, agent_type, kind)
        with self._connect() as conn:
            conn.begin_write()
            if current is None:
                incident = WorkerPoolIncidentV1(
                    incident_id=f"pool:{team_id}:{agent_type}:{kind}:{moment.strftime('%Y%m%dT%H%M%S%f')}",
                    team_id=team_id,
                    agent_type=agent_type,
                    kind=kind,
                    opened_at_utc=moment,
                    last_seen_at_utc=moment,
                    details=dict(details),
                )
                created = True
            else:
                incident = current.model_copy(
                    update={"last_seen_at_utc": moment, "details": dict(details)}
                )
                created = False
            self._write_incident(conn, incident)
            conn.commit()
        return incident, created

    def mark_reviewed(
        self, incident: WorkerPoolIncidentV1, *, review_task_id: str, now: Optional[datetime] = None
    ) -> WorkerPoolIncidentV1:
        updated = incident.model_copy(
            update={
                "reviewed_at_utc": now or _now(),
                "review_count": incident.review_count + 1,
                "last_review_task_id": review_task_id,
            }
        )
        with self._connect() as conn:
            conn.begin_write()
            self._write_incident(conn, updated)
            conn.commit()
        return updated

    def mark_administrators_told(self, incident: WorkerPoolIncidentV1) -> WorkerPoolIncidentV1:
        updated = incident.model_copy(update={"administrators_told": True})
        with self._connect() as conn:
            conn.begin_write()
            self._write_incident(conn, updated)
            conn.commit()
        return updated

    def close_incident(self, incident: WorkerPoolIncidentV1, *, now: Optional[datetime] = None) -> WorkerPoolIncidentV1:
        closed = incident.model_copy(update={"closed_at_utc": now or _now()})
        with self._connect() as conn:
            conn.begin_write()
            self._write_incident(conn, closed)
            conn.commit()
        return closed

    # -- provider-limit events ---------------------------------------------------

    def record_provider_limit(self, event: ProviderLimitEventV1) -> None:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO worker_pool_events(team_id,agent_type,at,payload_json) VALUES ({p},{p},{p},{p})",
                (event.team_id, event.agent_type, event.at_utc.isoformat(), event.model_dump_json()),
            )
            conn.commit()

    def provider_limit_events(
        self, team_id: str, *, since: datetime, agent_type: Optional[str] = None
    ) -> tuple[ProviderLimitEventV1, ...]:
        p = self.dialect.bind
        query = f"SELECT payload_json FROM worker_pool_events WHERE team_id={p} AND at>={p}"
        params: list[Any] = [team_id, since.isoformat()]
        if agent_type is not None:
            query += f" AND agent_type={p}"
            params.append(agent_type)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY at", tuple(params)).fetchall()
        return tuple(ProviderLimitEventV1.model_validate_json(row[0]) for row in rows)

    def prune(self, team_id: str, *, before: datetime) -> None:
        """Drop closed incidents and events older than ``before``."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.execute(
                f"DELETE FROM worker_pool_incidents WHERE team_id={p} AND closed_at IS NOT NULL "
                f"AND closed_at<{p}",
                (team_id, before.isoformat()),
            )
            conn.execute(
                f"DELETE FROM worker_pool_events WHERE team_id={p} AND at<{p}",
                (team_id, before.isoformat()),
            )
            conn.commit()


__all__ = [
    "INCIDENT_CAPACITY",
    "INCIDENT_PROVIDER_LIMITED",
    "INCIDENT_START_FAILED",
    "IncidentKind",
    "ProviderLimitEventV1",
    "TEAM_SCOPE",
    "WorkerPoolIncidentV1",
    "WorkerPoolLimitChangeV1",
    "WorkerPoolLimitV1",
    "WorkerPoolRepository",
]
