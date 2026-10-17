"""Schedule-owned worker policy and occurrence evidence.

Policy/target changes share the schedule transaction. Occurrence admission uses
an explicit connection participant called by the runtime snapshot unit of work;
only that commit can establish executable custody.
"""

from __future__ import annotations

from storage.read_only import repositories_are_read_only

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.schedule_worker_v1 import (
    SCHEDULE_WORKER_BINDING_SCHEMA_VERSION,
    ScheduleOccurrenceV1,
    ScheduleWorkerBindingV1,
    WorkerDefinitionRecordV1,
)
from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE, SqlDialect
from storage.sqlite_connection import connect_sqlite
from storage.database_clock import database_now


logger = logging.getLogger(__name__)

OLD_SCHEDULE_WORKER_BINDINGS_PRESENT = "old_schedule_worker_bindings_present"
# Fields of the removed permission system (binding schema version 1).
_REMOVED_BINDING_FIELDS = ("observed_policy_ids", "access_review_request_ids")
_OLD_AUTHORITY_MODES = {"authorized": "held_access"}
# Fields of saved definition records from before default tools (access plan
# Phase 7); such a record is reported and not used (get_definition_record).
_REMOVED_DEFINITION_RECORD_FIELDS = frozenset({"tool_groups", "registry_ids"})


class ScheduleWorkerConflict(RuntimeError):
    """A guarded policy or occurrence transition lost its expected version."""


class OldScheduleWorkerBinding(RuntimeError):
    """Saved scheduled-worker settings are in the old access format.

    The runtime does not rewrite them (no automatic migration): an
    administrator runs ``assistant_cli access approve-existing``, which
    rewrites them in place (``rewrite_old_bindings``).
    """

    def __init__(self, schedule_id: str | None) -> None:
        super().__init__(
            f"Saved scheduled-worker settings for schedule {schedule_id or 'unknown'} are in the old access format "
            f"(reason_code={OLD_SCHEDULE_WORKER_BINDINGS_PRESENT}). Stop Universal Agents and run: "
            "assistant_cli.py access approve-existing --config <instance_config.yaml> --by <your user id>"
        )
        self.schedule_id = schedule_id


def _is_old_binding(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("schema_version") != SCHEDULE_WORKER_BINDING_SCHEMA_VERSION


def binding_from_json(text: str) -> ScheduleWorkerBindingV1:
    """A saved binding; ``OldScheduleWorkerBinding`` for the old access format."""

    payload = json.loads(text)
    if _is_old_binding(payload):
        raise OldScheduleWorkerBinding(payload.get("schedule_id") if isinstance(payload, dict) else None)
    return ScheduleWorkerBindingV1.model_validate_json(text)


def rewritten_binding_json(text: str) -> str:
    """An old-format binding in the current format (drops removed fields, maps the authority mode)."""

    payload = {key: value for key, value in json.loads(text).items() if key not in _REMOVED_BINDING_FIELDS}
    payload["schema_version"] = SCHEDULE_WORKER_BINDING_SCHEMA_VERSION
    mode = str(payload.get("authority_mode") or "")
    payload["authority_mode"] = _OLD_AUTHORITY_MODES.get(mode, mode)
    return ScheduleWorkerBindingV1.model_validate(payload).model_dump_json()


def old_schedule_worker_bindings(settings: StorageSettings) -> tuple[str, ...]:
    """Schedule ids whose saved worker settings are in the old format, without creating anything.

    A SQLite database that does not exist yet has none. For Postgres a
    connection failure is logged and reported as none: the repository still
    refuses each old row when it is read.
    """

    if not settings.postgres_dsn and not (settings.sqlite_path and Path(settings.sqlite_path).is_file()):
        return ()
    try:
        connection = (
            connect_postgres(settings.postgres_dsn)
            if settings.postgres_dsn
            else connect_sqlite(str(settings.sqlite_path), row_factory=sqlite3.Row)
        )
        with connection as conn:
            rows = conn.execute(
                "SELECT schedule_id,payload_json,previous_binding_json FROM schedule_worker_bindings"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - a missing table means no bindings; see docstring
        if "schedule_worker_bindings" in str(exc):
            return ()
        logger.warning(
            "Could not check saved scheduled-worker settings for the old format; each old row is refused "
            "when read. event=schedule_worker_binding_check_failed error=%s",
            exc,
        )
        return ()
    return tuple(
        str(row[0])
        for row in rows
        if any(text and _is_old_binding(json.loads(text)) for text in (row[1], row[2]))
    )


def schedule_from_row(row: Any) -> ScheduledMessage:
    schedule = ScheduledMessage.model_validate_json(row["schedule_json"])
    overrides = {
        name: row[name]
        for name in (
            "next_fire_at",
            "paused_until",
            "expires_at_utc",
            "claim_team_id",
            "claim_named_agent_type",
            "claim_agent_id",
        )
    }
    return schedule.model_copy(update={**overrides, "enabled": bool(row["enabled"])})


class ScheduleWorkerRepository:
    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if repositories_are_read_only():
            return
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS schedule_worker_bindings (
                schedule_id TEXT PRIMARY KEY, team_id TEXT NOT NULL,
                owner_id TEXT NOT NULL, revision INTEGER NOT NULL,
                payload_json TEXT NOT NULL, pending_schedule_json TEXT,
                previous_binding_json TEXT
            )""")
            # Review evidence for a binding's target definition. Kept apart from
            # the binding so the binding's strict stored shape never changes for it.
            conn.execute("""CREATE TABLE IF NOT EXISTS schedule_worker_definition_records (
                schedule_id TEXT PRIMARY KEY, team_id TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS schedule_worker_team ON schedule_worker_bindings(team_id,owner_id)"
            )
            self.ensure_occurrence_schema(conn)
            conn.execute("""CREATE TABLE IF NOT EXISTS schedule_worker_attempts (
                occurrence_id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL,
                team_id TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                launch_id TEXT, next_attempt_at TEXT, reason TEXT,
                review_generation INTEGER NOT NULL DEFAULT 0, review_at TEXT
            )""")
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    def get_definition_record(
        self, schedule_id: str, team_id: str
    ) -> WorkerDefinitionRecordV1 | None:
        """The saved definition record, or None when there is none usable.

        A record saved before default tools replaced tool groups (access plan
        Phase 7) is review evidence in a removed shape. It is reported and
        treated as missing, never rewritten: the reconciliation pass records
        the current definition of a shipped type, and a changed team
        definition is reviewed without the earlier copy (the same path as any
        schedule saved without a record).
        """
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM schedule_worker_definition_records WHERE schedule_id={p} AND team_id={p}",
                (schedule_id, team_id),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        if isinstance(payload, dict) and _REMOVED_DEFINITION_RECORD_FIELDS & set(payload):
            logger.warning(
                "A scheduled worker's saved definition record names removed tool groups; it is not used. "
                "event=schedule_worker_definition_record_outdated schedule_id=%s team_id=%s",
                schedule_id,
                team_id,
            )
            return None
        return WorkerDefinitionRecordV1.model_validate(payload)

    def save_definition_record(self, record: WorkerDefinitionRecordV1) -> bool:
        """Save review evidence only for the binding revision it describes."""
        p = self.dialect.bind
        suffix = " FOR UPDATE" if self.dialect == POSTGRES else ""
        with self._connect() as conn:
            conn.begin_write()
            row = conn.execute(
                f"SELECT revision FROM schedule_worker_bindings WHERE schedule_id={p} AND team_id={p}{suffix}",
                (record.schedule_id, record.team_id),
            ).fetchone()
            if row is None or int(row[0]) != record.policy_revision:
                conn.commit()
                return False
            self._write_definition_record(conn, record)
            conn.commit()
            return True

    def _write_definition_record(
        self, conn: Any, record: WorkerDefinitionRecordV1
    ) -> None:
        p = self.dialect.bind
        conn.execute(
            f"INSERT INTO schedule_worker_definition_records(schedule_id,team_id,payload_json) VALUES ({p},{p},{p}) ON CONFLICT(schedule_id) DO UPDATE SET team_id=excluded.team_id,payload_json=excluded.payload_json",
            (record.schedule_id, record.team_id, record.model_dump_json()),
        )

    @staticmethod
    def ensure_occurrence_schema(conn: Any) -> None:
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_occurrences (
            occurrence_id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL,
            task_id TEXT NOT NULL UNIQUE, team_id TEXT NOT NULL,
            owner_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS schedule_occurrence_schedule ON schedule_occurrences(schedule_id)"
        )

    @staticmethod
    def transfer_custody(
        conn: Any,
        dialect: SqlDialect,
        *,
        sources: tuple[str, ...],
        replacement_agent_id: str,
        team_id: str | None,
        task_ids: tuple[str, ...],
    ) -> None:
        """Move occurrence evidence with the runtime recovery commit."""
        if not task_ids or not team_id:
            return
        p = dialect.bind
        rows = conn.execute(
            f"SELECT payload_json FROM schedule_occurrences WHERE team_id={p} AND agent_id IN ({','.join([p] * len(sources))})",
            (team_id, *sources),
        ).fetchall()
        for row in rows:
            record = ScheduleOccurrenceV1.model_validate_json(row[0])
            if (record.coalesced_into_task_id or record.task_id) in task_ids:
                record = record.model_copy(update={"agent_id": replacement_agent_id})
                conn.execute(
                    f"UPDATE schedule_occurrences SET agent_id={p},payload_json={p} WHERE occurrence_id={p}",
                    (
                        replacement_agent_id,
                        record.model_dump_json(),
                        record.occurrence_id,
                    ),
                )

    def get_binding(
        self,
        schedule_id: str,
        *,
        team_id: str,
        owner_id: str | None = None,
        active: bool = False,
    ) -> ScheduleWorkerBindingV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            column = (
                "CASE WHEN pending_schedule_json IS NULL THEN payload_json ELSE previous_binding_json END"
                if active
                else "payload_json"
            )
            row = conn.execute(
                f"SELECT {column} FROM schedule_worker_bindings WHERE schedule_id={p} AND team_id={p}",
                (schedule_id, team_id),
            ).fetchone()
        binding = binding_from_json(row[0]) if row and row[0] else None
        return (
            binding
            if binding is None
            or owner_id is None
            or binding.owner_canonical_user_id == owner_id
            else None
        )

    def list_bindings(self, team_id: str) -> tuple[ScheduleWorkerBindingV1, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM schedule_worker_bindings WHERE team_id={self.dialect.bind} ORDER BY schedule_id",
                (team_id,),
            ).fetchall()
        return tuple(binding_from_json(row[0]) for row in rows)

    def configure(
        self,
        binding: ScheduleWorkerBindingV1,
        candidate: ScheduledMessage,
        *,
        expected_revision: int,
        expected_control_digest: str,
        system_mutation_permit: Any = None,
        require_unclaimed: bool = False,
        system_request_binding: dict[str, Any] | None = None,
        definition_record: WorkerDefinitionRecordV1 | None = None,
    ) -> bool:
        from custom_agents.schedule_management import schedule_control_state_digest

        p = self.dialect.bind
        suffix = " FOR UPDATE" if self.dialect == POSTGRES else ""
        if binding.revision != expected_revision + 1:
            raise ScheduleWorkerConflict("policy_revision_must_advance")
        _require_record_for(binding, definition_record)
        with self._connect() as conn:
            conn.begin_write()
            row = conn.execute(
                f"SELECT * FROM scheduled_messages WHERE schedule_id={p}{suffix}",
                (binding.schedule_id,),
            ).fetchone()
            if (
                row is None
                or row["claim_team_id"] != binding.team_id
                or row["owner_canonical_user_id"] != binding.owner_canonical_user_id
            ):
                raise ScheduleWorkerConflict("schedule_not_found_in_scope")
            if (
                schedule_control_state_digest(schedule_from_row(row))
                != expected_control_digest
            ):
                raise ScheduleWorkerConflict("schedule_revision_conflict")
            from custom_agents.schedule_management import require_schedule_mutation_permit

            require_schedule_mutation_permit(
                schedule_from_row(row), operation="update",
                permit=system_mutation_permit, candidate=candidate,
                request_binding=system_request_binding,
            )
            stored = conn.execute(
                f"SELECT revision,CASE WHEN pending_schedule_json IS NOT NULL THEN previous_binding_json ELSE payload_json END FROM schedule_worker_bindings WHERE schedule_id={p}",
                (binding.schedule_id,),
            ).fetchone()
            if (int(stored[0]) if stored else 0) != expected_revision:
                raise ScheduleWorkerConflict("policy_revision_conflict")
            now = database_now(conn).isoformat()
            pending = bool(
                row["claim_token"]
                and row["claim_expires_at"]
                and row["claim_expires_at"] > now
            )
            if pending and require_unclaimed:
                raise ScheduleWorkerConflict("schedule_upgrade_requires_no_active_claim")
            conn.execute(
                f"INSERT INTO schedule_worker_bindings(schedule_id,team_id,owner_id,revision,payload_json,pending_schedule_json) VALUES ({','.join([p] * 6)}) ON CONFLICT(schedule_id) DO UPDATE SET revision=excluded.revision,payload_json=excluded.payload_json,pending_schedule_json=excluded.pending_schedule_json",
                (
                    binding.schedule_id,
                    binding.team_id,
                    binding.owner_canonical_user_id,
                    binding.revision,
                    binding.model_dump_json(),
                    candidate.model_dump_json() if pending else None,
                ),
            )
            conn.execute(
                f"UPDATE schedule_worker_bindings SET previous_binding_json={p} WHERE schedule_id={p}",
                (stored[1] if pending and stored else None, binding.schedule_id),
            )
            if definition_record is not None:
                self._write_definition_record(conn, definition_record)
            if not pending:
                # Run notes are not control state: keep the ones runs wrote
                # since the candidate was read, rather than the candidate's copy.
                self._apply_target(
                    conn, candidate.model_copy(update={"schedule_memory": schedule_from_row(row).schedule_memory})
                )
            conn.commit()
            return pending

    def _apply_target(self, conn: Any, schedule: ScheduledMessage) -> None:
        p = self.dialect.bind
        # Preserve due time, claims, counts and memory. Only the guarded control
        # candidate is replaced; firing bookkeeping owns the other columns.
        conn.execute(
            f"UPDATE scheduled_messages SET claim_named_agent_type={p},claim_agent_id={p},schedule_json={p},enabled={p} WHERE schedule_id={p}",
            (
                schedule.claim_named_agent_type,
                schedule.claim_agent_id,
                schedule.model_dump_json(),
                int(schedule.enabled),
                schedule.schedule_id,
            ),
        )

    def apply_pending(self, schedule_id: str, team_id: str) -> bool:
        p = self.dialect.bind
        suffix = " FOR UPDATE" if self.dialect == POSTGRES else ""
        with self._connect() as conn:
            conn.begin_write()
            row = conn.execute(
                f"SELECT * FROM scheduled_messages WHERE schedule_id={p} AND claim_team_id={p}{suffix}",
                (schedule_id, team_id),
            ).fetchone()
            pending = conn.execute(
                f"SELECT pending_schedule_json FROM schedule_worker_bindings WHERE schedule_id={p} AND team_id={p}",
                (schedule_id, team_id),
            ).fetchone()
            if row is None or pending is None or not pending[0]:
                return False
            now = database_now(conn).isoformat()
            if (
                row["claim_token"]
                and row["claim_expires_at"]
                and row["claim_expires_at"] > now
            ):
                return False
            candidate = ScheduledMessage.model_validate_json(pending[0])
            current = schedule_from_row(row)
            candidate = current.model_copy(
                update={
                    "claim_named_agent_type": candidate.claim_named_agent_type,
                    "claim_agent_id": candidate.claim_agent_id,
                    "payload_header": candidate.payload_header,
                    "payload_meta": candidate.payload_meta,
                }
            )
            self._apply_target(conn, candidate)
            conn.execute(
                f"UPDATE schedule_worker_bindings SET pending_schedule_json=NULL,previous_binding_json=NULL WHERE schedule_id={p}",
                (schedule_id,),
            )
            conn.commit()
            return True

    def get_occurrence(
        self, occurrence_id: str, team_id: str
    ) -> ScheduleOccurrenceV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM schedule_occurrences WHERE occurrence_id={p} AND team_id={p}",
                (occurrence_id, team_id),
            ).fetchone()
        return ScheduleOccurrenceV1.model_validate_json(row[0]) if row else None

    def for_task(self, task_id: str, team_id: str) -> ScheduleOccurrenceV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM schedule_occurrences WHERE task_id={p} AND team_id={p}",
                (task_id, team_id),
            ).fetchone()
        return ScheduleOccurrenceV1.model_validate_json(row[0]) if row else None

    def latest_occurrence(
        self, schedule_id: str, team_id: str
    ) -> ScheduleOccurrenceV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM schedule_occurrences WHERE schedule_id={p} AND team_id={p}",
                (schedule_id, team_id),
            ).fetchall()
        records = [ScheduleOccurrenceV1.model_validate_json(row[0]) for row in rows]
        return (
            max(
                records,
                key=lambda record: record.admitted_at_utc or record.created_at_utc,
            )
            if records
            else None
        )

    @staticmethod
    def commit_admission(
        conn: Any, dialect: SqlDialect, occurrence: ScheduleOccurrenceV1
    ) -> None:
        """Participate in the runtime-snapshot transaction; never commit here."""
        p = dialect.bind
        suffix = " FOR UPDATE" if dialect == POSTGRES else ""
        columns = (
            "claim_token",
            "claimed_by_agent_id",
            "next_fire_at",
            "enabled",
            "claim_expires_at",
            "paused_until",
            "expires_at_utc",
            "claim_team_id",
            "owner_canonical_user_id",
            "claim_named_agent_type",
            "claim_agent_id",
        )
        result = conn.execute(
            f"SELECT {','.join(columns)} FROM scheduled_messages WHERE schedule_id={p}{suffix}",
            (occurrence.schedule_id,),
        ).fetchone()
        row = (
            {key: result[index] for index, key in enumerate(columns)}
            if result
            else None
        )
        now = database_now(conn).isoformat()
        if (
            row is None
            or row["claim_token"] != occurrence.claim_token
            or row["claimed_by_agent_id"] != occurrence.agent_id
            or row["next_fire_at"] != occurrence.due_at
            or not row["enabled"]
            or not row["claim_expires_at"]
            or row["claim_expires_at"] <= now
            or (row["paused_until"] and row["paused_until"] > now)
            or (row["expires_at_utc"] and row["expires_at_utc"] <= now)
        ):
            raise ScheduleWorkerConflict("stale_schedule_admission_claim")
        if (row["claim_team_id"] or "") != occurrence.team_id or (
            row["owner_canonical_user_id"] or ""
        ) != occurrence.owner_canonical_user_id:
            raise ScheduleWorkerConflict("schedule_admission_scope_mismatch")
        if row["claim_named_agent_type"] != occurrence.named_agent_type or (
            row["claim_agent_id"] and row["claim_agent_id"] != occurrence.agent_id
        ):
            raise ScheduleWorkerConflict("schedule_admission_target_mismatch")
        if occurrence.policy_revision is not None:
            policy = conn.execute(
                f"SELECT COALESCE(previous_binding_json,payload_json) FROM schedule_worker_bindings WHERE schedule_id={p} AND team_id={p}{suffix}",
                (occurrence.schedule_id, occurrence.team_id),
            ).fetchone()
            binding = binding_from_json(policy[0]) if policy else None
            if (
                binding is None
                or binding.revision != occurrence.policy_revision
                or binding.execution_digest != occurrence.execution_digest
            ):
                raise ScheduleWorkerConflict("schedule_policy_changed_before_admission")
        if occurrence.launch_id is not None:
            from custom_messages.contracts.schedule_worker_v1 import (
                AgentLaunchV1,
                AgentLaunchState,
            )

            row = conn.execute(
                f"SELECT payload_json FROM agent_launches WHERE launch_id={p}{suffix}",
                (occurrence.launch_id,),
            ).fetchone()
            launch = AgentLaunchV1.model_validate_json(row[0]) if row else None
            if (
                launch is None
                or launch.agent_id != occurrence.agent_id
                or launch.state != AgentLaunchState.READY
                or launch.lease_expires_at_utc.isoformat() <= now
            ):
                raise ScheduleWorkerConflict("worker_launch_custody_lost")
        record = occurrence.model_copy(
            update={
                "state": "coalesced"
                if occurrence.coalesced_into_task_id
                else "admitted",
                "admitted_at_utc": database_now(conn),
            }
        )
        inserted = conn.execute(
            f"INSERT INTO schedule_occurrences(occurrence_id,schedule_id,task_id,team_id,owner_id,agent_id,payload_json) VALUES ({','.join([p] * 7)}) ON CONFLICT(occurrence_id) DO NOTHING",
            (
                record.occurrence_id,
                record.schedule_id,
                record.task_id,
                record.team_id,
                record.owner_canonical_user_id,
                record.agent_id,
                record.model_dump_json(),
            ),
        )
        if inserted.rowcount != 1:
            raise ScheduleWorkerConflict("schedule_occurrence_already_admitted")

    def attempt(self, occurrence_id: str, team_id: str) -> dict[str, Any] | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM schedule_worker_attempts WHERE occurrence_id={p} AND team_id={p}",
                (occurrence_id, team_id),
            ).fetchone()
        return dict(row) if row else None

    def record_attempt(
        self,
        *,
        occurrence_id: str,
        schedule_id: str,
        team_id: str,
        launch_id: str | None,
        next_attempt_at: datetime,
        reason: str | None,
        increment: bool,
    ) -> None:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO schedule_worker_attempts(occurrence_id,schedule_id,team_id,attempts,launch_id,next_attempt_at,reason) VALUES ({','.join([p] * 7)}) ON CONFLICT(occurrence_id) DO UPDATE SET attempts=schedule_worker_attempts.attempts+CASE WHEN schedule_worker_attempts.launch_id=excluded.launch_id THEN 0 ELSE excluded.attempts END,launch_id=excluded.launch_id,next_attempt_at=excluded.next_attempt_at,reason=excluded.reason",
                (
                    occurrence_id,
                    schedule_id,
                    team_id,
                    int(increment),
                    launch_id,
                    next_attempt_at.isoformat(),
                    reason,
                ),
            )
            conn.commit()

    def mark_blocked(
        self,
        schedule_id: str,
        team_id: str,
        reason: str | None,
    ) -> None:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            suffix = " FOR UPDATE" if self.dialect == POSTGRES else ""
            row = conn.execute(
                f"SELECT payload_json FROM schedule_worker_bindings WHERE schedule_id={p} AND team_id={p}{suffix}",
                (schedule_id, team_id),
            ).fetchone()
            if row:
                current = binding_from_json(row[0])
                binding = current.model_copy(
                    update={
                        "blocked_reason": reason,
                        "blocked_since_utc": (
                            current.blocked_since_utc or database_now(conn)
                        )
                        if reason
                        else None,
                    }
                )
                conn.execute(
                    f"UPDATE schedule_worker_bindings SET payload_json={p} WHERE schedule_id={p} AND revision={p}",
                    (binding.model_dump_json(), schedule_id, binding.revision),
                )
            conn.commit()

    def rebind_execution(
        self,
        schedule_id: str,
        team_id: str,
        *,
        expected_digest: str,
        new_digest: str,
        owner_id: str,
        clear_reasons: frozenset[str] = frozenset(),
    ) -> bool:
        """Point the saved policy at a schedule changed by an authorized path; True if done.

        Callers change a schedule under the authority a re-save needs (the
        owner's own request, a person's approval, or trusted runtime code) and
        then move the binding's execution identity along, compare-and-set on
        ``expected_digest`` so a binding that was already stale (or changed
        meanwhile) is left for review. The revision is kept: the policy did not
        change, and in-flight starts stay valid. A block listed in
        ``clear_reasons`` is lifted because its cause is gone. A binding with a
        retarget waiting behind a claim is left alone.
        """

        p = self.dialect.bind
        suffix = " FOR UPDATE" if self.dialect == POSTGRES else ""
        with self._connect() as conn:
            conn.begin_write()
            row = conn.execute(
                f"SELECT payload_json,pending_schedule_json FROM schedule_worker_bindings WHERE schedule_id={p} AND team_id={p}{suffix}",
                (schedule_id, team_id),
            ).fetchone()
            if row is None or row[1]:
                return False
            current = binding_from_json(row[0])
            if (
                current.execution_digest != expected_digest
                or current.owner_canonical_user_id != owner_id
            ):
                return False
            update: dict[str, Any] = {"execution_digest": new_digest}
            if current.blocked_reason in clear_reasons:
                update.update(blocked_reason=None, blocked_since_utc=None)
            binding = current.model_copy(update=update)
            cursor = conn.execute(
                f"UPDATE schedule_worker_bindings SET payload_json={p} WHERE schedule_id={p} AND revision={p}",
                (binding.model_dump_json(), schedule_id, binding.revision),
            )
            conn.commit()
            return int(cursor.rowcount) == 1

    def rewrite_old_bindings(self) -> tuple[str, ...]:
        """Rewrite every old-format binding in place; returns the schedule ids changed.

        Only ``assistant_cli access approve-existing`` calls this (an
        administrator's explicit step). Each rewritten row validates against
        the current model before it is written.
        """

        p = self.dialect.bind
        suffix = " FOR UPDATE" if self.dialect == POSTGRES else ""
        changed: list[str] = []
        with self._connect() as conn:
            conn.begin_write()
            rows = conn.execute(
                f"SELECT schedule_id,payload_json,previous_binding_json FROM schedule_worker_bindings{suffix}"
            ).fetchall()
            for schedule_id, payload, previous in rows:
                old_payload = bool(payload) and _is_old_binding(json.loads(payload))
                old_previous = bool(previous) and _is_old_binding(json.loads(previous))
                if not (old_payload or old_previous):
                    continue
                conn.execute(
                    f"UPDATE schedule_worker_bindings SET payload_json={p},previous_binding_json={p} WHERE schedule_id={p}",
                    (
                        rewritten_binding_json(payload) if old_payload else payload,
                        rewritten_binding_json(previous) if old_previous else previous,
                        schedule_id,
                    ),
                )
                changed.append(str(schedule_id))
            conn.commit()
        return tuple(changed)

    @staticmethod
    def record_terminal_task(
        conn: Any, dialect: SqlDialect, *, agent_id: str, task_id: str, outcome: str
    ) -> None:
        """Record completion with the runtime task's terminal snapshot transaction."""
        p = dialect.bind
        rows = conn.execute(
            f"SELECT payload_json FROM schedule_occurrences WHERE agent_id={p}",
            (agent_id,),
        ).fetchall()
        for row in rows:
            record = ScheduleOccurrenceV1.model_validate_json(row[0])
            if (
                record.coalesced_into_task_id or record.task_id
            ) == task_id and record.finished_at_utc is None:
                # A coalesced occurrence never ran itself; keep that fact and
                # only close it with the run it was merged into.
                record = record.model_copy(
                    update={
                        "state": "coalesced" if record.coalesced_into_task_id else "finished",
                        "finished_at_utc": database_now(conn),
                        "outcome": outcome,
                    }
                )
                conn.execute(
                    f"UPDATE schedule_occurrences SET payload_json={p} WHERE occurrence_id={p}",
                    (record.model_dump_json(), record.occurrence_id),
                )

    def unfinished_occurrences(
        self, schedule_id: str, team_id: str
    ) -> tuple[ScheduleOccurrenceV1, ...]:
        """Admitted runs of this schedule that have not finished yet."""
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM schedule_occurrences WHERE schedule_id={p} AND team_id={p}",
                (schedule_id, team_id),
            ).fetchall()
        records = (ScheduleOccurrenceV1.model_validate_json(row[0]) for row in rows)
        return tuple(
            record
            for record in records
            if record.state == "admitted" and record.finished_at_utc is None
        )

    def occurrences_for_schedule(
        self, schedule_id: str, team_id: str
    ) -> tuple[ScheduleOccurrenceV1, ...]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM schedule_occurrences WHERE schedule_id={p} AND team_id={p}",
                (schedule_id, team_id),
            ).fetchall()
        return tuple(ScheduleOccurrenceV1.model_validate_json(row[0]) for row in rows)

    def ran_again_since(self, schedule_id: str, team_id: str, due_at: str) -> bool:
        """Whether a later occurrence of this schedule ran as its own task.

        Occurrences coalesced into an earlier run did not run themselves, so
        they do not count: they rely on that earlier run.
        """
        cutoff = _occurrence_time(due_at)
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM schedule_occurrences WHERE schedule_id={p} AND team_id={p}",
                (schedule_id, team_id),
            ).fetchall()
        for row in rows:
            record = ScheduleOccurrenceV1.model_validate_json(row[0])
            if (
                record.coalesced_into_task_id is None
                and record.state in {"admitted", "finished"}
                and _occurrence_time(record.due_at) > cutoff
            ):
                return True
        return False

    def reassign_run(
        self,
        task_id: str,
        team_id: str,
        *,
        from_agent_id: str,
        to_agent_id: str,
        launch_id: str | None,
    ) -> int:
        """Move an unfinished run to the worker that accepted it again.

        Fenced on the previous owner so a run that another worker already took
        over, or that finished, is never moved. Occurrences coalesced into the
        run move with it so they close when it finishes.
        """
        p = self.dialect.bind
        moved = 0
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM schedule_occurrences WHERE team_id={p} AND agent_id={p}",
                (team_id, from_agent_id),
            ).fetchall()
            for row in rows:
                record = ScheduleOccurrenceV1.model_validate_json(row[0])
                if (
                    record.coalesced_into_task_id or record.task_id
                ) != task_id or record.finished_at_utc is not None:
                    continue
                record = record.model_copy(
                    update={"agent_id": to_agent_id, "launch_id": launch_id}
                )
                conn.execute(
                    f"UPDATE schedule_occurrences SET agent_id={p},payload_json={p} "
                    f"WHERE occurrence_id={p} AND agent_id={p}",
                    (to_agent_id, record.model_dump_json(), record.occurrence_id, from_agent_id),
                )
                moved += 1
            conn.commit()
        return moved

    def supersede_run(self, task_id: str, team_id: str, *, reason: str) -> bool:
        """Close an interrupted run that will not run again."""
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM schedule_occurrences WHERE task_id={p} AND team_id={p}",
                (task_id, team_id),
            ).fetchone()
            if row is None:
                return False
            record = ScheduleOccurrenceV1.model_validate_json(row[0])
            if record.finished_at_utc is not None:
                return False
            record = record.model_copy(
                update={
                    "state": "superseded",
                    "finished_at_utc": database_now(conn),
                    "outcome": "superseded",
                    "reason": reason,
                }
            )
            conn.execute(
                f"UPDATE schedule_occurrences SET payload_json={p} WHERE occurrence_id={p}",
                (record.model_dump_json(), record.occurrence_id),
            )
            conn.commit()
        return True

    def prune(self, team_id: str, *, before: datetime) -> None:
        """Retain replay identities while a schedule exists or task custody is live."""
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                f"DELETE FROM schedule_worker_attempts WHERE team_id={p} AND next_attempt_at < {p} AND occurrence_id IN (SELECT occurrence_id FROM schedule_occurrences WHERE team_id={p})",
                (team_id, before.isoformat(), team_id),
            )
            rows = conn.execute(
                f"SELECT payload_json FROM schedule_occurrences WHERE team_id={p} AND schedule_id NOT IN (SELECT schedule_id FROM scheduled_messages)",
                (team_id,),
            ).fetchall()
            for row in rows:
                occurrence = ScheduleOccurrenceV1.model_validate_json(row[0])
                if occurrence.finished_at_utc and occurrence.finished_at_utc < before:
                    conn.execute(
                        f"DELETE FROM schedule_occurrences WHERE occurrence_id={p}",
                        (occurrence.occurrence_id,),
                    )
            conn.execute(
                f"DELETE FROM schedule_worker_bindings WHERE team_id={p} AND schedule_id NOT IN (SELECT schedule_id FROM scheduled_messages) AND schedule_id NOT IN (SELECT schedule_id FROM schedule_occurrences)",
                (team_id,),
            )
            conn.execute(
                f"DELETE FROM schedule_worker_definition_records WHERE team_id={p} AND schedule_id NOT IN (SELECT schedule_id FROM schedule_worker_bindings)",
                (team_id,),
            )
            conn.commit()


def _require_record_for(
    binding: ScheduleWorkerBindingV1, record: WorkerDefinitionRecordV1 | None
) -> None:
    if record is not None and (
        record.schedule_id,
        record.team_id,
        record.policy_revision,
    ) != (binding.schedule_id, binding.team_id, binding.revision):
        raise ScheduleWorkerConflict("definition_record_does_not_match_policy")


def _occurrence_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
