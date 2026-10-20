"""Give a team's single-user gateway rows to its first administrator.

With ``gateway.users.enabled`` off, the gateway records sessions, requests and
deliveries from channels other than the CLI without an owner. When an operator
turns multi-user mode on, those rows belong to the one person who used the
instance, so gateway startup (and ``scripts/migrate_legacy_single_user_owner.py``)
gives every gateway row with no owner to the bootstrap administrator.

Only gateway rows take part. In the manager's store (conversations, archived
tasks, schedules) a missing owner means work for no person or work whose person
could not be verified, and handing such rows to the first administrator would
make system work theirs and let a schedule with no owner run as them. Those
rows keep the owner their verified identity gave them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from .config import StorageSettings
from .postgres_connection import PostgresConnection, connect_postgres, redact_postgres_dsn
from .sqlite_connection import connect_sqlite


GATEWAY_OWNER_TABLES: tuple[str, ...] = (
    "gateway_sessions",
    "gateway_session_conversations",
    "gateway_requests",
    "gateway_deliveries",
    "gateway_attachments",
    "gateway_questions",
    "gateway_scheduled_targets",
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwnerMigrationTableResult:
    table: str
    status: str
    updated_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "status": self.status, "updated_rows": self.updated_rows}


@dataclass(frozen=True)
class OwnerMigrationReport:
    owner_canonical_user_id: str
    backend: str
    target: str
    tables: tuple[OwnerMigrationTableResult, ...]

    @property
    def updated_rows(self) -> int:
        return sum(result.updated_rows for result in self.tables)

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_canonical_user_id": self.owner_canonical_user_id,
            "backend": self.backend,
            "target": self.target,
            "updated_rows": self.updated_rows,
            "tables": [result.to_dict() for result in self.tables],
        }


def migrate_legacy_null_owner_rows(
    *,
    owner_canonical_user_id: str,
    gateway_storage_settings: StorageSettings,
) -> OwnerMigrationReport:
    """Give every gateway row with no owner to ``owner_canonical_user_id`` (module docstring)."""

    owner = str(owner_canonical_user_id or "").strip()
    if not owner:
        raise ValueError("owner_canonical_user_id is required.")
    settings = gateway_storage_settings

    if settings.backend == STORAGE_BACKEND_SQLITE and settings.sqlite_path:
        with connect_sqlite(settings.sqlite_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            tables = tuple(_migrate_sqlite_table(conn, table, owner) for table in GATEWAY_OWNER_TABLES)
            conn.commit()
        return OwnerMigrationReport(owner, settings.backend, settings.sqlite_path, tables)

    if settings.backend == STORAGE_BACKEND_POSTGRES and settings.postgres_dsn:
        with connect_postgres(settings.postgres_dsn) as conn:
            tables = tuple(_migrate_postgres_table(conn, table, owner) for table in GATEWAY_OWNER_TABLES)
            conn.commit()
        return OwnerMigrationReport(owner, settings.backend, redact_postgres_dsn(settings.postgres_dsn), tables)

    raise ValueError(f"Unsupported storage settings for owner migration: {settings.backend!r}")


def _migrate_sqlite_table(conn: Any, table: str, owner_canonical_user_id: str) -> OwnerMigrationTableResult:
    if not _sqlite_table_exists(conn, table):
        return OwnerMigrationTableResult(table=table, status="missing_table")
    if not _sqlite_column_exists(conn, table, "owner_canonical_user_id"):
        return OwnerMigrationTableResult(table=table, status="missing_owner_column")
    return _assign_owner(conn, table, owner_canonical_user_id)


def _migrate_postgres_table(
    conn: PostgresConnection,
    table: str,
    owner_canonical_user_id: str,
) -> OwnerMigrationTableResult:
    if not _postgres_column_exists(conn, table, "owner_canonical_user_id"):
        return OwnerMigrationTableResult(table=table, status="missing_table_or_owner_column")
    return _assign_owner(conn, table, owner_canonical_user_id)


def _assign_owner(conn: Any, table: str, owner_canonical_user_id: str) -> OwnerMigrationTableResult:
    cursor = conn.execute(
        f"UPDATE {table} SET owner_canonical_user_id = {conn.dialect.bind} WHERE owner_canonical_user_id IS NULL",
        (owner_canonical_user_id,),
    )
    return OwnerMigrationTableResult(table=table, status="updated", updated_rows=max(0, int(cursor.rowcount)))


def _sqlite_table_exists(conn: Any, table: str) -> bool:
    row = conn.execute(
        f"SELECT name FROM sqlite_master WHERE type = 'table' AND name = {conn.dialect.bind}",
        (table,),
    ).fetchone()
    return row is not None


def _sqlite_column_exists(conn: Any, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return column in {row[1] for row in rows}


def _postgres_column_exists(conn: PostgresConnection, table: str, column: str) -> bool:
    row = conn.execute(
        f"""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = {conn.dialect.bind} AND column_name = {conn.dialect.bind}
        LIMIT 1
        """,
        (table, column),
    ).fetchone()
    return row is not None
