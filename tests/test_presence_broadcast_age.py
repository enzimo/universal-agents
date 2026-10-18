"""Presence replay survives startup delays; expired control broadcasts do not."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_agents.pending_work_runtime import close_pending_work
from custom_agents.delegation_runtime import exact_delegation_route
from custom_agents.agent_state_store import AgentStateStore
from custom_messages.contracts.control_v1 import AgentStatus, ControlInstruction, Instruction
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import ControlType, MessageMetaType, ProgressState, RoutingType
from tests.pending_work_support import Delivery, DrainAgent, use_simple_queue_checks
from tests.test_turn_action_outbox import _add_active_parent_task, _make_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("status_type", [MessageMetaType.REQUEST, MessageMetaType.RESPONSE, None])
async def test_expired_broadcast_only_dispatches_agent_status(monkeypatch, status_type):
    use_simple_queue_checks(monkeypatch)
    agent = DrainAgent()
    agent.max_broadcast_age_ms = 100
    sent_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    content = (
        AgentStatus(
            self_id="other_worker", named_agent_type="Worker", team_id=agent.team_id,
            status_type=status_type, info_type=ControlType.STATUS_REQUEST_BASIC,
            is_paused=False, name="Other worker", agent_role="worker", last_seen_at=sent_at,
        )
        if status_type is not None
        else ControlInstruction(type=ControlType.CONTROL_MSG, instruction=Instruction(instruction="pause"))
    )
    delivery = Delivery(
        MessageEnvelope(
            msg_content=content,
            msg_header=MessageHeader(
                from_agent_id="other_worker", to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                timestamp=sent_at,
            ),
        ),
        RoutingType.ALL_AGENTS_ROUTE.value,
    )
    try:
        await agent.on_nats_delivery(delivery)
        assert delivery.acked
        assert len(agent.dispatched) == (1 if status_type is not None else 0)
        if status_type is not None:
            assert delivery.envelope.msg_content.last_seen_at == sent_at
    finally:
        await close_pending_work(agent)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["database", "memory", "nonlead_database"])
@pytest.mark.parametrize("same_time", [False, True])
@pytest.mark.parametrize("current_exiting", [False, True])
async def test_exit_effects_follow_the_newest_known_status(tmp_path, source, same_time, current_exiting):
    agent = _make_agent(tmp_path)
    manager = agent.task_manager
    parent = _add_active_parent_task(manager)
    child = manager.add_subtask(
        parent.task_id, Task(task_id="child", task_type=MessageMetaType.REQUEST),
        MessageHeader(), MessageMeta(to_agent_name="Target worker"),
    )
    now = datetime.now(timezone.utc)
    manager.update_main_task_state(parent.task_id, ProgressState.WAITING, now.isoformat())
    current = AgentStatus(
        self_id="worker", named_agent_type="WebResearchAgent", team_id=agent.team_id,
        status_type=MessageMetaType.RESPONSE, info_type=ControlType.STATUS_REQUEST_BASIC,
        is_paused=False, name="Target worker", agent_role="worker",
        last_seen_at=now.isoformat(), is_exiting=current_exiting,
    )
    if source in {"database", "nonlead_database"}:
        store = AgentStateStore(storage_settings=manager.storage_settings)
        store.upsert(current)
        object.__setattr__(agent, "_agent_state_store", store)
        agent.is_team_lead = source == "database"
    else:
        agent.known_agents.agent_map["worker"] = current
    incoming = current.model_copy(update={
        "last_seen_at": (now if same_time else now - timedelta(seconds=30)).isoformat(),
        "is_exiting": not current_exiting,
    })
    delivery = Delivery(
        MessageEnvelope(
            msg_content=incoming,
            msg_header=MessageHeader(
                from_agent_id="worker", to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                timestamp=incoming.last_seen_at,
            ),
        ),
        RoutingType.ALL_AGENTS_ROUTE.value,
    )
    try:
        await agent.on_nats_delivery(delivery)
        assert delivery.acked
        assert ("worker" in agent.known_agents.agent_map) is not current_exiting
        should_end_work = agent.is_team_lead and current_exiting
        assert (manager.get_task_info(parent.task_id) is not None) is not should_end_work
        assert (manager.get_task_info(child.task.task_id) is not None) is not should_end_work
        if source in {"database", "nonlead_database"}:
            assert store.get_by_id("worker").is_exiting is current_exiting
    finally:
        await close_pending_work(agent)


@pytest.mark.asyncio
async def test_current_memory_is_persisted_instead_of_an_old_exit(tmp_path):
    agent = _make_agent(tmp_path)
    store = AgentStateStore(storage_settings=agent.task_manager.storage_settings)
    object.__setattr__(agent, "_agent_state_store", store)
    now = datetime.now(timezone.utc)
    current = AgentStatus(
        self_id="worker", named_agent_type="WebResearchAgent", team_id=agent.team_id,
        status_type=MessageMetaType.RESPONSE, info_type=ControlType.STATUS_REQUEST_BASIC,
        is_paused=False, name="Target worker", agent_role="worker", last_seen_at=now.isoformat(),
    )
    agent.known_agents.agent_map["worker"] = current
    store.upsert(current.model_copy(update={"last_seen_at": (now - timedelta(seconds=60)).isoformat()}))
    old_exit = current.model_copy(update={
        "last_seen_at": (now - timedelta(seconds=30)).isoformat(), "is_exiting": True,
    })
    try:
        await agent.handle_agent_status_message(
            old_exit, MessageHeader(from_agent_id="worker"), MessageMeta()
        )
        assert agent.known_agents.agent_map["worker"].is_exiting is False
        assert store.get_by_id("worker").is_exiting is False
        assert store.get_by_id("worker").last_seen_at == current.last_seen_at
    finally:
        await close_pending_work(agent)


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["old", "missing", "future", "fresh", "correlated"])
async def test_replay_discovery_does_not_replace_a_live_probe_reply(tmp_path, evidence):
    agent = _make_agent(tmp_path)
    store = AgentStateStore(storage_settings=agent.task_manager.storage_settings)
    object.__setattr__(agent, "_agent_state_store", store)
    object.__setattr__(agent, "_operational_exchange_name", "ua.test_team.operational")
    agent.subtask_status_probe_timeout_seconds = 0.1
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()

    async def publish_probe(request):
        sent_at = {
            "old": old_time,
            "missing": None,
            "future": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            "fresh": datetime.now(timezone.utc).isoformat(),
            "correlated": old_time,
        }[evidence]
        status = AgentStatus(
            self_id="worker", named_agent_type="WebResearchAgent", team_id=agent.team_id,
            status_type=MessageMetaType.RESPONSE, info_type=ControlType.STATUS_REQUEST_BASIC,
            is_paused=False, name="Target worker", agent_role="worker", last_seen_at=sent_at,
        )
        store.upsert(status.model_copy(update={"last_seen_at": datetime.now(timezone.utc).isoformat()}))
        delivery = Delivery(
            MessageEnvelope(
                msg_content=status,
                msg_header=MessageHeader(
                    from_agent_id="worker", to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                    timestamp=old_time,
                ),
                msg_meta=MessageMeta(
                    msg_correlation_id=(request.msg_meta.msg_correlation_id if evidence == "correlated" else None),
                ),
            ),
            RoutingType.ALL_AGENTS_ROUTE.value,
        )
        await agent.on_nats_delivery(delivery)
        assert delivery.acked

    object.__setattr__(agent, "_send_operational_message", publish_probe)
    try:
        route = await exact_delegation_route(agent, "WebResearchAgent", "Target worker")
        assert route == ("worker" if evidence in {"fresh", "correlated"} else None)
        # Every snapshot remains available for discovery, even when it cannot
        # prove that a worker answered this particular request.
        assert "worker" in agent.known_agents.agent_map
        if evidence == "old":
            assert agent.known_agents.agent_map["worker"].last_seen_at > old_time
        assert not agent._pending_agent_status_futures
    finally:
        await close_pending_work(agent)
