"""Transactional workflow state and redacted events on SQLite or PostgreSQL.

The state version fences concurrent progress updates. Event insertion shares the
state transaction so every progress change has a durable event. Agent-reported
completion is not an execution receipt or authorization decision.
Each connection owns its lifetime; no SQLite handle is shared across workers.

The tables carry a ``_v2`` suffix: schema version 2 names what each step needs
in the access model (``custom_messages.contracts.access_workflow_v2``).
Version 1 workflows stay in the old ``access_workflows`` tables, are never
read, and a request for one reads as "not found or saved by an older release".
"""

from __future__ import annotations

import sqlite3
from typing import Any

from custom_messages.contracts.access_workflow_v2 import (
    OPEN_STEP_STATES,
    WORKFLOW_PAGE_SIZE,
    AccessWorkflowV2,
    WorkflowEventV2,
    WorkflowStepProposalV2,
)
from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite

WORKFLOW_TABLE = "access_workflows_v2"
EVENT_TABLE = "access_workflow_events_v2"
STEP_EXPECTATION_FIELDS = frozenset({"change_kinds", "tool_access"})
CORRECTION_EVENT_CODE = "step_expectations_corrected"
TERMINAL_PHASES = "('complete','cancelled')"


class WorkflowConflict(RuntimeError):
    """The workflow changed since it was read, or the change is not allowed."""


class AccessWorkflowRepository:
    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if not settings.postgres_dsn and not settings.sqlite_path:
            raise ValueError("Workflow persistence requires SQLite or PostgreSQL.")
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {WORKFLOW_TABLE} (
                workflow_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                team_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                proposal_digest TEXT NOT NULL,
                source_task_id TEXT NOT NULL,
                source_context_id TEXT,
                version INTEGER NOT NULL,
                record_digest TEXT NOT NULL,
                phase TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_sequence INTEGER NOT NULL DEFAULT 0,
                UNIQUE(team_id, owner_id, idempotency_key),
                UNIQUE(team_id, owner_id, source_task_id)
            )""")
            conn.execute(f"""CREATE INDEX IF NOT EXISTS access_workflow_v2_owner_page
                ON {WORKFLOW_TABLE}(team_id, owner_id, updated_at, workflow_id)""")
            # Worker requests find their parent's workflow by the shared signed context.
            conn.execute(f"""CREATE INDEX IF NOT EXISTS access_workflow_v2_context
                ON {WORKFLOW_TABLE}(team_id, source_context_id)""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {EVENT_TABLE} (
                workflow_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(workflow_id, sequence)
            )""")
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    def _get(self, conn: Any, workflow_id: str, *, lock: bool = False) -> AccessWorkflowV2 | None:
        suffix = " FOR UPDATE" if lock and self.dialect == POSTGRES else ""
        row = conn.execute(
            f"SELECT payload_json FROM {WORKFLOW_TABLE} WHERE workflow_id={self.dialect.bind}{suffix}",
            (workflow_id,),
        ).fetchone()
        return AccessWorkflowV2.model_validate_json(row[0]) if row else None

    def get(self, workflow_id: str) -> AccessWorkflowV2 | None:
        with self._connect() as conn:
            return self._get(conn, workflow_id)

    def create(self, record: AccessWorkflowV2, event: WorkflowEventV2) -> AccessWorkflowV2:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            inserted = conn.execute(
                f"INSERT INTO {WORKFLOW_TABLE} (workflow_id,owner_id,team_id,idempotency_key,"
                f"proposal_digest,version,record_digest,phase,updated_at,payload_json,source_task_id,"
                f"source_context_id) VALUES ({','.join([p] * 12)}) ON CONFLICT DO NOTHING",
                (
                    record.workflow_id,
                    record.owner_person_id,
                    record.team_id,
                    record.idempotency_key,
                    record.proposal_digest,
                    record.version,
                    record.record_digest,
                    record.phase.value,
                    record.updated_at_utc.isoformat(),
                    record.model_dump_json(),
                    record.source_task_id,
                    record.source_context_id,
                ),
            )
            row = conn.execute(
                f"SELECT payload_json FROM {WORKFLOW_TABLE} WHERE team_id={p} AND owner_id={p} AND idempotency_key={p}",
                (record.team_id, record.owner_person_id, record.idempotency_key),
            ).fetchone()
            if row is None:
                raise WorkflowConflict("This task already has a workflow. Continue its existing plan.")
            current = AccessWorkflowV2.model_validate_json(row[0])
            if current.proposal_digest != record.proposal_digest:
                raise WorkflowConflict("This request key already names a different workflow.")
            if inserted.rowcount == 1:
                self._append_event(conn, event)
            conn.commit()
            return current

    def save(
        self,
        record: AccessWorkflowV2,
        *,
        expected_version: int,
        events: tuple[WorkflowEventV2, ...] = (),
        revised_step_id: str | None = None,
    ) -> AccessWorkflowV2:
        if record.version != expected_version + 1:
            raise WorkflowConflict("Workflow versions must advance exactly once.")
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            current = self._get(conn, record.workflow_id, lock=True)
            if current is None or current.version != expected_version:
                raise WorkflowConflict("Workflow changed. Refresh its current state.")
            if revised_step_id is not None:
                self._check_correction(current, revised_step_id, events)
            if self._immutable_projection(current, revised_step_id) != self._immutable_projection(
                record, revised_step_id
            ):
                raise WorkflowConflict("Workflow identity and original proposal are immutable.")
            result = conn.execute(
                f"UPDATE {WORKFLOW_TABLE} SET version={p},record_digest={p},phase={p},"
                f"updated_at={p},payload_json={p} WHERE workflow_id={p} AND version={p}",
                (
                    record.version,
                    record.record_digest,
                    record.phase.value,
                    record.updated_at_utc.isoformat(),
                    record.model_dump_json(),
                    record.workflow_id,
                    expected_version,
                ),
            )
            if result.rowcount != 1:
                raise WorkflowConflict("Another worker changed this workflow.")
            for event in events:
                if event.workflow_id != record.workflow_id:
                    raise ValueError("Event does not belong to the updated workflow.")
                self._append_event(conn, event)
            conn.commit()
            return record

    @staticmethod
    def _check_correction(
        current: AccessWorkflowV2, revised_step_id: str, events: tuple[WorkflowEventV2, ...]
    ) -> None:
        """Re-check under the write lock that a step's expectations may still be corrected."""

        step = current.step(revised_step_id)
        if (
            current.terminal
            or step is None
            or step.blocked_by_person
            or step.request_ids
            or step.setups
            or step.state.value not in {"pending", *(state.value for state in OPEN_STEP_STATES)}
            or len(events) != 1
            or events[0].code != CORRECTION_EVENT_CODE
            or events[0].step_id != revised_step_id
        ):
            raise WorkflowConflict(
                "Only an unfinished step without linked requests or setup pages can change what it needs, "
                "with its correction event."
            )

    @staticmethod
    def _immutable_projection(record: AccessWorkflowV2, revised_step_id: str | None = None) -> dict[str, Any]:
        """The submission and step definitions; only an explicit correction changes a step's expectations."""

        # Trusted code keeps adding requests and setup pages that match no step.
        mutable = {"steps", "phase", "updated_at_utc", "version", "unassigned_request_ids", "unassigned_setups"}
        projection = record.model_dump(exclude=mutable)
        projection["steps"] = [
            step.model_dump(
                include=set(WorkflowStepProposalV2.model_fields)
                - (STEP_EXPECTATION_FIELDS if step.step_id == revised_step_id else set())
            )
            for step in record.steps
        ]
        return projection

    def _append_event(self, conn: Any, event: WorkflowEventV2) -> WorkflowEventV2:
        p = self.dialect.bind
        # The caller holds the workflow write lock. Increment and insert atomically.
        sequence_row = conn.execute(
            f"UPDATE {WORKFLOW_TABLE} SET event_sequence=event_sequence+1 WHERE workflow_id={p} RETURNING event_sequence",
            (event.workflow_id,),
        ).fetchone()
        if sequence_row is None:
            raise WorkflowConflict("Workflow event source is missing.")
        stored = event.model_copy(update={"sequence": int(sequence_row[0])})
        conn.execute(
            f"INSERT INTO {EVENT_TABLE} (workflow_id,sequence,event_id,kind,payload_json) VALUES ({','.join([p] * 5)})",
            (stored.workflow_id, stored.sequence, stored.event_id, stored.kind, stored.model_dump_json()),
        )
        return stored

    def append_event(self, event: WorkflowEventV2) -> WorkflowEventV2:
        with self._connect() as conn:
            conn.begin_write()
            if self._get(conn, event.workflow_id, lock=True) is None:
                raise WorkflowConflict("Workflow is missing.")
            stored = self._append_event(conn, event)
            conn.commit()
            return stored

    def list_owned(
        self, *, owner_id: str, team_id: str, offset: int = 0, include_terminal: bool = False
    ) -> list[AccessWorkflowV2]:
        p = self.dialect.bind
        status = "" if include_terminal else f" AND phase NOT IN {TERMINAL_PHASES}"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM {WORKFLOW_TABLE} WHERE owner_id={p} AND team_id={p}{status} "
                f"ORDER BY updated_at DESC, workflow_id DESC LIMIT {p} OFFSET {p}",
                (owner_id, team_id, WORKFLOW_PAGE_SIZE, max(0, offset)),
            ).fetchall()
        return [AccessWorkflowV2.model_validate_json(row[0]) for row in rows]

    def count_owned(self, *, owner_id: str, team_id: str, include_terminal: bool = False) -> int:
        p = self.dialect.bind
        status = "" if include_terminal else f" AND phase NOT IN {TERMINAL_PHASES}"
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {WORKFLOW_TABLE} WHERE owner_id={p} AND team_id={p}{status}",
                (owner_id, team_id),
            ).fetchone()
        return int(row[0])

    def event_count(self, workflow_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {EVENT_TABLE} WHERE workflow_id={self.dialect.bind}", (workflow_id,)
            ).fetchone()
        return int(row[0])

    def for_task(self, *, task_id: str, team_id: str) -> list[AccessWorkflowV2]:
        """Open workflows published by this task."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM {WORKFLOW_TABLE} WHERE team_id={p} AND source_task_id={p} "
                f"AND phase NOT IN {TERMINAL_PHASES} ORDER BY updated_at DESC LIMIT {p}",
                (team_id, task_id, WORKFLOW_PAGE_SIZE),
            ).fetchall()
        return [AccessWorkflowV2.model_validate_json(row[0]) for row in rows]

    def for_context(self, *, context_id: str, team_id: str) -> list[AccessWorkflowV2]:
        """Open workflows of tasks carrying this signed task context (one person's request and its delegations)."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM {WORKFLOW_TABLE} WHERE team_id={p} AND source_context_id={p} "
                f"AND phase NOT IN {TERMINAL_PHASES} ORDER BY updated_at DESC LIMIT {p}",
                (team_id, context_id, WORKFLOW_PAGE_SIZE),
            ).fetchall()
        return [AccessWorkflowV2.model_validate_json(row[0]) for row in rows]

    def events(
        self,
        workflow_id: str,
        *,
        after: int = 0,
        limit: int = WORKFLOW_PAGE_SIZE,
        newest_first: bool = False,
        offset: int = 0,
    ) -> list[WorkflowEventV2]:
        p = self.dialect.bind
        order = "DESC" if newest_first else "ASC"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM {EVENT_TABLE} WHERE workflow_id={p} AND sequence>{p} "
                f"ORDER BY sequence {order} LIMIT {p} OFFSET {p}",
                (workflow_id, max(0, after), max(1, min(limit, 10001)), max(0, offset)),
            ).fetchall()
        return [WorkflowEventV2.model_validate_json(row[0]) for row in rows]


__all__ = ["AccessWorkflowRepository", "CORRECTION_EVENT_CODE", "WorkflowConflict"]
