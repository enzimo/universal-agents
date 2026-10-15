"""Upgrading configuration and files to the derived instance layout.

Config upgrades drop location settings that still hold their old default and
refuse customized ones; files are never moved automatically. Startup refuses an
old-layout instance and prints the moves (runtime_settings.legacy_layout).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant_gateway.config import GatewayInstanceConfig, _migrate_instance_to_layout
from config_loader import _migrate_to_instance_layout
from docker.launch_headless import RuntimeLayoutError, validate_runtime_layout


def test_config_yaml_custom_locations_stop_the_upgrade():
    config = {"agent": {"workspace_dir": "."}, "workspace": {"skills_dir": "custom-skills"}}

    with pytest.raises(ValueError, match="workspace.skills_dir=custom-skills"):
        _migrate_to_instance_layout(config)


def test_instance_config_custom_workspace_stops_the_upgrade(tmp_path):
    config = {"instance_root": str(tmp_path), "agent": {"workspace_dir": str(tmp_path / "elsewhere")}}

    with pytest.raises(ValueError, match="not the default"):
        _migrate_instance_to_layout(config)


def test_instance_config_database_inside_the_old_workspace_stops_the_upgrade(tmp_path):
    config = {
        "instance_root": str(tmp_path),
        "state": {"storage": {"sqlite": {"path": str(tmp_path / "workspace" / "custom.db")}}},
    }

    with pytest.raises(ValueError, match="agents could reach"):
        _migrate_instance_to_layout(config)


def _instance(root: Path) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.create(
        instance_name="test", instance_root=str(root), team_id="team_ua", timezone="UTC"
    )


def test_startup_refuses_an_old_layout_and_prints_the_moves(tmp_path):
    root = tmp_path / "data"
    (root / "workspace" / "dbs").mkdir(parents=True)
    (root / "workspace" / "SOUL.md").write_text("soul", encoding="utf-8")
    (root / "workspace" / "dbs" / "task_history.db").write_text("db", encoding="utf-8")

    with pytest.raises(RuntimeLayoutError) as raised:
        validate_runtime_layout(_instance(root))

    message = str(raised.value)
    assert f"mv -n {root / 'workspace' / 'SOUL.md'} {root / 'control' / 'SOUL.md'}" in message
    assert str(root / "dbs" / "task_history.db") in message
    # Nothing is moved automatically.
    assert (root / "workspace" / "SOUL.md").exists()


def test_startup_requires_onboarding_then_creates_the_areas(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    config = _instance(root)

    with pytest.raises(RuntimeLayoutError, match="Run `onboard`"):
        validate_runtime_layout(config)

    config.layout.control_dir.mkdir()
    validate_runtime_layout(config)

    for area in ("state", "dbs", "workspaces/users", "workspaces/team", "workspaces/agents"):
        assert (root / area).is_dir()
