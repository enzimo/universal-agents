"""TeamArchitect's worker pool tools (``fn:worker_pools``) on a real TaskManager and SQLite."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.delegated_work_runtime import delegated_tasks, record_publication
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from custom_tools.providers import worker_pools as tools
from tests.task_work_support import bind_task, set_agent_ref
from runtime_settings.worker_pools import WorkerPoolsConfig


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def lead(tmp_path, monkeypatch):
    manager = TaskManager(db_path=str(tmp_path / "lead.db"))
    manager.set_owner_identity("lead", "lead", "team", "TeamArchitect")
    context = GatewayContext(canonical_user_id="jordan", session_key="s")
    agent = SimpleNamespace(
        id="lead",
        name="lead",
        team_id="team",
        task_manager=manager,
        team_comms=SimpleNamespace(send_message=AsyncMock(), delete_work_message=AsyncMock(return_value=True)),
        named_agent_type_queue_map={"WebResearchAgent": "team_WebResearchAgent"},
        _local_team_runtime=SimpleNamespace(agent_types=lambda: ("TeamArchitect", "WebResearchAgent")),
        _worker_pool_snapshot=None,
        _logger=logging.getLogger("worker-pool-tools-test"),
    )
    for root in ("root", "other_root"):
        manager.add_task(
            Task(task_id=root, task_type=MessageMetaType.REQUEST, problem_statement="Research"),
            MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
            MessageMeta(gateway_context=context),
            owner_person_id="jordan",
        )
        child = f"{root}_child"
        manager.add_subtask(
            root,
            Task(task_id=child, parent_task_id=root, task_type=MessageMetaType.REQUEST, problem_statement="Fetch prices"),
            MessageHeader(from_agent_id="lead", to_routing_path="team_WebResearchAgent"),
            MessageMeta(to_named_agent_type="WebResearchAgent", gateway_context=context),
        )
        record_publication(
            agent,
            subtask_id=child,
            parent_task_id=root,
            named_agent_type="WebResearchAgent",
            target_agent_name=None,
            publish_sequence=17 if root == "root" else 18,
        )
    manager.update_main_task_state("root", ProgressState.IN_PROGRESS, _iso())
    manager.task_state.current_in_progress_task_id = "root"
    bind_task(manager.task_state.current_in_progress_task_id)
    monkeypatch.setattr(
        "custom_agents.worker_pool_settings.current_worker_pool_settings",
        lambda _agent: WorkerPoolsConfig(max_workers_per_type_ceiling=8),
    )
    notices: list[dict] = []
    monkeypatch.setattr(
        "custom_agents.access_runtime.send_runtime_notice_once",
        lambda _agent, **kwargs: notices.append(kwargs) or True,
    )
    monkeypatch.setattr("custom_agents.access_runtime.verified_person_id", lambda _agent: "jordan")
    agent.notices = notices
    set_agent_ref(agent)
    yield agent
    set_agent_ref(None)


@pytest.mark.asyncio
async def test_pool_sizes_change_inside_the_ceiling_and_administrators_are_told(lead):
    temporary = json.loads(
        await tools.set_worker_pool_limit(
            agent_type="WebResearchAgent", max_workers=8, reason="24 research tasks waiting", for_minutes=60
        )
    )
    assert temporary["pool_size"]["effective"] == 8
    assert temporary["pool_size"]["source"] == "temporary"
    (notice,) = lead.notices
    assert notice["kind"].value == "worker_pool_changed"
    assert "WebResearchAgent workers to 8 until" in notice["title"]
    assert "24 research tasks waiting" in notice["summary"]
    assert notice["roles"] == ("admin",)

    lasting = json.loads(
        await tools.set_worker_pool_limit(agent_type="WebResearchAgent", max_workers=2, reason="provider limits calls")
    )
    assert (lasting["pool_size"]["effective"], lasting["pool_size"]["source"]) == (2, "lasting")
    team = json.loads(await tools.set_worker_pool_limit(agent_type="team", max_workers=20, reason="smaller host"))
    assert team["scope"] == "*"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_type", "max_workers", "reason", "message"),
    [
        ("WebResearchAgent", 9, "more", "above the operator ceiling of 8"),
        ("TeamArchitect", 2, "more", "not a worker type"),
        ("UnknownAgent", 2, "more", "not a worker type"),
        ("WebResearchAgent", 3, "  ", "Give a reason"),
    ],
)
async def test_pool_size_changes_outside_the_rules_are_refused(lead, agent_type, max_workers, reason, message):
    result = json.loads(
        await tools.set_worker_pool_limit(agent_type=agent_type, max_workers=max_workers, reason=reason)
    )
    assert message in result["error"]
    assert lead.notices == []


@pytest.mark.asyncio
async def test_inspect_delegated_work_shows_only_the_current_request(lead):
    delegated_tasks(lead).record_claim(
        team_id="team", task_id="root_child", worker_agent_id="w1", worker_agent_name="research-1",
        claimed_at=datetime.now(timezone.utc),
    )
    result = json.loads(await tools.inspect_delegated_work())
    assert result["request"] == "root"
    assert [(row["task_id"], row["status"], row["worker"]) for row in result["tasks"]] == [
        ("root_child", "running", "research-1")
    ]
    assert result["counts"]["running"] == 1


@pytest.mark.asyncio
async def test_cancelling_a_waiting_task_withdraws_it_from_its_queue(lead):
    result = json.loads(await tools.cancel_delegated_task(task_id="root_child", reason="no longer needed"))
    assert result == {"task_id": "root_child", "status": "cancelled", "worker_told": True}
    lead.team_comms.delete_work_message.assert_awaited_once_with(17)
    assert lead.task_manager.is_execution_cancelled("root_child")


@pytest.mark.asyncio
async def test_a_task_of_another_request_cannot_be_cancelled(lead):
    result = json.loads(await tools.cancel_delegated_task(task_id="other_root_child", reason="x"))
    assert "not a delegated task of the current request" in result["error"]
    lead.team_comms.delete_work_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspect_worker_pools_explains_the_controller_view(lead):
    lead._worker_pool_snapshot = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "team": {"workers_active": 4},
        "pools": {
            "WebResearchAgent": {"tasks_waiting": 6, "workers_running": 4},
            "IdleAgent": {"tasks_waiting": 0, "workers_running": 0},
        },
    }
    result = json.loads(await tools.inspect_worker_pools())
    assert list(result["pools"]) == ["WebResearchAgent"]
    assert result["settings"]["max_workers_per_type_ceiling"] == 8
    assert "tasks_waiting" in result["explanation"]
