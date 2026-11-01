"""Add the columns an earlier release's tables lack, so an instance upgraded in place keeps working.

``CREATE TABLE IF NOT EXISTS`` never changes a table that already exists, so a
release that adds a column to a table an earlier release created adds it when
the store opens (AGENTS.md, "Change-specific requirements"). Stores call
``ensure_column`` from their schema setup on either backend; the connection's
``dialect`` picks the implementation. The column type carries the default that
keeps older rows correct. Each addition is logged (``event=storage_column_added``),
so an upgrade leaves a record of what it changed.
"""

from __future__ import annotations

import logging
from typing import Any

from .sql_dialect import POSTGRES
from .sqlite_connection import ensure_sqlite_column, log_added_column, sqlite_column_names

__all__ = [
    "ensure_column",
    "ensure_postgres_column",
    "postgres_column_names",
    "table_column_names",
]


def table_column_names(conn: Any, table_name: str) -> set[str]:
    """Return the column names of a repository-owned table on either backend."""

    if _is_postgres(conn):
        return postgres_column_names(conn, table_name)
    return sqlite_column_names(conn, table_name)


def ensure_column(
    conn: Any,
    table_name: str,
    column_name: str,
    column_type: str,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Add ``column_name`` to ``table_name`` when an earlier release created the table without it."""

    if _is_postgres(conn):
        ensure_postgres_column(conn, table_name, column_name, column_type, logger=logger)
    else:
        ensure_sqlite_column(conn, table_name, column_name, column_type, logger=logger)


def postgres_column_names(conn: Any, table_name: str) -> set[str]:
    """Return a PostgreSQL table's columns, finding the table the way the store's statements do.

    ``to_regclass`` resolves the unqualified name through the connection's
    search path, as ``ALTER TABLE`` does, so an instance kept in its own schema
    (and a temporary table) is read correctly. A lookup in
    ``information_schema`` for ``'public'`` found no columns for such an
    instance and then failed to add one its table already had.
    """

    rows = conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) AND attnum > 0 AND NOT attisdropped",
        (table_name,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def ensure_postgres_column(
    conn: Any,
    table_name: str,
    column_name: str,
    column_type: str,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Add a PostgreSQL column an earlier release's table lacks.

    The read first keeps later opens from taking the table's exclusive lock,
    which ``ALTER TABLE`` takes even when the column exists. ``ADD COLUMN IF
    NOT EXISTS`` lets processes starting together both run it: an error would
    abort the transaction and discard the schema statements before it.
    """

    if column_name in postgres_column_names(conn, table_name):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_type}")
    log_added_column(logger, "Postgres", table_name, column_name)


def _is_postgres(conn: Any) -> bool:
    return getattr(conn, "dialect", None) is POSTGRES
