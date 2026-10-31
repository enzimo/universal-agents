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
  whether it created, changed or only added to it (a note), the names of the
  fields it set (never their values), who wrote it (``ChangeWriterKind``: a
  person's request, scheduled work, a person, the system), the request and
  person, and the record's version after it.
- ``change_requests``: one row per person's request (its signed context id,
  ``AgentPrincipal.work_id``): its start position on the counter and whether
  it is open, stopped or ended.
- ``change_reviews``: for each request and record, the newest position the
  request was shown in a brief. Changes up to it count as seen.
- ``change_cases``: one per distinct conflict a request met on a record: its
  case id, shown position and state. Escalation counts them, never retries.

A covered store calls ``check_change`` before it writes and ``record_change``
after, on its own open write connection, so the check and the change are
saved together or not at all. Neither commits. The store already holds its
write lock (``BEGIN IMMEDIATE`` in SQLite, its advisory lock in PostgreSQL),
so no other write to the same record can come between the check and the
change. The counter row is updated last: in PostgreSQL its row lock orders
all covered stores' changes, and ``lock_timeout`` bounds the wait for it.

Rule (b), for a writer that is a person's request (the coordination plan's
7.10, revision 4, accepted 2026-10-30):

- A stopped or ended request may not write (``request_closed``): a worker
  still running after its request ended is refused and logged.
- Creating a record and adding a note are recorded and never refused by rule
  (b): nothing is overwritten, as two requests that each add a memory fact
  do not stop each other.
- Changes that count against a request are those made after it started by
  another request, by scheduled work or by a person in the gateway. System
  upkeep never counts, and neither do the request's own tasks (the store's
  version check still applies to them).
- A change the request has not yet seen is refused with ``change_conflict``
  and a brief written here: who changed which fields, the record's current
  values and the request's planned ones. The store saves the review with
  ``save_review`` after its refused transaction rolls back, so the same
  change is not refused twice, while a later one is.
- Once seen, the request may save fields those changes did not set. A change
  that sets one of them, or removes the record, is refused with
  ``needs_person_decision``: only the person decides which to keep.
- Escalation: after ``ESCALATION_CASES`` conflicts on one record within
  ``ESCALATION_WINDOW``, every request's change to it is refused with
  ``needs_person_decision`` until the window has passed.
- Scheduled work is not stopped by rule (b) yet (coordination plan phase R3);
  its changes still count against requests.

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

Ledger, review and case rows never leave the instance: the team transfer
does not carry them, and they are pruned after ``LEDGER_RETENTION``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any

from access_control.identity import ContextVerifier, ISSUER_GATEWAY, IdentityRejected
from custom_messages.contracts.runtime_snapshot_v3 import TaskSnapshotRecord
from custom_messages.enums import ChangeRecordKind, ChangeRefusal, ProgressState
from knowledge_base.sensitive_content import SensitiveContentRules
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
CHANGE_REVIEWS_TABLE = "change_reviews"
CHANGE_CASES_TABLE = "change_cases"
CHANGE_PERSON_COLUMN = "person_id"
# How long a PostgreSQL write waits for the counter row before it fails.
LEDGER_LOCK_TIMEOUT_MS = 5000
# Ledger entries, reviews, cases and closed request rows older than this are
# pruned when a request closes; a request open longer loses rule (b) for
# older changes, while the stores' version checks still apply.
LEDGER_RETENTION = timedelta(days=7)
# The field name for "every field": creating or removing a record.
ALL_FIELDS = "*"
# Escalation: this many conflicts on one record within the window make every
# request's change to it wait for the person until the window has passed.
ESCALATION_CASES = 3
ESCALATION_WINDOW = timedelta(hours=1)
# A brief shows at most this many characters of one value.
BRIEF_VALUE_LIMIT = 1000
# A brief lists at most this many of the changes it explains, newest last.
BRIEF_CHANGES_LIMIT = 5


class ChangeWriterKind(StrEnum):
    """Who made a change, from verified identity only."""

    REQUEST = "request"  # an agent working on a person's request (gateway-signed context)
    SCHEDULED = "scheduled"  # scheduled work for a person (scheduler-signed context or schedule run)
    PERSON = "person"  # a person in the gateway's own controls
    SYSTEM = "system"  # work for no person: system upkeep and trusted writers


# Writers whose changes count against a request; system upkeep never does.
COUNTED_WRITERS = (ChangeWriterKind.REQUEST, ChangeWriterKind.SCHEDULED, ChangeWriterKind.PERSON)

# How a brief names who made a change: (writer, same person as the request).
_WRITER_LABELS = {
    (ChangeWriterKind.REQUEST, True): "another of your requests",
    (ChangeWriterKind.REQUEST, False): "another team member's request",
    (ChangeWriterKind.SCHEDULED, True): "one of your scheduled jobs",
    (ChangeWriterKind.SCHEDULED, False): "another team member's scheduled job",
    (ChangeWriterKind.PERSON, True): "you (in the app)",
    (ChangeWriterKind.PERSON, False): "another team member (in the app)",
}
_HIDDEN_VALUE = "(hidden: it looks like a credential or an identity number)"
_SHORTENED_VALUE = " … (shortened; read the record for the rest)"


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


class CaseState(StrEnum):
    OPEN = "open"  # the request was shown the brief


@dataclass(frozen=True)
class RecordRef:
    """A record within a team: its kind and a key that stays the same while it exists."""

    kind: ChangeRecordKind
    key: str


@dataclass(frozen=True)
class PlannedChange:
    """One change a store is about to make, described for the ledger and for a brief.

    ``fields`` names what it sets: ``ALL_FIELDS`` for creating or removing the
    record, nothing for a note. ``current`` and ``planned`` give field values
    as text, from saved state the writer's person may read; they only explain
    a refusal, and the ledger stores field names, never values. ``label`` is
    how the record is named to the agent ("personal/trip").
    """

    record: RecordRef
    change_type: ChangeType
    label: str = ""
    fields: frozenset[str] = frozenset()
    current: Mapping[str, str] = field(default_factory=dict)
    planned: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PendingReview:
    """What a refused request was shown, saved by the store after its transaction rolls back."""

    team_id: str
    request_id: str
    person_id: str
    record: RecordRef
    shown_position: int
    case_id: str
    at: datetime


@dataclass
class ChangeRefused(Exception):
    """A store refused a change; ``reason`` is one of the shared refusal names.

    ``review`` is set when the request was shown a brief; the store passes it
    to ``save_review`` in a transaction of its own.
    """

    reason: ChangeRefusal
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    review: PendingReview | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class _CountedChange:
    """A change that counts against a request: one ledger row."""

    position: int
    writer_kind: ChangeWriterKind
    person_id: str
    fields: frozenset[str]
    changed_at: str


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
            changed_fields TEXT NOT NULL,
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
        f"""
        CREATE TABLE IF NOT EXISTS change_reviews (
            team_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            record_kind TEXT NOT NULL,
            record_key TEXT NOT NULL,
            person_id TEXT NOT NULL DEFAULT '',
            reviewed_position {integer} NOT NULL,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (team_id, request_id, record_kind, record_key)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS change_cases (
            team_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            record_kind TEXT NOT NULL,
            record_key TEXT NOT NULL,
            case_id TEXT NOT NULL,
            shown_position {integer} NOT NULL,
            person_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            opened_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (team_id, case_id),
            UNIQUE (team_id, request_id, record_kind, record_key, shown_position)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_change_cases_record
        ON change_cases(team_id, record_kind, record_key, opened_at_utc)
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
    change: PlannedChange,
    now: datetime,
) -> None:
    """Raise ``ChangeRefused`` when a person's request may not make this change (module docstring)."""

    if writer.kind is not ChangeWriterKind.REQUEST or not writer.checked:
        return
    record = change.record
    start, state = _request_row(conn, dialect, team_id=team_id, writer=writer)
    if state is not RequestState.OPEN:
        raise _closed(writer, record, state)
    if change.change_type is not ChangeType.CHANGE:
        return
    until = _escalated_until(conn, dialect, team_id=team_id, record=record, now=now)
    if until is not None:
        raise _escalated(writer, change, until)
    others = _counted_changes(conn, dialect, team_id=team_id, writer=writer, record=record, after=start)
    if others:
        seen = max(start, _reviewed_position(conn, dialect, team_id=team_id, writer=writer, record=record))
        unseen = [counted for counted in others if counted.position > seen]
        if unseen:
            case_id = _new_case_id(team_id, writer, record, unseen[-1].position)
            raise _not_yet_seen(team_id, writer, change, others=others, unseen=unseen, case_id=case_id, now=now)
    overlap = _overlapping_fields(change.fields, others)
    if overlap:
        case_id = _open_case_id(conn, dialect, team_id=team_id, writer=writer, record=record)
        raise _needs_person(writer, change, others=others, overlap=overlap, case_id=case_id)


def record_change(
    conn: Any,
    dialect: SqlDialect,
    *,
    team_id: str,
    writer: ChangeWriter,
    change: PlannedChange,
    version: str,
    now: datetime,
) -> int:
    """Record one change after the store wrote it, in the same transaction; returns its position."""

    position = _next_position(conn, dialect, team_id)
    record = change.record
    p = dialect.bind
    conn.execute(
        f"""
        INSERT INTO change_ledger (
            team_id, position, record_kind, record_key, change_type, writer_kind,
            request_id, person_id, version, checked, changed_at_utc, changed_fields
        ) VALUES ({', '.join([p] * 12)})
        """,
        (
            team_id,
            position,
            record.kind.value,
            record.key,
            change.change_type.value,
            writer.kind.value,
            writer.request_id,
            writer.person_id,
            version,
            1 if writer.checked else 0,
            now.isoformat(),
            json.dumps(_stored_fields(change)),
        ),
    )
    logger.debug(
        "Recorded a change. event=change_ledger_recorded position=%s record_kind=%s record_key=%s change_type=%s "
        "writer_kind=%s request_id=%s",
        position,
        record.kind.value,
        record.key,
        change.change_type.value,
        writer.kind.value,
        writer.request_id,
    )
    return position


def save_review(conn: Any, dialect: SqlDialect, review: PendingReview) -> None:
    """Save what a refused request was shown, in the store's own new transaction (the caller commits).

    The refused write rolled back, so this cannot share its transaction. A
    review only moves forward: a slower save never hides a newer change.
    """

    p = dialect.bind
    at = review.at.isoformat()
    record = review.record
    conn.execute(
        f"""
        INSERT INTO change_reviews (
            team_id, request_id, record_kind, record_key, person_id, reviewed_position, updated_at_utc
        ) VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})
        ON CONFLICT (team_id, request_id, record_kind, record_key) DO UPDATE SET
            reviewed_position = CASE
                WHEN excluded.reviewed_position > change_reviews.reviewed_position
                THEN excluded.reviewed_position ELSE change_reviews.reviewed_position END,
            updated_at_utc = excluded.updated_at_utc
        """,
        (review.team_id, review.request_id, record.kind.value, record.key, review.person_id, review.shown_position, at),
    )
    conn.execute(
        f"""
        INSERT INTO change_cases (
            team_id, request_id, record_kind, record_key, case_id, shown_position,
            person_id, state, opened_at_utc, updated_at_utc
        ) VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
        ON CONFLICT (team_id, case_id) DO NOTHING
        """,
        (
            review.team_id, review.request_id, record.kind.value, record.key, review.case_id, review.shown_position, review.person_id,
            CaseState.OPEN.value, at, at,
        ),
    )
    logger.info(
        "Saved what a request was shown about another writer's change. event=change_ledger_review_saved "
        "request_id=%s record_kind=%s record_key=%s case_id=%s shown_position=%s",
        review.request_id, record.kind.value, record.key, review.case_id, review.shown_position,
    )


def prune(conn: Any, dialect: SqlDialect, *, team_id: str, before: datetime) -> None:
    """Delete ledger entries, reviews, cases and closed request rows older than ``before`` (``LEDGER_RETENTION``)."""

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
        for table in (CHANGE_REVIEWS_TABLE, CHANGE_CASES_TABLE):
            # A record can have several cases: pruning one old case must not
            # remove its newer ones. Reviews still have one row per request/record.
            keys = "team_id, case_id" if table == CHANGE_CASES_TABLE else "team_id, request_id, record_kind, record_key"
            conn.execute(
                f"DELETE FROM {table} WHERE ({keys}) IN "
                f"(SELECT {keys} FROM {table} "
                f"WHERE team_id={p} AND updated_at_utc < {p} FOR UPDATE SKIP LOCKED)",
                (team_id, before.isoformat()),
            )
        return
    conn.execute(f"DELETE FROM change_ledger WHERE team_id={p} AND changed_at_utc < {p}", (team_id, before.isoformat()))
    conn.execute(
        f"DELETE FROM change_requests WHERE team_id={p} AND state <> {p} AND closed_at_utc < {p}",
        (team_id, RequestState.OPEN.value, before.isoformat()),
    )
    for table in (CHANGE_REVIEWS_TABLE, CHANGE_CASES_TABLE):
        conn.execute(f"DELETE FROM {table} WHERE team_id={p} AND updated_at_utc < {p}", (team_id, before.isoformat()))


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


# -- reading what counts against a request --------------------------------------------


def _counted_changes(
    conn: Any, dialect: SqlDialect, *, team_id: str, writer: ChangeWriter, record: RecordRef, after: int
) -> list[_CountedChange]:
    """Changes to the record after ``after`` that count against this request, oldest first."""

    p = dialect.bind
    writers = ", ".join([p] * len(COUNTED_WRITERS))
    rows = conn.execute(
        f"""
        SELECT position, writer_kind, person_id, changed_fields, changed_at_utc FROM change_ledger
        WHERE team_id={p} AND record_kind={p} AND record_key={p} AND position > {p}
          AND writer_kind IN ({writers}) AND request_id <> {p} AND change_type <> {p} AND checked = 1
        ORDER BY position
        """,
        (
            team_id,
            record.kind.value,
            record.key,
            after,
            *(kind.value for kind in COUNTED_WRITERS),
            writer.request_id,
            ChangeType.APPEND.value,
        ),
    ).fetchall()
    return [
        _CountedChange(
            position=int(row[0]),
            writer_kind=ChangeWriterKind(str(row[1])),
            person_id=str(row[2]),
            fields=frozenset(json.loads(str(row[3]))),
            changed_at=str(row[4]),
        )
        for row in rows
    ]


def _reviewed_position(conn: Any, dialect: SqlDialect, *, team_id: str, writer: ChangeWriter, record: RecordRef) -> int:
    p = dialect.bind
    row = conn.execute(
        f"""
        SELECT reviewed_position FROM change_reviews
        WHERE team_id={p} AND request_id={p} AND record_kind={p} AND record_key={p}
        """,
        (team_id, writer.request_id, record.kind.value, record.key),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _open_case_id(conn: Any, dialect: SqlDialect, *, team_id: str, writer: ChangeWriter, record: RecordRef) -> str:
    p = dialect.bind
    row = conn.execute(
        f"""
        SELECT case_id FROM change_cases
        WHERE team_id={p} AND request_id={p} AND record_kind={p} AND record_key={p} AND state={p}
        ORDER BY shown_position DESC LIMIT 1
        """,
        (team_id, writer.request_id, record.kind.value, record.key, CaseState.OPEN.value),
    ).fetchone()
    return str(row[0]) if row is not None else ""


def _new_case_id(team_id: str, writer: ChangeWriter, record: RecordRef, shown_position: int) -> str:
    """A new ledger position is a new conflict; concurrent retries share its case id."""

    identity = json.dumps([team_id, writer.request_id, record.kind.value, record.key, shown_position])
    return f"case_{sha256(identity.encode()).hexdigest()[:16]}"


def _escalated_until(
    conn: Any, dialect: SqlDialect, *, team_id: str, record: RecordRef, now: datetime
) -> datetime | None:
    """When escalation of this record ends, or None when it is not escalated."""

    p = dialect.bind
    rows = conn.execute(
        f"""
        SELECT opened_at_utc FROM change_cases
        WHERE team_id={p} AND record_kind={p} AND record_key={p} AND opened_at_utc > {p}
        ORDER BY opened_at_utc DESC LIMIT {int(ESCALATION_CASES)}
        """,
        (team_id, record.kind.value, record.key, (now - ESCALATION_WINDOW).isoformat()),
    ).fetchall()
    if len(rows) < ESCALATION_CASES:
        return None
    # It lasts until the oldest of the latest conflicts leaves the window.
    return datetime.fromisoformat(str(rows[-1][0])) + ESCALATION_WINDOW


def _overlapping_fields(planned: frozenset[str], others: list[_CountedChange]) -> frozenset[str]:
    """The fields both this change and the counted changes set (``ALL_FIELDS`` overlaps everything)."""

    other_fields: frozenset[str] = frozenset().union(*(counted.fields for counted in others))
    if not planned or not other_fields:
        return frozenset()
    if ALL_FIELDS in planned:
        return other_fields
    if ALL_FIELDS in other_fields:
        return planned
    return planned & other_fields


def _stored_fields(change: PlannedChange) -> list[str]:
    if change.change_type is ChangeType.CREATE:
        return [ALL_FIELDS]
    if change.change_type is ChangeType.APPEND:
        return []
    return sorted(change.fields)


# -- refusals and their briefs ----------------------------------------------------------

_SENSITIVE_CONTENT = SensitiveContentRules()


def _shown_value(text: str) -> str:
    """A value as a brief shows it: hidden when it looks like a credential, cut when long."""

    if _SENSITIVE_CONTENT.scan(text).is_blocked:
        return _HIDDEN_VALUE
    if len(text) <= BRIEF_VALUE_LIMIT:
        return text
    return text[:BRIEF_VALUE_LIMIT] + _SHORTENED_VALUE


def _writer_label(writer: ChangeWriter, counted: _CountedChange) -> str:
    same_person = bool(writer.person_id) and counted.person_id == writer.person_id
    return _WRITER_LABELS[(counted.writer_kind, same_person)]


def _fields_text(fields: frozenset[str]) -> str:
    return "the whole record" if ALL_FIELDS in fields else ", ".join(sorted(fields))


def _record_label(change: PlannedChange) -> str:
    return change.label or change.record.key


def _brief(writer: ChangeWriter, change: PlannedChange, shown: list[_CountedChange], case_id: str) -> dict[str, Any]:
    """What a refused request is shown: who changed which fields, the values now, and its own plan."""

    fields: frozenset[str] = frozenset().union(*(counted.fields for counted in shown))
    names = sorted(change.current) if ALL_FIELDS in fields else sorted(fields)
    listed = shown[-BRIEF_CHANGES_LIMIT:]
    return {
        "record": _record_label(change),
        "case_id": case_id,
        "changes": [
            {
                "by": _writer_label(writer, counted),
                "at": counted.changed_at,
                "fields": sorted(counted.fields),
            }
            for counted in listed
        ],
        "earlier_changes_not_listed": len(shown) - len(listed),
        "now": {name: _shown_value(change.current[name]) for name in names if name in change.current},
        "you_planned": {name: _shown_value(value) for name, value in sorted(change.planned.items())},
    }


def _changes_text(writer: ChangeWriter, shown: list[_CountedChange]) -> str:
    latest = shown[-1]
    text = f"{_writer_label(writer, latest)} changed {_fields_text(latest.fields)} at {latest.changed_at}"
    return text if len(shown) == 1 else f"{len(shown)} changes, the latest: {text}"


def _person_decides_text(overlap: frozenset[str]) -> str:
    return (
        f"It set {_fields_text(overlap)}, which your change sets too, and only the person can decide which to keep. "
        "Leave the record as it is, or read it again and save a change to other fields only; otherwise tell the "
        "person in your reply what changed and what you planned."
    )


def _closed(writer: ChangeWriter, record: RecordRef, state: RequestState) -> ChangeRefused:
    logger.info(
        "Refused a change from a request that is no longer running. event=change_ledger_request_closed "
        "request_id=%s state=%s record_kind=%s record_key=%s",
        writer.request_id,
        state.value,
        record.kind.value,
        record.key,
    )
    return ChangeRefused(
        ChangeRefusal.REQUEST_CLOSED,
        f"This request was {state.value}, so it no longer changes anything. Nothing was changed.",
        {"request_state": state.value},
    )


def _not_yet_seen(
    team_id: str,
    writer: ChangeWriter,
    change: PlannedChange,
    *,
    others: list[_CountedChange],
    unseen: list[_CountedChange],
    case_id: str,
    now: datetime,
) -> ChangeRefused:
    record = change.record
    overlap = _overlapping_fields(change.fields, others)
    next_step = (
        _person_decides_text(overlap)
        if overlap
        else "Your change sets other fields, so read the record again and save it; it goes through unless the "
        "record changes again."
    )
    logger.info(
        "Refused a change: another writer changed the record while this request ran. event=change_ledger_conflict "
        "request_id=%s record_kind=%s record_key=%s case_id=%s unseen=%s overlap=%s",
        writer.request_id, record.kind.value, record.key, case_id, len(unseen), bool(overlap),
    )
    return ChangeRefused(
        ChangeRefusal.CHANGE_CONFLICT,
        f"{_record_label(change)} was changed while this request was running ({_changes_text(writer, unseen)}), "
        "so this change was not made. The brief shows the record now and what you planned; you have now seen "
        f"this change. {next_step}",
        {"record_kind": record.kind.value, "case_id": case_id, "brief": _brief(writer, change, unseen, case_id)},
        review=PendingReview(
            team_id=team_id,
            request_id=writer.request_id,
            person_id=writer.person_id,
            record=record,
            shown_position=unseen[-1].position,
            case_id=case_id,
            at=now,
        ),
    )


def _needs_person(
    writer: ChangeWriter, change: PlannedChange, *, others: list[_CountedChange], overlap: frozenset[str], case_id: str
) -> ChangeRefused:
    record = change.record
    shown = [counted for counted in others if _overlapping_fields(change.fields, [counted])]
    logger.info(
        "Refused a change that sets fields another writer set while this request ran. "
        "event=change_ledger_needs_person request_id=%s record_kind=%s record_key=%s case_id=%s fields=%s",
        writer.request_id, record.kind.value, record.key, case_id, ",".join(sorted(overlap)),
    )
    return ChangeRefused(
        ChangeRefusal.NEEDS_PERSON_DECISION,
        f"{_record_label(change)} was changed while this request was running ({_changes_text(writer, shown)}), "
        f"so this change was not made. {_person_decides_text(overlap)}",
        {"record_kind": record.kind.value, "case_id": case_id, "brief": _brief(writer, change, shown, case_id)},
    )


def _escalated(writer: ChangeWriter, change: PlannedChange, until: datetime) -> ChangeRefused:
    record = change.record
    logger.warning(
        "Refused a change to a record several requests conflicted over. event=change_ledger_escalated "
        "request_id=%s record_kind=%s record_key=%s until=%s",
        writer.request_id, record.kind.value, record.key, until.isoformat(),
    )
    return ChangeRefused(
        ChangeRefusal.NEEDS_PERSON_DECISION,
        f"{_record_label(change)} met {ESCALATION_CASES} or more conflicts between requests within "
        f"{int(ESCALATION_WINDOW.total_seconds() // 60)} minutes, so requests' changes to it wait for the person "
        f"until {until.isoformat()}. This change was not made. Leave the record as it is and tell the person in "
        "your reply what you planned.",
        {"record_kind": record.kind.value, "escalated_until": until.isoformat()},
    )


__all__ = [
    "ALL_FIELDS",
    "BRIEF_VALUE_LIMIT",
    "CHANGE_CASES_TABLE",
    "CHANGE_LEDGER_TABLE",
    "CHANGE_PERSON_COLUMN",
    "CHANGE_REQUESTS_TABLE",
    "CHANGE_REVIEWS_TABLE",
    "COUNTED_WRITERS",
    "CaseState",
    "ChangeRefused",
    "ChangeRequestStore",
    "ChangeType",
    "ChangeWriter",
    "ChangeWriterKind",
    "ESCALATION_CASES",
    "ESCALATION_WINDOW",
    "LEDGER_LOCK_TIMEOUT_MS",
    "LEDGER_RETENTION",
    "PendingReview",
    "PlannedChange",
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
    "save_review",
]
