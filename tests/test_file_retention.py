"""Settings > Team > File Retention's service (assistant_gateway.file_retention).

It reads and writes the real saved ``instance_config.yaml`` of a temporary
instance; only the person check is a stand-in, recording the change kind it
was asked about. The page holds the days files are kept and the storage
warnings (``gateway.storage_warnings``); a save hands new numbers to the
running upload cleanup and storage checks.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event

import pytest

from assistant_gateway.application.file_retention_page import (
    FileRetention,
    FileRetentionConflict,
    FileRetentionInvalid,
    FileRetentionRefused,
    StorageWarnings,
)
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.file_retention import GatewayFileRetention
from assistant_gateway.storage_capacity import StorageCapacity, StorageLevels
from assistant_gateway.storage_usage import StorageUsageNotices
from assistant_gateway.upload_retention import UploadRetention
from custom_agents.runtime_settings_service import RUNTIME_SETTINGS_CHANGE_KIND
from config_file_updates import ConfigFileConflict


class _PersonChanges:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.asked: list[tuple[str | None, str]] = []

    def may_change(self, person_id: str | None, kind: str) -> bool:
        self.asked.append((person_id, kind))
        return self.allowed


class _NoRecords:
    def delete_uploads_created_before(self, cutoff: str, *, uploads_dir: str, limit: int) -> tuple[str, ...]:
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
    service = GatewayFileRetention(
        config_path=config.config_path,
        person_changes=changes,
        uploads=uploads,
        storage_usage=StorageUsageNotices(
            team_id="team_test", step_gb=5, people=list, measure=dict, store=lambda: None,
            send_notice_once=lambda notice: True,
        ),
        storage_capacity=StorageCapacity(
            disks=list, databases=list, levels=StorageLevels(tmp_path / "levels.json"),
            nearing_limit_percent=80, nearly_full_percent=95, message_storage_kind="file",
        ),
    )
    return service, config, changes


# The storage warnings as new instances have them.
DEFAULT_WARNINGS = {"person_step_gb": 5, "nearing_limit_percent": 80, "nearly_full_percent": 95}


def _save(
    service: GatewayFileRetention,
    *,
    person_id: str,
    upload_days: object,
    scratch_days: object,
    output_days: object = 90,
    **warnings: object,
):
    """Save the days with the storage warnings unchanged, unless a test names them."""

    return service.save(
        person_id=person_id,
        upload_days=upload_days,
        scratch_days=scratch_days,
        output_days=output_days,
        **{**DEFAULT_WARNINGS, **warnings},
    )


def _backups(config: GatewayInstanceConfig) -> list[Path]:
    path = config.layout.instance_config_path
    return sorted(path.parent.glob(f"{path.name}.bak.*"))


def _saved(config: GatewayInstanceConfig) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.load_saved(config.config_path)


def test_new_instances_keep_uploads_and_saved_tool_outputs_three_months_and_scratch_three_weeks(
    tmp_path: Path,
) -> None:
    service, _, changes = _service(tmp_path, allowed=False)

    page = service.page(person_id="usr_member")

    assert page.retention == FileRetention(upload_days=90, scratch_days=21, output_days=90)
    assert page.warnings == StorageWarnings(person_step_gb=5, nearing_limit_percent=80, nearly_full_percent=95)
    assert page.can_change is False
    assert changes.asked == [("usr_member", RUNTIME_SETTINGS_CHANGE_KIND)]


def test_saving_changes_only_the_three_numbers_keeps_a_backup_and_tells_the_cleanup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, config, _ = _service(tmp_path, allowed=True)
    previous = config.layout.instance_config_path.read_bytes()
    expected = _saved(config)
    expected.interactive_apps.internal_website.attachments.retention_days = 30
    expected.tool_policy.scratch_retention_days = 0
    expected.tool_policy.output_retention_days = 180
    caplog.set_level(logging.INFO)

    saved = _save(service, person_id="usr_admin", upload_days=30, scratch_days=0, output_days=180)

    assert saved.retention == FileRetention(upload_days=30, scratch_days=0, output_days=180)
    after = _saved(config)
    assert after.interactive_apps.internal_website.attachments.retention_days == 30
    assert after.tool_policy.scratch_retention_days == 0
    assert after.tool_policy.output_retention_days == 180
    # Loading fills in empty defaults (as every settings writer's save does),
    # so compare with the expected settings after the same save and load.
    expected.save(tmp_path / "expected.yaml")
    expected = GatewayInstanceConfig.load(tmp_path / "expected.yaml")
    assert after.to_yaml_dict() == expected.to_yaml_dict(), "nothing else in the file changed"
    [backup] = _backups(config)
    assert backup.read_bytes() == previous
    assert service._uploads.retention_days == 30, "the next cleanup uses the new number without a restart"
    assert (
        "event=file_retention_changed person_id=usr_admin upload_days=90->30 scratch_days=21->0 "
        "output_days=90->180"
    ) in caplog.text
    assert service.page(person_id="usr_admin").retention == saved.retention


def test_someone_who_may_not_change_it_changes_nothing(tmp_path: Path) -> None:
    service, config, _ = _service(tmp_path, allowed=False)
    previous = config.layout.instance_config_path.read_bytes()

    with pytest.raises(FileRetentionRefused, match="Only team administrators"):
        _save(service, person_id="usr_member", upload_days=30, scratch_days=7, output_days=60)

    assert config.layout.instance_config_path.read_bytes() == previous
    assert _backups(config) == []


@pytest.mark.parametrize(
    ("upload_days", "scratch_days", "output_days"),
    [
        (-1, 21, 90),
        (3651, 21, 90),
        ("30", 21, 90),
        (30.5, 21, 90),
        (True, 21, 90),
        (None, 21, 90),
        (90, -5, 90),
        (90, 21, -1),
        (90, 21, None),
        (90, 21, "90"),
    ],
)
def test_numbers_the_settings_cannot_hold_are_refused_and_nothing_is_saved(
    tmp_path: Path, upload_days: object, scratch_days: object, output_days: object
) -> None:
    service, config, _ = _service(tmp_path, allowed=True)
    previous = config.layout.instance_config_path.read_bytes()

    with pytest.raises(FileRetentionInvalid, match="whole number|from 0"):
        _save(
            service,
            person_id="usr_admin", upload_days=upload_days, scratch_days=scratch_days, output_days=output_days
        )

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
        _save(service, person_id="usr_admin", upload_days=30, scratch_days=7, output_days=60)

    assert path.read_text(encoding="utf-8").endswith("# changed by another settings writer\n")
    assert _backups(config) == []
    assert service._uploads.retention_days == 90


def test_a_writer_racing_the_retention_backup_must_reload_before_it_can_save(tmp_path, monkeypatch) -> None:
    """The former gap between comparing and replacing is protected across writers."""

    import config_file_updates

    service, config, _ = _service(tmp_path, allowed=True)
    other = _saved(config)
    other.instance_name = "another writer"
    at_backup = Event()
    continue_save = Event()
    second_started = Event()
    second_done = Event()
    real_write = config_file_updates.atomic_write_bytes

    def pause_at_backup(path, content):
        if ".bak." in path.name:
            at_backup.set()
            assert continue_save.wait(timeout=10)
        real_write(path, content)

    def save_other():
        second_started.set()
        try:
            other.save()
        finally:
            second_done.set()

    monkeypatch.setattr(config_file_updates, "atomic_write_bytes", pause_at_backup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_save, service, person_id="usr_admin", upload_days=30, scratch_days=7, output_days=60)
        try:
            assert at_backup.wait(timeout=10)
            second = pool.submit(save_other)
            assert second_started.wait(timeout=10)
            assert not second_done.wait(timeout=0.05), "the other writer must wait for the checked replacement"
        finally:
            continue_save.set()
        assert first.result(timeout=10).retention == FileRetention(upload_days=30, scratch_days=7, output_days=60)
        with pytest.raises(ConfigFileConflict):
            second.result(timeout=10)
    after = _saved(config)
    assert after.instance_name == "retention-test"
    assert after.interactive_apps.internal_website.attachments.retention_days == 30
    # The refused writer can deliberately reapply its one change after reload.
    after.instance_name = "another writer"
    after.save()
    assert _saved(config).interactive_apps.internal_website.attachments.retention_days == 30


def test_concurrent_retention_saves_keep_disk_and_live_values_in_the_same_order(tmp_path, monkeypatch) -> None:
    service, config, _ = _service(tmp_path, allowed=True)
    first_written = Event()
    finish_first = Event()
    second_started = Event()
    second_done = Event()
    real_save = GatewayInstanceConfig.save

    def save_then_pause(self, *args, **kwargs):
        saved = real_save(self, *args, **kwargs)
        if self.interactive_apps.internal_website.attachments.retention_days == 30:
            first_written.set()
            assert finish_first.wait(timeout=10)
        return saved

    def save_second():
        second_started.set()
        try:
            return _save(service, person_id="usr_admin", upload_days=60, scratch_days=9, output_days=120)
        finally:
            second_done.set()

    monkeypatch.setattr(GatewayInstanceConfig, "save", save_then_pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_save, service, person_id="usr_admin", upload_days=30, scratch_days=7, output_days=60)
        try:
            assert first_written.wait(timeout=10)
            second = pool.submit(save_second)
            assert second_started.wait(timeout=10)
            assert not second_done.wait(timeout=0.05)
        finally:
            finish_first.set()
        first.result(timeout=10)
        second.result(timeout=10)
    assert service._uploads.retention_days == _saved(config).interactive_apps.internal_website.attachments.retention_days == 60


def test_a_save_without_saved_tool_output_days_keeps_the_saved_number(tmp_path: Path) -> None:
    """A page from before that setting sends only the other two numbers."""

    from assistant_gateway.application.file_retention_page import KEEP_SAVED_DAYS

    service, config, _ = _service(tmp_path, allowed=True)
    service.save(person_id="usr_admin", upload_days=90, scratch_days=21, output_days=120)

    saved = service.save(person_id="usr_admin", upload_days=30, scratch_days=7, output_days=KEEP_SAVED_DAYS)

    assert saved.retention == FileRetention(upload_days=30, scratch_days=7, output_days=120)
    assert _saved(config).tool_policy.output_retention_days == 120


@pytest.mark.parametrize("days", [3651, 1_000_000])
def test_the_configuration_refuses_more_than_ten_years_of_saved_tool_outputs(days: int) -> None:
    """Like the page's limit; a period reaching before the year 1 could not even be turned into a date."""

    from pydantic import ValidationError

    from runtime_settings.governance import ToolPolicySettings

    assert ToolPolicySettings.model_validate({"output_retention_days": 3650}).output_retention_days == 3650
    with pytest.raises(ValidationError):
        ToolPolicySettings.model_validate({"output_retention_days": days})


def test_saving_storage_warnings_changes_the_saved_file_and_the_running_checks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, config, _ = _service(tmp_path, allowed=True)
    caplog.set_level(logging.INFO)

    saved = _save(
        service, person_id="usr_admin", upload_days=90, scratch_days=21,
        person_step_gb=0, nearing_limit_percent=70, nearly_full_percent=90,
    )

    assert saved.warnings == StorageWarnings(person_step_gb=0, nearing_limit_percent=70, nearly_full_percent=90)
    stored = _saved(config).gateway.storage_warnings
    assert (stored.person_step_gb, stored.nearing_limit_percent, stored.nearly_full_percent) == (0, 70, 90)
    assert service._storage_usage.step_gb == 0, "people's notices stop from the next check"
    assert (service._storage_capacity.nearing_limit_percent, service._storage_capacity.nearly_full_percent) == (70, 90)
    assert "person_step_gb=5->0 nearing_limit_percent=80->70 nearly_full_percent=95->90" in caplog.text
    assert service.page(person_id="usr_admin").warnings == saved.warnings


@pytest.mark.parametrize(
    ("warnings", "message"),
    [
        ({"person_step_gb": -1}, "whole number of GB from 0"),
        ({"person_step_gb": 10_001}, "whole number of GB from 0"),
        ({"person_step_gb": "5"}, "whole number of GB from 0"),
        ({"person_step_gb": 2.5}, "whole number of GB from 0"),
        ({"nearing_limit_percent": 0}, "warning level must be a whole percent"),
        ({"nearly_full_percent": 101}, "problem level must be a whole percent"),
        ({"nearing_limit_percent": 95, "nearly_full_percent": 95}, "below the problem level"),
    ],
)
def test_storage_warnings_the_settings_cannot_hold_are_refused_and_nothing_is_saved(
    tmp_path: Path, warnings: dict, message: str
) -> None:
    service, config, _ = _service(tmp_path, allowed=True)
    previous = config.layout.instance_config_path.read_bytes()

    with pytest.raises(FileRetentionInvalid, match=message):
        _save(service, person_id="usr_admin", upload_days=30, scratch_days=7, **warnings)

    assert config.layout.instance_config_path.read_bytes() == previous
    assert _backups(config) == []
    assert service._storage_usage.step_gb == 5 and service._uploads.retention_days == 90
