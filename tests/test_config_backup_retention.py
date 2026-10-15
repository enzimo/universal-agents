from __future__ import annotations

import os
from pathlib import Path

import pytest

from config_backup_retention import (
    CONFIG_BACKUP_RETENTION_LIMIT,
    prune_config_backups,
)


def _write_with_mtime(path: Path, *, timestamp_ns: int) -> None:
    path.write_text(path.name, encoding="utf-8")
    os.utime(path, ns=(timestamp_ns, timestamp_ns))


def test_prune_config_backups_keeps_five_newest_across_suffix_types(
    tmp_path: Path,
) -> None:
    target = tmp_path / "LLM_OVERRIDES.yaml"
    target.write_text("default: {}\n", encoding="utf-8")
    backups = [
        tmp_path / "LLM_OVERRIDES.yaml.bak.v1",
        tmp_path / "LLM_OVERRIDES.yaml.bak.runtime-settings.20260801",
        tmp_path / "LLM_OVERRIDES.yaml.bak.20260802",
        tmp_path / "LLM_OVERRIDES.yaml.bak.runtime-settings.20260803",
        tmp_path / "LLM_OVERRIDES.yaml.bak.20260804",
        tmp_path / "LLM_OVERRIDES.yaml.bak.20260805",
        tmp_path / "LLM_OVERRIDES.yaml.bak.20260806",
    ]
    for index, backup in enumerate(backups, start=1):
        _write_with_mtime(backup, timestamp_ns=index * 1_000_000_000)

    unrelated_target_backup = tmp_path / "instance_config.yaml.bak.20260801"
    fixed_single_backup = tmp_path / "LLM_OVERRIDES.yaml.bak"
    prefix_collision = tmp_path / "LLM_OVERRIDES.yaml.bakery.20260801"
    for path in (unrelated_target_backup, fixed_single_backup, prefix_collision):
        _write_with_mtime(path, timestamp_ns=1)

    result = prune_config_backups(target)

    assert CONFIG_BACKUP_RETENTION_LIMIT == 5
    assert {path.name for path in result.retained} == {
        path.name for path in backups[-5:]
    }
    assert {path.name for path in result.deleted} == {
        path.name for path in backups[:2]
    }
    assert result.errors == ()
    assert not backups[0].exists()
    assert not backups[1].exists()
    assert all(path.exists() for path in backups[-5:])
    assert unrelated_target_backup.exists()
    assert fixed_single_backup.exists()
    assert prefix_collision.exists()


def test_prune_config_backups_rejects_non_positive_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be at least 1"):
        prune_config_backups(tmp_path / "config.yaml", retention_limit=0)
