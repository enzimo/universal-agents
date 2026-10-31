"""Shared fixtures for knowledge base tests: real SQLite, and real PostgreSQL when configured.

PostgreSQL runs need ``LIVE_TEST_POSTGRES_DSN``; each test gets its own schema,
dropped afterwards (the pattern of ``tests/test_schedule_worker_transactions.py``).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest

from knowledge_base.records import KnowledgeActor
from knowledge_base.vocabulary import ActorKind
from storage.config import StorageSettings, resolve_storage_settings

BACKENDS = [
    "sqlite",
    pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres, pytest.mark.integration]),
]
TEAM = "team_kb"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

# The change ledger table as 0.9 created it, before entries named the fields
# they set. An instance upgraded in place from 0.9 still has it.
LEDGER_TABLE_0_9 = """
    CREATE TABLE change_ledger (
        team_id TEXT NOT NULL,
        position {integer} NOT NULL,
        record_kind TEXT NOT NULL,
        record_key TEXT NOT NULL,
        change_type TEXT NOT NULL,
        writer_kind TEXT NOT NULL,
        request_id TEXT NOT NULL DEFAULT '',
        person_id TEXT NOT NULL DEFAULT '',
        version TEXT NOT NULL DEFAULT '',
        checked INTEGER NOT NULL DEFAULT 1,
        changed_at_utc TEXT NOT NULL,
        PRIMARY KEY (team_id, position)
    )
"""


@contextmanager
def knowledge_storage(backend: str, tmp_path: Path) -> Iterator[StorageSettings]:
    if backend == "sqlite":
        yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "knowledge.db")}})
        return
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL knowledge tests")
    schema = "knowledge_test_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.commit()
    try:
        yield resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()


def agent(
    actor_id: str = "agent_1",
    *,
    person: str = "",
    agent_type: str = "Researcher",
    work_id: str = "",
    task_id: str = "task_1",
) -> KnowledgeActor:
    return KnowledgeActor(
        kind=ActorKind.AGENT,
        actor_id=actor_id,
        team_id=TEAM,
        agent_type=agent_type,
        work_person_id=person,
        task_id=task_id,
        work_id=work_id or (f"ctx_{person}" if person else ""),
    )


def person(person_id: str) -> KnowledgeActor:
    return KnowledgeActor(kind=ActorKind.PERSON, actor_id=person_id, team_id=TEAM, work_person_id=person_id)
