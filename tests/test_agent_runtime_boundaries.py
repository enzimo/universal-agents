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




def test_an_agent_definition_that_still_sets_process_broadcasts_keeps_starting(tmp_path) -> None:
    """In-place upgrade: the removed field is ignored, as every unknown field of an agent's settings is."""

    from tests.test_turn_action_outbox import _make_agent

    agent = _make_agent(tmp_path, process_broadcasts=True)

    assert "process_broadcasts" not in UniversalAgent.model_fields
    assert not hasattr(agent, "process_broadcasts")
