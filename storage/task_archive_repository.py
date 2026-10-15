"""Backend-neutral repository boundary for archived task persistence."""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Protocol

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
from .sqlite_connection import connect_sqlite, ensure_sqlite_column


@dataclass(frozen=True)
class ArchivedTaskRecord:
    task_json: str
    status: str
    timestamp: Optional[str]
    owner_agent_id: Optional[str]
    owner_agent_name: Optional[str]
    team_id: Optional[str]
    owner_canonical_user_id: Optional[str]


@dataclass(frozen=True)
class ArchivedTaskListRecord:
    task_id: str
    owner_agent_id: Optional[str]
    owner_agent_name: Optional[str]
    team_id: Optional[str]
    owner_canonical_user_id: Optional[str]
    status: str
    timestamp: Optional[str]
    task_json: str
    conversation_id: Optional[str]
    turn_index: Optional[int]
    followup_to_task_id: Optional[str]


class TaskArchiveRepository(Protocol):
    def list_finished_tasks(
        self, *, team_id: str, owner_canonical_user_id: str, offset: int, limit: int,
    ) -> tuple[list[ArchivedTaskListRecord], int]: ...

    def save_completed_task(
        self,
        *,
        archive_id: str,
        task_id: str,
        owner_agent_id: Optional[str],
        owner_agent_name: Optional[str],
        team_id: Optional[str],
        task_json: str,
        status: str,
        owner_canonical_user_id: Optional[str] = None,
    ) -> None: ...

    def get_archived_task(
        self,
        task_id: str,
        *,
        owner_agent_id: Optional[str],
        include_all_agents: bool,
        owner_canonical_user_id: Optional[str] = None,
    ) -> Optional[ArchivedTaskRecord]: ...

    def get_subtask_task_jsons(self, parent_task_id: str) -> List[str]: ...

    def list_archived_tasks(
        self,
        *,
        limit: int,
        status: Optional[str],
        owner_agent_id: Optional[str],
        include_all_agents: bool,
        owner_canonical_user_id: Optional[str] = None,
    ) -> List[ArchivedTaskListRecord]: ...


class _RelationalTaskArchiveRepository:
    """Shared record operations for archived task persistence."""

    _dialect: SqlDialect

    def list_finished_tasks(
        self, *, team_id: str, owner_canonical_user_id: str, offset: int, limit: int,
    ) -> tuple[list[ArchivedTaskListRecord], int]:
        """Page archived work across this user's team, including stopped agents.

        Do not join conversation links: a task may have multiple links, which
        would duplicate rows and make page counts disagree with displayed work.
        """
        if not team_id or not owner_canonical_user_id:
            raise ValueError("Finished task history requires a team and task owner.")
        bind = self._dialect.bind
        where = f"team_id = {bind} AND owner_canonical_user_id = {bind}"
        params = (team_id, owner_canonical_user_id)
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM completed_tasks WHERE {where}", params).fetchone()[0]
            rows = conn.execute(
                "SELECT task_id, owner_agent_id, owner_agent_name, team_id, owner_canonical_user_id, status, timestamp, task_data "
                f"FROM completed_tasks WHERE {where} ORDER BY timestamp DESC, archive_id DESC LIMIT {bind} OFFSET {bind}",
                (*params, max(1, min(limit, 10)), max(0, offset)),
            ).fetchall()
        return [ArchivedTaskListRecord(*row, None, None, None) for row in rows], total


    def _init_schema(self) -> None:
        backend_name = self._dialect.name
        storage_target = redact_postgres_dsn(self._dsn) if backend_name == "Postgres" else self._db_path
        try:
            ensure_storage_parent_dir(self._db_path)
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS completed_tasks (
                        archive_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        owner_agent_id TEXT,
                        owner_agent_name TEXT,
                        team_id TEXT,
                        owner_canonical_user_id TEXT,
                        task_data TEXT,
                        status TEXT,
                        timestamp TEXT
                    )
                    """
                )
                self._ensure_column(conn, "completed_tasks", "team_id", "TEXT")
                self._ensure_column(conn, "completed_tasks", "owner_canonical_user_id", "TEXT")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_completed_tasks_task_id ON completed_tasks(task_id)")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_completed_tasks_owner_agent ON completed_tasks(owner_agent_id, timestamp DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_completed_tasks_team ON completed_tasks(team_id, timestamp DESC)"
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_completed_tasks_owner_user
                    ON completed_tasks(owner_canonical_user_id, timestamp DESC)
                    """
                )
                conn.commit()
            self._logger.debug(f"{backend_name} task-archive repository initialized at {storage_target}")
        except Exception as exc:
            self._logger.error(f"Failed to initialize {backend_name} task-archive repository: {exc}")
            raise


    def get_archived_task(
        self,
        task_id: str,
        *,
        owner_agent_id: Optional[str],
        include_all_agents: bool,
        owner_canonical_user_id: Optional[str] = None,
    ) -> Optional[ArchivedTaskRecord]:
        with self._connect() as conn:
            select_sql = f"""
                SELECT task_data, status, timestamp, owner_agent_id, owner_agent_name, team_id,
                       owner_canonical_user_id
                FROM completed_tasks
                WHERE task_id = {self._dialect.bind}
            """
            params: list[Any] = [task_id]
            if not include_all_agents and owner_agent_id is not None:
                select_sql += f" AND owner_agent_id = {self._dialect.bind}"
                params.append(owner_agent_id)
            if owner_canonical_user_id is not None:
                select_sql += f" AND owner_canonical_user_id = {self._dialect.bind}"
                params.append(owner_canonical_user_id)
            select_sql += " ORDER BY timestamp DESC LIMIT 1"
            row = conn.execute(select_sql, params).fetchone()
        if not row:
            return None
        return ArchivedTaskRecord(
            task_json=row[0],
            status=row[1],
            timestamp=row[2],
            owner_agent_id=row[3],
            owner_agent_name=row[4],
            team_id=row[5],
            owner_canonical_user_id=row[6],
        )


class SqliteTaskArchiveRepository(_RelationalTaskArchiveRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._init_schema()


    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path, logger=self._logger)


    def save_completed_task(
        self,
        *,
        archive_id: str,
        task_id: str,
        owner_agent_id: Optional[str],
        owner_agent_name: Optional[str],
        team_id: Optional[str],
        task_json: str,
        status: str,
        owner_canonical_user_id: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT OR REPLACE INTO completed_tasks
                    (
                        archive_id, task_id, owner_agent_id, owner_agent_name, team_id,
                        owner_canonical_user_id, task_data, status, timestamp
                    )
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    archive_id,
                    task_id,
                    owner_agent_id,
                    owner_agent_name,
                    team_id,
                    owner_canonical_user_id,
                    task_json,
                    status,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()


    def get_subtask_task_jsons(self, parent_task_id: str) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT task_data
                FROM completed_tasks
                WHERE json_extract(task_data, '$.parent_task_id') = {self._dialect.bind}
                ORDER BY timestamp DESC
                """,
                (parent_task_id,),
            ).fetchall()
        return [row[0] for row in rows if row and row[0]]


    def list_archived_tasks(
        self,
        *,
        limit: int,
        status: Optional[str],
        owner_agent_id: Optional[str],
        include_all_agents: bool,
        owner_canonical_user_id: Optional[str] = None,
    ) -> List[ArchivedTaskListRecord]:
        safe_limit = max(1, min(limit, 100))
        has_conversation_links = False
        try:
            with self._connect() as conn:
                table_row = conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'conversation_task_links'
                    """
                ).fetchone()
                has_conversation_links = table_row is not None
        except Exception:
            has_conversation_links = False

        query = """
            SELECT ct.task_id, ct.owner_agent_id, ct.owner_agent_name, ct.team_id,
                   ct.owner_canonical_user_id,
                   ct.status, ct.timestamp, ct.task_data
        """
        if has_conversation_links:
            query += """
                , ctl.conversation_id, ctl.turn_index, ctl.followup_to_task_id
                FROM completed_tasks ct
                LEFT JOIN conversation_task_links ctl
                    ON ctl.task_id = ct.task_id
            """
        else:
            query += " FROM completed_tasks ct"

        params: List[Any] = []
        where_clauses: List[str] = []
        if status:
            where_clauses.append(f"ct.status = {self._dialect.bind}")
            params.append(status)
        if not include_all_agents and owner_agent_id:
            where_clauses.append(f"ct.owner_agent_id = {self._dialect.bind}")
            params.append(owner_agent_id)
        if owner_canonical_user_id is not None:
            where_clauses.append(f"ct.owner_canonical_user_id = {self._dialect.bind}")
            params.append(owner_canonical_user_id)
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += f" ORDER BY timestamp DESC LIMIT {self._dialect.bind}"
        params.append(safe_limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        archived_tasks: List[ArchivedTaskListRecord] = []
        for row in rows:
            archived_tasks.append(
                ArchivedTaskListRecord(
                    task_id=row[0],
                    owner_agent_id=row[1],
                    owner_agent_name=row[2],
                    team_id=row[3],
                    owner_canonical_user_id=row[4],
                    status=row[5],
                    timestamp=row[6],
                    task_json=row[7],
                    conversation_id=row[8] if has_conversation_links and len(row) > 8 else None,
                    turn_index=row[9] if has_conversation_links and len(row) > 9 else None,
                    followup_to_task_id=row[10] if has_conversation_links and len(row) > 10 else None,
                )
            )
        return archived_tasks


    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
        ensure_sqlite_column(conn, table_name, column_name, column_type, logger=self._logger)


    def _column_names(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f'PRAGMA table_info({table_name})').fetchall()
        return {str(row[1]) for row in rows}


class PostgresTaskArchiveRepository(_RelationalTaskArchiveRepository):
    """Postgres implementation for archived task persistence."""

    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        validate_postgres_connection(self._dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)

    def save_completed_task(
        self,
        *,
        archive_id: str,
        task_id: str,
        owner_agent_id: Optional[str],
        owner_agent_name: Optional[str],
        team_id: Optional[str],
        task_json: str,
        status: str,
        owner_canonical_user_id: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO completed_tasks
                    (
                        archive_id, task_id, owner_agent_id, owner_agent_name, team_id,
                        owner_canonical_user_id, task_data, status, timestamp
                    )
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(archive_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    owner_agent_id = excluded.owner_agent_id,
                    owner_agent_name = excluded.owner_agent_name,
                    team_id = excluded.team_id,
                    owner_canonical_user_id = excluded.owner_canonical_user_id,
                    task_data = excluded.task_data,
                    status = excluded.status,
                    timestamp = excluded.timestamp
                """,
                (
                    archive_id,
                    task_id,
                    owner_agent_id,
                    owner_agent_name,
                    team_id,
                    owner_canonical_user_id,
                    task_json,
                    status,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

    def get_subtask_task_jsons(self, parent_task_id: str) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT task_data
                FROM completed_tasks
                WHERE CAST(task_data AS JSONB) ->> 'parent_task_id' = {self._dialect.bind}
                ORDER BY timestamp DESC
                """,
                (parent_task_id,),
            ).fetchall()
        return [row[0] for row in rows if row and row[0]]

    def list_archived_tasks(
        self,
        *,
        limit: int,
        status: Optional[str],
        owner_agent_id: Optional[str],
        include_all_agents: bool,
        owner_canonical_user_id: Optional[str] = None,
    ) -> List[ArchivedTaskListRecord]:
        safe_limit = max(1, min(limit, 100))
        has_conversation_links = False
        try:
            with self._connect() as conn:
                table_row = conn.execute(
                    "SELECT to_regclass('public.conversation_task_links')"
                ).fetchone()
                has_conversation_links = bool(table_row and table_row[0])
        except Exception:
            has_conversation_links = False

        return self._list_archived_tasks_with_flag(
            has_conversation_links=has_conversation_links,
            limit=safe_limit,
            status=status,
            owner_agent_id=owner_agent_id,
            include_all_agents=include_all_agents,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def _list_archived_tasks_with_flag(
        self,
        *,
        has_conversation_links: bool,
        limit: int,
        status: Optional[str],
        owner_agent_id: Optional[str],
        include_all_agents: bool,
        owner_canonical_user_id: Optional[str] = None,
    ) -> List[ArchivedTaskListRecord]:
        query = """
            SELECT ct.task_id, ct.owner_agent_id, ct.owner_agent_name, ct.team_id,
                   ct.owner_canonical_user_id,
                   ct.status, ct.timestamp, ct.task_data
        """
        if has_conversation_links:
            query += """
                , ctl.conversation_id, ctl.turn_index, ctl.followup_to_task_id
                FROM completed_tasks ct
                LEFT JOIN conversation_task_links ctl
                    ON ctl.task_id = ct.task_id
            """
        else:
            query += " FROM completed_tasks ct"

        params: List[Any] = []
        where_clauses: List[str] = []
        if status:
            where_clauses.append(f"ct.status = {self._dialect.bind}")
            params.append(status)
        if not include_all_agents and owner_agent_id:
            where_clauses.append(f"ct.owner_agent_id = {self._dialect.bind}")
            params.append(owner_agent_id)
        if owner_canonical_user_id is not None:
            where_clauses.append(f"ct.owner_canonical_user_id = {self._dialect.bind}")
            params.append(owner_canonical_user_id)
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += f" ORDER BY timestamp DESC LIMIT {self._dialect.bind}"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        archived_tasks: List[ArchivedTaskListRecord] = []
        for row in rows:
            archived_tasks.append(
                ArchivedTaskListRecord(
                    task_id=row[0],
                    owner_agent_id=row[1],
                    owner_agent_name=row[2],
                    team_id=row[3],
                    owner_canonical_user_id=row[4],
                    status=row[5],
                    timestamp=row[6],
                    task_json=row[7],
                    conversation_id=row[8] if has_conversation_links and len(row) > 8 else None,
                    turn_index=row[9] if has_conversation_links and len(row) > 9 else None,
                    followup_to_task_id=row[10] if has_conversation_links and len(row) > 10 else None,
                )
            )
        return archived_tasks


    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
        conn.execute(f'ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_type}')
        return


    def _column_names(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f'\n                SELECT column_name\n                FROM information_schema.columns\n                WHERE table_name = {self._dialect.bind}\n                ', (table_name,)).fetchall()
        return {str(row[0]) for row in rows}


def create_task_archive_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> TaskArchiveRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="TaskArchiveRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqliteTaskArchiveRepository(storage_settings.sqlite_path, logger=logger)
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresTaskArchiveRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(
        f"TaskArchiveRepository has no implementation for backend {storage_settings.backend!r}."
    )
