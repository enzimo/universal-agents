"""The gateway's team view (assistant_gateway.team_view) with a stand-in for the team's record reader.

The reader (``custom_transport.protocol1.team_agents.TeamAgents``) has its own tests; here a
stand-in gives the view exactly the members, departures and failures each case needs, and a
stand-in host answers ``agent.status``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional

import pytest

from assistant_gateway import team_view
from assistant_gateway.team_view import GatewayTeamView
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.live import ServiceUnavailable
from custom_transport.protocol1.team_agents import READER_STOPPED, TeamAgent

TEAM = "team_test"


def member(agent_id: str, *, lead: bool = False, state: str = "idle", name: Optional[str] = None,
           other_contract: Optional[str] = None) -> TeamAgent:
    return TeamAgent(agent=agent_id, agent_type="TeamArchitect" if lead else "Researcher",
                     display_name=name if name is not None else agent_id, state=state, accepting_work=True,
                     since=datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc), host="lead", session=f"s-{agent_id}",
                     is_lead=lead, other_contract=other_contract)


class Reader:
    """Stands in for TeamAgents: one instance per test, built by the view through ``team_view.TeamAgents``."""

    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.catches_up = True
        self.fail_runs = 0
        self.runs = 0
        self.crash = asyncio.Event()
        self._listeners: list[Callable[[frozenset[str]], None]] = []
        self._view: dict[str, TeamAgent] = {}
        self._departures: dict[str, str] = {}

    def __call__(self, host: Any, team: str, **_: Any) -> "Reader":
        return self

    def listen(self, listener: Callable[[frozenset[str]], None]) -> None:
        self._listeners.append(listener)

    def live(self) -> Mapping[str, TeamAgent]:
        return dict(self._view)

    def agent(self, agent_id: str) -> Optional[TeamAgent]:
        return self._view.get(agent_id)

    def lead(self) -> Optional[TeamAgent]:
        leads = [item for item in self._view.values() if item.is_lead]
        return leads[0] if len(leads) == 1 else None

    def departure_reason(self, agent_id: str) -> Optional[str]:
        return self._departures.get(agent_id)

    def host_components(self) -> list:
        return []

    def other_contract_hosts(self) -> dict:
        return {}

    def publish(self, *members: TeamAgent, reasons: Optional[dict[str, str]] = None) -> None:
        """The view as the reader would publish it next: these members, the others gone for ``reasons``."""

        view = {item.agent: item for item in members}
        changed = frozenset(agent for agent in set(view) | set(self._view) if view.get(agent) != self._view.get(agent))
        self._departures.update(reasons or {})
        for agent in view:
            self._departures.pop(agent, None)
        self._view = view
        for listener in self._listeners:
            listener(changed)

    async def run(self, stop: asyncio.Event) -> None:
        """As TeamAgents.run: catches up (unless told not to), and on its way out clears readiness and publishes
        an empty view with every member's reason READER_STOPPED."""

        self.runs += 1
        if self.runs <= self.fail_runs:
            raise RuntimeError("the reader failed")
        try:
            if self.catches_up:
                self.ready.set()
            crash = asyncio.create_task(self.crash.wait())
            stopping = asyncio.create_task(stop.wait())
            await asyncio.wait({crash, stopping}, return_when=asyncio.FIRST_COMPLETED)
            crash.cancel()
            stopping.cancel()
            if self.crash.is_set() and not stop.is_set():
                self.crash.clear()
                raise RuntimeError("the reader crashed")
        finally:
            self.ready.clear()
            self.publish(reasons={agent: READER_STOPPED for agent in self._view})


class Host:
    """Stands in for the gateway's joined MessageHost: ``agent.status`` answers by agent id."""

    def __init__(self, answers: Optional[dict[str, Any]] = None) -> None:
        self.identity = SimpleNamespace(installation="install_test")
        self.answers = answers or {}
        self.asked: list[str] = []
        self.live = SimpleNamespace(request=self._request,
                                    trust=SimpleNamespace(store=SimpleNamespace(contract_sha256="1" * 64)))

    async def _request(self, kind: str, body: dict, audience: dict, *, team: str, timeout: float) -> Any:
        agent_id = audience["id"]
        self.asked.append(agent_id)
        answer = self.answers.get(agent_id, {"outcome": "ok"})
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(raw={"body": answer})


async def until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


class Running:
    """A view running on a stand-in reader and host, stopped at the end of the test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, reader: Reader, host: Host, **options: Any) -> None:
        monkeypatch.setattr(team_view, "TeamAgents", reader)
        self.reader, self.host = reader, host
        self.statuses: list[AgentStatus] = []
        self.leads: list[AgentStatus] = []

        async def on_lead(status: AgentStatus) -> None:
            self.leads.append(status)

        options.setdefault("restart_seconds", (0.01, 0.05))
        self.view = GatewayTeamView(TEAM, on_status=lambda status, at, source: self.statuses.append(status),
                                    on_lead=on_lead, **options)
        self.stop = asyncio.Event()
        self.task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "Running":
        self.task = asyncio.create_task(self.view.run(self.host, self.stop))
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.stop.set()
        assert self.task is not None
        await asyncio.wait_for(self.task, timeout=2)


@pytest.mark.asyncio
async def test_the_view_stops_when_the_gateway_stops_even_before_the_records_were_read(monkeypatch) -> None:
    """Review F2: a reader that never catches up (a watch that keeps failing) must not hold the gateway's close."""

    reader = Reader()
    reader.catches_up = False
    async with Running(monkeypatch, reader, Host()) as running:
        await asyncio.sleep(0.05)
        assert not running.view.ready.is_set()
    assert running.task is not None and running.task.done()


@pytest.mark.asyncio
async def test_one_agents_failed_answer_is_logged_and_the_others_count(monkeypatch, caplog) -> None:
    """Review F1: a reply that fails verification ends neither the round nor the asking."""

    reader = Reader()
    host = Host({"worker_bad": FrameRefused("not_yet_valid", at="/issued_at"),
                 "worker_quiet": ServiceUnavailable("agent.status", "ctl.agent.worker_quiet", "no_answer_in_time"),
                 "worker_ok": {"outcome": "ok", "skills": ["grill-me"]}})
    with caplog.at_level(logging.INFO, logger="assistant_gateway.team_view"):
        async with Running(monkeypatch, reader, host, details_seconds=0.02) as running:
            reader.publish(member("worker_bad"), member("worker_ok"), member("worker_quiet"))
            await until(lambda: host.asked.count("worker_bad") >= 2 and host.asked.count("worker_ok") >= 2)
            shown = [status for status in running.statuses if status.self_id == "worker_ok"]
            assert shown[-1].available_skills == ["grill-me"]
    assert "event=gateway_agent_status_failed agent=worker_bad error_type=FrameRefused" in caplog.text
    assert "event=gateway_agent_status_unanswered agent=worker_quiet" in caplog.text


@pytest.mark.asyncio
async def test_a_part_of_the_view_that_fails_is_logged_and_started_again(monkeypatch, caplog) -> None:
    reader = Reader()
    reader.fail_runs = 1
    with caplog.at_level(logging.INFO, logger="assistant_gateway.team_view"):
        async with Running(monkeypatch, reader, Host()) as running:
            await until(lambda: running.view.ready.is_set())
    assert reader.runs == 2
    assert "event=gateway_team_view_task_failed task=gateway_team_agents error_type=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_a_part_of_the_view_that_ends_early_is_logged_and_started_again(monkeypatch, caplog) -> None:
    reader = Reader()
    calls: list[str] = []

    async def ends_at_once(stop: asyncio.Event) -> None:
        calls.append("details")

    async with Running(monkeypatch, reader, Host()) as running:
        running.view._ask_details = ends_at_once
        await until(lambda: len(calls) >= 2)
    assert "event=gateway_team_view_task_ended task=gateway_team_details" in caplog.text


@pytest.mark.asyncio
async def test_a_departure_is_logged_with_its_reason(monkeypatch, caplog) -> None:
    """Review F15: the gateway says why a member left."""

    reader = Reader()
    with caplog.at_level(logging.INFO, logger="assistant_gateway.team_view"):
        async with Running(monkeypatch, reader, Host(), details_seconds=30) as running:
            await until(lambda: running.view.ready.is_set())
            reader.publish(member("lead_1", lead=True), member("worker_1"))
            reader.publish(member("lead_1", lead=True), reasons={"worker_1": "host_unavailable"})
            await until(lambda: any(status.self_id == "worker_1" and status.is_exiting for status in running.statuses))
    assert "event=gateway_team_member_left agent=worker_1 lead=False reason=host_unavailable" in caplog.text


@pytest.mark.asyncio
async def test_a_reader_that_stops_and_starts_again_is_no_departure(monkeypatch) -> None:
    """Nobody left when only the reader stopped: no member is shown leaving and the lead's route is not touched."""

    reader = Reader()
    async with Running(monkeypatch, reader, Host(), details_seconds=30) as running:
        await until(lambda: running.view.ready.is_set())
        reader.publish(member("lead_1", lead=True), member("worker_1"))
        await until(lambda: len(running.leads) == 1)
        reader.crash.set()
        await until(lambda: reader.runs == 2 and running.view.ready.is_set())
        reader.publish(member("lead_1", lead=True), member("worker_1"))
        await until(lambda: len(running.leads) == 2)
        assert not any(status.is_exiting for status in running.statuses + running.leads)
