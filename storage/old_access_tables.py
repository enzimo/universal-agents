"""Tables left behind by the removed permission system, and dropping them.

The old permission system (access Phase 4d removed it) and earlier removed
access pieces kept their records in database tables that no code reads now.
Upgrading leaves them in place, untouched, so an operator can inspect them
first; ``assistant_cli access drop-old-tables`` lists them and, when asked,
drops them.

Which database held what:

- the manager database: permission requests, reviews, decisions, grants,
  policies and snapshots (16 tables), the human-review Inbox (3 tables), the
  version 1 access workflows (2 tables; version 2 uses ``*_v2`` tables), and
  two schedule tables of the old schedule permits;
- the gateway database never held them, but it is checked too, because an
  operator may point both at one database;
- the broker's credential catalog: the credential-binding tables removed in
  access Phase 3.

Dropping is idempotent (``DROP TABLE IF EXISTS``), runs child tables first so
it also works with foreign-key enforcement, and never creates a missing SQLite
file.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from storage.config import SqliteStorageSettings, StorageSettings
from storage.postgres_connection import connect_postgres, redact_postgres_dsn
from storage.sqlite_connection import connect_sqlite

logger = logging.getLogger(__name__)

# Children before the records they point to (the old repository's delete order).
OLD_PERMISSION_TABLES: tuple[str, ...] = (
    "permission_grants",
    "permission_policy_proposals",
    "permission_request_transitions",
    "permission_reviews",
    "permission_decisions",
    "permission_approval_challenges",
    "permission_request_suppressions",
    "authorization_denial_receipts",
    "authorization_policy_snapshots",
    "authorization_administration_proposals",
    "authorization_administration_requests",
    "authorization_administration_snapshots",
    "emergency_deny_proposals",
    "emergency_deny_snapshots",
    "authorization_audit_events",
    "permission_requests",
)
OLD_HUMAN_INTERACTION_TABLES: tuple[str, ...] = (
    "human_interaction_outbox",
    "human_interaction_messages",
    "human_interactions",
)
# Version 1 workflows; storage.access_workflow_repository uses the *_v2 tables.
OLD_ACCESS_WORKFLOW_TABLES: tuple[str, ...] = (
    "access_workflow_events",
    "access_workflows",
)
OLD_SCHEDULE_ACCESS_TABLES: tuple[str, ...] = (
    "schedule_pause_mutation_receipts",
    "schedule_access_preparations",
)
OLD_STATE_TABLES: tuple[str, ...] = (
    *OLD_HUMAN_INTERACTION_TABLES,
    *OLD_PERMISSION_TABLES,
    *OLD_ACCESS_WORKFLOW_TABLES,
    *OLD_SCHEDULE_ACCESS_TABLES,
)
OLD_CREDENTIAL_CATALOG_TABLES: tuple[str, ...] = (
    "credential_capability_bindings",
    "credential_mcp_authentication",
)


@dataclass(frozen=True)
class OldTable:
    name: str
    rows: int


@dataclass(frozen=True)
class OldTableDatabase:
    """One database to check: what it is, where it is, and which tables to look for."""

    label: str
    settings: StorageSettings
    tables: tuple[str, ...]

    @property
    def location(self) -> str:
        if self.settings.postgres_dsn:
            return redact_postgres_dsn(self.settings.postgres_dsn)
        return str(self.settings.sqlite_path or "")

    @property
    def target(self) -> str:
        """Identity used to check a database once when two settings name it."""

        return self.settings.postgres_dsn or str(Path(str(self.settings.sqlite_path)).resolve())

    @property
    def exists(self) -> bool:
        return bool(self.settings.postgres_dsn) or Path(str(self.settings.sqlite_path or "")).is_file()


def sqlite_database(label: str, path: str | Path, tables: tuple[str, ...]) -> OldTableDatabase:
    """A plain SQLite file (the broker's credential catalog) as a database to check."""

    return OldTableDatabase(label, StorageSettings(backend="sqlite", sqlite=SqliteStorageSettings(path=str(path))), tables)


def merge_databases(databases: Iterable[OldTableDatabase]) -> list[OldTableDatabase]:
    """Check a database once even when two settings name it; labels and tables combine."""

    merged: dict[str, OldTableDatabase] = {}
    for database in databases:
        known = merged.get(database.target)
        if known is None:
            merged[database.target] = database
            continue
        tables = known.tables + tuple(name for name in database.tables if name not in known.tables)
        merged[database.target] = OldTableDatabase(f"{known.label} and {database.label}", known.settings, tables)
    return list(merged.values())


def _connect(settings: StorageSettings) -> Any:
    if settings.postgres_dsn:
        return connect_postgres(settings.postgres_dsn)
    return connect_sqlite(str(settings.sqlite_path), row_factory=sqlite3.Row)


def _existing_tables(conn: Any, *, postgres: bool) -> set[str]:
    if postgres:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        ).fetchall()
        return {str(row["table_name"]) for row in rows}
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def find_old_tables(database: OldTableDatabase) -> list[OldTable]:
    """The old tables present in one database, with their row counts, in drop order."""

    if not database.exists:
        return []
    postgres = bool(database.settings.postgres_dsn)
    with _connect(database.settings) as conn:
        existing = _existing_tables(conn, postgres=postgres)
        found = []
        for name in database.tables:
            if name not in existing:
                continue
            # Names come only from the fixed tuples above, never from input.
            row = conn.execute(f'SELECT COUNT(*) AS row_count FROM "{name}"').fetchone()
            found.append(OldTable(name, int(row["row_count"])))
    return found


def drop_old_tables(database: OldTableDatabase, tables: Iterable[OldTable]) -> list[str]:
    """Drop the given old tables in one transaction; returns the names dropped."""

    names = [table.name for table in tables]
    unknown = sorted(set(names) - set(database.tables))
    if unknown:
        raise ValueError(f"Not an old access table here: {', '.join(unknown)}")
    if not names:
        return []
    with _connect(database.settings) as conn:
        # SQLite runs DDL outside an implicit transaction; begin one so a
        # failure leaves every table in place (the context manager rolls back).
        conn.begin_write()
        for name in names:
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        conn.commit()
    logger.warning(
        "Dropped old access tables. event=old_access_tables_dropped database=%s location=%s tables=%s",
        database.label,
        database.location,
        ",".join(names),
    )
    return names


__all__ = [
    "OLD_ACCESS_WORKFLOW_TABLES",
    "OLD_CREDENTIAL_CATALOG_TABLES",
    "OLD_HUMAN_INTERACTION_TABLES",
    "OLD_PERMISSION_TABLES",
    "OLD_SCHEDULE_ACCESS_TABLES",
    "OLD_STATE_TABLES",
    "OldTable",
    "OldTableDatabase",
    "drop_old_tables",
    "find_old_tables",
    "merge_databases",
    "sqlite_database",
]
