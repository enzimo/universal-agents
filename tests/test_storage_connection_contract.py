"""SQL is constructed for the database and executed without rewriting literals."""

import os
import ast
from pathlib import Path

import pytest

from storage.postgres_connection import connect_postgres
from storage.sqlite_connection import connect_sqlite


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])])
def connection(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("LIVE_TEST_POSTGRES_DSN required")
        return connect_postgres(dsn)
    return connect_sqlite(str(tmp_path / "native.db"))


def test_native_bind_does_not_rewrite_literals_comments_or_values(connection):
    with connection as conn:
        row = conn.execute(
            f"SELECT '?' AS punctuation, 'DATETIME' AS type_name, "
            f"{conn.dialect.bind} AS input_value /* ? DATETIME */",
            ("? DATETIME %s",),
        ).fetchone()
        assert tuple(row[index] for index in range(3)) == ("?", "DATETIME", "? DATETIME %s")


def test_iteration_retains_transaction_and_does_not_limit_candidates(connection):
    with connection as conn:
        conn.execute("CREATE TEMP TABLE candidates (position INTEGER)")
        for i in range(601):
            conn.execute(f"INSERT INTO candidates VALUES ({conn.dialect.bind})", (i,))
        rows = conn.iterate("SELECT position FROM candidates ORDER BY position", batch_size=32)
        assert [row[0] for row in rows] == list(range(601))


@pytest.mark.live
@pytest.mark.postgres
def test_postgres_idempotent_column_add_preserves_prior_transaction_work():
    from storage.memory_graph_repository import PostgresMemoryGraphRepository

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN required")
    repo = PostgresMemoryGraphRepository.__new__(PostgresMemoryGraphRepository)
    with connect_postgres(dsn) as conn:
        conn.execute("CREATE TEMP TABLE schema_work (value TEXT)")
        conn.execute("INSERT INTO schema_work VALUES ('retained')")
        repo._ensure_column(conn, "schema_work", "value", "TEXT")
        assert conn.execute("SELECT value FROM schema_work").fetchone()[0] == "retained"


def test_postgres_adapters_do_not_inherit_sqlite_implementations():
    storage = Path(__file__).resolve().parents[1] / "storage"
    for source in storage.glob("*_repository.py"):
        for node in ast.parse(source.read_text()).body:
            if isinstance(node, ast.ClassDef) and node.name.startswith("Postgres"):
                assert all(
                    not ast.unparse(base).startswith("Sqlite") for base in node.bases
                ), f"{source.name}: {node.name} depends on SQLite implementation"
