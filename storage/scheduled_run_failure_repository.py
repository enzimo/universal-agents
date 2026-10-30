"""Pending failure notices saved with a scheduled task's terminal transaction.

Task archival immediately removes the live snapshot. This small outbox keeps
only attribution and routing, never the task's private instructions or error
output. The lead verifies the original identity before delivering a notice.
Reported rows retain their key for deduplication and discard their routing.
This operational state does not move in a team transfer.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import TaskSnapshotRecord
from custom_messages.enums import ProgressState, RequesterType
from storage.config import StorageSettings
from storage.database_clock import database_now
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.read_only import repositories_are_read_only
from storage.sql_dialect import POSTGRES, SQLITE, SqlDialect
from storage.sqlite_connection import connect_sqlite


class ScheduledRunFailureV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    team_id: str
    agent_id: str
    header: MessageHeader
    meta: MessageMeta
    failed_at_utc: datetime


class ScheduledRunFailureRepository:
    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if not repositories_are_read_only():
            ensure_storage_parent_dir(settings.sqlite_path)
            with self._connect() as conn:
                self.ensure_schema(conn)
                conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    @staticmethod
    def ensure_schema(conn: Any) -> None:
        conn.execute("""CREATE TABLE IF NOT EXISTS scheduled_run_failures (
            team_id TEXT NOT NULL, task_id TEXT NOT NULL, payload_json TEXT,
            PRIMARY KEY (team_id, task_id)
        )""")

    @staticmethod
    def record(conn: Any, dialect: SqlDialect, task: TaskSnapshotRecord, team_id: str | None, agent_id: str) -> None:
        """Join the snapshot transaction; no authority is inferred here."""
        if (not team_id or task.state != ProgressState.FAILED or task.task.parent_task_id is not None
                or task.meta.requester_type != RequesterType.TIMER_REQUESTER):
            return
        failure = ScheduledRunFailureV1(
            task_id=task.task_id, team_id=team_id, agent_id=agent_id, header=task.header, meta=task.meta,
            failed_at_utc=database_now(conn),
        )
        p = dialect.bind
        conn.execute(
            f"INSERT INTO scheduled_run_failures(team_id,task_id,payload_json) VALUES ({p},{p},{p}) "
            "ON CONFLICT(team_id,task_id) DO NOTHING",
            (team_id, task.task_id, failure.model_dump_json()),
        )

    def pending(self, team_id: str, *, limit: int = 100) -> tuple[ScheduledRunFailureV1, ...]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM scheduled_run_failures WHERE team_id={p} "
                f"AND payload_json IS NOT NULL ORDER BY task_id LIMIT {p}",
                (team_id, limit),
            ).fetchall()
        return tuple(ScheduledRunFailureV1.model_validate_json(row[0]) for row in rows)

    def reported(self, team_id: str, task_id: str) -> None:
        """Keep the delivery key, release the saved identity and routing."""
        p = self.dialect.bind
        with self._connect() as conn:
            conn.execute(
                f"UPDATE scheduled_run_failures SET payload_json=NULL WHERE team_id={p} AND task_id={p}",
                (team_id, task_id),
            )
            conn.commit()
