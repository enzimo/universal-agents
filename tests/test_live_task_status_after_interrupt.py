"""A worker answers task probes while an approval resume holds its turn (nats-server 2.15.1).

The requester probes its delegated task with ``task.probe`` (custom_agents.live_controls)
through its process's host, signed with its session; the worker's host answers on the
worker's own address. Until step 7b the probe went over the operational subject, whose
delivery an approval resume once blocked; the answer must still come while the resume is
held.
"""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents import agent_requests
from custom_agents.agent_host import (
    LEAD_HOST_RUNTIME,
    AgentProcessHost,
    definition_sha256,
    lead_host_profile,
    startup_agent_host_profile,
)
from custom_agents.live_controls import LIVE_CONTROL_HANDLERS
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from custom_transport.protocol1.host_runtime import MessageHost
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401 - broker is a fixture
from tests.test_turn_action_outbox import _make_agent, _make_task_manager

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

WORKER_RUNTIME = "agent-WebResearchAgent"
DEFINITION = definition_sha256("ab" * 32)


async def _until(condition, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_status_reply_while_approval_resumes_a_long_turn(
    broker: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811 - the shared live NATS fixture
) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect", "WebResearchAgent"), 2)
    worker_profile = startup_agent_host_profile(TEAM, WORKER_RUNTIME, "WebResearchAgent")
    release, entered = asyncio.Event(), asyncio.Event()
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), lead_profile, worker_profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))

        requester = _make_agent(tmp_path / "requester", task_manager=_make_task_manager(tmp_path, owner_id="requester"))
        worker = _make_agent(tmp_path / "worker", task_manager=_make_task_manager(tmp_path, owner_id="worker"))
        for agent, name, agent_type in ((requester, "requester", "TeamArchitect"), (worker, "worker", "WebResearchAgent")):
            agent.team_id, agent.name, agent.named_agent_type = TEAM, name, agent_type
        requester.subtask_status_probe_timeout_seconds = 2.0
        requester_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
        worker_host = AgentProcessHost(layout, worker_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
        # The requester's process: ask_agent and team_view go through its host.
        monkeypatch.setattr(agent_requests, "_PROCESS_HOST", requester_host)
        requester_host.attach(requester, agent_type="TeamArchitect", definition=DEFINITION,
                              nc=await connect(stack, broker, LEAD_HOST_RUNTIME), state=("busy", True))
        worker_host.attach(worker, agent_type="WebResearchAgent", definition=DEFINITION,
                           nc=await connect(stack, broker, WORKER_RUNTIME), state=("busy", True))
        await _until(lambda: agent_requests.team_view() is not None
                     and agent_requests.team_view().agent(str(worker.id)) is not None)

        task = Task(task_id="delegated_task", task_type=MessageMetaType.REQUEST)
        worker.task_manager.add_task(task, MessageHeader(from_agent_id=requester.id), MessageMeta(),
                                     owner_person_id=None)
        worker.task_manager.update_main_task_state(task.task_id, ProgressState.WAITING,
                                                   datetime.now(timezone.utc).isoformat())
        worker._interrupted_turn_sessions[task.task_id] = SimpleNamespace()

        async def held_resume(task_id):
            worker._pending_interrupt_responses.pop(task_id, None)
            entered.set()
            await release.wait()

        object.__setattr__(worker, "_resume_interrupted_task_turn", held_resume)
        root = Task(task_id="root", task_type=MessageMetaType.REQUEST)
        requester.task_manager.add_task(root, MessageHeader(from_agent_id="gateway"), MessageMeta(),
                                        owner_person_id=None)
        child = requester.task_manager.add_subtask(
            root.task_id, task, MessageHeader(to_routing_path=worker.id),
            MessageMeta(to_agent_name=worker.name, to_named_agent_type=worker.named_agent_type),
        )
        resuming = None
        try:
            before = await requester._probe_subtask_task_status(subtask_info=child)
            assert before is not None and before.is_known_task and before.responder_agent_id == worker.id
            # The approval resume holds the worker's turn; its address still answers.
            resuming = asyncio.create_task(worker._resume_interrupted_task_turn(task.task_id))
            await asyncio.wait_for(entered.wait(), 3)
            after = await requester._probe_subtask_task_status(subtask_info=child)
            assert after is not None, "Approval resume blocked the worker's task probes"
            assert after.is_known_task and after.responder_agent_id == worker.id
        finally:
            release.set()
            if resuming is not None:
                await asyncio.gather(resuming, return_exceptions=True)
            await worker_host.stop()
            await requester_host.stop()
            stop.set()
            await asyncio.wait_for(serving, timeout=10)
