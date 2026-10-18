"""Backend-neutral repository boundary for scheduled-message persistence."""

from __future__ import annotations

from storage.read_only import repositories_are_read_only

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import logging
import sqlite3
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
from .runtime_custody import DeferredMessageClaim, check_deferred_message
from .sqlite_connection import connect_sqlite


class ScheduleRepository(Protocol):
    def add_schedule_row(
        self,
        *,
        schedule_id: str,
        payload_json: str,
        schedule_json: Optional[str],
        schedule_name: Optional[str],
        objective_id: Optional[str],
        interval_ms: Optional[int],
        cron_expression: Optional[str],
        repeat_count: Optional[int],
        remaining_count: Optional[int],
        next_fire_at: str,
        paused_until: Optional[str],
        expires_at_utc: Optional[str] = None,
        created_at: str,
        enabled: bool,
        claim_team_id: Optional[str] = None,
        claim_named_agent_type: Optional[str] = None,
        claim_agent_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
        deferred_message: DeferredMessageClaim | None = None,
    ) -> None: ...

    def add_schedule_row_if_absent(
        self,
        *,
        schedule_id: str,
        payload_json: str,
        schedule_json: Optional[str],
        schedule_name: Optional[str],
        objective_id: Optional[str],
        interval_ms: Optional[int],
        cron_expression: Optional[str],
        repeat_count: Optional[int],
        remaining_count: Optional[int],
        next_fire_at: str,
        paused_until: Optional[str],
        expires_at_utc: Optional[str] = None,
        created_at: str,
        enabled: bool,
        claim_team_id: Optional[str] = None,
        claim_named_agent_type: Optional[str] = None,
        claim_agent_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> bool: ...

    def remove(self, schedule_id: str) -> bool: ...

    def get_schedule_row(self, schedule_id: str) -> Optional[tuple]: ...

    def update_after_fire(
        self,
        *,
        schedule_id: str,
        next_fire_at: Optional[str],
        remaining_count: Optional[int],
        claim_token: Optional[str] = None,
        claimer_agent_id: Optional[str] = None,
    ) -> bool: ...

    def get_all_enabled(self) -> List[dict]: ...

    def get_all(self) -> List[dict]: ...

    def get_next_due(self, *, now_iso: str) -> Optional[dict]: ...

    def claim_next_due(
        self,
        *,
        now_iso: str,
        claim_expires_at: str,
        claimer_agent_id: str,
        claimer_named_agent_type: Optional[str],
        claimer_team_id: Optional[str],
        claim_token: str,
        allow_unscoped: bool = False,
        exclude_schedule_ids: tuple[str, ...] = (),
    ) -> Optional[dict]: ...

    def release_claim(
        self,
        *,
        schedule_id: str,
        claim_token: str,
        claimer_agent_id: str,
    ) -> bool: ...

    def prune_expired(self, *, now_iso: str) -> int: ...

    def get_next_wake_time(self) -> Optional[str]: ...

    def set_enabled(self, schedule_id: str, enabled: bool, schedule_json: Optional[str] = None) -> bool: ...

    def set_paused_until(self, schedule_id: str, paused_until: Optional[str], schedule_json: str) -> bool: ...

    def set_timing(self, schedule_id: str, *, schedule_json: str, next_fire_at: str) -> bool: ...

    def count_enabled(self) -> int: ...

    def rows_needing_run_permit(self, *, now_iso: str, limit: int) -> List[str]: ...

    def set_run_permit(
        self,
        schedule_id: str,
        *,
        permit_json: Optional[str],
        checked_owner: Optional[str],
        recheck_at: str,
    ) -> bool: ...


_SCHEDULE_COLUMN_MIGRATIONS: dict[str, str] = {
    "objective_id": "TEXT",
    "claim_team_id": "TEXT",
    "claim_named_agent_type": "TEXT",
    "claim_agent_id": "TEXT",
    "claimed_by_agent_id": "TEXT",
    "claim_token": "TEXT",
    "claim_expires_at": "TEXT",
    "claimed_at": "TEXT",
    "expires_at_utc": "TEXT",
    "owner_canonical_user_id": "TEXT",
    # The schedule's run permit (custom_agents.schedule_permits): runtime
    # bookkeeping like next_fire_at, never part of schedule_json or of the
    # schedule's control state. run_permit_owner is the row owner the manager
    # last checked for; run_permit_recheck_at is when it looks again.
    "run_permit_json": "TEXT",
    "run_permit_owner": "TEXT",
    "run_permit_recheck_at": "TEXT",
}


class _RelationalScheduleRepository:
    """Shared record operations for scheduled-message persistence."""

    _dialect: SqlDialect


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
                    CREATE TABLE IF NOT EXISTS scheduled_messages (
                        schedule_id TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        schedule_json TEXT,
                        schedule_name TEXT,
                        objective_id TEXT,
                        interval_ms INTEGER,
                        cron_expression TEXT,
                        repeat_count INTEGER,
                        remaining_count INTEGER,
                        next_fire_at TEXT NOT NULL,
                        paused_until TEXT,
                        expires_at_utc TEXT,
                        created_at TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        claim_team_id TEXT,
                        claim_named_agent_type TEXT,
                        claim_agent_id TEXT,
                        owner_canonical_user_id TEXT,
                        claimed_by_agent_id TEXT,
                        claim_token TEXT,
                        claim_expires_at TEXT,
                        claimed_at TEXT,
                        run_permit_json TEXT,
                        run_permit_owner TEXT,
                        run_permit_recheck_at TEXT
                    )
                    """
                )
                self._ensure_schedule_columns(conn, backend_name)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scheduled_messages_objective_id ON scheduled_messages(objective_id)"
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_scheduled_messages_due_claim
                    ON scheduled_messages(
                        enabled,
                        next_fire_at,
                        claim_team_id,
                        claim_named_agent_type,
                        claim_agent_id,
                        claim_expires_at
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scheduled_messages_claim_token ON scheduled_messages(claim_token)"
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_scheduled_messages_owner_user
                    ON scheduled_messages(owner_canonical_user_id, next_fire_at)
                    """
                )
                conn.commit()
            self._logger.info(f"{backend_name} schedule repository initialized at {storage_target}")
        except Exception as exc:
            self._logger.error(f"Failed to initialize {backend_name} schedule repository: {exc}")
            raise

    def _ensure_schedule_columns(self, conn: Any, backend_name: str) -> None:
        if backend_name == "Postgres":
            existing = {
                str(row["column_name"] if "column_name" in row else row[0])
                for row in conn.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'scheduled_messages'
                    """
                ).fetchall()
            }
        else:
            existing = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(scheduled_messages)").fetchall()
                if row and len(row) > 1
            }
        for column_name, column_type in _SCHEDULE_COLUMN_MIGRATIONS.items():
            if column_name in existing:
                continue
            self._add_schedule_column_if_missing(conn, backend_name, column_name, column_type)

    def _add_schedule_column_if_missing(
        self,
        conn: Any,
        backend_name: str,
        column_name: str,
        column_type: str,
    ) -> None:
        if backend_name == "Postgres":
            conn.execute(f"ALTER TABLE scheduled_messages ADD COLUMN IF NOT EXISTS {column_name} {column_type}")
            return
        try:
            conn.execute(f"ALTER TABLE scheduled_messages ADD COLUMN {column_name} {column_type}")
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "duplicate column" in message or "already exists" in message:
                self._logger.debug(
                    "Schedule column %s already existed during concurrent schema migration.",
                    column_name,
                )
                return
            raise


    def remove(self, schedule_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM scheduled_messages WHERE schedule_id = {self._dialect.bind}",
                (schedule_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_schedule_row(self, schedule_id: str) -> Optional[tuple]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT schedule_json, payload_json, schedule_name, interval_ms, cron_expression,
                       repeat_count, next_fire_at, paused_until, created_at, enabled, remaining_count,
                       objective_id, claim_team_id, claim_named_agent_type, claim_agent_id,
                       claimed_by_agent_id, claim_token, claim_expires_at, claimed_at, expires_at_utc,
                       owner_canonical_user_id, run_permit_json
                FROM scheduled_messages WHERE schedule_id = {self._dialect.bind}
                """,
                (schedule_id,),
            ).fetchone()
        return tuple(row[index] for index in range(len(row))) if row else None

    def update_after_fire(
        self,
        *,
        schedule_id: str,
        next_fire_at: Optional[str],
        remaining_count: Optional[int],
        claim_token: Optional[str] = None,
        claimer_agent_id: Optional[str] = None,
    ) -> bool:
        with self._connect() as conn:
            claim_where = ""
            claim_params: tuple[Any, ...] = ()
            if claim_token and claimer_agent_id:
                claim_where = f" AND claim_token = {self._dialect.bind} AND claimed_by_agent_id = {self._dialect.bind}"
                claim_params = (claim_token, claimer_agent_id)
            if next_fire_at is None:
                cursor = conn.execute(
                    f"DELETE FROM scheduled_messages WHERE schedule_id = {self._dialect.bind}{claim_where}",
                    (schedule_id, *claim_params),
                )
                conn.commit()
                return cursor.rowcount > 0
            cursor = conn.execute(
                f"""
                UPDATE scheduled_messages
                   SET next_fire_at = {self._dialect.bind},
                       remaining_count = {self._dialect.bind},
                       paused_until = NULL,
                       claimed_by_agent_id = NULL,
                       claim_token = NULL,
                       claim_expires_at = NULL,
                       claimed_at = NULL
                 WHERE schedule_id = {self._dialect.bind}{claim_where}
                """,
                (next_fire_at, remaining_count, schedule_id, *claim_params),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_all_enabled(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_messages
                 WHERE enabled = 1
                 ORDER BY next_fire_at ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_all(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_messages
                ORDER BY
                    CASE
                        WHEN paused_until IS NOT NULL AND paused_until > next_fire_at THEN paused_until
                        ELSE next_fire_at
                    END ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_next_due(self, *, now_iso: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM scheduled_messages
                 WHERE enabled = 1
                   AND (paused_until IS NULL OR paused_until <= {self._dialect.bind})
                   AND next_fire_at <= {self._dialect.bind}
                   AND (expires_at_utc IS NULL OR expires_at_utc > {self._dialect.bind})
                   AND (
                       claimed_by_agent_id IS NULL
                       OR claim_expires_at IS NULL
                       OR claim_expires_at <= {self._dialect.bind}
                   )
                 ORDER BY next_fire_at ASC LIMIT 1
                """,
                (now_iso, now_iso, now_iso, now_iso),
            ).fetchone()
        return dict(row) if row else None

    def _excluded_claim_sql(self, schedule_ids: tuple[str, ...]) -> str:
        if not schedule_ids:
            return ""
        return f"AND schedule_id NOT IN ({','.join([self._dialect.bind] * len(schedule_ids))})"

    def _claim_eligibility_sql(self) -> str:
        return f"""
            enabled = 1
            AND (paused_until IS NULL OR paused_until <= {self._dialect.bind})
            AND next_fire_at <= {self._dialect.bind}
            AND (expires_at_utc IS NULL OR expires_at_utc > {self._dialect.bind})
            AND (
                claimed_by_agent_id IS NULL
                OR claim_expires_at IS NULL
                OR claim_expires_at <= {self._dialect.bind}
            )
            AND (
                claim_agent_id = {self._dialect.bind}
                OR (
                    claim_agent_id IS NULL
                    AND claim_named_agent_type IS NOT NULL
                    AND claim_named_agent_type = {self._dialect.bind}
                    AND (claim_team_id IS NULL OR claim_team_id = {self._dialect.bind})
                )
                OR (
                    {self._dialect.bind} = 1
                    AND claim_agent_id IS NULL
                    AND claim_named_agent_type IS NULL
                    AND (claim_team_id IS NULL OR claim_team_id = {self._dialect.bind})
                )
            )
        """

    def _claim_eligibility_params(
        self,
        *,
        now_iso: str,
        claimer_agent_id: str,
        claimer_named_agent_type: Optional[str],
        claimer_team_id: Optional[str],
        allow_unscoped: bool,
    ) -> tuple[Any, ...]:
        return (
            now_iso,
            now_iso,
            now_iso,
            now_iso,
            claimer_agent_id,
            claimer_named_agent_type,
            claimer_team_id,
            1 if allow_unscoped else 0,
            claimer_team_id,
        )


    def release_claim(
        self,
        *,
        schedule_id: str,
        claim_token: str,
        claimer_agent_id: str,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE scheduled_messages
                   SET claimed_by_agent_id = NULL,
                       claim_token = NULL,
                       claim_expires_at = NULL,
                       claimed_at = NULL
                 WHERE schedule_id = {self._dialect.bind}
                   AND claim_token = {self._dialect.bind}
                   AND claimed_by_agent_id = {self._dialect.bind}
                """,
                (schedule_id, claim_token, claimer_agent_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def prune_expired(self, *, now_iso: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                DELETE FROM scheduled_messages
                 WHERE expires_at_utc IS NOT NULL
                   AND expires_at_utc <= {self._dialect.bind}
                """,
                (now_iso,),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def get_next_wake_time(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    MIN(
                        CASE
                            WHEN expires_at_utc IS NOT NULL
                                 AND expires_at_utc < CASE
                                     WHEN paused_until IS NOT NULL AND paused_until > next_fire_at THEN paused_until
                                     ELSE next_fire_at
                                 END THEN expires_at_utc
                            WHEN paused_until IS NOT NULL AND paused_until > next_fire_at THEN paused_until
                            ELSE next_fire_at
                        END
                    )
                FROM scheduled_messages
                WHERE enabled = 1
                """
            ).fetchone()
        return row[0] if row else None

    def set_enabled(self, schedule_id: str, enabled: bool, schedule_json: Optional[str] = None) -> bool:
        with self._connect() as conn:
            if schedule_json is not None:
                cursor = conn.execute(
                    f"""
                    UPDATE scheduled_messages
                       SET enabled = {self._dialect.bind},
                           schedule_json = {self._dialect.bind},
                           claimed_by_agent_id = NULL,
                           claim_token = NULL,
                           claim_expires_at = NULL,
                           claimed_at = NULL
                     WHERE schedule_id = {self._dialect.bind}
                    """,
                    (1 if enabled else 0, schedule_json, schedule_id),
                )
            else:
                cursor = conn.execute(
                    f"""
                    UPDATE scheduled_messages
                       SET enabled = {self._dialect.bind},
                           claimed_by_agent_id = NULL,
                           claim_token = NULL,
                           claim_expires_at = NULL,
                           claimed_at = NULL
                     WHERE schedule_id = {self._dialect.bind}
                    """,
                    (1 if enabled else 0, schedule_id),
                )
            conn.commit()
            return cursor.rowcount > 0

    def set_paused_until(self, schedule_id: str, paused_until: Optional[str], schedule_json: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE scheduled_messages
                   SET paused_until = {self._dialect.bind},
                       schedule_json = {self._dialect.bind},
                       claimed_by_agent_id = NULL,
                       claim_token = NULL,
                       claim_expires_at = NULL,
                       claimed_at = NULL
                 WHERE schedule_id = {self._dialect.bind}
                """,
                (paused_until, schedule_json, schedule_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def set_timing(self, schedule_id: str, *, schedule_json: str, next_fire_at: str) -> bool:
        """Replace one schedule's record and next run time (``ScheduleStore.fill_missing_cron_timezones``)."""

        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE scheduled_messages
                   SET schedule_json = {self._dialect.bind},
                       next_fire_at = {self._dialect.bind}
                 WHERE schedule_id = {self._dialect.bind}
                """,
                (schedule_json, next_fire_at, schedule_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def count_enabled(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM scheduled_messages WHERE enabled = 1"
            ).fetchone()
        return row[0] if row else 0

    def rows_needing_run_permit(self, *, now_iso: str, limit: int) -> List[str]:
        """Enabled schedules whose run permit the manager should issue, renew or re-check.

        Never checked, due for a re-check (the permit expires within its
        renewal window, or a refusal's wait has passed), or owned by someone
        other than the owner last checked for. One query, oldest due first.
        """

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT schedule_id FROM scheduled_messages
                 WHERE enabled = 1
                   AND (run_permit_recheck_at IS NULL
                        OR run_permit_recheck_at <= {self._dialect.bind}
                        OR COALESCE(run_permit_owner, '') <> COALESCE(owner_canonical_user_id, ''))
                 ORDER BY run_permit_recheck_at ASC, schedule_id ASC
                 LIMIT {self._dialect.bind}
                """,
                (now_iso, int(limit)),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def set_run_permit(
        self,
        schedule_id: str,
        *,
        permit_json: Optional[str],
        checked_owner: Optional[str],
        recheck_at: str,
    ) -> bool:
        """Record the manager's check of a schedule's run permit (None when it issued none).

        Applies only while the row's owner is still ``checked_owner``; if the
        owner changed meanwhile, nothing is written and the next check sees it.
        """

        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE scheduled_messages
                   SET run_permit_json = {self._dialect.bind},
                       run_permit_owner = {self._dialect.bind},
                       run_permit_recheck_at = {self._dialect.bind}
                 WHERE schedule_id = {self._dialect.bind}
                   AND COALESCE(owner_canonical_user_id, '') = COALESCE({self._dialect.bind}, '')
                """,
                (permit_json, checked_owner, recheck_at, schedule_id, checked_owner),
            )
            conn.commit()
            return cursor.rowcount > 0


# A save replaces the whole row in SQLite (INSERT OR REPLACE), which would
# drop the run permit on every save, and runs that record schedule memory save
# the schedule each time. The permit is runtime bookkeeping, not part of what
# was saved, so the save keeps it (Postgres updates in place and keeps it too).
_KEPT_RUN_PERMIT_COLUMNS = ", ".join(
    f"(SELECT {column} FROM scheduled_messages WHERE schedule_id = ?)"
    for column in ("run_permit_json", "run_permit_owner", "run_permit_recheck_at")
)


class SqliteScheduleRepository(_RelationalScheduleRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._init_schema()


    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path, logger=self._logger, row_factory=sqlite3.Row)


    def add_schedule_row(
        self,
        *,
        schedule_id: str,
        payload_json: str,
        schedule_json: Optional[str],
        schedule_name: Optional[str],
        objective_id: Optional[str],
        interval_ms: Optional[int],
        cron_expression: Optional[str],
        repeat_count: Optional[int],
        remaining_count: Optional[int],
        next_fire_at: str,
        paused_until: Optional[str],
        expires_at_utc: Optional[str] = None,
        created_at: str,
        enabled: bool,
        claim_team_id: Optional[str] = None,
        claim_named_agent_type: Optional[str] = None,
        claim_agent_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
        deferred_message: DeferredMessageClaim | None = None,
    ) -> None:
        with self._connect() as conn:
            if deferred_message is not None:
                conn.execute("BEGIN IMMEDIATE")
                check_deferred_message(conn, self._dialect, deferred_message, consume=True, logger=self._logger)
            conn.execute(
                f"""
                INSERT OR REPLACE INTO scheduled_messages
                   (schedule_id, payload_json, schedule_name, objective_id, interval_ms, cron_expression,
                    schedule_json, repeat_count, remaining_count, next_fire_at, paused_until, created_at, enabled,
                    expires_at_utc, claim_team_id, claim_named_agent_type, claim_agent_id, owner_canonical_user_id,
                    claimed_by_agent_id, claim_token, claim_expires_at, claimed_at,
                    run_permit_json, run_permit_owner, run_permit_recheck_at)
                   VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, NULL, NULL, NULL,
                           {_KEPT_RUN_PERMIT_COLUMNS})
                """,
                (
                    schedule_id,
                    payload_json,
                    schedule_name,
                    objective_id,
                    interval_ms,
                    cron_expression,
                    schedule_json,
                    repeat_count,
                    remaining_count,
                    next_fire_at,
                    paused_until,
                    created_at,
                    1 if enabled else 0,
                    expires_at_utc,
                    claim_team_id,
                    claim_named_agent_type,
                    claim_agent_id,
                    owner_canonical_user_id,
                    schedule_id,
                    schedule_id,
                    schedule_id,
                ),
            )
            conn.commit()


    def add_schedule_row_if_absent(
        self,
        *,
        schedule_id: str,
        payload_json: str,
        schedule_json: Optional[str],
        schedule_name: Optional[str],
        objective_id: Optional[str],
        interval_ms: Optional[int],
        cron_expression: Optional[str],
        repeat_count: Optional[int],
        remaining_count: Optional[int],
        next_fire_at: str,
        paused_until: Optional[str],
        expires_at_utc: Optional[str] = None,
        created_at: str,
        enabled: bool,
        claim_team_id: Optional[str] = None,
        claim_named_agent_type: Optional[str] = None,
        claim_agent_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> bool:
        """Atomically insert a schedule without mutating an existing row."""

        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT OR IGNORE INTO scheduled_messages
                   (schedule_id, payload_json, schedule_name, objective_id, interval_ms, cron_expression,
                    schedule_json, repeat_count, remaining_count, next_fire_at, paused_until, created_at, enabled,
                    expires_at_utc, claim_team_id, claim_named_agent_type, claim_agent_id, owner_canonical_user_id,
                    claimed_by_agent_id, claim_token, claim_expires_at, claimed_at)
                   VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, NULL, NULL, NULL)
                """,
                (
                    schedule_id,
                    payload_json,
                    schedule_name,
                    objective_id,
                    interval_ms,
                    cron_expression,
                    schedule_json,
                    repeat_count,
                    remaining_count,
                    next_fire_at,
                    paused_until,
                    created_at,
                    1 if enabled else 0,
                    expires_at_utc,
                    claim_team_id,
                    claim_named_agent_type,
                    claim_agent_id,
                    owner_canonical_user_id,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0


    def claim_next_due(
        self,
        *,
        now_iso: str,
        claim_expires_at: str,
        claimer_agent_id: str,
        claimer_named_agent_type: Optional[str],
        claimer_team_id: Optional[str],
        claim_token: str,
        allow_unscoped: bool = False,
        exclude_schedule_ids: tuple[str, ...] = (),
    ) -> Optional[dict]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""
                SELECT * FROM scheduled_messages
                 WHERE {self._claim_eligibility_sql()}
                   {self._excluded_claim_sql(exclude_schedule_ids)}
                 ORDER BY next_fire_at ASC LIMIT 1
                """,
                self._claim_eligibility_params(
                    now_iso=now_iso,
                    claimer_agent_id=claimer_agent_id,
                    claimer_named_agent_type=claimer_named_agent_type,
                    claimer_team_id=claimer_team_id,
                    allow_unscoped=allow_unscoped,
                ) + exclude_schedule_ids,
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            schedule_id = str(row["schedule_id"])
            cursor = conn.execute(
                f"""
                UPDATE scheduled_messages
                   SET claimed_by_agent_id = {self._dialect.bind},
                       claim_token = {self._dialect.bind},
                       claim_expires_at = {self._dialect.bind},
                       claimed_at = {self._dialect.bind}
                 WHERE schedule_id = {self._dialect.bind}
                """,
                (
                    claimer_agent_id,
                    claim_token,
                    claim_expires_at,
                    now_iso,
                    schedule_id,
                ),
            )
            if cursor.rowcount <= 0:
                conn.rollback()
                return None
            claimed = conn.execute(
                f"SELECT * FROM scheduled_messages WHERE schedule_id = {self._dialect.bind}",
                (schedule_id,),
            ).fetchone()
            conn.commit()
        return dict(claimed) if claimed else None


class PostgresScheduleRepository(_RelationalScheduleRepository):
    """Postgres implementation for scheduled-message persistence."""

    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        validate_postgres_connection(self._dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)

    def add_schedule_row(
        self,
        *,
        schedule_id: str,
        payload_json: str,
        schedule_json: Optional[str],
        schedule_name: Optional[str],
        objective_id: Optional[str],
        interval_ms: Optional[int],
        cron_expression: Optional[str],
        repeat_count: Optional[int],
        remaining_count: Optional[int],
        next_fire_at: str,
        paused_until: Optional[str],
        expires_at_utc: Optional[str] = None,
        created_at: str,
        enabled: bool,
        claim_team_id: Optional[str] = None,
        claim_named_agent_type: Optional[str] = None,
        claim_agent_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
        deferred_message: DeferredMessageClaim | None = None,
    ) -> None:
        with self._connect() as conn:
            if deferred_message is not None:
                check_deferred_message(conn, self._dialect, deferred_message, consume=True, logger=self._logger)
            conn.execute(
                f"""
                INSERT INTO scheduled_messages
                   (schedule_id, payload_json, schedule_name, objective_id, interval_ms, cron_expression,
                    schedule_json, repeat_count, remaining_count, next_fire_at, paused_until, created_at, enabled,
                    expires_at_utc, claim_team_id, claim_named_agent_type, claim_agent_id, owner_canonical_user_id,
                    claimed_by_agent_id, claim_token, claim_expires_at, claimed_at)
                   VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, NULL, NULL, NULL)
                ON CONFLICT(schedule_id) DO UPDATE SET
                   payload_json = excluded.payload_json,
                   schedule_name = excluded.schedule_name,
                   objective_id = excluded.objective_id,
                   interval_ms = excluded.interval_ms,
                   cron_expression = excluded.cron_expression,
                   schedule_json = excluded.schedule_json,
                   repeat_count = excluded.repeat_count,
                   remaining_count = excluded.remaining_count,
                   next_fire_at = excluded.next_fire_at,
                   paused_until = excluded.paused_until,
                   created_at = excluded.created_at,
                   enabled = excluded.enabled,
                   expires_at_utc = excluded.expires_at_utc,
                   claim_team_id = excluded.claim_team_id,
                   claim_named_agent_type = excluded.claim_named_agent_type,
                   claim_agent_id = excluded.claim_agent_id,
                   owner_canonical_user_id = excluded.owner_canonical_user_id,
                   claimed_by_agent_id = NULL,
                   claim_token = NULL,
                   claim_expires_at = NULL,
                   claimed_at = NULL
                """,
                (
                    schedule_id,
                    payload_json,
                    schedule_name,
                    objective_id,
                    interval_ms,
                    cron_expression,
                    schedule_json,
                    repeat_count,
                    remaining_count,
                    next_fire_at,
                    paused_until,
                    created_at,
                    1 if enabled else 0,
                    expires_at_utc,
                    claim_team_id,
                    claim_named_agent_type,
                    claim_agent_id,
                    owner_canonical_user_id,
                ),
            )
            conn.commit()

    def add_schedule_row_if_absent(
        self,
        *,
        schedule_id: str,
        payload_json: str,
        schedule_json: Optional[str],
        schedule_name: Optional[str],
        objective_id: Optional[str],
        interval_ms: Optional[int],
        cron_expression: Optional[str],
        repeat_count: Optional[int],
        remaining_count: Optional[int],
        next_fire_at: str,
        paused_until: Optional[str],
        expires_at_utc: Optional[str] = None,
        created_at: str,
        enabled: bool,
        claim_team_id: Optional[str] = None,
        claim_named_agent_type: Optional[str] = None,
        claim_agent_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> bool:
        """Atomically insert a schedule without mutating an existing row."""

        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO scheduled_messages
                   (schedule_id, payload_json, schedule_name, objective_id, interval_ms, cron_expression,
                    schedule_json, repeat_count, remaining_count, next_fire_at, paused_until, created_at, enabled,
                    expires_at_utc, claim_team_id, claim_named_agent_type, claim_agent_id, owner_canonical_user_id,
                    claimed_by_agent_id, claim_token, claim_expires_at, claimed_at)
                   VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, NULL, NULL, NULL)
                ON CONFLICT(schedule_id) DO NOTHING
                """,
                (
                    schedule_id,
                    payload_json,
                    schedule_name,
                    objective_id,
                    interval_ms,
                    cron_expression,
                    schedule_json,
                    repeat_count,
                    remaining_count,
                    next_fire_at,
                    paused_until,
                    created_at,
                    1 if enabled else 0,
                    expires_at_utc,
                    claim_team_id,
                    claim_named_agent_type,
                    claim_agent_id,
                    owner_canonical_user_id,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def claim_next_due(
        self,
        *,
        now_iso: str,
        claim_expires_at: str,
        claimer_agent_id: str,
        claimer_named_agent_type: Optional[str],
        claimer_team_id: Optional[str],
        claim_token: str,
        allow_unscoped: bool = False,
        exclude_schedule_ids: tuple[str, ...] = (),
    ) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM scheduled_messages
                 WHERE {self._claim_eligibility_sql()}
                   {self._excluded_claim_sql(exclude_schedule_ids)}
                 ORDER BY next_fire_at ASC LIMIT 1
                 FOR UPDATE SKIP LOCKED
                """,
                self._claim_eligibility_params(
                    now_iso=now_iso,
                    claimer_agent_id=claimer_agent_id,
                    claimer_named_agent_type=claimer_named_agent_type,
                    claimer_team_id=claimer_team_id,
                    allow_unscoped=allow_unscoped,
                ) + exclude_schedule_ids,
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            schedule_id = str(row["schedule_id"])
            cursor = conn.execute(
                f"""
                UPDATE scheduled_messages
                   SET claimed_by_agent_id = {self._dialect.bind},
                       claim_token = {self._dialect.bind},
                       claim_expires_at = {self._dialect.bind},
                       claimed_at = {self._dialect.bind}
                 WHERE schedule_id = {self._dialect.bind}
                """,
                (
                    claimer_agent_id,
                    claim_token,
                    claim_expires_at,
                    now_iso,
                    schedule_id,
                ),
            )
            if cursor.rowcount <= 0:
                conn.rollback()
                return None
            claimed = conn.execute(
                f"SELECT * FROM scheduled_messages WHERE schedule_id = {self._dialect.bind}",
                (schedule_id,),
            ).fetchone()
            conn.commit()
        return dict(claimed) if claimed else None


def create_schedule_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> ScheduleRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="ScheduleRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqliteScheduleRepository(storage_settings.sqlite_path, logger=logger)
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresScheduleRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(
        f"ScheduleRepository has no implementation for backend {storage_settings.backend!r}."
    )
