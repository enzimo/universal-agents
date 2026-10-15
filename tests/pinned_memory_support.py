"""Helpers shared by the ``assistant_cli memory export|import`` tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from custom_agents.pinned_memory_transfer import run_memory_command
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
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


def record_people(config: Any, team_id: str, standing: dict[str, PersonStanding]) -> None:
    """Write the gateway's copy of the team's people, as a started gateway does."""

    now = datetime.now(timezone.utc)
    AccessRepository(config.manager_storage_settings).replace_people(
        team_id,
        [
            TeamPersonV1(team_id=team_id, person_id=person, standing=value, changed_at_utc=now)
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
