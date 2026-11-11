"""Durable admission and replies for commands that act on an agent's in-memory state.

Queue cancellation and runtime settings cannot share a database transaction with their
reply. Save admission before acting, and the final reply afterwards. A crash between
them leaves an explicit unknown result, so a retry never applies an older command over
a newer one. People can refresh the queue or setting to see what actually happened.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

from storage.config import StorageSettings
from storage.operation_outcomes import (
    OUTCOMES_TABLE, CommandOutcome, ensure_outcome_table, is_receipt, purge_expired, stored_outcome,
)
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite


class RelayedCommandOutcomes:
    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        with self._write() as conn:
            ensure_outcome_table(conn)

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        import sqlite3

        ensure_storage_parent_dir(self.settings.sqlite_path)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    @contextmanager
    def _write(self) -> Iterator[Any]:
        with self._connect() as conn:
            conn.begin_write()
            yield conn
            conn.commit()

    def admit(self, outcome: CommandOutcome) -> dict[str, Any] | None:
        with self._write() as conn:
            purge_expired(conn, self.dialect, outcome.now)
            if self.settings.postgres_dsn:
                # Separate agent processes can share the same manager database.
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                             (f"relayed-command:{outcome.team_id}:{outcome.retry_key}",))
            held = stored_outcome(conn, self.dialect, outcome.team_id, outcome.retry_key)
            if held is not None:
                return outcome.answer_from(held)
            outcome.save(conn, self.dialect, {"receipt": {"agent_command_admitted": True}})
        return None

    def complete(self, outcome: CommandOutcome, reply: dict[str, Any]) -> dict[str, Any]:
        with self._write() as conn:
            held = stored_outcome(conn, self.dialect, outcome.team_id, outcome.retry_key)
            if held is None:
                raise RuntimeError("The command's admission record is missing.")
            previous = outcome.answer_from(held)
            if not is_receipt(previous):
                return previous
            p = self.dialect.bind
            conn.execute(f"UPDATE {OUTCOMES_TABLE} SET reply_json = {p} WHERE team_id = {p} AND retry_key = {p}",
                         (json.dumps(reply, separators=(",", ":")), outcome.team_id, outcome.retry_key))
        return reply
