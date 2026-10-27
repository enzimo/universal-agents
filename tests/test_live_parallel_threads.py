"""Chat threads share a lead while workers run, through real NATS and a real gateway.

Ordinary threads (New Chat) take turns on the lead's main lane; a thread
started with Launch parallel thread runs at the same time
(custom_agents.execution_lanes).
"""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.website_application import WebTaskListRequestV1
from assistant_gateway.application.website_tasks import list_tasks
from custom_messages.enums import ProgressState
from id_generation import new_id
from tests.broker_access_support import use_gateway_key
from tests.test_live_busy_thread_guidance import (
    WAIT_SECONDS, _answer, _ScriptedLead, _team, _Turn, _wait_until,
)
from tests.test_live_multi_agent_state_boundaries import (
    _agent, _start_agent, _stop_agents, _task_request,
)


pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.live, pytest.mark.nats]


async def test_new_thread_is_listed_while_busy_and_finishes_while_other_thread_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_gateway_key(monkeypatch)
    release_lead, release_worker = asyncio.Event(), asyncio.Event()
    turns = (
        _Turn("FIRST-REQUEST", _task_request("WORKER-REQUEST", target_named_agent_type="WebResearchAgent"), release=release_lead),
        _Turn("SECOND-REQUEST", _answer("Second thread finished.")),
        _Turn("FIRST-REQUEST", _answer("First thread finished.")),
    )
    worker_turn = _Turn("WORKER-REQUEST", _answer("Research finished."), release=release_worker)
    worker_model = _ScriptedLead(worker_turn)
    async with _team(tmp_path, *turns) as team:
        with patch("custom_agents.strands_agent.Agent", return_value=worker_model):
            worker = _agent(
                agent_id=new_id("worker"), name="researcher", team_id=team.lead.team_id,
                named_agent_type="WebResearchAgent", db_path=Path(team.lead.task_manager._db_path),
                workspace_dir=tmp_path / "workspace",
            )
        team.lead.named_agent_type_queue_map["WebResearchAgent"] = f"{worker.team_id}_WebResearchAgent"
        worker.named_agent_type_queue_map["TeamArchitect"] = f"{worker.team_id}_TeamArchitect"
        workers = [(worker, await _start_agent(worker))]
        sends = []
        try:
            first_session = team.gateway.application.start_new_session().session_key
            first = team.send("FIRST-REQUEST", session_key=first_session)
            sends.append(first)
            await asyncio.wait_for(turns[0].started.wait(), WAIT_SECONDS)
            first_id = team.root_tasks_named("FIRST-REQUEST")[0].task_id
            second_session = team.gateway.application.start_new_session().session_key
            second = team.send("SECOND-REQUEST", session_key=second_session)
            sends.append(second)
            await _wait_until(lambda: bool(team.root_tasks_named("SECOND-REQUEST")))
            second_id = team.root_tasks_named("SECOND-REQUEST")[0].task_id
            await _wait_until(lambda: team.lead.task_manager.get_task_info(second_id) is not None)

            # This is the same typed service the authenticated Tasks endpoint uses.
            inventory = await list_tasks(team.gateway._operational_controls, WebTaskListRequestV1(
                context=GatewayRouteContextV1(
                    session_key=second_session, canonical_user_id=team.config.user.canonical_user_id,
                ),
                agent_id=team.lead.id,
            ))
            assert inventory.total == 2 and inventory.current_count == 1 and inventory.queued_count == 1
            assert {row.root_task_id for row in inventory.tasks} == {first_id, second_id}
            # New Chat threads are ordinary: they share the lead's main lane.
            assert not any(row.is_parallel for row in inventory.tasks)
            assert next(row for row in inventory.tasks if row.root_task_id == second_id).queue_position == 1
            assert not turns[1].started.is_set()
            assert not team.lead.deferred_inbox.messages
            assert not release_lead.is_set()

            release_lead.set()
            await asyncio.wait_for(worker_turn.started.wait(), WAIT_SECONDS)
            second_result = await asyncio.wait_for(asyncio.shield(second), WAIT_SECONDS)
            assert second_result.task_id == second_id and second_result.text == "Second thread finished."
            assert not first.done() and not release_worker.is_set()
            assert team.lead.task_manager.get_task_info(first_id).state == ProgressState.WAITING
            assert team.request_for(first_session, "FIRST-REQUEST").run_state == "running"
            assert team.request_for(second_session, "SECOND-REQUEST").run_state == "finished"

            release_worker.set()
            first_result = await asyncio.wait_for(asyncio.shield(first), WAIT_SECONDS)
            assert first_result.task_id == first_id and first_result.text == "First thread finished."
            await _wait_until(lambda: all(
                team.lead.task_manager.get_task_from_db(task_id) is not None
                for task_id in (first_id, second_id)
            ))
            assert not team.lead_model.unexpected and not worker_model.unexpected
        finally:
            release_lead.set()
            release_worker.set()
            for send in sends:
                send.cancel()
            await asyncio.gather(*sends, return_exceptions=True)
            await _stop_agents(workers)


async def test_a_parallel_threads_request_finishes_while_an_ordinary_turn_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_gateway_key(monkeypatch)
    release_lead = asyncio.Event()
    turns = (
        _Turn("FIRST-REQUEST", _answer("First thread finished."), release=release_lead),
        _Turn("PARALLEL-REQUEST", _answer("Parallel thread finished.")),
    )
    async with _team(tmp_path, *turns) as team:
        sends = []
        # A parallel lane builds a Strands agent of its own; here it is the same scripted model.
        with patch("custom_agents.strands_agent.Agent", return_value=team.lead_model):
            try:
                first_session = team.gateway.application.start_new_session().session_key
                first = team.send("FIRST-REQUEST", session_key=first_session)
                sends.append(first)
                await asyncio.wait_for(turns[0].started.wait(), WAIT_SECONDS)

                parallel_session = team.gateway.application.start_new_session(parallel_thread=True).session_key
                parallel = team.send("PARALLEL-REQUEST", session_key=parallel_session)
                sends.append(parallel)
                parallel_result = await asyncio.wait_for(asyncio.shield(parallel), WAIT_SECONDS)

                assert parallel_result.text == "Parallel thread finished."
                assert not first.done() and not release_lead.is_set(), "the ordinary turn is still held"
                first_id = team.root_tasks_named("FIRST-REQUEST")[0].task_id
                assert team.lead.task_manager.get_task_info(first_id).state == ProgressState.IN_PROGRESS

                release_lead.set()
                first_result = await asyncio.wait_for(asyncio.shield(first), WAIT_SECONDS)
                assert first_result.text == "First thread finished."
                assert not team.lead_model.unexpected
            finally:
                release_lead.set()
                for send in sends:
                    send.cancel()
                await asyncio.gather(*sends, return_exceptions=True)
