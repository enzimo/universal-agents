"""The stored state of ``set_up_tools`` runs on SQLite or PostgreSQL.

One row per run (``custom_agents.tool_setup_runs.ToolSetupRunV1``), in the
manager's database next to the task it pauses, so a run survives restarts
of the agent and of the gateway: setup results and access decisions that
arrive later still find it. A compare-and-swap version fences concurrent
updates (two results arriving together). Rows hold states and ids only,
never credentials or page addresses.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from custom_agents.tool_setup_runs import ToolSetupRunV1
from custom_messages.contracts.access_workflow_v2 import utc_now
from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite

RUN_TABLE = "tool_setup_runs"


class ToolSetupRunConflict(RuntimeError):
    """The run changed since it was read."""


class ToolSetupRunRepository:
    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if not settings.postgres_dsn and not settings.sqlite_path:
            raise ValueError("Tool setup runs need SQLite or PostgreSQL.")
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {RUN_TABLE} (
                run_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )""")
            conn.execute(f"CREATE INDEX IF NOT EXISTS tool_setup_runs_task ON {RUN_TABLE}(team_id, task_id)")
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    def get(self, run_id: str) -> ToolSetupRunV1 | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM {RUN_TABLE} WHERE run_id={self.dialect.bind}", (run_id,)
            ).fetchone()
        return ToolSetupRunV1.model_validate_json(row[0]) if row else None

    def for_task(self, *, team_id: str, task_id: str) -> list[ToolSetupRunV1]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM {RUN_TABLE} WHERE team_id={p} AND task_id={p} ORDER BY updated_at",
                (team_id, task_id),
            ).fetchall()
        return [ToolSetupRunV1.model_validate_json(row[0]) for row in rows]

    def create(self, run: ToolSetupRunV1) -> ToolSetupRunV1:
        """Store a new run; an existing run with the same id is returned unchanged."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                f"INSERT INTO {RUN_TABLE} (run_id,team_id,task_id,status,version,updated_at,payload_json) "
                f"VALUES ({','.join([p] * 7)}) ON CONFLICT DO NOTHING",
                (
                    run.run_id,
                    run.team_id,
                    run.task_id,
                    run.status,
                    run.version,
                    run.updated_at_utc.isoformat(),
                    run.model_dump_json(),
                ),
            )
            row = conn.execute(f"SELECT payload_json FROM {RUN_TABLE} WHERE run_id={p}", (run.run_id,)).fetchone()
            conn.commit()
        return ToolSetupRunV1.model_validate_json(row[0])

    def save(self, run: ToolSetupRunV1) -> ToolSetupRunV1:
        """Store a changed run over the version it was read at; raises ``ToolSetupRunConflict``."""

        changed = run.model_copy(update={"version": run.version + 1, "updated_at_utc": utc_now()})
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            result = conn.execute(
                f"UPDATE {RUN_TABLE} SET status={p},version={p},updated_at={p},payload_json={p} "
                f"WHERE run_id={p} AND version={p}",
                (
                    changed.status,
                    changed.version,
                    changed.updated_at_utc.isoformat(),
                    changed.model_dump_json(),
                    run.run_id,
                    run.version,
                ),
            )
            if result.rowcount != 1:
                raise ToolSetupRunConflict("The tool setup run changed; read it again.")
            conn.commit()
        return changed


__all__ = ["RUN_TABLE", "ToolSetupRunConflict", "ToolSetupRunRepository"]
