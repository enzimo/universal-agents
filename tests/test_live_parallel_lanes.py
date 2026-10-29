"""A request from a parallel thread runs while another turn of the team lead is held.

Over real NATS, a real team-lead agent receives an ordinary chat request whose
model turn is held open, then a request sent in a thread started with Launch
parallel thread (``GatewayContext.parallel_thread``). The parallel request's
turn must run and finish while the ordinary turn is still held, in a lane of
its own, reading its own task (custom_agents.execution_lanes). A worker's
question about the parallel request then waits in the deferred inbox and is
answered in that request's lane, still while the ordinary turn is held. Only
the model call is scripted; admission, the deferred inbox, the lanes and the
queue drain are the production ones.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_agents import task_message_runtime
from custom_agents.conversation_store import ConversationStore
from custom_agents.execution_lanes import parallel_lane
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from custom_tools.comms_tools import TeamComms
from custom_tools.runtime_context import bound_task_id
from runtime_hosting.agent_execution import MAIN_LANE, current_lane
from test_live_nats_replacement_semantics import _adapter, _subscribe_queue, _team
from test_turn_action_outbox import _make_agent


async def _wait_until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Condition was not reached in time.")
        await asyncio.sleep(0.02)


def _request(task_id: str, reply: str, agent_id: str, *, parallel: bool) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=task_id),
        msg_header=MessageHeader(from_agent_id="gateway", to_routing_path=agent_id, response_routing_key=reply),
        msg_meta=MessageMeta(
            msg_correlation_id=f"corr_{task_id}",
            conversation_id=f"conversation_{task_id}",
            requester_type=RequesterType.USER_REQUESTER,
            is_response_requested=True,
            gateway_context=GatewayContext(
                canonical_user_id="owner", delivery_mode="interactive", parallel_thread=parallel
            ),
        ),
    )


@pytest.mark.live
@pytest.mark.integration
@pytest.mark.nats
@pytest.mark.asyncio
async def test_a_parallel_threads_request_runs_while_another_turn_is_held(tmp_path, monkeypatch):
    team = _team("parallel")
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"), max_queued_top_level_tasks=10)
    manager.set_owner_identity("lead", "Lead", team, "TeamArchitect")
    agent = _make_agent(tmp_path, task_manager=manager)
    agent.team_id = team
    agent.task_manager.set_owner_identity(agent.id, "Lead", team, "TeamArchitect")
    assert manager.parallel_lanes_enabled, "the team lead opens parallel lanes"
    adapter = _adapter(team)
    ordinary_held, release_ordinary = asyncio.Event(), asyncio.Event()
    running: set[str] = set()
    overlaps: list[frozenset[str]] = []
    seen: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    finished: list[str] = []

    async def scripted_turn(content, header, meta):
        task_id = agent._history_task_id_for_content(content)
        agent._set_active_execution_context(task_id)
        try:
            running.add(task_id)
            overlaps.append(frozenset(running))
            seen[(type(content).__name__, task_id)] = (bound_task_id(), current_lane())
            if task_id == "ordinary":
                ordinary_held.set()
                await release_ordinary.wait()
            if isinstance(content, TaskQuery):
                return TaskQuery(
                    task_query_id=content.task_query_id,
                    parent_task_id=content.parent_task_id,
                    query_type=MessageMetaType.RESPONSE,
                    message="Use the team account.",
                )
            return content.model_copy(update={"task_type": MessageMetaType.RESPONSE, "answer": "done"})
        finally:
            running.discard(task_id)
            agent._clear_active_execution_context()

    async def apply_result(_agent, task_id, _task_info, _result):
        finished.append(task_id)
        now = datetime.now(timezone.utc).isoformat()
        if task_id == "parallel":
            # It waits for a worker it delegated to.
            await manager.operations_runtime.run(manager.update_main_task_state, task_id, ProgressState.WAITING, now)
        else:
            await manager.operations_runtime.run(manager.complete_task, task_id, now)

    agent._llm_turn_executor = SimpleNamespace(process_llm_interaction=scripted_turn)
    agent._conversation_store = ConversationStore(
        db_path=str(tmp_path / "conversations.db"), artifacts_dir=tmp_path / "conversation_artifacts"
    )
    agent._conversation_summary_generator = SimpleNamespace(summarize_turn=Mock())
    agent._conversation_compactor = SimpleNamespace(compact_if_needed=Mock())
    for name in ("_refresh_self_agent_state", "_notify_context_after_turn"):
        object.__setattr__(agent, name, AsyncMock())
    monkeypatch.setattr(task_message_runtime, "_apply_task_processing_result", apply_result)
    object.__setattr__(agent, "_drain_due_turn_action_outbox", AsyncMock(return_value=False))
    agent._nats_transport_adapter = adapter
    reply = f"{team}_gateway_response"
    try:
        await adapter.connect(reconcile_topology=True)
        await adapter.subscribe_route(
            name=f"{agent.id}:direct", routing_key=agent.id, handler=agent.on_nats_delivery,
            durable_name=adapter.mapper.durable_name(f"agent_{agent.id}"),
        )
        await _subscribe_queue(adapter, name="responses", routing_key=reply, durable_name=adapter.mapper.durable_name("responses"))
        agent.team_comms = TeamComms(team_id=team, nats_adapter=adapter)

        await agent.team_comms.send_message(message=_request("ordinary", reply, agent.id, parallel=False), routing_key=agent.id)
        await asyncio.wait_for(ordinary_held.wait(), 5)

        # The parallel request runs and finishes while the ordinary turn is held.
        await agent.team_comms.send_message(message=_request("parallel", reply, agent.id, parallel=True), routing_key=agent.id)
        await _wait_until(lambda: "parallel" in finished, 5)
        assert frozenset({"ordinary", "parallel"}) in overlaps
        assert seen[("Task", "ordinary")] == ("ordinary", MAIN_LANE)
        assert seen[("Task", "parallel")] == ("parallel", parallel_lane("parallel"))
        assert manager.get_task_info("ordinary").state == ProgressState.IN_PROGRESS, "not paused by the other request"

        # A worker's question about the parallel request is answered in its lane.
        question = TaskQuery(
            task_query_id="query_1", parent_task_id="parallel", query_type=MessageMetaType.REQUEST,
            message="Which account should I use?",
        )
        await agent.team_comms.send_message(
            message=MessageEnvelope(
                msg_content=question,
                msg_header=MessageHeader(from_agent_id="worker_1", to_routing_path=agent.id, response_routing_key=reply),
                msg_meta=MessageMeta(from_agent_name="Worker", msg_correlation_id="corr_query"),
            ),
            routing_key=agent.id,
        )
        await _wait_until(lambda: ("TaskQuery", "parallel") in seen, 5)
        assert seen[("TaskQuery", "parallel")] == ("parallel", parallel_lane("parallel"))
        assert not release_ordinary.is_set() and "ordinary" in running

        release_ordinary.set()
        await _wait_until(lambda: "ordinary" in finished, 5)
        await _wait_until(lambda: not agent.is_executing, 5)
        assert agent._parallel_lanes.parts == {}, "a lane's Strands agent goes with its worker"
    finally:
        agent._should_exit.set()
        release_ordinary.set()
        for task in (getattr(agent, "_pending_work_task", None), *agent._owned_tasks):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await adapter.close()
