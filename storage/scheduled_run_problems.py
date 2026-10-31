"""One open problem per schedule whose scheduled runs fail, saved with the run's terminal state.

A top-level scheduled run that ends records its outcome in the same
transaction as its task's terminal snapshot (``record_outcome``), because the
task is archived right after and its live snapshot is gone. A failed run opens
the schedule's problem, or keeps the open one as it is; a run that finishes
afterwards marks the problem recovered. The team lead reports the rows through
the stuck-work notices (``custom_agents.scheduled_run_failures``) and deletes
each once its people were told, so there is at most one row per schedule.

The row is keyed by the schedule id the run's signed identity names, read
here without checking the signature; the lead checks it before using the row.
It keeps the run's ids, routing and the schedule's name, never the task's
instructions or output. This operational state does not move in a team
transfer.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import TaskSnapshotRecord
from custom_messages.enums import ProgressState, RequesterType
from storage.config import StorageSettings
from storage.database_clock import database_now
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.read_only import repositories_are_read_only
from storage.sql_dialect import POSTGRES, SQLITE, SqlDialect
from storage.sqlite_connection import connect_sqlite

logger = logging.getLogger(__name__)


class ScheduledRunRecordV1(BaseModel):
    """One ended run: who ran it, the routing back to its person, and when it ended."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    agent_id: str
    header: MessageHeader
    meta: MessageMeta
    ended_at_utc: datetime
    # The schedule's name when the run ended, for display only: a failed
    # one-time reminder's schedule is removed right after its run.
    schedule_name: str | None = None


class ScheduledRunProblemV1(BaseModel):
    """A schedule's open problem: its first failed run, and the run that finished after it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str
    schedule_id: str
    failed: ScheduledRunRecordV1
    recovered: ScheduledRunRecordV1 | None = None


def claimed_schedule_id(meta: MessageMeta) -> str | None:
    """The schedule the run's identity names, unchecked; only a row key until the lead checks it."""

    gateway = meta.gateway_context
    if gateway is None:
        return None
    if gateway.schedule_run is not None:
        return gateway.schedule_run.permit.permit.schedule_id
    if gateway.task_context is not None:
        return gateway.task_context.context.schedule_id
    return None


class ScheduledRunProblems:
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
        conn.execute("""CREATE TABLE IF NOT EXISTS scheduled_run_problems (
            team_id TEXT NOT NULL, schedule_id TEXT NOT NULL, failed_task_id TEXT NOT NULL,
            failed_json TEXT NOT NULL, recovered_json TEXT,
            PRIMARY KEY (team_id, schedule_id)
        )""")

    @staticmethod
    def record_outcome(
        conn: Any, dialect: SqlDialect, task: TaskSnapshotRecord, team_id: str | None, agent_id: str,
    ) -> None:
        """Record a top-level scheduled run entering FAILED or COMPLETED, in the snapshot transaction.

        Call it only when the task enters that state, so a later save of an
        old failure cannot reopen a problem a newer run resolved. A failure
        starts a new problem only when none is open or the open one has
        recovered; a finished run marks the open problem recovered.
        """

        if (not team_id or task.task.parent_task_id is not None
                or task.meta.requester_type != RequesterType.TIMER_REQUESTER
                or task.state not in (ProgressState.FAILED, ProgressState.COMPLETED)):
            return
        schedule_id = claimed_schedule_id(task.meta)
        if not schedule_id:
            return
        p = dialect.bind
        record = ScheduledRunRecordV1(
            task_id=task.task_id, agent_id=agent_id, header=task.header, meta=task.meta,
            ended_at_utc=database_now(conn), schedule_name=_schedule_name(conn, dialect, schedule_id),
        )
        if task.state == ProgressState.COMPLETED:
            conn.execute(
                f"UPDATE scheduled_run_problems SET recovered_json={p} WHERE team_id={p} AND schedule_id={p}",
                (record.model_dump_json(), team_id, schedule_id),
            )
            return
        conn.execute(
            f"INSERT INTO scheduled_run_problems(team_id,schedule_id,failed_task_id,failed_json,recovered_json) "
            f"VALUES ({p},{p},{p},{p},NULL) ON CONFLICT(team_id,schedule_id) DO UPDATE SET "
            "failed_task_id=excluded.failed_task_id, failed_json=excluded.failed_json, recovered_json=NULL "
            "WHERE scheduled_run_problems.recovered_json IS NOT NULL",
            (team_id, schedule_id, task.task_id, record.model_dump_json()),
        )

    def problems(self, team_id: str) -> tuple[ScheduledRunProblemV1, ...]:
        """The team's problems; an unreadable row is logged and deleted so it cannot stop the reports."""
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT schedule_id, failed_json, recovered_json FROM scheduled_run_problems WHERE team_id={p} "
                "ORDER BY schedule_id",
                (team_id,),
            ).fetchall()
            problems: list[ScheduledRunProblemV1] = []
            for row in rows:
                try:
                    problems.append(_problem(team_id, row))
                except ValidationError:
                    logger.warning(
                        "Deleted a scheduled run problem that cannot be read. "
                        "event=scheduled_run_problem_unreadable team_id=%s schedule_id=%s",
                        team_id, row[0],
                    )
                    conn.execute(
                        f"DELETE FROM scheduled_run_problems WHERE team_id={p} AND schedule_id={p}", (team_id, row[0]),
                    )
            conn.commit()
        return tuple(problems)

    def problem(self, team_id: str, schedule_id: str) -> ScheduledRunProblemV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT schedule_id, failed_json, recovered_json FROM scheduled_run_problems "
                f"WHERE team_id={p} AND schedule_id={p}",
                (team_id, schedule_id),
            ).fetchone()
        return _problem(team_id, row) if row else None

    def clear_recovery(self, problem: ScheduledRunProblemV1) -> None:
        """Drop a recovery that does not check out; the problem stays open."""
        self._write(
            "UPDATE scheduled_run_problems SET recovered_json=NULL", problem,
        )

    def forget(self, problem: ScheduledRunProblemV1) -> None:
        """Delete the problem, unless a newer one replaced it meanwhile."""
        self._write("DELETE FROM scheduled_run_problems", problem)

    def _write(self, statement: str, problem: ScheduledRunProblemV1) -> None:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.execute(
                f"{statement} WHERE team_id={p} AND schedule_id={p} AND failed_task_id={p}",
                (problem.team_id, problem.schedule_id, problem.failed.task_id),
            )
            conn.commit()


def _schedule_name(conn: Any, dialect: SqlDialect, schedule_id: str) -> str | None:
    """The schedule's name now; None when it has none or there is no such schedule.

    A store used on its own (as in isolated tests) has no schedule table; a
    running team always has it.
    """

    if dialect == POSTGRES:
        exists = conn.execute("SELECT to_regclass(%s)", ("scheduled_messages",)).fetchone()[0] is not None
    else:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scheduled_messages'"
        ).fetchone() is not None
    if not exists:
        return None
    row = conn.execute(
        f"SELECT schedule_name FROM scheduled_messages WHERE schedule_id={dialect.bind}", (schedule_id,),
    ).fetchone()
    return row[0] if row else None


def _problem(team_id: str, row: Any) -> ScheduledRunProblemV1:
    return ScheduledRunProblemV1(
        team_id=team_id,
        schedule_id=str(row[0]),
        failed=ScheduledRunRecordV1.model_validate_json(row[1]),
        recovered=ScheduledRunRecordV1.model_validate_json(row[2]) if row[2] else None,
    )
