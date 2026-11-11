"""A signed pause keeps direct work queued until a signed resume, with a real agent and NATS."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_agents.agent_host import AgentProcessHost, LEAD_HOST_RUNTIME, lead_host_profile, startup_agent_host_profile
from custom_agents.live_controls import LIVE_CONTROL_HANDLERS
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.nats_adapter import NatsTransportConfig
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.transport import TransportSettings
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from tests import test_live_multi_agent_state_boundaries as boundary
from tests.test_live_agent_controls import DEFINITION, _until, stand_in
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.live, pytest.mark.nats]


async def test_signed_pause_holds_direct_work_until_signed_resume(
    broker: str, tmp_path: Path, monkeypatch,  # noqa: F811 - shared fixture
):
    transport = TransportSettings(nats_url=broker, nats_servers=[broker])
    monkeypatch.setattr(boundary, "resolve_live_transport_settings", lambda: transport)
    monkeypatch.setattr(boundary, "nats_transport_config_from_env", lambda **_: NatsTransportConfig(servers=[broker]))
    model = boundary.FakeStrandsAgent(boundary._task_response("completed after resume"))
    monkeypatch.setattr("custom_agents.strands_agent.Agent", lambda **_: model)
    monkeypatch.setattr("custom_agents.strands_agent.UniversalAgent._configure_model", lambda *_: MagicMock())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker = boundary._agent(agent_id="paused_worker", name="Researcher-1", team_id=TEAM,
                             named_agent_type="WebResearchAgent", db_path=tmp_path / "tasks.db", workspace_dir=workspace)
    layout = InstanceLayout.at(tmp_path / "instance")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect",), 1)
    worker_profile = startup_agent_host_profile(TEAM, "paused-worker-host", "WebResearchAgent")
    lead = stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True)
    lead_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
    worker_host = AgentProcessHost(layout, worker_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
    stop = asyncio.Event()
    agent_tasks = []
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), lead_profile, worker_profile)
        services = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services, TEAM, stop))
        connection = await boundary._connect_nats(TEAM)
        try:
            channel = await connection.channel()
            responses = await channel.declare_queue("paused_test_user", auto_delete=True)
            agent_tasks.append((worker, await boundary._start_agent(worker)))
            lead_host.attach(lead, agent_type="TeamArchitect", definition=DEFINITION,
                             nc=await connect(stack, broker, LEAD_HOST_RUNTIME), state=("idle", True))
            worker_host.attach(worker, agent_type="WebResearchAgent", definition=DEFINITION,
                               nc=await connect(stack, broker, worker_profile.runtime), state=("idle", True))
            await _until(lambda: worker_host.host is not None and worker.id in worker_host.agents
                         and worker_host.agents[worker.id].session is not None
                         and "paused_worker" in lead.known_agents.agent_map)
            assert (await lead_host.ask("agent.pause", {"request_id": "pause-before-work"}, worker.id,
                                        sender_agent=lead.id))["state"] == "paused"
            await channel.publish(worker.id, MessageEnvelope(
                msg_content=Task(task_id="paused_direct_task", task_type=MessageMetaType.REQUEST,
                                 problem_statement="Reply when resumed."),
                msg_header=MessageHeader(from_agent_id="test_user", to_routing_path=worker.id,
                                         response_routing_key=responses.name),
                msg_meta=MessageMeta(requester_type=RequesterType.USER_REQUESTER, is_response_requested=True,
                                     msg_correlation_id="paused_request"),
            ))
            # Wait across a real broker fetch interval and prove both ends: no model
            # turn and no response while paused, then exactly one completed turn.
            assert await responses.get(timeout=1, fail=False) is None
            assert worker.paused and model.invocations == []
            assert (await lead_host.ask("agent.resume", {"request_id": "resume-work"}, worker.id,
                                        sender_agent=lead.id))["state"] == "resumed"
            reply = await boundary._wait_for_response(responses, timeout=15)
            assert reply.msg_content.answer == "completed after resume"
            assert len(model.invocations) == 1
        finally:
            await worker_host.stop()
            await lead_host.stop()
            await boundary._stop_agents(agent_tasks)
            await connection.close()
            stop.set()
            await asyncio.wait_for(serving, timeout=10)
