"""Backend-neutral repository boundary for agent state persistence."""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE

from custom_messages.contracts.control_v1 import AgentStatus

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
class AgentStateQuery:
    filters: Dict[str, Any]


class AgentStateRepository(Protocol):
    def upsert(self, status: AgentStatus) -> None: ...

    def upsert_if_newer(self, status: AgentStatus) -> bool: ...

    def remove(self, agent_id: str) -> bool: ...

    def remove_unseen(self, agent_id: str, last_seen_at: Optional[str]) -> bool: ...

    def get_by_id(self, agent_id: str) -> Optional[AgentStatus]: ...

    def get_all(self) -> List[AgentStatus]: ...

    def query(self, filters: Dict[str, Any]) -> List[AgentStatus]: ...


class _RelationalAgentStateRepository:
    """Shared record operations of the agent-state repository."""

    _dialect: SqlDialect
    _logger: logging.Logger

    def _connect(self) -> Any:
        """Open a connection (each backend overrides this)."""
        raise NotImplementedError


    def _init_schema(self) -> None:
        backend_name = self._dialect.name
        storage_target = redact_postgres_dsn(self._dsn) if backend_name == "Postgres" else self._db_path
        try:
            ensure_storage_parent_dir(self._db_path)
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_states (
                        agent_id TEXT PRIMARY KEY,
                        agent_name TEXT,
                        named_agent_type TEXT,
                        team_id TEXT,
                        is_busy INTEGER,
                        is_executing INTEGER,
                        has_execution_capacity INTEGER,
                        is_paused INTEGER,
                        is_team_lead INTEGER,
                        top_level_queue_depth INTEGER,
                        waiting_top_level_task_count INTEGER,
                        deferred_message_count INTEGER,
                        agent_role TEXT,
                        tools_json TEXT,
                        diagnostic_json TEXT,
                        last_seen_at TEXT,
                        is_exiting INTEGER,
                        full_status_json TEXT NOT NULL
                    )
                    """
                )
                self._ensure_column(conn, "agent_states", "team_id", "TEXT")
                for column_name, column_type in (
                    ("is_executing", "INTEGER"),
                    ("has_execution_capacity", "INTEGER"),
                    ("is_paused", "INTEGER"),
                    ("is_team_lead", "INTEGER"),
                    ("top_level_queue_depth", "INTEGER"),
                    ("waiting_top_level_task_count", "INTEGER"),
                    ("deferred_message_count", "INTEGER"),
                    ("agent_role", "TEXT"),
                    ("tools_json", "TEXT"),
                    ("diagnostic_json", "TEXT"),
                    ("last_seen_at", "TEXT"),
                    ("is_exiting", "INTEGER"),
                ):
                    self._ensure_column(conn, "agent_states", column_name, column_type)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_name ON agent_states(agent_name)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_named_agent_type ON agent_states(named_agent_type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_team_id ON agent_states(team_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_states_is_executing ON agent_states(is_executing)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_states_has_capacity ON agent_states(has_execution_capacity)")
                conn.commit()
            self._logger.info(f"{backend_name} agent-state repository initialized at {storage_target}")
        except Exception as exc:
            self._logger.error(f"Failed to initialize {backend_name} agent-state repository: {exc}")
            raise


    def upsert(self, status: AgentStatus) -> None:
        """Write an agent's own status (its own refresh is always current)."""
        self._write_status(status, only_if_newer=False)

    def upsert_if_newer(self, status: AgentStatus) -> bool:
        """Write a status another agent sent only if it is newer than the stored row.

        The team lead stores received presence this way, in one statement, so
        a delayed older status cannot overwrite what the agent wrote itself
        (such as its exiting mark), however the two writes interleave.
        """
        return self._write_status(status, only_if_newer=True)

    def _write_status(self, status: AgentStatus, *, only_if_newer: bool) -> bool:
        agent_id = status.self_id
        if not agent_id:
            self._logger.warning("Cannot upsert AgentStatus without self_id.")
            return False
        # One fixed-width UTC form, so comparing the stored text compares the times.
        status = status.model_copy(update={"last_seen_at": _sortable_time(status.last_seen_at)})
        is_executing = status.is_executing if status.is_executing is not None else status.is_busy
        has_capacity = None if status.has_execution_capacity is None else int(bool(status.has_execution_capacity))
        bind = self._dialect.bind
        # Postgres compares text by the database collation; "C" compares bytes like SQLite.
        collate = ' COLLATE "C"' if self._dialect == POSTGRES else ""
        newer_only = (
            f" WHERE agent_states.last_seen_at IS NULL OR agent_states.last_seen_at{collate}"
            f" < excluded.last_seen_at{collate}"
            if only_if_newer
            else ""
        )
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"""
                    INSERT INTO agent_states
                    (
                        agent_id, agent_name, named_agent_type, team_id, is_busy,
                        is_executing, has_execution_capacity, is_paused, is_team_lead,
                        top_level_queue_depth, waiting_top_level_task_count, deferred_message_count,
                        agent_role, tools_json, diagnostic_json, last_seen_at, is_exiting, full_status_json
                    )
                    VALUES ({", ".join([bind] * 18)})
                    ON CONFLICT(agent_id) DO UPDATE SET
                        agent_name = excluded.agent_name,
                        named_agent_type = excluded.named_agent_type,
                        team_id = excluded.team_id,
                        is_busy = excluded.is_busy,
                        is_executing = excluded.is_executing,
                        has_execution_capacity = excluded.has_execution_capacity,
                        is_paused = excluded.is_paused,
                        is_team_lead = excluded.is_team_lead,
                        top_level_queue_depth = excluded.top_level_queue_depth,
                        waiting_top_level_task_count = excluded.waiting_top_level_task_count,
                        deferred_message_count = excluded.deferred_message_count,
                        agent_role = excluded.agent_role,
                        tools_json = excluded.tools_json,
                        diagnostic_json = excluded.diagnostic_json,
                        last_seen_at = excluded.last_seen_at,
                        is_exiting = excluded.is_exiting,
                        full_status_json = excluded.full_status_json{newer_only}
                    """,
                    (
                        agent_id,
                        status.name,
                        status.named_agent_type,
                        status.team_id,
                        int(bool(is_executing)),
                        int(bool(is_executing)),
                        has_capacity,
                        int(bool(status.is_paused)),
                        int(bool(status.is_team_lead)),
                        status.top_level_queue_depth,
                        status.waiting_top_level_task_count,
                        status.deferred_message_count,
                        status.agent_role,
                        None if status.tools is None else json.dumps(status.tools),
                        None if status.diagnostic is None else json.dumps(status.diagnostic),
                        status.last_seen_at,
                        int(bool(status.is_exiting)),
                        status.model_dump_json(),
                    ),
                )
                conn.commit()
                return bool(cursor.rowcount)
        except Exception as exc:
            self._logger.error(f"Failed to upsert agent state {agent_id}: {exc}")
            return False

    def remove(self, agent_id: str) -> bool:
        try:
            with self._connect() as conn:
                cursor = conn.execute(f"DELETE FROM agent_states WHERE agent_id = {self._dialect.bind}", (agent_id,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as exc:
            self._logger.error(f"Failed to remove agent state {agent_id}: {exc}")
            return False

    def remove_unseen(self, agent_id: str, last_seen_at: Optional[str]) -> bool:
        """Delete one agent's row only if it still holds the heartbeat the caller read.

        The team lead prunes rows of agents that are gone
        (``agent_liveness.prune_departed_agent_rows``). An agent that reported in
        between that read and this delete keeps its row.
        """
        bind = self._dialect.bind
        params: tuple[str, ...]
        if last_seen_at is None:
            statement, params = f"DELETE FROM agent_states WHERE agent_id = {bind} AND last_seen_at IS NULL", (agent_id,)
        else:
            statement, params = (
                f"DELETE FROM agent_states WHERE agent_id = {bind} AND last_seen_at = {bind}",
                (agent_id, last_seen_at),
            )
        try:
            with self._connect() as conn:
                cursor = conn.execute(statement, params)
                conn.commit()
                return cursor.rowcount > 0
        except Exception:
            self._logger.exception(
                "Could not remove an agent status row. event=agent_status_row_remove_failed agent_id=%s",
                agent_id,
            )
            return False

    def get_by_id(self, agent_id: str) -> Optional[AgentStatus]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"SELECT full_status_json FROM agent_states WHERE agent_id = {self._dialect.bind}",
                    (agent_id,),
                ).fetchone()
                if row:
                    return AgentStatus.model_validate_json(row[0])
                return None
        except Exception as exc:
            self._logger.error(f"Failed to get agent state ID {agent_id}: {exc}")
            return None

    def get_all(self) -> List[AgentStatus]:
        try:
            agents: List[AgentStatus] = []
            with self._connect() as conn:
                rows = conn.execute("SELECT full_status_json FROM agent_states").fetchall()
                for row in rows:
                    if row[0]:
                        try:
                            agents.append(AgentStatus.model_validate_json(row[0]))
                        except Exception as parse_exc:
                            self._logger.warning(f"Error parsing AgentStatus json: {parse_exc}")
            return agents
        except Exception as exc:
            self._logger.error(f"Failed to get all agent states: {exc}")
            return []

    def query(self, filters: Dict[str, Any]) -> List[AgentStatus]:
        query_parts = ["SELECT full_status_json FROM agent_states WHERE 1=1"]
        params = []

        if "agent_name" in filters and filters["agent_name"]:
            query_parts.append(f"AND agent_name LIKE {self._dialect.bind}")
            params.append(f"%{filters['agent_name']}%")

        if "named_agent_type" in filters and filters["named_agent_type"]:
            query_parts.append(f"AND named_agent_type = {self._dialect.bind}")
            params.append(filters["named_agent_type"])

        if "team_id" in filters and filters["team_id"]:
            query_parts.append(f"AND team_id = {self._dialect.bind}")
            params.append(filters["team_id"])

        if "is_busy" in filters and filters["is_busy"] is not None:
            query_parts.append(f"AND is_busy = {self._dialect.bind}")
            params.append(1 if filters["is_busy"] else 0)

        if "is_executing" in filters and filters["is_executing"] is not None:
            query_parts.append(f"AND is_executing = {self._dialect.bind}")
            params.append(1 if filters["is_executing"] else 0)

        if "has_execution_capacity" in filters and filters["has_execution_capacity"] is not None:
            query_parts.append(f"AND has_execution_capacity = {self._dialect.bind}")
            params.append(1 if filters["has_execution_capacity"] else 0)

        if "is_team_lead" in filters and filters["is_team_lead"] is not None:
            query_parts.append(f"AND is_team_lead = {self._dialect.bind}")
            params.append(1 if filters["is_team_lead"] else 0)

        try:
            agents: List[AgentStatus] = []
            with self._connect() as conn:
                rows = conn.execute(" ".join(query_parts), params).fetchall()
                for row in rows:
                    if row[0]:
                        try:
                            agents.append(AgentStatus.model_validate_json(row[0]))
                        except Exception as parse_exc:
                            self._logger.warning(f"Error parsing AgentStatus json in query: {parse_exc}")
            return agents
        except Exception as exc:
            self._logger.error(f"Failed to query agent states with filters {filters}: {exc}")
            return []


class SqliteAgentStateRepository(_RelationalAgentStateRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._init_schema()


    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path, logger=self._logger)


    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
        ensure_sqlite_column(conn, table_name, column_name, column_type, logger=self._logger)


    def _column_names(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f'PRAGMA table_info({table_name})').fetchall()
        return {str(row[1]) for row in rows}


class PostgresAgentStateRepository(_RelationalAgentStateRepository):
    """Postgres implementation of the agent-state repository."""

    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        validate_postgres_connection(self._dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
        conn.execute(f'ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_type}')
        return


    def _column_names(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f'\n                SELECT column_name\n                FROM information_schema.columns\n                WHERE table_name = {self._dialect.bind}\n                ', (table_name,)).fetchall()
        return {str(row[0]) for row in rows}


def create_agent_state_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> AgentStateRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="AgentStateRepository",
        capability_supported=capabilities.supports_agent_state_repository,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqliteAgentStateRepository(storage_settings.sqlite_path, logger=logger)
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresAgentStateRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(
        f"AgentStateRepository has no implementation for backend {storage_settings.backend!r}."
    )


def _sortable_time(value: Optional[str]) -> Optional[str]:
    """A stored time in one fixed-width UTC form; text that is not a time is kept as is."""
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
