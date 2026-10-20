"""The change ledger: which of a person's requests changed which record, in one order.

Why: a person's separate requests can run at the same time, and two of them
can change the same record (a personal knowledge page today; memory facts,
schedules, Objectives, heartbeat items and files as their stores join). A
version check alone ("the record changed since you read it") stops stale
writes but not a request that read the newest version and then changes a
record another request changed while it ran. Separate requests should stop
and check instead, so a store refuses such a change with ``change_conflict``
("rule (b)"). Which change is refused follows the order the database saved
changes and request starts in, never timing luck.

Tables, in the manager database next to the stores that use them:

- ``change_ledger_positions``: one counter per team. Every recorded change
  takes the next position, so all covered stores share one order.
- ``change_ledger``: one row per recorded change: the record (kind and key),
  whether it created, changed or only added to it (a note), who wrote it
  (``ChangeWriterKind``: a person's request, scheduled work, a person, the
  system), the request and person, and the record's version after it.
- ``change_requests``: one row per person's request (its signed context id,
  ``AgentPrincipal.work_id``): its start position on the counter and whether
  it is open, stopped or ended.

A covered store calls ``check_change`` before it writes and ``record_change``
after, on its own open write connection, so the check and the change are
saved together or not at all. Neither commits. The store already holds its
write lock (``BEGIN IMMEDIATE`` in SQLite, its advisory lock in PostgreSQL),
so no other write to the same record can come between the check and the
change. The counter row is updated last: in PostgreSQL its row lock orders
all covered stores' changes, and ``lock_timeout`` bounds the wait for it.

Rule (b), for a writer that is a person's request:

- A stopped or ended request may not write (``request_closed``): a worker
  still running after its request ended is refused and logged.
- Creating a record and adding a note are recorded and never refused by rule
  (b): nothing is overwritten, as two requests that each add a memory fact
  do not stop each other.
- Changing an existing record is refused with ``change_conflict`` when a
  different request created or changed it after this request started.
  Scheduled work, people's own edits and system upkeep are recorded but
  neither stopped by rule (b) nor counted against a request; the store's
  version check still applies to them.

Request admission and closure share the task snapshot's write transaction.
A missing request row therefore cannot establish a safe start position and
is refused. PostgreSQL writers lock the request row until their change
commits, so closure waits for an already checked write and later writes see
the closed state.

The operator's switch (``agent.request_conflict_checks``, default on) turns
rule (b) off: a writer with ``checked`` False is refused neither for another
request's change nor for having ended, and its changes are recorded as
unchecked. Unchecked entries never count against a request, so when the
switch is turned on again rule (b) counts only later changes. The stores'
version checks apply either way.

Ledger rows never leave the instance: the team transfer does not carry them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from access_control.identity import ContextVerifier, ISSUER_GATEWAY, IdentityRejected
from custom_messages.contracts.runtime_snapshot_v3 import TaskSnapshotRecord
from custom_messages.enums import ChangeRecordKind, ChangeRefusal, ProgressState
from storage.config import StorageSettings
from storage.postgres_connection import (
    CHANGE_LEDGER_SCHEMA_LOCK_KEY,
    connect_postgres,
    ensure_storage_parent_dir,
)
from storage.read_only import repositories_are_read_only
from storage.sql_dialect import POSTGRES, SQLITE, SqlDialect
from storage.sqlite_connection import connect_sqlite
from storage.backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE

logger = logging.getLogger(__name__)

# Table and column names other stores use (person departure deletes a removed
# person's rows with their task records).
CHANGE_LEDGER_TABLE = "change_ledger"
CHANGE_REQUESTS_TABLE = "change_requests"
CHANGE_PERSON_COLUMN = "person_id"
# How long a PostgreSQL write waits for the counter row before it fails.
LEDGER_LOCK_TIMEOUT_MS = 5000
# Ledger entries and closed request rows older than this are pruned when a
# request closes; a request open longer loses rule (b) for older changes,
# while the stores' version checks still apply.
LEDGER_RETENTION = timedelta(days=7)


class ChangeWriterKind(StrEnum):
    """Who made a change, from verified identity only."""

    REQUEST = "request"  # an agent working on a person's request (gateway-signed context)
    SCHEDULED = "scheduled"  # scheduled work for a person (scheduler-signed context or schedule run)
    PERSON = "person"  # a person in the gateway's own controls
    SYSTEM = "system"  # work for no person: system upkeep and trusted writers


class ChangeType(StrEnum):
    CREATE = "create"  # the record did not exist
    CHANGE = "change"  # an existing record changed (rule (b) applies)
    APPEND = "append"  # something was added beside it, such as a note; nothing overwritten


class RequestState(StrEnum):
    OPEN = "open"
    STOPPED = "stopped"  # cancelled, by a person or a stop
    ENDED = "ended"  # completed, failed or abandoned


@dataclass(frozen=True)
class ChangeWriter:
    """Who writes: ``request_id`` only for a person's request, ``person_id`` for whom the work is."""

    kind: ChangeWriterKind
    request_id: str = ""
    person_id: str = ""
    # False when the operator turned checks between requests off.
    checked: bool = True

    def __post_init__(self) -> None:
        if (self.kind is ChangeWriterKind.REQUEST) != bool(self.request_id):
            raise ValueError("A request id is given exactly for a person's request.")


@dataclass(frozen=True)
class RecordRef:
    """A record within a team: its kind and a key that stays the same while it exists."""

    kind: ChangeRecordKind
    key: str


@dataclass
class ChangeRefused(Exception):
    """A store refused a change; ``reason`` is one of the shared refusal names."""

    reason: ChangeRefusal
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


def ledger_table_statements(dialect: SqlDialect) -> list[str]:
    """``CREATE ... IF NOT EXISTS`` for the ledger tables; run with ``CHANGE_LEDGER_SCHEMA_LOCK_KEY`` held."""

    integer = "BIGINT" if dialect is POSTGRES else "INTEGER"
    return [
        f"""
        CREATE TABLE IF NOT EXISTS change_ledger_positions (
            team_id TEXT PRIMARY KEY,
            position {integer} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS change_ledger (
            team_id TEXT NOT NULL,
            position {integer} NOT NULL,
            record_kind TEXT NOT NULL,
            record_key TEXT NOT NULL,
            change_type TEXT NOT NULL,
            writer_kind TEXT NOT NULL,
            request_id TEXT NOT NULL DEFAULT '',
            person_id TEXT NOT NULL DEFAULT '',
            version TEXT NOT NULL DEFAULT '',
            checked INTEGER NOT NULL DEFAULT 1,
            changed_at_utc TEXT NOT NULL,
            PRIMARY KEY (team_id, position)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_change_ledger_record
        ON change_ledger(team_id, record_kind, record_key, position)
        """,
        f"""
        CREATE TABLE IF NOT EXISTS change_requests (
            team_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            person_id TEXT NOT NULL DEFAULT '',
            start_position {integer} NOT NULL,
            state TEXT NOT NULL,
            opened_at_utc TEXT NOT NULL,
            closed_at_utc TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (team_id, request_id)
        )
        """,
    ]


def ensure_ledger_tables(conn: Any, dialect: SqlDialect) -> None:
    """Create the ledger tables on an open connection (the caller commits)."""

    if dialect is POSTGRES:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (CHANGE_LEDGER_SCHEMA_LOCK_KEY,))
    for statement in ledger_table_statements(dialect):
        conn.execute(statement)


def current_position(conn: Any, dialect: SqlDialect, team_id: str) -> int:
    row = conn.execute(
        f"SELECT position FROM change_ledger_positions WHERE team_id={dialect.bind}", (team_id,)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def open_request(conn: Any, dialect: SqlDialect, *, team_id: str, request_id: str, person_id: str, now: datetime) -> int:
    """Record that a person's request started now; returns its start position (kept if already open)."""

    p = dialect.bind
    conn.execute(
        f"""
        INSERT INTO change_requests (team_id, request_id, person_id, start_position, state, opened_at_utc)
        VALUES ({p}, {p}, {p}, {p}, {p}, {p})
        ON CONFLICT (team_id, request_id) DO NOTHING
        """,
        (team_id, request_id, person_id, current_position(conn, dialect, team_id), RequestState.OPEN.value, now.isoformat()),
    )
    row = conn.execute(
        f"SELECT start_position FROM change_requests WHERE team_id={p} AND request_id={p}", (team_id, request_id)
    ).fetchone()
    return int(row[0])


def close_request(
    conn: Any, dialect: SqlDialect, *, team_id: str, request_id: str, person_id: str, state: RequestState, now: datetime
) -> None:
    """Mark a request stopped or ended; later writes for it are refused (a missing row is added closed)."""

    if state is RequestState.OPEN:
        raise ValueError("A request is closed as stopped or ended.")
    _set_lock_timeout(conn, dialect)
    p = dialect.bind
    conn.execute(
        f"""
        INSERT INTO change_requests (
            team_id, request_id, person_id, start_position, state, opened_at_utc, closed_at_utc
        ) VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})
        ON CONFLICT (team_id, request_id) DO UPDATE SET state=excluded.state, closed_at_utc=excluded.closed_at_utc
        WHERE change_requests.state={p}
        """,
        (
            team_id,
            request_id,
            person_id,
            current_position(conn, dialect, team_id),
            state.value,
            now.isoformat(),
            now.isoformat(),
            RequestState.OPEN.value,
        ),
    )


def record_task_request_state(
    conn: Any,
    dialect: SqlDialect,
    *,
    team_id: str | None,
    task: TaskSnapshotRecord,
    now: datetime,
) -> None:
    """Save a root request's lifecycle with its task, never in a later callback.

    The stored owner was verified at admission. Recheck the original signature
    and team before attributing the request id, including when the context has
    since expired. This reads historical identity only and grants no authority.
    A failed lifecycle write aborts the task save, so recovery cannot expose a
    queued or terminal task whose request state was lost.
    """

    if not team_id or task.task.parent_task_id or not task.owner_person_id:
        return
    gateway = task.meta.gateway_context
    signed = gateway.task_context if gateway is not None else None
    if gateway is not None and signed is not None and gateway.schedule_run is not None:
        raise IdentityRejected("A task cannot carry both a signed context and a scheduled run identity.")
    if signed is None:
        return
    try:
        context = ContextVerifier.from_env().verify(signed, team_id=team_id, check_expiry=False)
    except (IdentityRejected, ValueError):
        logger.warning(
            "Ignored unverified request identity while saving a task. "
            "event=change_ledger_task_identity_rejected task_id=%s team_id=%s",
            task.task_id, team_id,
        )
        raise
    if context.issuer != ISSUER_GATEWAY:
        return
    if context.person_id != task.owner_person_id:
        logger.warning(
            "Ignored request identity that does not match the admitted task owner. "
            "event=change_ledger_task_owner_mismatch task_id=%s team_id=%s",
            task.task_id, team_id,
        )
        raise IdentityRejected("The request identity does not match the task's admitted owner.")
    terminal = task.state in {
        ProgressState.COMPLETED, ProgressState.FAILED, ProgressState.CANCELLED, ProgressState.ABANDONED,
    }
    if terminal:
        state = RequestState.STOPPED if task.state is ProgressState.CANCELLED else RequestState.ENDED
        close_request(
            conn, dialect, team_id=team_id, request_id=context.context_id,
            person_id=task.owner_person_id, now=now, state=state,
        )
        prune(conn, dialect, team_id=team_id, before=now - LEDGER_RETENTION)
    else:
        open_request(conn, dialect, team_id=team_id, request_id=context.context_id, person_id=task.owner_person_id, now=now)


def require_same_request_identity(previous: TaskSnapshotRecord, current: TaskSnapshotRecord) -> None:
    """An admitted root cannot lose or replace the identity its ledger records.

    The previous snapshot is the trusted admission record. Removing its signed
    context, changing its owner or turning it into a child must abort the save,
    rather than persisting a terminal task without closing its real request.
    """

    gateway = previous.meta.gateway_context
    original = gateway.task_context if gateway is not None else None
    if previous.task.parent_task_id or not previous.owner_person_id or original is None:
        return
    if original.context.issuer != ISSUER_GATEWAY:
        return
    current_gateway = current.meta.gateway_context
    if (
        current.owner_person_id != previous.owner_person_id or current.task.parent_task_id
        or current_gateway is None or current_gateway.task_context != original
        or current_gateway.schedule_run is not None
    ):
        raise IdentityRejected("An admitted request task cannot change or remove its original signed identity.")


def check_change(
    conn: Any,
    dialect: SqlDialect,
    *,
    team_id: str,
    writer: ChangeWriter,
    record: RecordRef,
    change_type: ChangeType,
    now: datetime,
) -> None:
    """Raise ``ChangeRefused`` when a person's request may not make this change (module docstring)."""

    if writer.kind is not ChangeWriterKind.REQUEST or not writer.checked:
        return
    start, state = _request_row(conn, dialect, team_id=team_id, writer=writer)
    if state is not RequestState.OPEN:
        logger.info(
            "Refused a change from a request that is no longer running. event=change_ledger_request_closed "
            "request_id=%s state=%s record_kind=%s record_key=%s",
            writer.request_id,
            state.value,
            record.kind.value,
            record.key,
        )
        raise ChangeRefused(
            ChangeRefusal.REQUEST_CLOSED,
            f"This request was {state.value}, so it no longer changes anything. Nothing was changed.",
            {"request_state": state.value},
        )
    if change_type is not ChangeType.CHANGE:
        return
    p = dialect.bind
    competing = conn.execute(
        f"""
        SELECT position, change_type FROM change_ledger
        WHERE team_id={p} AND record_kind={p} AND record_key={p} AND position > {p}
          AND writer_kind={p} AND request_id <> {p} AND change_type <> {p} AND checked = 1
        ORDER BY position DESC LIMIT 1
        """,
        (
            team_id,
            record.kind.value,
            record.key,
            start,
            ChangeWriterKind.REQUEST.value,
            writer.request_id,
            ChangeType.APPEND.value,
        ),
    ).fetchone()
    if competing is None:
        return
    logger.info(
        "Refused a change: another request changed the record while this one ran. event=change_ledger_conflict "
        "request_id=%s record_kind=%s record_key=%s competing_position=%s request_start=%s",
        writer.request_id,
        record.kind.value,
        record.key,
        competing[0],
        start,
    )
    raise ChangeRefused(
        ChangeRefusal.CHANGE_CONFLICT,
        "Another of this person's requests changed this record while this request was running, so this change "
        "was not made. Read the record again and tell the person what changed; they can repeat the change in "
        "a new message.",
        {"record_kind": record.kind.value},
    )


def record_change(
    conn: Any,
    dialect: SqlDialect,
    *,
    team_id: str,
    writer: ChangeWriter,
    record: RecordRef,
    change_type: ChangeType,
    version: str,
    now: datetime,
) -> int:
    """Record one change after the store wrote it, in the same transaction; returns its position."""

    position = _next_position(conn, dialect, team_id)
    p = dialect.bind
    conn.execute(
        f"""
        INSERT INTO change_ledger (
            team_id, position, record_kind, record_key, change_type, writer_kind,
            request_id, person_id, version, checked, changed_at_utc
        ) VALUES ({', '.join([p] * 11)})
        """,
        (
            team_id,
            position,
            record.kind.value,
            record.key,
            change_type.value,
            writer.kind.value,
            writer.request_id,
            writer.person_id,
            version,
            1 if writer.checked else 0,
            now.isoformat(),
        ),
    )
    logger.debug(
        "Recorded a change. event=change_ledger_recorded position=%s record_kind=%s record_key=%s change_type=%s "
        "writer_kind=%s request_id=%s",
        position,
        record.kind.value,
        record.key,
        change_type.value,
        writer.kind.value,
        writer.request_id,
    )
    return position


def prune(conn: Any, dialect: SqlDialect, *, team_id: str, before: datetime) -> None:
    """Delete ledger entries and closed request rows older than ``before`` (``LEDGER_RETENTION``)."""

    p = dialect.bind
    if dialect is POSTGRES:
        # A terminal save already holds its own request row. Never wait for
        # another save's row while pruning: two closures could otherwise each
        # delete the other's old row and deadlock. Busy rows remain for a later
        # cleanup; retention is independent of lifecycle correctness.
        conn.execute(
            f"DELETE FROM change_ledger WHERE (team_id, position) IN "
            f"(SELECT team_id, position FROM change_ledger WHERE team_id={p} AND changed_at_utc < {p} "
            "FOR UPDATE SKIP LOCKED)", (team_id, before.isoformat()),
        )
        conn.execute(
            f"DELETE FROM change_requests WHERE (team_id, request_id) IN "
            f"(SELECT team_id, request_id FROM change_requests WHERE team_id={p} AND state <> {p} "
            f"AND closed_at_utc < {p} FOR UPDATE SKIP LOCKED)",
            (team_id, RequestState.OPEN.value, before.isoformat()),
        )
        return
    conn.execute(f"DELETE FROM change_ledger WHERE team_id={p} AND changed_at_utc < {p}", (team_id, before.isoformat()))
    conn.execute(
        f"DELETE FROM change_requests WHERE team_id={p} AND state <> {p} AND closed_at_utc < {p}",
        (team_id, RequestState.OPEN.value, before.isoformat()),
    )


class ChangeRequestStore:
    """Opens and closes request rows in their own short transaction (the team lead's admission and task end)."""

    def __init__(self, settings: StorageSettings) -> None:
        if settings.backend == STORAGE_BACKEND_SQLITE and settings.sqlite_path:
            self._dialect, self._target = SQLITE, settings.sqlite_path
        elif settings.backend == STORAGE_BACKEND_POSTGRES and settings.postgres_dsn:
            self._dialect, self._target = POSTGRES, settings.postgres_dsn
        else:
            raise NotImplementedError(f"The change ledger has no storage for backend {settings.backend!r}.")
        if repositories_are_read_only():
            return
        if self._dialect is SQLITE:
            ensure_storage_parent_dir(self._target)
        with self._connect() as conn:
            conn.begin_write()
            ensure_ledger_tables(conn, self._dialect)
            conn.commit()

    def _connect(self) -> Any:
        return connect_sqlite(self._target) if self._dialect is SQLITE else connect_postgres(self._target)

    def open(self, *, team_id: str, request_id: str, person_id: str, now: datetime) -> int:
        with self._connect() as conn:
            conn.begin_write()
            start = open_request(conn, self._dialect, team_id=team_id, request_id=request_id, person_id=person_id, now=now)
            conn.commit()
        return start

    def close(self, *, team_id: str, request_id: str, person_id: str, state: RequestState, now: datetime) -> None:
        with self._connect() as conn:
            conn.begin_write()
            close_request(
                conn, self._dialect, team_id=team_id, request_id=request_id, person_id=person_id, state=state, now=now
            )
            prune(conn, self._dialect, team_id=team_id, before=now - LEDGER_RETENTION)
            conn.commit()


def _next_position(conn: Any, dialect: SqlDialect, team_id: str) -> int:
    _set_lock_timeout(conn, dialect)
    p = dialect.bind
    row = conn.execute(
        f"""
        INSERT INTO change_ledger_positions (team_id, position) VALUES ({p}, 1)
        ON CONFLICT (team_id) DO UPDATE SET position = change_ledger_positions.position + 1
        RETURNING position
        """,
        (team_id,),
    ).fetchone()
    return int(row[0])


def _set_lock_timeout(conn: Any, dialect: SqlDialect) -> None:
    if dialect is POSTGRES:
        # SET cannot take a bind parameter; the value is a constant.
        conn.execute(f"SET LOCAL lock_timeout = '{int(LEDGER_LOCK_TIMEOUT_MS)}ms'")


def _request_row(
    conn: Any, dialect: SqlDialect, *, team_id: str, writer: ChangeWriter
) -> tuple[int, RequestState]:
    # Lock before checking, held until the store's write commits. Request
    # closure updates this same row and cannot commit in the middle of a write.
    # SQLite's BEGIN IMMEDIATE already provides the corresponding exclusion.
    lock = " FOR UPDATE" if dialect is POSTGRES else ""
    _set_lock_timeout(conn, dialect)
    p = dialect.bind
    row = conn.execute(
        f"SELECT start_position, state FROM change_requests WHERE team_id={p} AND request_id={p}{lock}",
        (team_id, writer.request_id),
    ).fetchone()
    if row is not None:
        return int(row[0]), RequestState(str(row[1]))
    logger.warning(
        "Refused a change from a request with no durable admission. "
        "event=change_ledger_request_not_admitted request_id=%s", writer.request_id,
    )
    raise ChangeRefused(
        ChangeRefusal.REQUEST_CLOSED,
        "This request has no recorded admission, so its change was not made. Repeat the request in a new message.",
        {"request_state": "not_admitted"},
    )


__all__ = [
    "CHANGE_LEDGER_TABLE",
    "CHANGE_PERSON_COLUMN",
    "CHANGE_REQUESTS_TABLE",
    "ChangeRefused",
    "ChangeRequestStore",
    "ChangeType",
    "ChangeWriter",
    "ChangeWriterKind",
    "LEDGER_LOCK_TIMEOUT_MS",
    "LEDGER_RETENTION",
    "RecordRef",
    "RequestState",
    "check_change",
    "close_request",
    "current_position",
    "ensure_ledger_tables",
    "ledger_table_statements",
    "open_request",
    "prune",
    "record_change",
    "record_task_request_state",
    "require_same_request_identity",
]
