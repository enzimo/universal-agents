"""The gateway follows its team from the signed agent records (nats-server 2.15.1).

The installation authority, the team services, the team lead's process, a worker's process
and the gateway's host run on a real NATS server. The gateway's team view
(assistant_gateway.team_view) hands the lead's arrival and leaving to the manager client's
path, every member's state to the Team page's records, and each member's skills, which only
``agent.status`` gives, as the gateway asks for them.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from assistant_gateway.message_host import GATEWAY_RUNTIME, GatewayMessageHost, gateway_profile
from assistant_gateway.team_presence import GatewayTeamPresence
from assistant_gateway.team_view import GatewayTeamView
from custom_agents.agent_host import (
    LEAD_HOST_RUNTIME,
    AgentProcessHost,
    definition_sha256,
    lead_host_profile,
    startup_agent_host_profile,
)
from custom_agents.live_controls import LIVE_CONTROL_HANDLERS
from custom_messages.contracts.control_v1 import AgentStatus
from custom_transport.protocol1.host_runtime import MessageHost
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from tests.test_live_agent_controls import stand_in
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401 - broker is a fixture

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

WORKER_RUNTIME = "agent-Researcher"
DEFINITION = definition_sha256("ab" * 32)


async def _serve(stack: AsyncExitStack, services_host: MessageHost, stop: asyncio.Event, *args) -> None:
    """Run the team services until the test ends, however it ends (review F10: a failed check must not leave them
    running and hang the test)."""

    serving = asyncio.create_task(serve_team(services_host, TEAM, stop, *args))

    async def stop_serving() -> None:
        stop.set()
        await asyncio.wait_for(serving, timeout=10)

    stack.push_async_callback(stop_serving)


async def _started(stack: AsyncExitStack, url: str, runtime: str, host: AgentProcessHost):
    """An agent host and its connection. The host is stopped when the test ends, before its connection closes (the
    stack runs in reverse), so it can still write its last records; stopping it twice is harmless."""

    nc = await connect(stack, url, runtime)
    stack.push_async_callback(host.stop)
    return host, nc


async def _until(condition, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_the_gateway_follows_the_lead_and_the_members_from_the_records(
    broker: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811 - the shared live NATS fixture
) -> None:
    from custom_agents import skill_catalog_runtime

    monkeypatch.setattr(skill_catalog_runtime, "repair_agent_skill_catalog", AsyncMock(return_value=True))
    layout = InstanceLayout.at(tmp_path / "instance")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect", "Researcher"), 2)
    worker_profile = startup_agent_host_profile(TEAM, WORKER_RUNTIME, "Researcher")
    statuses: list[AgentStatus] = []

    def latest(agent_id: str) -> AgentStatus | None:
        return next((status for status in reversed(statuses) if status.self_id == agent_id), None)

    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM), lead_profile,
                           worker_profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        await _serve(stack, services_host, stop)
        gateway = GatewayMessageHost(layout, TEAM)
        view = GatewayTeamView(TEAM, on_change=statuses.append)
        gateway.observe_team(view)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        stack.push_async_callback(gateway.close)
        await _until(view.ready.is_set)

        lead = stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True)
        worker = stand_in("worker_1", "Researcher-1", "Researcher", is_lead=False)
        worker.get_self_status = lambda info_type=None: AgentStatus(
            self_id="worker_1", team_id=TEAM, name="Researcher-1", named_agent_type="Researcher",
            agent_role="worker", is_paused=False, is_executing=False, has_execution_capacity=True,
            accepting_work=True, status_type="response", available_skills=["grill-me"])
        lead_host, lead_nc = await _started(stack, broker, LEAD_HOST_RUNTIME, AgentProcessHost(
            layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS)))
        worker_host, worker_nc = await _started(stack, broker, WORKER_RUNTIME, AgentProcessHost(
            layout, worker_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS)))
        lead_host.attach(lead, agent_type="TeamArchitect", definition=DEFINITION, nc=lead_nc, state=("idle", True))
        worker_host.attach(worker, agent_type="Researcher", definition=DEFINITION, nc=worker_nc, state=("idle", True))

        # The lead's record reaches the view the lead follower reads; both members reach the Team page's records.
        await _until(lambda: "lead_1" in view.live())
        assert view.live()["lead_1"].is_lead and view.live()["lead_1"].agent_type == "TeamArchitect"
        await _until(lambda: latest("worker_1") is not None)
        assert (latest("worker_1").name, latest("worker_1").is_team_lead) == ("Researcher-1", False)
        # What only the agent can say (its skills) arrives once a page asks for it (the Team tab opens).
        assert not any(status.available_skills for status in view.statuses())
        view.want_details()
        await _until(lambda: any(status.self_id == "worker_1" and status.available_skills == ["grill-me"]
                                 for status in view.statuses()), timeout=10)
        assert view.live_ids() == {"lead_1", "worker_1"}

        # A worker becoming busy is shown as it happens, with no ask, and keeps its skills.
        worker_host.report("worker_1", "busy", True)
        await _until(lambda: latest("worker_1").is_executing, timeout=5)
        assert next(status for status in view.statuses() if status.self_id == "worker_1").available_skills == [
            "grill-me"]

        # The lead leaves: it is gone from the view, and the Team page shows it leaving.
        await lead_host.stop()
        await _until(lambda: "lead_1" not in view.live())
        assert latest("lead_1").is_exiting and view.departure_reason("lead_1") == "exiting"


@pytest.mark.asyncio
async def test_the_status_page_shows_an_agent_without_a_session_until_its_session_opens(
    broker: str, tmp_path: Path,  # noqa: F811 - the shared live NATS fixture
) -> None:
    """Plan 13B.5: a refused agent has no address, so its host's record lists it, and the gateway shows it."""

    from assistant_gateway.status_page import StatusPage
    from tests.test_status_page import _config, _Notices

    layout = InstanceLayout.at(tmp_path / "instance")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect", "Researcher"), 2)  # the lead and one worker
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM), lead_profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        await _serve(stack, services_host, stop)
        gateway = GatewayMessageHost(layout, TEAM)
        view = GatewayTeamView(TEAM)
        gateway.observe_team(view)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        stack.push_async_callback(gateway.close)
        await _until(view.ready.is_set)

        async def no_keys() -> dict:
            return {"endpoints": []}

        page = StatusPage(config=_config(tmp_path), model_key_status=no_keys, send_notice_once=_Notices(),
                          team_findings=view.team_findings)

        async def without_session() -> list:
            return [finding for finding in (await page.report()).findings
                    if finding.finding_id.startswith("agent_without_session:")]

        lead_host, lead_nc = await _started(stack, broker, LEAD_HOST_RUNTIME, AgentProcessHost(
            layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS), refused_retry_seconds=(0.3, 1.0)))
        lead_host.attach(stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True), agent_type="TeamArchitect",
                         definition=DEFINITION, nc=lead_nc, state=("idle", True))
        lead_host.attach(stand_in("worker_1", "Researcher-1", "Researcher", is_lead=False), agent_type="Researcher",
                         definition=DEFINITION, nc=None, state=("idle", True))
        await _until(lambda: view.live_ids() == {"lead_1", "worker_1"})
        assert await without_session() == []  # starting is no finding

        # A second worker: the lead's process has no room for it, so its session is refused.
        lead_host.attach(stand_in("worker_2", "Researcher-2", "Researcher", is_lead=False), agent_type="Researcher",
                         definition=DEFINITION, nc=None, state=("idle", True))
        for _ in range(200):
            found = await without_session()
            if found:
                break
            await asyncio.sleep(0.05)
        assert [finding.finding_id for finding in found] == ["agent_without_session:lead:session_limit_reached"]
        assert found[0].title == "Agent Researcher-worker_2 cannot run its work"
        assert "worker limit" in found[0].summary and "team-wide restart" in found[0].action
        assert "worker_2" not in view.live_ids()

        # The cause goes: a worker leaves, the refused one gets its session, and the finding goes with it.
        await asyncio.wrap_future(lead_host.detach("worker_1"))
        await _until(lambda: "worker_2" in view.live_ids())
        for _ in range(200):
            found = await without_session()
            if not found:
                break
            await asyncio.sleep(0.05)
        assert found == []


@pytest.mark.asyncio
async def test_a_lead_whose_host_runs_another_contract_is_unavailable_and_shown_on_the_status_page(
    broker: str, tmp_path: Path,  # noqa: F811 - the shared live NATS fixture
) -> None:
    """Contract README "Versions": a chat is new work for the team lead, so a lead whose host runs another contract
    is unavailable to the gateway; it stays on the Team page, not accepting work, and the Status page names its
    host and both digests."""

    from assistant_gateway.status_page import StatusPage
    from custom_messages.wire.kinds import CONTRACT_SHA256
    from tests.test_live_team_services import OTHER_CONTRACT, run_on_another_contract
    from tests.test_status_page import _config, _Notices

    layout = InstanceLayout.at(tmp_path / "instance")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect", "Researcher"), 2)
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM), lead_profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        await _serve(stack, services_host, stop)
        gateway = GatewayMessageHost(layout, TEAM)
        shown: list[AgentStatus] = []
        view = GatewayTeamView(TEAM, on_change=shown.append)
        gateway.observe_team(view)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        stack.push_async_callback(gateway.close)
        await _until(view.ready.is_set)

        async def no_keys() -> dict:
            return {"endpoints": []}

        page = StatusPage(config=_config(tmp_path), model_key_status=no_keys, send_notice_once=_Notices(),
                          team_findings=view.team_findings)

        async def keeps(agent, reason):
            return None

        async def holds(agent):
            return True  # it still holds work: it drains, and keeps its agents meanwhile

        lead_host, lead_nc = await _started(stack, broker, LEAD_HOST_RUNTIME, AgentProcessHost(
            layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS), drain_agent=keeps, holds_work=holds))
        lead_host.attach(stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True), agent_type="TeamArchitect",
                         definition=DEFINITION, nc=lead_nc, state=("idle", True))
        await _until(lambda: "lead_1" in view.live())

        assert lead_host.host is not None
        run_on_another_contract(lead_host.host)
        await lead_host.host.report_now()
        # The lead follower then marks it unavailable (no chats); it stays live, so its tasks' commands reach it.
        await _until(lambda: view.live()["lead_1"].other_contract == OTHER_CONTRACT)
        assert view.live_ids() == {"lead_1"}
        assert shown[-1].self_id == "lead_1" and shown[-1].accepting_work is False and not shown[-1].is_exiting
        found = [finding for finding in (await page.report()).findings
                 if finding.finding_id == f"host_other_contract:{LEAD_HOST_RUNTIME}"]
        assert len(found) == 1 and OTHER_CONTRACT in found[0].details and CONTRACT_SHA256 in found[0].details
        # The Team page says so (review F22).
        [shown_member] = GatewayTeamPresence(members=view.statuses, want_details=view.want_details,
                                             list_agent_classes=list).list_web_chat_team_members(
            presence_only=True).members
        assert (shown_member.agent_id, shown_member.status.label) == ("lead_1", "Not accepting work")


# -- the gateway itself: UserGateway with the gateway service's runtime ---------------------------------


def _gateway_config(tmp_path: Path, nats_url: str, saved: str):
    """A gateway's configuration as the launcher saves it, on this test's NATS server, with the route to the team
    lead its previous run saved."""

    from assistant_gateway.config import GatewayInstanceConfig

    config = GatewayInstanceConfig.create(
        instance_name="live_team_view", instance_root=str(tmp_path / "instance"), team_id=TEAM, timezone="UTC",
        display_name="Team view", manager_agent_id=saved, manager_storage_sqlite_path=str(tmp_path / "manager.db"),
        state_storage_sqlite_path=str(tmp_path / "gateway.db"), named_agent_type="TeamArchitect",
    )
    config.transport.nats_url = nats_url
    config.transport.nats_servers = [nats_url]
    config.save()  # a launched gateway's is saved: the route to the lead is written to it
    return config


async def _running_gateway(stack: AsyncExitStack, config, transitions: list):
    """UserGateway with ``NatsGatewayManagerRuntime.for_gateway_service``, the one that runs the gateway's protocol
    1 host (as run-gateway builds it), its availability transitions recorded, closed when the test ends."""

    from assistant_gateway.gateway import UserGateway
    from assistant_gateway.nats_manager_runtime import NatsGatewayManagerRuntime
    from assistant_gateway.store import GatewayStateStore

    gateway = UserGateway(config, output_sink=None, state_store=GatewayStateStore(str(config.layout.root / "s.db")),
                          manager_runtime=NatsGatewayManagerRuntime.for_gateway_service(config))
    assert gateway._team_view is not None  # the gateway service's runtime has the message host

    async def available(transition) -> None:
        transitions.append(("available", transition.manager_id, transition.became_available))
        await gateway.manager_transitions.handle_available(transition)

    async def unavailable(transition) -> None:
        transitions.append(("unavailable", transition.manager_id))
        await gateway.manager_transitions.handle_unavailable(transition)

    gateway._manager_client.bind_inbound_handlers(on_unsolicited=gateway.inbound_messages.handle,
                                                  on_manager_available=available, on_manager_unavailable=unavailable)
    stack.push_async_callback(gateway.close)
    await gateway.connect(start_services=False)
    return gateway


@pytest.mark.asyncio
async def test_the_gateway_itself_follows_its_lead_from_the_records(
    broker: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811 - the shared live NATS fixture
) -> None:
    """Review F9, through UserGateway and the gateway service's runtime: the saved route of a previous run is cleared
    when the records are read and have no lead; the lead's record sets the route; the Status page shows what the
    lead's host lists; the lead leaving makes it unavailable."""

    from assistant_gateway import team_view

    # Long enough for the join and the first catch-up (under 1 s here) on a busy runner (critique C10).
    monkeypatch.setattr(team_view, "LEAD_START_SECONDS", 5.0)
    config = _gateway_config(tmp_path, broker, saved="lead_from_last_run")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect", "Researcher"), 2)  # the lead and one worker
    transitions: list = []
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, config.layout, team_services_profile(TEAM), gateway_profile(TEAM),
                           lead_profile)
        services_host = MessageHost(config.layout, team_services_profile(TEAM),
                                    await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        await _serve(stack, services_host, stop)

        gateway = await _running_gateway(stack, config, transitions)
        # No lead in the records: the previous run's route is cleared, once.
        assert config.manager.manager_agent_id is None
        assert transitions == [("unavailable", "lead_from_last_run")]

        lead_host, lead_nc = await _started(stack, broker, LEAD_HOST_RUNTIME, AgentProcessHost(
            config.layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS), refused_retry_seconds=(0.3, 1.0)))
        lead_host.attach(stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True), agent_type="TeamArchitect",
                         definition=DEFINITION, nc=lead_nc, state=("idle", True))
        await _until(lambda: config.manager.manager_agent_id == "lead_1")
        assert transitions[-1] == ("available", "lead_1", True)

        # Two workers where the lead's process has room for one: the second gets no session, and the gateway's own
        # Status page shows it, with no lead problem while the lead runs.
        for worker in ("worker_1", "worker_2"):
            lead_host.attach(stand_in(worker, worker, "Researcher", is_lead=False), agent_type="Researcher",
                             definition=DEFINITION, nc=None, state=("idle", True))
        found: list[str] = []
        deadline = asyncio.get_running_loop().time() + 20  # each check also asks the broker this test does not run
        while asyncio.get_running_loop().time() < deadline:
            found = [finding.finding_id for finding in (await gateway.application.status.report()).findings]
            if "agent_without_session:lead:session_limit_reached" in found:
                break
            await asyncio.sleep(0.05)
        assert "agent_without_session:lead:session_limit_reached" in found
        assert "team_lead_not_reporting" not in found

        # The lead leaves: unavailable at once.
        await asyncio.wrap_future(lead_host.detach("lead_1"))
        await _until(lambda: config.manager.manager_agent_id is None)
        assert transitions[-1] == ("unavailable", "lead_1")


@pytest.mark.asyncio
async def test_a_lead_running_when_the_gateway_starts_is_followed_without_any_transition_notice(
    broker: str, tmp_path: Path,  # noqa: F811 - the shared live NATS fixture
) -> None:
    """Review F5: the gateway's start replaces the previous run's saved route with the running lead directly: no
    unavailable transition, and the route is the lead's when connect returns."""

    config = _gateway_config(tmp_path, broker, saved="lead_from_last_run")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect",), 1)
    transitions: list = []
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, config.layout, team_services_profile(TEAM), gateway_profile(TEAM),
                           lead_profile)
        services_host = MessageHost(config.layout, team_services_profile(TEAM),
                                    await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        await _serve(stack, services_host, stop)
        lead_host, lead_nc = await _started(stack, broker, LEAD_HOST_RUNTIME, AgentProcessHost(
            config.layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS)))
        lead_host.attach(stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True), agent_type="TeamArchitect",
                         definition=DEFINITION, nc=lead_nc, state=("idle", True))

        await _running_gateway(stack, config, transitions)
        assert config.manager.manager_agent_id == "lead_1"
        assert transitions == [("available", "lead_1", False)]

