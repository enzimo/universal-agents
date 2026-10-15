"""Offline config repair preserves retained settings and original config bytes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.retire_memory_plugins import RETIRED_PLUGIN_IDS, retire_memory_plugins
from runtime_settings.memory import validate_memory_plugin_config


@pytest.fixture
def config_file(tmp_path):
    config = {
        "agent": {
            "memory": {
                "plugin_manager": {"enabled": True},
                "plugins": {
                    "order": ["coremem", *sorted(RETIRED_PLUGIN_IDS), "usermem"],
                    "coremem": {"enabled": True},
                    "usermem": {"enabled": False},
                    **{name: {"enabled": False} for name in RETIRED_PLUGIN_IDS},
                },
            }
        },
        "gateway": {"team_id": "team_ua"},
        "unrelated": "keep this",
    }
    path = tmp_path / "instance_config.yaml"
    path.write_text("# Original comment retained in backup\n" + yaml.safe_dump(config))
    return path


def test_retirement_backs_up_exact_bytes_and_preserves_supported_settings(config_file):
    original = config_file.read_bytes()
    before = yaml.safe_load(original)
    with pytest.raises(ValueError, match="Unknown memory plugin"):
        validate_memory_plugin_config(before["agent"]["memory"])
    removed, backup = retire_memory_plugins(config_file, apply=True)
    assert set(removed) == RETIRED_PLUGIN_IDS
    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == 0o600
    after = yaml.safe_load(config_file.read_bytes())
    validate_memory_plugin_config(after["agent"]["memory"])
    expected = before
    plugins = expected["agent"]["memory"]["plugins"]
    for name in RETIRED_PLUGIN_IDS:
        plugins.pop(name)
    plugins["order"] = ["coremem", "usermem"]
    assert after == expected
    assert retire_memory_plugins(config_file, apply=True) == ([], None)


def test_preview_does_not_change_config_or_create_backup(config_file):
    original = config_file.read_bytes()
    removed, backup = retire_memory_plugins(config_file, apply=False)
    assert set(removed) == RETIRED_PLUGIN_IDS
    assert backup is None
    assert config_file.read_bytes() == original
    assert list(config_file.parent.iterdir()) == [config_file]


def test_failed_replace_keeps_original_and_backup(config_file, monkeypatch):
    original = config_file.read_bytes()

    def fail(*args):
        raise OSError("Injected replacement failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="Injected"):
        retire_memory_plugins(config_file, apply=True)
    assert config_file.read_bytes() == original
    backups = list(config_file.parent.glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert len(list(config_file.parent.iterdir())) == 2


def test_invalid_order_is_not_rewritten(config_file):
    config = yaml.safe_load(config_file.read_bytes())
    config["agent"]["memory"]["plugins"]["order"] = "coremem"
    config_file.write_text(yaml.safe_dump(config))
    original = config_file.read_bytes()
    with pytest.raises(ValueError, match="Expected"):
        retire_memory_plugins(config_file, apply=True)
    assert config_file.read_bytes() == original


def test_script_runs_directly_without_runtime_imports(config_file):
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "retire_memory_plugins.py"
    )
    result = subprocess.run(
        [sys.executable, str(script), "--config", str(config_file), "--yes"],
        cwd=config_file.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Removed retired plugin references" in result.stderr
    validate_memory_plugin_config(
        yaml.safe_load(config_file.read_bytes())["agent"]["memory"]
    )
