"""Each agent's signed record, and the live view readers build from it (nats-server 2.15.1).

The installation authority, the team services, an agent process's host
(custom_agents.agent_host) with stand-in agents and the gateway's host run on a real NATS
server. The agent's host writes its record when the agent's state changes, at most every
2 seconds; the gateway's host reads the team's live agents
(custom_transport.protocol1.team_agents): the record, the agent's placement and its host's
liveness together.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import nats

from assistant_gateway.message_host import GATEWAY_RUNTIME, GatewayMessageHost, gateway_profile
from custom_agents import agent_host as agent_host_module
from custom_agents.agent_host import AgentProcessHost, definition_sha256, lead_host_profile, startup_agent_host_profile
from custom_transport.protocol1.host_runtime import LIVENESS_LIFETIME, MessageHost
from custom_transport.protocol1.team_agents import TeamAgents
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401 - broker is a fixture

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

RUNTIME = "agent-Researcher"
DEFINITION = definition_sha256("ab" * 32)


async def _until(condition, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


def _agent(agent_id: str, name: str, agent_type: str = "Researcher") -> SimpleNamespace:
    return SimpleNamespace(id=agent_id, name=name, named_agent_type=agent_type, _runtime_loop=asyncio.get_running_loop())


@pytest.mark.asyncio
async def test_readers_see_an_agents_state_name_and_leaving_and_writes_are_spaced(
    broker: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811 - the shared live NATS fixture
) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    profile = startup_agent_host_profile(TEAM, RUNTIME, "Researcher")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM), profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        await gateway.ready(timeout=20)
        reader = TeamAgents(gateway.host, TEAM, recheck_seconds=0.2)
        changes: list[frozenset[str]] = []
        reader.listen(changes.append)
        reading = asyncio.create_task(reader.run(stop))
        await asyncio.wait_for(reader.ready.wait(), timeout=10)
        assert reader.live() == {}

        # The agent connects: once its session opens, its record says it is idle, with its name.
        agent_host = AgentProcessHost(layout, profile, TEAM)
        agent_host.attach(_agent("researcher_1", "Researcher-1"), agent_type="Researcher", definition=DEFINITION,
                          nc=await connect(stack, broker, RUNTIME), state=("idle", True))
        await _until(lambda: reader.agent("researcher_1") is not None)
        seen = reader.agent("researcher_1")
        assert (seen.agent_type, seen.display_name, seen.state, seen.accepting_work, seen.host, seen.is_lead) == (
            "Researcher", "Researcher-1", "idle", True, RUNTIME, False)
        assert reader.named("Researcher-1") == seen and reader.of_type("Researcher") == [seen]

        # A burst of changes costs at most one more write within the interval, and readers end on the last.
        written: list[str] = []
        records = agent_host.host.records
        put = records.put

        async def counted(record, key, document, **kwargs):
            if record == "agent":
                written.append(document["body"]["state"])
            return await put(record, key, document, **kwargs)

        monkeypatch.setattr(records, "put", counted)
        for state in ("busy", "idle", "busy"):
            agent_host.report("researcher_1", state, True)
            await asyncio.sleep(0.05)
        await _until(lambda: reader.agent("researcher_1").state == "busy")
        assert written == ["busy"]  # changes within the first interval coalesce into the final state
        agent_host.report("researcher_1", "paused", False)
        await _until(lambda: reader.agent("researcher_1").state == "paused", timeout=5)
        assert reader.agent("researcher_1").accepting_work is False and written == ["busy", "paused"]

        # The agent leaves: "exiting" is written at once, then the record is removed; readers drop it.
        close_entered, close_release = asyncio.Event(), asyncio.Event()
        request = agent_host._request

        async def delayed_close(kind, body):
            if kind == "sessions.close":
                close_entered.set()
                await close_release.wait()
            return await request(kind, body)

        monkeypatch.setattr(agent_host, "_request", delayed_close)
        leaving = agent_host.detach("researcher_1")
        await asyncio.wait_for(close_entered.wait(), 5)
        await _until(lambda: reader.agent("researcher_1") is None)
        assert reader.departure_reason("researcher_1") == "exiting"
        close_release.set()
        await asyncio.wrap_future(leaving)
        assert written[-1] == "exiting"
        assert await records.read_report("agent", f"{TEAM}.researcher_1", now=agent_host.host.clock()) is None
        assert any("researcher_1" in changed for changed in changes)

        # A host whose run stopped without closing: its next start removes the old agent's record.
        agent_host.attach(_agent("researcher_2", "Researcher-2"), agent_type="Researcher", definition=DEFINITION,
                          nc=await connect(stack, broker, RUNTIME), state=("idle", True))
        await _until(lambda: reader.agent("researcher_2") is not None)
        agent_host._stop.set()
        restarted = AgentProcessHost(layout, profile, TEAM)
        restarted.attach(_agent("researcher_3", "Researcher-3"), agent_type="Researcher", definition=DEFINITION,
                         nc=await connect(stack, broker, RUNTIME), state=("idle", True))
        await _until(lambda: reader.agent("researcher_3") is not None)
        assert await restarted.host.records.read_report("agent", f"{TEAM}.researcher_2",
                                                        now=restarted.host.clock()) is None
        assert set(reader.live()) == {"researcher_3"}

        # A host that stops writing its liveness: its agents drop out once its record has expired,
        # LIVENESS_LIFETIME (30 seconds) after its last write. Seen a second later than that from
        # now, even the newest record has.
        reader_clock = gateway.host.clock
        monkeypatch.setattr(gateway.host, "clock", lambda: reader_clock() + LIVENESS_LIFETIME + timedelta(seconds=1))
        await _until(lambda: reader.agent("researcher_3") is None, timeout=5)
        monkeypatch.setattr(gateway.host, "clock", reader_clock)
        await _until(lambda: reader.agent("researcher_3") is not None, timeout=5)

        await restarted.stop()
        await gateway.close()
        stop.set()
        await asyncio.wait_for(asyncio.gather(serving, reading), timeout=10)


@pytest.mark.asyncio
async def test_the_team_leads_record_names_it_the_lead(
    broker: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811 - shared live NATS fixture
) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    profile = lead_host_profile(TEAM, ("TeamArchitect", "Researcher"), 3)
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM), profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        await gateway.ready(timeout=20)
        reader = TeamAgents(gateway.host, TEAM, recheck_seconds=0.2, logger=logging.getLogger("test.team_agents"))
        reading = asyncio.create_task(reader.run(stop))

        lead_host = AgentProcessHost(layout, profile, TEAM)
        nc = await connect(stack, broker, agent_host_module.LEAD_HOST_RUNTIME)
        lead_host.attach(_agent("lead_1", "TeamLead", "TeamArchitect"), agent_type="TeamArchitect",
                         definition=DEFINITION, nc=nc, state=("idle", True))
        lead_host.attach(_agent("worker_1", "Researcher-1"), agent_type="Researcher", definition=DEFINITION, nc=nc,
                         state=("idle", True))
        await _until(lambda: reader.agent("lead_1") is not None and reader.agent("worker_1") is not None)

        assert reader.lead() == reader.agent("lead_1") and reader.agent("lead_1").is_lead
        assert reader.agent("worker_1").is_lead is False

        # A replacement session may already exist at the services while its reply
        # has not reached a detaching host. Both sessions must close over real NATS.
        hosted = lead_host.agents["worker_1"]
        opened, release = asyncio.Event(), asyncio.Event()
        request = lead_host._request
        replacement = []

        async def delayed_open(kind, body):
            reply = await request(kind, body)
            if kind == "sessions.open":
                replacement.append(reply["session"]["body"]["session"])
                opened.set()
                await release.wait()
            return reply

        monkeypatch.setattr(lead_host, "_request", delayed_open)
        reopening = asyncio.create_task(lead_host._hold_session(hosted))
        await asyncio.wait_for(opened.wait(), 5)
        await lead_host._close("worker_1", "exited")
        release.set()
        await asyncio.wait_for(reopening, 5)
        await _until(lambda: reader.agent("worker_1") is None)
        assert replacement[0] in hosted.closed_sessions
        records, now = lead_host.host.records, lead_host.host.clock()
        assert await records.read_report("agent", f"{TEAM}.worker_1", now=now) is None
        assert await records.read_report("placement", f"{TEAM}.worker_1", now=now) is None
        key = f"session.{lead_host.host.identity.installation}.{TEAM}.{replacement[0]}"
        await records.take_trust_record("agent_session", key, now=now)
        assert lead_host.host.live.trust.store.agent_session(
            lead_host.host.identity.installation, TEAM, replacement[0]).ends_at <= now

        await lead_host.stop()
        await _until(lambda: reader.live() == {})
        await gateway.close()
        stop.set()
        await asyncio.wait_for(asyncio.gather(serving, reading), timeout=10)


@pytest.mark.asyncio
async def test_a_real_reader_reconnect_recovers_and_a_host_that_stops_publishing_expires(
    broker: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811 - shared isolated NATS fixture
) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    profile = startup_agent_host_profile(TEAM, RUNTIME, "Researcher")
    stop, disconnected = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(agent_host_module, "AGENT_RECORD_REFRESH_SECONDS", 0.5)
    monkeypatch.setattr(agent_host_module, "AGENT_RECORD_CLEANUP_SECONDS", 0.2)

    async def on_disconnect():
        disconnected.set()

    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM), profile)
        services = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services, TEAM, stop))
        nc = await nats.connect(broker, inbox_prefix=f"_INBOX.{GATEWAY_RUNTIME}".encode(),
                                disconnected_cb=on_disconnect, reconnect_time_wait=0.05)
        stack.push_async_callback(nc.close)
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(nc)
        await gateway.ready(timeout=20)
        reader = TeamAgents(gateway.host, TEAM, recheck_seconds=0.1)
        reading = asyncio.create_task(reader.run(stop))
        process = AgentProcessHost(layout, profile, TEAM)
        process.attach(_agent("researcher_1", "Researcher-1"), agent_type="Researcher", definition=DEFINITION,
                       nc=await connect(stack, broker, RUNTIME), state=("idle", True))
        try:
            await _until(lambda: reader.agent("researcher_1") is not None)
            await nc.force_reconnect()
            await asyncio.wait_for(disconnected.wait(), 5)
            await _until(lambda: nc.is_connected)
            process.report("researcher_1", "busy", True)
            await _until(lambda: reader.agent("researcher_1") is not None
                         and reader.agent("researcher_1").state == "busy")
            assert not reading.done()

            records = process.host.records
            key = f"{TEAM}.researcher_1"
            bucket = await process.host.nc.jetstream().stream_info("KV_UA1_AGENTS")
            assert bucket.config.max_age == 180
            stored = await records.read_report("agent", key, now=process.host.clock())
            assert await records.delete("agent", key, last_revision=stored.revision)
            await _until(lambda: reader.agent("researcher_1") is None)
            await _until(lambda: reader.agent("researcher_1") is not None)

            previous_session = reader.agent("researcher_1").session
            placement = await services.records.read_report("placement", key, now=services.clock())
            assert await services.records.delete("placement", key, last_revision=placement.revision)
            await _until(lambda: reader.agent("researcher_1") is None)
            await _until(lambda: reader.agent("researcher_1") is not None
                         and reader.agent("researcher_1").session != previous_session)

            # Cancel the process's upkeep, without publishing a graceful close and
            # without moving the reader's clock. Its last heartbeat expires over NATS.
            process._task.cancel()
            await asyncio.gather(process._task, return_exceptions=True)
            await _until(lambda: reader.agent("researcher_1") is None, timeout=36)
            assert reader.departure_reason("researcher_1") == "host_unavailable"
        finally:
            await process.stop()
            await gateway.close()
            stop.set()
            await asyncio.wait_for(asyncio.gather(serving, reading), timeout=10)


@pytest.mark.asyncio
async def test_readers_keep_an_agent_whose_host_runs_another_contract_and_send_it_no_new_work(
    broker: str, tmp_path: Path, caplog,  # noqa: F811 - the shared live NATS fixture
) -> None:
    """Contract README "Versions": a host record that names another contract digest is authentic but its body is
    not read. Reading its certificate from the body stopped the whole view; now its agent stays, marked, and the
    host is logged once, not at every record it writes."""

    from tests.test_live_team_services import OTHER_CONTRACT, run_on_another_contract

    layout = InstanceLayout.at(tmp_path / "instance")
    profile = startup_agent_host_profile(TEAM, RUNTIME, "Researcher")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM), profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        await gateway.ready(timeout=20)
        reader = TeamAgents(gateway.host, TEAM, recheck_seconds=0.2, logger=logging.getLogger("test.team_agents"))
        reading = asyncio.create_task(reader.run(stop))

        async def keeps(agent, reason):  # the agent's own drain is tests/test_contract_drain.py's
            return None

        async def holds(agent):
            return True

        agent_host = AgentProcessHost(layout, profile, TEAM, drain_agent=keeps, holds_work=holds)
        agent_host.attach(_agent("researcher_1", "Researcher-1"), agent_type="Researcher", definition=DEFINITION,
                          nc=await connect(stack, broker, RUNTIME), state=("idle", True))
        await _until(lambda: reader.agent("researcher_1") is not None)
        assert reader.agent("researcher_1").takes_work

        with caplog.at_level(logging.WARNING, logger="test.team_agents"):
            run_on_another_contract(agent_host.host)
            for _ in range(3):
                await agent_host.host.report_now()
            await _until(lambda: reader.agent("researcher_1") is not None
                         and reader.agent("researcher_1").other_contract == OTHER_CONTRACT)
            await asyncio.sleep(0.5)  # several checks of the view
        seen = reader.agent("researcher_1")
        assert seen.accepting_work is True and not seen.takes_work and not reading.done()
        assert reader.other_contract_hosts() == {RUNTIME: OTHER_CONTRACT}
        logged = [record.getMessage() for record in caplog.records
                  if "event=team_agents_host_other_contract" in record.getMessage()]
        assert len(logged) == 1 and f"host={RUNTIME}" in logged[0] and OTHER_CONTRACT in logged[0]

        await agent_host.stop()
        await gateway.close()
        stop.set()
        await asyncio.wait_for(asyncio.gather(serving, reading), timeout=10)
