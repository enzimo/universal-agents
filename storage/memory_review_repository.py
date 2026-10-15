"""Backend-neutral repository for pinned-memory review jobs."""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from id_generation import new_id

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from .config import StorageSettings
from .postgres_connection import (
    PostgresConnection,
    connect_postgres,
    ensure_storage_parent_dir,
    validate_postgres_connection,
)
from .runtime import describe_storage_capabilities, require_repository_backend
from .sqlite_connection import connect_sqlite, ensure_sqlite_column


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MemoryReviewJobRow:
    review_job_id: str
    owner_team_id: str
    owner_named_agent_type: str
    task_id: str
    conversation_id: str
    turn_index: int
    agent_subject_id: Optional[str]
    user_subject_id: Optional[str]
    team_subject_id: Optional[str]
    source_payload_json: str
    status: str
    claim_owner_agent_id: Optional[str]
    claim_expires_at: Optional[str]
    error_text: Optional[str]
    created_at: str
    updated_at: str

    @property
    def source_payload(self) -> dict[str, Any]:
        return json.loads(self.source_payload_json or "{}")


class MemoryReviewRepository(Protocol):
    def enqueue_review_job(
        self,
        *,
        owner_team_id: str,
        owner_named_agent_type: str,
        task_id: str,
        conversation_id: str,
        turn_index: int,
        source_payload: dict[str, Any],
    ) -> MemoryReviewJobRow: ...

    def get_review_job(self, review_job_id: str) -> Optional[MemoryReviewJobRow]: ...

    def list_review_jobs(self, *, limit: int = 100, owner_team_id: str | None = None, owner_named_agent_type: str | None = None, pending_only: bool = False, offset: int = 0) -> list[MemoryReviewJobRow]: ...

    def claim_review_jobs_for_role(
        self,
        *,
        owner_team_id: str,
        owner_named_agent_type: str,
        claimer_agent_id: str,
        limit: int = 10,
        claim_ttl_seconds: int = 300,
    ) -> list[MemoryReviewJobRow]: ...

    def claim_review_job(
        self,
        review_job_id: str,
        *,
        owner_team_id: str,
        owner_named_agent_type: str,
        claimer_agent_id: str,
        claim_ttl_seconds: int = 300,
    ) -> Optional[MemoryReviewJobRow]: ...

    def mark_review_job_completed(self, review_job_id: str, *, claimer_agent_id: str) -> None: ...

    def mark_review_job_failed(self, review_job_id: str, *, claimer_agent_id: str, error_text: str) -> None: ...

    def mark_review_job_skipped(
        self,
        review_job_id: str,
        *,
        claimer_agent_id: str,
        error_text: Optional[str] = None,
    ) -> None: ...


class _RelationalMemoryReviewRepository:
    """Shared record operations for durable pinned-memory review jobs."""

    _dialect: SqlDialect


    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_review_jobs (
                    review_job_id TEXT PRIMARY KEY,
                    owner_team_id TEXT NOT NULL,
                    owner_named_agent_type TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    agent_subject_id TEXT,
                    user_subject_id TEXT,
                    team_subject_id TEXT,
                    source_payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    claim_owner_agent_id TEXT,
                    claim_expires_at TEXT,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "memory_review_jobs", "owner_team_id", "TEXT")
            self._ensure_column(conn, "memory_review_jobs", "team_subject_id", "TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_review_jobs_role_status
                ON memory_review_jobs(owner_team_id, owner_named_agent_type, status, created_at)
                """
            )
            conn.commit()


    def enqueue_review_job(
        self,
        *,
        owner_team_id: str,
        owner_named_agent_type: str,
        task_id: str,
        conversation_id: str,
        turn_index: int,
        source_payload: dict[str, Any],
    ) -> MemoryReviewJobRow:
        now = utc_now_iso()
        review_job_id = new_id("memory_review")
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO memory_review_jobs (
                    review_job_id, owner_team_id, owner_named_agent_type, task_id,
                    conversation_id, turn_index, agent_subject_id, user_subject_id,
                    team_subject_id, source_payload_json, status,
                    claim_owner_agent_id, claim_expires_at, error_text,
                    created_at, updated_at
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, NULL, NULL, {self._dialect.bind}, 'queued', NULL, NULL, NULL, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    review_job_id,
                    owner_team_id,
                    owner_named_agent_type,
                    task_id,
                    conversation_id,
                    turn_index,
                    json.dumps(source_payload or {}, sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.commit()
        row = self.get_review_job(review_job_id)
        if row is None:
            raise RuntimeError(f"Review job {review_job_id} was not readable after insert.")
        return row

    def get_review_job(self, review_job_id: str) -> Optional[MemoryReviewJobRow]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT review_job_id, owner_team_id, owner_named_agent_type, task_id,
                       conversation_id, turn_index, agent_subject_id, user_subject_id,
                       team_subject_id, source_payload_json, status,
                       claim_owner_agent_id, claim_expires_at, error_text,
                       created_at, updated_at
                FROM memory_review_jobs
                WHERE review_job_id = {self._dialect.bind}
                """,
                (review_job_id,),
            ).fetchone()
        return self._row_to_review_job(row)

    def list_review_jobs(self, *, limit: int = 100, owner_team_id: str | None = None, owner_named_agent_type: str | None = None, pending_only: bool = False, offset: int = 0) -> list[MemoryReviewJobRow]:
        conditions = []
        params: list[Any] = []
        for column, value in (("owner_team_id", owner_team_id), ("owner_named_agent_type", owner_named_agent_type)):
            if value is not None:
                conditions.append(f"{column} = {self._dialect.bind}")
                params.append(value)
        if pending_only:
            conditions.append("status IN ('queued', 'claimed')")
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT review_job_id, owner_team_id, owner_named_agent_type, task_id,
                       conversation_id, turn_index, agent_subject_id, user_subject_id,
                       team_subject_id, source_payload_json, status,
                       claim_owner_agent_id, claim_expires_at, error_text,
                       created_at, updated_at
                FROM memory_review_jobs
                {where}
                ORDER BY created_at ASC, review_job_id ASC
                LIMIT {self._dialect.bind} OFFSET {self._dialect.bind}
                """,
                (*params, limit, offset),
            ).fetchall()
        return [row for row in (self._row_to_review_job(item) for item in rows) if row is not None]


    def mark_review_job_completed(self, review_job_id: str, *, claimer_agent_id: str) -> None:
        self._transition_claimed_review_job(
            review_job_id,
            claimer_agent_id=claimer_agent_id,
            new_status="completed",
            error_text=None,
        )

    def mark_review_job_failed(self, review_job_id: str, *, claimer_agent_id: str, error_text: str) -> None:
        self._transition_claimed_review_job(
            review_job_id,
            claimer_agent_id=claimer_agent_id,
            new_status="failed",
            error_text=error_text,
        )

    def mark_review_job_skipped(
        self,
        review_job_id: str,
        *,
        claimer_agent_id: str,
        error_text: Optional[str] = None,
    ) -> None:
        self._transition_claimed_review_job(
            review_job_id,
            claimer_agent_id=claimer_agent_id,
            new_status="skipped",
            error_text=error_text,
        )

    def _transition_claimed_review_job(
        self,
        review_job_id: str,
        *,
        claimer_agent_id: str,
        new_status: str,
        error_text: Optional[str],
    ) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT claim_owner_agent_id, status
                FROM memory_review_jobs
                WHERE review_job_id = {self._dialect.bind}
                """,
                (review_job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown review job: {review_job_id}")
            if row["status"] != "claimed" or row["claim_owner_agent_id"] != claimer_agent_id:
                raise ValueError(f"Review job {review_job_id} is not claimed by {claimer_agent_id}")
            conn.execute(
                f"""
                UPDATE memory_review_jobs
                SET status = {self._dialect.bind}, claim_expires_at = NULL, error_text = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE review_job_id = {self._dialect.bind}
                """,
                (new_status, error_text, now, review_job_id),
            )
            conn.commit()

    @staticmethod
    def _row_to_review_job(row: sqlite3.Row | None) -> Optional[MemoryReviewJobRow]:
        if row is None:
            return None
        return MemoryReviewJobRow(
            review_job_id=row["review_job_id"],
            owner_team_id=row["owner_team_id"],
            owner_named_agent_type=row["owner_named_agent_type"],
            task_id=row["task_id"],
            conversation_id=row["conversation_id"],
            turn_index=int(row["turn_index"]),
            agent_subject_id=row["agent_subject_id"],
            user_subject_id=row["user_subject_id"],
            team_subject_id=row["team_subject_id"],
            source_payload_json=row["source_payload_json"],
            status=row["status"],
            claim_owner_agent_id=row["claim_owner_agent_id"],
            claim_expires_at=row["claim_expires_at"],
            error_text=row["error_text"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class SqliteMemoryReviewRepository(_RelationalMemoryReviewRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        ensure_storage_parent_dir(self._db_path)
        self._init_schema()


    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path, logger=self._logger, row_factory=sqlite3.Row)


    def claim_review_jobs_for_role(
        self,
        *,
        owner_team_id: str,
        owner_named_agent_type: str,
        claimer_agent_id: str,
        limit: int = 10,
        claim_ttl_seconds: int = 300,
    ) -> list[MemoryReviewJobRow]:
        now = utc_now_iso()
        claim_seconds = datetime.now(timezone.utc).timestamp() + max(1, claim_ttl_seconds)
        claim_expires_at = datetime.fromtimestamp(claim_seconds, tz=timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT review_job_id
                FROM memory_review_jobs
                WHERE owner_team_id = {self._dialect.bind}
                  AND owner_named_agent_type = {self._dialect.bind}
                  AND status = 'queued'
                ORDER BY created_at ASC
                LIMIT {self._dialect.bind}
                """,
                (owner_team_id, owner_named_agent_type, limit),
            ).fetchall()
            claimed_ids = [str(row["review_job_id"]) for row in rows]
            for review_job_id in claimed_ids:
                conn.execute(
                    f"""
                    UPDATE memory_review_jobs
                    SET status = 'claimed',
                        claim_owner_agent_id = {self._dialect.bind},
                        claim_expires_at = {self._dialect.bind},
                        error_text = NULL,
                        updated_at = {self._dialect.bind}
                    WHERE review_job_id = {self._dialect.bind}
                    """,
                    (claimer_agent_id, claim_expires_at, now, review_job_id),
                )
            conn.commit()
        return [row for row in (self.get_review_job(job_id) for job_id in claimed_ids) if row is not None]


    def claim_review_job(
        self,
        review_job_id: str,
        *,
        owner_team_id: str,
        owner_named_agent_type: str,
        claimer_agent_id: str,
        claim_ttl_seconds: int = 300,
    ) -> Optional[MemoryReviewJobRow]:
        """Claim queued work. Expired ownership requires cleanup review, not replay."""
        now = utc_now_iso()
        claim_seconds = datetime.now(timezone.utc).timestamp() + max(1, claim_ttl_seconds)
        claim_expires_at = datetime.fromtimestamp(claim_seconds, tz=timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""
                SELECT review_job_id
                FROM memory_review_jobs
                WHERE review_job_id = {self._dialect.bind}
                  AND owner_team_id = {self._dialect.bind}
                  AND owner_named_agent_type = {self._dialect.bind}
                  AND status = 'queued'
                """,
                (review_job_id, owner_team_id, owner_named_agent_type),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                f"""
                UPDATE memory_review_jobs
                SET status = 'claimed',
                    claim_owner_agent_id = {self._dialect.bind},
                    claim_expires_at = {self._dialect.bind},
                    error_text = NULL,
                    updated_at = {self._dialect.bind}
                WHERE review_job_id = {self._dialect.bind}
                """,
                (claimer_agent_id, claim_expires_at, now, review_job_id),
            )
            conn.commit()
        return self.get_review_job(review_job_id)


    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
        ensure_sqlite_column(conn, table_name, column_name, ddl, logger=self._logger)


    def _column_names(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f'PRAGMA table_info({table_name})').fetchall()
        return {str(row[1]) for row in rows}


class PostgresMemoryReviewRepository(_RelationalMemoryReviewRepository):
    """Postgres implementation for durable pinned-memory review jobs."""

    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        validate_postgres_connection(self._dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)

    def claim_review_jobs_for_role(
        self,
        *,
        owner_team_id: str,
        owner_named_agent_type: str,
        claimer_agent_id: str,
        limit: int = 10,
        claim_ttl_seconds: int = 300,
    ) -> list[MemoryReviewJobRow]:
        now = utc_now_iso()
        claim_seconds = datetime.now(timezone.utc).timestamp() + max(1, claim_ttl_seconds)
        claim_expires_at = datetime.fromtimestamp(claim_seconds, tz=timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT review_job_id
                FROM memory_review_jobs
                WHERE owner_team_id = {self._dialect.bind}
                  AND owner_named_agent_type = {self._dialect.bind}
                  AND status = 'queued'
                ORDER BY created_at ASC
                LIMIT {self._dialect.bind}
                FOR UPDATE SKIP LOCKED
                """,
                (owner_team_id, owner_named_agent_type, limit),
            ).fetchall()
            claimed_ids = [str(row["review_job_id"]) for row in rows]
            for review_job_id in claimed_ids:
                conn.execute(
                    f"""
                    UPDATE memory_review_jobs
                    SET status = 'claimed',
                        claim_owner_agent_id = {self._dialect.bind},
                        claim_expires_at = {self._dialect.bind},
                        error_text = NULL,
                        updated_at = {self._dialect.bind}
                    WHERE review_job_id = {self._dialect.bind}
                    """,
                    (claimer_agent_id, claim_expires_at, now, review_job_id),
                )
            conn.commit()
        return [row for row in (self.get_review_job(job_id) for job_id in claimed_ids) if row is not None]

    def claim_review_job(
        self,
        review_job_id: str,
        *,
        owner_team_id: str,
        owner_named_agent_type: str,
        claimer_agent_id: str,
        claim_ttl_seconds: int = 300,
    ) -> Optional[MemoryReviewJobRow]:
        """Atomically claim one specific job without claiming unrelated role backlog."""
        now = utc_now_iso()
        claim_seconds = datetime.now(timezone.utc).timestamp() + max(1, claim_ttl_seconds)
        claim_expires_at = datetime.fromtimestamp(claim_seconds, tz=timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT review_job_id
                FROM memory_review_jobs
                WHERE review_job_id = {self._dialect.bind}
                  AND owner_team_id = {self._dialect.bind}
                  AND owner_named_agent_type = {self._dialect.bind}
                  AND status = 'queued'
                FOR UPDATE SKIP LOCKED
                """,
                (review_job_id, owner_team_id, owner_named_agent_type),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                f"""
                UPDATE memory_review_jobs
                SET status = 'claimed',
                    claim_owner_agent_id = {self._dialect.bind},
                    claim_expires_at = {self._dialect.bind},
                    error_text = NULL,
                    updated_at = {self._dialect.bind}
                WHERE review_job_id = {self._dialect.bind}
                """,
                (claimer_agent_id, claim_expires_at, now, review_job_id),
            )
            conn.commit()
        return self.get_review_job(review_job_id)


    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
        conn.execute(f'ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {ddl}')


    def _column_names(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f'\n                SELECT column_name\n                FROM information_schema.columns\n                WHERE table_name = {self._dialect.bind}\n                ', (table_name,)).fetchall()
        return {str(row[0]) for row in rows}


def create_memory_review_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> MemoryReviewRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="MemoryReviewRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqliteMemoryReviewRepository(storage_settings.sqlite_path, logger=logger)
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresMemoryReviewRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(
        f"MemoryReviewRepository has no implementation for backend {storage_settings.backend!r}."
    )
