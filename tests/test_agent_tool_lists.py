"""The operator's tool allow and block lists name real tool functions (runtime_settings.agents.check_tool_names).

The lists match exact function names. A misspelled or renamed entry used to
match nothing: a block list quietly stopped blocking and an allow list quietly
hid a tool. Loading settings now refuses such an entry and names it.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from assistant_gateway.runtime_config import load_runtime_config_file
from config_loader import CONFIG_SCHEMA_VERSION, load_config, resolve_agent_type_settings
from runtime_settings.agents import AgentRuntimeDefaultsSettings, AgentRuntimeOverrideSettings


@pytest.mark.parametrize("model", [AgentRuntimeDefaultsSettings, AgentRuntimeOverrideSettings])
def test_tool_lists_take_real_function_names(model) -> None:
    # A provider function, a dispatcher and the skills tool are all tool functions.
    settings = model(tool_allowlist=["shell", "secure_cli", "skills"], tool_blocklist=["update_scheduled_job"])

    assert settings.tool_allowlist == ["shell", "secure_cli", "skills"]
    assert settings.tool_blocklist == ["update_scheduled_job"]
    assert model(tool_allowlist=[]).tool_allowlist == [], "[] still means no tools"


@pytest.mark.parametrize("model", [AgentRuntimeDefaultsSettings, AgentRuntimeOverrideSettings])
@pytest.mark.parametrize("field", ["tool_allowlist", "tool_blocklist"])
def test_a_name_that_is_no_tool_function_is_refused(model, field) -> None:
    with pytest.raises(ValidationError, match=rf"agent\.{field} names delete_everything, shel, which are not a tool function"):
        model(**{field: ["shell", "shel", "delete_everything"]})


def test_the_instance_configuration_refuses_it_at_load(tmp_path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    config = GatewayInstanceConfig.create(
        instance_name="tool-lists", instance_root=str(tmp_path / "instance"), team_id="team_lists", timezone="UTC"
    )
    path = config.save(tmp_path / "instance" / "instance_config.yaml")
    path.write_text(path.read_text().replace("tool_blocklist: []", "tool_blocklist:\n  - create_usermem_old"))

    with pytest.raises(ValidationError, match="create_usermem_old, which is not a tool function"):
        GatewayInstanceConfig.load(path)


@pytest.mark.parametrize("field", ["tool_allowlist", "tool_blocklist"])
@pytest.mark.parametrize("location", ["defaults", "unused_type", "overridden_defaults"])
def test_generic_config_refuses_unknown_names_before_selecting_an_agent(tmp_path, field, location) -> None:
    payload = {"schema_version": CONFIG_SCHEMA_VERSION}
    if location == "unused_type":
        payload["agent_types"] = {"WebResearchAgent": {"agent": {field: ["shel"]}}}
        setting_path = f"agent_types.WebResearchAgent.agent.{field}"
    else:
        payload["agent"] = {field: ["shel"]}
        setting_path = f"agent.{field}"
        if location == "overridden_defaults":
            payload["agent_types"] = {"TeamArchitect": {"agent": {field: []}}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match=rf"agent\.{field} names shel") as raised:
        load_config(str(path), search_default_paths=False)

    assert ".".join(str(part) for part in raised.value.errors()[0]["loc"]) == setting_path


def test_shared_config_loader_refuses_an_invalid_unused_agent_type(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({
            "schema_version": CONFIG_SCHEMA_VERSION,
            "agent_types": {"WebResearchAgent": {"agent": {"tool_blocklist": ["shel"]}}},
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="agent.tool_blocklist names shel"):
        load_runtime_config_file(path)


def test_generic_tool_list_validation_preserves_values_and_override_presence(tmp_path) -> None:
    agent = {"tool_allowlist": ["shell", "secure_cli", "skills"], "tool_blocklist": ["update_scheduled_job"]}
    agent_types = {
        "TeamArchitect": {"agent": {"tool_allowlist": [], "tool_blocklist": []}},
        "WebResearchAgent": {"agent": {"tool_allowlist": None, "tool_blocklist": None}},
        "BackgroundOperationsAgent": {"agent": {}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"schema_version": CONFIG_SCHEMA_VERSION, "agent": agent, "agent_types": agent_types}),
        encoding="utf-8",
    )

    loaded = load_config(str(path), search_default_paths=False)

    assert loaded.get("agent.tool_allowlist") == agent["tool_allowlist"]
    assert loaded.get("agent.tool_blocklist") == agent["tool_blocklist"]
    assert loaded.to_dict()["agent_types"] == agent_types
    lead = resolve_agent_type_settings(loaded, "TeamArchitect")
    assert lead["tool_allowlist"] == [] and lead["tool_blocklist"] == []
    inherited = resolve_agent_type_settings(loaded, "BackgroundOperationsAgent")
    assert inherited["tool_allowlist"] == agent["tool_allowlist"]
    assert inherited["tool_blocklist"] == agent["tool_blocklist"]
