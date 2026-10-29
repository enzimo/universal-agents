"""Browser task operations use real queue custody and cancellation persistence."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.task_work_support import TurnHost
from custom_agents.manager_control_runtime import handle_manager_runtime_work_command
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.control_v1 import ManagerRuntimeWorkCommand
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from custom_agents.manager_control_runtime import CommandPerson
from test_manager_control_runtime import _CaptureComms, _gateway_signing_keys, _signed  # noqa: F401


@pytest.fixture(params=[True, False], ids=["lead", "worker"])
def agent(tmp_path, request):
    manager = TaskManager(
        db_path=str(tmp_path / "tasks.db"), max_queued_top_level_tasks=50
    )
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    return TurnHost(
        id="lead",
        name="Lead",
        team_id="team",
        is_team_lead=request.param,
        task_manager=manager,
        team_comms=_CaptureComms(),
        _logger=logging.getLogger(__name__),
        _stop_reasons_by_task_id={},
        _strands_agent=SimpleNamespace(),
        _finalize_conversation_turn=AsyncMock(),
        named_agent_type_queue_map={},
        _agent_id_by_name={},
    )


def add(agent, task_id, owner="owner", state=None):
    assert agent.task_manager.add_task(
        Task(
            task_id=task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Work " + task_id,
        ),
        MessageHeader(),
        MessageMeta(gateway_context=GatewayContext(canonical_user_id=owner)),
        owner_person_id=owner,  # whom admission verified the task works for
    )
    if state:
        agent.task_manager.update_main_task_state(
            task_id, state, "2026-09-20T12:00:00+00:00"
        )


async def command(agent, action="list_tasks", **kwargs):
    request = ManagerRuntimeWorkCommand(
        action=action,
        scope="user",
        **kwargs,
    )
    await handle_manager_runtime_work_command(
        agent,
        command=request,
        msg_header=MessageHeader(
            from_agent_id="gateway", response_routing_key="gateway"
        ),
        msg_meta=MessageMeta(
            gateway_context=_signed(GatewayContext(canonical_user_id="owner"), team_id="team", roles=["member"])
        ),
    )
    return agent.team_comms.messages[-1].msg_content


@pytest.mark.asyncio
async def test_finished_task_pages_include_stopped_agents_and_isolate_owners_and_teams(agent):
    repo = agent.task_manager.task_archive_repository
    for i in range(24):
        repo.save_completed_task(
            archive_id=f"finished-{i:02}", task_id=f"task-{i}",
            owner_agent_id="stopped-worker", owner_agent_name="Worker",
            team_id="other-team" if i == 23 else "team",
            owner_canonical_user_id="someone_else" if i == 22 else "owner",
            task_json=Task(task_id=f"task-{i}", task_type=MessageMetaType.RESPONSE, problem_statement="Finished work").model_dump_json(),
            status="failed" if i == 21 else "completed",
        )
    first = await command(agent, "list_finished_tasks", task_limit=20)
    second = await command(agent, "list_finished_tasks", task_offset=10, task_limit=10)
    last = await command(agent, "list_finished_tasks", task_offset=20, task_limit=10)
    assert first.success and first.task_total == 22
    assert len(first.finished_tasks) == 10
    assert len(second.finished_tasks) == 10
    assert len(last.finished_tasks) == 2
    assert first.finished_tasks[0].state == "failed"
    assert len({row.task_id for page in [first, second, last] for row in page.finished_tasks}) == 22
    assert all(row.owner_agent_id == "stopped-worker" for row in first.finished_tasks)


@pytest.mark.asyncio
async def test_full_queue_purge_preserves_current_and_other_owner_and_frees_capacity(
    agent,
):
    add(agent, "current", state=ProgressState.IN_PROGRESS)
    add(agent, "other", owner="someone_else")
    for index in range(48):
        add(
            agent,
            f"queued_{index}",
            state=ProgressState.WAITING if index == 0 else None,
        )
    assert agent.task_manager.is_buffer_full()
    view = await command(agent)
    assert view.success
    assert view.task_total == 49
    assert view.current_task_count == 1
    assert view.queued_task_count == 48
    assert len(view.roots) == 20
    assert view.roots[0].root_task_id == "current"
    assert view.roots[0].is_current
    page = await command(agent, task_offset=20)
    assert len(page.roots) == 20
    assert page.inventory_digest == view.inventory_digest
    assert not set(row.root_task_id for row in page.roots) & set(
        row.root_task_id for row in view.roots
    )
    result = await command(agent, "purge_tasks", inventory_digest=view.inventory_digest)
    assert result.success
    assert len(result.cancellation_receipts) == 48
    assert result.task_total == 1
    assert agent.task_manager.get_task_info("current") is not None
    assert agent.task_manager.get_task_info("other") is not None
    assert not agent.task_manager.is_buffer_full()
    repeated = await command(agent, "purge_tasks", inventory_digest=result.inventory_digest)
    assert repeated.success
    assert repeated.cancellation_receipts == []
    assert "No queued tasks were removed" in repeated.message
    assert agent.task_manager.is_execution_cancelled("queued_0")
    assert agent.task_manager.get_task_from_db("queued_0") is not None
    assert not agent.task_manager.add_task(
        Task(
            task_id="queued_0",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Late redelivery",
        ),
        MessageHeader(),
        MessageMeta(),
        owner_person_id=None,
    )


@pytest.mark.asyncio
async def test_waiting_task_shows_the_access_requests_it_waits_on(agent):
    from custom_agents.task_human_interaction_blockers import record_human_interaction_blocker

    add(agent, "review", state=ProgressState.WAITING)
    record_human_interaction_blocker(agent.task_manager, "review", {
        "kind": "access_request",
        "access_request_id": "areq_1",
        "access_request_ids": ["areq_1", "areq_2"],
    })
    view = await command(agent)
    assert view.roots[0].blocker == (
        "Waiting for a person to decide access request(s) in the Inbox: areq_1, areq_2."
    )


@pytest.mark.asyncio
async def test_parallel_marks_requests_from_parallel_threads_on_the_team_lead(agent):
    # The team lead runs requests from threads started with Launch parallel
    # thread in lanes of their own (custom_agents.execution_lanes).
    agent.task_manager.parallel_lanes_enabled = agent.is_team_lead
    add(agent, "ordinary", state=ProgressState.IN_PROGRESS)
    add(agent, "other-chat")
    agent.task_manager.get_task_info("other-chat").meta.conversation_id = "conversation-other"
    add(agent, "parallel")
    context = agent.task_manager.get_task_info("parallel").meta.gateway_context
    context.parallel_thread, context.delivery_mode = True, "interactive"

    labels = {row.root_task_id: row.is_parallel for row in (await command(agent)).roots}

    # Another conversation alone is not parallel work; a worker has no parallel lanes.
    assert labels == {"ordinary": False, "other-chat": False, "parallel": agent.is_team_lead}


@pytest.mark.asyncio
async def test_single_removal_is_owner_scoped_and_can_stop_current_work(agent):
    add(agent, "current", state=ProgressState.IN_PROGRESS)
    add(agent, "private", owner="someone_else")
    view = await command(agent)
    denied = await command(
        agent,
        "remove_task",
        target_task_id="private",
        inventory_digest=view.inventory_digest,
    )
    assert not denied.success
    assert agent.task_manager.get_task_info("private")
    removed = await command(
        agent,
        "remove_task",
        target_task_id="current",
        inventory_digest=view.inventory_digest,
    )
    assert removed.success
    assert removed.cancellation_receipts[0].confirmed
    assert agent.task_manager.get_task_info("current") is None


@pytest.mark.asyncio
async def test_cancelled_task_cannot_start_between_queue_selection_and_terminalization(agent):
    from functools import partial
    from custom_agents.runtime_task_controls import _reserve_queued_cancellation

    add(agent, "selected")
    assert agent.task_manager.get_next_task_to_process() == "selected"
    request = ManagerRuntimeWorkCommand(
        action="purge_tasks", scope="user", )
    assert await agent.task_manager.operations_runtime.run(
        partial(_reserve_queued_cancellation, agent.task_manager), "selected", request,
        CommandPerson(canonical_user_id="owner", team_id="team", roles=("member",)),
    )
    assert not agent.task_manager.update_main_task_state(
        "selected", ProgressState.IN_PROGRESS, "2026-09-20T12:00:00+00:00",
    )
    assert agent.task_manager.current_task_id is None


@pytest.mark.live
@pytest.mark.integration
@pytest.mark.nats
@pytest.mark.asyncio
async def test_live_task_inventory_and_purge_round_trip(agent):
    import asyncio
    from custom_messages.contracts.envelope_v1 import MessageEnvelope
    from custom_tools.comms_tools import TeamComms
    from custom_transport.subjects import NatsSubjectMapper
    from test_live_nats_replacement_semantics import _adapter, _subscribe_queue, _team

    team = _team("tasks_controls")
    agent.team_id = team
    agent.task_manager.set_owner_identity("lead", "Lead", team, "TeamArchitect")
    route, reply = f"{team}_TeamArchitect", f"{team}_gateway_response"
    adapter = _adapter(
        team,
        mapper=NatsSubjectMapper(
            team_id=team, named_agent_type_queue_map={"TeamArchitect": route}
        ),
    )
    try:
        await adapter.connect(reconcile_topology=True)
        requests = await _subscribe_queue(
            adapter,
            name="task-controls-lead",
            subject=adapter.mapper.agent_control_subject(route),
            durable=False,
        )
        responses = await _subscribe_queue(
            adapter,
            name="task-controls-response",
            routing_key=reply,
            durable_name=adapter.mapper.durable_name("tasks_response"),
        )
        agent.team_comms = TeamComms(team_id=team, nats_adapter=adapter)
        add(agent, "current", state=ProgressState.IN_PROGRESS)
        add(agent, "queued")

        async def exchange(action, **kwargs):
            request = ManagerRuntimeWorkCommand(
                action=action,
                scope="user",
                **kwargs,
            )
            envelope = MessageEnvelope(
                msg_content=request,
                msg_header=MessageHeader(
                    from_agent_id="gateway",
                    to_routing_path=route,
                    response_routing_key=reply,
                ),
                msg_meta=MessageMeta(
                    gateway_context=_signed(
                        GatewayContext(canonical_user_id="owner"), team_id=team, roles=["member"]
                    )
                ),
            )
            await agent.team_comms.send_message(message=envelope, routing_key=route)
            delivery = await asyncio.wait_for(requests.get(), timeout=5)
            incoming = delivery.envelope
            await handle_manager_runtime_work_command(
                agent,
                command=incoming.msg_content,
                msg_header=incoming.msg_header,
                msg_meta=incoming.msg_meta,
            )
            await delivery.ack()
            response = await asyncio.wait_for(responses.get(), timeout=5)
            await response.ack()
            return response.envelope.msg_content

        listed = await exchange("list_tasks")
        assert listed.task_total == 2
        result = await exchange("purge_tasks", inventory_digest=listed.inventory_digest)
        assert result.success
        assert result.task_total == 1
        assert result.roots[0].root_task_id == "current"
        assert agent.task_manager.is_execution_cancelled("queued")
        archived = await exchange("list_finished_tasks", task_limit=10)
        assert archived.success
        assert [(row.task_id, row.state) for row in archived.finished_tasks] == [("queued", "cancelled")]
    finally:
        await adapter.close()
