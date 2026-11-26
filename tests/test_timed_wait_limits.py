"""How long a task may pause itself: the operator's two limits, checked once and carried to every agent.

``agent.max_wait_minutes_without_approval`` (``wait``) and
``agent.max_wait_minutes_with_approval`` (``long_wait``, after a person's
approval) live in config.yaml and instance_config.yaml
(``runtime_settings.timed_waits``). A configuration that breaks their rules
refuses to load; agents cannot stage a change to them.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from assistant_gateway.config import GatewayInstanceConfig
from config_loader import resolve_agent_type_settings
from custom_agents.runtime_settings_service import operator_only_runtime_setting_reason
from custom_agents.strands_team import create_strands_agent
from runtime_settings.agents import AgentRuntimeDefaultsSettings
from runtime_settings.timed_waits import WAIT_MINUTES_CEILING, TimedWaitLimits
from strands_main import _build_runtime_config_from_instance_config


def _instance(tmp_path: Path) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.create(
        instance_name="waits", instance_root=str(tmp_path / "instance"), team_id="team_waits", timezone="UTC"
    )


def test_defaults_allow_twenty_minutes_without_approval_and_a_day_with_it() -> None:
    assert AgentRuntimeDefaultsSettings().timed_wait_limits() == TimedWaitLimits(
        without_approval=20, with_approval=WAIT_MINUTES_CEILING
    )
    assert WAIT_MINUTES_CEILING == 1440


@pytest.mark.parametrize(
    ("without", "with_", "message"),
    [
        (20, 1441, "a task may wait at most 1440 minutes (24 hours) in total"),
        (30, 25, "must be at least agent.max_wait_minutes_without_approval (30)"),
        (0, 1440, "greater than or equal to 1"),
    ],
)
def test_a_configuration_that_breaks_the_rules_refuses_to_load(tmp_path: Path, without, with_, message) -> None:
    with pytest.raises(ValidationError, match=message.replace("(", r"\(").replace(")", r"\)")):
        AgentRuntimeDefaultsSettings(max_wait_minutes_without_approval=without, max_wait_minutes_with_approval=with_)

    config = _instance(tmp_path)
    data = config.to_yaml_dict()
    data["agent"].update(max_wait_minutes_without_approval=without, max_wait_minutes_with_approval=with_)
    with pytest.raises(ValidationError):
        GatewayInstanceConfig.model_validate(data)


def test_the_instance_limits_reach_every_agent_type(tmp_path: Path) -> None:
    config = _instance(tmp_path)
    config.agent.max_wait_minutes_without_approval = 10
    config.agent.max_wait_minutes_with_approval = 90
    runtime_config = _build_runtime_config_from_instance_config(config)

    for agent_type in ("TeamArchitect", "InternalImprovementAgent"):
        resolved = resolve_agent_type_settings(runtime_config, agent_type)
        assert (resolved["max_wait_minutes_without_approval"], resolved["max_wait_minutes_with_approval"]) == (10, 90)


@pytest.mark.parametrize(("configured", "expected"), [(None, (20, 1440)), ((5, 60), (5, 60))])
def test_every_agent_holds_the_limits_it_was_built_with(tmp_path: Path, configured, expected) -> None:
    config = {
        "team_id": "team_waits",
        "role": "Manager",
        "goal": "Run the team.",
        "backstory": "Coordinates work for the team.",
        "tools": [],
        "named_agent_type": "TeamArchitect",
        "is_team_lead": True,
        "authorization_config": {"enabled": False},
        "task_history_db_path": str(tmp_path / "task_history.db"),
        "storage_sqlite_path": str(tmp_path / "task_history.db"),
    }
    if configured is not None:
        config["max_wait_minutes_without_approval"], config["max_wait_minutes_with_approval"] = configured
    with patch("custom_agents.strands_agent.Agent"), patch(
        "custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()
    ):
        agent = create_strands_agent(config)

    limits = agent.timed_wait_limits
    assert (limits.without_approval, limits.with_approval) == expected


@pytest.mark.parametrize("path", ["agent.max_wait_minutes_without_approval", "agent.max_wait_minutes_with_approval"])
def test_agents_cannot_stage_a_change_to_the_limits(path: str) -> None:
    reason = operator_only_runtime_setting_reason(path)

    assert reason is not None and "only the operator" in reason


def test_the_example_configuration_documents_both_limits_inline() -> None:
    lines = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text().splitlines()

    for key, value in (("max_wait_minutes_without_approval", "20"), ("max_wait_minutes_with_approval", "1440")):
        line = next(line for line in lines if line.strip().startswith(f"{key}:"))
        assert line.split("#", 1)[0].split(":", 1)[1].strip() == value
        assert "#" in line, f"{key} needs an inline comment"
