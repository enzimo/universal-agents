"""Live NATS: a manager-only restart keeps a running startup agent's status row.

Real UniversalAgents (with a stand-in model) on a live NATS/JetStream server and
one SQLite file. The old lead exits gracefully and a new lead starts, as when
the supervisor restarts only the manager. Before 2026-10-17 the exiting lead
deleted every status row of its team, so the startup agent, still running,
counted as gone until its next heartbeat.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest
from live_nats_client import resolve_live_transport_settings

from custom_agents.agent_liveness import owner_liveness
from custom_agents.task_manager import TaskManager
from id_generation import new_id
from runtime_settings.projection import _project_transport

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]


async def _until(check, *, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("Condition not reached in time.")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_live_manager_restart_keeps_startup_worker_rows(tmp_path):
    from custom_agents.strands_agent import UniversalAgent

    transport = resolve_live_transport_settings()
    team_id = new_id("live_rows")
    db_path = str(tmp_path / "runtime.db")
    running: dict[str, tuple[UniversalAgent, asyncio.Task]] = {}

    def start(label: str, agent_type: str, *, is_lead: bool) -> UniversalAgent:
        agent = UniversalAgent(
            **_project_transport(transport_cfg=transport.model_dump()),
            role=label, tools=[], team_id=team_id, named_agent_type=agent_type,
            is_team_lead=is_lead, skills_enabled=False, task_manager=TaskManager(db_path=db_path),
        )
        running[label] = (agent, asyncio.create_task(agent.connect()))
        return agent

    async def stop(label: str) -> None:
        agent, task = running.pop(label)
        agent.set_exit_flag()
        await asyncio.wait_for(task, timeout=15)

    with patch("custom_agents.strands_agent.Agent", return_value=MagicMock()), patch(
        "custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()
    ):
        try:
            old_lead = start("old lead", "TeamArchitect", is_lead=True)
            await _until(lambda: old_lead._worker_ready)  # the lead creates the team's streams
            worker = start("startup worker", "WebResearchAgent", is_lead=False)
            await _until(lambda: worker._worker_ready)
            store = old_lead._agent_state_store
            await _until(lambda: store.get_by_id(worker.id) is not None)

            await stop("old lead")
            kept = store.get_by_id(worker.id)
            assert kept is not None and kept.is_exiting is False
            assert store.get_by_id(old_lead.id).is_exiting is True

            new_lead = start("new lead", "TeamArchitect", is_lead=True)
            await _until(lambda: new_lead._worker_ready)
            # The worker's replayed presence keeps the time the worker sent it.
            await _until(lambda: new_lead.known_agents.agent_map.get(worker.id) is not None)
            replayed = new_lead.known_agents.agent_map[worker.id]
            assert replayed.last_seen_at <= new_lead._agent_state_store.get_by_id(worker.id).last_seen_at
            assert owner_liveness(new_lead, worker.id).gone is False
        finally:
            for label in list(running):
                with contextlib.suppress(Exception):
                    await stop(label)
