"""``assistant_cli access drop-old-tables`` lists, and with --yes drops, only old tables.

The command runs against real SQLite files built by a saved instance config,
the same way an operator runs it. PostgreSQL is covered by a statement-recording
connection here and by a live test when ``LIVE_TEST_POSTGRES_DSN`` is set.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from storage.config import resolve_storage_settings
from storage.old_access_tables import (
    OLD_CREDENTIAL_CATALOG_TABLES,
    OLD_STATE_TABLES,
    OldTable,
    OldTableDatabase,
    drop_old_tables,
    find_old_tables,
    sqlite_database,
)

# Two old tables linked by a foreign key, two unlinked old tables, and tables
# the current system uses (which must survive).
MANAGER_TABLES = {
    "permission_requests": "CREATE TABLE permission_requests (request_id TEXT PRIMARY KEY)",
    "permission_grants": (
        "CREATE TABLE permission_grants (grant_id TEXT PRIMARY KEY, "
        "request_id TEXT REFERENCES permission_requests(request_id))"
    ),
    "human_interactions": "CREATE TABLE human_interactions (interaction_id TEXT PRIMARY KEY)",
    "access_workflows": "CREATE TABLE access_workflows (workflow_id TEXT PRIMARY KEY)",
    "schedule_access_preparations": "CREATE TABLE schedule_access_preparations (request_id TEXT PRIMARY KEY)",
    "access_workflows_v2": "CREATE TABLE access_workflows_v2 (workflow_id TEXT PRIMARY KEY)",
    "access_grants": "CREATE TABLE access_grants (grant_id TEXT PRIMARY KEY)",
}
MANAGER_ROWS = {
    "permission_requests": ["req_1", "req_2"],
    "permission_grants": ["grant_1"],
    "human_interactions": ["hi_1"],
    "access_workflows_v2": ["wf_new"],
    "access_grants": ["g_new"],
}
KEPT = {"access_workflows_v2", "access_grants"}


def _build(path: Path, tables: dict[str, str], rows: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for ddl in tables.values():
            conn.execute(ddl)
        for table, ids in rows.items():
            for index, row_id in enumerate(ids):
                if table == "permission_grants":
                    conn.execute(f"INSERT INTO {table} VALUES (?, ?)", (row_id, MANAGER_ROWS["permission_requests"][index]))
                else:
                    conn.execute(f"INSERT INTO {table} VALUES (?)", (row_id,))
    conn.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


@pytest.fixture
def instance(tmp_path: Path) -> tuple[Path, Any]:
    from assistant_gateway.config import GatewayInstanceConfig

    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="old-tables", instance_root=str(root), team_id="team_old", timezone="UTC"
    )
    config_path = root / "instance_config.yaml"
    config.save(config_path)
    _build(Path(config.manager_storage_settings.sqlite_path), MANAGER_TABLES, MANAGER_ROWS)
    _build(
        config.credential_catalog_database_path,
        {
            "credential_capability_bindings": "CREATE TABLE credential_capability_bindings (binding_id TEXT)",
            "credential_records": "CREATE TABLE credential_records (credential_key TEXT)",
        },
        {"credential_capability_bindings": ["b_1"]},
    )
    return config_path, config


def _run(config_path: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> str:
    import assistant_cli

    args = assistant_cli.build_parser().parse_args(
        ["access", "drop-old-tables", "--config", str(config_path), *extra]
    )
    assistant_cli._access_command(args)
    return capsys.readouterr().out


def test_the_cli_lists_old_tables_and_changes_nothing_without_yes(instance, capsys) -> None:
    config_path, config = instance
    manager_db = Path(config.manager_storage_settings.sqlite_path)
    gateway_db = Path(config.state_storage_settings.sqlite_path)
    gateway_existed = gateway_db.exists()

    out = _run(config_path, capsys)

    assert "manager database" in out
    assert "  permission_requests: 2 row(s)" in out
    assert "  permission_grants: 1 row(s)" in out
    assert "  human_interactions: 1 row(s)" in out
    assert "  access_workflows: 0 row(s)" in out
    assert "  schedule_access_preparations: 0 row(s)" in out
    assert "access_workflows_v2" not in out and "access_grants" not in out
    assert "credential catalog" in out and "  credential_capability_bindings: 1 row(s)" in out
    assert "Nothing was changed. Run again with --yes" in out
    assert set(MANAGER_TABLES) <= _tables(manager_db)
    # Checking never creates a database that was not there.
    assert gateway_db.exists() == gateway_existed


def test_the_cli_drops_only_old_tables_with_yes_and_is_idempotent(instance, capsys) -> None:
    config_path, config = instance
    manager_db = Path(config.manager_storage_settings.sqlite_path)

    out = _run(config_path, capsys, "--yes")

    assert "  Dropped 5 table(s)." in out and "  Dropped 1 table(s)." in out
    assert _tables(manager_db) == KEPT
    conn = sqlite3.connect(manager_db)
    try:
        assert conn.execute("SELECT workflow_id FROM access_workflows_v2").fetchall() == [("wf_new",)]
    finally:
        conn.close()
    assert _tables(config.credential_catalog_database_path) == {"credential_records"}

    again = _run(config_path, capsys, "--yes")
    assert "Dropped" not in again and again.count("no old tables.") >= 2


def test_drop_refuses_a_table_that_is_not_an_old_one(tmp_path: Path) -> None:
    path = tmp_path / "catalog.db"
    _build(path, {"credential_records": "CREATE TABLE credential_records (k TEXT)"}, {})
    database = sqlite_database("credential catalog", path, OLD_CREDENTIAL_CATALOG_TABLES)

    with pytest.raises(ValueError, match="Not an old access table here: credential_records"):
        drop_old_tables(database, [OldTable("credential_records", 0)])
    assert _tables(path) == {"credential_records"}


def test_a_missing_sqlite_file_is_reported_empty_and_not_created(tmp_path: Path) -> None:
    path = tmp_path / "absent.db"

    assert find_old_tables(sqlite_database("credential catalog", path, OLD_CREDENTIAL_CATALOG_TABLES)) == []
    assert not path.exists()


class _RecordingPostgres:
    """Stands in for a PostgreSQL connection and records every statement."""

    def __init__(self, existing: set[str], statements: list[str]) -> None:
        self.existing = existing
        self.statements = statements

    def __enter__(self) -> "_RecordingPostgres":
        return self

    def __exit__(self, *exc: object) -> None:
        self.statements.append("-- closed")

    def begin_write(self) -> None:
        self.statements.append("-- begin")

    def commit(self) -> None:
        self.statements.append("-- commit")

    def execute(self, query: str, params: object = None) -> "_RecordingPostgres":
        self.statements.append(" ".join(query.split()))
        self._last = query
        return self

    def fetchall(self) -> list[dict[str, str]]:
        return [{"table_name": name} for name in sorted(self.existing)]

    def fetchone(self) -> dict[str, int]:
        return {"row_count": 3}


def test_postgres_lists_the_current_schema_and_drops_children_first(monkeypatch) -> None:
    statements: list[str] = []
    existing = {"permission_requests", "permission_grants", "human_interactions", "access_grants"}
    monkeypatch.setattr(
        "storage.old_access_tables.connect_postgres", lambda dsn: _RecordingPostgres(existing, statements)
    )
    settings = resolve_storage_settings({"backend": "postgres", "postgres": {"dsn": "postgresql://ua:secret@db/ua"}})
    database = OldTableDatabase("manager database", settings, OLD_STATE_TABLES)

    found = find_old_tables(database)
    assert [table.name for table in found] == ["human_interactions", "permission_grants", "permission_requests"]
    assert "table_schema = current_schema()" in statements[0]
    assert "secret" not in database.location

    statements.clear()
    assert drop_old_tables(database, found) == ["human_interactions", "permission_grants", "permission_requests"]
    assert statements == [
        "-- begin",
        'DROP TABLE IF EXISTS "human_interactions"',
        'DROP TABLE IF EXISTS "permission_grants"',
        'DROP TABLE IF EXISTS "permission_requests"',
        "-- commit",
        "-- closed",
    ]


@pytest.mark.live
@pytest.mark.postgres
def test_live_postgres_drops_old_tables_and_keeps_current_ones() -> None:
    dsn = str(os.getenv("LIVE_TEST_POSTGRES_DSN", "")).strip()
    if not dsn:
        pytest.skip("Set LIVE_TEST_POSTGRES_DSN to a disposable database to run this test.")
    from storage.postgres_connection import connect_postgres

    settings = resolve_storage_settings({"backend": "postgres", "postgres": {"dsn": dsn}})
    probe = "ua_drop_old_tables_probe"  # a current-looking table that must survive
    old_tables = {name: ddl for name, ddl in MANAGER_TABLES.items() if name not in KEPT}
    with connect_postgres(dsn) as conn:
        for ddl in old_tables.values():
            conn.execute(ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{probe}" (probe_id TEXT)')
        conn.execute("INSERT INTO permission_requests VALUES ('req_live') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO permission_grants VALUES ('grant_live', 'req_live') ON CONFLICT DO NOTHING")
    database = OldTableDatabase("manager database", settings, OLD_STATE_TABLES)
    try:
        found = {table.name: table.rows for table in find_old_tables(database)}
        assert set(old_tables) <= set(found)
        assert found["permission_requests"] >= 1 and found["permission_grants"] >= 1
        drop_old_tables(database, find_old_tables(database))
        assert find_old_tables(database) == []
        with connect_postgres(dsn) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            ).fetchall()
        assert probe in {row["table_name"] for row in rows}
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{probe}"')
