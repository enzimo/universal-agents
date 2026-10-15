import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.postgres_test_helpers import patch_fake_postgres
from storage.config import resolve_storage_settings
from storage.schedule_repository import SqliteScheduleRepository, create_schedule_repository


def test_schedule_repository_claims_due_row_once_and_releases(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_schedule_repository(settings, logger=logging.getLogger("test"))
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    repo.add_schedule_row(
        schedule_id="sched_claim",
        payload_json="{}",
        schedule_json=None,
        schedule_name="claim",
        objective_id=None,
        interval_ms=1000,
        cron_expression=None,
        repeat_count=2,
        remaining_count=2,
        next_fire_at=(now - timedelta(seconds=1)).isoformat(),
        paused_until=None,
        created_at=now_iso,
        enabled=True,
        claim_team_id="test_team",
        claim_named_agent_type="TeamArchitect",
    )

    claimed = repo.claim_next_due(
        now_iso=now_iso,
        claim_expires_at=(now + timedelta(minutes=2)).isoformat(),
        claimer_agent_id="agent_a",
        claimer_named_agent_type="TeamArchitect",
        claimer_team_id="test_team",
        claim_token="claim_a",
    )
    assert claimed is not None
    assert claimed["schedule_id"] == "sched_claim"
    assert claimed["claimed_by_agent_id"] == "agent_a"
    assert claimed["claim_token"] == "claim_a"

    second_claim = repo.claim_next_due(
        now_iso=now_iso,
        claim_expires_at=(now + timedelta(minutes=2)).isoformat(),
        claimer_agent_id="agent_b",
        claimer_named_agent_type="TeamArchitect",
        claimer_team_id="test_team",
        claim_token="claim_b",
    )
    assert second_claim is None

    assert repo.release_claim(
        schedule_id="sched_claim",
        claim_token="wrong",
        claimer_agent_id="agent_a",
    ) is False
    assert repo.release_claim(
        schedule_id="sched_claim",
        claim_token="claim_a",
        claimer_agent_id="agent_a",
    ) is True

    reclaimed = repo.claim_next_due(
        now_iso=now_iso,
        claim_expires_at=(now + timedelta(minutes=2)).isoformat(),
        claimer_agent_id="agent_b",
        claimer_named_agent_type="TeamArchitect",
        claimer_team_id="test_team",
        claim_token="claim_b",
    )
    assert reclaimed is not None
    assert reclaimed["claimed_by_agent_id"] == "agent_b"


def test_schedule_repository_tolerates_concurrent_duplicate_column_migration() -> None:
    class _Rows:
        def __init__(self, rows: list[tuple[int, str]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[int, str]]:
            return self._rows

    class _RaceConn:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str):
            self.statements.append(statement)
            if statement.startswith("PRAGMA table_info"):
                columns = [
                    "objective_id",
                    "claim_team_id",
                    "claim_named_agent_type",
                    "claim_agent_id",
                    "claimed_by_agent_id",
                    "claim_token",
                    "claimed_at",
                ]
                return _Rows([(index, column_name) for index, column_name in enumerate(columns)])
            if "ADD COLUMN claim_expires_at" in statement:
                raise sqlite3.OperationalError("duplicate column name: claim_expires_at")
            return _Rows([])

    repo = SqliteScheduleRepository.__new__(SqliteScheduleRepository)
    repo._logger = logging.getLogger("test")
    conn = _RaceConn()

    repo._ensure_schedule_columns(conn, "SQLite")

    assert any("ADD COLUMN claim_expires_at" in statement for statement in conn.statements)
    assert any("ADD COLUMN expires_at_utc" in statement for statement in conn.statements)


def test_schedule_repository_accepts_postgres_when_driver_is_available(tmp_path: Path, monkeypatch) -> None:
    settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    patch_fake_postgres(monkeypatch)

    repo = create_schedule_repository(settings, logger=logging.getLogger("test"))
    assert repo is not None

    created = repo.add_schedule_row_if_absent(
        schedule_id="sched_postgres_ensure",
        payload_json="{}",
        schedule_json=None,
        schedule_name="postgres ensure",
        objective_id=None,
        interval_ms=1000,
        cron_expression=None,
        repeat_count=None,
        remaining_count=None,
        next_fire_at=datetime.now(timezone.utc).isoformat(),
        paused_until=None,
        created_at=datetime.now(timezone.utc).isoformat(),
        enabled=True,
    )
    assert created is True
