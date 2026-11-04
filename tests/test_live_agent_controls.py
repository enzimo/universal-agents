"""Agents asking each other over protocol 1, and their directories from the team's records (nats-server 2.15.1).

The installation authority, the team services, the team lead's process (host ``lead``) and a
startup worker's process (its own host) run on a real NATS server, each agent a stand-in with
the parts the handlers use. The lead asks the worker for its status, pauses and resumes it
and probes a task with the contract's kinds, signed with the lead's session; the worker gets
the lead's ``skills.changed`` event; each agent's directory follows the records, and the lead
fails what it asked the worker for by name when the worker leaves.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.agent_host import (
    LEAD_HOST_RUNTIME,
    AgentProcessHost,
    definition_sha256,
    lead_host_profile,
    startup_agent_host_profile,
)
from custom_agents.live_controls import LIVE_CONTROL_HANDLERS
from custom_messages.contracts.control_v1 import AgentStatus, AgentStatusMap
from custom_messages.enums import MessageMetaType
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.live import RequestRefused
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401 - broker is a fixture

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

WORKER_RUNTIME = "agent-Researcher"
DEFINITION = definition_sha256("ab" * 32)


async def _until(condition, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


def stand_in(agent_id: str, name: str, agent_type: str, *, is_lead: bool, pending: list | None = None):
    agent = SimpleNamespace(
        id=agent_id, name=name, named_agent_type=agent_type, team_id=TEAM, is_team_lead=is_lead, paused=False,
        is_executing=False, _should_exit=asyncio.Event(), _runtime_loop=asyncio.get_running_loop(),
        known_agents=AgentStatusMap(), _agent_id_by_name={}, _logger=logging.getLogger(f"test.{agent_id}"),
        _fail_subtask_and_reconcile_parent=AsyncMock(), tasks={},
    )

    async def pause():
        agent.paused = True

    async def resume():
        agent.paused = False

    agent.pause, agent.resume, agent.exit_agent = pause, resume, AsyncMock()
    agent.get_self_status = lambda info_type=None: AgentStatus(
        self_id=agent_id, team_id=TEAM, name=name, named_agent_type=agent_type, agent_role="worker",
        is_paused=agent.paused, is_executing=False, has_execution_capacity=True, accepting_work=True,
        status_type=MessageMetaType.RESPONSE, is_team_lead=is_lead, diagnostic={"llm_model": "model-x"})
    subtasks = list(pending or [])
    operations = SimpleNamespace(run=AsyncMock(side_effect=lambda function, *args: function(*args)))
    agent.task_manager = SimpleNamespace(
        operations_runtime=operations, get_task_info=lambda task_id: agent.tasks.get(task_id),
        get_pending_subtasks=lambda: subtasks, get_parent_task_id=lambda task_id: "root")
    return agent


@pytest.mark.asyncio
async def test_the_lead_asks_pauses_and_probes_a_worker_and_sees_it_leave(
    broker: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811 - the shared live NATS fixture
) -> None:
    from custom_agents import skill_catalog_runtime, turn_registry

    monkeypatch.setattr(skill_catalog_runtime, "repair_agent_skill_catalog", AsyncMock(return_value=True))
    monkeypatch.setattr(turn_registry, "executing_task_ids", lambda agent: set())
    layout = InstanceLayout.at(tmp_path / "instance")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect", "Researcher"), 2)
    worker_profile = startup_agent_host_profile(TEAM, WORKER_RUNTIME, "Researcher")
    events: list[tuple[str, str, dict]] = []

    async def skills_changed(agent, verified):
        events.append((agent.id, verified.session.agent_role, dict(verified.raw["body"])))
        return {}

    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), lead_profile, worker_profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))

        subtask = SimpleNamespace(task=SimpleNamespace(task_id="sub_1"), meta=SimpleNamespace(to_agent_name="Researcher-1"))
        lead = stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True, pending=[subtask])
        worker = stand_in("worker_1", "Researcher-1", "Researcher", is_lead=False)
        lead_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS),
                                     event_handlers={"skills.changed": skills_changed})
        worker_host = AgentProcessHost(layout, worker_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS),
                                       event_handlers={"skills.changed": skills_changed})
        lead_host.attach(lead, agent_type="TeamArchitect", definition=DEFINITION,
                         nc=await connect(stack, broker, LEAD_HOST_RUNTIME), state=("idle", True))
        worker_host.attach(worker, agent_type="Researcher", definition=DEFINITION,
                           nc=await connect(stack, broker, WORKER_RUNTIME), state=("idle", True))

        # Each directory follows the records: the lead sees the worker by name, the worker sees the lead.
        await _until(lambda: "worker_1" in lead.known_agents.agent_map and "lead_1" in worker.known_agents.agent_map)
        assert lead._agent_id_by_name == {"Researcher-1": "worker_1"}
        assert worker.known_agents.agent_map["lead_1"].is_team_lead is True

        # The lead reads the worker's full status. A worker's own process may not ask (the contract's senders).
        full = await lead_host.ask("agent.status", {"level": "full"}, "worker_1", sender_agent="lead_1")
        assert full["outcome"] == "ok" and full["diagnostic_json"] == '{"llm_model": "model-x"}'
        with pytest.raises(RequestRefused) as refused:
            await worker_host.ask("agent.status", {"level": "basic"}, "lead_1", sender_agent="worker_1")
        assert refused.value.code == "wrong_sender"

        # Pause and resume, signed as the lead; the worker's record follows.
        paused = await lead_host.ask("agent.pause", {"request_id": "control-1"}, "worker_1", sender_agent="lead_1")
        assert paused == {"outcome": "ok", "state": "paused"} and worker.paused
        worker_host.report("worker_1", "paused", True)
        await _until(lambda: lead.known_agents.agent_map["worker_1"].is_paused, timeout=5)
        resumed = await lead_host.ask("agent.resume", {"request_id": "control-2"}, "worker_1", sender_agent="lead_1")
        assert resumed == {"outcome": "ok", "state": "resumed"} and not worker.paused

        # A task probe answers from the worker's own task records.
        unknown = await lead_host.ask("task.probe", {"task_id": "sub_1"}, "worker_1", sender_agent="lead_1")
        assert (unknown["known"], unknown["current"]) == (False, False)
        # Any agent may delegate, so a worker in its own process probes too (plan decision 70).
        lead.tasks["delegated_1"] = SimpleNamespace(state="in_progress", task=SimpleNamespace(task_state=[]))
        held = await worker_host.ask("task.probe", {"task_id": "delegated_1"}, "lead_1", sender_agent="worker_1")
        assert (held["known"], held["state"]) == (True, "in_progress")

        # The lead's skill change reaches every agent process once, marked as the lead's.
        body = {"operation": "install", "change_id": "skill_install_0123456789ab", "skill_name": "grill-me",
                "installed_tree_sha256": "a" * 64}
        await lead_host.announce("skills.changed", body, sender_agent="lead_1")
        await _until(lambda: {agent for agent, _, _ in events} == {"lead_1", "worker_1"})
        assert all(role == "lead" and got == body for _, role, got in events)

        # The worker leaves: the lead fails the subtask it asked Researcher-1 for, and forgets it.
        await asyncio.wrap_future(worker_host.detach("worker_1"))
        await _until(lambda: "worker_1" not in lead.known_agents.agent_map)
        await _until(lambda: lead._fail_subtask_and_reconcile_parent.await_count == 1, timeout=5)
        assert lead._fail_subtask_and_reconcile_parent.await_args.kwargs["subtask_info"] is subtask

        await worker_host.stop()
        await lead_host.stop()
        stop.set()
        await asyncio.wait_for(serving, timeout=10)
