import pytest
import logging
from unittest.mock import MagicMock, patch

from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import ControlType, MessageMetaType


def _make_agent(tmp_path):
    from custom_agents.strands_agent import UniversalAgent

    task_manager = TaskManager(
        db_path=str(tmp_path / "runtime_repair.db"),
        logger=logging.getLogger("TaskManager.runtime_repair"),
    )
    with (
        patch("custom_agents.strands_agent.Agent"),
        patch(
            "custom_agents.strands_agent.UniversalAgent._configure_model",
            return_value=MagicMock(),
        ),
    ):
        return UniversalAgent(
            role="Manager",
            tools=[],
            team_id="team_1",
            named_agent_type="TeamArchitect",
            is_team_lead=True,
            task_manager=task_manager,
        )


@pytest.mark.asyncio
async def test_runtime_repair_anomaly_is_aggregated_and_recorded_in_task_history(
    tmp_path,
):
    agent = _make_agent(tmp_path)
    task = Task(
        task_id="task_repair",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Do work.",
    )
    assert agent.task_manager.add_task(
        task,
        MessageHeader(from_agent_id="gateway", response_routing_key="gateway_response"),
        MessageMeta(msg_correlation_id="corr_repair"),
        owner_person_id=None,
    )

    await agent._record_runtime_repair_anomaly(
        "requester_bound_request_route_repaired",
        "Requester-bound request had no target route; runtime routed it back to the original requester.",
        related_task_id="task_repair",
        details={"fallback_route": "gateway_response"},
    )

    status = agent.get_self_status(info_type=ControlType.STATUS_REQUEST_FULL)
    assert status.diagnostic["runtime_repair_anomaly_counts"] == {
        "requester_bound_request_route_repaired": 1
    }
    assert status.diagnostic["runtime_repair_anomalies"]["recent"][0]["event"] == (
        "requester_bound_request_route_repaired"
    )

    history_entry = agent.task_manager.get_task_info(
        "task_repair"
    ).task.execution_history[-1]
    assert history_entry.entry_type.value == "error_event"
    assert history_entry.details["event"] == "runtime_repair_anomaly"
    assert (
        history_entry.details["repair_event"]
        == "requester_bound_request_route_repaired"
    )
