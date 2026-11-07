from __future__ import annotations

import logging
from types import SimpleNamespace

from custom_agents.strands_agent import UniversalAgent
from custom_messages.contracts.control_v1 import AgentCreationRequest


def test_agent_creation_without_any_team_identity_fails_before_spawning(caplog) -> None:
    agent = SimpleNamespace(
        id="manager",
        team_id=None,
        _logger=logging.getLogger("test_agent_runtime_boundaries"),
    )
    request = AgentCreationRequest(
        named_agent_type="Researcher",
        team_id=None,
    )

    with caplog.at_level("ERROR"):
        result = UniversalAgent.handle_agent_creation_request(agent, request)

    assert not result.accepted
    assert result.results[0].reason == "agent_creation_team_id_missing"
    assert "event=agent_creation_team_id_missing" in caplog.text


