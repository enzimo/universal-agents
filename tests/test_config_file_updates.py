"""Real-file contention tests for every instance configuration writer's boundary."""

from __future__ import annotations

import errno
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

import assistant_gateway.config as config_module
import file_locking
from assistant_gateway.config import GatewayInstanceConfig
from config_file_updates import ConfigFileConflict, config_revision, replace_config_file
from custom_agents.runtime_settings_service import RuntimeSettingsService


def _config(tmp_path: Path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="original", instance_root=str(tmp_path / "instance"), team_id="team_test", timezone="UTC"
    )
    config.save()
    return config


def _contending_write(path: str, revision: str, value: bytes, ready: Any, results: Any) -> None:
    ready.wait(timeout=10)
    try:
        replace_config_file(Path(path), value, expected_revision=revision)
        results.put("saved")
    except ConfigFileConflict:
        results.put("conflict")


def test_two_processes_cannot_both_replace_the_same_revision(tmp_path: Path) -> None:
    path = tmp_path / "instance_config.yaml"
    path.write_bytes(b"original")
    context = multiprocessing.get_context("spawn")
    ready = context.Barrier(2)
    results = context.Queue()
    revision = config_revision(path.read_bytes())
    workers = [
        context.Process(target=_contending_write, args=(str(path), revision, value, ready, results))
        for value in (b"first", b"second")
    ]
    try:
        for worker in workers:
            worker.start()
        assert sorted(results.get(timeout=15) for _ in workers) == ["conflict", "saved"]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        assert path.read_bytes() in {b"first", b"second"}
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()


def test_two_threads_saving_loaded_models_refuse_the_stale_one(tmp_path: Path) -> None:
    config = _config(tmp_path)
    copies = [GatewayInstanceConfig.load(config.config_path) for _ in range(2)]
    copies[0].instance_name = "first"
    copies[1].user.display_name = "second"
    ready = Barrier(2)

    def save(copy: GatewayInstanceConfig) -> str:
        ready.wait(timeout=10)
        try:
            copy.save()
            return "saved"
        except ConfigFileConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, copies)) == ["conflict", "saved"]
    final = GatewayInstanceConfig.load(config.config_path)
    assert (final.instance_name == "first") != (final.user.display_name == "second")


def test_new_models_need_the_destination_revision_to_replace_existing_settings(tmp_path: Path) -> None:
    config = _config(tmp_path)
    replacement = GatewayInstanceConfig.model_validate(config.model_dump())
    replacement.instance_name = "replacement"
    with pytest.raises(ConfigFileConflict):
        replacement.save()
    replacement.save(expected_revision=config.source_revision)
    assert GatewayInstanceConfig.load(config.config_path).instance_name == "replacement"


def test_load_captures_the_revision_of_the_same_bytes_it_parsed(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    real_parse = config_module._parse_instance_config_yaml

    def parse_then_replace(content: bytes, path: Path):
        parsed = real_parse(content, path)
        path.write_bytes(content + b"\n# another writer\n")
        return parsed

    monkeypatch.setattr(config_module, "_parse_instance_config_yaml", parse_then_replace)
    loaded = GatewayInstanceConfig.load(config.config_path)
    with pytest.raises(ConfigFileConflict):
        loaded.save()
    assert config.config_path.read_bytes().endswith(b"# another writer\n")


def test_lock_failure_leaves_the_configuration_and_backup_unchanged(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    before = config.config_path.read_bytes()
    backup = config.config_path.with_suffix(".bak")

    def unavailable(*_args):
        raise OSError(errno.ENOLCK, "no file locks")

    monkeypatch.setattr(file_locking.fcntl, "flock", unavailable)
    with pytest.raises(file_locking.FileLockUnavailable):
        config.save(backup_path=backup)
    assert config.config_path.read_bytes() == before
    assert not backup.exists()


def test_runtime_settings_undo_refuses_to_erase_a_later_update(tmp_path: Path) -> None:
    config = _config(tmp_path)
    original = config.config_path.read_bytes()
    backup = config.config_path.with_suffix(".bak")
    backup.write_bytes(original)
    config.heartbeat.enabled = not config.heartbeat.enabled
    config.save()
    applied_revision = config.source_revision
    later = GatewayInstanceConfig.load(config.config_path)
    later.instance_name = "later update"
    later.save()

    with pytest.raises(ConfigFileConflict):
        RuntimeSettingsService._restore_backup({
            "persistence_target": "instance_config", "saved_path": str(config.config_path),
            "backup_path": str(backup), "previous_existed": True, "sha256": applied_revision,
        })
    assert GatewayInstanceConfig.load(config.config_path).instance_name == "later update"


@pytest.mark.parametrize("existing_handover", [False, True])
def test_stale_bootstrap_does_not_change_control_env_or_key_handover_files(tmp_path, existing_handover) -> None:
    from assistant_gateway.bootstrap import bootstrap_instance, hand_over_onboarding_keys

    config = _config(tmp_path)
    config.layout.ensure_directories()
    env = config.instance_root_path / "team.env"
    env.write_text("APP_TEST_VALUE=original\n", encoding="utf-8")
    config.launch.env_file_path = str(env)
    config.save()
    source = tmp_path / "source.env"
    source.write_text("APP_TEST_VALUE=replacement\nEXA_API_KEY=test-bootstrap-example-key\n", encoding="utf-8")
    control = config.layout.control_dir / "SOUL.md"
    control.write_text("existing identity", encoding="utf-8")
    missing_control = config.layout.control_dir / "TEAM_TEMPLATES.yaml"
    handover = config.layout.onboarding_account_keys_path
    if existing_handover:
        hand_over_onboarding_keys(config, {"http:exa": "test-existing-example-key"})
        assert handover.is_file()
    previous_handover = handover.read_bytes() if handover.exists() else None
    stale = GatewayInstanceConfig.load(config.config_path)
    other = GatewayInstanceConfig.load(config.config_path)
    other.instance_name = "another writer"
    other.save()

    with pytest.raises(ConfigFileConflict):
        bootstrap_instance(stale, source_env_file_path=str(source))

    assert env.read_text(encoding="utf-8") == "APP_TEST_VALUE=original\n"
    assert control.read_text(encoding="utf-8") == "existing identity"
    assert not missing_control.exists()
    assert (handover.read_bytes() if handover.exists() else None) == previous_handover
    assert GatewayInstanceConfig.load(config.config_path).instance_name == "another writer"


def test_bootstrap_checks_source_files_before_publishing_configuration(tmp_path) -> None:
    from assistant_gateway.bootstrap import bootstrap_instance

    config = _config(tmp_path)
    config.launch.env_file_path = str(config.instance_root_path / "team.env")
    previous = config.config_path.read_bytes()
    source = tmp_path / "invalid.env"
    source.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        bootstrap_instance(config, source_env_file_path=str(source))
    assert config.config_path.read_bytes() == previous
