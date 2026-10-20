"""Settings > Team > File Retention's service (assistant_gateway.file_retention).

It reads and writes the real saved ``instance_config.yaml`` of a temporary
instance; only the person check is a stand-in, recording the change kind it
was asked about.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from assistant_gateway.application.file_retention_page import (
    FileRetention,
    FileRetentionConflict,
    FileRetentionInvalid,
    FileRetentionRefused,
)
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.file_retention import GatewayFileRetention
from assistant_gateway.upload_retention import UploadRetention
from custom_agents.runtime_settings_service import RUNTIME_SETTINGS_CHANGE_KIND


class _PersonChanges:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.asked: list[tuple[str | None, str]] = []

    def may_change(self, person_id: str | None, kind: str) -> bool:
        self.asked.append((person_id, kind))
        return self.allowed


class _NoRecords:
    def delete_attachments_created_before(self, cutoff: str, *, limit: int) -> tuple[str, ...]:
        return ()


def _service(tmp_path: Path, *, allowed: bool) -> tuple[GatewayFileRetention, GatewayInstanceConfig, _PersonChanges]:
    config = GatewayInstanceConfig.create(
        instance_name="retention-test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
    )
    config.save(config.layout.instance_config_path)
    changes = _PersonChanges(allowed)
    uploads = UploadRetention(
        records=_NoRecords(),
        uploads_dir=config.layout.uploads_dir,
        retention_days=config.interactive_apps.internal_website.attachments.retention_days,
        interval=timedelta(hours=6),
    )
    service = GatewayFileRetention(config_path=config.config_path, person_changes=changes, uploads=uploads)
    return service, config, changes


def _backups(config: GatewayInstanceConfig) -> list[Path]:
    path = config.layout.instance_config_path
    return sorted(path.parent.glob(f"{path.name}.bak.*"))


def _saved(config: GatewayInstanceConfig) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.load_saved(config.config_path)


def test_new_instances_keep_uploads_three_months_and_scratch_three_weeks(tmp_path: Path) -> None:
    service, _, changes = _service(tmp_path, allowed=False)

    page = service.page(person_id="usr_member")

    assert page.retention == FileRetention(upload_days=90, scratch_days=21)
    assert page.can_change is False
    assert changes.asked == [("usr_member", RUNTIME_SETTINGS_CHANGE_KIND)]


def test_saving_changes_only_the_two_numbers_keeps_a_backup_and_tells_the_cleanup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, config, _ = _service(tmp_path, allowed=True)
    previous = config.layout.instance_config_path.read_bytes()
    expected = _saved(config)
    expected.interactive_apps.internal_website.attachments.retention_days = 30
    expected.tool_policy.scratch_retention_days = 0
    caplog.set_level(logging.INFO)

    saved = service.save(person_id="usr_admin", upload_days=30, scratch_days=0)

    assert saved == FileRetention(upload_days=30, scratch_days=0)
    after = _saved(config)
    assert after.interactive_apps.internal_website.attachments.retention_days == 30
    assert after.tool_policy.scratch_retention_days == 0
    # Loading fills in empty defaults (as every settings writer's save does),
    # so compare with the expected settings after the same save and load.
    expected.save(tmp_path / "expected.yaml")
    expected = GatewayInstanceConfig.load(tmp_path / "expected.yaml")
    assert after.to_yaml_dict() == expected.to_yaml_dict(), "nothing else in the file changed"
    [backup] = _backups(config)
    assert backup.read_bytes() == previous
    assert service._uploads.retention_days == 30, "the next cleanup uses the new number without a restart"
    assert "event=file_retention_changed person_id=usr_admin upload_days=90->30 scratch_days=21->0" in caplog.text
    assert service.page(person_id="usr_admin").retention == saved


def test_someone_who_may_not_change_it_changes_nothing(tmp_path: Path) -> None:
    service, config, _ = _service(tmp_path, allowed=False)
    previous = config.layout.instance_config_path.read_bytes()

    with pytest.raises(FileRetentionRefused, match="Only team administrators"):
        service.save(person_id="usr_member", upload_days=30, scratch_days=7)

    assert config.layout.instance_config_path.read_bytes() == previous
    assert _backups(config) == []


@pytest.mark.parametrize(
    ("upload_days", "scratch_days"),
    [(-1, 21), (3651, 21), ("30", 21), (30.5, 21), (True, 21), (None, 21), (90, -5)],
)
def test_numbers_the_settings_cannot_hold_are_refused_and_nothing_is_saved(
    tmp_path: Path, upload_days: object, scratch_days: object
) -> None:
    service, config, _ = _service(tmp_path, allowed=True)
    previous = config.layout.instance_config_path.read_bytes()

    with pytest.raises(FileRetentionInvalid, match="whole number|from 0"):
        service.save(person_id="usr_admin", upload_days=upload_days, scratch_days=scratch_days)

    assert config.layout.instance_config_path.read_bytes() == previous
    assert _backups(config) == []


def test_a_settings_change_made_meanwhile_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _ = _service(tmp_path, allowed=True)
    path = config.layout.instance_config_path
    real_load = GatewayInstanceConfig.load.__func__

    def load_then_someone_else_saves(cls, load_path):
        loaded = real_load(cls, load_path)
        path.write_text(path.read_text(encoding="utf-8") + "\n# changed by another settings writer\n", encoding="utf-8")
        return loaded

    monkeypatch.setattr(GatewayInstanceConfig, "load", classmethod(load_then_someone_else_saves))

    with pytest.raises(FileRetentionConflict, match="changed while this was being saved"):
        service.save(person_id="usr_admin", upload_days=30, scratch_days=7)

    assert path.read_text(encoding="utf-8").endswith("# changed by another settings writer\n")
    assert _backups(config) == []
    assert service._uploads.retention_days == 90
