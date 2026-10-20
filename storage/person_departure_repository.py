"""Records a person leaves behind when they are disabled or removed, held or deleted.

A person's work and personal records live in the manager's database
(``task_history.db``, or the manager's Postgres database): task records,
objectives, memory-review jobs, their personal memory ("usermem"), their
conversations, and their own knowledge (personal pages, pending team
contributions, knowledge edit log). When an administrator removes a person (``/user remove``):

- their task records (with the after-action report reviews about their work),
  objectives and memory-review jobs are deleted at once
  (``delete_person_records``);
- their personal memory, conversations and own knowledge are held (``hold_person_records``):
  the rows move, in one transaction, into ``person_held_rows`` as JSON and
  leave the tables agents read, so no agent sees them. ``/user restore``
  within the retention time moves them back (``restore_person_records``); a
  maintenance run after it deletes them (``delete_held_rows``).

Why one generic holding table instead of a "held" flag on every table: every
reader of those tables would otherwise have to skip held rows, and one missed
reader would show a removed person's memory to an agent. Moving the rows is
one transaction each way, needs no change to the tables' owners, and the list
of what is held is in one place (``_held_record_sets``).

``person_departures`` records one hold per person: when they were removed,
until when their data is held (``gateway.users.removed_data_retention_days``,
chosen by the gateway), and whether it was restored or purged. The gateway
writes it before it tells the manager, the manager holds the rows, and either
purges. The table sits beside the access store, which both already open.

This module writes other repositories' tables directly, on purpose: holding
must move rows of several tables in one transaction, and the tables are all in
this database. Identifiers come only from the constants below.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from id_generation import new_id
from storage.knowledge_repository import (
    KNOWLEDGE_EVENTS_PERSON_COLUMN,
    KNOWLEDGE_EVENTS_TABLE,
    KNOWLEDGE_OWNER_COLUMN,
    KNOWLEDGE_PERSONAL_TABLES,
    KNOWLEDGE_RECEIPTS_PERSON_COLUMN,
    KNOWLEDGE_RECEIPTS_TABLE,
)
from storage.change_ledger import CHANGE_LEDGER_TABLE, CHANGE_PERSON_COLUMN, CHANGE_REQUESTS_TABLE
from storage.config import StorageSettings
from storage.pinned_memory_repository import USERMEM_BANK
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite

logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_BYTES_KEY = "$bytes"


@dataclass(frozen=True)
class PersonHold:
    """One person's held data: until when, and whether it was restored or purged."""

    team_id: str
    person_id: str
    removed_at: datetime
    hold_until: datetime
    restored_at: datetime | None = None
    purged_at: datetime | None = None

    def restorable(self, now: datetime) -> bool:
        """Still held and inside the retention time."""

        return self.restored_at is None and self.purged_at is None and now < self.hold_until


@dataclass(frozen=True)
class _RecordSet:
    """Rows of one table that belong to the person: ``column`` is one of ``values``."""

    table: str
    column: str
    values: tuple[str, ...]
    extra: tuple[tuple[str, str], ...] = ()

    def where(self, bind: str) -> tuple[str, tuple[str, ...]]:
        for name in (self.table, self.column, *(column for column, _ in self.extra)):
            if not _IDENTIFIER.fullmatch(name):
                raise ValueError(f"Unsafe identifier: {name!r}")
        clauses = [f"{self.column} IN ({', '.join(bind for _ in self.values)})"]
        clauses += [f"{column} = {bind}" for column, _ in self.extra]
        return " AND ".join(clauses), (*self.values, *(value for _, value in self.extra))


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: Any) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value else None


def _encode(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {_BYTES_KEY: base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {_BYTES_KEY}:
        return base64.b64decode(value[_BYTES_KEY])
    return value


class PersonDepartureRepository:
    """Holds, restores and deletes a departed person's records (module docstring)."""

    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS person_departures (
                team_id TEXT NOT NULL, person_id TEXT NOT NULL,
                removed_at TEXT NOT NULL, hold_until TEXT NOT NULL,
                restored_at TEXT, purged_at TEXT,
                PRIMARY KEY (team_id, person_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS person_held_rows (
                held_row_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, person_id TEXT NOT NULL,
                position INTEGER NOT NULL, source_table TEXT NOT NULL, row_json TEXT NOT NULL,
                held_at TEXT NOT NULL
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_person_held_rows_person ON person_held_rows(team_id, person_id, position)"
            )
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    # -- the hold record ---------------------------------------------------------------

    def record_hold(self, team_id: str, person_id: str, *, at: datetime, hold_until: datetime) -> PersonHold:
        """Start (or start again) holding the person's data until ``hold_until``."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(f"DELETE FROM person_departures WHERE team_id={p} AND person_id={p}", (team_id, person_id))
            conn.execute(
                f"INSERT INTO person_departures (team_id, person_id, removed_at, hold_until) VALUES ({p}, {p}, {p}, {p})",
                (team_id, person_id, _utc(at), _utc(hold_until)),
            )
            conn.commit()
        return PersonHold(team_id, person_id, at, hold_until)

    def hold_of(self, team_id: str, person_id: str) -> PersonHold | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM person_departures WHERE team_id={p} AND person_id={p}", (team_id, person_id)
            ).fetchone()
        return self._hold(row) if row else None

    @staticmethod
    def _hold(row: Any) -> PersonHold:
        return PersonHold(
            team_id=row["team_id"],
            person_id=row["person_id"],
            removed_at=_parse(row["removed_at"]),  # type: ignore[arg-type]
            hold_until=_parse(row["hold_until"]),  # type: ignore[arg-type]
            restored_at=_parse(row["restored_at"]),
            purged_at=_parse(row["purged_at"]),
        )

    def expired_holds(self, team_id: str, now: datetime) -> list[PersonHold]:
        """Holds past their time that were neither restored nor purged."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM person_departures WHERE team_id={p} AND restored_at IS NULL AND purged_at IS NULL",
                (team_id,),
            ).fetchall()
        return [hold for hold in (self._hold(row) for row in rows) if hold.hold_until <= now]

    def mark_restored(self, team_id: str, person_id: str, *, at: datetime) -> None:
        self._mark(team_id, person_id, "restored_at", at)

    def mark_purged(self, team_id: str, person_id: str, *, at: datetime) -> None:
        self._mark(team_id, person_id, "purged_at", at)

    def _mark(self, team_id: str, person_id: str, column: str, at: datetime) -> None:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                f"UPDATE person_departures SET {column}={p} WHERE team_id={p} AND person_id={p}",
                (_utc(at), team_id, person_id),
            )
            conn.commit()

    # -- which rows belong to the person -------------------------------------------------

    def _table_exists(self, conn: Any, table: str) -> bool:
        p = self.dialect.bind
        if self.dialect is POSTGRES:
            # Resolve the same table the following unqualified query uses.
            # Another instance's schema must not make a missing table look present.
            sql = f"SELECT 1 FROM pg_catalog.pg_class WHERE oid=to_regclass({p}) AND relkind IN ('r', 'p')"
        else:
            sql = f"SELECT 1 FROM sqlite_master WHERE type='table' AND name={p}"
        return conn.execute(sql, (table,)).fetchone() is not None

    def _columns(self, conn: Any, table: str) -> set[str]:
        if self.dialect is POSTGRES:
            rows = conn.execute(
                f"""SELECT attname FROM pg_catalog.pg_attribute
                    WHERE attrelid=to_regclass({self.dialect.bind}) AND attnum > 0 AND NOT attisdropped""",
                (table,),
            ).fetchall()
            return {str(row[0]) for row in rows}
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _ids(self, conn: Any, table: str, id_column: str, owner_column: str, person_id: str) -> tuple[str, ...]:
        if not self._table_exists(conn, table):
            return ()
        p = self.dialect.bind
        rows = conn.execute(f"SELECT {id_column} FROM {table} WHERE {owner_column}={p}", (person_id,)).fetchall()
        return tuple(sorted({str(row[0]) for row in rows if row[0]}))

    def _held_record_sets(self, conn: Any, person_id: str) -> list[_RecordSet]:
        """Personal memory and conversations: held on removal, restorable."""

        conversations = self._ids(conn, "conversation_threads", "conversation_id", "owner_canonical_user_id", person_id)
        person = (person_id,)
        sets = [
            _RecordSet("pinned_memory_facts_active", "canonical_user_id", person, (("bank", USERMEM_BANK),)),
            _RecordSet("pinned_memory_fact_events", "canonical_user_id", person, (("bank", USERMEM_BANK),)),
            _RecordSet("conversation_task_links", "owner_canonical_user_id", person),
            _RecordSet("conversation_context_epochs", "owner_canonical_user_id", person),
            _RecordSet("conversation_thread_tags", "canonical_user_id", person),
            _RecordSet("conversation_turn_tags", "canonical_user_id", person),
        ]
        if conversations:
            sets += [
                _RecordSet("conversation_task_links", "conversation_id", conversations),
                _RecordSet("conversation_context_epochs", "conversation_id", conversations),
                _RecordSet("conversation_thread_tags", "conversation_id", conversations),
                _RecordSet("conversation_turn_tags", "conversation_id", conversations),
                _RecordSet("conversation_artifacts", "conversation_id", conversations),
            ]
        # The person's own knowledge (personal pages and everything under them,
        # and their pending team contributions) and their knowledge edit log.
        # Team pages stay: published knowledge belongs to the team.
        sets += [_RecordSet(table, KNOWLEDGE_OWNER_COLUMN, person) for table in KNOWLEDGE_PERSONAL_TABLES]
        sets.append(_RecordSet(KNOWLEDGE_EVENTS_TABLE, KNOWLEDGE_EVENTS_PERSON_COLUMN, person))
        # The threads last, so the conversation ids above are found first.
        sets.append(_RecordSet("conversation_threads", "owner_canonical_user_id", person))
        return sets

    def _deleted_record_sets(self, conn: Any, person_id: str) -> list[_RecordSet]:
        """Task records and objectives: deleted at once on removal."""

        objectives = self._ids(conn, "objectives", "objective_id", "owner_canonical_user_id", person_id)
        person = (person_id,)
        sets = [
            _RecordSet("completed_tasks", "owner_canonical_user_id", person),
            # After-action reports about their work, recorded from verified
            # identity (storage.after_action_report_repository).
            _RecordSet("after_action_report_reviews", "person_id", person),
            # A lesson's receipt has the same lifetime as its review; the
            # saved page or proposal is held separately with the person's knowledge.
            _RecordSet(KNOWLEDGE_RECEIPTS_TABLE, KNOWLEDGE_RECEIPTS_PERSON_COLUMN, person),
            _RecordSet("after_action_report_review_batches", "person_id", person),
            _RecordSet("objective_work_profile_proposals", "owner_canonical_user_id", person),
            _RecordSet("objective_work_installed_profiles", "owner_canonical_user_id", person),
            # Which of their requests changed which record (storage.change_ledger):
            # ids and positions only, with the same lifetime as their task records.
            _RecordSet(CHANGE_REQUESTS_TABLE, CHANGE_PERSON_COLUMN, person),
            _RecordSet(CHANGE_LEDGER_TABLE, CHANGE_PERSON_COLUMN, person),
        ]
        if objectives:
            sets += [
                _RecordSet("objective_work_checkpoints", "objective_id", objectives),
                _RecordSet("objective_work_sessions", "objective_id", objectives),
                _RecordSet("objective_work_policies", "objective_id", objectives),
                _RecordSet("plans", "objective_id", objectives),
            ]
        sets.append(_RecordSet("objectives", "owner_canonical_user_id", person))
        return sets

    def _delete(self, conn: Any, record_set: _RecordSet) -> int:
        where, params = record_set.where(self.dialect.bind)
        return int(conn.execute(f"DELETE FROM {record_set.table} WHERE {where}", params).rowcount or 0)

    def _take(self, conn: Any, record_set: _RecordSet) -> list[dict[str, Any]]:
        """Delete the set's rows and return exactly the rows deleted (``DELETE ... RETURNING``).

        One statement, so every row that leaves the table is the row that is
        held: under PostgreSQL's READ COMMITTED isolation a SELECT and a later
        DELETE see different rows, and a row another process committed in
        between would be deleted without being held.
        """

        where, params = record_set.where(self.dialect.bind)
        rows = conn.execute(f"DELETE FROM {record_set.table} WHERE {where} RETURNING *", params).fetchall()
        return [dict(row) for row in rows]

    # -- deleting, holding, restoring ------------------------------------------------------

    def delete_person_records(self, person_id: str) -> dict[str, int]:
        """Delete the person's task records and objectives in one transaction; rows deleted by table."""

        deleted: dict[str, int] = {}
        with self._connect() as conn:
            conn.begin_write()
            for record_set in self._deleted_record_sets(conn, person_id):
                if self._table_exists(conn, record_set.table):
                    deleted[record_set.table] = deleted.get(record_set.table, 0) + self._delete(conn, record_set)
            conn.commit()
        return deleted

    def delete_memory_review_jobs(self, person_id: str) -> int:
        """Delete memory-review jobs made for the person (``source_payload_json.canonical_user_id``)."""

        p = self.dialect.bind
        with self._connect() as conn:
            if not self._table_exists(conn, "memory_review_jobs"):
                return 0
            conn.begin_write()
            rows = conn.execute("SELECT review_job_id, source_payload_json FROM memory_review_jobs").fetchall()
            job_ids = [row[0] for row in rows if _payload_person(row[1]) == person_id]
            for job_id in job_ids:
                conn.execute(f"DELETE FROM memory_review_jobs WHERE review_job_id={p}", (job_id,))
            conn.commit()
        return len(job_ids)

    def hold_person_records(self, team_id: str, person_id: str, *, at: datetime) -> int:
        """Move the person's personal memory, own knowledge pages and conversations into the holding table; rows moved."""

        p = self.dialect.bind
        moved = 0
        with self._connect() as conn:
            conn.begin_write()
            position = self._next_position(conn, team_id, person_id)
            for record_set in self._held_record_sets(conn, person_id):
                if not self._table_exists(conn, record_set.table):
                    continue
                taken = self._take(conn, record_set)
                for row in taken:
                    conn.execute(
                        "INSERT INTO person_held_rows (held_row_id, team_id, person_id, position, source_table, "
                        f"row_json, held_at) VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})",
                        (
                            new_id("held_row"),
                            team_id,
                            person_id,
                            position,
                            record_set.table,
                            json.dumps({key: _encode(value) for key, value in row.items()}, default=str),
                            _utc(at),
                        ),
                    )
                    position += 1
                moved += len(taken)
            conn.commit()
        return moved

    def _next_position(self, conn: Any, team_id: str, person_id: str) -> int:
        p = self.dialect.bind
        row = conn.execute(
            f"SELECT MAX(position) FROM person_held_rows WHERE team_id={p} AND person_id={p}", (team_id, person_id)
        ).fetchone()
        return int(row[0]) + 1 if row and row[0] is not None else 0

    def held_row_count(self, team_id: str, person_id: str) -> int:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM person_held_rows WHERE team_id={p} AND person_id={p}", (team_id, person_id)
            ).fetchone()
        return int(row[0]) if row else 0

    def restore_person_records(self, team_id: str, person_id: str) -> int:
        """Move the held rows back into their tables in one transaction; rows restored.

        A row whose key exists again is kept as it is now (logged), and a
        column the table no longer has is dropped (logged): the schema may
        have changed while the rows were held.
        """

        p = self.dialect.bind
        restored = 0
        with self._connect() as conn:
            conn.begin_write()
            # Deleted and returned in one statement (see ``_take``), so a row
            # held meanwhile is never deleted without being restored.
            rows = conn.execute(
                f"DELETE FROM person_held_rows WHERE team_id={p} AND person_id={p} "
                "RETURNING position, source_table, row_json",
                (team_id, person_id),
            ).fetchall()
            columns_by_table: dict[str, set[str]] = {}
            # By position: a PostgreSQL row iterates over its column names.
            for row in sorted(rows, key=lambda row: int(row[0])):
                restored += self._restore_row(conn, str(row[1]), json.loads(row[2]), columns_by_table, person_id)
            conn.commit()
        return restored

    def _restore_row(
        self, conn: Any, table: str, row: dict[str, Any], columns_by_table: dict[str, set[str]], person_id: str
    ) -> int:
        if not _IDENTIFIER.fullmatch(table) or not self._table_exists(conn, table):
            logger.warning(
                "A held row's table no longer exists, so the row was not restored. "
                "event=person_held_row_table_missing person_id=%s table=%s",
                person_id,
                table,
            )
            return 0
        known = columns_by_table.setdefault(table, self._columns(conn, table))
        names = [name for name in row if name in known and _IDENTIFIER.fullmatch(name)]
        dropped = sorted(set(row) - set(names))
        if dropped:
            logger.warning(
                "A held row had columns its table no longer has; they were dropped. "
                "event=person_held_row_columns_dropped person_id=%s table=%s columns=%s",
                person_id,
                table,
                ",".join(dropped),
            )
        p = self.dialect.bind
        cursor = conn.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(p for _ in names)}) ON CONFLICT DO NOTHING",
            tuple(_decode(row[name]) for name in names),
        )
        if not cursor.rowcount:
            logger.warning(
                "A held row was not restored because a row with the same key exists now. "
                "event=person_held_row_conflict person_id=%s table=%s",
                person_id,
                table,
            )
            return 0
        return 1

    def delete_held_rows(self, team_id: str, person_id: str) -> int:
        """Delete the person's held rows for good; rows deleted."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            deleted = conn.execute(
                f"DELETE FROM person_held_rows WHERE team_id={p} AND person_id={p}", (team_id, person_id)
            ).rowcount
            conn.commit()
        return int(deleted or 0)

    def held_artifact_paths(self, team_id: str, person_id: str) -> list[str]:
        """Files of the person's held conversation artifacts, deleted with the held rows."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT row_json FROM person_held_rows WHERE team_id={p} AND person_id={p} AND source_table={p}",
                (team_id, person_id, "conversation_artifacts"),
            ).fetchall()
        return [str(path) for row in rows if (path := json.loads(row[0]).get("storage_path"))]


def _payload_person(payload: Any) -> str | None:
    try:
        data = json.loads(payload or "{}")
    except (TypeError, ValueError):
        return None
    person = data.get("canonical_user_id") if isinstance(data, dict) else None
    return str(person) if person else None


__all__ = ["PersonDepartureRepository", "PersonHold"]
