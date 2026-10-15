"""Durable record of tasks returned by workers that lost their launch lease.

The TeamLead saves each handback before acting on it, so a restart or a
redelivered message never loses or duplicates returned work. The handback id
is deterministic per worker launch and task (worker_handback_v1), which makes
the insert idempotent.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from custom_messages.contracts.worker_handback_v1 import WorkerTaskHandbackV1
from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite

# A handback is saved as received, then settles into exactly one outcome.
HANDBACK_STATUS_RECEIVED = "received"
HANDBACK_STATUS_REDISPATCHED = "redispatched"
HANDBACK_STATUS_CLOSED_CANCELLED = "closed_cancelled"
HANDBACK_STATUS_CLOSED_FINISHED = "closed_finished"
# Scheduled runs: the schedule already ran again or its next run is due soon.
HANDBACK_STATUS_SUPERSEDED = "superseded"
# Scheduled runs: the schedule was paused or disabled after the run started.
HANDBACK_STATUS_CLOSED_SCHEDULE_INACTIVE = "closed_schedule_inactive"
# Interrupted too many times in a row: cancelled and sent to the TeamLead.
HANDBACK_STATUS_ESCALATED = "escalated"
HANDBACK_STATUSES = frozenset(
    {
        HANDBACK_STATUS_RECEIVED,
        HANDBACK_STATUS_REDISPATCHED,
        HANDBACK_STATUS_CLOSED_CANCELLED,
        HANDBACK_STATUS_CLOSED_FINISHED,
        HANDBACK_STATUS_SUPERSEDED,
        HANDBACK_STATUS_CLOSED_SCHEDULE_INACTIVE,
        HANDBACK_STATUS_ESCALATED,
    }
)


class WorkerHandbackRepository:
    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if not settings.postgres_dsn and not settings.sqlite_path:
            raise ValueError("Worker handback records require configured storage.")
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS worker_task_handbacks (
                handback_id TEXT PRIMARY KEY, team_id TEXT NOT NULL,
                task_id TEXT NOT NULL, worker_agent_id TEXT NOT NULL,
                disposition TEXT NOT NULL, status TEXT NOT NULL,
                received_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS worker_task_handbacks_task "
                "ON worker_task_handbacks(team_id, task_id, received_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS worker_task_handbacks_status "
                "ON worker_task_handbacks(team_id, status, updated_at)"
            )
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    def record(self, handback: WorkerTaskHandbackV1) -> bool:
        """Save a received handback; return False if it was already saved."""

        p = self.dialect.bind
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO worker_task_handbacks(handback_id,team_id,task_id,"
                "worker_agent_id,disposition,status,received_at,updated_at,payload_json) "
                f"VALUES ({','.join([p] * 9)}) ON CONFLICT DO NOTHING",
                (
                    handback.handback_id,
                    handback.team_id,
                    handback.handed_back.task_id,
                    handback.worker_agent_id,
                    handback.handed_back.disposition,
                    HANDBACK_STATUS_RECEIVED,
                    now,
                    now,
                    handback.model_dump_json(),
                ),
            )
            conn.commit()
            return bool(cursor.rowcount)

    def list_for_task(self, team_id: str, task_id: str) -> tuple[WorkerTaskHandbackV1, ...]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM worker_task_handbacks "
                f"WHERE team_id={p} AND task_id={p} ORDER BY received_at,handback_id",
                (team_id, task_id),
            ).fetchall()
        return tuple(WorkerTaskHandbackV1.model_validate_json(row[0]) for row in rows)

    def list_pending(
        self, team_id: str, *, updated_before: datetime
    ) -> tuple[WorkerTaskHandbackV1, ...]:
        """Received handbacks not touched since ``updated_before`` (retry pacing)."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM worker_task_handbacks "
                f"WHERE team_id={p} AND status={p} AND updated_at<={p} "
                "ORDER BY received_at,handback_id",
                (team_id, HANDBACK_STATUS_RECEIVED, updated_before.isoformat()),
            ).fetchall()
        return tuple(WorkerTaskHandbackV1.model_validate_json(row[0]) for row in rows)

    def list_redispatched(
        self, team_id: str, *, updated_before: datetime
    ) -> tuple[tuple[WorkerTaskHandbackV1, datetime], ...]:
        """Handbacks sent out again but not touched since ``updated_before``, with that time.

        The stuck-task check (``custom_agents.stuck_tasks``) reports one whose
        task no agent holds: nobody picked it up.
        """

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json, updated_at FROM worker_task_handbacks "
                f"WHERE team_id={p} AND status={p} AND updated_at<={p} "
                "ORDER BY received_at,handback_id",
                (team_id, HANDBACK_STATUS_REDISPATCHED, updated_before.isoformat()),
            ).fetchall()
        return tuple(
            (WorkerTaskHandbackV1.model_validate_json(row[0]), datetime.fromisoformat(str(row[1])))
            for row in rows
        )

    def status(self, handback_id: str, team_id: str) -> str | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM worker_task_handbacks "
                f"WHERE handback_id={p} AND team_id={p}",
                (handback_id, team_id),
            ).fetchone()
        return str(row[0]) if row else None

    def set_status(self, handback_id: str, team_id: str, status: str) -> None:
        if status not in HANDBACK_STATUSES:
            raise ValueError(f"unknown handback status: {status}")
        self._touch(handback_id, team_id, status=status)

    def touch(self, handback_id: str, team_id: str) -> None:
        """Record a processing attempt so pending retries are paced."""

        self._touch(handback_id, team_id, status=None)

    def _touch(self, handback_id: str, team_id: str, *, status: str | None) -> None:
        p = self.dialect.bind
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            if status is None:
                conn.execute(
                    f"UPDATE worker_task_handbacks SET updated_at={p} "
                    f"WHERE handback_id={p} AND team_id={p}",
                    (now, handback_id, team_id),
                )
            else:
                conn.execute(
                    f"UPDATE worker_task_handbacks SET status={p},updated_at={p} "
                    f"WHERE handback_id={p} AND team_id={p}",
                    (status, now, handback_id, team_id),
                )
            conn.commit()

    def interruption_count(self, team_id: str, task_id: str) -> int:
        """How many runs of this task were interrupted by a lost lease.

        Task ids are unique per request and a finished task is never handed
        back again, so every interrupted handback for a task id is part of one
        unbroken streak.
        """

        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM worker_task_handbacks "
                f"WHERE team_id={p} AND task_id={p} AND disposition={p}",
                (team_id, task_id, "interrupted"),
            ).fetchone()
        return int(row[0]) if row else 0

    def interrupted_for_tasks(
        self, team_id: str, task_ids: frozenset[str]
    ) -> tuple[WorkerTaskHandbackV1, ...]:
        """Interrupted handbacks for any of these tasks, oldest first."""

        if not task_ids:
            return ()
        p = self.dialect.bind
        ordered = sorted(task_ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM worker_task_handbacks "
                f"WHERE team_id={p} AND disposition={p} "
                f"AND task_id IN ({','.join([p] * len(ordered))}) "
                "ORDER BY received_at,handback_id",
                (team_id, "interrupted", *ordered),
            ).fetchall()
        return tuple(WorkerTaskHandbackV1.model_validate_json(row[0]) for row in rows)
