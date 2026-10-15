"""Backend-neutral repository boundary for canonical heartbeat persistence."""

from __future__ import annotations

from storage.read_only import repositories_are_read_only

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import logging
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Protocol

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE

from .config import StorageSettings
from .postgres_connection import (
    PostgresConnection,
    connect_postgres,
    ensure_storage_parent_dir,
    redact_postgres_dsn,
    validate_postgres_connection,
)
from .runtime import describe_storage_capabilities, require_repository_backend
from .sqlite_connection import connect_sqlite


@dataclass
class HeartbeatSourceRecord:
    source_id: str
    title: str
    enabled: bool
    created_at: str
    updated_at: str


@dataclass
class HeartbeatItemRecord:
    heartbeat_item_id: str
    source_id: str
    title: str
    instruction: str
    conversation_id: str
    target_agent_type: Optional[str]
    objective_id: Optional[str]
    enabled: bool
    created_at: str
    updated_at: str
    last_run_at: Optional[str]
    last_success_at: Optional[str]
    last_failure_at: Optional[str]
    source_key: Optional[str] = None


class HeartbeatRepository(Protocol):
    def get_source(self, source_id: str) -> Optional[HeartbeatSourceRecord]: ...

    def upsert_source(
        self,
        *,
        source_id: str,
        title: str,
        enabled: bool,
        created_at: str,
        updated_at: str,
    ) -> None: ...

    def list_items(self, source_id: str, *, enabled_only: bool = False) -> list[HeartbeatItemRecord]: ...

    def get_item(self, heartbeat_item_id: str) -> Optional[HeartbeatItemRecord]: ...

    def get_item_by_source_key(self, source_id: str, source_key: str) -> Optional[HeartbeatItemRecord]: ...

    def add_item(
        self,
        *,
        heartbeat_item_id: str,
        source_id: str,
        title: str,
        instruction: str,
        conversation_id: str,
        target_agent_type: Optional[str],
        objective_id: Optional[str],
        enabled: bool,
        created_at: str,
        updated_at: str,
        source_key: Optional[str] = None,
    ) -> None: ...

    def update_item(
        self,
        heartbeat_item_id: str,
        *,
        title: str,
        instruction: str,
        target_agent_type: Optional[str],
        objective_id: Optional[str],
        updated_at: str,
    ) -> bool: ...

    def set_item_enabled(self, heartbeat_item_id: str, enabled: bool, *, updated_at: str) -> bool: ...

    def set_item_conversation(self, heartbeat_item_id: str, conversation_id: str, *, updated_at: str) -> bool: ...

    def remove_item(self, heartbeat_item_id: str) -> bool: ...

    def mark_item_run_started(self, heartbeat_item_id: str, *, timestamp: str) -> None: ...

    def mark_item_run_finished(self, heartbeat_item_id: str, *, success: bool, timestamp: str) -> None: ...


class _RelationalHeartbeatRepository(ABC):
    """Shared record operations for canonical heartbeat persistence."""

    _dialect: SqlDialect
    _db_path: str
    _dsn: str
    _logger: logging.Logger

    @abstractmethod
    def _connect(self) -> sqlite3.Connection | PostgresConnection:
        """Open the backend's connection for the shared record operations."""
        raise NotImplementedError

    def _init_schema(self) -> None:
        if repositories_are_read_only():
            return
        backend_name = self._dialect.name
        storage_target = redact_postgres_dsn(self._dsn) if backend_name == "Postgres" else self._db_path
        try:
            if backend_name != "Postgres":
                ensure_storage_parent_dir(self._db_path)
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS heartbeat_sources (
                        source_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS heartbeat_items (
                        heartbeat_item_id TEXT PRIMARY KEY,
                        source_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        instruction TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        target_agent_type TEXT,
                        objective_id TEXT,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_run_at TEXT,
                        last_success_at TEXT,
                        last_failure_at TEXT,
                        source_key TEXT,
                        FOREIGN KEY (source_id) REFERENCES heartbeat_sources(source_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_heartbeat_items_source ON heartbeat_items(source_id, enabled, updated_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_heartbeat_items_objective_id ON heartbeat_items(objective_id)"
                )
                if backend_name == "Postgres":
                    conn.execute("ALTER TABLE heartbeat_items ADD COLUMN IF NOT EXISTS source_key TEXT")
                else:
                    columns = {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(heartbeat_items)").fetchall()
                        if row and len(row) > 1
                    }
                    if "source_key" not in columns:
                        conn.execute("ALTER TABLE heartbeat_items ADD COLUMN source_key TEXT")
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_heartbeat_items_source_key
                    ON heartbeat_items(source_id, source_key)
                    WHERE source_key IS NOT NULL AND source_key != ''
                    """
                )
                if backend_name == "Postgres":
                    column_row = conn.execute(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'heartbeat_items'
                          AND column_name = 'objective_id'
                        """
                    ).fetchone()
                    if not column_row:
                        conn.execute("ALTER TABLE heartbeat_items ADD COLUMN objective_id TEXT")
                else:
                    columns = {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(heartbeat_items)").fetchall()
                        if row and len(row) > 1
                    }
                    if "objective_id" not in columns:
                        conn.execute("ALTER TABLE heartbeat_items ADD COLUMN objective_id TEXT")
                conn.commit()
            self._logger.info(f"{backend_name} heartbeat repository initialized at {storage_target}")
        except Exception as exc:
            self._logger.error(f"Failed to initialize {backend_name} heartbeat repository: {exc}")
            raise

    def get_source(self, source_id: str) -> Optional[HeartbeatSourceRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT source_id, title, enabled, created_at, updated_at
                FROM heartbeat_sources
                WHERE source_id = {self._dialect.bind}
                """,
                (source_id,),
            ).fetchone()
        if not row:
            return None
        return HeartbeatSourceRecord(
            source_id=row["source_id"],
            title=row["title"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def upsert_source(
        self,
        *,
        source_id: str,
        title: str,
        enabled: bool,
        created_at: str,
        updated_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO heartbeat_sources (source_id, title, enabled, created_at, updated_at)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(source_id) DO UPDATE SET
                    title = excluded.title,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (source_id, title, 1 if enabled else 0, created_at, updated_at),
            )
            conn.commit()

    def list_items(self, source_id: str, *, enabled_only: bool = False) -> list[HeartbeatItemRecord]:
        query = f"""
            SELECT heartbeat_item_id, source_id, title, instruction, conversation_id, target_agent_type,
                   objective_id,
                   enabled, created_at, updated_at, last_run_at, last_success_at, last_failure_at, source_key
            FROM heartbeat_items
            WHERE source_id = {self._dialect.bind}
        """
        params: list[object] = [source_id]
        if enabled_only:
            query += " AND enabled = 1"
        query += " ORDER BY updated_at DESC, heartbeat_item_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [
            HeartbeatItemRecord(
                heartbeat_item_id=row["heartbeat_item_id"],
                source_id=row["source_id"],
                title=row["title"],
                instruction=row["instruction"],
                conversation_id=row["conversation_id"],
                target_agent_type=row["target_agent_type"],
                objective_id=row["objective_id"],
                enabled=bool(row["enabled"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                last_run_at=row["last_run_at"],
                last_success_at=row["last_success_at"],
                last_failure_at=row["last_failure_at"],
                source_key=row["source_key"],
            )
            for row in rows
        ]

    def get_item(self, heartbeat_item_id: str) -> Optional[HeartbeatItemRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT heartbeat_item_id, source_id, title, instruction, conversation_id, target_agent_type,
                       objective_id,
                       enabled, created_at, updated_at, last_run_at, last_success_at, last_failure_at, source_key
                FROM heartbeat_items
                WHERE heartbeat_item_id = {self._dialect.bind}
                """,
                (heartbeat_item_id,),
            ).fetchone()
        if not row:
            return None
        return HeartbeatItemRecord(
            heartbeat_item_id=row["heartbeat_item_id"],
            source_id=row["source_id"],
            title=row["title"],
            instruction=row["instruction"],
            conversation_id=row["conversation_id"],
            target_agent_type=row["target_agent_type"],
            objective_id=row["objective_id"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_run_at=row["last_run_at"],
            last_success_at=row["last_success_at"],
            last_failure_at=row["last_failure_at"],
            source_key=row["source_key"],
        )

    def get_item_by_source_key(self, source_id: str, source_key: str) -> Optional[HeartbeatItemRecord]:
        normalized = str(source_key or "").strip()
        if not normalized:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT heartbeat_item_id
                FROM heartbeat_items
                WHERE source_id = {self._dialect.bind} AND source_key = {self._dialect.bind}
                """,
                (source_id, normalized),
            ).fetchone()
        if row is None:
            return None
        return self.get_item(str(row["heartbeat_item_id"]))

    def add_item(
        self,
        *,
        heartbeat_item_id: str,
        source_id: str,
        title: str,
        instruction: str,
        conversation_id: str,
        target_agent_type: Optional[str],
        objective_id: Optional[str],
        enabled: bool,
        created_at: str,
        updated_at: str,
        source_key: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO heartbeat_items
                (heartbeat_item_id, source_id, title, instruction, conversation_id, target_agent_type, objective_id,
                 enabled, created_at, updated_at, last_run_at, last_success_at, last_failure_at, source_key)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, NULL, NULL, {self._dialect.bind})
                """,
                (
                    heartbeat_item_id,
                    source_id,
                    title,
                    instruction,
                    conversation_id,
                    target_agent_type,
                    objective_id,
                    1 if enabled else 0,
                    created_at,
                    updated_at,
                    str(source_key or "").strip() or None,
                ),
            )
            conn.commit()

    def update_item(
        self,
        heartbeat_item_id: str,
        *,
        title: str,
        instruction: str,
        target_agent_type: Optional[str],
        objective_id: Optional[str],
        updated_at: str,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE heartbeat_items
                SET title = {self._dialect.bind}, instruction = {self._dialect.bind}, target_agent_type = {self._dialect.bind}, objective_id = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE heartbeat_item_id = {self._dialect.bind}
                """,
                (title, instruction, target_agent_type, objective_id, updated_at, heartbeat_item_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def set_item_enabled(self, heartbeat_item_id: str, enabled: bool, *, updated_at: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE heartbeat_items
                SET enabled = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE heartbeat_item_id = {self._dialect.bind}
                """,
                (1 if enabled else 0, updated_at, heartbeat_item_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def set_item_conversation(self, heartbeat_item_id: str, conversation_id: str, *, updated_at: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE heartbeat_items
                SET conversation_id = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE heartbeat_item_id = {self._dialect.bind}
                """,
                (conversation_id, updated_at, heartbeat_item_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def remove_item(self, heartbeat_item_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM heartbeat_items WHERE heartbeat_item_id = {self._dialect.bind}",
                (heartbeat_item_id,),
            )
            conn.commit()
        return cursor.rowcount > 0

    def mark_item_run_started(self, heartbeat_item_id: str, *, timestamp: str) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE heartbeat_items
                SET last_run_at = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE heartbeat_item_id = {self._dialect.bind}
                """,
                (timestamp, timestamp, heartbeat_item_id),
            )
            conn.commit()

    def mark_item_run_finished(self, heartbeat_item_id: str, *, success: bool, timestamp: str) -> None:
        column = "last_success_at" if success else "last_failure_at"
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE heartbeat_items
                SET {column} = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE heartbeat_item_id = {self._dialect.bind}
                """,
                (timestamp, timestamp, heartbeat_item_id),
            )
            conn.commit()


class SqliteHeartbeatRepository(_RelationalHeartbeatRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._init_schema()


    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path, logger=self._logger, row_factory=sqlite3.Row)


class PostgresHeartbeatRepository(_RelationalHeartbeatRepository):
    """Postgres implementation for canonical heartbeat persistence."""

    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        validate_postgres_connection(self._dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)


def create_heartbeat_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> HeartbeatRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="HeartbeatRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqliteHeartbeatRepository(storage_settings.sqlite_path, logger=logger)
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresHeartbeatRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(
        f"HeartbeatRepository has no implementation for backend {storage_settings.backend!r}."
    )
