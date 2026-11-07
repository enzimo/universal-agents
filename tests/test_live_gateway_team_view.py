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
    leads: list[AgentStatus] = []

    async def on_lead(status: AgentStatus) -> None:
        leads.append(status)

    def latest(agent_id: str) -> AgentStatus | None:
        return next((status for status in reversed(statuses) if status.self_id == agent_id), None)

    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM), lead_profile,
                           worker_profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))
        gateway = GatewayMessageHost(layout, TEAM)
        view = GatewayTeamView(TEAM, on_status=lambda status, reported_at, source: statuses.append(status),
                               on_lead=on_lead, details_seconds=0.3)
        gateway.observe_team(view)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        assert await view.wait_ready(20)

        lead = stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True)
        worker = stand_in("worker_1", "Researcher-1", "Researcher", is_lead=False)
        worker.get_self_status = lambda info_type=None: AgentStatus(
            self_id="worker_1", team_id=TEAM, name="Researcher-1", named_agent_type="Researcher",
            agent_role="worker", is_paused=False, is_executing=False, has_execution_capacity=True,
            accepting_work=True, status_type="response", available_skills=["grill-me"])
        lead_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
        worker_host = AgentProcessHost(layout, worker_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
        lead_host.attach(lead, agent_type="TeamArchitect", definition=DEFINITION,
                         nc=await connect(stack, broker, LEAD_HOST_RUNTIME), state=("idle", True))
        worker_host.attach(worker, agent_type="Researcher", definition=DEFINITION,
                           nc=await connect(stack, broker, WORKER_RUNTIME), state=("idle", True))

        # The lead's record reaches the manager client's path; both members reach the Team page's records.
        await _until(lambda: any(status.self_id == "lead_1" and not status.is_exiting for status in leads))
        assert leads[-1].is_team_lead and leads[-1].named_agent_type == "TeamArchitect"
        await _until(lambda: latest("worker_1") is not None)
        assert (latest("worker_1").name, latest("worker_1").is_team_lead) == ("Researcher-1", False)
        # What only the agent can say (its skills) arrives once the gateway asks it.
        await _until(lambda: latest("worker_1").available_skills == ["grill-me"], timeout=10)
        assert view.lead().agent == "lead_1" and view.live_ids() == {"lead_1", "worker_1"}

        # A worker becoming busy is shown as it happens.
        worker_host.report("worker_1", "busy", True)
        await _until(lambda: latest("worker_1").is_executing, timeout=5)

        # The lead leaves: the manager client hears it, so the gateway marks it unavailable.
        await lead_host.stop()
        await _until(lambda: leads[-1].self_id == "lead_1" and leads[-1].is_exiting)
        assert view.lead() is None

        await worker_host.stop()
        await gateway.close()
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


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
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))
        gateway = GatewayMessageHost(layout, TEAM)
        view = GatewayTeamView(TEAM, on_status=lambda status, reported_at, source: None, on_lead=AsyncMock(),
                               details_seconds=30)
        gateway.observe_team(view)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        assert await view.wait_ready(20)

        async def no_keys() -> dict:
            return {"endpoints": []}

        page = StatusPage(config=_config(tmp_path), model_key_status=no_keys, agent_statuses=lambda: [],
                          send_notice_once=_Notices(), host_findings=view.host_findings)

        async def without_session() -> list:
            return [finding for finding in (await page.report()).findings
                    if finding.finding_id.startswith("agent_without_session:")]

        lead_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS),
                                     refused_retry_seconds=(0.3, 1.0))
        lead_host.attach(stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True), agent_type="TeamArchitect",
                         definition=DEFINITION, nc=await connect(stack, broker, LEAD_HOST_RUNTIME), state=("idle", True))
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

        await lead_host.stop()
        await gateway.close()
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


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
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))
        gateway = GatewayMessageHost(layout, TEAM)
        leads: list[AgentStatus] = []
        shown: list[AgentStatus] = []

        async def on_lead(status: AgentStatus) -> None:
            leads.append(status)

        view = GatewayTeamView(TEAM, on_status=lambda status, reported_at, source: shown.append(status),
                               on_lead=on_lead, details_seconds=30)
        gateway.observe_team(view)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        assert await view.wait_ready(20)

        async def no_keys() -> dict:
            return {"endpoints": []}

        page = StatusPage(config=_config(tmp_path), model_key_status=no_keys, agent_statuses=lambda: [],
                          send_notice_once=_Notices(), host_findings=view.host_findings)

        async def keeps(agent, reason):
            return None

        async def holds(agent):
            return True  # it still holds work: it drains, and keeps its agents meanwhile

        lead_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS),
                                     drain_agent=keeps, holds_work=holds)
        lead_host.attach(stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True), agent_type="TeamArchitect",
                         definition=DEFINITION, nc=await connect(stack, broker, LEAD_HOST_RUNTIME), state=("idle", True))
        await _until(lambda: view.lead() is not None and leads and not leads[-1].is_exiting)

        assert lead_host.host is not None
        run_on_another_contract(lead_host.host)
        await lead_host.host.report_now()
        await _until(lambda: leads[-1].is_exiting)  # for the manager client: unavailable, no chats
        assert view.lead() is None and view.live_ids() == {"lead_1"}
        assert shown[-1].self_id == "lead_1" and shown[-1].accepting_work is False and not shown[-1].is_exiting
        found = [finding for finding in (await page.report()).findings
                 if finding.finding_id == f"host_other_contract:{LEAD_HOST_RUNTIME}"]
        assert len(found) == 1 and OTHER_CONTRACT in found[0].details and CONTRACT_SHA256 in found[0].details

        await lead_host.stop()
        await gateway.close()
        stop.set()
        await asyncio.wait_for(serving, timeout=10)
