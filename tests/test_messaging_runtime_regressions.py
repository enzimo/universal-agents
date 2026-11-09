"""Delivery, claim and steering regressions at their runtime boundaries."""

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_gateway.application.deferred_delivery import (
    GatewayDeferredDeliveryApplication,
    GatewayDeferredDeliverySettings,
)
from custom_agents.delegated_work_runtime import send_task_claim
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import (
    consume_pending_proactive_guidance_message,
    handle_task_steering_message,
)
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, RequesterType, TaskSteeringType
from custom_tools.providers import agent_management


@pytest.mark.asyncio
async def test_receipt_uses_durable_lead_route_before_discovery():
    manager = SimpleNamespace(
        manager_id=None,
        settings=SimpleNamespace(team_id="team"),
        publish_durable=AsyncMock(),
    )
    application = GatewayDeferredDeliveryApplication(
        settings=GatewayDeferredDeliverySettings(instance_name="example", response_route="gateway_response"),
        delivery=None, sessions=None, manager=manager, event_output=None, clock=None,
    )
    envelope = MessageEnvelope(
        msg_content=Task(task_id="result", task_type=MessageMetaType.RESPONSE),
        msg_header=MessageHeader(from_agent_id="departed-lead"),
        msg_meta=MessageMeta(conversation_id="conversation", requester_type=RequesterType.TIMER_REQUESTER),
    )
    kwargs = dict(envelope=envelope, delivery_state="delivered", delivery_via="digest", delivered_at=datetime.now(timezone.utc).isoformat())
    await application.send_receipt(**kwargs)
    await application.send_receipt(**kwargs)
    assert manager.publish_durable.await_count == 2
    first, second = manager.publish_durable.await_args_list
    assert first.kwargs["routing_key"] == "team"
    assert first.args[0].msg_header.idempotency_key
    assert first.args[0].msg_header.idempotency_key == second.args[0].msg_header.idempotency_key


@pytest.mark.asyncio
async def test_same_worker_can_announce_distinct_admissions_of_one_task():
    agent = SimpleNamespace(id="worker", team_id="team", name="Worker", named_agent_type="ResearchAgent", team_comms=SimpleNamespace(send_message=AsyncMock()), _logger=logging.getLogger(__name__))
    for _ in range(2):
        assert await send_task_claim(agent, task_id="child", parent_task_id="root", header=MessageHeader(response_routing_key="lead"), meta=MessageMeta(requester_type=RequesterType.AGENT_REQUESTER, is_response_requested=True))
    first, second = agent.team_comms.send_message.await_args_list
    assert first.kwargs["message"].msg_header.idempotency_key != second.kwargs["message"].msg_header.idempotency_key


def test_live_refresh_without_target_asks_each_matching_agent(monkeypatch):
    status = AgentStatus(self_id="worker", team_id="team", name="Worker", agent_role="Researcher", named_agent_type="ResearchAgent", status_type=MessageMetaType.RESPONSE, is_paused=False, is_busy=False)
    asked = []
    agent = SimpleNamespace(
        id="lead", team_id="team", name="Lead", role="Manager", named_agent_type="TeamArchitect", paused=False, is_executing=False,
        task_manager=SimpleNamespace(current_task_id=None),
        _agent_state_store=SimpleNamespace(query=lambda filters: [status]),
        _agent_id_by_name={},
    )

    async def ask_agent(asker, kind, body, target, *, timeout=None):
        asked.append((asker.id, kind, body, target))
        return {"outcome": "ok", "agent_type": "ResearchAgent", "is_lead": False, "paused": False}

    from custom_agents import agent_requests

    monkeypatch.setattr(agent_requests, "ask_agent", ask_agent)
    monkeypatch.setattr(agent_requests, "_PROCESS_HOST", SimpleNamespace(cannot_ask=lambda agent_id: None))
    monkeypatch.setattr(agent_management, "get_agent_ref", lambda: agent)

    def run(_agent, coroutine, *, timeout=15.0):
        import asyncio
        return asyncio.run(coroutine)

    monkeypatch.setattr(agent_management, "_run_agent_coroutine", run)
    result = json.loads(agent_management.query_agent_state(live="true", info_level="full"))
    # The matching agent is asked itself (agent.status), and its answer is returned.
    assert asked == [("lead", "agent.status", {"level": "full"}, "worker")]
    assert result["live_status"]["worker"]["agent_type"] == "ResearchAgent"


def test_live_status_asks_every_agent_at_once_and_keeps_each_answer_apart(monkeypatch):
    """EVID-7: one deadline for all; one agent's failure, or silence, is reported for that agent only."""

    import asyncio
    import time

    rows = [AgentStatus(self_id=agent_id, team_id="team", name=agent_id, agent_role="Researcher",
                        named_agent_type="ResearchAgent", status_type=MessageMetaType.RESPONSE, is_paused=False,
                        is_busy=False) for agent_id in ("answers", "breaks", "hangs")]
    agent = SimpleNamespace(id="lead", team_id="team", _agent_id_by_name={},
                            _agent_state_store=SimpleNamespace(query=lambda filters: rows))
    waits = []

    async def ask_agent(asker, kind, body, target, *, timeout=None):
        if target == "breaks":
            raise RuntimeError("the reply could not be decoded")
        if target == "hangs":
            await asyncio.sleep(30)
        await asyncio.sleep(0.2)
        return {"outcome": "ok", "agent_type": "ResearchAgent"}

    def run(_agent, coroutine, *, timeout=15.0):
        waits.append(timeout)
        return asyncio.run(coroutine)

    from custom_agents import agent_requests

    monkeypatch.setattr(agent_requests, "ask_agent", ask_agent)
    monkeypatch.setattr(agent_requests, "_PROCESS_HOST", SimpleNamespace(cannot_ask=lambda agent_id: None))
    monkeypatch.setattr(agent_management, "get_agent_ref", lambda: agent)
    monkeypatch.setattr(agent_management, "_run_agent_coroutine", run)
    monkeypatch.setattr(agent_management, "LIVE_STATUS_SECONDS", 0.5)

    started = time.monotonic()
    result = json.loads(agent_management.query_agent_state(live="true"))
    took = time.monotonic() - started

    answers = result["live_status"]
    assert answers["answers"]["agent_type"] == "ResearchAgent"
    assert answers["breaks"] == {"outcome": "unavailable", "message": "RuntimeError: the reply could not be decoded"}
    assert answers["hangs"]["outcome"] == "unavailable" and "no answer in time" in answers["hangs"]["message"]
    assert took < 1.5, "asked at once, under one deadline"
    assert waits == [0.5 + agent_management.CONTROL_WAIT_MARGIN_SECONDS]


def test_live_status_says_why_when_this_process_cannot_ask(monkeypatch):
    from custom_agents import agent_requests

    rows = [AgentStatus(self_id="worker", team_id="team", name="worker", agent_role="Researcher",
                        named_agent_type="ResearchAgent", status_type=MessageMetaType.RESPONSE, is_paused=False,
                        is_busy=False)]
    agent = SimpleNamespace(id="lead", team_id="team", _agent_id_by_name={},
                            _agent_state_store=SimpleNamespace(query=lambda filters: rows))
    monkeypatch.setattr(agent_requests, "_PROCESS_HOST", None)
    monkeypatch.setattr(agent_management, "get_agent_ref", lambda: agent)
    monkeypatch.setattr(agent_management, "_run_agent_coroutine",
                        lambda _agent, coroutine, *, timeout=15.0: __import__("asyncio").run(coroutine))

    result = json.loads(agent_management.query_agent_state(live="true"))

    assert result["live_status"]["worker"] == {"outcome": "unavailable", "message": agent_requests.NO_HOST.reason}


def _steering_agent(manager):
    return SimpleNamespace(
        id="lead", name="Lead", team_id="team", task_manager=manager,
        mid_turn_steering={"enabled": True}, _pending_proactive_task_steering={},
        _processed_task_steering_ids=set(), _agent_id_by_name={}, named_agent_type_queue_map={},
        _logger=logging.getLogger(__name__),
    )


@pytest.mark.asyncio
async def test_accepted_guidance_and_deduplication_survive_restart(tmp_path):
    path = str(tmp_path / "tasks.db")
    manager = TaskManager(db_path=path)
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    manager.add_task(Task(task_id="root", task_type=MessageMetaType.REQUEST), MessageHeader(), MessageMeta(), owner_person_id=None)
    steering = TaskSteering(steering_type=TaskSteeringType.PROACTIVE_GUIDANCE, target_task_id="root", guidance_text="Use the revised budget.", idempotency_key="budget-change")
    header, meta = MessageHeader(), MessageMeta(is_response_requested=False)
    await handle_task_steering_message(_steering_agent(manager), steering, header, meta)
    restored = TaskManager(db_path=path)
    restored.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    assert restored.restore_runtime_state()
    restarted = _steering_agent(restored)
    await handle_task_steering_message(restarted, steering, header, meta)
    accepted = [entry for entry in restored.get_task_info("root").task.execution_history if entry.details and entry.details.get("event") == "task_steering_proactive_guidance_accepted"]
    assert len(accepted) == 1
    assert "Use the revised budget." in await consume_pending_proactive_guidance_message(restarted, "root")
    assert await consume_pending_proactive_guidance_message(restarted, "root") == ""
    # A model checkpoint is held only in memory until its decision is saved.
    # Losing that process must present the guidance to the next turn again.
    interrupted = TaskManager(db_path=path)
    interrupted.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    assert interrupted.restore_runtime_state()
    interrupted_agent = _steering_agent(interrupted)
    assert "Use the revised budget." in await consume_pending_proactive_guidance_message(interrupted_agent, "root")
    assert interrupted.create_turn_action_batch("root", []) is not None
    finished = TaskManager(db_path=path)
    finished.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    assert finished.restore_runtime_state()
    assert await consume_pending_proactive_guidance_message(_steering_agent(finished), "root") == ""


@pytest.mark.asyncio
async def test_guidance_is_not_accepted_when_task_history_cannot_be_saved(tmp_path, monkeypatch):
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    manager.add_task(Task(task_id="root", task_type=MessageMetaType.REQUEST), MessageHeader(), MessageMeta(), owner_person_id=None)
    original_history = list(manager.get_task_info("root").task.execution_history)
    monkeypatch.setattr(manager.runtime_snapshot_coordinator, "persist_task_history", lambda _task_id: False)
    agent = _steering_agent(manager)
    steering = TaskSteering(steering_type=TaskSteeringType.PROACTIVE_GUIDANCE, target_task_id="root", guidance_text="Use the revised budget.")
    await handle_task_steering_message(agent, steering, MessageHeader(), MessageMeta(is_response_requested=False))
    assert agent._pending_proactive_task_steering == {}
    assert agent._processed_task_steering_ids == set()
    assert manager.get_task_info("root").task.execution_history == original_history
