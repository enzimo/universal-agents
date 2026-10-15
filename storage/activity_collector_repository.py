"""Backend-neutral repository boundary for curated TaskActivityEvent storage."""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Protocol

from .backends import STORAGE_BACKEND_POSTGRES

from custom_messages.contracts.activity_v1 import TaskActivityEvent
from default_values import STORAGE_DEFAULTS

from .config import StorageSettings
from .postgres_connection import (
    PostgresConnection,
    connect_postgres,
    ensure_storage_parent_dir,
    redact_postgres_dsn,
    validate_postgres_connection,
)
from .runtime import describe_storage_capabilities, require_repository_backend
from .sqlite_connection import connect_sqlite, ensure_sqlite_column, sqlite_column_names


_ACTIVITY_EVENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("activity_event_id", "TEXT"),
    ("created_at", "TEXT"),
    ("sequence", "INTEGER"),
    ("team_id", "TEXT"),
    ("manager_agent_id", "TEXT"),
    ("manager_name", "TEXT"),
    ("top_level_task_id", "TEXT"),
    ("task_id", "TEXT"),
    ("parent_task_id", "TEXT"),
    ("objective_id", "TEXT"),
    ("objective_title", "TEXT"),
    ("correlation_id", "TEXT"),
    ("conversation_id", "TEXT"),
    ("session_key", "TEXT"),
    ("agent_id", "TEXT"),
    ("agent_name", "TEXT"),
    ("named_agent_type", "TEXT"),
    ("activity_type", "TEXT"),
    ("stage", "TEXT"),
    ("summary", "TEXT"),
    ("detail", "TEXT"),
    ("user_visible", "INTEGER"),
    ("trace_id", "TEXT"),
    ("span_id", "TEXT"),
    ("details_json", "TEXT"),
    ("full_event_json", "TEXT"),
)


@dataclass(frozen=True)
class ActivityCollectorHealth:
    total_events: int
    distinct_teams: int
    latest_created_at: Optional[str]


class ActivityCollectorRepository(Protocol):
    def upsert_events(self, events: List[TaskActivityEvent]) -> int: ...

    def list_recent_events(
        self,
        *,
        limit: int = 200,
        team_id: Optional[str] = None,
        user_visible: Optional[bool] = None,
        top_level_task_id: Optional[str] = None,
        objective_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[TaskActivityEvent]: ...

    def get_health(self) -> ActivityCollectorHealth: ...


class _RelationalActivityCollectorRepository:
    _dialect: SqlDialect


    def list_recent_events(
        self,
        *,
        limit: int = 200,
        team_id: Optional[str] = None,
        user_visible: Optional[bool] = None,
        top_level_task_id: Optional[str] = None,
        objective_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[TaskActivityEvent]:
        query_parts = ["SELECT full_event_json FROM activity_events WHERE 1=1"]
        params: list[object] = []
        if team_id:
            query_parts.append(f"AND team_id = {self._dialect.bind}")
            params.append(team_id)
        if user_visible is not None:
            query_parts.append(f"AND user_visible = {self._dialect.bind}")
            params.append(1 if user_visible else 0)
        if top_level_task_id:
            query_parts.append(f"AND top_level_task_id = {self._dialect.bind}")
            params.append(top_level_task_id)
        if objective_id:
            query_parts.append(f"AND objective_id = {self._dialect.bind}")
            params.append(objective_id)
        if agent_id:
            query_parts.append(f"AND agent_id = {self._dialect.bind}")
            params.append(agent_id)
        query_parts.append(f"ORDER BY created_at DESC, sequence DESC LIMIT {self._dialect.bind}")
        params.append(max(1, int(limit)))
        try:
            with self._connect() as conn:
                rows = conn.execute(" ".join(query_parts), params).fetchall()
            return [TaskActivityEvent.model_validate_json(row[0]) for row in rows if row[0]]
        except Exception as exc:
            self._logger.error("Failed to query activity events: %s", exc)
            return []

    def get_health(self) -> ActivityCollectorHealth:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total_events,
                           COUNT(DISTINCT COALESCE(team_id, '')) AS distinct_teams,
                           MAX(created_at) AS latest_created_at
                    FROM activity_events
                    """
                ).fetchone()
            return ActivityCollectorHealth(
                total_events=int(row[0] or 0),
                distinct_teams=int(row[1] or 0),
                latest_created_at=row[2],
            )
        except Exception as exc:
            self._logger.error("Failed to read collector health: %s", exc)
            return ActivityCollectorHealth(total_events=0, distinct_teams=0, latest_created_at=None)


class SqliteActivityCollectorRepository(_RelationalActivityCollectorRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None) -> None:
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._init_schema()


    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path, logger=self._logger)


    def _ensure_activity_event_columns(self, conn: sqlite3.Connection) -> None:
        columns = sqlite_column_names(conn, "activity_events")
        for column_name, column_type in _ACTIVITY_EVENT_COLUMNS:
            if column_name not in columns:
                ensure_sqlite_column(conn, "activity_events", column_name, column_type, logger=self._logger)


    def _init_schema(self) -> None:
        try:
            ensure_storage_parent_dir(self._db_path)
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS activity_events (
                        activity_event_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        team_id TEXT,
                        manager_agent_id TEXT,
                        manager_name TEXT,
                        top_level_task_id TEXT,
                        task_id TEXT,
                        parent_task_id TEXT,
                        objective_id TEXT,
                        objective_title TEXT,
                        correlation_id TEXT,
                        conversation_id TEXT,
                        session_key TEXT,
                        agent_id TEXT,
                        agent_name TEXT,
                        named_agent_type TEXT,
                        activity_type TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        detail TEXT,
                        user_visible INTEGER NOT NULL,
                        trace_id TEXT,
                        span_id TEXT,
                        details_json TEXT NOT NULL,
                        full_event_json TEXT NOT NULL
                    )
                    """
                )
                self._ensure_activity_event_columns(conn)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_events_created_at ON activity_events(created_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_events_team_id ON activity_events(team_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_events_top_level_task_id ON activity_events(top_level_task_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_events_objective_id ON activity_events(objective_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_events_agent_id ON activity_events(agent_id)"
                )
                conn.commit()
        except Exception as exc:
            self._logger.error("Failed to initialize SQLite activity collector repository: %s", exc)
            raise


    def upsert_events(self, events: List[TaskActivityEvent]) -> int:
        if not events:
            return 0
        inserted = 0
        try:
            with self._connect() as conn:
                for event in events:
                    cursor = conn.execute(
                        f"""
                        INSERT OR IGNORE INTO activity_events (
                            activity_event_id, created_at, sequence, team_id, manager_agent_id, manager_name,
                            top_level_task_id, task_id, parent_task_id, objective_id, objective_title,
                            correlation_id, conversation_id,
                            session_key, agent_id, agent_name, named_agent_type, activity_type, stage,
                            summary, detail, user_visible, trace_id, span_id, details_json, full_event_json
                        ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                        """,
                        (
                            event.activity_event_id,
                            event.created_at,
                            event.sequence,
                            event.team_id,
                            event.manager_agent_id,
                            event.manager_name,
                            event.top_level_task_id,
                            event.task_id,
                            event.parent_task_id,
                            event.objective_id,
                            event.objective_title,
                            event.correlation_id,
                            event.conversation_id,
                            event.session_key,
                            event.agent_id,
                            event.agent_name,
                            event.named_agent_type,
                            event.activity_type.value,
                            event.stage.value,
                            event.summary,
                            event.detail,
                            1 if event.user_visible else 0,
                            event.trace_id,
                            event.span_id,
                            json.dumps(event.details or {}),
                            event.model_dump_json(),
                        ),
                    )
                    if cursor.rowcount > 0:
                        inserted += 1
                conn.commit()
        except Exception as exc:
            self._logger.error("Failed to persist activity events: %s", exc)
            raise
        return inserted


class PostgresActivityCollectorRepository(_RelationalActivityCollectorRepository):
    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None) -> None:
        self._dsn = dsn
        self._logger = logger or logging.getLogger(__name__)
        validate_postgres_connection(dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)

    def _ensure_activity_event_columns(self, conn: PostgresConnection) -> None:
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'activity_events'
            """
        ).fetchall()
        columns = {str(row[0]) for row in rows}
        for column_name, column_type in _ACTIVITY_EVENT_COLUMNS:
            if column_name not in columns:
                conn.execute(f"ALTER TABLE activity_events ADD COLUMN {column_name} {column_type}")

    def _init_schema(self) -> None:
        storage_target = redact_postgres_dsn(self._dsn)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS activity_events (
                        activity_event_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        team_id TEXT,
                        manager_agent_id TEXT,
                        manager_name TEXT,
                        top_level_task_id TEXT,
                        task_id TEXT,
                        parent_task_id TEXT,
                        objective_id TEXT,
                        objective_title TEXT,
                        correlation_id TEXT,
                        conversation_id TEXT,
                        session_key TEXT,
                        agent_id TEXT,
                        agent_name TEXT,
                        named_agent_type TEXT,
                        activity_type TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        detail TEXT,
                        user_visible INTEGER NOT NULL,
                        trace_id TEXT,
                        span_id TEXT,
                        details_json TEXT NOT NULL,
                        full_event_json TEXT NOT NULL
                    )
                    """
                )
                self._ensure_activity_event_columns(conn)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_events_created_at ON activity_events(created_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_events_team_id ON activity_events(team_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_events_top_level_task_id ON activity_events(top_level_task_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_events_objective_id ON activity_events(objective_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_events_agent_id ON activity_events(agent_id)"
                )
                conn.commit()
            self._logger.info("Postgres activity collector repository initialized at %s", storage_target)
        except Exception as exc:
            self._logger.error("Failed to initialize Postgres activity collector repository: %s", exc)
            raise

    def upsert_events(self, events: List[TaskActivityEvent]) -> int:
        if not events:
            return 0
        inserted = 0
        try:
            with self._connect() as conn:
                for event in events:
                    cursor = conn.execute(
                        f"""
                        INSERT INTO activity_events (
                            activity_event_id, created_at, sequence, team_id, manager_agent_id, manager_name,
                            top_level_task_id, task_id, parent_task_id, objective_id, objective_title,
                            correlation_id, conversation_id,
                            session_key, agent_id, agent_name, named_agent_type, activity_type, stage,
                            summary, detail, user_visible, trace_id, span_id, details_json, full_event_json
                        ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                        ON CONFLICT (activity_event_id) DO NOTHING
                        """,
                        (
                            event.activity_event_id,
                            event.created_at,
                            event.sequence,
                            event.team_id,
                            event.manager_agent_id,
                            event.manager_name,
                            event.top_level_task_id,
                            event.task_id,
                            event.parent_task_id,
                            event.objective_id,
                            event.objective_title,
                            event.correlation_id,
                            event.conversation_id,
                            event.session_key,
                            event.agent_id,
                            event.agent_name,
                            event.named_agent_type,
                            event.activity_type.value,
                            event.stage.value,
                            event.summary,
                            event.detail,
                            1 if event.user_visible else 0,
                            event.trace_id,
                            event.span_id,
                            json.dumps(event.details or {}),
                            event.model_dump_json(),
                        ),
                    )
                    if cursor.rowcount > 0:
                        inserted += 1
                conn.commit()
        except Exception as exc:
            self._logger.error("Failed to persist activity events: %s", exc)
            raise
        return inserted


def create_activity_collector_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> ActivityCollectorRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="ActivityCollectorRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES:
        return PostgresActivityCollectorRepository(storage_settings.postgres_dsn or "", logger=logger)
    return SqliteActivityCollectorRepository(
        storage_settings.sqlite_path or STORAGE_DEFAULTS.activity_collector_db_path.value,
        logger=logger,
    )
