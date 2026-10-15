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
    assert CONFIG_SCHEMA_VERSION == 10
    assert INSTANCE_CONFIG_SCHEMA_VERSION == 12


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
