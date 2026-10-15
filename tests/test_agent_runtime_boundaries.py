from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from custom_agents.strands_agent import UniversalAgent
from custom_messages.contracts.control_v1 import AgentCreationRequest, AgentStatus
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import MessageMetaType


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


def test_agent_status_without_identity_is_not_inserted_into_known_agents(
    caplog,
) -> None:
    known_agents = SimpleNamespace(agent_map={})
    agent = SimpleNamespace(
        id="manager",
        _logger=logging.getLogger("test_agent_runtime_boundaries"),
        known_agents=known_agents,
    )
    status = AgentStatus(
        named_agent_type=None,
        self_id=None,
        is_paused=False,
        name=None,
        agent_role=None,
        status_type=MessageMetaType.RESPONSE,
    )

    with caplog.at_level("ERROR"):
        asyncio.run(
            UniversalAgent.handle_agent_status_message(
                agent,
                status,
                MessageHeader(from_agent_id=None),
                MessageMeta(msg_correlation_id="corr_missing_identity"),
            )
        )

    assert known_agents.agent_map == {}
    assert "event=agent_status_identity_missing" in caplog.text
    assert "corr_missing_identity" in caplog.text
