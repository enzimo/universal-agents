from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from assistant_gateway.config import (
    INSTANCE_CONFIG_SCHEMA_VERSION,
    GatewayInstanceConfig,
    _migrate_instance_config_dict,
    migrate_instance_config_file,
)
from config_loader import (
    CONFIG_SCHEMA_VERSION,
    _migrate_config_dict,
    migrate_config_file,
)


def test_public_config_schema_versions_are_current() -> None:
    assert CONFIG_SCHEMA_VERSION == 13
    assert INSTANCE_CONFIG_SCHEMA_VERSION == 15


@pytest.mark.parametrize(
    ("migrate", "version", "source_name"),
    [
        (_migrate_config_dict, 6, "config.yaml"),
        (_migrate_instance_config_dict, 8, "instance_config.yaml"),
    ],
)
def test_removed_mutable_automation_jobs_require_explicit_recreation(
    tmp_path: Path,
    migrate,
    version: int,
    source_name: str,
) -> None:
    with pytest.raises(ValueError, match="automation_jobs cannot be migrated"):
        migrate(
            {"schema_version": version, "automation_jobs": {"enabled": True}},
            source_path=tmp_path / source_name,
        )


@pytest.mark.parametrize(
    ("migrate", "version", "source_name"),
    [
        (_migrate_config_dict, 5, "config.yaml"),
        (_migrate_instance_config_dict, 7, "instance_config.yaml"),
    ],
)
def test_objective_execution_singular_path_is_rejected(
    tmp_path: Path,
    migrate,
    version: int,
    source_name: str,
) -> None:
    with pytest.raises(ValueError, match="objective_execution_paths"):
        migrate(
            {
                "schema_version": version,
                "workspace": {"objective_execution_path": "policy.yaml"},
            },
            source_path=tmp_path / source_name,
        )


@pytest.mark.parametrize(
    ("migrate", "payload", "source_name", "removed_path"),
    [
        (
            _migrate_config_dict,
            {"schema_version": 4, "interactive_apps": {"telegram": {}}},
            "config.yaml",
            "interactive_apps.telegram",
        ),
        (
            _migrate_instance_config_dict,
            {"schema_version": 5, "interactive_apps": {"telegram": {}}},
            "instance_config.yaml",
            "interactive_apps.telegram",
        ),
    ],
)
def test_phase3_schema_bump_rejects_removed_adapter_configuration(
    tmp_path: Path,
    migrate,
    payload: dict[str, object],
    source_name: str,
    removed_path: str,
) -> None:
    with pytest.raises(ValueError, match=rf"{removed_path} has been removed"):
        migrate(payload, source_path=tmp_path / source_name)


@pytest.mark.parametrize("invalid_version", [None, True, "1", 0, -1])
def test_generic_config_requires_an_explicit_positive_integer_version(
    tmp_path: Path,
    invalid_version: object,
) -> None:
    payload = {} if invalid_version is None else {"schema_version": invalid_version}

    with pytest.raises(ValueError, match="schema_version"):
        _migrate_config_dict(payload, source_path=tmp_path / "config.yaml")


@pytest.mark.parametrize("invalid_version", [None, True, "1", 0, -1])
def test_instance_config_requires_an_explicit_positive_integer_version(
    tmp_path: Path,
    invalid_version: object,
) -> None:
    payload = {} if invalid_version is None else {"schema_version": invalid_version}

    with pytest.raises(ValueError, match="schema_version"):
        _migrate_instance_config_dict(payload, source_path=tmp_path / "instance_config.yaml")


@pytest.mark.parametrize(
    ("migrate", "name"),
    [
        (migrate_config_file, "config.yaml"),
        (migrate_instance_config_file, "instance_config.yaml"),
    ],
)
def test_migration_file_helpers_retain_only_five_rotating_backups(
    tmp_path: Path,
    migrate,
    name: str,
) -> None:
    config_path = tmp_path / name
    config_path.write_text("schema_version: 3\n", encoding="utf-8")
    for index in range(6):
        config_path.with_name(f"{name}.bak.preexisting.{index}").write_text(
            f"backup {index}\n",
            encoding="utf-8",
        )

    result = migrate(config_path)

    assert result.backup_path is not None and result.backup_path.is_file()
    assert len(list(tmp_path.glob(f"{name}.bak.*"))) == 5


@pytest.mark.parametrize(
    ("migrate", "name"),
    [
        (_migrate_config_dict, "config.yaml"),
        (_migrate_instance_config_dict, "instance_config.yaml"),
    ],
)
def test_current_schema_rejects_unreleased_future_versions(
    tmp_path: Path,
    migrate,
    name: str,
) -> None:
    expected_latest = (
        CONFIG_SCHEMA_VERSION
        if name == "config.yaml"
        else INSTANCE_CONFIG_SCHEMA_VERSION
    )
    with pytest.raises(
        ValueError,
        match=rf"newer than supported latest version {expected_latest}",
    ):
        migrate(
            {"schema_version": expected_latest + 1},
            source_path=tmp_path / name,
        )


def test_generic_config_rejects_current_workspace_task_history_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"workspace\.task_history_db_path has been removed"):
        _migrate_config_dict(
            {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "workspace": {"task_history_db_path": "runtime/task-history.db"},
            },
            source_path=tmp_path / "config.yaml",
        )


@pytest.mark.parametrize(
    "removed_key",
    ["skills_dir", "python_envs_dir", "task_history_db_path", "llm_override_path", "team_composition_path"],
)
def test_generic_config_rejects_removed_agent_workspace_keys(
    tmp_path: Path,
    removed_key: str,
) -> None:
    with pytest.raises(ValueError, match=rf"agent\.{removed_key} has been removed.*workspace"):
        _migrate_config_dict(
            {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "agent": {removed_key: "old-path"},
            },
            source_path=tmp_path / "config.yaml",
        )


def test_instance_config_rejects_current_workspace_task_history_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"workspace\.task_history_db_path has been removed"):
        GatewayInstanceConfig.model_validate(
            {
                "schema_version": INSTANCE_CONFIG_SCHEMA_VERSION,
                "instance_name": "test",
                "instance_root": str(tmp_path),
                "workspace": {"task_history_db_path": "runtime/task-history.db"},
            }
        )


@pytest.mark.parametrize(
    ("removed_key", "replacement"),
    [
        ("manager", "gateway"),
        ("agent_default", "agent"),
    ],
)
def test_instance_config_rejects_removed_top_level_blocks(
    tmp_path: Path,
    removed_key: str,
    replacement: str,
) -> None:
    with pytest.raises(ValueError, match=rf"{removed_key} has been removed.*{replacement}"):
        GatewayInstanceConfig.model_validate(
            {
                "schema_version": INSTANCE_CONFIG_SCHEMA_VERSION,
                "instance_name": "test",
                "instance_root": str(tmp_path),
                removed_key: {"team_id": "team_test"},
            }
        )


def test_config_layout_migration_refuses_customized_locations(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"workspace\.skills_dir=/srv/skills"):
        _migrate_config_dict(
            {"schema_version": 7, "workspace": {"skills_dir": "/srv/skills"}},
            source_path=tmp_path / "config.yaml",
        )


def test_config_current_schema_rejects_removed_workspace_dir(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"agent\.workspace_dir has been removed"):
        _migrate_config_dict(
            {"schema_version": CONFIG_SCHEMA_VERSION, "agent": {"workspace_dir": "/data"}},
            source_path=tmp_path / "config.yaml",
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"agent": {"workspace_dir": "/elsewhere"}}, r"agent\.workspace_dir is set to /elsewhere"),
        (
            {"state": {"storage": {"sqlite": {"path": "{root}/workspace/private.db"}}}},
            r"state\.storage\.sqlite\.path .* is inside the old workspace",
        ),
        (
            {"workspace": {"python_envs_dir": "/srv/envs"}},
            r"workspace\.python_envs_dir=/srv/envs",
        ),
    ],
)
def test_instance_layout_migration_refuses_customized_locations(
    tmp_path: Path,
    override: dict[str, object],
    message: str,
) -> None:
    root = tmp_path / "instance"
    payload = yaml.safe_load(yaml.safe_dump(override).replace("{root}", str(root)))
    with pytest.raises(ValueError, match=message):
        _migrate_instance_config_dict(
            {
                "schema_version": 9,
                "instance_name": "test",
                "instance_root": str(root),
                **payload,
            },
            source_path=tmp_path / "instance_config.yaml",
        )


def _old_capacity_settings() -> dict[str, object]:
    return {
        "schedule_workers": {
            "enabled": True,
            "max_auto_workers_per_type": 1,
            "max_total_workers_per_team": 48,
        },
        "delegation_assurance": {"enabled": True},
    }


def test_config_upgrade_moves_worker_capacity_to_worker_pools(tmp_path: Path) -> None:
    migrated, steps = _migrate_config_dict(
        {"schema_version": 8, "agent": _old_capacity_settings()},
        source_path=tmp_path / "config.yaml",
    )
    assert migrated["schema_version"] == CONFIG_SCHEMA_VERSION
    assert migrated["agent"]["worker_pools"] == {"max_total_workers_per_team": 48}
    assert migrated["agent"]["schedule_workers"] == {"enabled": True}
    assert "delegation_assurance" not in migrated["agent"]
    assert any(step.startswith("8 -> 9: worker capacity moved") for step in steps)


def test_instance_config_upgrade_moves_worker_capacity_to_worker_pools(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    migrated, steps = _migrate_instance_config_dict(
        {
            "schema_version": 10,
            "instance_name": "test",
            "instance_root": str(root),
            "agent": _old_capacity_settings(),
        },
        source_path=tmp_path / "instance_config.yaml",
    )
    assert migrated["schema_version"] == INSTANCE_CONFIG_SCHEMA_VERSION
    assert migrated["agent"]["worker_pools"] == {"max_total_workers_per_team": 48}
    assert "delegation_assurance" not in migrated["agent"]
    assert any(step.startswith("10 -> 11: worker capacity moved") for step in steps)


# The MCP folder settings removed in config.yaml 10 / instance config 12, with
# the default each had. InstanceLayout derives these folders from the root.
_REMOVED_MCP_FOLDERS = {
    ("registry_dir",): "runtime/mcp-connections",
    ("hosting", "definitions_dir"): "runtime/mcp-hosting/definitions",
    ("hosting", "state_dir"): "runtime/mcp-hosting/state",
}


def _mcp_section(values: dict[tuple[str, ...], str]) -> dict[str, object]:
    mcp: dict[str, object] = {"enabled": True, "hosting": {"enabled": False}}
    for path, value in values.items():
        node = mcp
        for key in path[:-1]:
            node = node[key]  # type: ignore[assignment]
        node[path[-1]] = value  # type: ignore[index]
    return mcp


def _instance_payload(root: Path, version: int, mcp: dict[str, object]) -> dict[str, object]:
    return {"schema_version": version, "instance_name": "test", "instance_root": str(root), "mcp": mcp}


def test_config_upgrade_drops_mcp_folders_that_hold_their_defaults(tmp_path: Path) -> None:
    migrated, steps = _migrate_config_dict(
        {"schema_version": 9, "mcp": _mcp_section(_REMOVED_MCP_FOLDERS)},
        source_path=tmp_path / "config.yaml",
    )

    assert migrated["schema_version"] == CONFIG_SCHEMA_VERSION
    assert migrated["mcp"] == {"enabled": True, "hosting": {"enabled": False}}
    assert any(step.startswith("9 -> 10: MCP connection and hosting folders") for step in steps)


def test_instance_config_upgrade_drops_mcp_folders_that_hold_their_defaults(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    migrated, steps = _migrate_instance_config_dict(
        _instance_payload(root, 11, _mcp_section(_REMOVED_MCP_FOLDERS)),
        source_path=tmp_path / "instance_config.yaml",
    )

    assert migrated["schema_version"] == INSTANCE_CONFIG_SCHEMA_VERSION
    assert migrated["mcp"] == {"enabled": True, "hosting": {"enabled": False}}
    assert any(step.startswith("11 -> 12: MCP connection and hosting folders") for step in steps)
    config = GatewayInstanceConfig.model_validate(migrated)
    assert not hasattr(config.mcp, "registry_dir")
    assert not hasattr(config.mcp.hosting, "definitions_dir")


def test_saved_instance_config_with_default_mcp_folders_loads_after_upgrade(tmp_path: Path) -> None:
    """An instance saved by the previous release (every default written out) still starts."""

    root = tmp_path / "instance"
    path = tmp_path / "instance_config.yaml"
    saved = GatewayInstanceConfig.create(instance_name="test", instance_root=str(root), team_id="team_test", timezone="UTC")
    payload = saved.model_dump(mode="json")
    payload["schema_version"] = 11
    payload["mcp"] = {**payload["mcp"], **_mcp_section(_REMOVED_MCP_FOLDERS)}
    payload["mcp"]["hosting"] = {**saved.model_dump(mode="json")["mcp"]["hosting"], **payload["mcp"]["hosting"]}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    result = migrate_instance_config_file(path, create_backup=False)

    assert result.migrated_version == INSTANCE_CONFIG_SCHEMA_VERSION
    rewritten = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "registry_dir" not in rewritten["mcp"]
    assert not {"definitions_dir", "state_dir"} & set(rewritten["mcp"]["hosting"])
    assert GatewayInstanceConfig.load(path).instance_root_path == root.resolve()


@pytest.mark.parametrize("path", list(_REMOVED_MCP_FOLDERS))
def test_config_upgrade_refuses_a_customized_mcp_folder(tmp_path: Path, path: tuple[str, ...]) -> None:
    name = ".".join(("mcp", *path))
    with pytest.raises(ValueError, match=rf"no longer configurable: {name}=/srv/mcp"):
        _migrate_config_dict(
            {"schema_version": 9, "mcp": _mcp_section({path: "/srv/mcp"})},
            source_path=tmp_path / "config.yaml",
        )


@pytest.mark.parametrize("path", list(_REMOVED_MCP_FOLDERS))
def test_instance_config_upgrade_refuses_a_customized_mcp_folder(tmp_path: Path, path: tuple[str, ...]) -> None:
    name = ".".join(("mcp", *path))
    with pytest.raises(ValueError, match=rf"no longer configurable: {name}=/srv/mcp"):
        _migrate_instance_config_dict(
            _instance_payload(tmp_path / "instance", 11, _mcp_section({path: "/srv/mcp"})),
            source_path=tmp_path / "instance_config.yaml",
        )


@pytest.mark.parametrize("path", list(_REMOVED_MCP_FOLDERS))
def test_current_schemas_refuse_a_removed_mcp_folder_by_name(tmp_path: Path, path: tuple[str, ...]) -> None:
    """A current-version file naming the setting was written by hand; it is refused, not dropped."""

    name = ".".join(("mcp", *path))
    mcp = _mcp_section({path: _REMOVED_MCP_FOLDERS[path]})
    with pytest.raises(ValueError, match=rf"{re.escape(name)} has been removed"):
        _migrate_config_dict(
            {"schema_version": CONFIG_SCHEMA_VERSION, "mcp": mcp}, source_path=tmp_path / "config.yaml"
        )
    with pytest.raises(ValueError, match=rf"{re.escape(name)} has been removed"):
        GatewayInstanceConfig.model_validate(
            _instance_payload(tmp_path / "instance", INSTANCE_CONFIG_SCHEMA_VERSION, mcp)
        )


# -- the presence stream's setting (message contract step 7d) ------------------------------

PINNED_0_9_CONFIG = Path(__file__).parent / "fixtures" / "pinned_memory" / "main_instance_config.yaml"


CONCURRENCY_STEP = "13 -> 14: remove the obsolete per-agent concurrency setting; agent.max_concurrent_top_level_tasks removed"
PARTITION_COUNT_STEP = "14 -> 15: store the protocol 1 partition count; transport.queues.partitions set to 96"


def test_upgrading_the_pinned_old_instance_config_removes_the_retired_settings() -> None:
    """The fixture is an earlier version's file (schema 9), kept byte for byte; it holds both settings.

    The whole chain runs from 9. The file itself does not load: it also holds settings of the
    old permission system, which this version refuses by name (the fixture's header).
    """

    raw = yaml.safe_load(PINNED_0_9_CONFIG.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 9
    assert raw["transport"]["presence_max_age_seconds"] == 600.0
    assert raw["agent"]["max_concurrent_top_level_tasks"] == 1

    migrated, steps = _migrate_instance_config_dict(raw, source_path=PINNED_0_9_CONFIG)

    assert migrated["schema_version"] == INSTANCE_CONFIG_SCHEMA_VERSION
    assert "presence_max_age_seconds" not in migrated["transport"]
    assert "max_concurrent_top_level_tasks" not in migrated["agent"]
    assert migrated["agent"]["max_queued_top_level_tasks"] == raw["agent"]["max_queued_top_level_tasks"]
    assert "12 -> 13: the presence stream was removed; transport.presence_max_age_seconds removed" in steps
    assert CONCURRENCY_STEP in steps
    assert PARTITION_COUNT_STEP in steps and migrated["transport"]["queues"] == {"partitions": 96}
    assert raw["transport"]["presence_max_age_seconds"] == 600.0, "the caller's mapping is not changed"
    assert raw["agent"]["max_concurrent_top_level_tasks"] == 1, "the caller's mapping is not changed"


def test_upgrading_a_0_9_instance_config_file_removes_the_retired_settings_and_keeps_a_backup(tmp_path: Path) -> None:
    from tests.pinned_memory_support import zero_nine_instance

    path = zero_nine_instance(tmp_path, "alpha", "team_a")

    result = migrate_instance_config_file(path)

    assert (result.original_version, result.migrated_version) == (12, INSTANCE_CONFIG_SCHEMA_VERSION)
    assert any(step.startswith("12 -> 13: the presence stream was removed;") for step in result.applied_steps)
    assert CONCURRENCY_STEP in result.applied_steps
    assert PARTITION_COUNT_STEP in result.applied_steps
    text = path.read_text(encoding="utf-8")
    upgraded = yaml.safe_load(text)
    assert upgraded["transport"]["queues"] == {"partitions": 96}, "the count is in the file from the first 0.10 start"
    partitions_line = next(line for line in text.splitlines() if line.strip().startswith("partitions:"))
    assert "Fixed when the instance is created" in partitions_line
    backup = yaml.safe_load(result.backup_path.read_text(encoding="utf-8"))
    assert "presence_max_age_seconds" not in upgraded["transport"]
    assert "max_concurrent_top_level_tasks" not in upgraded["agent"]
    assert backup["transport"]["presence_max_age_seconds"] == 600.0
    assert backup["agent"]["max_concurrent_top_level_tasks"] == 1
    loaded = GatewayInstanceConfig.load(path)  # the current format, which refuses either setting by name
    assert loaded.transport.work_max_age_seconds == 604800.0
    assert "max_concurrent_top_level_tasks" not in loaded.agent.model_dump()


def test_upgrading_a_config_yaml_removes_the_retired_settings_and_keeps_a_backup(tmp_path: Path) -> None:
    """Schema 10 is what 0.9 releases write; their config.example.yaml also holds the concurrency setting."""

    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({
            "schema_version": 10,
            "transport": {"presence_max_age_seconds": 600.0, "max_deliver": 5},
            "agent": {"max_concurrent_top_level_tasks": 1, "max_queued_top_level_tasks": 22},
        }),
        encoding="utf-8",
    )

    result = migrate_config_file(path)

    assert (result.original_version, result.migrated_version) == (10, CONFIG_SCHEMA_VERSION)
    assert "10 -> 11: the presence stream was removed; transport.presence_max_age_seconds removed" in result.applied_steps
    assert (
        "11 -> 12: remove the obsolete per-agent concurrency setting; agent.max_concurrent_top_level_tasks removed"
        in result.applied_steps
    )
    assert "12 -> 13: transport.replicas is 1 to 5" in result.applied_steps
    upgraded = yaml.safe_load(path.read_text(encoding="utf-8"))
    backup = yaml.safe_load(result.backup_path.read_text(encoding="utf-8"))
    assert upgraded["transport"] == {"max_deliver": 5}
    assert upgraded["agent"] == {"max_queued_top_level_tasks": 22}
    assert backup["transport"]["presence_max_age_seconds"] == 600.0
    assert backup["agent"]["max_concurrent_top_level_tasks"] == 1


def test_the_upgrade_step_without_the_setting_only_advances_the_version(tmp_path: Path) -> None:
    migrated, steps = _migrate_config_dict({"schema_version": 10}, source_path=tmp_path / "config.yaml")

    assert migrated == {"schema_version": CONFIG_SCHEMA_VERSION}
    assert "10 -> 11: the presence stream was removed" in steps


def test_current_files_naming_the_presence_setting_are_refused(tmp_path: Path) -> None:
    """The upgrade step removes it, so a current file holding it was edited by hand."""

    transport = {"transport": {"presence_max_age_seconds": 600.0}}
    with pytest.raises(ValueError, match=r"transport\.presence_max_age_seconds has been removed"):
        _migrate_config_dict({"schema_version": CONFIG_SCHEMA_VERSION, **transport}, source_path=tmp_path / "config.yaml")
    with pytest.raises(ValueError, match=r"transport\.presence_max_age_seconds has been removed"):
        GatewayInstanceConfig.model_validate(
            {"schema_version": INSTANCE_CONFIG_SCHEMA_VERSION, "instance_name": "test",
             "instance_root": str(tmp_path), **transport}
        )


@pytest.mark.parametrize("migrate,previous,current,later", [
    (_migrate_config_dict, 11, CONFIG_SCHEMA_VERSION, {}),
    (_migrate_instance_config_dict, 13, INSTANCE_CONFIG_SCHEMA_VERSION, {"transport": {"queues": {"partitions": 96}}}),
])
def test_the_concurrency_upgrade_removes_the_obsolete_value_without_mutating_its_source(
    tmp_path, migrate, previous, current, later,
):
    source = {"schema_version": previous, "agent": {
        "max_concurrent_top_level_tasks": 4, "max_queued_top_level_tasks": 22}}
    upgraded, steps = migrate(source, source_path=tmp_path / "config.yaml")
    assert upgraded == {"schema_version": current, "agent": {"max_queued_top_level_tasks": 22}, **later}
    assert source["agent"]["max_concurrent_top_level_tasks"] == 4
    assert any("agent.max_concurrent_top_level_tasks removed" in step for step in steps)


def test_current_files_reject_the_retired_concurrency_setting(tmp_path):
    setting = {"agent": {"max_concurrent_top_level_tasks": 1}}
    with pytest.raises(ValueError, match="agent.max_concurrent_top_level_tasks has been removed"):
        _migrate_config_dict({"schema_version": CONFIG_SCHEMA_VERSION, **setting}, source_path=tmp_path / "config.yaml")
    with pytest.raises(ValueError, match="agent.max_concurrent_top_level_tasks has been removed"):
        GatewayInstanceConfig.model_validate({"schema_version": INSTANCE_CONFIG_SCHEMA_VERSION,
            "instance_name": "test", "instance_root": str(tmp_path), **setting})


# -- the protocol 1 partition count (message contract phase 3, step 2a) ----------------------


@pytest.mark.parametrize(("transport", "expected"), [
    (None, {"queues": {"partitions": 96}}),
    ({"max_deliver": 5}, {"max_deliver": 5, "queues": {"partitions": 96}}),
    ({"queues": None}, {"queues": {"partitions": 96}}),
    ({"queues": {"max_bytes": {"pool": 64 * 1024 * 1024}}},
     {"queues": {"max_bytes": {"pool": 64 * 1024 * 1024}, "partitions": 96}}),
])
def test_the_partition_count_upgrade_writes_the_default_into_a_file_without_one(tmp_path, transport, expected) -> None:
    """The count is fixed when an installation is created, so it is in the file rather than left to the code."""

    source = {"schema_version": 14, **({} if transport is None else {"transport": transport})}
    before = yaml.safe_dump(source)

    upgraded, steps = _migrate_instance_config_dict(source, source_path=tmp_path / "instance_config.yaml")

    assert upgraded == {"schema_version": 15, "transport": expected}
    assert steps == (PARTITION_COUNT_STEP,)
    assert yaml.safe_dump(source) == before, "the caller's mapping is not changed"


# -- a replica count of 0, which releases before 0.10 read as one copy (C24 of the step 2a run) -------


@pytest.mark.parametrize(("migrate", "previous", "current", "step", "later"), [
    (_migrate_config_dict, 12, 13, "12 -> 13: transport.replicas is 1 to 5", {}),
    (_migrate_instance_config_dict, 14, 15, "14 -> 15: store the protocol 1 partition count; "
     "transport.queues.partitions set to 96", {"queues": {"partitions": 96}}),
])
@pytest.mark.parametrize(("replicas", "upgraded_replicas"), [
    (0, 1), (0.0, 1), ("0", 1), (False, 1),  # 0 meant one copy
    (1.0, 1), (True, 1), ("3", 3), (" 3 ", 3), ("3.0", 3),  # DI-1 of PR 152's review: what 0.9's int field read
    (3, 3), (1.5, 1.5), ("many", "many"), (None, None), (7, 7), ("-2", "-2"),  # left for validation to name
])
def test_a_saved_replica_count_is_written_as_the_whole_number_earlier_releases_read(
        tmp_path, migrate, previous, current, step, later, replicas, upgraded_replicas) -> None:
    """Earlier releases read the setting as a pydantic int and made every stream with int(replicas or 1) copies, so 0
    meant one; validation takes only a whole number from 1 to 5 now."""

    source = {"schema_version": previous, "transport": {"replicas": replicas}}
    before = yaml.safe_dump(source)

    upgraded, steps = migrate(source, source_path=tmp_path / "settings.yaml")

    assert upgraded == {"schema_version": current, "transport": {"replicas": upgraded_replicas, **later}}
    written = type(upgraded_replicas) is int and not (type(replicas) is int and replicas == upgraded_replicas)
    assert steps[-1] == step + (f"; transport.replicas {replicas!r} set to {upgraded_replicas}" if written else "")
    assert yaml.safe_dump(source) == before, "the caller's mapping is not changed"


def test_a_0_9_config_yaml_with_a_replica_count_of_0_loads_with_one_copy(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 10, "transport": {"replicas": 0}}), encoding="utf-8")

    result = migrate_config_file(path)

    assert "12 -> 13: transport.replicas is 1 to 5; transport.replicas 0 set to 1" in result.applied_steps
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["transport"] == {"replicas": 1}
    assert yaml.safe_load(result.backup_path.read_text(encoding="utf-8"))["transport"] == {"replicas": 0}


@pytest.mark.parametrize("transport", [{"queues": {"partitions": 64}}, {"queues": "many"}, "nats"])
def test_the_partition_count_upgrade_keeps_a_stored_count_and_leaves_a_wrong_value_to_validation(tmp_path, transport) -> None:
    upgraded, steps = _migrate_instance_config_dict({"schema_version": 14, "transport": transport},
                                                    source_path=tmp_path / "instance_config.yaml")

    assert upgraded == {"schema_version": 15, "transport": transport}
    assert steps == ("14 -> 15: store the protocol 1 partition count",)
