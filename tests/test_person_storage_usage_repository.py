"""The shared per-person stored-data totals (storage.person_storage_usage_repository).

The gateway and the manager each write their own side; the gateway's row also
keeps the step announced. Real databases: SQLite always, and PostgreSQL when
``LIVE_TEST_POSTGRES_DSN`` is set (each test gets its own schema).

Covered: a side replaces its rows and people who left lose theirs; the two
sides never touch each other's rows; only a side's own kinds are accepted, and
no negative size; sizes beyond PostgreSQL's 32-bit INTEGER are kept; the
announced step stays while the person stays and goes with them; the schema
version is checked by the database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from storage.person_storage_usage_repository import (
    PERSON_STORAGE_USAGE_TABLE,
    Announcement,
    PersonStorageUsageRepository,
    StorageSide,
)
from tests.knowledge_support import BACKENDS, knowledge_storage

TEAM = "team_storage"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
TEN_GB = 10_000_000_000  # above 2**31


@pytest.mark.parametrize("backend", BACKENDS)
def test_each_side_replaces_only_its_own_rows_and_people_who_left_lose_theirs(backend: str, tmp_path: Path) -> None:
    with knowledge_storage(backend, tmp_path) as settings:
        store = PersonStorageUsageRepository(settings)
        store.record_side(TEAM, StorageSide.GATEWAY, {"usr_a": {"chats": 10}, "usr_b": {"chats": 1, "uploads": 2}}, measured_at=NOW)
        store.record_side(TEAM, StorageSide.MANAGER, {"usr_a": {"folder": TEN_GB, "tasks": 5}}, measured_at=NOW)
        store.record_side("team_other", StorageSide.GATEWAY, {"usr_a": {"chats": 99}}, measured_at=NOW)
        later = NOW + timedelta(days=1)
        store.record_side(TEAM, StorageSide.GATEWAY, {"usr_a": {"chats": 20}}, measured_at=later)  # usr_b left

        gateway = store.side_usage(TEAM, StorageSide.GATEWAY)
        manager = store.side_usage(TEAM, StorageSide.MANAGER)
        other = store.side_usage("team_other", StorageSide.GATEWAY)

    assert set(gateway) == {"usr_a"}
    assert gateway["usr_a"].bytes_by_kind == {"chats": 20, "uploads": 0} and gateway["usr_a"].measured_at == later
    assert manager["usr_a"].bytes_by_kind == {"tasks": 5, "conversation_files": 0, "folder": TEN_GB}
    assert manager["usr_a"].total == TEN_GB + 5 and manager["usr_a"].measured_at == NOW
    assert other["usr_a"].total == 99


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_announced_step_stays_with_the_person_and_goes_when_they_leave(backend: str, tmp_path: Path) -> None:
    with knowledge_storage(backend, tmp_path) as settings:
        store = PersonStorageUsageRepository(settings)
        assert store.record_announcement(TEAM, "usr_a", Announcement(TEN_GB, 1)) is False  # no gateway row yet
        store.record_side(TEAM, StorageSide.GATEWAY, {"usr_a": {"chats": 1}}, measured_at=NOW)
        assert store.record_announcement(TEAM, "usr_a", Announcement(TEN_GB, 3)) is True
        store.record_side(TEAM, StorageSide.GATEWAY, {"usr_a": {"chats": 2}}, measured_at=NOW)
        kept = store.announcement(TEAM, "usr_a")
        store.record_side(TEAM, StorageSide.GATEWAY, {}, measured_at=NOW)  # the person left
        gone = store.announcement(TEAM, "usr_a")

    assert kept == Announcement(TEN_GB, 3)
    assert gone == Announcement(0, 0)


def test_a_side_records_only_its_own_kinds_and_no_negative_size(tmp_path: Path) -> None:
    from storage.config import resolve_storage_settings

    store = PersonStorageUsageRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    )

    with pytest.raises(ValueError, match="does not measure"):
        store.record_side(TEAM, StorageSide.GATEWAY, {"usr_a": {"folder": 1}}, measured_at=NOW)
    with pytest.raises(ValueError, match="negative"):
        store.record_side(TEAM, StorageSide.MANAGER, {"usr_a": {"folder": -1}}, measured_at=NOW)
    with store._connect() as conn, pytest.raises(Exception, match="CHECK constraint"):
        conn.execute(
            f"INSERT INTO {PERSON_STORAGE_USAGE_TABLE} (schema_version, team_id, person_id, side, bytes_json, "
            "total_bytes, measured_at) VALUES (2, 't', 'p', 'gateway', '{}', 0, 'now')"
        )
    assert store.side_usage(TEAM, StorageSide.GATEWAY) == {}
