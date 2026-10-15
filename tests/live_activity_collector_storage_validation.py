from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from custom_messages.contracts.activity_v1 import TaskActivityEvent
from custom_messages.enums import TaskActivityStage, TaskActivityType
from live_storage_utils import redact_dsn, reset_postgres_storage, resolve_live_storage_settings
from storage.activity_collector_repository import create_activity_collector_repository
from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


RESULT_DIR = Path("test_results/live_activity_collector_validation")
logger = logging.getLogger("live_activity_collector_storage_validation")


def _write_report(report: dict[str, Any]) -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def _create_legacy_activity_table(settings: StorageSettings) -> None:
    if settings.backend == "postgres":
        reset_postgres_storage(settings)
        with connect_postgres(settings.postgres_dsn or "") as conn:
            conn.execute(
                """
                CREATE TABLE activity_events (
                    activity_event_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    team_id TEXT,
                    top_level_task_id TEXT,
                    agent_id TEXT,
                    activity_type TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    user_visible INTEGER NOT NULL,
                    details_json TEXT NOT NULL,
                    full_event_json TEXT NOT NULL
                )
                """
            )
            conn.commit()
        return

    sqlite_path = Path(settings.sqlite_path or RESULT_DIR / "activity_collector.db")
    if sqlite_path.exists():
        sqlite_path.unlink()
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute(
            """
            CREATE TABLE activity_events (
                activity_event_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                team_id TEXT,
                top_level_task_id TEXT,
                agent_id TEXT,
                activity_type TEXT NOT NULL,
                stage TEXT NOT NULL,
                summary TEXT NOT NULL,
                user_visible INTEGER NOT NULL,
                details_json TEXT NOT NULL,
                full_event_json TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _table_columns(settings: StorageSettings) -> set[str]:
    if settings.backend == "postgres":
        with connect_postgres(settings.postgres_dsn or "") as conn:
            rows = conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'activity_events'
                """
            ).fetchall()
        return {str(row[0]) for row in rows}

    with sqlite3.connect(settings.sqlite_path or "") as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(activity_events)").fetchall()}


def run_validation(*, config_path: str | None = None) -> dict[str, Any]:
    settings = resolve_live_storage_settings(
        config_path=config_path,
        sqlite_fallback_path=RESULT_DIR / "activity_collector.db",
        workspace_dir=Path.cwd(),
    )
    _create_legacy_activity_table(settings)
    repository = create_activity_collector_repository(
        settings,
        logger=logging.getLogger("live.activity.collector.repository"),
    )

    event = TaskActivityEvent(
        activity_event_id="evt_live_activity_objective",
        created_at="2026-05-24T00:00:00+00:00",
        sequence=1,
        team_id="team_live_activity",
        top_level_task_id="task_live_activity",
        task_id="task_live_activity",
        objective_id="objective_live_activity",
        objective_title="Live activity objective",
        agent_id="manager_live_activity",
        agent_name="Live Activity Manager",
        named_agent_type="TeamArchitect",
        activity_type=TaskActivityType.OBJECTIVE_UPDATED,
        stage=TaskActivityStage.RUNNING,
        summary="Objective activity persisted after legacy schema upgrade.",
    )
    assert repository.upsert_events([event]) == 1
    rows = repository.list_recent_events(
        team_id="team_live_activity",
        objective_id="objective_live_activity",
    )
    assert [row.activity_event_id for row in rows] == ["evt_live_activity_objective"]

    columns = _table_columns(settings)
    assert "objective_id" in columns
    assert "objective_title" in columns

    return {
        "status": "passed",
        "storage_backend": settings.backend,
        "storage": redact_dsn(settings.postgres_dsn or "") if settings.backend == "postgres" else settings.sqlite_path,
        "objective_id": "objective_live_activity",
        "upgraded_columns": sorted(column for column in columns if column in {"objective_id", "objective_title"}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate activity collector legacy schema upgrade.")
    parser.add_argument("--config", default="config.yaml", help="Config file used to resolve live storage settings.")
    parser.add_argument("--env", default=None, help="Optional env file to load before validation.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if args.env and load_dotenv:
        load_dotenv(args.env, override=True)
    if RESULT_DIR.exists():
        shutil.rmtree(RESULT_DIR)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        report = run_validation(config_path=args.config)
    except Exception as exc:
        report = {"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}
        _write_report(report)
        logger.exception("Live activity collector validation failed.")
        raise
    _write_report(report)
    logger.info("Live activity collector validation passed. Report written to %s", RESULT_DIR / "report.json")


if __name__ == "__main__":
    main()
