"""Stores set up their tables on new and older databases, also when several processes open one at once.

Every store creates its tables and indexes and adds the columns an earlier
release's tables lack when it opens. The launcher starts the manager, the
startup agents and the gateway together, so on PostgreSQL several processes
open one store at the same moment; ``CREATE ... IF NOT EXISTS`` statements
running together then fail with a duplicate key in the system catalog or a
deadlock, and the launcher stops the runtime. Each store's setup takes its
setup lock first (``storage.schema_setup``), so the openers take turns.

The concurrent tests open a store from several threads, each with its own
connection: the race is in the server, so threads reproduce it as separate
processes do. Without the setup lock every round of them fails.
"""

from __future__ import annotations

import ast
import logging
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest

# storage.adapter_host_repository cannot be the first of the two to load (they import each other).
import assistant_gateway.adapter_host  # noqa: F401
from storage.access_repository import AccessRepository
from storage.access_workflow_repository import AccessWorkflowRepository
from storage.activity_collector_repository import create_activity_collector_repository
from storage.adapter_host_repository import create_adapter_host_repository
from storage.after_action_report_repository import create_after_action_report_repository
from storage.agent_launch_repository import AgentLaunchRepository
from storage.agent_state_repository import create_agent_state_repository
from storage.change_ledger import ChangeRequestStore
from storage.config import StorageSettings, resolve_storage_settings
from storage.conversation_repository import create_conversation_repository
from storage.delegated_task_repository import DelegatedTaskRepository
from storage.gateway_state_repository import create_gateway_state_repository
from storage.heartbeat_repository import create_heartbeat_repository
from storage.history_cleanup_repository import HistoryCleanupRepository
from storage.knowledge_repository import create_knowledge_repository
from storage.memory_graph_repository import create_memory_graph_repository
from storage.memory_review_repository import create_memory_review_repository
from storage.objective_repository import create_objective_repository
from storage.objective_work_repository import create_objective_work_repository
from storage.person_departure_repository import PersonDepartureRepository
from storage.person_storage_usage_repository import PersonStorageUsageRepository
from storage.pinned_memory_repository import create_pinned_memory_repository
from storage.plan_repository import create_plan_repository
from storage.postgres_connection import connect_postgres
from storage.runtime_state_repository import create_runtime_state_repository
from storage.schedule_repository import create_schedule_repository
from storage.schedule_worker_repository import ScheduleWorkerRepository
from storage.scheduled_run_problems import ScheduledRunProblems
from storage.solution_variation_repository import create_solution_variation_repository
from storage.sqlite_connection import connect_sqlite
from storage.stuck_work_repository import StuckWorkRepository
from storage.table_columns import table_column_names
from storage.task_archive_repository import create_task_archive_repository
from storage.tool_setup_run_repository import ToolSetupRunRepository
from storage.worker_handback_repository import WorkerHandbackRepository
from storage.worker_pool_repository import WorkerPoolRepository

REPO = Path(__file__).resolve().parents[1]
ADDED = "event=storage_column_added"
OPENERS = 4

# Every store that sets up tables when it opens, on both backends.
STORES: dict[str, Callable[[StorageSettings, Path], object]] = {
    "access": lambda settings, _tmp: AccessRepository(settings),
    "access_workflow": lambda settings, _tmp: AccessWorkflowRepository(settings),
    "activity_collector": lambda settings, _tmp: create_activity_collector_repository(settings),
    "adapter_host": lambda settings, _tmp: create_adapter_host_repository(settings),
    "after_action_report": lambda settings, _tmp: create_after_action_report_repository(settings),
    "agent_launch": lambda settings, _tmp: AgentLaunchRepository(settings),
    "agent_state": lambda settings, _tmp: create_agent_state_repository(settings),
    "change_ledger": lambda settings, _tmp: ChangeRequestStore(settings),
    "conversation": lambda settings, tmp: create_conversation_repository(settings, artifacts_dir=tmp / "artifacts"),
    "delegated_task": lambda settings, _tmp: DelegatedTaskRepository(settings),
    "gateway_state": lambda settings, _tmp: create_gateway_state_repository(settings),
    "heartbeat": lambda settings, _tmp: create_heartbeat_repository(settings),
    "history_cleanup": lambda settings, _tmp: HistoryCleanupRepository(settings),
    "knowledge": lambda settings, _tmp: create_knowledge_repository(settings),
    "memory_graph": lambda settings, _tmp: create_memory_graph_repository(settings),
    "memory_review": lambda settings, _tmp: create_memory_review_repository(settings),
    "objective": lambda settings, _tmp: create_objective_repository(settings),
    "objective_work": lambda settings, _tmp: create_objective_work_repository(settings),
    "person_departure": lambda settings, _tmp: PersonDepartureRepository(settings),
    "person_storage_usage": lambda settings, _tmp: PersonStorageUsageRepository(settings),
    "pinned_memory": lambda settings, _tmp: create_pinned_memory_repository(settings),
    "plan": lambda settings, _tmp: create_plan_repository(settings),
    "runtime_state": lambda settings, _tmp: create_runtime_state_repository(settings),
    "schedule": lambda settings, _tmp: create_schedule_repository(settings),
    "schedule_worker": lambda settings, _tmp: ScheduleWorkerRepository(settings),
    "scheduled_run_problems": lambda settings, _tmp: ScheduledRunProblems(settings),
    "solution_variation": lambda settings, _tmp: create_solution_variation_repository(settings),
    "stuck_work": lambda settings, _tmp: StuckWorkRepository(settings),
    "task_archive": lambda settings, _tmp: create_task_archive_repository(settings),
    "tool_setup_run": lambda settings, _tmp: ToolSetupRunRepository(settings),
    "worker_handback": lambda settings, _tmp: WorkerHandbackRepository(settings),
    "worker_pool": lambda settings, _tmp: WorkerPoolRepository(settings),
}

# Code that creates tables without the setup lock, and why that is safe.
SETUP_LOCK_EXEMPT = {
    # Its archive table is created inside the manager's owner reconciliation,
    # after that transaction has locked rows: a setup lock there could
    # deadlock two reconciliations, and only the manager runs them.
    "storage/durable_owner_scope_repository.py",
    # The knowledge store's setup first takes the store's write lock
    # (KNOWLEDGE_WRITE_LOCK_KEY), which every opener waits for.
    "storage/knowledge_repository.py",
    # SQLite only.
    "storage/execution_control_repository.py",
    "secure_tool_broker/credential_catalog_store.py",
    "assistant_gateway/adapter_host/client_state.py",
}


def test_every_schema_setup_takes_the_setup_lock():
    """Code that creates tables or indexes takes ``begin_schema_setup`` first (or is exempt above, with why)."""

    missing = []
    for path in sorted(REPO.glob("**/*.py")):
        relative = path.relative_to(REPO).as_posix()
        if relative.startswith(("tests/", "target/")) or "/." in f"/{relative}" or "node_modules/" in relative:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if relative in SETUP_LOCK_EXEMPT or not _creates_schema(tree):
            continue
        if not any(isinstance(node, ast.Call) and getattr(node.func, "id", None) == "begin_schema_setup" for node in ast.walk(tree)):
            missing.append(relative)
    assert missing == []


def _creates_schema(tree: ast.Module) -> bool:
    """Whether a statement in the module's SQL (string constants other than docstrings) creates a table or index."""

    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and any(f"CREATE {kind} IF NOT EXISTS" in node.value for kind in ("TABLE", "INDEX", "UNIQUE INDEX"))
        for node in ast.walk(tree)
    )


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])])
def storage_settings(request, tmp_path) -> Iterator[StorageSettings]:
    if request.param == "sqlite":
        yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "stores.db")}})
        return
    yield from _postgres_schema()


@pytest.fixture
def postgres_settings() -> Iterator[StorageSettings]:
    yield from _postgres_schema()


def _postgres_schema() -> Iterator[StorageSettings]:
    from psycopg.conninfo import make_conninfo

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN required")
    schema = "store_setup_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_a_new_database_has_every_column_without_adding_any(storage_settings, tmp_path, caplog):
    """``CREATE TABLE`` carries every column, so a new install logs no upgrade it never had."""

    with caplog.at_level(logging.INFO):
        for open_store in STORES.values():
            open_store(storage_settings, tmp_path)

    assert [record.getMessage() for record in caplog.records if ADDED in record.getMessage()] == []


def test_a_heartbeat_table_from_before_its_source_columns_gets_them_and_keeps_its_items(storage_settings, tmp_path):
    """The columns come before the indexes that name them; an older table failed every open."""

    create_heartbeat_repository(storage_settings)
    _make_older(storage_settings, {"heartbeat_items": ["source_key", "objective_id"]})
    bind = "%s" if storage_settings.backend == "postgres" else "?"
    now = "2026-01-05T09:00:00+00:00"
    with _connect(storage_settings) as conn:
        conn.execute(
            f"INSERT INTO heartbeat_sources (source_id, title, created_at, updated_at) VALUES ({bind}, {bind}, {bind}, {bind})",
            ("source-1", "Inbox", now, now),
        )
        conn.execute(
            "INSERT INTO heartbeat_items (heartbeat_item_id, source_id, title, instruction, conversation_id, created_at, updated_at) "
            f"VALUES ({bind}, {bind}, {bind}, {bind}, {bind}, {bind}, {bind})",
            ("item-1", "source-1", "Check the inbox", "Summarize new mail", "conversation-1", now, now),
        )
        conn.commit()

    item = create_heartbeat_repository(storage_settings).get_item("item-1")

    assert item is not None
    assert (item.title, item.source_key, item.objective_id) == ("Check the inbox", None, None)


@pytest.mark.live
@pytest.mark.postgres
@pytest.mark.parametrize("store", sorted(STORES))
def test_processes_opening_a_store_at_once_all_start(store, postgres_settings, tmp_path):
    """On a new database, then after an upgrade that adds indexes (every plain index dropped)."""

    assert _open_at_once(store, postgres_settings, tmp_path) == []

    _make_older(postgres_settings, {})
    assert _open_at_once(store, postgres_settings, tmp_path) == []


@pytest.mark.live
@pytest.mark.postgres
def test_processes_upgrading_older_memory_graph_tables_at_once_all_start_and_log_each_column_once(
    postgres_settings, tmp_path, caplog
):
    """Indexes were created before the columns were added, and two openers deadlocked on the tables.

    Openers that both found a column missing also both logged adding it.
    """

    create_memory_graph_repository(postgres_settings)
    older = {table: ["embedding_profile_id"] for table in ("case_memories", "memory_entities", "memory_claims")}
    _make_older(postgres_settings, older)

    with caplog.at_level(logging.INFO):
        assert _open_at_once("memory_graph", postgres_settings, tmp_path) == []

    added = sorted(record.getMessage().rsplit("table=", 1)[1] for record in caplog.records if ADDED in record.getMessage())
    assert added == [f"{table} column=embedding_profile_id" for table in sorted(older)]
    with _connect(postgres_settings) as conn:
        for table in older:
            assert "embedding_profile_id" in table_column_names(conn, table)


def _open_at_once(store: str, settings: StorageSettings, tmp_path: Path) -> list[str]:
    start = threading.Barrier(OPENERS)
    errors: list[str] = []

    def open_store() -> None:
        start.wait(timeout=30)
        try:
            STORES[store](settings, tmp_path)
        except Exception as exc:  # Reported by the assertion with its cause.
            errors.append(f"{type(exc).__name__}: {exc}")

    openers = [threading.Thread(target=open_store) for _ in range(OPENERS)]
    for opener in openers:
        opener.start()
    for opener in openers:
        opener.join(timeout=120)
    return errors


def _make_older(settings: StorageSettings, missing_columns: dict[str, list[str]]) -> None:
    """Make the database look like an earlier release's: no plain indexes, and without the named columns.

    Indexes behind a primary key or unique constraint stay, as every release had them.
    """

    with _connect(settings) as conn:
        if settings.backend == "postgres":
            indexes = conn.execute(
                """
                SELECT index_class.relname
                FROM pg_index entry
                JOIN pg_class index_class ON index_class.oid = entry.indexrelid
                WHERE index_class.relnamespace = current_schema()::regnamespace
                  AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = entry.indexrelid)
                """
            ).fetchall()
        else:
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            ).fetchall()
        for row in indexes:
            conn.execute(f"DROP INDEX {row[0]}")
        for table, columns in missing_columns.items():
            for column in columns:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        conn.commit()


def _connect(settings: StorageSettings):
    if settings.backend == "postgres":
        return connect_postgres(settings.postgres_dsn or "")
    return connect_sqlite(str(settings.sqlite_path))
