from __future__ import annotations

import pytest

from unittest.mock import MagicMock, patch

from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType


def _task(task_id: str) -> Task:
    return Task(
        task_id=task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement=f"task {task_id}",
    )


def _header() -> MessageHeader:
    return MessageHeader(
        from_agent_id="user_queue",
        to_routing_path="manager_queue",
        response_routing_key="user_queue",
    )


def _meta() -> MessageMeta:
    return MessageMeta(from_agent_name="user")


@pytest.mark.asyncio
@patch("custom_agents.strands_agent.Agent")
async def test_agent_logs_deferred_message_backlog_threshold_and_recovery(
    mock_strands_agent, tmp_path, caplog
):
    from custom_agents.strands_agent import UniversalAgent

    with patch(
        "custom_agents.strands_agent.UniversalAgent._configure_model"
    ) as mock_model:
        mock_model.return_value = MagicMock()
        agent = UniversalAgent(
            role="Test Agent",
            tools=[],
            team_id="test_team",
            named_agent_type="TestAgent",
            deferred_message_warning_threshold=2,
        )

    envelope_a = MessageEnvelope(
        msg_content=_task("task_da"), msg_meta=_meta(), msg_header=_header()
    )
    envelope_b = MessageEnvelope(
        msg_content=_task("task_db"), msg_meta=_meta(), msg_header=_header()
    )

    with caplog.at_level("WARNING"):
        await agent.deferred_inbox.admit(envelope_a)
        await agent.deferred_inbox.admit(envelope_b)

    assert "Deferred message backlog threshold reached" in caplog.text

    with caplog.at_level("INFO"):
        await agent.deferred_inbox.clear()

    assert "Deferred message backlog recovered" in caplog.text
