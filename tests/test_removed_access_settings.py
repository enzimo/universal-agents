"""Removed access settings are refused by name (access Phase 4d-6 and Phase 5).

Both loaders (``config.yaml`` through ``config_loader`` and
``instance_config.yaml`` through ``GatewayInstanceConfig``) must name each
removed key and point to what replaces it, instead of silently ignoring it.
That covers the old permission system's settings, the three ``tool_policy``
switches replaced by grants, and ``tool_policy.data_folders``, replaced by
data folders an administrator registers in the access store (decision D16).
"""

from pathlib import Path

import pytest
import yaml

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.runtime_config import load_runtime_config_file
from config_loader import CONFIG_SCHEMA_VERSION, Config, resolve_agent_type_settings
from runtime_settings.governance import (
    REMOVED_AUTHORIZATION_KEYS,
    REMOVED_TOOL_POLICY_GUIDANCE,
    REMOVED_TOOL_POLICY_KEYS,
)

KEPT_AUTHORIZATION_KEYS = {"enabled", "cedar_binary_path", "starter_profile", "presentation"}
# What every refusal must point to.
REPLACEMENT_POINTERS = (
    "authorization.enabled",
    "authorization.starter_profile",
    "/access",
    "assistant_cli access",
)
# One realistic value per removed key, as older generated files held them.
OLD_VALUES = {
    "engine": "cedar",
    "fail_closed": True,
    "policy_path": None,
    "schema_path": None,
    "cedar_request_timeout_seconds": 0.5,
    "user_approval": {"enabled": True},
    "admin_elevation": {"enabled": True},
    "emergency_deny": {"approval_keys": []},
    "permission_delegation": {"mode": "enforce", "enforced_actions": ["tool.invoke"]},
    "shell_command_profiles": [],
}
# The tool_policy switches as older generated files held them.
OLD_TOOL_POLICY_VALUES = {
    "restrict_to_workspace": True,
    "allow_web_search": True,
    "allow_shell_commands": True,
    "data_folders": [{"name": "reports", "path": "/srv/reports"}],
}


def generated_instance(tmp_path: Path) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.create(
        instance_name="removed-settings",
        instance_root=str(tmp_path / "instance"),
        team_id="team_removed",
        timezone="UTC",
    )


def write_document(tmp_path: Path, kind: str, **sections) -> Path:
    document = (
        generated_instance(tmp_path).to_yaml_dict()
        if kind == "instance"
        else {"schema_version": CONFIG_SCHEMA_VERSION}
    )
    document.update(sections)
    path = tmp_path / f"{kind}.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


def assert_refused_by_name(error: pytest.ExceptionInfo, *names: str) -> None:
    message = str(error.value)
    for name in names:
        assert name in message
    assert "has been removed" in message or "have been removed" in message
    for pointer in REPLACEMENT_POINTERS:
        assert pointer in message


def test_generated_and_example_configs_hold_only_the_kept_settings(tmp_path):
    saved = yaml.safe_load(generated_instance(tmp_path).save().read_text())
    example = yaml.safe_load(Path("config.example.yaml").read_text())
    for document in (saved, example):
        # Saved instance files leave out null values (cedar_binary_path).
        assert {"enabled", "starter_profile", "presentation"} <= set(document["authorization"])
        assert set(document["authorization"]) <= KEPT_AUTHORIZATION_KEYS
        assert "human_interactions" not in document
        assert set(document["access_workflows"]) == {"max_steps", "diagnostic_event_limit"}
        assert not set(document.get("tool_policy") or {}) & set(REMOVED_TOOL_POLICY_KEYS)


def test_the_example_config_loads_and_projects_the_kept_settings(tmp_path):
    loaded = load_runtime_config_file(Path("config.example.yaml"))
    runtime = resolve_agent_type_settings(loaded, "TeamArchitect")
    assert set(runtime["authorization_config"]) == KEPT_AUTHORIZATION_KEYS
    assert runtime["authorization_config"]["enabled"] is True


@pytest.mark.parametrize("kind", ["generic", "instance"])
@pytest.mark.parametrize("key", REMOVED_AUTHORIZATION_KEYS)
def test_each_removed_authorization_key_is_refused_by_name(tmp_path, kind, key):
    authorization = {"enabled": True, key: OLD_VALUES[key]}
    path = write_document(tmp_path, kind, authorization=authorization)
    with pytest.raises(ValueError) as error:
        load_runtime_config_file(path)
    assert_refused_by_name(error, f"authorization.{key}")


@pytest.mark.parametrize("kind", ["generic", "instance"])
def test_the_human_interactions_block_is_refused_and_names_the_moved_limits(tmp_path, kind):
    path = write_document(
        tmp_path,
        kind,
        human_interactions={"enabled": True, "workflow_max_steps": 10},
    )
    with pytest.raises(ValueError) as error:
        load_runtime_config_file(path)
    assert_refused_by_name(error, "human_interactions")
    assert "human_interactions.workflow_max_steps moved to access_workflows.max_steps" in str(error.value)


def test_every_removed_key_is_named_in_one_message(tmp_path):
    path = write_document(
        tmp_path,
        "instance",
        authorization={"enabled": True, **OLD_VALUES},
        human_interactions={"enabled": True},
    )
    with pytest.raises(ValueError) as error:
        GatewayInstanceConfig.load(path)
    assert_refused_by_name(
        error,
        *(f"authorization.{key}" for key in REMOVED_AUTHORIZATION_KEYS),
        "human_interactions",
    )


def test_agent_type_overrides_of_the_removed_block_are_refused(tmp_path):
    path = write_document(
        tmp_path,
        "generic",
        agent_types={"TeamArchitect": {"human_interactions": {"enabled": False}}},
    )
    with pytest.raises(ValueError) as error:
        load_runtime_config_file(path)
    assert_refused_by_name(error, "agent_types.TeamArchitect.human_interactions")


def test_configs_built_in_memory_are_checked_too():
    config = Config({"schema_version": CONFIG_SCHEMA_VERSION, "authorization": {"emergency_deny": {}}})
    with pytest.raises(ValueError) as error:
        resolve_agent_type_settings(config, "TeamArchitect")
    assert_refused_by_name(error, "authorization.emergency_deny")


@pytest.mark.parametrize("kind", ["generic", "instance"])
@pytest.mark.parametrize("key", REMOVED_TOOL_POLICY_KEYS)
def test_each_removed_tool_policy_switch_is_refused_by_name(tmp_path, kind, key):
    tool_policy = {key: OLD_TOOL_POLICY_VALUES[key]}
    path = write_document(tmp_path, kind, tool_policy=tool_policy)
    with pytest.raises(ValueError) as error:
        load_runtime_config_file(path)
    assert_refused_by_name(error, f"tool_policy.{key}")
    # The message also names what replaces the switches.
    assert REMOVED_TOOL_POLICY_GUIDANCE in str(error.value)


@pytest.mark.parametrize("kind", ["generic", "instance"])
def test_all_tool_policy_switches_are_named_in_one_message(tmp_path, kind):
    path = write_document(tmp_path, kind, tool_policy=dict(OLD_TOOL_POLICY_VALUES))
    with pytest.raises(ValueError) as error:
        load_runtime_config_file(path)
    assert_refused_by_name(error, *(f"tool_policy.{key}" for key in REMOVED_TOOL_POLICY_KEYS))


def test_instance_config_load_refuses_a_tool_policy_switch(tmp_path):
    path = write_document(tmp_path, "instance", tool_policy={"restrict_to_workspace": False})
    with pytest.raises(ValueError) as error:
        GatewayInstanceConfig.load(path)
    assert_refused_by_name(error, "tool_policy.restrict_to_workspace")


def test_in_memory_configs_with_a_tool_policy_switch_are_refused():
    config = Config({"schema_version": CONFIG_SCHEMA_VERSION, "tool_policy": {"allow_shell_commands": True}})
    with pytest.raises(ValueError) as error:
        resolve_agent_type_settings(config, "TeamArchitect")
    assert_refused_by_name(error, "tool_policy.allow_shell_commands")


@pytest.mark.parametrize("variable", ["TOOL_RESTRICT_TO_WORKSPACE", "TOOL_DENY_PATHS", "TOOL_DENIED_PATHS"])
def test_the_removed_file_access_variables_stop_start_up(variable: str) -> None:
    from runtime_settings.governance import reject_removed_file_access_env_vars

    reject_removed_file_access_env_vars({"PATH": "/usr/bin"})
    with pytest.raises(ValueError, match=variable) as refused:
        reject_removed_file_access_env_vars({variable: "false"})
    assert "/access folders add" in str(refused.value)
