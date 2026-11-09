"""Agents asking each other over protocol 1, and their directories from the team's records (nats-server 2.15.1).

The installation authority, the team services, the team lead's process (host ``lead``) and a
startup worker's process (its own host) run on a real NATS server, each agent a stand-in with
the parts the handlers use. The lead asks the worker for its status, pauses and resumes it
and probes a task with the contract's kinds, signed with the lead's session; an ``all``
restart's capture names the worker's running task from the worker's own answer, and shows a
worker that does not answer in time as not answering; the worker gets the lead's
``skills.changed`` event; each agent's directory follows the records, and the lead fails what it
asked the worker for by name when the worker leaves.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import agent_requests, runtime_restart_service
from custom_agents.agent_host import (
    LEAD_HOST_RUNTIME,
    AgentProcessHost,
    definition_sha256,
    lead_host_profile,
    startup_agent_host_profile,
)
from custom_agents.live_controls import LIVE_CONTROL_HANDLERS
from custom_agents.runtime_restart_service import capture_runtime_restart_impact
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
        is_executing=False, current_task=None, _should_exit=asyncio.Event(), _runtime_loop=asyncio.get_running_loop(),
        known_agents=AgentStatusMap(), _agent_id_by_name={}, _logger=logging.getLogger(f"test.{agent_id}"),
        _fail_subtask_and_reconcile_parent=AsyncMock(), tasks={},
        _delegated_task_repository=SimpleNamespace(get=lambda team_id, task_id: None),
    )

    async def pause():
        agent.paused = True

    async def resume():
        agent.paused = False

    agent.pause, agent.resume, agent.exit_agent = pause, resume, AsyncMock()
    agent.get_self_status = lambda info_type=None: AgentStatus(
        self_id=agent_id, team_id=TEAM, name=name, named_agent_type=agent_type, agent_role="worker",
        is_paused=agent.paused, is_executing=agent.current_task is not None, current_task_id=agent.current_task,
        current_task_age_seconds=7 if agent.current_task else None, has_execution_capacity=True, accepting_work=True,
        status_type=MessageMetaType.RESPONSE, is_team_lead=is_lead, diagnostic={"llm_model": "model-x"})
    subtasks = list(pending or [])
    operations = SimpleNamespace(run=AsyncMock(side_effect=lambda function, *args: function(*args)))
    agent.task_manager = SimpleNamespace(
        current_task_id="", operations_runtime=operations, get_task_info=lambda task_id: agent.tasks.get(task_id),
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

        # Asked of Researcher-1 by name: sent to its own mailbox, and no worker took it yet (no record).
        subtask = SimpleNamespace(task=SimpleNamespace(task_id="sub_1"), header=SimpleNamespace(to_routing_path="worker_1"),
                                  meta=SimpleNamespace(to_agent_name="Researcher-1"))
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

        # An 'all' restart's capture asks the worker what it runs, from a tool's worker thread as the
        # restart tools run, and names its task; the lead itself is not asked.
        monkeypatch.setattr(agent_requests, "_PROCESS_HOST", lead_host)
        worker.current_task = "task_research_1"
        snapshot = await asyncio.to_thread(capture_runtime_restart_impact, lead, "all")
        [seen] = snapshot.known_team_agents
        assert (seen.agent_id, seen.name, seen.answered) == ("worker_1", "Researcher-1", True)
        assert (seen.is_executing, seen.current_task_id, seen.current_task_age_seconds) == (True, "task_research_1", 7)
        assert snapshot.interrupted_work() == {"task_research_1": "running on Researcher-1"}
        # A worker that does not answer in time is shown as not answering, within the one short wait.
        monkeypatch.setattr(runtime_restart_service, "TEAM_STATUS_TIMEOUT_SECONDS", 1.0)
        released = asyncio.Event()

        async def late_status(agent, verified):
            await released.wait()
            return await LIVE_CONTROL_HANDLERS["agent.status"](agent, verified)

        worker_host.handlers["agent.status"] = late_status
        started = asyncio.get_running_loop().time()
        snapshot = await asyncio.to_thread(capture_runtime_restart_impact, lead, "all")
        assert asyncio.get_running_loop().time() - started < 3.0
        [silent] = snapshot.known_team_agents
        assert (silent.answered, silent.is_executing) == (False, None)
        assert snapshot.interrupted_work() == {
            "worker_1#not-answering": "Researcher-1 did not answer, so its work is unknown"}
        released.set()
        worker_host.handlers["agent.status"] = LIVE_CONTROL_HANDLERS["agent.status"]
        worker.current_task = None

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


@pytest.mark.asyncio
async def test_a_worker_on_another_contract_gets_no_new_work_still_answers_probes_and_is_given_up(
    broker: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,  # noqa: F811 - the shared live NATS fixture
) -> None:
    """Contract README "Versions" and plan 13B.12, with two agent hosts: the team lead's process (the lead and a
    Researcher worker) and a startup Researcher's process that runs another contract.

    The lead's directory keeps the other worker, so the watchdog's ``task.probe`` about the task it holds is
    answered, but marks it as taking no work, so delegation picks the lead's own worker. Its host drains: its
    record says ``draining``, the next task on the type's queue goes to the lead's worker, the task it held is
    handed back and run there, and then it gives up its agent (``drained``) and renews to hold none.
    """

    from nats.js.api import RetentionPolicy, StreamConfig

    from custom_agents.team_directory import live_agent_status
    from tests.test_live_agent_hosts import _QueueWorker
    from tests.test_live_team_services import OTHER_CONTRACT, run_on_another_contract

    from custom_agents import turn_registry

    monkeypatch.setattr(turn_registry, "executing_task_ids", lambda agent: set())
    layout = InstanceLayout.at(tmp_path / "instance")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect", "Researcher"), 2)
    worker_profile = startup_agent_host_profile(TEAM, WORKER_RUNTIME, "Researcher")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), lead_profile, worker_profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))
        js = (await connect(stack, broker, "work-queue")).jetstream()
        await js.add_stream(StreamConfig(name="WORK_Researcher", subjects=["work.Researcher"],
                                         retention=RetentionPolicy.WORK_QUEUE))

        lead = stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True)
        ours = _QueueWorker("worker_a", js, runs=True)
        ours.agent = stand_in("worker_a", "Researcher-A", "Researcher", is_lead=False)
        theirs = _QueueWorker("worker_b", js, runs=False)
        theirs.agent = stand_in("worker_b", "Researcher-B", "Researcher", is_lead=False)
        lead_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
        worker_host = AgentProcessHost(layout, worker_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS),
                                       drain_agent=theirs.drain, holds_work=theirs.holds_work, drain_poll_seconds=0.2)
        lead_nc = await connect(stack, broker, LEAD_HOST_RUNTIME)
        lead_host.attach(lead, agent_type="TeamArchitect", definition=DEFINITION, nc=lead_nc, state=("idle", True))
        lead_host.attach(ours.agent, agent_type="Researcher", definition=DEFINITION, nc=lead_nc, state=("idle", True))
        worker_host.attach(theirs.agent, agent_type="Researcher", definition=DEFINITION,
                           nc=await connect(stack, broker, WORKER_RUNTIME), state=("idle", True))
        await _until(lambda: {"worker_a", "worker_b"} <= set(lead.known_agents.agent_map))
        monkeypatch.setattr(agent_requests, "_PROCESS_HOST", lead_host)
        takers = [asyncio.create_task(worker.take(stop)) for worker in (ours, theirs)]

        # The other worker takes the first task and holds it; then the lead's worker takes tasks too.
        theirs.taking.set()
        await js.publish("work.Researcher", b"task-1")
        await _until(lambda: len(theirs.held) == 1)
        theirs.agent.tasks["task-1"] = SimpleNamespace(state="accepted", task=SimpleNamespace(task_state=[]))
        ours.taking.set()

        with caplog.at_level(logging.WARNING):
            assert worker_host.host is not None
            run_on_another_contract(worker_host.host)
            await _until(lambda: theirs.drained.is_set(), timeout=30)  # the team services' record, within 10 s
            await worker_host.host.report_now()  # its record names its contract now
            await _until(lambda: lead.known_agents.agent_map["worker_b"].accepting_work is False)

            # Still listed, taking no work: delegation picks the lead's own worker.
            assert lead_host.team_agents is not None
            assert lead_host.team_agents.agent("worker_b").other_contract == OTHER_CONTRACT
            assert live_agent_status(lead, "Researcher").self_id == "worker_a"
            assert live_agent_status(lead, "Researcher", "Researcher-B") is None
            # The watchdog's probe about the task it holds is still answered.
            probe = await lead_host.ask("task.probe", {"task_id": "task-1"}, "worker_b", sender_agent="lead_1")
            assert (probe["known"], probe["state"]) == (True, "accepted")
            # No new work: the next task on the type's queue goes to the lead's worker.
            await js.publish("work.Researcher", b"task-2")
            await _until(lambda: ours.ran == ["task-2"])

            # It hands its task back; the lead's worker runs it; the other host gives its agent up.
            await theirs.hand_back()
            await _until(lambda: ours.ran == ["task-2", "task-1"])
            await _until(lambda: worker_host.host.state == "drained" and not worker_host.agents, timeout=20)
            await _until(lambda: "worker_b" not in lead.known_agents.agent_map)
            assert worker_host.host.identity.certificate.agent_types == frozenset()

        # The lead's process logged the other host once, with both digests, not at every record it wrote.
        logged = [record.getMessage() for record in caplog.records
                  if "event=team_agents_host_other_contract" in record.getMessage()
                  and f"host={WORKER_RUNTIME} " in record.getMessage()]
        assert len(logged) == 1 and OTHER_CONTRACT in logged[0]

        for taker in takers:
            taker.cancel()
        await worker_host.stop()
        await lead_host.stop()
        stop.set()
        await asyncio.wait_for(serving, timeout=10)
