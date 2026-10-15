"""Agents pick up a switched team endpoint at their next turn (``custom_agents.endpoint_refresh``).

Covered: the agent type's own endpoint wins over the team's; an operator's
LITELLM_API_BASE keeps winning; a rebuild that fails keeps the old endpoint
and parts; the saved file is read again only when it changed; the turn-start
step calls the refresh.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents import endpoint_refresh, model_failover_runtime
from custom_agents.endpoint_refresh import forget_cached_configuration, refresh_team_endpoint
from custom_messages.contracts.status_v1 import EnvironmentOverridesV1
from docker.settings_environment import write_environment_overrides
from tests.test_runtime_settings_tools import _write_config

OLD = "https://litellm.example.com/v1"
NEW = "https://bifrost.example.com/v1"


@pytest.fixture(autouse=True)
def _fresh_cache():
    forget_cached_configuration()
    yield
    forget_cached_configuration()


def _save(config_path: Path, team: str, *, own_type: str | None = None, own: str | None = None) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    config = GatewayInstanceConfig.load_saved(config_path)
    data = config.model_dump(mode="json")
    data["llm"]["litellm_api_base"] = team
    if own_type:
        data.setdefault("agent_types", {}).setdefault(own_type, {})["llm"] = {"litellm_api_base": own}
    GatewayInstanceConfig.model_validate(data).save(config_path)


def _agent(config_path: Path, *, agent_type: str = "GeneralAssistant", fail: bool = False) -> SimpleNamespace:
    agent = SimpleNamespace(
        id="agent-1", named_agent_type=agent_type, instance_config_path=str(config_path), litellm_api_base=OLD,
        _strands_model="old-model", rebuilt=[],
    )

    def rebuild() -> None:
        if fail:
            agent._strands_model = "half-built"
            raise RuntimeError("the provider library refused the address")
        agent.rebuilt.append(agent.litellm_api_base)
        agent._strands_model = "new-model"

    agent._rebuild_runtime_llm_components = rebuild
    return agent


def test_the_agent_types_own_endpoint_wins_over_the_teams(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    _save(config_path, NEW, own_type="Researcher", own="https://research.example.com/v1")

    general, researcher = _agent(config_path), _agent(config_path, agent_type="Researcher")

    assert refresh_team_endpoint(general) and general.litellm_api_base == NEW
    assert refresh_team_endpoint(researcher) and researcher.litellm_api_base == "https://research.example.com/v1"


def test_an_operators_variable_keeps_winning(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    config_path = _write_config(tmp_path)
    _save(config_path, NEW)
    write_environment_overrides(
        GatewayInstanceConfig.load_saved(config_path).layout.environment_overrides_path,
        EnvironmentOverridesV1(written_at=datetime.now(timezone.utc), setting_variables=("LITELLM_API_BASE",)),
    )
    agent = _agent(config_path)

    assert refresh_team_endpoint(agent) is False and agent.litellm_api_base == OLD and agent.rebuilt == []


def test_a_failed_rebuild_keeps_the_old_endpoint_and_parts(tmp_path: Path, caplog) -> None:
    config_path = _write_config(tmp_path)
    _save(config_path, NEW)
    agent = _agent(config_path, fail=True)

    assert refresh_team_endpoint(agent) is False
    assert (agent.litellm_api_base, agent._strands_model) == (OLD, "old-model")
    assert "event=model_endpoint_switch_failed" in caplog.text


def test_the_saved_file_is_read_again_only_when_it_changed(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    _save(config_path, NEW)
    reads: list[Path] = []
    original = endpoint_refresh._load
    monkeypatch.setattr(endpoint_refresh, "_load", lambda path: reads.append(path) or original(path))
    agent = _agent(config_path)

    for _turn in range(3):
        refresh_team_endpoint(agent)
    _save(config_path, "https://third.example.com/v1")
    refresh_team_endpoint(agent)

    assert len(reads) == 2 and agent.rebuilt == [NEW, "https://third.example.com/v1"]


def test_the_turn_start_step_refreshes_the_endpoint(monkeypatch) -> None:
    seen: list[object] = []
    monkeypatch.setattr(model_failover_runtime, "refresh_team_endpoint", seen.append)
    agent = SimpleNamespace(refresh_persistent_llm_override=lambda: None)
    monkeypatch.setattr(model_failover_runtime, "_config", lambda _agent: {"enabled": False})
    monkeypatch.setattr(model_failover_runtime, "_state_path", lambda _agent, _cfg: Path("/nonexistent/state.json"))

    model_failover_runtime.refresh_runtime_llm_selection(agent)

    assert seen == [agent]
