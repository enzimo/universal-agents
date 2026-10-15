"""Backend-neutral repository boundary for durable plan persistence."""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import logging
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Protocol

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE

from custom_messages.records.plans import PlanRecord

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
class PlanListRecord:
    plan_id: str
    owner_agent_id: Optional[str]
    owner_agent_name: Optional[str]
    team_id: Optional[str]
    owner_named_agent_type: Optional[str]
    root_task_id: Optional[str]
    root_conversation_id: Optional[str]
    objective_id: Optional[str]
    objective_title: Optional[str]
    status: str
    created_at: Optional[str]
    updated_at: Optional[str]


class PlanRepository(Protocol):
    def save_plan(self, plan: PlanRecord) -> None: ...

    def get_plan(
        self,
        plan_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[PlanRecord]: ...

    def get_plan_by_root_task(
        self,
        root_task_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[PlanRecord]: ...

    def get_plan_by_task(
        self,
        task_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[PlanRecord]: ...

    def list_plans(
        self,
        *,
        limit: int,
        owner_agent_id: str,
    ) -> List[PlanListRecord]: ...


class _RelationalPlanRepository:
    """Shared record operations for durable plan persistence."""

    _dialect: SqlDialect


    @staticmethod
    def _plan_record_id(plan: PlanRecord) -> str:
        owner_scope = plan.owner_agent_id or "unknown_agent"
        return f"{owner_scope}::{plan.plan_id}"

    @staticmethod
    def _require_owner_agent_id(owner_agent_id: Optional[str]) -> str:
        if not owner_agent_id:
            raise ValueError("PlanRepository access requires owner_agent_id.")
        return owner_agent_id

    def _init_schema(self) -> None:
        backend_name = self._dialect.name
        storage_target = redact_postgres_dsn(self._dsn) if backend_name == "Postgres" else self._db_path
        try:
            ensure_storage_parent_dir(self._db_path)
            with self._connect() as conn:
                self._create_schema(conn)
                conn.commit()
            self._logger.info(f"{backend_name} plan repository initialized at {storage_target}")
        except Exception as exc:
            self._logger.error(f"Failed to initialize {backend_name} plan repository: {exc}")
            raise

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plans (
                plan_record_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                owner_agent_id TEXT,
                owner_agent_name TEXT,
                team_id TEXT,
                owner_named_agent_type TEXT,
                root_task_id TEXT,
                root_conversation_id TEXT,
                objective_id TEXT,
                objective_title TEXT,
                status TEXT,
                plan_data TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        self._ensure_column(conn, "plans", "team_id", "TEXT")
        self._ensure_column(conn, "plans", "owner_named_agent_type", "TEXT")
        self._ensure_column(conn, "plans", "root_conversation_id", "TEXT")
        self._ensure_column(conn, "plans", "objective_id", "TEXT")
        self._ensure_column(conn, "plans", "objective_title", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_plan_id ON plans(plan_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_root_task ON plans(root_task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_objective_id ON plans(objective_id)")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_plans_owner
            ON plans(owner_agent_id, updated_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_plans_team_type
            ON plans(team_id, owner_named_agent_type, updated_at DESC)
            """
        )


    def save_plan(self, plan: PlanRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO plans
                    (
                        plan_record_id, plan_id, owner_agent_id, owner_agent_name,
                        team_id, owner_named_agent_type, root_task_id,
                        root_conversation_id, objective_id, objective_title,
                        status, plan_data, created_at, updated_at
                    )
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(plan_record_id) DO UPDATE SET
                    owner_agent_id = excluded.owner_agent_id,
                    owner_agent_name = excluded.owner_agent_name,
                    team_id = excluded.team_id,
                    owner_named_agent_type = excluded.owner_named_agent_type,
                    root_task_id = excluded.root_task_id,
                    root_conversation_id = excluded.root_conversation_id,
                    objective_id = excluded.objective_id,
                    objective_title = excluded.objective_title,
                    status = excluded.status,
                    plan_data = excluded.plan_data,
                    updated_at = excluded.updated_at
                """,
                (
                    self._plan_record_id(plan),
                    plan.plan_id,
                    plan.owner_agent_id,
                    plan.owner_agent_name,
                    plan.team_id,
                    plan.owner_named_agent_type,
                    plan.root_task_id,
                    plan.root_conversation_id,
                    plan.objective_id,
                    plan.objective_title,
                    plan.status.value,
                    plan.model_dump_json(),
                    plan.created_at,
                    plan.updated_at,
                ),
            )
            conn.commit()

    def get_plan(
        self,
        plan_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[PlanRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT plan_data
                FROM plans
                WHERE plan_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (plan_id, owner_agent_id),
            ).fetchone()
        if not row:
            return None
        return PlanRecord.model_validate_json(row[0])

    def get_plan_by_root_task(
        self,
        root_task_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[PlanRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT plan_data
                FROM plans
                WHERE root_task_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (root_task_id, owner_agent_id),
            ).fetchone()
        if not row:
            return None
        return PlanRecord.model_validate_json(row[0])

    def get_plan_by_task(
        self,
        task_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[PlanRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT plan_data
                FROM plans
                WHERE owner_agent_id = {self._dialect.bind}
                  AND (root_task_id = {self._dialect.bind} OR plan_data LIKE {self._dialect.bind})
                ORDER BY updated_at DESC
                """,
                (owner_agent_id, task_id, f'%"{task_id}"%'),
            ).fetchall()
        for row in rows:
            plan = PlanRecord.model_validate_json(row[0])
            if task_id == plan.root_task_id or task_id in plan.related_task_ids or task_id in plan.recovery_task_ids:
                return plan
        return None

    def list_plans(
        self,
        *,
        limit: int,
        owner_agent_id: str,
    ) -> List[PlanListRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        safe_limit = max(1, min(limit, 100))
        params: List[object] = [owner_agent_id]
        query = f"""
            SELECT plan_id, owner_agent_id, owner_agent_name, team_id,
                   owner_named_agent_type, root_task_id, root_conversation_id,
                   objective_id, objective_title, status, created_at, updated_at
            FROM plans
            WHERE owner_agent_id = {self._dialect.bind}
        """
        query += f" ORDER BY updated_at DESC LIMIT {self._dialect.bind}"
        params.append(safe_limit)

        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        return [
            PlanListRecord(
                plan_id=row[0],
                owner_agent_id=row[1],
                owner_agent_name=row[2],
                team_id=row[3],
                owner_named_agent_type=row[4],
                root_task_id=row[5],
                root_conversation_id=row[6],
                objective_id=row[7],
                objective_title=row[8],
                status=row[9],
                created_at=row[10],
                updated_at=row[11],
            )
            for row in rows
        ]


class SqlitePlanRepository(_RelationalPlanRepository):
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


class PostgresPlanRepository(_RelationalPlanRepository):
    """Postgres implementation for durable plan persistence."""

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

def create_plan_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> PlanRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="PlanRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqlitePlanRepository(storage_settings.sqlite_path, logger=logger)
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresPlanRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(
        f"PlanRepository has no implementation for backend {storage_settings.backend!r}."
    )
