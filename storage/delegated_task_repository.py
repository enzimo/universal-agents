"""The requester's record of each task it delegated, and of its waits on them.

A delegated task waits in its worker type's shared queue until a free worker
takes it (ADR-019). The requester's runtime records here when it published the
task, which worker took it (``DelegatedTaskClaimedV1``), how often the task had
to move to another worker, and how it ended. Inspection tools, progress notes
and automatic retry read these rows without decoding runtime snapshots.

``delegation_waits`` holds one row per parent task that waits on delegated work:
an optional check-back time set by the requester's model and the last progress
note sent to the person.

The requester's runtime is the only writer for its own rows.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite

DelegatedTaskStatus = Literal["queued", "running", "finished", "failed", "cancelled"]
DELEGATED_QUEUED: DelegatedTaskStatus = "queued"
DELEGATED_RUNNING: DelegatedTaskStatus = "running"
DELEGATED_FINISHED: DelegatedTaskStatus = "finished"
DELEGATED_FAILED: DelegatedTaskStatus = "failed"
DELEGATED_CANCELLED: DelegatedTaskStatus = "cancelled"
OPEN_DELEGATED_STATUSES = frozenset({DELEGATED_QUEUED, DELEGATED_RUNNING})


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DelegatedTaskRecordV1(BaseModel):
    """One delegated task as its requester sees it."""

    model_config = ConfigDict(extra="forbid")

    team_id: str
    task_id: str
    parent_task_id: str
    root_task_id: str
    requester_agent_id: str
    named_agent_type: Optional[str] = None
    target_agent_name: Optional[str] = None
    status: DelegatedTaskStatus = DELEGATED_QUEUED
    published_at_utc: datetime
    # The work stream sequence of the published message, to withdraw a task no
    # worker has taken yet.
    publish_sequence: Optional[int] = None
    holder_agent_id: Optional[str] = None
    holder_agent_name: Optional[str] = None
    claimed_at_utc: Optional[datetime] = None
    # Every worker that took the task, oldest first.
    holders: list[str] = Field(default_factory=list)
    # How often the task moved to another worker because its worker was lost.
    interruptions: int = 0
    failure_category: Optional[str] = None
    finished_at_utc: Optional[datetime] = None
    updated_at_utc: datetime


class ClaimOutcome(BaseModel):
    """What recording a claim changed."""

    model_config = ConfigDict(extra="forbid")

    record: Optional[DelegatedTaskRecordV1]
    # The claim came from a different worker than the last one that held it.
    moved: bool = False
    # The record was already finished, failed or cancelled.
    already_closed: bool = False


class DelegationWaitV1(BaseModel):
    """A parent task waiting on its delegated tasks."""

    model_config = ConfigDict(extra="forbid")

    team_id: str
    parent_task_id: str
    requester_agent_id: str
    check_back_at_utc: Optional[datetime] = None
    last_progress_at_utc: Optional[datetime] = None
    last_progress_counts: dict[str, int] = Field(default_factory=dict)
    updated_at_utc: datetime


class DelegatedTaskRepository:
    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if not settings.postgres_dsn and not settings.sqlite_path:
            raise ValueError("Delegated task records require configured storage.")
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS delegated_tasks (
                team_id TEXT NOT NULL, task_id TEXT NOT NULL,
                parent_task_id TEXT NOT NULL, root_task_id TEXT NOT NULL,
                requester_agent_id TEXT NOT NULL, status TEXT NOT NULL,
                updated_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY (team_id, task_id)
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS delegated_tasks_parent "
                "ON delegated_tasks(team_id, parent_task_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS delegated_tasks_root "
                "ON delegated_tasks(team_id, root_task_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS delegated_tasks_requester "
                "ON delegated_tasks(team_id, requester_agent_id, status)"
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS delegation_waits (
                team_id TEXT NOT NULL, parent_task_id TEXT NOT NULL,
                requester_agent_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (team_id, parent_task_id)
            )""")
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    # -- delegated tasks ---------------------------------------------------------

    def _write(self, conn: Any, record: DelegatedTaskRecordV1) -> None:
        p = self.dialect.bind
        conn.execute(
            "INSERT INTO delegated_tasks(team_id,task_id,parent_task_id,root_task_id,"
            "requester_agent_id,status,updated_at,payload_json) "
            f"VALUES ({','.join([p] * 8)}) ON CONFLICT (team_id, task_id) DO UPDATE SET "
            "parent_task_id=excluded.parent_task_id, root_task_id=excluded.root_task_id, "
            "requester_agent_id=excluded.requester_agent_id, status=excluded.status, "
            "updated_at=excluded.updated_at, payload_json=excluded.payload_json",
            (
                record.team_id,
                record.task_id,
                record.parent_task_id,
                record.root_task_id,
                record.requester_agent_id,
                record.status,
                record.updated_at_utc.isoformat(),
                record.model_dump_json(),
            ),
        )

    def _read(self, conn: Any, team_id: str, task_id: str) -> Optional[DelegatedTaskRecordV1]:
        p = self.dialect.bind
        row = conn.execute(
            f"SELECT payload_json FROM delegated_tasks WHERE team_id={p} AND task_id={p}",
            (team_id, task_id),
        ).fetchone()
        return DelegatedTaskRecordV1.model_validate_json(row[0]) if row else None

    def record_published(
        self,
        *,
        team_id: str,
        task_id: str,
        parent_task_id: str,
        root_task_id: str,
        requester_agent_id: str,
        named_agent_type: Optional[str],
        target_agent_name: Optional[str],
        publish_sequence: Optional[int],
    ) -> DelegatedTaskRecordV1:
        """Record a task before it is published; the first record wins.

        The requester writes this before publishing, and fills in the stream
        sequence afterwards (``set_publish_sequence``), because a fast worker's
        claim can arrive before the publish call returns. Writing the record
        after publishing reset a claimed record to "queued" with no holder
        (2026-10-17): its stop was then never sent and the watchdog stopped
        checking the worker. An existing record (an outbox retry of the same
        delegation, or a record the claim handler created) keeps its status,
        holder and worker history.
        """

        now = _now()
        with self._connect() as conn:
            conn.begin_write()
            current = self._read(conn, team_id, task_id)
            if current is None:
                record = DelegatedTaskRecordV1(
                    team_id=team_id,
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    root_task_id=root_task_id,
                    requester_agent_id=requester_agent_id,
                    named_agent_type=named_agent_type,
                    target_agent_name=target_agent_name,
                    published_at_utc=now,
                    publish_sequence=publish_sequence,
                    updated_at_utc=now,
                )
            else:
                record = current.model_copy(
                    update={
                        "named_agent_type": current.named_agent_type or named_agent_type,
                        "publish_sequence": publish_sequence or current.publish_sequence,
                        "updated_at_utc": now,
                    }
                )
            self._write(conn, record)
            conn.commit()
            return record

    def set_publish_sequence(
        self, *, team_id: str, task_id: str, publish_sequence: Optional[int]
    ) -> Optional[DelegatedTaskRecordV1]:
        """Store the stream sequence a publish returned; nothing else changes."""

        if publish_sequence is None:
            return None
        with self._connect() as conn:
            conn.begin_write()
            current = self._read(conn, team_id, task_id)
            if current is None:
                conn.commit()
                return None
            record = current.model_copy(
                update={"publish_sequence": publish_sequence, "updated_at_utc": _now()}
            )
            self._write(conn, record)
            conn.commit()
            return record

    def record_claim(
        self,
        *,
        team_id: str,
        task_id: str,
        worker_agent_id: str,
        worker_agent_name: Optional[str],
        claimed_at: datetime,
    ) -> ClaimOutcome:
        """Record which worker holds a task; a different worker counts as a move."""

        with self._connect() as conn:
            conn.begin_write()
            current = self._read(conn, team_id, task_id)
            if current is None:
                conn.commit()
                return ClaimOutcome(record=None)
            if current.status not in OPEN_DELEGATED_STATUSES:
                conn.commit()
                return ClaimOutcome(record=current, already_closed=True)
            last_holder = current.holders[-1] if current.holders else None
            moved = last_holder is not None and last_holder != worker_agent_id
            holders = list(current.holders)
            if last_holder != worker_agent_id:
                holders.append(worker_agent_id)
            record = current.model_copy(
                update={
                    "status": DELEGATED_RUNNING,
                    "holder_agent_id": worker_agent_id,
                    "holder_agent_name": worker_agent_name,
                    "claimed_at_utc": claimed_at,
                    "holders": holders,
                    "interruptions": current.interruptions + (1 if moved else 0),
                    "updated_at_utc": _now(),
                }
            )
            self._write(conn, record)
            conn.commit()
            return ClaimOutcome(record=record, moved=moved)

    def mark_requeued(
        self,
        *,
        team_id: str,
        task_id: str,
        publish_sequence: Optional[int],
        from_holder_agent_id: str,
    ) -> Optional[DelegatedTaskRecordV1]:
        """A task goes back to the queue because ``from_holder_agent_id`` handed it back.

        Returns the requeued record, or None when nothing changed: the record
        is gone or closed, or another worker already claimed the re-sent task
        (a fast claim, or a retried handback whose task was already re-sent).
        A claim recorded since must never be turned back into "queued".
        """

        with self._connect() as conn:
            conn.begin_write()
            current = self._read(conn, team_id, task_id)
            if (
                current is None
                or current.status not in OPEN_DELEGATED_STATUSES
                or current.holder_agent_id not in {None, from_holder_agent_id}
            ):
                conn.commit()
                return None
            record = current.model_copy(
                update={
                    "status": DELEGATED_QUEUED,
                    "holder_agent_id": None,
                    "holder_agent_name": None,
                    "claimed_at_utc": None,
                    "publish_sequence": publish_sequence,
                    "updated_at_utc": _now(),
                }
            )
            self._write(conn, record)
            conn.commit()
            return record

    def mark_closed(
        self,
        *,
        team_id: str,
        task_id: str,
        status: DelegatedTaskStatus,
        failure_category: Optional[str] = None,
    ) -> Optional[DelegatedTaskRecordV1]:
        """Record how a delegated task ended; the first ending wins."""

        if status in OPEN_DELEGATED_STATUSES:
            raise ValueError(f"not a closing status: {status}")
        with self._connect() as conn:
            conn.begin_write()
            current = self._read(conn, team_id, task_id)
            if current is None or current.status not in OPEN_DELEGATED_STATUSES:
                conn.commit()
                return current
            now = _now()
            record = current.model_copy(
                update={
                    "status": status,
                    "failure_category": failure_category,
                    "finished_at_utc": now,
                    "updated_at_utc": now,
                }
            )
            self._write(conn, record)
            conn.commit()
            return record

    def get(self, team_id: str, task_id: str) -> Optional[DelegatedTaskRecordV1]:
        with self._connect() as conn:
            return self._read(conn, team_id, task_id)

    def list_for_parent(self, team_id: str, parent_task_id: str) -> tuple[DelegatedTaskRecordV1, ...]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM delegated_tasks "
                f"WHERE team_id={p} AND parent_task_id={p} ORDER BY task_id",
                (team_id, parent_task_id),
            ).fetchall()
        return tuple(DelegatedTaskRecordV1.model_validate_json(row[0]) for row in rows)

    def list_for_root(self, team_id: str, root_task_id: str) -> tuple[DelegatedTaskRecordV1, ...]:
        """Every delegated task of one request (all its parents), oldest publication first."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM delegated_tasks "
                f"WHERE team_id={p} AND root_task_id={p} ORDER BY task_id",
                (team_id, root_task_id),
            ).fetchall()
        records = [DelegatedTaskRecordV1.model_validate_json(row[0]) for row in rows]
        return tuple(sorted(records, key=lambda record: record.published_at_utc))

    def list_open(self, team_id: str, requester_agent_id: str) -> tuple[DelegatedTaskRecordV1, ...]:
        """Queued and running tasks this requester delegated."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM delegated_tasks "
                f"WHERE team_id={p} AND requester_agent_id={p} AND status IN ({p},{p}) "
                "ORDER BY parent_task_id, task_id",
                (team_id, requester_agent_id, DELEGATED_QUEUED, DELEGATED_RUNNING),
            ).fetchall()
        return tuple(DelegatedTaskRecordV1.model_validate_json(row[0]) for row in rows)

    def list_open_for_team(self, team_id: str) -> tuple[DelegatedTaskRecordV1, ...]:
        """Queued and running delegated tasks of every requester in the team."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM delegated_tasks "
                f"WHERE team_id={p} AND status IN ({p},{p}) ORDER BY parent_task_id, task_id",
                (team_id, DELEGATED_QUEUED, DELEGATED_RUNNING),
            ).fetchall()
        return tuple(DelegatedTaskRecordV1.model_validate_json(row[0]) for row in rows)

    def prune(self, team_id: str, *, before: datetime) -> int:
        """Delete closed records and finished waits not updated since ``before``."""

        p = self.dialect.bind
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM delegated_tasks "
                f"WHERE team_id={p} AND status NOT IN ({p},{p}) AND updated_at<{p}",
                (team_id, DELEGATED_QUEUED, DELEGATED_RUNNING, before.isoformat()),
            )
            conn.execute(
                "DELETE FROM delegation_waits "
                f"WHERE team_id={p} AND updated_at<{p} AND parent_task_id NOT IN ("
                f"SELECT parent_task_id FROM delegated_tasks WHERE team_id={p} "
                f"AND status IN ({p},{p}))",
                (team_id, before.isoformat(), team_id, DELEGATED_QUEUED, DELEGATED_RUNNING),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    # -- waits ------------------------------------------------------------------

    def _read_wait(self, conn: Any, team_id: str, parent_task_id: str) -> Optional[DelegationWaitV1]:
        p = self.dialect.bind
        row = conn.execute(
            f"SELECT payload_json FROM delegation_waits WHERE team_id={p} AND parent_task_id={p}",
            (team_id, parent_task_id),
        ).fetchone()
        return DelegationWaitV1.model_validate_json(row[0]) if row else None

    def _write_wait(self, conn: Any, wait: DelegationWaitV1) -> None:
        p = self.dialect.bind
        conn.execute(
            "INSERT INTO delegation_waits(team_id,parent_task_id,requester_agent_id,updated_at,"
            f"payload_json) VALUES ({','.join([p] * 5)}) "
            "ON CONFLICT (team_id, parent_task_id) DO UPDATE SET "
            "requester_agent_id=excluded.requester_agent_id, updated_at=excluded.updated_at, "
            "payload_json=excluded.payload_json",
            (
                wait.team_id,
                wait.parent_task_id,
                wait.requester_agent_id,
                wait.updated_at_utc.isoformat(),
                wait.model_dump_json(),
            ),
        )

    def _update_wait(
        self, team_id: str, parent_task_id: str, requester_agent_id: str, **changes: Any
    ) -> DelegationWaitV1:
        now = _now()
        with self._connect() as conn:
            conn.begin_write()
            wait = self._read_wait(conn, team_id, parent_task_id) or DelegationWaitV1(
                team_id=team_id,
                parent_task_id=parent_task_id,
                requester_agent_id=requester_agent_id,
                updated_at_utc=now,
            )
            wait = wait.model_copy(update={**changes, "updated_at_utc": now})
            self._write_wait(conn, wait)
            conn.commit()
            return wait

    def get_wait(self, team_id: str, parent_task_id: str) -> Optional[DelegationWaitV1]:
        with self._connect() as conn:
            return self._read_wait(conn, team_id, parent_task_id)

    def set_check_back(
        self,
        *,
        team_id: str,
        parent_task_id: str,
        requester_agent_id: str,
        at: Optional[datetime],
    ) -> DelegationWaitV1:
        return self._update_wait(
            team_id, parent_task_id, requester_agent_id, check_back_at_utc=at
        )

    def record_progress(
        self,
        *,
        team_id: str,
        parent_task_id: str,
        requester_agent_id: str,
        counts: dict[str, int],
        at: datetime,
    ) -> DelegationWaitV1:
        return self._update_wait(
            team_id,
            parent_task_id,
            requester_agent_id,
            last_progress_at_utc=at,
            last_progress_counts=dict(counts),
        )

    def waits_for_requester(self, team_id: str, requester_agent_id: str) -> tuple[DelegationWaitV1, ...]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM delegation_waits "
                f"WHERE team_id={p} AND requester_agent_id={p} ORDER BY parent_task_id",
                (team_id, requester_agent_id),
            ).fetchall()
        return tuple(DelegationWaitV1.model_validate_json(row[0]) for row in rows)


def status_counts(records: tuple[DelegatedTaskRecordV1, ...] | list[DelegatedTaskRecordV1]) -> dict[str, int]:
    """How many delegated tasks are in each status (all five keys present)."""

    counts = {status: 0 for status in ("queued", "running", "finished", "failed", "cancelled")}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    return counts


__all__ = [
    "ClaimOutcome",
    "DELEGATED_CANCELLED",
    "DELEGATED_FAILED",
    "DELEGATED_FINISHED",
    "DELEGATED_QUEUED",
    "DELEGATED_RUNNING",
    "DelegatedTaskRecordV1",
    "DelegatedTaskRepository",
    "DelegatedTaskStatus",
    "DelegationWaitV1",
    "OPEN_DELEGATED_STATUSES",
    "status_counts",
]
