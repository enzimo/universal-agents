"""Stored outcomes of protocol 1 change operations (message contract README "Stored outcomes").

A change operation is carried out once per command, however often its sender retries
it. The receiver keeps each command's final reply under the team and the command's
retry key (the frame's idempotency key, which ``verify_frame`` has already recomputed
from the registry's parts), so a repeat gets the same reply and changes nothing.

Why it is shaped this way:

- **In the change's own transaction.** A store that makes the change checks for a
  stored reply under its write lock, before it changes anything
  (``CommandOutcome.raise_if_stored``), and saves the reply before it commits
  (``CommandOutcome.save``). A crash between the change and the stored reply, or two
  retries at once, can therefore never carry out a command twice. The table lives in
  the same database as the stores that use it (the manager database), for SQLite and
  PostgreSQL alike. A handler may also look first, outside the lock
  (``stored_outcome``), so a repeat is answered before its body is checked again.
- **Refusals are final outcomes too.** A refused change rolls back; its reply is then
  stored in a transaction of its own (``save_refusal``), so a retry is told the same.
  Outcomes that ask for a retry (the contract's ``retry_outcomes``) are never stored.
- **Whose command it was.** Each reply records the person the command acted for. A
  stored reply answers only a repeat for the same person: the retry key comes from the
  sender's request id alone, so another person's command under a reused id is refused
  (``OutcomeKeyTaken``) and never shown that reply. The person column also lets a
  person's removal delete their replies (``storage.person_departure_repository``), which
  can quote their personal memory.
- **Kept 7 days**, the longest life of an authority token. Older replies are deleted
  whenever a new one is stored and by the team services' upkeep (``purge_expired``),
  so they also go when nothing new is stored.
- **A repeated key with another body** is the sender's defect: the stored reply still
  answers, and both body digests are logged.
- **Receipts for replies read after the change.** Some replies show what the change
  produced as readers see it once committed (a knowledge page with its links and notes),
  which the change's transaction cannot read. Such a command stores a receipt instead, a
  stored reply whose only key is ``receipt`` (``is_receipt``): what it changed, by id,
  and what the person confirmed, never the text. Its reply is built from the receipt
  after the commit, on the first try and every retry alike (``team_services.outcomes``),
  so a retry gets the same outcome for the same records, shown as they are then; the
  change itself is never carried out again.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from storage.schema_setup import begin_schema_setup
from storage.sql_dialect import SqlDialect

OUTCOMES_TABLE = "protocol1_operation_outcomes"
# The person a stored reply's command acted for (module docstring).
OUTCOME_PERSON_COLUMN = "person_id"
OUTCOME_RETENTION = timedelta(days=7)

# The one key of a stored receipt (module docstring); a contract reply always has an ``outcome``.
RECEIPT_KEY = "receipt"

_LOGGER = logging.getLogger(__name__)


def is_receipt(reply: dict[str, Any]) -> bool:
    """Whether a stored reply is a receipt to build the reply from (module docstring)."""

    return set(reply) == {RECEIPT_KEY}


def outcome_table_statements() -> tuple[str, ...]:
    return (
        f"""
        CREATE TABLE IF NOT EXISTS {OUTCOMES_TABLE} (
            team_id TEXT NOT NULL,
            retry_key TEXT NOT NULL,
            person_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            body_digest TEXT NOT NULL,
            reply_json TEXT NOT NULL,
            stored_at_utc TEXT NOT NULL,
            PRIMARY KEY (team_id, retry_key)
        )
        """,
        f"CREATE INDEX IF NOT EXISTS {OUTCOMES_TABLE}_stored_at ON {OUTCOMES_TABLE}(stored_at_utc)",
        f"CREATE INDEX IF NOT EXISTS {OUTCOMES_TABLE}_person ON {OUTCOMES_TABLE}({OUTCOME_PERSON_COLUMN})",
    )


def ensure_outcome_table(conn: Any) -> None:
    """Create the table on an open connection inside a store's own setup (the caller commits).

    Several stores create it inside their own setup, so it takes its own setup lock
    (``storage.schema_setup``) after the store's, as the change ledger's tables do.
    """

    begin_schema_setup(conn, __name__)
    for statement in outcome_table_statements():
        conn.execute(statement)


class OutcomeAlreadyStored(Exception):
    """The command was already answered; ``reply`` is its stored reply. Raised inside the write, which rolls back."""

    def __init__(self, reply: dict[str, Any]) -> None:
        super().__init__("This command was already answered.")
        self.reply = reply


class OutcomeKeyTaken(Exception):
    """Another person's command was already answered under this retry key; nothing was changed or shown.

    The sender reused a request id (a defect: ids are fresh UUIDv7s). The command is
    refused, and the stored reply, which may quote that person's memory, stays private.
    """

    def __init__(self, kind: str, retry_key: str) -> None:
        super().__init__(f"The request id of this {kind} was already used; send the command again.")
        self.kind = kind
        self.retry_key = retry_key


@dataclass(frozen=True)
class StoredOutcome:
    reply: dict[str, Any]
    body_digest: str
    person_id: str


@dataclass(frozen=True)
class CommandOutcome:
    """One change command's retry key and person, and how to turn the store's result into its reply."""

    team_id: str
    retry_key: str
    person_id: str
    kind: str
    body_digest: str
    now: datetime
    reply_for: Callable[[Any], dict[str, Any]]
    logger: logging.Logger = _LOGGER

    def raise_if_stored(self, conn: Any, dialect: SqlDialect) -> None:
        """Under the store's write lock, before any change: answer a repeat with its stored reply."""

        stored = stored_outcome(conn, dialect, self.team_id, self.retry_key)
        if stored is not None:
            raise OutcomeAlreadyStored(self.answer_from(stored))

    def answer_from(self, stored: StoredOutcome) -> dict[str, Any]:
        """The stored reply for this command, after checking it is the same person's (module docstring)."""

        if stored.person_id != self.person_id:
            self.logger.error(
                "A command reused another person's retry key; it is refused and the stored reply is not shown. "
                "event=operation_outcome_key_taken kind=%s retry_key=%s person=%s",
                self.kind, self.retry_key, self.person_id,
            )
            raise OutcomeKeyTaken(self.kind, self.retry_key)
        if stored.body_digest != self.body_digest:
            self.logger.warning(
                "A command was repeated with another body; the stored reply answers it. "
                "event=operation_outcome_body_mismatch kind=%s retry_key=%s stored_body_digest=%s body_digest=%s",
                self.kind, self.retry_key, stored.body_digest, self.body_digest,
            )
        return stored.reply

    def save(self, conn: Any, dialect: SqlDialect, result: Any) -> dict[str, Any]:
        """Inside the change's transaction, before it commits: store and return the reply for ``result``."""

        reply = self.reply_for(result)
        _insert(conn, dialect, self, reply)
        return reply


def stored_outcome(conn: Any, dialect: SqlDialect, team_id: str, retry_key: str) -> Optional[StoredOutcome]:
    p = dialect.bind
    row = conn.execute(
        f"SELECT reply_json, body_digest, {OUTCOME_PERSON_COLUMN} FROM {OUTCOMES_TABLE} "
        f"WHERE team_id = {p} AND retry_key = {p}",
        (team_id, retry_key),
    ).fetchone()
    if row is None:
        return None
    return StoredOutcome(json.loads(row["reply_json"]), str(row["body_digest"]), str(row[OUTCOME_PERSON_COLUMN]))


def purge_expired(conn: Any, dialect: SqlDialect, now: datetime) -> int:
    """Delete replies older than ``OUTCOME_RETENTION`` (the caller commits); how many were deleted."""

    cursor = conn.execute(f"DELETE FROM {OUTCOMES_TABLE} WHERE stored_at_utc < {dialect.bind}",
                          ((now - OUTCOME_RETENTION).isoformat(),))
    return int(cursor.rowcount or 0)


def save_refusal(conn: Any, dialect: SqlDialect, outcome: CommandOutcome, reply: dict[str, Any]) -> Optional[StoredOutcome]:
    """Store a refused command's final reply in its own transaction (the caller commits).

    Returns what is stored under the key now: this reply, or the one a copy of the command
    that raced this one stored first, which then answers. None, storing nothing, for an
    outcome that asks for a retry.
    """

    # The contract's retry_outcomes (generated): storing one would answer every later retry with it.
    # Imported here: the memory store is imported by processes that never store an outcome (the gateway,
    # command-line runs), and the generated contract models take half a second to load.
    from custom_messages.wire.kinds import RETRY_OUTCOMES

    if reply.get("outcome") in RETRY_OUTCOMES:
        return None
    _insert(conn, dialect, outcome, reply)
    return stored_outcome(conn, dialect, outcome.team_id, outcome.retry_key)


def _insert(conn: Any, dialect: SqlDialect, outcome: CommandOutcome, reply: dict[str, Any]) -> None:
    p = dialect.bind
    purge_expired(conn, dialect, outcome.now)
    conn.execute(
        f"""
        INSERT INTO {OUTCOMES_TABLE}
            (team_id, retry_key, {OUTCOME_PERSON_COLUMN}, kind, body_digest, reply_json, stored_at_utc)
        VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})
        ON CONFLICT (team_id, retry_key) DO NOTHING
        """,
        (outcome.team_id, outcome.retry_key, outcome.person_id, outcome.kind, outcome.body_digest,
         json.dumps(reply, separators=(",", ":"), sort_keys=True), outcome.now.isoformat()),
    )
