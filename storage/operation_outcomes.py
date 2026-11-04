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
  PostgreSQL alike.
- **Refusals are final outcomes too.** A refused change rolls back; its reply is then
  stored in a transaction of its own (``save_refusal``), so a retry is told the same.
  Outcomes that ask for a retry (``RETRY_OUTCOMES``) are never stored.
- **Kept 7 days**, the longest life of an authority token; older replies are deleted
  whenever a new one is stored.
- **A repeated key with another body** is the sender's defect: the stored reply still
  answers, and both body digests are logged.
- **Receipts for replies read after the change.** Some replies show what the change
  produced as readers see it once committed (a knowledge page with its links and notes).
  Such a command stores a receipt in the change's transaction (``complete=False``: what
  it changed, by id), and the full reply right after the commit (``complete_outcome``).
  A retry that finds only the receipt (the process stopped between the two) rebuilds the
  reply from it; the change itself is never carried out again.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from storage.sql_dialect import SqlDialect

OUTCOMES_TABLE = "protocol1_operation_outcomes"
OUTCOME_RETENTION = timedelta(days=7)
# Outcomes that ask the sender to try again: storing one would answer every later retry with it.
RETRY_OUTCOMES = frozenset({"not_started", "record_busy", "waiting", "runtime_error"})

_LOGGER = logging.getLogger(__name__)


def outcome_table_statements() -> tuple[str, ...]:
    return (
        f"""
        CREATE TABLE IF NOT EXISTS {OUTCOMES_TABLE} (
            team_id TEXT NOT NULL,
            retry_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            body_digest TEXT NOT NULL,
            reply_json TEXT NOT NULL,
            complete INTEGER NOT NULL,
            stored_at_utc TEXT NOT NULL,
            PRIMARY KEY (team_id, retry_key)
        )
        """,
        f"CREATE INDEX IF NOT EXISTS {OUTCOMES_TABLE}_stored_at ON {OUTCOMES_TABLE}(stored_at_utc)",
    )


def ensure_outcome_table(conn: Any) -> None:
    """Create the table on an open connection inside a store's own setup (the caller commits)."""

    for statement in outcome_table_statements():
        conn.execute(statement)


class OutcomeAlreadyStored(Exception):
    """The command was already carried out; raised inside the write, which rolls back.

    ``reply`` is its stored reply, or its receipt when ``complete`` is False.
    """

    def __init__(self, reply: dict[str, Any], complete: bool = True) -> None:
        super().__init__("This command was already answered.")
        self.reply = reply
        self.complete = complete


@dataclass(frozen=True)
class CommandOutcome:
    """One change command's retry key, and how to turn the store's result into its reply."""

    team_id: str
    retry_key: str
    kind: str
    body_digest: str
    now: datetime
    reply_for: Callable[[Any], dict[str, Any]]
    logger: logging.Logger = _LOGGER
    # False: reply_for gives a receipt, and the full reply follows with complete_outcome.
    complete: bool = True

    def raise_if_stored(self, conn: Any, dialect: SqlDialect) -> None:
        """Under the store's write lock, before any change: answer a repeat with its stored reply."""

        stored = stored_reply(conn, dialect, self.team_id, self.retry_key)
        if stored is None:
            return
        reply, body_digest, complete = stored
        if body_digest != self.body_digest:
            self.logger.warning(
                "A command was repeated with another body; the stored reply answers it. "
                "event=operation_outcome_body_mismatch kind=%s retry_key=%s stored_body_digest=%s body_digest=%s",
                self.kind, self.retry_key, body_digest, self.body_digest,
            )
        raise OutcomeAlreadyStored(reply, complete)

    def save(self, conn: Any, dialect: SqlDialect, result: Any) -> dict[str, Any]:
        """Inside the change's transaction, before it commits: store and return the reply (or receipt) for ``result``."""

        reply = self.reply_for(result)
        _insert(conn, dialect, self, reply, complete=self.complete)
        return reply


def stored_reply(conn: Any, dialect: SqlDialect, team_id: str,
                 retry_key: str) -> Optional[tuple[dict[str, Any], str, bool]]:
    """(reply or receipt, body digest, whether it is the full reply), or None."""

    p = dialect.bind
    row = conn.execute(
        f"SELECT reply_json, body_digest, complete FROM {OUTCOMES_TABLE} WHERE team_id = {p} AND retry_key = {p}",
        (team_id, retry_key),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["reply_json"]), str(row["body_digest"]), bool(row["complete"])


def complete_outcome(conn: Any, dialect: SqlDialect, outcome: CommandOutcome, reply: dict[str, Any]) -> None:
    """Replace a stored receipt with the full reply (the caller commits); a full reply already stored is kept."""

    p = dialect.bind
    conn.execute(
        f"UPDATE {OUTCOMES_TABLE} SET reply_json = {p}, complete = 1 WHERE team_id = {p} AND retry_key = {p} "
        f"AND complete = 0",
        (_json(reply), outcome.team_id, outcome.retry_key),
    )


def save_refusal(conn: Any, dialect: SqlDialect, outcome: CommandOutcome, reply: dict[str, Any]) -> bool:
    """Store a refused command's final reply in its own transaction (the caller commits).

    Returns False, storing nothing, for an outcome that asks for a retry. A reply already
    stored under the key (a retry that raced this one) is kept.
    """

    if reply.get("outcome") in RETRY_OUTCOMES:
        return False
    _insert(conn, dialect, outcome, reply, complete=True)
    return True


def _json(reply: dict[str, Any]) -> str:
    return json.dumps(reply, separators=(",", ":"), sort_keys=True)


def _insert(conn: Any, dialect: SqlDialect, outcome: CommandOutcome, reply: dict[str, Any], *, complete: bool) -> None:
    p = dialect.bind
    conn.execute(f"DELETE FROM {OUTCOMES_TABLE} WHERE stored_at_utc < {p}",
                 ((outcome.now - OUTCOME_RETENTION).isoformat(),))
    conn.execute(
        f"""
        INSERT INTO {OUTCOMES_TABLE} (team_id, retry_key, kind, body_digest, reply_json, complete, stored_at_utc)
        VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})
        ON CONFLICT (team_id, retry_key) DO NOTHING
        """,
        (outcome.team_id, outcome.retry_key, outcome.kind, outcome.body_digest, _json(reply), int(complete),
         outcome.now.isoformat()),
    )


class CommandOutcomeWrites:
    """For a store with ``_write()`` (a write transaction under its lock) and ``_dialect``.

    Refusals and completed replies are stored in transactions of their own, after the
    change's transaction rolled back or committed.
    """

    _dialect: SqlDialect

    def _write(self) -> Any:  # pragma: no cover - the store provides it
        raise NotImplementedError

    def _connect(self) -> Any:  # pragma: no cover - the store provides it
        raise NotImplementedError

    def stored_command_outcome(self, command_outcome: CommandOutcome) -> Optional[tuple[dict[str, Any], str, bool]]:
        """A read outside any change: (reply or receipt, body digest, complete), or None."""

        with self._connect() as conn:
            return stored_reply(conn, self._dialect, command_outcome.team_id, command_outcome.retry_key)

    def save_command_refusal(self, command_outcome: CommandOutcome, reply: dict[str, Any]) -> None:
        with self._write() as conn:
            save_refusal(conn, self._dialect, command_outcome, reply)

    def complete_command_outcome(self, command_outcome: CommandOutcome, reply: dict[str, Any]) -> None:
        with self._write() as conn:
            complete_outcome(conn, self._dialect, command_outcome, reply)

