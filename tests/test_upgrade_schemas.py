"""The in-place upgrade test option (tests/upgrade_schemas.py) and its fixture."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from storage.config import resolve_storage_settings
from tests.upgrade_schemas import _Schemas, read_schema_statements

FIXTURE = Path(__file__).parent / "fixtures" / "upgrade" / "0.9-sqlite.sql"


def test_the_previous_minor_versions_tables_load_into_an_empty_database():
    statements = read_schema_statements(FIXTURE)
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.execute("BEGIN")
    for statement in statements:
        conn.execute(statement)
    conn.execute("COMMIT")

    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"scheduled_messages", "change_ledger", "knowledge_pages", "pinned_memory_facts_active"} <= names
    # 0.9 ledger entries had no changed_fields; 0.10 adds it when the ledger opens.
    assert "changed_fields" not in {row[1] for row in conn.execute("PRAGMA table_info(change_ledger)")}


def test_databases_stores_create_start_with_the_older_tables_and_tests_own_do_not(tmp_path, monkeypatch):
    from storage.heartbeat_repository import create_heartbeat_repository
    from storage.sqlite_connection import connect_sqlite

    schemas = _Schemas(seed=("CREATE TABLE older (value TEXT)",), capture=None)
    monkeypatch.setattr(sqlite3, "connect", schemas.connect)
    store_path = tmp_path / "heartbeat.db"
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(store_path)}})

    create_heartbeat_repository(settings, logger=logging.getLogger("test"))
    with connect_sqlite(str(store_path)) as conn:
        assert {"older", "heartbeat_items"} <= _tables(conn)
        conn.execute("INSERT INTO older VALUES ('kept')")
    create_heartbeat_repository(settings, logger=logging.getLogger("test"))  # Not new any more.
    with connect_sqlite(str(store_path)) as conn:
        assert conn.execute("SELECT value FROM older").fetchall() == [("kept",)]

    with connect_sqlite(str(tmp_path / "own.db")) as own:  # A test's own database keeps what it builds.
        assert _tables(own) == set()
    assert _tables(sqlite3.connect(":memory:")) == set()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_capture_records_the_tables_product_code_creates_and_not_the_tests_own(tmp_path, monkeypatch):
    from storage.heartbeat_repository import create_heartbeat_repository

    capture = tmp_path / "capture.sql"
    schemas = _Schemas(seed=(), capture=capture)
    monkeypatch.setattr(sqlite3, "connect", schemas.connect)

    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "heartbeat.db")}})
    create_heartbeat_repository(settings, logger=logging.getLogger("test"))
    own = sqlite3.connect(str(tmp_path / "own.db"))
    own.execute("CREATE TABLE test_only (value TEXT)")
    own.close()
    schemas.finish()

    captured = read_schema_statements(capture)
    assert any(statement.startswith("CREATE TABLE IF NOT EXISTS heartbeat_items") for statement in captured)
    assert not any("test_only" in statement for statement in captured)
