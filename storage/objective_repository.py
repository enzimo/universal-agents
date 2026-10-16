"""Backend-neutral repository boundary for durable objective persistence."""

from __future__ import annotations

from storage.read_only import repositories_are_read_only

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import logging
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Protocol

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE

from custom_messages.records.objectives import ObjectiveRecord

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
class ObjectiveListRecord:
    objective_id: str
    owner_agent_id: Optional[str]
    owner_agent_name: Optional[str]
    team_id: Optional[str]
    owner_named_agent_type: Optional[str]
    owner_canonical_user_id: Optional[str]
    title: str
    status: str
    horizon: str
    priority: str
    current_plan_id: Optional[str]
    root_conversation_id: Optional[str]
    created_by_task_id: Optional[str]
    promoted_from_task_id: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]


@dataclass(frozen=True)
class ObjectiveView:
    """Which Objectives a listing may return for one task's work.

    The team's Objectives (no owner) and those of ``person_id``, the person
    the task verifiably works for (None: work for no person, which sees only
    the team's). This is the reading rule of
    ``access_control.ownership.objective_access``, applied in the query so
    that the limit counts only Objectives the task may see. A listing without
    a view returns every Objective of the scope and is for trusted runtime
    code only.
    """

    person_id: Optional[str]


class ObjectiveRepository(Protocol):
    def save_objective(self, objective: ObjectiveRecord) -> None: ...

    def get_objective(
        self,
        objective_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[ObjectiveRecord]: ...

    def get_objective_by_plan(
        self,
        plan_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[ObjectiveRecord]: ...

    def get_objective_by_task(
        self,
        task_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[ObjectiveRecord]: ...

    def list_objectives(
        self,
        *,
        limit: int,
        owner_agent_id: str,
        statuses: Optional[List[str]] = None,
        horizons: Optional[List[str]] = None,
        include_closed: bool = False,
        view: Optional[ObjectiveView] = None,
    ) -> List[ObjectiveListRecord]: ...

    def get_objective_for_user(
        self,
        objective_id: str,
        *,
        owner_canonical_user_id: str,
        team_id: str,
    ) -> Optional[ObjectiveRecord]: ...

    def list_objectives_for_user(
        self,
        *,
        owner_canonical_user_id: str,
        team_id: str,
        limit: int = 100,
        include_closed: bool = False,
    ) -> List[ObjectiveRecord]: ...

    def list_team_objectives(
        self,
        *,
        team_id: str,
        include_closed: bool = False,
    ) -> List[ObjectiveRecord]: ...


class _RelationalObjectiveRepository:
    """Shared record operations for durable objective persistence."""

    _dialect: SqlDialect

    _TERMINAL_STATUSES = {
        "satisfied",
        "failed",
        "cancelled",
        "abandoned",
        "deleted",
    }


    @staticmethod
    def _objective_record_id(objective: ObjectiveRecord) -> str:
        owner_scope = objective.owner_agent_id or "unknown_agent"
        return f"{owner_scope}::{objective.objective_id}"

    @staticmethod
    def _require_owner_agent_id(owner_agent_id: Optional[str]) -> str:
        if not owner_agent_id:
            raise ValueError("ObjectiveRepository access requires owner_agent_id.")
        return owner_agent_id

    def _init_schema(self) -> None:
        if repositories_are_read_only():
            return
        backend_name = self._dialect.name
        storage_target = redact_postgres_dsn(self._dsn) if backend_name == "Postgres" else self._db_path
        try:
            if backend_name != "Postgres":
                ensure_storage_parent_dir(self._db_path)
            with self._connect() as conn:
                self._create_schema(conn)
                conn.commit()
            self._logger.info(f"{backend_name} objective repository initialized at {storage_target}")
        except Exception as exc:
            self._logger.error(f"Failed to initialize {backend_name} objective repository: {exc}")
            raise

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS objectives (
                objective_record_id TEXT PRIMARY KEY,
                objective_id TEXT NOT NULL,
                owner_agent_id TEXT,
                owner_agent_name TEXT,
                team_id TEXT,
                owner_named_agent_type TEXT,
                owner_canonical_user_id TEXT,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                horizon TEXT NOT NULL,
                priority TEXT NOT NULL,
                current_plan_id TEXT,
                root_conversation_id TEXT,
                created_by_task_id TEXT,
                promoted_from_task_id TEXT,
                objective_data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._ensure_column(
            conn,
            "objectives",
            "owner_canonical_user_id",
            "TEXT",
        )
        self._backfill_canonical_owners(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_objectives_objective_id ON objectives(objective_id)")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_objectives_owner_status
            ON objectives(owner_agent_id, status, updated_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_objectives_canonical_owner
            ON objectives(owner_canonical_user_id, team_id, updated_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_objectives_owner_updated
            ON objectives(owner_agent_id, updated_at DESC)
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_objectives_current_plan "
            "ON objectives(current_plan_id)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_objectives_root_conversation
            ON objectives(root_conversation_id)
            """
        )


    def _backfill_canonical_owners(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT objective_record_id, objective_data
            FROM objectives
            WHERE owner_canonical_user_id IS NULL
               OR owner_canonical_user_id = ''
            """
        ).fetchall()
        updated = 0
        for record_id, objective_data in rows:
            try:
                owner = str(
                    ObjectiveRecord.model_validate_json(
                        objective_data
                    ).owner_canonical_user_id
                    or ""
                ).strip()
            except Exception:
                self._logger.warning(
                    "Objective canonical-owner backfill skipped an unreadable row. "
                    "event=objective_owner_backfill_row_invalid record_id=%s",
                    record_id,
                    exc_info=True,
                )
                continue
            if not owner:
                continue
            conn.execute(
                f"""
                UPDATE objectives
                SET owner_canonical_user_id = {self._dialect.bind}
                WHERE objective_record_id = {self._dialect.bind}
                """,
                (owner, record_id),
            )
            updated += 1
        if updated:
            self._logger.warning(
                "Recovered normalized Objective canonical ownership from durable "
                "record data. event=objective_owner_backfill_completed rows=%d",
                updated,
            )

    def save_objective(self, objective: ObjectiveRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO objectives
                    (
                        objective_record_id, objective_id, owner_agent_id,
                        owner_agent_name, team_id, owner_named_agent_type,
                        owner_canonical_user_id,
                        title, status, horizon, priority, current_plan_id,
                        root_conversation_id, created_by_task_id,
                        promoted_from_task_id, objective_data, created_at,
                        updated_at
                    )
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(objective_record_id) DO UPDATE SET
                    owner_agent_id = excluded.owner_agent_id,
                    owner_agent_name = excluded.owner_agent_name,
                    team_id = excluded.team_id,
                    owner_named_agent_type = excluded.owner_named_agent_type,
                    owner_canonical_user_id = excluded.owner_canonical_user_id,
                    title = excluded.title,
                    status = excluded.status,
                    horizon = excluded.horizon,
                    priority = excluded.priority,
                    current_plan_id = excluded.current_plan_id,
                    root_conversation_id = excluded.root_conversation_id,
                    created_by_task_id = excluded.created_by_task_id,
                    promoted_from_task_id = excluded.promoted_from_task_id,
                    objective_data = excluded.objective_data,
                    updated_at = excluded.updated_at
                """,
                (
                    self._objective_record_id(objective),
                    objective.objective_id,
                    objective.owner_agent_id,
                    objective.owner_agent_name,
                    objective.team_id,
                    objective.owner_named_agent_type,
                    objective.owner_canonical_user_id,
                    objective.title,
                    objective.status.value,
                    objective.horizon.value,
                    objective.priority.value,
                    objective.current_plan_id,
                    objective.root_conversation_id,
                    objective.created_by_task_id,
                    objective.promoted_from_task_id,
                    objective.model_dump_json(),
                    objective.created_at,
                    objective.updated_at,
                ),
            )
            conn.commit()

    def get_objective(
        self,
        objective_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[ObjectiveRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT objective_data
                FROM objectives
                WHERE objective_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (objective_id, owner_agent_id),
            ).fetchone()
        if not row:
            return None
        return ObjectiveRecord.model_validate_json(row[0])

    @staticmethod
    def _require_canonical_owner(owner_canonical_user_id: str) -> str:
        owner = str(owner_canonical_user_id or "").strip()
        if not owner:
            raise ValueError(
                "ObjectiveRepository user access requires owner_canonical_user_id."
            )
        return owner

    def get_objective_for_user(
        self,
        objective_id: str,
        *,
        owner_canonical_user_id: str,
        team_id: str,
    ) -> Optional[ObjectiveRecord]:
        owner = self._require_canonical_owner(owner_canonical_user_id)
        team = str(team_id or "").strip()
        if not team:
            raise ValueError("ObjectiveRepository user access requires team_id.")
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT objective_data
                FROM objectives
                WHERE objective_id = {self._dialect.bind}
                  AND owner_canonical_user_id = {self._dialect.bind}
                  AND (team_id = {self._dialect.bind} OR team_id IS NULL OR team_id = '')
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (objective_id, owner, team),
            ).fetchone()
        return ObjectiveRecord.model_validate_json(row[0]) if row else None

    def list_objectives_for_user(
        self,
        *,
        owner_canonical_user_id: str,
        team_id: str,
        limit: int = 100,
        include_closed: bool = False,
    ) -> List[ObjectiveRecord]:
        owner = self._require_canonical_owner(owner_canonical_user_id)
        team = str(team_id or "").strip()
        if not team:
            raise ValueError("ObjectiveRepository user access requires team_id.")
        query = [
            f"""
            SELECT objective_data
            FROM objectives
            WHERE owner_canonical_user_id = {self._dialect.bind}
              AND (team_id = {self._dialect.bind} OR team_id IS NULL OR team_id = '')
            """
        ]
        params: list[object] = [owner, team]
        if not include_closed:
            placeholders = ",".join(f"{self._dialect.bind}" for _ in self._TERMINAL_STATUSES)
            query.append(f"AND status NOT IN ({placeholders})")
            params.extend(sorted(self._TERMINAL_STATUSES))
        query.append(f"ORDER BY updated_at DESC LIMIT {self._dialect.bind}")
        params.append(max(1, min(int(limit or 100), 500)))
        with self._connect() as conn:
            rows = conn.execute(" ".join(query), tuple(params)).fetchall()
        return [ObjectiveRecord.model_validate_json(row[0]) for row in rows]

    def list_team_objectives(
        self,
        *,
        team_id: str,
        include_closed: bool = False,
    ) -> List[ObjectiveRecord]:
        """Every objective of the team, of every owner, without a limit (read only).

        For the team export (``team_transfer.objectives``), which must see each
        open objective, including ones that belong to no person, so that none
        is left out of the transfer without being listed.
        """

        team = str(team_id or "").strip()
        if not team:
            raise ValueError("ObjectiveRepository team listing requires team_id.")
        query = [
            f"""
            SELECT objective_data
            FROM objectives
            WHERE (team_id = {self._dialect.bind} OR team_id IS NULL OR team_id = '')
            """
        ]
        params: list[object] = [team]
        if not include_closed:
            placeholders = ",".join(f"{self._dialect.bind}" for _ in self._TERMINAL_STATUSES)
            query.append(f"AND status NOT IN ({placeholders})")
            params.extend(sorted(self._TERMINAL_STATUSES))
        query.append("ORDER BY objective_record_id")
        with self._connect() as conn:
            rows = conn.execute(" ".join(query), tuple(params)).fetchall()
        return [ObjectiveRecord.model_validate_json(row[0]) for row in rows]

    def get_objective_by_plan(
        self,
        plan_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[ObjectiveRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT objective_data
                FROM objectives
                WHERE current_plan_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                   OR (owner_agent_id = {self._dialect.bind} AND objective_data LIKE {self._dialect.bind})
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (plan_id, owner_agent_id, owner_agent_id, f'%"{plan_id}"%'),
            ).fetchone()
        if not row:
            return None
        objective = ObjectiveRecord.model_validate_json(row[0])
        if objective.current_plan_id == plan_id or plan_id in objective.plan_ids:
            return objective
        return None

    def get_objective_by_task(
        self,
        task_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[ObjectiveRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT objective_data
                FROM objectives
                WHERE owner_agent_id = {self._dialect.bind} AND objective_data LIKE {self._dialect.bind}
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (owner_agent_id, f'%"{task_id}"%'),
            ).fetchone()
        if not row:
            return None
        objective = ObjectiveRecord.model_validate_json(row[0])
        if (
            objective.created_by_task_id == task_id
            or objective.promoted_from_task_id == task_id
            or objective.satisfied_by_task_id == task_id
            or task_id in objective.related_task_ids
            or task_id in objective.recovery_task_ids
        ):
            return objective
        return None

    def _view_clause(self, view: ObjectiveView) -> tuple[str, list[object]]:
        """The team's Objectives, plus the person's own when there is one."""

        team_only = "(owner_canonical_user_id IS NULL OR owner_canonical_user_id = '')"
        person = str(view.person_id or "").strip()
        if not person:
            return f"AND {team_only}", []
        return f"AND ({team_only} OR owner_canonical_user_id = {self._dialect.bind})", [person]

    def list_objectives(
        self,
        *,
        limit: int,
        owner_agent_id: str,
        statuses: Optional[List[str]] = None,
        horizons: Optional[List[str]] = None,
        include_closed: bool = False,
        view: Optional[ObjectiveView] = None,
    ) -> List[ObjectiveListRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        safe_limit = max(1, min(int(limit or 20), 100))
        query_parts = [
            f"""
            SELECT objective_id, owner_agent_id, owner_agent_name, team_id,
                   owner_named_agent_type, owner_canonical_user_id,
                   title, status, horizon, priority,
                   current_plan_id, root_conversation_id, created_by_task_id,
                   promoted_from_task_id, created_at, updated_at
            FROM objectives
            WHERE owner_agent_id = {self._dialect.bind}
            """
        ]
        params: List[object] = [owner_agent_id]
        if statuses:
            placeholders = ",".join(f"{self._dialect.bind}" for _ in statuses)
            query_parts.append(f"AND status IN ({placeholders})")
            params.extend(statuses)
        elif not include_closed:
            placeholders = ",".join(f"{self._dialect.bind}" for _ in self._TERMINAL_STATUSES)
            query_parts.append(f"AND status NOT IN ({placeholders})")
            params.extend(sorted(self._TERMINAL_STATUSES))
        if horizons:
            placeholders = ",".join(f"{self._dialect.bind}" for _ in horizons)
            query_parts.append(f"AND horizon IN ({placeholders})")
            params.extend(horizons)
        if view is not None:
            clause, view_params = self._view_clause(view)
            query_parts.append(clause)
            params.extend(view_params)
        query_parts.append(f"ORDER BY updated_at DESC LIMIT {self._dialect.bind}")
        params.append(safe_limit)

        with self._connect() as conn:
            rows = conn.execute(" ".join(query_parts), tuple(params)).fetchall()

        return [
            ObjectiveListRecord(
                objective_id=row[0],
                owner_agent_id=row[1],
                owner_agent_name=row[2],
                team_id=row[3],
                owner_named_agent_type=row[4],
                owner_canonical_user_id=row[5],
                title=row[6],
                status=row[7],
                horizon=row[8],
                priority=row[9],
                current_plan_id=row[10],
                root_conversation_id=row[11],
                created_by_task_id=row[12],
                promoted_from_task_id=row[13],
                created_at=row[14],
                updated_at=row[15],
            )
            for row in rows
        ]


class SqliteObjectiveRepository(_RelationalObjectiveRepository):
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


class PostgresObjectiveRepository(_RelationalObjectiveRepository):
    """Postgres implementation for durable objective persistence."""

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


def create_objective_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> ObjectiveRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="ObjectiveRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqliteObjectiveRepository(storage_settings.sqlite_path, logger=logger)
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresObjectiveRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(
        f"ObjectiveRepository has no implementation for backend {storage_settings.backend!r}."
    )
