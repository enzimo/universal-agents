from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant_cli import _memory_graph_embedding_reset
from assistant_gateway.config import GatewayInstanceConfig
from storage.memory_graph_repository import create_memory_graph_repository


def _make_config(tmp_path: Path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="memorygraphcli",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_storage_sqlite_path="runtime/task_history.db",
    )
    config.save(config.config_path)
    return config


def test_memory_graph_embedding_reset_archive_archives_profile_and_clears_rows(tmp_path, capsys) -> None:
    config = _make_config(tmp_path)
    settings = config.resolve_manager_storage_settings(env={})
    repo = create_memory_graph_repository(settings)
    repo.activate_embedding_profile(
        provider="local",
        model_id=None,
        api_base=None,
        dimensions=48,
    )
    repo.ensure_entity(
        subject_id="subject_1",
        entity_type="concept",
        canonical_name="Postgres",
        embedding=json.dumps([1.0] + [0.0] * 47),
    )

    _memory_graph_embedding_reset(str(config.config_path), env_path=None, archive=True, purge=False)
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "archive"
    assert payload["cleared_rows"]["memory_entities"] == 1
    assert repo.get_active_embedding_profile() is None
