"""Helpers shared by the ``assistant_cli memory export|import`` tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from custom_agents.pinned_memory_transfer import run_memory_command
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1, new_person_ref
from storage.access_repository import AccessRepository
from storage.pinned_memory_repository import create_pinned_memory_repository


def new_instance(tmp_path: Path, name: str, team_id: str, memory: dict[str, Any] | None = None) -> tuple[Path, Any]:
    """A saved ``instance_config.yaml`` of this version, as onboarding writes it."""

    from assistant_gateway.config import GatewayInstanceConfig

    root = tmp_path / name
    config = GatewayInstanceConfig.create(instance_name=name, instance_root=str(root), team_id=team_id, timezone="UTC")
    for bank, settings in (memory or {}).items():
        config.agent.memory.setdefault(bank, {}).update(settings)
    path = root / "instance_config.yaml"
    config.save(path)
    return path, config


def zero_nine_instance(tmp_path: Path, name: str, team_id: str) -> Path:
    """A saved ``instance_config.yaml`` shaped like 0.9's (schema 12), not yet upgraded.

    0.9 onboarding wrote every default out, ``transport.presence_max_age_seconds`` included;
    the presence stream it sized was removed in message contract step 7d. 0.9.4's onboarding
    also wrote ``agent.max_concurrent_top_level_tasks: 1``, and later 0.9 releases kept that
    ignored key in saved files; 0.10 removes it (instance config 13 -> 14).
    """

    import yaml

    path, _config = new_instance(tmp_path, name, team_id)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    saved["schema_version"] = 12
    saved["transport"]["presence_max_age_seconds"] = 600.0
    saved["agent"]["max_concurrent_top_level_tasks"] = 1
    path.write_text(yaml.safe_dump(saved, sort_keys=False), encoding="utf-8")
    return path


def record_people(config: Any, team_id: str, standing: dict[str, PersonStanding]) -> None:
    """Write the gateway's copy of the team's people, as a started gateway does."""

    now = datetime.now(timezone.utc)
    AccessRepository(config.manager_storage_settings).replace_people(
        team_id,
        [
            TeamPersonV1(person_ref=new_person_ref(), team_id=team_id, person_id=person, standing=value, changed_at_utc=now)
            for person, value in standing.items()
        ],
        at=now,
    )


def repository(config: Any):
    return create_pinned_memory_repository(config.manager_storage_settings)


def row_count(config: Any, table: str) -> int:
    with sqlite3.connect(config.manager_storage_settings.sqlite_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def run_cli(*argv: str) -> None:
    """Run ``assistant_cli memory ...`` through the real parser."""

    import assistant_cli

    run_memory_command(assistant_cli.build_parser().parse_args(["memory", *argv]))
