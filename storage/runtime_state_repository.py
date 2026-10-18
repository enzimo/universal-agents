"""Backend-neutral repository boundary for live runtime snapshot persistence."""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import logging
import json
import sqlite3
from typing import Any
from custom_messages.contracts.runtime_snapshot_v3 import TaskSnapshotRecord, decode_runtime_snapshot
from custom_messages.contracts.schedule_worker_v1 import ScheduleOccurrenceV1
from storage.schedule_worker_repository import ScheduleWorkerRepository
from storage import task_custody_repository as custody
from storage.task_custody_repository import (
    TaskCustodyConflict, TaskCustodyRow, TaskCustodyTakenOver, TaskCustodyTakeover,
)
from storage.runtime_snapshot_layout import (
    assemble_snapshot, canonical_json, parse_storage_header,
    payload_digest, split_snapshot,
)
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Protocol, Sequence

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
from .sqlite_connection import connect_sqlite, ensure_sqlite_column


@dataclass
class TaskRuntimeSnapshotRow:
    owner_agent_id: str
    owner_agent_name: Optional[str]
    team_id: Optional[str]
    owner_named_agent_type: Optional[str]
    runtime_state_json: str
    updated_at: Optional[str]


@dataclass
class DeferredMessageSnapshotRow:
    owner_agent_id: str
    owner_agent_name: Optional[str]
    team_id: Optional[str]
    owner_named_agent_type: Optional[str]
    messages_json: str
    updated_at: Optional[str]


class RuntimeRecoveryConflict(RuntimeError):
    """A source changed or another runtime completed this recovery first."""


@dataclass(frozen=True)
class RuntimeTaskCancellationRow:
    """Durable task-level member of a cancelled execution graph."""

    team_id: str
    task_id: str
    root_task_id: str
    canonical_user_id: Optional[str]
    command_id: str
    reason: str
    cancelled_at: str


class RuntimeStateRepository(Protocol):
    def save_task_runtime_task(
        self, *, owner_agent_id: str, task: TaskSnapshotRecord,
        task_plan_links: dict[str, str], task_objective_links: dict[str, str],
        plan_objective_links: dict[str, str],
    ) -> None: ...

    def commit_task_runtime_recovery(
        self, *, sources: Sequence[TaskRuntimeSnapshotRow], replacement: TaskRuntimeSnapshotRow,
    ) -> None: ...

    def commit_deferred_messages_recovery(
        self, *, sources: Sequence[DeferredMessageSnapshotRow], replacement: DeferredMessageSnapshotRow,
    ) -> None: ...

    def save_task_runtime(
        self,
        *,
        owner_agent_id: str,
        owner_agent_name: Optional[str],
        team_id: Optional[str],
        owner_named_agent_type: Optional[str],
        runtime_state_json: str,
        schedule_admission: ScheduleOccurrenceV1 | None = None,
        custody_takeover: TaskCustodyTakeover | None = None,
    ) -> None: ...

    def clear_task_runtime(self, owner_agent_id: str) -> None: ...

    def get_task_custody(self, team_id: str, task_ids: Sequence[str]) -> dict[str, TaskCustodyRow]: ...

    def get_task_runtime_team_id(self, owner_agent_id: str) -> Optional[str]: ...

    def load_task_runtime_by_owner(self, owner_agent_id: str) -> List[TaskRuntimeSnapshotRow]: ...


    def peek_task_runtime_by_role(self, team_id: str, owner_named_agent_type: str) -> List[TaskRuntimeSnapshotRow]: ...

    def peek_task_runtime_by_team(self, team_id: str) -> List[TaskRuntimeSnapshotRow]: ...

    def save_deferred_messages(
        self,
        *,
        owner_agent_id: str,
        owner_agent_name: Optional[str],
        team_id: Optional[str],
        owner_named_agent_type: Optional[str],
        messages_json: str,
    ) -> None: ...

    def clear_deferred_messages(self, owner_agent_id: str) -> None: ...

    def get_deferred_messages_team_id(self, owner_agent_id: str) -> Optional[str]: ...

    def load_deferred_messages_by_owner(self, owner_agent_id: str) -> List[DeferredMessageSnapshotRow]: ...


    def peek_deferred_messages_by_role(
        self,
        team_id: str,
        owner_named_agent_type: str,
    ) -> List[DeferredMessageSnapshotRow]: ...

    def record_task_cancellations(
        self,
        *,
        team_id: str,
        root_task_id: str,
        task_ids: List[str],
        canonical_user_id: Optional[str],
        command_id: str,
        reason: str,
        cancelled_at: str,
    ) -> None: ...

    def get_task_cancellation(
        self,
        *,
        team_id: str,
        task_id: str,
    ) -> Optional[RuntimeTaskCancellationRow]: ...


class _RelationalRuntimeStateRepository:
    """Shared record operations for runtime snapshot persistence."""

    _dialect: SqlDialect

    _recovery_lock_clause = ""


    def commit_task_runtime_recovery(
        self, *, sources: Sequence[TaskRuntimeSnapshotRow], replacement: TaskRuntimeSnapshotRow,
    ) -> None:
        self._commit_recovery(
            table="active_task_runtime", payload_column="runtime_state_json",
            sources=sources, replacement=replacement,
        )

    def commit_deferred_messages_recovery(
        self, *, sources: Sequence[DeferredMessageSnapshotRow], replacement: DeferredMessageSnapshotRow,
    ) -> None:
        self._commit_recovery(
            table="agent_deferred_messages", payload_column="messages_json",
            sources=sources, replacement=replacement,
        )

    def _commit_recovery(
        self, *, table: str, payload_column: str,
        sources: Sequence[TaskRuntimeSnapshotRow | DeferredMessageSnapshotRow],
        replacement: TaskRuntimeSnapshotRow | DeferredMessageSnapshotRow,
    ) -> None:
        """Compare the read versions and replace custody in one transaction.

        Reading a snapshot never consumes it. Callers decode before entering this
        transaction and may expose restored work only after it commits. Concurrent
        replacements can both read, but only one can consume the exact sources.
        """
        expected = {row.owner_agent_id: self._snapshot_values(row) for row in sources}
        if not expected or len(expected) != len(sources):
            raise ValueError("Recovery requires distinct source owners.")
        owners = sorted(set(expected) | {replacement.owner_agent_id})
        placeholders = ",".join(f"{self._dialect.bind}" for _ in owners)
        columns = f"owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, {payload_column}, updated_at"
        parts = split_snapshot(decode_runtime_snapshot(replacement.runtime_state_json)) if isinstance(replacement, TaskRuntimeSnapshotRow) else None
        try:
            with self._connect() as conn:
                self._begin_recovery(conn)
                rows = conn.execute(
                    f"SELECT {columns} FROM {table} WHERE owner_agent_id IN ({placeholders}) "
                    f"ORDER BY owner_agent_id{self._recovery_lock_clause}",
                    owners,
                ).fetchall()
                current = {row[0]: tuple(row[index] for index in range(6)) for row in rows}
                if parts is not None:
                    read_rows = self._read_task_rows(conn, f"SELECT {columns} FROM {table} WHERE owner_agent_id IN ({placeholders})", owners)
                    current = {row.owner_agent_id: self._snapshot_values(row) for row in read_rows}
                if current != expected:
                    raise RuntimeRecoveryConflict("Runtime recovery source or destination changed.")
                self._delete_rows_with_connection(conn, table, list(expected))
                values = (*self._snapshot_values(replacement)[:5], datetime.now(timezone.utc).isoformat())
                if parts is not None:
                    values = (*values[:4], parts.header_json, values[5])
                # INSERT, not UPSERT: a concurrently created destination must
                # abort this transfer instead of overwriting accepted work.
                conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})", values)
                if parts is not None:
                    self._write_task_records(conn, replacement.owner_agent_id, parts.records, replace_all=True)
                    self._transfer_task_custody(conn, sources=list(expected), replacement=replacement, records=parts.records)
                    ScheduleWorkerRepository.transfer_custody(
                        conn, self._dialect, sources=tuple(expected),
                        replacement_agent_id=replacement.owner_agent_id,
                        team_id=replacement.team_id,
                        task_ids=tuple(key for section, key in parts.records if section == "tasks"),
                    )
                conn.commit()
        except Exception:
            self._logger.exception(
                "Runtime recovery transfer did not commit",
                extra={"event": "runtime_recovery_commit_failed", "state_table": table,
                       "source_owner_ids": sorted(expected), "destination_owner_id": replacement.owner_agent_id},
            )
            raise

    def _transfer_task_custody(
        self, conn: Any, *, sources: list[str], replacement: TaskRuntimeSnapshotRow | DeferredMessageSnapshotRow,
        records: dict[tuple[str, str], str],
    ) -> None:
        if not isinstance(replacement, TaskRuntimeSnapshotRow) or not replacement.team_id:
            return
        try:
            custody.transfer_on_recovery(
                conn, self._dialect, team_id=replacement.team_id, source_owner_ids=sources,
                replacement_agent_id=replacement.owner_agent_id,
                replacement_named_agent_type=replacement.owner_named_agent_type,
                adopted_task_ids=custody.executed_task_ids(records),
            )
        except TaskCustodyConflict as exc:
            # The caller left out tasks another agent held when it read them;
            # custody changed since. Same outcome as a concurrent recovery.
            raise RuntimeRecoveryConflict(
                f"Task {exc.task_id} was taken by agent {exc.holder_agent_id} during recovery."
            ) from exc

    @staticmethod
    def _snapshot_values(
        row: TaskRuntimeSnapshotRow | DeferredMessageSnapshotRow,
    ) -> tuple[str, Optional[str], Optional[str], Optional[str], str, Optional[str]]:
        payload = row.runtime_state_json if isinstance(row, TaskRuntimeSnapshotRow) else row.messages_json
        return (row.owner_agent_id, row.owner_agent_name, row.team_id,
                row.owner_named_agent_type, payload, row.updated_at)

    def _init_schema(self) -> None:
        backend_name = self._dialect.name
        storage_target = redact_postgres_dsn(self._dsn) if backend_name == "Postgres" else self._db_path
        try:
            ensure_storage_parent_dir(self._db_path)
            with self._connect() as conn:
                ScheduleWorkerRepository.ensure_occurrence_schema(conn)
                custody.ensure_schema(conn)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS active_task_runtime (
                        owner_agent_id TEXT PRIMARY KEY,
                        owner_agent_name TEXT,
                        team_id TEXT,
                        owner_named_agent_type TEXT,
                        runtime_state_json TEXT NOT NULL,
                        updated_at TEXT
                    )
                    """
                )
                self._ensure_column(conn, "active_task_runtime", "team_id", "TEXT")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS active_task_runtime_records (
                        owner_agent_id TEXT NOT NULL,
                        section TEXT NOT NULL,
                        record_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        PRIMARY KEY (owner_agent_id, section, record_id)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_active_task_runtime_team ON active_task_runtime(team_id, updated_at DESC)"
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_active_task_runtime_team_type
                    ON active_task_runtime(team_id, owner_named_agent_type, updated_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_deferred_messages (
                        owner_agent_id TEXT PRIMARY KEY,
                        owner_agent_name TEXT,
                        team_id TEXT,
                        owner_named_agent_type TEXT,
                        messages_json TEXT NOT NULL,
                        updated_at TEXT
                    )
                    """
                )
                self._ensure_column(conn, "agent_deferred_messages", "team_id", "TEXT")
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_agent_deferred_messages_team
                    ON agent_deferred_messages(team_id, updated_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_agent_deferred_messages_team_type
                    ON agent_deferred_messages(team_id, owner_named_agent_type, updated_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS runtime_task_cancellations (
                        team_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        root_task_id TEXT NOT NULL,
                        canonical_user_id TEXT,
                        command_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        cancelled_at TEXT NOT NULL,
                        PRIMARY KEY (team_id, task_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_runtime_task_cancellations_root
                    ON runtime_task_cancellations(team_id, root_task_id, cancelled_at DESC)
                    """
                )
                conn.commit()
            self._logger.info(f"{backend_name} runtime-state repository initialized at {storage_target}")
        except Exception as exc:
            self._logger.error(f"Failed to initialize {backend_name} runtime-state repository: {exc}")
            raise


    def save_task_runtime(
        self, *, owner_agent_id: str, owner_agent_name: Optional[str], team_id: Optional[str],
        owner_named_agent_type: Optional[str], runtime_state_json: str,
        schedule_admission: ScheduleOccurrenceV1 | None = None,
        custody_takeover: TaskCustodyTakeover | None = None,
    ) -> None:
        """Replace the owner's snapshot; tasks entering or leaving it claim or release custody.

        ``custody_takeover`` lets the save replace that holder's row for that
        task (``custom_agents.task_custody``); any other held row refuses the
        save with ``TaskCustodyConflict``. A task this owner executed in its
        previous save whose row another agent now holds refuses the save with
        ``TaskCustodyTakenOver`` before anything is written.
        """
        parts = split_snapshot(decode_runtime_snapshot(runtime_state_json))
        if schedule_admission is not None and (
            ("tasks", schedule_admission.coalesced_into_task_id or schedule_admission.task_id) not in parts.records
            or schedule_admission.agent_id != owner_agent_id
            or schedule_admission.team_id != team_id
            or schedule_admission.named_agent_type != owner_named_agent_type
        ):
            raise ValueError("Schedule admission must match durable task custody")
        bind = self._dialect.bind
        try:
            with self._connect() as conn:
                self._begin_recovery(conn)
                previous = conn.execute(
                    f"SELECT runtime_state_json FROM active_task_runtime WHERE owner_agent_id = {bind}{self._recovery_lock_clause}",
                    (owner_agent_id,),
                ).fetchone()
                previous_task_ids = set(parse_storage_header(previous[0])["task_order"]) if previous is not None else set()
                if team_id and previous_task_ids:
                    self._refuse_taken_over_tasks(
                        conn, parts.records, previous_task_ids, owner_agent_id=owner_agent_id, team_id=team_id,
                    )
                conn.execute(
                    f"""INSERT INTO active_task_runtime
                        (owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, runtime_state_json, updated_at)
                    VALUES ({bind}, {bind}, {bind}, {bind}, {bind}, {bind})
                    ON CONFLICT(owner_agent_id) DO UPDATE SET
                        owner_agent_name = excluded.owner_agent_name, team_id = excluded.team_id,
                        owner_named_agent_type = excluded.owner_named_agent_type,
                        runtime_state_json = excluded.runtime_state_json, updated_at = excluded.updated_at""",
                    (owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, parts.header_json, datetime.now(timezone.utc).isoformat()),
                )
                self._write_task_records(conn, owner_agent_id, parts.records, replace_all=True)
                if schedule_admission is not None:
                    ScheduleWorkerRepository.commit_admission(conn, self._dialect, schedule_admission)
                if team_id:
                    self._sync_task_custody(
                        conn, parts.records, previous_task_ids, owner_agent_id=owner_agent_id,
                        team_id=team_id, owner_named_agent_type=owner_named_agent_type,
                        takeover=custody_takeover,
                    )
                conn.commit()
        except (TaskCustodyConflict, TaskCustodyTakenOver):
            raise  # expected refusals; the admitting caller or the snapshot coordinator logs them
        except Exception:
            self._logger.exception("Failed to persist active runtime state", extra={"owner_agent_id": owner_agent_id})
            raise

    def _refuse_taken_over_tasks(
        self, conn: Any, records: dict[tuple[str, str], str], previous_task_ids: set[str], *,
        owner_agent_id: str, team_id: str,
    ) -> None:
        """Refuse the save if another agent took over a task this owner already held.

        Only the custody row moves in an admission takeover; the old holder's
        snapshot still lists the task, and a holder wrongly judged gone keeps
        running and saving it (2026-10-17). A task the previous save had
        delegated out is the worker's to hold, also once its record leaves the
        delegation map, so it is not checked.
        """
        bind = self._dialect.bind
        previously_delegated = {row[0] for row in conn.execute(
            f"SELECT record_id FROM active_task_runtime_records WHERE owner_agent_id = {bind} AND section = {bind}",
            (owner_agent_id, "subtask_to_parent_map"),
        ).fetchall()}
        still_held = custody.executed_task_ids(records) & (previous_task_ids - previously_delegated)
        holders = custody.taken_over(
            conn, self._dialect, team_id=team_id, owner_agent_id=owner_agent_id, task_ids=still_held,
        )
        if holders:
            raise TaskCustodyTakenOver(owner_agent_id, holders)

    def save_task_runtime_task(
        self, *, owner_agent_id: str, task: TaskSnapshotRecord,
        task_plan_links: dict[str, str], task_objective_links: dict[str, str],
        plan_objective_links: dict[str, str],
    ) -> None:
        """Commit one admitted task and links resolved while writing its history.

        This cannot admit work. The initial full-state admission must already
        own this task, and recovery cannot race a partial write past its fence.
        """
        bind = self._dialect.bind
        records = {("tasks", task.task_id): canonical_json(task.model_dump(mode="json"))}
        for section, links in (
            ("task_to_plan_id", task_plan_links), ("task_to_objective_id", task_objective_links),
            ("plan_to_objective_id", plan_objective_links),
        ):
            records.update({(section, key): canonical_json(value) for key, value in links.items()})
        try:
            with self._connect() as conn:
                self._begin_recovery(conn)
                row = conn.execute(
                    f"SELECT runtime_state_json FROM active_task_runtime WHERE owner_agent_id = {bind}{self._recovery_lock_clause}",
                    (owner_agent_id,),
                ).fetchone()
                if row is None or task.task_id not in parse_storage_header(row[0])["task_order"]:
                    raise RuntimeRecoveryConflict("Task history requires existing durable task custody")
                self._write_task_records(conn, owner_agent_id, records, replace_all=False)
                conn.execute(f"UPDATE active_task_runtime SET updated_at = {bind} WHERE owner_agent_id = {bind}",
                             (datetime.now(timezone.utc).isoformat(), owner_agent_id))
                conn.commit()
        except Exception:
            self._logger.exception("Failed to persist task history", extra={"owner_agent_id": owner_agent_id, "task_id": task.task_id})
            raise

    def _write_task_records(
        self, conn: Any, owner_agent_id: str, records: dict[tuple[str, str], str], *, replace_all: bool,
    ) -> None:
        bind = self._dialect.bind
        # Compare compact digests, never read unrelated histories back merely
        # to decide whether their rows need an UPDATE.
        existing = {(row[0], row[1]): row[2] for row in conn.execute(
            f"SELECT section, record_id, payload_digest FROM active_task_runtime_records WHERE owner_agent_id = {bind}",
            (owner_agent_id,),
        ).fetchall()}
        if replace_all:
            for section, record_id in existing.keys() - records.keys():
                conn.execute(f"DELETE FROM active_task_runtime_records WHERE owner_agent_id = {bind} AND section = {bind} AND record_id = {bind}",
                             (owner_agent_id, section, record_id))
        for (section, record_id), payload in records.items():
            digest = payload_digest(payload)
            if existing.get((section, record_id)) == digest:
                continue
            conn.execute(f"""INSERT INTO active_task_runtime_records
                (owner_agent_id, section, record_id, payload_json, payload_digest)
                VALUES ({bind}, {bind}, {bind}, {bind}, {bind})
                ON CONFLICT(owner_agent_id, section, record_id) DO UPDATE SET
                    payload_json = excluded.payload_json, payload_digest = excluded.payload_digest""",
                (owner_agent_id, section, record_id, payload, digest))
            if section == "tasks":
                from custom_messages.enums import ProgressState

                state = json.loads(payload).get("state")
                if state in {item.value for item in (ProgressState.COMPLETED, ProgressState.FAILED, ProgressState.CANCELLED, ProgressState.ABANDONED)}:
                    ScheduleWorkerRepository.record_terminal_task(conn, self._dialect,
                        agent_id=owner_agent_id, task_id=record_id, outcome=state)

    def _sync_task_custody(
        self, conn: Any, records: dict[tuple[str, str], str], previous_task_ids: set[str], *,
        owner_agent_id: str, team_id: str, owner_named_agent_type: Optional[str],
        takeover: TaskCustodyTakeover | None,
    ) -> None:
        """Claim custody of executed tasks entering the snapshot; release tasks that left."""
        current_task_ids = {key for section, key in records if section == "tasks"}
        custody.claim_entering(
            conn, self._dialect, team_id=team_id, owner_agent_id=owner_agent_id,
            owner_named_agent_type=owner_named_agent_type,
            task_ids=custody.executed_task_ids(records) - previous_task_ids, takeover=takeover,
        )
        custody.release_leaving(
            conn, self._dialect, team_id=team_id, owner_agent_id=owner_agent_id,
            task_ids=previous_task_ids - current_task_ids,
        )

    def clear_task_runtime(self, owner_agent_id: str) -> None:
        self._delete_rows("active_task_runtime", [owner_agent_id], "active task runtime")

    def get_task_custody(self, team_id: str, task_ids: Sequence[str]) -> dict[str, TaskCustodyRow]:
        """Who holds each of these tasks (``storage.task_custody_repository``)."""
        with self._connect() as conn:
            return custody.read_rows(conn, self._dialect, team_id, task_ids)

    def get_task_runtime_team_id(self, owner_agent_id: str) -> Optional[str]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"""
                    SELECT team_id
                    FROM active_task_runtime
                    WHERE owner_agent_id = {self._dialect.bind}
                    """,
                    (owner_agent_id,),
                ).fetchone()
            return row[0] if row else None
        except Exception as exc:
            self._logger.error(f"Failed to load stored runtime team id for {owner_agent_id}: {exc}")
            return None

    def load_task_runtime_by_owner(self, owner_agent_id: str) -> List[TaskRuntimeSnapshotRow]:
        return self._load_task_runtime_rows(
            f"""
            SELECT owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, runtime_state_json, updated_at
            FROM active_task_runtime
            WHERE owner_agent_id = {self._dialect.bind}
            """,
            (owner_agent_id,),
        )


    def peek_task_runtime_by_role(
        self,
        team_id: str,
        owner_named_agent_type: str,
    ) -> List[TaskRuntimeSnapshotRow]:
        return self._load_task_runtime_rows(
            f"""
            SELECT owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, runtime_state_json, updated_at
            FROM active_task_runtime
            WHERE team_id = {self._dialect.bind} AND owner_named_agent_type = {self._dialect.bind}
            ORDER BY updated_at ASC
            """,
            (team_id, owner_named_agent_type),
        )

    def peek_task_runtime_by_team(self, team_id: str) -> List[TaskRuntimeSnapshotRow]:
        """Every agent's saved work in the team, read-only (stuck-task check, takeover).

        One unreadable snapshot is logged and skipped, so it cannot hide the rest.
        """
        return self._load_task_runtime_rows(
            f"""
            SELECT owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, runtime_state_json, updated_at
            FROM active_task_runtime
            WHERE team_id = {self._dialect.bind}
            ORDER BY updated_at ASC
            """,
            (team_id,),
            skip_unreadable=True,
        )

    def save_deferred_messages(
        self,
        *,
        owner_agent_id: str,
        owner_agent_name: Optional[str],
        team_id: Optional[str],
        owner_named_agent_type: Optional[str],
        messages_json: str,
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    INSERT INTO agent_deferred_messages
                        (owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, messages_json, updated_at)
                    VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    ON CONFLICT(owner_agent_id) DO UPDATE SET
                        owner_agent_name = excluded.owner_agent_name,
                        team_id = excluded.team_id,
                        owner_named_agent_type = excluded.owner_named_agent_type,
                        messages_json = excluded.messages_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        owner_agent_id,
                        owner_agent_name,
                        team_id,
                        owner_named_agent_type,
                        messages_json,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.commit()
        except Exception:
            self._logger.exception("Failed to persist deferred messages", extra={"owner_agent_id": owner_agent_id})
            raise

    def clear_deferred_messages(self, owner_agent_id: str) -> None:
        self._delete_rows("agent_deferred_messages", [owner_agent_id], "deferred messages")

    def get_deferred_messages_team_id(self, owner_agent_id: str) -> Optional[str]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"""
                    SELECT team_id
                    FROM agent_deferred_messages
                    WHERE owner_agent_id = {self._dialect.bind}
                    """,
                    (owner_agent_id,),
                ).fetchone()
            return row[0] if row else None
        except Exception as exc:
            self._logger.error(f"Failed to load deferred message team id for {owner_agent_id}: {exc}")
            return None

    def load_deferred_messages_by_owner(self, owner_agent_id: str) -> List[DeferredMessageSnapshotRow]:
        return self._load_deferred_message_rows(
            f"""
            SELECT owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, messages_json, updated_at
            FROM agent_deferred_messages
            WHERE owner_agent_id = {self._dialect.bind}
            """,
            (owner_agent_id,),
        )


    def peek_deferred_messages_by_role(
        self,
        team_id: str,
        owner_named_agent_type: str,
    ) -> List[DeferredMessageSnapshotRow]:
        return self._load_deferred_message_rows(
            f"""
            SELECT owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, messages_json, updated_at
            FROM agent_deferred_messages
            WHERE team_id = {self._dialect.bind} AND owner_named_agent_type = {self._dialect.bind}
            ORDER BY updated_at ASC
            """,
            (team_id, owner_named_agent_type),
        )

    def record_task_cancellations(
        self,
        *,
        team_id: str,
        root_task_id: str,
        task_ids: List[str],
        canonical_user_id: Optional[str],
        command_id: str,
        reason: str,
        cancelled_at: str,
    ) -> None:
        normalized_task_ids = [
            task_id for task_id in dict.fromkeys(task_ids) if str(task_id).strip()
        ]
        if not team_id or not root_task_id or not normalized_task_ids:
            raise ValueError(
                "Runtime cancellation requires team_id, root_task_id, and task_ids."
            )
        try:
            with self._connect() as conn:
                for task_id in normalized_task_ids:
                    conn.execute(
                        f"""
                        INSERT INTO runtime_task_cancellations
                            (team_id, task_id, root_task_id, canonical_user_id,
                             command_id, reason, cancelled_at)
                        VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                        ON CONFLICT(team_id, task_id) DO UPDATE SET
                            root_task_id = excluded.root_task_id,
                            canonical_user_id = excluded.canonical_user_id,
                            command_id = excluded.command_id,
                            reason = excluded.reason,
                            cancelled_at = excluded.cancelled_at
                        """,
                        (
                            team_id,
                            task_id,
                            root_task_id,
                            canonical_user_id,
                            command_id,
                            reason,
                            cancelled_at,
                        ),
                    )
                conn.commit()
        except Exception as exc:
            self._logger.error(
                "Failed to persist runtime cancellation root=%s team=%s: %s",
                root_task_id,
                team_id,
                exc,
            )
            raise

    def get_task_cancellation(
        self,
        *,
        team_id: str,
        task_id: str,
    ) -> Optional[RuntimeTaskCancellationRow]:
        if not team_id or not task_id:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"""
                    SELECT team_id, task_id, root_task_id, canonical_user_id,
                           command_id, reason, cancelled_at
                    FROM runtime_task_cancellations
                    WHERE team_id = {self._dialect.bind} AND task_id = {self._dialect.bind}
                    """,
                    (team_id, task_id),
                ).fetchone()
            if row is None:
                return None
            return RuntimeTaskCancellationRow(
                team_id=row[0],
                task_id=row[1],
                root_task_id=row[2],
                canonical_user_id=row[3],
                command_id=row[4],
                reason=row[5],
                cancelled_at=row[6],
            )
        except Exception as exc:
            self._logger.error(
                "Failed to load runtime cancellation task=%s team=%s: %s",
                task_id,
                team_id,
                exc,
            )
            raise


    def _load_task_runtime_rows(
        self, query: str, params: tuple, *, skip_unreadable: bool = False
    ) -> List[TaskRuntimeSnapshotRow]:
        try:
            with self._connect() as conn:
                return self._read_task_rows(conn, query, params, skip_unreadable=skip_unreadable)
        except Exception:
            self._logger.exception("Failed to load active runtime rows")
            raise

    def _read_task_rows(
        self, conn: Any, owner_query: str, params: Any, *, skip_unreadable: bool = False
    ) -> List[TaskRuntimeSnapshotRow]:
        # One statement observes the header and its records at the same database
        # snapshot, including PostgreSQL's default READ COMMITTED isolation.
        rows = conn.execute(f"""WITH owners AS ({owner_query})
            SELECT o.owner_agent_id, o.owner_agent_name, o.team_id, o.owner_named_agent_type,
                   o.runtime_state_json, o.updated_at, r.section, r.record_id, r.payload_json
              FROM owners o LEFT JOIN active_task_runtime_records r ON o.owner_agent_id = r.owner_agent_id
             ORDER BY o.updated_at, o.owner_agent_id, r.section, r.record_id""", params).fetchall()
        owners: dict[str, tuple[Any, dict[tuple[str, str], str]]] = {}
        for row in rows:
            _, records = owners.setdefault(row[0], (row, {}))
            if row[6] is not None:
                records[(row[6], row[7])] = row[8]
        snapshots = []
        for row, records in owners.values():
            try:
                payload = assemble_snapshot(row[4], records)
            except ValueError:
                self._logger.error(
                    "event=runtime_snapshot_unreadable owner_agent_id=%s team_id=%s named_agent_type=%s",
                    row[0], row[2], row[3],
                )
                if skip_unreadable:
                    continue
                raise
            snapshots.append(TaskRuntimeSnapshotRow(
                owner_agent_id=row[0], owner_agent_name=row[1], team_id=row[2],
                owner_named_agent_type=row[3], runtime_state_json=payload, updated_at=row[5],
            ))
        return snapshots

    def _load_deferred_message_rows(
        self,
        query: str,
        params: tuple,
    ) -> List[DeferredMessageSnapshotRow]:
        try:
            with self._connect() as conn:
                rows = conn.execute(query, params).fetchall()
            return [
                DeferredMessageSnapshotRow(
                    owner_agent_id=row[0],
                    owner_agent_name=row[1],
                    team_id=row[2],
                    owner_named_agent_type=row[3],
                    messages_json=row[4],
                    updated_at=row[5],
                )
                for row in rows
            ]
        except Exception as exc:
            self._logger.error(f"Failed to load deferred message rows: {exc}")
            raise

    def _delete_rows(self, table: str, owner_agent_ids: List[str], description: str) -> None:
        unique_owner_ids = [owner_id for owner_id in dict.fromkeys(owner_agent_ids) if owner_id]
        if not unique_owner_ids:
            return
        try:
            with self._connect() as conn:
                self._begin_recovery(conn)
                placeholders = ",".join(self._dialect.bind for _ in unique_owner_ids)
                conn.execute(f"SELECT owner_agent_id FROM {table} WHERE owner_agent_id IN ({placeholders}) ORDER BY owner_agent_id{self._recovery_lock_clause}", sorted(unique_owner_ids)).fetchall()
                self._delete_rows_with_connection(conn, table, unique_owner_ids)
                conn.commit()
        except Exception:
            self._logger.exception("Failed to delete runtime state", extra={"state_kind": description})
            raise

    @staticmethod
    def _delete_rows_with_connection(conn: Any, table: str, owner_agent_ids: List[str]) -> None:
        unique_owner_ids = [owner_id for owner_id in dict.fromkeys(owner_agent_ids) if owner_id]
        if not unique_owner_ids:
            return
        placeholders = ",".join([f"{conn.dialect.bind}"] * len(unique_owner_ids))
        if table == "active_task_runtime":
            conn.execute(f"DELETE FROM active_task_runtime_records WHERE owner_agent_id IN ({placeholders})", unique_owner_ids)
            custody.release_owners(conn, conn.dialect, unique_owner_ids)
        conn.execute(
            f"DELETE FROM {table} WHERE owner_agent_id IN ({placeholders})",
            unique_owner_ids,
        )


class SqliteRuntimeStateRepository(_RelationalRuntimeStateRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._init_schema()


    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path, logger=self._logger)


    def _begin_recovery(self, conn: sqlite3.Connection | PostgresConnection) -> None:
        conn.execute("BEGIN IMMEDIATE")


    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
        ensure_sqlite_column(conn, table_name, column_name, column_type, logger=self._logger)


    def _column_names(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f'PRAGMA table_info({table_name})').fetchall()
        return {str(row[1]) for row in rows}


class PostgresRuntimeStateRepository(_RelationalRuntimeStateRepository):
    """Postgres implementation for runtime snapshot persistence."""

    _dialect = POSTGRES

    _recovery_lock_clause = " FOR UPDATE"

    def _begin_recovery(self, conn: sqlite3.Connection | PostgresConnection) -> None:
        # psycopg starts a transaction on the locking SELECT. Ordered row locks
        # serialize overlapping source sets without relying on SQLite syntax.
        pass

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


def create_runtime_state_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> RuntimeStateRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="RuntimeStateRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqliteRuntimeStateRepository(storage_settings.sqlite_path, logger=logger)
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresRuntimeStateRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(
        f"RuntimeStateRepository has no implementation for backend {storage_settings.backend!r}."
    )
