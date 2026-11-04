"""Agent processes as protocol 1 hosts, and people's commands relayed to an agent (nats-server 2.15.1).

The installation authority, the team services (with their relays), an agent process's host
(custom_agents.agent_host) with a stand-in agent, and the gateway's host run on a real NATS
server. A person's ``tasks.list`` goes gateway → team services → the agent's host with the
gateway's command token unchanged, and the agent's reply comes back signed as the agent.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_gateway.message_host import GATEWAY_RUNTIME, GatewayMessageHost, gateway_profile
from custom_agents.agent_host import AgentProcessHost, definition_sha256, startup_agent_host_profile
from custom_transport.protocol1.host_runtime import MessageHost
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from team_services.relays import AGENT_UNAVAILABLE_TEXT, Relays
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401 - broker is a fixture

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

PERSON = "p-PersonPersonPersonPers"
RUNTIME = "agent-Researcher"
DEFINITION = definition_sha256("ab" * 32)


async def _until(condition, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


def _agent(agent_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=agent_id, named_agent_type="Researcher", _runtime_loop=asyncio.get_running_loop())


@pytest.mark.asyncio
async def test_a_persons_task_list_is_relayed_to_the_agents_host_and_back(
    broker: str, tmp_path: Path,  # noqa: F811 - the shared live NATS fixture
) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    profile = startup_agent_host_profile(TEAM, RUNTIME, "Researcher")
    asked: list[tuple[str, str, frozenset[str]]] = []

    async def tasks_list(agent, verified):
        asked.append((agent.id, verified.authority.token.person, verified.certificate.roles))
        return {"outcome": "ok", "tasks": [], "total": 0, "queued_count": 0, "current_count": 0,
                "queue_digest": "digest-1"}

    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM), profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        relays = Relays(lambda: services_host.live, services_host.nc.jetstream(), TEAM, "TeamArchitect",
                        logging.getLogger("test.relays"))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop, [relays.handlers()]))
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        await gateway.ready(timeout=20)

        # The agent's process: its host joins on the agent's connection, and the agent gets a session.
        agent_host = AgentProcessHost(layout, profile, TEAM, handlers={"tasks.list": tasks_list})
        agent_host.attach(_agent("researcher_1"), agent_type="Researcher", definition=DEFINITION,
                          nc=await connect(stack, broker, RUNTIME))
        await _until(lambda: "researcher_1" in agent_host.agents and agent_host.agents["researcher_1"].subscription)

        body = {"request_id": "cmd-1", "agent": "researcher_1", "offset": 0, "limit": 20}
        reply = await gateway.command("tasks.list", body, person_ref=PERSON, request_id="cmd-1")
        assert reply.raw["body"].get("queue_digest") == "digest-1", reply.raw["body"]
        # The agent saw the gateway's token, relayed by the team services.
        assert asked == [("researcher_1", PERSON, frozenset({"team_services", "scheduler"}))]

        # The agent leaves: its session closes, and a command for it is answered plainly.
        await asyncio.wrap_future(agent_host.detach("researcher_1"))
        gone = await gateway.command("tasks.list", {**body, "request_id": "cmd-2"}, person_ref=PERSON,
                                     request_id="cmd-2")
        assert gone.raw["body"] == {"outcome": "runtime_error", "message": AGENT_UNAVAILABLE_TEXT}

        # A host whose run stopped without closing its agent's session: its next start closes it, so its
        # one session is free again for the new run's agent (agent ids are new at every start).
        agent_host.attach(_agent("researcher_2"), agent_type="Researcher", definition=DEFINITION,
                          nc=await connect(stack, broker, RUNTIME))
        await _until(lambda: "researcher_2" in agent_host.agents and agent_host.agents["researcher_2"].subscription)
        agent_host._stop.set()  # the process stops without closing its sessions
        restarted = AgentProcessHost(layout, profile, TEAM, handlers={"tasks.list": tasks_list})
        restarted.attach(_agent("researcher_3"), agent_type="Researcher", definition=DEFINITION,
                         nc=await connect(stack, broker, RUNTIME))
        await _until(lambda: "researcher_3" in restarted.agents and restarted.agents["researcher_3"].subscription)
        relayed = await gateway.command("tasks.list", {**body, "request_id": "cmd-3", "agent": "researcher_3"},
                                        person_ref=PERSON, request_id="cmd-3")
        assert relayed.raw["body"]["outcome"] == "ok"

        await restarted.stop()
        await gateway.close()
        stop.set()
        await asyncio.wait_for(serving, timeout=10)
