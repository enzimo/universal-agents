"""Adding the columns an earlier release's tables lack (``storage.table_columns``).

An instance upgraded in place keeps its tables, and ``CREATE TABLE IF NOT
EXISTS`` never changes them, so stores add new columns when they open.
"""

from __future__ import annotations

import logging
import os
from uuid import uuid4

import pytest

from storage.postgres_connection import connect_postgres
from storage.sql_dialect import POSTGRES
from storage.sqlite_connection import connect_sqlite
from storage.table_columns import ensure_column, table_column_names

ADDED = "event=storage_column_added"
LOGGER = logging.getLogger("test.table_columns")


def test_sqlite_adds_a_missing_column_once_and_older_rows_get_its_default(tmp_path, caplog):
    path = str(tmp_path / "older.db")
    with connect_sqlite(path) as conn:
        conn.execute("CREATE TABLE work (work_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO work VALUES ('older')")

    with caplog.at_level(logging.INFO, logger=LOGGER.name):
        for _ in range(2):  # The second open finds the column.
            with connect_sqlite(path) as conn:
                ensure_column(conn, "work", "state", "TEXT NOT NULL DEFAULT 'open'", logger=LOGGER)

    assert caplog.text.count(f"{ADDED} backend=SQLite table=work column=state") == 1
    with connect_sqlite(path) as conn:
        assert table_column_names(conn, "work") == {"work_id", "state"}
        assert conn.execute("SELECT state FROM work").fetchone()[0] == "open"


class _Rows:
    def __init__(self, rows: list[tuple[str]]):
        self._rows = rows

    def fetchall(self) -> list[tuple[str]]:
        return self._rows


class _FakePostgres:
    """Records statements and answers the column read with fixed columns."""

    dialect = POSTGRES

    def __init__(self, columns: set[str]):
        self.columns = columns
        self.statements: list[str] = []

    def execute(self, query: str, params: object = None) -> _Rows:
        self.statements.append(query)
        return _Rows([(column,) for column in sorted(self.columns)] if "pg_attribute" in query else [])


def test_postgres_takes_no_table_lock_when_the_column_exists():
    conn = _FakePostgres({"entity_id", "embedding_profile_id"})

    ensure_column(conn, "memory_entities", "embedding_profile_id", "TEXT")

    assert not any(statement.startswith("ALTER") for statement in conn.statements)


def test_postgres_adds_a_missing_column_in_a_form_concurrent_starts_both_run(caplog):
    conn = _FakePostgres({"entity_id"})

    with caplog.at_level(logging.INFO, logger=LOGGER.name):
        ensure_column(conn, "memory_entities", "embedding_profile_id", "TEXT", logger=LOGGER)

    assert conn.statements[-1] == "ALTER TABLE memory_entities ADD COLUMN IF NOT EXISTS embedding_profile_id TEXT"
    assert f"{ADDED} backend=Postgres table=memory_entities column=embedding_profile_id" in caplog.text


@pytest.mark.live
@pytest.mark.postgres
def test_postgres_reads_the_table_in_the_instances_own_schema(caplog):
    """A same-named table in ``public`` is neither read nor changed.

    The activity collector read columns from ``public`` and then added a
    column its own-schema table already had, which failed its start.
    """

    from psycopg.conninfo import make_conninfo

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN required")
    schema = "table_columns_" + uuid4().hex
    table = "work_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE TABLE {schema}.{table} (work_id TEXT PRIMARY KEY, state TEXT)")
        conn.execute(f"CREATE TABLE public.{table} (work_id TEXT PRIMARY KEY)")
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER.name):
            with connect_postgres(make_conninfo(dsn, options=f"-csearch_path={schema}")) as conn:
                ensure_column(conn, table, "state", "TEXT", logger=LOGGER)
                ensure_column(conn, table, "note", "TEXT NOT NULL DEFAULT ''", logger=LOGGER)
                assert table_column_names(conn, table) == {"work_id", "state", "note"}
        assert "column=state" not in caplog.text
        assert caplog.text.count(f"{ADDED} backend=Postgres table={table} column=note") == 1
        with connect_postgres(dsn) as conn:
            assert table_column_names(conn, f"public.{table}") == {"work_id"}
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.execute(f"DROP TABLE public.{table}")
