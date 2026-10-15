import logging
from pathlib import Path


from tests.postgres_test_helpers import patch_fake_postgres
from storage.agent_state_repository import create_agent_state_repository
from storage.config import resolve_storage_settings


def test_create_agent_state_repository_accepts_postgres_when_driver_is_available(tmp_path: Path, monkeypatch) -> None:
    storage_settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    patch_fake_postgres(monkeypatch)

    repo = create_agent_state_repository(storage_settings, logger=logging.getLogger("test"))
    assert repo is not None
