"""The in-place upgrade test option (tests/upgrade_schemas.py) and its fixture."""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import ModuleType, SimpleNamespace

import pytest

from storage.config import resolve_storage_settings
from storage.sqlite_vec_support import load_sqlite_vec, serialize_float32
from tests import upgrade_schemas
from tests.upgrade_schemas import _Schemas, read_schema_statements

FIXTURE = Path(__file__).parent / "fixtures" / "upgrade" / "0.9-sqlite.sql"


def test_the_previous_minor_versions_tables_load_into_an_empty_database():
    statements = read_schema_statements(FIXTURE)
    from storage.memory_graph_repository import sqlite3 as driver

    conn = driver.connect(":memory:")
    load_sqlite_vec(conn)
    conn.isolation_level = None
    conn.execute("BEGIN")
    for statement in statements:
        conn.execute(statement)
    conn.execute("COMMIT")

    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"scheduled_messages", "change_ledger", "knowledge_pages", "pinned_memory_facts_active"} <= names
    # 0.9 ledger entries had no changed_fields; 0.10 adds it when the ledger opens.
    assert "changed_fields" not in {row[1] for row in conn.execute("PRAGMA table_info(change_ledger)")}
    assert "memory_graph_vectors" in names
    conn.close()


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
    from scripts.upgrade_schema_fixture import build
    from storage.heartbeat_repository import create_heartbeat_repository

    capture = tmp_path / "capture.sql"
    schemas = _Schemas(seed=(), capture=capture)
    monkeypatch.setattr(sqlite3, "connect", schemas.connect)

    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "heartbeat.db")}})
    create_heartbeat_repository(settings, logger=logging.getLogger("test"))
    own = sqlite3.connect(str(tmp_path / "own.db"))
    own.execute("CREATE TABLE test_only (value TEXT)")
    own.close()
    fixture_only = build(["CREATE TABLE fixture_test_only (value TEXT)"])
    fixture_only.close()
    schemas.finish()

    captured = read_schema_statements(capture)
    assert any(statement.startswith("CREATE TABLE IF NOT EXISTS heartbeat_items") for statement in captured)
    assert not any("test_only" in statement for statement in captured)


@pytest.mark.own_database
def test_historical_vector_table_and_stored_embedding_remain_searchable(tmp_path):
    from storage.memory_graph_repository import SqliteMemoryGraphRepository, sqlite3 as driver

    path = tmp_path / "memory.db"
    conn = driver.connect(str(path))
    load_sqlite_vec(conn)
    with conn:
        for statement in read_schema_statements(FIXTURE):
            conn.execute(statement)
        # Columns and values written by 0.9, before the current repository opens
        # the file. Searching must reuse the old table and its stored embedding.
        conn.execute(
            "INSERT INTO memory_entities "
            "(entity_id, subject_id, entity_type, canonical_name, normalized_name, embedding, created_at, updated_at) "
            "VALUES ('entity_old', 'subject_old', 'concept', 'SQLite', 'sqlite', '[1,0,0]', 'old', 'old')"
        )
        conn.execute(
            "INSERT INTO memory_graph_vector_index_state VALUES ('default', 'sqlite_vec', 3, '0.1.9', 'old', 'old')"
        )
        conn.execute(
            "INSERT INTO memory_graph_vector_records VALUES (1, 'entity', 'entity_old', 'subject_old', NULL, 3, 'old')"
        )
        conn.execute(
            "INSERT INTO memory_graph_vectors "
            "(rowid, record_kind, record_id, subject_id, embedding_profile_id, embedding) "
            "VALUES (1, 'entity', 'entity_old', 'subject_old', '', ?)",
            (serialize_float32([1, 0, 0]),),
        )
        before = conn.execute("SELECT sql FROM sqlite_master WHERE name='memory_graph_vectors'").fetchone()[0]
    conn.close()

    repo = SqliteMemoryGraphRepository(str(path))
    found = repo.search_entities_by_embedding(subject_id="subject_old", query_embedding=[1, 0, 0])
    assert [entity.entity_id for entity in found] == ["entity_old"]
    with repo._connect() as current:
        load_sqlite_vec(current)
        assert current.execute("SELECT sql FROM sqlite_master WHERE name='memory_graph_vectors'").fetchone()[0] == before
        assert current.execute("SELECT COUNT(*) FROM memory_graph_vectors").fetchone()[0] == 1


def test_configured_alternate_driver_seeds_and_captures_the_real_memory_store(tmp_path, monkeypatch):
    import storage.memory_graph_repository as memory

    # A separate module has a separate connect attribute, as pysqlite3 does.
    driver = ModuleType("alternate_sqlite")
    driver.__dict__.update(sqlite3.__dict__)
    driver.connect = upgrade_schemas._original_connect
    monkeypatch.setattr(upgrade_schemas, "_DRIVERS", (driver,))
    monkeypatch.setitem(upgrade_schemas._ORIGINAL_CONNECTS, driver, driver.connect)
    monkeypatch.setattr(memory, "sqlite3", driver)
    fixture = tmp_path / "older.sql"
    fixture.write_text("CREATE TABLE older (value TEXT);\n")
    capture = tmp_path / "capture.sql"
    options = {"--upgrade-from": str(fixture), "--capture-schema": str(capture)}
    config = SimpleNamespace(getoption=options.get, stash={})
    upgrade_schemas.pytest_configure(config)
    try:
        repo = memory.SqliteMemoryGraphRepository(str(tmp_path / "memory.db"), vector_enabled=False)
        with repo._connect() as conn:
            assert "older" in _tables(conn)
    finally:
        upgrade_schemas.pytest_unconfigure(config)
    assert any("CREATE TABLE IF NOT EXISTS memory_entities" in statement for statement in read_schema_statements(capture))
    assert driver.connect is upgrade_schemas._original_connect


def test_concurrent_first_open_seeds_once(tmp_path, monkeypatch):
    from storage.heartbeat_repository import create_heartbeat_repository

    path = tmp_path / "heartbeat.db"
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(path)}})
    barrier = Barrier(2, timeout=10)
    is_new = upgrade_schemas._is_new_database

    def simultaneous_first_open(target):
        new = is_new(target)
        if new and str(target) == str(path):
            barrier.wait()
        return new

    schemas = _Schemas(seed=("CREATE TABLE older (value TEXT)",), capture=None)
    monkeypatch.setattr(upgrade_schemas, "_is_new_database", simultaneous_first_open)
    monkeypatch.setattr(sqlite3, "connect", schemas.connect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(create_heartbeat_repository, settings, logger=logging.getLogger("test")) for _ in range(2)]
        for result in results:
            result.result(timeout=15)
    with upgrade_schemas._original_connect(str(path)) as conn:
        assert {"older", "heartbeat_items"} <= _tables(conn)


def test_spawn_only_store_uses_previous_tables_and_is_captured(tmp_path):
    root = tmp_path / "spawn"
    capture = tmp_path / "spawn.sql"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short",
         "tests/test_knowledge_concurrency.py::test_write_limit_is_exact_across_processes[sqlite]",
         "--upgrade-from", str(FIXTURE), "--capture-schema", str(capture), "--basetemp", str(root)],
        cwd=FIXTURE.parents[3],
        env={**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    path, = root.rglob("knowledge.db")
    with upgrade_schemas._original_connect(str(path)) as conn:
        assert {"scheduled_messages", "pinned_memory_facts_active", "knowledge_pages"} <= _tables(conn)
    assert any("CREATE TABLE IF NOT EXISTS knowledge_pages" in statement for statement in read_schema_statements(capture))
