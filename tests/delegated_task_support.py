"""Storage for the delegated-task store's tests: real SQLite, and real PostgreSQL when configured.

PostgreSQL runs need ``LIVE_TEST_POSTGRES_DSN``; each test gets its own schema,
dropped afterwards (the pattern of ``tests/knowledge_support.py``). Its
connections give up on a lock after 20 seconds, so a write that never gets the
store's write lock fails its test instead of holding up the serial PostgreSQL
job.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest

from storage.config import StorageSettings, resolve_storage_settings

BACKENDS = [
    "sqlite",
    pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres, pytest.mark.integration]),
]


@contextmanager
def delegated_task_storage(backend: str, tmp_path: Path) -> Iterator[StorageSettings]:
    if backend == "sqlite":
        yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "delegated.db")}})
        return
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL delegated-task tests")
    schema = "delegated_tasks_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield resolve_storage_settings(
            {
                "backend": "postgres",
                "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema} -clock_timeout=20s")},
            }
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
