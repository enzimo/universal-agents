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
from custom_agents.agent_host import (
    LEAD_HOST_RUNTIME,
    AgentProcessHost,
    definition_sha256,
    lead_host_profile,
    startup_agent_host_profile,
)
from custom_transport.protocol1.host_keys import load_authority_keys, load_host_keys
from custom_transport.protocol1.host_runtime import MessageHost
from docker import message_hosts
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
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


def _agent(agent_id: str, agent_type: str = "Researcher") -> SimpleNamespace:
    return SimpleNamespace(id=agent_id, named_agent_type=agent_type, _runtime_loop=asyncio.get_running_loop())


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
        relays = Relays(services_host, TEAM, "TeamArchitect", logging.getLogger("test.relays"))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop, [relays.family()]))
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        await gateway.ready(timeout=20)

        # The agent's process: its host joins on the agent's connection, and the agent gets a session.
        agent_host = AgentProcessHost(layout, profile, TEAM, handlers={"tasks.list": tasks_list})
        agent_host.attach(_agent("researcher_1"), agent_type="Researcher", definition=DEFINITION,
                          nc=await connect(stack, broker, RUNTIME))
        await _until(lambda: "researcher_1" in agent_host.agents and agent_host.agents["researcher_1"].subscription)

        body = {"request_id": "cmd-1", "agent": "researcher_1", "offset": 0, "limit": 20}
        reply = await gateway.command("tasks.list", body, person_ref=PERSON)
        assert reply.raw["body"].get("queue_digest") == "digest-1", reply.raw["body"]
        # The agent saw the gateway's token, relayed by the team services.
        assert asked == [("researcher_1", PERSON, frozenset({"team_services"}))]

        # The agent leaves: its session closes, and a command for it is answered plainly.
        await asyncio.wrap_future(agent_host.detach("researcher_1"))
        gone = await gateway.command("tasks.list", {**body, "request_id": "cmd-2"}, person_ref=PERSON)
        assert gone.raw["body"] == {"outcome": "runtime_error", "message": AGENT_UNAVAILABLE_TEXT}

        # A host whose run stopped without closing its agent's session: its next start closes it, so its
        # one session is free again for the new run's agent (agent ids are new at every start).
        agent_host.attach(_agent("researcher_2"), agent_type="Researcher", definition=DEFINITION,
                          nc=await connect(stack, broker, RUNTIME))
        await _until(lambda: "researcher_2" in agent_host.agents and agent_host.agents["researcher_2"].subscription)
        # Upkeep rewrites placements under the team services' renewed certificate.
        # The session keeps its original placement revision.
        from team_services.sessions import SessionService
        await services_host._renew()
        sessions = SessionService(services_host.records, services_host.live.trust, services_host.identity.installation,
                                  services_host.clock)
        await sessions.maintain(TEAM)
        agent_host._stop.set()  # the process stops without closing its sessions
        restarted = AgentProcessHost(layout, profile, TEAM, handlers={"tasks.list": tasks_list})
        restarted.attach(_agent("researcher_3"), agent_type="Researcher", definition=DEFINITION,
                         nc=await connect(stack, broker, RUNTIME))
        await _until(lambda: "researcher_3" in restarted.agents and restarted.agents["researcher_3"].subscription)
        relayed = await gateway.command("tasks.list", {**body, "request_id": "cmd-3", "agent": "researcher_3"},
                                        person_ref=PERSON)
        assert relayed.raw["body"]["outcome"] == "ok"

        await restarted.stop()
        await gateway.close()
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_a_real_agent_opens_its_session_through_its_process_host(tmp_path: Path) -> None:
    """A real agent's own NATS connection carries its host's protocol 1 traffic.

    Replies to a host's requests must arrive under its inbox (``_INBOX.<host>.``); agents
    once connected without it, so every reply, starting with their sessions', was refused.
    """

    from unittest.mock import MagicMock, patch

    from id_generation import new_id
    from tests.protocol1_team_support import protocol1_team
    from tests.test_live_multi_agent_state_boundaries import _agent, _start_agent, _stop_agents

    team_id = new_id("team_hosts")[:24]
    with patch("custom_agents.strands_agent.Agent", return_value=MagicMock()), patch(
        "custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()
    ):
        async with protocol1_team(tmp_path, team_id, ("TeamArchitect",)) as host:
            lead = _agent(agent_id=new_id("lead"), name="lead", team_id=team_id, named_agent_type="TeamArchitect",
                          db_path=tmp_path / "lead.db", workspace_dir=tmp_path, is_lead=True)
            running = [(lead, await _start_agent(lead))]
            try:
                await _until(lambda: lead.id in host.agents and host.agents[lead.id].session is not None, timeout=30)
                assert host.agents[lead.id].subscription is not None
                # Drain the production UniversalAgent, including its actual durable subscriptions.
                from custom_transport.nats_adapter import NAMED_WORKER_CONSUMER_ROLE, TEAM_LEAD_CONSUMER_ROLE
                host.host.learned_other_contract("2" * 64, "live_test")
                await _until(lambda: host._draining is not None and host._draining.done(), timeout=30)
                assert host.host.state == "drained"
                assert lead._contract_drain.left_queues >= 2
                roles = {handle.consumer_role for handle in lead._ensure_nats_transport_adapter()._subscriptions}
                assert not roles & {NAMED_WORKER_CONSUMER_ROLE, TEAM_LEAD_CONSUMER_ROLE}
                assert "agent_direct" in roles
                assert lead.id not in host.agents

            finally:
                await _stop_agents(running)


async def _team_with_a_lead_host(stack: AsyncExitStack, broker: str, layout: InstanceLayout,  # noqa: F811
                                 lead_profile, catalog: set[str], stop: asyncio.Event) -> tuple[MessageHost, asyncio.Task]:
    """The launcher's set-up: the team lead's host listed to ask for the catalog's types, which the authority
    reads at each join; and the team services serving."""

    services_profile = team_services_profile(TEAM)
    message_hosts.prepare_message_host_trust(layout, {
        services_profile.runtime: message_hosts.expected_host(layout, services_profile),
        lead_profile.runtime: message_hosts.expected_host(layout, lead_profile, agent_types_from_catalog=True)})
    authority = InstallationAuthority(layout, load_authority_keys(layout), load_host_keys(layout, AUTHORITY_RUNTIME),
                                      catalog_agent_types=lambda: frozenset(catalog))
    await authority.start(await connect(stack, broker, AUTHORITY_RUNTIME), teams=[TEAM])
    services = MessageHost(layout, services_profile, await connect(stack, broker, TEAM_SERVICES_RUNTIME))
    return services, asyncio.create_task(serve_team(services, TEAM, stop))


def _has_session(host: AgentProcessHost, agent_id: str) -> bool:
    hosted = host.agents.get(agent_id)
    return hosted is not None and hosted.session is not None and hosted.subscription is not None


def _events(caplog, event: str, agent: str) -> list[str]:
    return [record.getMessage() for record in caplog.records
            if f"event={event} " in record.getMessage() and f"agent={agent} " in record.getMessage()]


@pytest.mark.asyncio
async def test_a_specialist_created_while_the_lead_runs_gets_its_session_after_its_host_joins_again(
    broker: str, tmp_path: Path, caplog,  # noqa: F811 - the shared live NATS fixture
) -> None:
    """Plan 13B.5: every running agent holds a session, a specialist type the team created at runtime too."""

    layout = InstanceLayout.at(tmp_path / "instance")
    catalog = {"TeamArchitect", "Researcher"}
    lead_profile = lead_host_profile(TEAM, sorted(catalog), 4)
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        _services, serving = await _team_with_a_lead_host(stack, broker, layout, lead_profile, catalog, stop)
        lead = AgentProcessHost(layout, lead_profile, TEAM, catalog_types=lambda: set(catalog),
                                refused_retry_seconds=(0.2, 1.0))
        lead.attach(_agent("lead_1", "TeamArchitect"), agent_type="TeamArchitect", definition=DEFINITION,
                    nc=await connect(stack, broker, LEAD_HOST_RUNTIME))
        await _until(lambda: _has_session(lead, "lead_1"))
        assert lead.host is not None and lead.host.identity.certificate.membership_epoch == 1

        # The team creates a specialist; its first worker starts in the lead's process, no restart.
        catalog.add("Analyst")
        with caplog.at_level(logging.INFO, logger="custom_agents.agent_host"):
            lead.attach(_agent("analyst_1", "Analyst"), agent_type="Analyst", definition=DEFINITION, nc=None)
            await _until(lambda: _has_session(lead, "analyst_1"))
        certificate = lead.host.identity.certificate
        assert certificate.membership_epoch == 2 and "Analyst" in certificate.agent_types
        # Refused once for the type, joined again, and opened at once: no finding, no refusal to log.
        assert not _events(caplog, "agent_host_session_refused", "analyst_1")
        assert "refusals_before=0" in _events(caplog, "agent_host_session_opened", "analyst_1")[0]
        assert "event=protocol1_rejoined" in caplog.text
        # The lead keeps its session through the new certificate: both sign with it now.
        assert set(lead.host.identity.sessions) == {"lead_1", "analyst_1"}

        await lead.stop()
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_a_refused_session_is_asked_for_again_and_listed_in_the_host_record_until_it_opens(
    broker: str, tmp_path: Path, caplog,  # noqa: F811 - the shared live NATS fixture
) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    catalog = {"TeamArchitect", "Researcher"}
    lead_profile = lead_host_profile(TEAM, sorted(catalog), 2)  # the lead and one worker
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        services, serving = await _team_with_a_lead_host(stack, broker, layout, lead_profile, catalog, stop)
        lead = AgentProcessHost(layout, lead_profile, TEAM, refused_retry_seconds=(0.3, 1.0))
        with caplog.at_level(logging.INFO, logger="custom_agents.agent_host"):
            lead.attach(_agent("lead_1", "TeamArchitect"), agent_type="TeamArchitect", definition=DEFINITION,
                        nc=await connect(stack, broker, LEAD_HOST_RUNTIME))
            await _until(lambda: _has_session(lead, "lead_1"))
            lead.attach(_agent("worker_1"), agent_type="Researcher", definition=DEFINITION, nc=None)
            await _until(lambda: _has_session(lead, "worker_1"))
            lead.attach(_agent("worker_2"), agent_type="Researcher", definition=DEFINITION, nc=None)
            await _until(lambda: "worker_2" in lead.agents and lead.agents["worker_2"].refusal is not None)

            # Others read the refused agent in the lead host's record, as the gateway's Status page does.
            async def components() -> tuple[str, list[tuple[str, str, str]]]:
                assert services.records is not None
                report = await services.records.read_report("host", LEAD_HOST_RUNTIME, now=services.clock())
                assert report is not None
                body = report.record.body
                return body.health, [(item.component, item.state, item.code) for item in body.components]

            for _ in range(100):
                health, listed = await components()
                if listed:
                    break
                await asyncio.sleep(0.05)
            assert (health, listed) == ("degraded", [("Researcher-worker_2", "unhealthy", "session_limit_reached")])
            await asyncio.sleep(2.5)  # asked again several times meanwhile
            assert lead.agents["worker_2"].session is None

            # The cause goes: a worker leaves, so there is room, and the next request opens the session.
            await asyncio.wrap_future(lead.detach("worker_1"))
            await _until(lambda: _has_session(lead, "worker_2"))
            for _ in range(100):
                health, listed = await components()
                if not listed:
                    break
                await asyncio.sleep(0.05)
            assert (health, listed) == ("healthy", [])

        refused = _events(caplog, "agent_host_session_refused", "worker_2")
        assert len(refused) == 1 and "code=session_limit_reached" in refused[0]  # once per change, not per request
        opened = _events(caplog, "agent_host_session_opened", "worker_2")
        assert len(opened) == 1 and "refusals_before=0" not in opened[0]

        await lead.stop()
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


class _QueueWorker:
    """A stand-in agent that takes its type's tasks from the queue all agents of the type share, as today's queued
    work does, and holds each task it took until it runs or hands it back.

    Its drain (``AgentProcessHost.drain_agent``) stops taking tasks; ``hand_back`` returns what it holds to the
    queue unacknowledged, so the queue gives it to another agent of the type. The real drain of an agent is
    ``custom_agents.contract_drain`` (tests/test_contract_drain.py).
    """

    def __init__(self, agent_id: str, js, *, runs: bool) -> None:
        self.agent = _agent(agent_id)
        self.js, self.runs = js, runs
        self.held: list = []
        self.ran: list[str] = []
        self.taking = asyncio.Event()
        self.drained = asyncio.Event()

    async def take(self, stop: asyncio.Event) -> None:
        from nats.errors import TimeoutError as NatsTimeoutError

        queue = await self.js.pull_subscribe("work.Researcher", durable="workers_Researcher")
        while not stop.is_set():
            await self.taking.wait()
            try:
                messages = await queue.fetch(1, timeout=0.2)
            except NatsTimeoutError:
                continue
            for message in messages:
                if not self.taking.is_set():  # drained while it waited: not taken
                    await message.nak()
                elif self.runs:
                    self.ran.append(message.data.decode())
                    await message.ack()
                else:
                    self.held.append(message)

    async def drain(self, agent, reason: str) -> None:
        self.taking.clear()
        self.drained.set()

    async def holds_work(self, agent) -> bool:
        return bool(self.held)

    async def hand_back(self) -> None:
        for message in self.held:
            await message.nak()
        self.held.clear()

    async def tasks_list(self, agent, verified):
        return {"outcome": "ok", "tasks": [], "total": len(self.held), "queued_count": len(self.held),
                "current_count": 0, "queue_digest": f"held-{len(self.held)}"}


@pytest.mark.asyncio
async def test_a_host_on_another_contract_drains_hands_back_its_task_and_gives_up_its_agent(
    broker: str, tmp_path: Path, caplog,  # noqa: F811 - the shared live NATS fixture
) -> None:
    """Contract README "Versions" and plan 13B.12: a host on another contract digest takes no new work and gets
    no new placement, still answers a person's command about the task it holds, drains (its record says
    ``draining``, then ``drained``), hands back its unfinished task, which another host's agent then runs, and
    renews only to give up its agent.

    Host B joined on the installation's contract and then runs another one, as a host that kept running while
    the rest of the installation moved to another pre-release contract (``run_on_another_contract``). It learns
    it from the team services' host record.
    """

    from nats.js.api import RetentionPolicy, StreamConfig

    from custom_messages.wire.kinds import CONTRACT_SHA256
    from tests.test_live_team_services import OTHER_CONTRACT, run_on_another_contract

    layout = InstanceLayout.at(tmp_path / "instance")
    a_profile = startup_agent_host_profile(TEAM, "agent-Researcher-1", "Researcher")
    b_profile = startup_agent_host_profile(TEAM, "agent-Researcher-2", "Researcher")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM), a_profile,
                           b_profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        relays = Relays(services_host, TEAM, "TeamArchitect", logging.getLogger("test.relays"))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop, [relays.family()]))
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        await gateway.ready(timeout=20)
        # The type's queue, shared by every agent of the type.
        js = (await connect(stack, broker, "work-queue")).jetstream()
        await js.add_stream(StreamConfig(name="WORK_Researcher", subjects=["work.Researcher"],
                                         retention=RetentionPolicy.WORK_QUEUE))

        a_worker, b_worker = _QueueWorker("researcher_a", js, runs=True), _QueueWorker("researcher_b", js, runs=False)
        a_host = AgentProcessHost(layout, a_profile, TEAM)
        b_host = AgentProcessHost(layout, b_profile, TEAM, handlers={"tasks.list": b_worker.tasks_list},
                                  drain_agent=b_worker.drain, holds_work=b_worker.holds_work, drain_poll_seconds=0.2)
        a_host.attach(a_worker.agent, agent_type="Researcher", definition=DEFINITION,
                      nc=await connect(stack, broker, a_profile.runtime))
        b_host.attach(b_worker.agent, agent_type="Researcher", definition=DEFINITION,
                      nc=await connect(stack, broker, b_profile.runtime))
        await _until(lambda: _has_session(a_host, "researcher_a") and _has_session(b_host, "researcher_b"))
        takers = [asyncio.create_task(worker.take(stop)) for worker in (a_worker, b_worker)]

        # B takes the first task and holds it; then A takes tasks too.
        b_worker.taking.set()
        await js.publish("work.Researcher", b"task-1")
        await _until(lambda: len(b_worker.held) == 1)
        a_worker.taking.set()

        async def b_record():
            assert b_host.host is not None and b_host.host.records is not None
            # Readers of the installation's contract cannot read the rest of B's record; one of B's own can.
            stored = await b_host.host.records.read_report("host", b_profile.runtime, now=b_host.host.clock())
            assert stored is not None
            return stored.record

        assert b_host.host is not None
        with caplog.at_level(logging.INFO):
            run_on_another_contract(b_host.host)
            await _until(lambda: b_worker.drained.is_set(), timeout=30)  # the team services' record, within 10 s
            assert b_host.host.installation_contract == CONTRACT_SHA256
            assert (await b_record()).body.state == "draining"
            # The installation's readers see that B's record names another contract, and no more.
            assert services_host.records is not None
            seen = await services_host.records.read_report("host", b_profile.runtime, now=services_host.clock())
            assert seen is not None and seen.record.other_contract
            assert seen.record.their_contract_sha256 == OTHER_CONTRACT

            # No new work: the next task goes to A. No new placement: an agent attached to B now gets no session.
            await js.publish("work.Researcher", b"task-2")
            await _until(lambda: a_worker.ran == ["task-2"])
            late = _agent("researcher_late")
            b_host.attach(late, agent_type="Researcher", definition=DEFINITION, nc=None)
            await asyncio.sleep(0.5)
            assert b_host.agents["researcher_late"].session is None
            placements = await (await services_host.nc.jetstream().key_value("UA1_PLACEMENT")).keys()
            assert f"{TEAM}.researcher_late" not in placements

            # A person's command about the task B holds still reaches it, signed as its agent.
            body = {"request_id": "cmd-1", "agent": "researcher_b", "offset": 0, "limit": 20}
            reply = await gateway.command("tasks.list", body, person_ref=PERSON)
            assert reply.raw["body"]["queue_digest"] == "held-1", reply.raw["body"]

            # B hands its task back; A runs it. B then holds nothing: it gives its agents up and says so.
            epoch = b_host.host.identity.certificate.membership_epoch
            await b_worker.hand_back()
            await _until(lambda: a_worker.ran == ["task-2", "task-1"])
            await _until(lambda: b_host.host._give_up_answered and not b_host.agents, timeout=20)
            assert (await b_record()).body.state == "drained"
            given_up = b_host.host.identity.certificate
            assert (given_up.agent_types, given_up.session_limit) == (frozenset(), 0)
            assert given_up.membership_epoch == epoch + 1
            gone = await gateway.command("tasks.list", {**body, "request_id": "cmd-2"}, person_ref=PERSON)
            assert gone.raw["body"] == {"outcome": "runtime_error", "message": AGENT_UNAVAILABLE_TEXT}

        assert caplog.text.count("event=protocol1_host_other_contract") == 1
        assert "event=agent_host_drained" in caplog.text and "event=host_renewal_gave_up_agents" in caplog.text
        assert "event=agent_session_refused_other_contract" not in caplog.text  # B asked for no new session

        for taker in takers:
            taker.cancel()
        await a_host.stop()
        await b_host.stop()
        await gateway.close()
        stop.set()
        await asyncio.wait_for(serving, timeout=10)
