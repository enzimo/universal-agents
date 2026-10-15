import logging
from pathlib import Path


from tests.postgres_test_helpers import patch_fake_postgres
from storage.config import resolve_storage_settings
from storage.heartbeat_repository import create_heartbeat_repository


def test_heartbeat_repository_roundtrips_source_and_items(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_heartbeat_repository(settings, logger=logging.getLogger("test"))

    repo.upsert_source(
        source_id="hb_source_test",
        title="Heartbeat Test",
        enabled=True,
        created_at="2026-04-05T00:00:00+00:00",
        updated_at="2026-04-05T00:00:00+00:00",
    )
    repo.add_item(
        heartbeat_item_id="heartbeat_item_test",
        source_id="hb_source_test",
        title="Alerts",
        instruction="Check alerts",
        conversation_id="conversation_id_test",
        target_agent_type=None,
        objective_id="objective_id_alerts",
        enabled=True,
        created_at="2026-04-05T00:00:00+00:00",
        updated_at="2026-04-05T00:00:00+00:00",
    )

    source = repo.get_source("hb_source_test")
    items = repo.list_items("hb_source_test")

    assert source is not None
    assert source.title == "Heartbeat Test"
    assert len(items) == 1
    assert items[0].instruction == "Check alerts"
    assert items[0].objective_id == "objective_id_alerts"

    # An item that continues in a new conversation (heartbeat_store.item_conversation_for_run).
    assert repo.set_item_conversation("heartbeat_item_test", "conversation_id_new", updated_at="2026-04-06T00:00:00+00:00")
    assert repo.get_item("heartbeat_item_test").conversation_id == "conversation_id_new"
    assert not repo.set_item_conversation("heartbeat_item_missing", "conversation_id_x", updated_at="2026-04-06T00:00:00+00:00")


def test_heartbeat_repository_accepts_postgres_when_driver_is_available(tmp_path: Path, monkeypatch) -> None:
    settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    patch_fake_postgres(monkeypatch)

    repo = create_heartbeat_repository(settings, logger=logging.getLogger("test"))
    assert repo is not None
