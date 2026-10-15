"""Backend-neutral repository boundary for AfterActionReport review state.

Each review and batch records the person the reported work was for
(``person_id``, None for work for nobody) and whether that came from verified
identity (``identity_verified``). Lookups that serve a review are scoped to one
person: they match only verified rows of exactly that person (or of nobody),
so one person's review never sees another person's reports, and rows saved
before the person was recorded match no one
(``custom_agents.after_action_report_identity``).

Report ids proposed by a model are unique only inside one person's work.
Deduplication and unique indexes use that same scope, including work for no
person, and an existing review's person and verification state never change.
"""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import logging
import sqlite3
from typing import List, Optional, Protocol

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE

from custom_messages.enums import AfterActionReportReviewBatchStatus, AfterActionReportReviewStatus
from custom_messages.records.reviews import AfterActionReportReviewBatchRecord, AfterActionReportReviewRecord

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


class AfterActionReportRepository(Protocol):
    def upsert_review(self, record: AfterActionReportReviewRecord) -> AfterActionReportReviewRecord: ...

    def get_review(
        self,
        review_id: str,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
    ) -> Optional[AfterActionReportReviewRecord]: ...

    def list_reviews(
        self,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
        status: Optional[AfterActionReportReviewStatus],
        limit: int,
    ) -> List[AfterActionReportReviewRecord]: ...

    def get_review_by_report_id(
        self,
        report_id: str,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
    ) -> Optional[AfterActionReportReviewRecord]: ...

    def list_reviews_for_batch(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
    ) -> List[AfterActionReportReviewRecord]: ...

    def resolve_review(
        self,
        review_id: str,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
        status: AfterActionReportReviewStatus,
        decision_summary: str,
        review_rationale: Optional[str],
        resolution_history_entry_id: Optional[str],
        reviewed_at: str,
    ) -> Optional[AfterActionReportReviewRecord]: ...

    def ignore_unverified_pending_reviews(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
        stale_reason: str,
        decision_summary: str,
        reviewed_at: str,
    ) -> int: ...

    def upsert_batch(self, record: AfterActionReportReviewBatchRecord) -> AfterActionReportReviewBatchRecord: ...

    def get_batch(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[AfterActionReportReviewBatchRecord]: ...

    def get_latest_batch_for_task(
        self,
        parent_task_id: str,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
    ) -> Optional[AfterActionReportReviewBatchRecord]: ...

    def get_open_batch_for_task(
        self,
        parent_task_id: str,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
    ) -> Optional[AfterActionReportReviewBatchRecord]: ...

    def list_open_batches_for_task(
        self,
        parent_task_id: str,
        *,
        owner_agent_id: str,
    ) -> List[AfterActionReportReviewBatchRecord]: ...

    def mark_batch_scheduled(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
        scheduled_message_id: str,
        scheduled_review_task_id: str,
        updated_at: str,
    ) -> Optional[AfterActionReportReviewBatchRecord]: ...

    def claim_batch_for_finalizing(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
        updated_at: str,
    ) -> Optional[AfterActionReportReviewBatchRecord]: ...

    def close_batch(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
        final_history_entry_id: Optional[str],
        report_count: int,
        stale_report_count: int,
        closed_at: str,
    ) -> Optional[AfterActionReportReviewBatchRecord]: ...


class _RelationalAfterActionReportRepository:
    """Shared record operations for manager-owned AAR review state."""

    _dialect: SqlDialect


    @staticmethod
    def _review_record_id(record: AfterActionReportReviewRecord) -> str:
        owner_scope = record.owner_agent_id or "unknown_agent"
        return f"{owner_scope}::{record.review_id}"

    @staticmethod
    def _batch_record_id(record: AfterActionReportReviewBatchRecord) -> str:
        owner_scope = record.owner_agent_id or "unknown_agent"
        return f"{owner_scope}::{record.batch_id}"

    @staticmethod
    def _require_owner_agent_id(owner_agent_id: Optional[str]) -> str:
        if not owner_agent_id:
            raise ValueError("AfterActionReportRepository access requires owner_agent_id.")
        return owner_agent_id

    def _person_clause(self, person_id: Optional[str]) -> tuple[str, tuple[object, ...]]:
        """Rows recorded, from verified identity, for exactly ``person_id`` (None: work for nobody).

        Rows saved before the person was recorded (``identity_verified`` 0)
        match no one, so nobody's review sees them.
        """

        if person_id is None:
            return "identity_verified = 1 AND person_id IS NULL", ()
        return f"identity_verified = 1 AND person_id = {self._dialect.bind}", (person_id,)

    def _init_schema(self) -> None:
        backend_name = self._dialect.name
        storage_target = redact_postgres_dsn(self._dsn) if backend_name == "Postgres" else self._db_path
        try:
            ensure_storage_parent_dir(self._db_path)
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS after_action_report_reviews (
                        review_record_id TEXT PRIMARY KEY,
                        review_id TEXT NOT NULL,
                        report_id TEXT NOT NULL,
                        batch_id TEXT,
                        owner_agent_id TEXT,
                        owner_agent_name TEXT,
                        team_id TEXT,
                        owner_named_agent_type TEXT,
                        source_task_id TEXT,
                        parent_task_id TEXT,
                        source_agent_id TEXT,
                        source_agent_name TEXT,
                        source_named_agent_type TEXT,
                        person_id TEXT,
                        identity_verified INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        created_at TEXT,
                        updated_at TEXT,
                        reviewed_at TEXT,
                        stale_reason TEXT,
                        review_data TEXT NOT NULL
                    )
                    """
                )
                self._ensure_column(conn, "after_action_report_reviews", "batch_id", "TEXT")
                self._ensure_column(conn, "after_action_report_reviews", "stale_reason", "TEXT")
                self._ensure_column(conn, "after_action_report_reviews", "person_id", "TEXT")
                self._ensure_column(
                    conn, "after_action_report_reviews", "identity_verified", "INTEGER NOT NULL DEFAULT 0"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS after_action_report_review_batches (
                        batch_record_id TEXT PRIMARY KEY,
                        batch_id TEXT NOT NULL,
                        owner_agent_id TEXT,
                        owner_agent_name TEXT,
                        team_id TEXT,
                        owner_named_agent_type TEXT,
                        parent_task_id TEXT NOT NULL,
                        person_id TEXT,
                        identity_verified INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        opened_at TEXT,
                        accept_until TEXT,
                        scheduled_review_at TEXT,
                        scheduled_message_id TEXT,
                        scheduled_review_task_id TEXT,
                        final_history_entry_id TEXT,
                        report_count INTEGER,
                        stale_report_count INTEGER,
                        created_at TEXT,
                        updated_at TEXT,
                        closed_at TEXT,
                        batch_data TEXT NOT NULL
                    )
                    """
                )
                self._ensure_column(conn, "after_action_report_review_batches", "scheduled_review_task_id", "TEXT")
                self._ensure_column(conn, "after_action_report_review_batches", "person_id", "TEXT")
                self._ensure_column(
                    conn, "after_action_report_review_batches", "identity_verified", "INTEGER NOT NULL DEFAULT 0"
                )
                # A model may reuse a report id in another person's work.
                # Null-person work needs its own index because SQL uniqueness
                # otherwise permits several NULL values. Unverified old rows
                # must not prevent a new, verified report from being saved.
                conn.execute("DROP INDEX IF EXISTS idx_aar_reviews_owner_report")
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_aar_reviews_owner_person_report
                    ON after_action_report_reviews(owner_agent_id, person_id, identity_verified, report_id)
                    WHERE person_id IS NOT NULL
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_aar_reviews_owner_nobody_report
                    ON after_action_report_reviews(owner_agent_id, identity_verified, report_id)
                    WHERE person_id IS NULL
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_aar_reviews_owner_status
                    ON after_action_report_reviews(owner_agent_id, status, updated_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_aar_reviews_parent_task
                    ON after_action_report_reviews(parent_task_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_aar_reviews_batch
                    ON after_action_report_reviews(owner_agent_id, batch_id)
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_aar_batches_owner_batch
                    ON after_action_report_review_batches(owner_agent_id, batch_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_aar_batches_owner_parent_status
                    ON after_action_report_review_batches(owner_agent_id, parent_task_id, status, updated_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_aar_batches_schedule
                    ON after_action_report_review_batches(owner_agent_id, scheduled_message_id)
                    """
                )
                # A removed person's reviews and batches are deleted by person
                # (storage.person_departure_repository).
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_aar_reviews_person
                    ON after_action_report_reviews(person_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_aar_batches_person
                    ON after_action_report_review_batches(person_id)
                    """
                )
                conn.commit()
            self._logger.info("%s after-action-report repository initialized at %s", backend_name, storage_target)
        except Exception as exc:
            self._logger.error("Failed to initialize %s after-action-report repository: %s", backend_name, exc)
            raise


    def upsert_review(self, record: AfterActionReportReviewRecord) -> AfterActionReportReviewRecord:
        owner_agent_id = self._require_owner_agent_id(record.owner_agent_id)
        existing_by_report = self.get_review_by_report_id(
            record.report_id, owner_agent_id=owner_agent_id, person_id=record.person_id
        )
        if existing_by_report and record.status != AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW:
            return existing_by_report
        if existing_by_report and existing_by_report.status != AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW:
            return existing_by_report
        if existing_by_report:
            record.review_id = existing_by_report.review_id
            record.created_at = existing_by_report.created_at

        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO after_action_report_reviews (
                    review_record_id, review_id, report_id, batch_id, owner_agent_id,
                    owner_agent_name, team_id, owner_named_agent_type,
                    source_task_id, parent_task_id, source_agent_id,
                    source_agent_name, source_named_agent_type, person_id,
                    identity_verified, status,
                    created_at, updated_at, reviewed_at, stale_reason, review_data
                )
                VALUES ({", ".join([self._dialect.bind] * 21)})
                ON CONFLICT(review_record_id) DO UPDATE SET
                    report_id = excluded.report_id,
                    batch_id = excluded.batch_id,
                    owner_agent_name = excluded.owner_agent_name,
                    team_id = excluded.team_id,
                    owner_named_agent_type = excluded.owner_named_agent_type,
                    source_task_id = excluded.source_task_id,
                    parent_task_id = excluded.parent_task_id,
                    source_agent_id = excluded.source_agent_id,
                    source_agent_name = excluded.source_agent_name,
                    source_named_agent_type = excluded.source_named_agent_type,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    reviewed_at = excluded.reviewed_at,
                    stale_reason = excluded.stale_reason,
                    review_data = excluded.review_data
                WHERE after_action_report_reviews.identity_verified = excluded.identity_verified
                    AND (after_action_report_reviews.person_id = excluded.person_id
                        OR (after_action_report_reviews.person_id IS NULL AND excluded.person_id IS NULL))
                """,
                self._row_values(record),
            )
            if cursor.rowcount != 1:
                raise ValueError("An after-action report review cannot change whose work it describes.")
            conn.commit()
        return record

    def _row_values(self, record: AfterActionReportReviewRecord) -> tuple[object, ...]:
        return (
            self._review_record_id(record),
            record.review_id,
            record.report_id,
            record.batch_id,
            record.owner_agent_id,
            record.owner_agent_name,
            record.team_id,
            record.owner_named_agent_type,
            record.source_task_id,
            record.parent_task_id,
            record.source_agent_id,
            record.source_agent_name,
            record.source_named_agent_type,
            record.person_id,
            int(record.identity_verified),
            record.status.value,
            record.created_at,
            record.updated_at,
            record.reviewed_at,
            record.stale_reason,
            record.model_dump_json(),
        )

    def get_review(
        self,
        review_id: str,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
    ) -> Optional[AfterActionReportReviewRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        person_clause, person_params = self._person_clause(person_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT review_data
                FROM after_action_report_reviews
                WHERE review_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind} AND {person_clause}
                LIMIT 1
                """,
                (review_id, owner_agent_id, *person_params),
            ).fetchone()
        if not row:
            return None
        return AfterActionReportReviewRecord.model_validate_json(row[0])

    def get_review_by_report_id(
        self,
        report_id: str,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
    ) -> Optional[AfterActionReportReviewRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        person_clause, person_params = self._person_clause(person_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT review_data
                FROM after_action_report_reviews
                WHERE report_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind} AND {person_clause}
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (report_id, owner_agent_id, *person_params),
            ).fetchone()
        if not row:
            return None
        return AfterActionReportReviewRecord.model_validate_json(row[0])

    def list_reviews(
        self,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
        status: Optional[AfterActionReportReviewStatus],
        limit: int,
    ) -> List[AfterActionReportReviewRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        safe_limit = max(1, min(int(limit or 20), 100))
        person_clause, person_params = self._person_clause(person_id)
        params: list[object] = [owner_agent_id, *person_params]
        query = f"""
            SELECT review_data
            FROM after_action_report_reviews
            WHERE owner_agent_id = {self._dialect.bind} AND {person_clause}
        """
        if status is not None:
            query += f" AND status = {self._dialect.bind}"
            params.append(status.value)
        query += f" ORDER BY updated_at DESC LIMIT {self._dialect.bind}"
        params.append(safe_limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [AfterActionReportReviewRecord.model_validate_json(row[0]) for row in rows if row and row[0]]

    def list_reviews_for_batch(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
    ) -> List[AfterActionReportReviewRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT review_data
                FROM after_action_report_reviews
                WHERE owner_agent_id = {self._dialect.bind} AND batch_id = {self._dialect.bind}
                ORDER BY created_at ASC
                """,
                (owner_agent_id, batch_id),
            ).fetchall()
        return [AfterActionReportReviewRecord.model_validate_json(row[0]) for row in rows if row and row[0]]

    def resolve_review(
        self,
        review_id: str,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
        status: AfterActionReportReviewStatus,
        decision_summary: str,
        review_rationale: Optional[str],
        resolution_history_entry_id: Optional[str],
        reviewed_at: str,
    ) -> Optional[AfterActionReportReviewRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        record = self.get_review(review_id, owner_agent_id=owner_agent_id, person_id=person_id)
        if record is None:
            return None
        record.status = status
        record.decision_summary = decision_summary
        record.review_rationale = review_rationale
        record.resolution_history_entry_id = resolution_history_entry_id
        record.reviewed_at = reviewed_at
        record.updated_at = reviewed_at
        self._write_review_state(record)
        return record

    def ignore_unverified_pending_reviews(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
        stale_reason: str,
        decision_summary: str,
        reviewed_at: str,
    ) -> int:
        """Close the batch's pending reviews saved without a verified person; how many were closed."""

        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        closed = 0
        for record in self.list_reviews_for_batch(batch_id, owner_agent_id=owner_agent_id):
            if record.identity_verified or record.status != AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW:
                continue
            record.status = AfterActionReportReviewStatus.IGNORED
            record.stale_reason = stale_reason
            record.decision_summary = decision_summary
            record.reviewed_at = reviewed_at
            record.updated_at = reviewed_at
            self._write_review_state(record)
            closed += 1
        return closed

    def _write_review_state(self, record: AfterActionReportReviewRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE after_action_report_reviews
                SET status = {self._dialect.bind}, updated_at = {self._dialect.bind}, reviewed_at = {self._dialect.bind}, stale_reason = {self._dialect.bind}, review_data = {self._dialect.bind}
                WHERE review_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                """,
                (
                    record.status.value,
                    record.updated_at,
                    record.reviewed_at,
                    record.stale_reason,
                    record.model_dump_json(),
                    record.review_id,
                    record.owner_agent_id,
                ),
            )
            conn.commit()

    def upsert_batch(self, record: AfterActionReportReviewBatchRecord) -> AfterActionReportReviewBatchRecord:
        self._require_owner_agent_id(record.owner_agent_id)
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO after_action_report_review_batches (
                    batch_record_id, batch_id, owner_agent_id, owner_agent_name,
                    team_id, owner_named_agent_type, parent_task_id, person_id,
                    identity_verified, status,
                    opened_at, accept_until, scheduled_review_at, scheduled_message_id,
                    scheduled_review_task_id,
                    final_history_entry_id, report_count, stale_report_count,
                    created_at, updated_at, closed_at, batch_data
                )
                VALUES ({", ".join([self._dialect.bind] * 22)})
                ON CONFLICT(batch_record_id) DO UPDATE SET
                    owner_agent_name = excluded.owner_agent_name,
                    team_id = excluded.team_id,
                    owner_named_agent_type = excluded.owner_named_agent_type,
                    parent_task_id = excluded.parent_task_id,
                    person_id = excluded.person_id,
                    identity_verified = excluded.identity_verified,
                    status = excluded.status,
                    opened_at = excluded.opened_at,
                    accept_until = excluded.accept_until,
                    scheduled_review_at = excluded.scheduled_review_at,
                    scheduled_message_id = excluded.scheduled_message_id,
                    scheduled_review_task_id = excluded.scheduled_review_task_id,
                    final_history_entry_id = excluded.final_history_entry_id,
                    report_count = excluded.report_count,
                    stale_report_count = excluded.stale_report_count,
                    updated_at = excluded.updated_at,
                    closed_at = excluded.closed_at,
                    batch_data = excluded.batch_data
                """,
                self._batch_row_values(record),
            )
            conn.commit()
        return record

    def _batch_row_values(self, record: AfterActionReportReviewBatchRecord) -> tuple[object, ...]:
        return (
            self._batch_record_id(record),
            record.batch_id,
            record.owner_agent_id,
            record.owner_agent_name,
            record.team_id,
            record.owner_named_agent_type,
            record.parent_task_id,
            record.person_id,
            int(record.identity_verified),
            record.status.value,
            record.opened_at,
            record.accept_until,
            record.scheduled_review_at,
            record.scheduled_message_id,
            record.scheduled_review_task_id,
            record.final_history_entry_id,
            record.report_count,
            record.stale_report_count,
            record.created_at,
            record.updated_at,
            record.closed_at,
            record.model_dump_json(),
        )

    def get_batch(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
    ) -> Optional[AfterActionReportReviewBatchRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT batch_data
                FROM after_action_report_review_batches
                WHERE batch_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                LIMIT 1
                """,
                (batch_id, owner_agent_id),
            ).fetchone()
        if not row:
            return None
        return AfterActionReportReviewBatchRecord.model_validate_json(row[0])

    def get_latest_batch_for_task(
        self,
        parent_task_id: str,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
    ) -> Optional[AfterActionReportReviewBatchRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        person_clause, person_params = self._person_clause(person_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT batch_data
                FROM after_action_report_review_batches
                WHERE parent_task_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind} AND {person_clause}
                ORDER BY opened_at DESC
                LIMIT 1
                """,
                (parent_task_id, owner_agent_id, *person_params),
            ).fetchone()
        if not row:
            return None
        return AfterActionReportReviewBatchRecord.model_validate_json(row[0])

    def get_open_batch_for_task(
        self,
        parent_task_id: str,
        *,
        owner_agent_id: str,
        person_id: Optional[str],
    ) -> Optional[AfterActionReportReviewBatchRecord]:
        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        person_clause, person_params = self._person_clause(person_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT batch_data
                FROM after_action_report_review_batches
                WHERE parent_task_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind} AND status = {self._dialect.bind} AND {person_clause}
                ORDER BY opened_at DESC
                LIMIT 1
                """,
                (parent_task_id, owner_agent_id, AfterActionReportReviewBatchStatus.OPEN.value, *person_params),
            ).fetchone()
        if not row:
            return None
        return AfterActionReportReviewBatchRecord.model_validate_json(row[0])

    def list_open_batches_for_task(
        self,
        parent_task_id: str,
        *,
        owner_agent_id: str,
    ) -> List[AfterActionReportReviewBatchRecord]:
        """Every open batch of the task, whoever it is for (verified or not): the task's completion handles each."""

        owner_agent_id = self._require_owner_agent_id(owner_agent_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT batch_data
                FROM after_action_report_review_batches
                WHERE parent_task_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind} AND status = {self._dialect.bind}
                ORDER BY opened_at ASC
                """,
                (parent_task_id, owner_agent_id, AfterActionReportReviewBatchStatus.OPEN.value),
            ).fetchall()
        return [AfterActionReportReviewBatchRecord.model_validate_json(row[0]) for row in rows if row and row[0]]

    def mark_batch_scheduled(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
        scheduled_message_id: str,
        scheduled_review_task_id: str,
        updated_at: str,
    ) -> Optional[AfterActionReportReviewBatchRecord]:
        record = self.get_batch(batch_id, owner_agent_id=owner_agent_id)
        if record is None:
            return None
        record.scheduled_message_id = scheduled_message_id
        record.scheduled_review_task_id = scheduled_review_task_id
        record.updated_at = updated_at
        return self.upsert_batch(record)

    def claim_batch_for_finalizing(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
        updated_at: str,
    ) -> Optional[AfterActionReportReviewBatchRecord]:
        record = self.get_batch(batch_id, owner_agent_id=owner_agent_id)
        if record is None:
            return None
        if record.status == AfterActionReportReviewBatchStatus.CLOSED:
            return record
        record.status = AfterActionReportReviewBatchStatus.FINALIZING
        record.updated_at = updated_at
        return self.upsert_batch(record)

    def close_batch(
        self,
        batch_id: str,
        *,
        owner_agent_id: str,
        final_history_entry_id: Optional[str],
        report_count: int,
        stale_report_count: int,
        closed_at: str,
    ) -> Optional[AfterActionReportReviewBatchRecord]:
        record = self.get_batch(batch_id, owner_agent_id=owner_agent_id)
        if record is None:
            return None
        record.status = AfterActionReportReviewBatchStatus.CLOSED
        record.final_history_entry_id = final_history_entry_id
        record.report_count = report_count
        record.stale_report_count = stale_report_count
        record.closed_at = closed_at
        record.updated_at = closed_at
        return self.upsert_batch(record)


class SqliteAfterActionReportRepository(_RelationalAfterActionReportRepository):
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


class PostgresAfterActionReportRepository(_RelationalAfterActionReportRepository):
    """Postgres implementation for manager-owned AAR review state."""

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


def create_after_action_report_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> AfterActionReportRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="AfterActionReportRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqliteAfterActionReportRepository(storage_settings.sqlite_path, logger=logger)
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresAfterActionReportRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(
        f"AfterActionReportRepository has no implementation for backend {storage_settings.backend!r}."
    )
