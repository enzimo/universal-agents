"""The gateway's team view (assistant_gateway.team_view) with a stand-in for the team's record reader.

The reader (``custom_transport.protocol1.team_agents.TeamAgents``) has its own tests; here a
stand-in gives the view exactly the members, departures and failures each case needs, and a
stand-in host answers ``agent.status``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional
from unittest.mock import AsyncMock

import pytest

from assistant_gateway import team_view
from assistant_gateway.application.manager_client import GatewayManagerClient, GatewayManagerClientSettings
from assistant_gateway.team_view import GatewayTeamView, TeamLeadFollower
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.live import ServiceUnavailable
from custom_transport.protocol1.team_agents import READER_STOPPED, TeamAgent

TEAM = "team_test"


def member(agent_id: str, *, lead: bool = False, state: str = "idle", name: Optional[str] = None,
           other_contract: Optional[str] = None, minute: int = 0) -> TeamAgent:
    return TeamAgent(agent=agent_id, agent_type="TeamArchitect" if lead else "Researcher",
                     display_name=name if name is not None else agent_id, state=state, accepting_work=True,
                     since=datetime(2026, 11, 11, 9, minute, tzinfo=timezone.utc), host="lead",
                     session=f"s-{agent_id}", is_lead=lead, other_contract=other_contract)


class Reader:
    """Stands in for TeamAgents: one instance per test, built by the view through ``team_view.TeamAgents``."""

    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.catches_up = True
        # The members a run finds when it catches up (a restarted reader reads the buckets again).
        self.found: tuple[TeamAgent, ...] = ()
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
                if self.found:
                    self.publish(*self.found)
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
        options.setdefault("restart_seconds", (0.01, 0.05))
        self.view = GatewayTeamView(TEAM, on_change=self.statuses.append, **options)
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
        async with Running(monkeypatch, reader, host, details_fresh_seconds=0) as running:
            await until(running.view.ready.is_set)
            reader.publish(member("worker_bad"), member("worker_ok"), member("worker_quiet"))
            await running.view.refresh_details()
            await running.view.refresh_details()  # later asks go on
            assert host.asked.count("worker_bad") == 2 and host.asked.count("worker_ok") == 2
            shown = {status.self_id: status for status in running.view.statuses()}
            assert shown["worker_ok"].available_skills == ["grill-me"] and shown["worker_bad"].available_skills == []
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
        running.view._answer_details_wanted = ends_at_once
        await until(lambda: len(calls) >= 2)
    assert "event=gateway_team_view_task_ended task=gateway_team_details" in caplog.text


@pytest.mark.asyncio
async def test_a_departure_is_logged_with_its_reason(monkeypatch, caplog) -> None:
    """Review F15: the gateway says why a member left."""

    reader = Reader()
    with caplog.at_level(logging.INFO, logger="assistant_gateway.team_view"):
        async with Running(monkeypatch, reader, Host()) as running:
            await until(lambda: running.view.ready.is_set())
            reader.publish(member("lead_1", lead=True), member("worker_1"))
            reader.publish(member("lead_1", lead=True), reasons={"worker_1": "host_unavailable"})
            await until(lambda: any(status.self_id == "worker_1" and status.is_exiting for status in running.statuses))
    assert "event=gateway_team_member_left agent=worker_1 lead=False reason=host_unavailable" in caplog.text


@pytest.mark.asyncio
async def test_a_reader_that_stops_and_starts_again_is_no_departure(monkeypatch) -> None:
    """Nobody left when only the reader stopped: no member is shown leaving (the lead's route: the follower tests)."""

    reader = Reader()
    async with Running(monkeypatch, reader, Host()) as running:
        await until(lambda: running.view.ready.is_set())
        reader.publish(member("lead_1", lead=True), member("worker_1"))
        reader.found = (member("lead_1", lead=True), member("worker_1"))
        reader.crash.set()
        await until(lambda: reader.runs == 2 and running.view.ready.is_set())
        assert [status.self_id for status in running.statuses] == ["lead_1", "worker_1"] * 2
        assert not any(status.is_exiting for status in running.statuses)


# -- the Team page and details on demand -----------------------------------------------------------------


def _presence(view: GatewayTeamView):
    from assistant_gateway.team_presence import GatewayTeamPresence

    return GatewayTeamPresence(members=view.statuses, want_details=view.want_details, list_agent_classes=list)


def _shown(view: GatewayTeamView) -> dict[str, AgentStatus]:
    return {status.self_id: status for status in view.statuses()}


@pytest.mark.asyncio
async def test_nothing_asks_the_agents_until_a_page_needs_their_details(monkeypatch) -> None:
    """Decision 69 (review F6): agent.status is asked when the Team tab is opened or refreshed, once in the fresh
    window, and never by the two-second live update, the activity mesh or Tasks."""

    reader = Reader()
    host = Host({"worker_1": {"outcome": "ok", "skills": ["grill-me"]}})
    async with Running(monkeypatch, reader, host) as running:
        await until(running.view.ready.is_set)
        reader.publish(member("lead_1", lead=True), member("worker_1"))
        presence = _presence(running.view)
        await asyncio.sleep(0.2)
        for _ in range(5):
            presence.list_web_chat_team_members(presence_only=True)
        await asyncio.sleep(0.1)
        assert host.asked == []

        presence.list_web_chat_team_members()  # the Team tab opens
        await until(lambda: sorted(host.asked) == ["lead_1", "worker_1"])
        await until(lambda: _shown(running.view)["worker_1"].available_skills == ["grill-me"])
        presence.list_web_chat_team_members()  # Refresh, within the fresh window
        await asyncio.sleep(0.1)
        assert sorted(host.asked) == ["lead_1", "worker_1"]


@pytest.mark.asyncio
async def test_a_page_handlers_thread_can_ask_for_details(monkeypatch) -> None:
    reader = Reader()
    host = Host()
    async with Running(monkeypatch, reader, host) as running:
        await until(running.view.ready.is_set)
        reader.publish(member("worker_1"))
        await asyncio.to_thread(running.view.want_details)
        await until(lambda: host.asked == ["worker_1"])


@pytest.mark.asyncio
async def test_a_record_change_shows_at_once_and_keeps_the_answered_skills_and_findings(monkeypatch) -> None:
    """Review F21 (mutations M26, M15, M7): a busy record shows without any ask, and what the last answer said
    (skills, the agent's own findings) stays with the member across record changes."""

    finding = {"finding_id": "mcp_unreachable:docs", "severity": "problem", "title": "An MCP server does not answer"}
    reader = Reader()
    host = Host({"worker_1": {"outcome": "ok", "skills": ["grill-me"], "findings": [finding]}})
    async with Running(monkeypatch, reader, host) as running:
        await until(running.view.ready.is_set)
        reader.publish(member("worker_1"))
        await running.view.refresh_details()
        reader.publish(member("worker_1", state="busy"))
        shown = _shown(running.view)["worker_1"]
        assert shown.is_executing and host.asked == ["worker_1"]
        assert shown.available_skills == ["grill-me"]
        assert [item.finding_id for item in shown.status_findings] == ["mcp_unreachable:docs"]


@pytest.mark.asyncio
async def test_an_answered_capacity_counts_only_while_the_record_is_unchanged(monkeypatch) -> None:
    """Review F8 (and M8): asked while busy, the agent said it could start nothing more; once its record says idle
    it is Available at once. While the record is the one it was asked under, the answer counts."""

    from assistant_gateway.team_presence import _status_label

    reader = Reader()
    host = Host({"worker_1": {"outcome": "ok", "has_capacity": False}})
    async with Running(monkeypatch, reader, host, details_fresh_seconds=0) as running:
        await until(running.view.ready.is_set)
        reader.publish(member("worker_1", state="busy"))
        await running.view.refresh_details()
        reader.publish(member("worker_1"))
        assert _status_label(_shown(running.view)["worker_1"])["key"] == "available"

        await running.view.refresh_details()  # asked while idle, it says it has no room (its lane is held)
        assert _status_label(_shown(running.view)["worker_1"])["key"] == "busy"


@pytest.mark.asyncio
async def test_a_member_that_left_is_shown_exiting_for_a_minute_then_forgotten(monkeypatch) -> None:
    """Review F21 (M22) and critique C04: a leaving member is shown, then dropped with its details."""

    now = [datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)]
    reader = Reader()
    host = Host({"worker_1": {"outcome": "ok", "skills": ["grill-me"]}})
    async with Running(monkeypatch, reader, host, clock=lambda: now[0]) as running:
        await until(running.view.ready.is_set)
        reader.publish(member("lead_1", lead=True), member("worker_1"))
        await running.view.refresh_details()
        reader.publish(member("lead_1", lead=True), reasons={"worker_1": "exiting"})
        assert _shown(running.view)["worker_1"].is_exiting
        now[0] += timedelta(seconds=61)
        assert "worker_1" not in _shown(running.view)
        reader.publish(member("lead_1", lead=True, state="busy"))
        assert "worker_1" not in running.view._details and "worker_1" not in running.view._departed


@pytest.mark.asyncio
async def test_a_member_that_does_not_answer_stays_listed(monkeypatch) -> None:
    """Review F7: the records count the lead live, so it is listed whether or not it answers agent.status."""

    from assistant_gateway.team_presence import _status_label

    now = [datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)]
    reader = Reader()
    host = Host({"lead_1": ServiceUnavailable("agent.status", "ctl.agent.lead_1", "no_answer_in_time")})
    async with Running(monkeypatch, reader, host, clock=lambda: now[0], details_fresh_seconds=0) as running:
        await until(running.view.ready.is_set)
        reader.publish(member("lead_1", lead=True))
        for _ in range(3):
            await running.view.refresh_details()
            now[0] += timedelta(minutes=1)
        shown = _shown(running.view)["lead_1"]
        assert _status_label(shown)["key"] == "available" and shown.is_team_lead
        assert shown.last_seen_at == now[0].isoformat()


@pytest.mark.asyncio
async def test_agents_on_another_contract_are_not_asked_and_take_no_new_work(monkeypatch) -> None:
    """Review F22: they cannot answer in this contract, and the Team page says they are not accepting work."""

    reader = Reader()
    host = Host()
    async with Running(monkeypatch, reader, host) as running:
        await until(running.view.ready.is_set)
        reader.publish(member("worker_1", other_contract="2" * 64), member("worker_2"))
        await running.view.refresh_details()
        assert host.asked == ["worker_2"]
        members = {item.agent_id: item for item in _presence(running.view).list_web_chat_team_members(
            presence_only=True).members}
        assert (members["worker_1"].status.key, members["worker_1"].status.label) == ("not_accepting",
                                                                                       "Not accepting work")
        assert members["worker_2"].status.key == "available"


@pytest.mark.asyncio
async def test_a_blank_display_name_shows_as_the_agents_id(monkeypatch) -> None:
    """Review F18: a name of spaces no longer makes the whole team list invalid."""

    reader = Reader()
    async with Running(monkeypatch, reader, Host()) as running:
        await until(running.view.ready.is_set)
        reader.publish(member("worker_1", name="   "), member("worker_2"))
        members = _presence(running.view).list_web_chat_team_members(presence_only=True).members
        assert sorted(item.name for item in members) == ["Agent_worker_1", "worker_2"]


@pytest.mark.asyncio
async def test_a_reader_that_starts_again_shows_nobody_leaving(monkeypatch) -> None:
    """Critique C05(b): the reader's own stop is no departure, so nobody is shown as exiting."""

    reader = Reader()
    async with Running(monkeypatch, reader, Host()) as running:
        await until(running.view.ready.is_set)
        reader.publish(member("lead_1", lead=True), member("worker_1"))
        reader.crash.set()
        await until(lambda: reader.runs == 2 and running.view.ready.is_set())
        assert not any(status.is_exiting for status in running.view.statuses())
        reader.found = (member("lead_1", lead=True), member("worker_1"))
        reader.crash.set()
        await until(lambda: reader.runs == 3 and running.view.ready.is_set())
        assert sorted(_shown(running.view)) == ["lead_1", "worker_1"]
        assert not any(status.is_exiting for status in running.view.statuses())


@pytest.mark.asyncio
async def test_an_unreadable_finding_in_an_answer_is_logged_and_left_out(monkeypatch, caplog) -> None:
    reader = Reader()
    host = Host({"worker_1": {"outcome": "ok", "findings": [{"finding_id": "x"}]}})
    async with Running(monkeypatch, reader, host) as running:
        await until(running.view.ready.is_set)
        reader.publish(member("worker_1"))
        await running.view.refresh_details()
        assert _shown(running.view)["worker_1"].status_findings == []
    assert "event=gateway_agent_finding_invalid agent=worker_1" in caplog.text


# -- the lead follower (TeamLeadFollower) ---------------------------------------------------------------


class Waiting:
    """Stands in for the transitions' fail_waiting: the replies it was asked to fail."""

    def __init__(self) -> None:
        self.failed: list[list[str]] = []

    async def fail_waiting(self, correlation_ids) -> None:
        self.failed.append(list(correlation_ids))


class Following:
    """A running view, the real manager client holding the route, and the follower between them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, reader: Reader, *, saved: Optional[str] = None) -> None:
        self.running = Running(monkeypatch, reader, Host())
        self.reader, self.view = reader, self.running.view
        self.writes: list[Optional[str]] = []
        self.available: list = []
        self.unavailable: list = []
        self.client = GatewayManagerClient(
            settings=GatewayManagerClientSettings(instance_name="test", user_display_name="Test", team_id=TEAM,
                                                  manager_named_agent_type="TeamArchitect",
                                                  response_route="test_gateway_response", initial_manager_id=saved),
            id_generator=SimpleNamespace(),
            logger=logging.getLogger("test.gateway.lead_follower"), write_manager_route=self.writes.append,
            on_manager_available=self.available.append, on_manager_unavailable=self.unavailable.append)
        self.waiting = Waiting()
        self.follower = TeamLeadFollower(self.view, self.client, self.waiting, check_seconds=0.02)
        self.stop = asyncio.Event()
        self.task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "Following":
        await self.running.__aenter__()
        return self

    def follow(self) -> None:
        """After ``start``: the gateway's background check (UserGateway.connect)."""

        self.task = asyncio.create_task(self.follower.run(self.stop))

    async def __aexit__(self, *_: Any) -> None:
        self.stop.set()
        if self.task is not None:
            await asyncio.wait_for(self.task, timeout=2)
        await self.running.__aexit__()

    def transitions(self) -> list[tuple[str, str, bool]]:
        return ([("unavailable", item.manager_id, item.keep_waiting) for item in self.unavailable]
                + [("available", item.manager_id, item.became_available) for item in self.available])


@pytest.mark.asyncio
async def test_a_lead_that_writes_its_record_during_the_start_wait_is_followed_quietly(monkeypatch) -> None:
    """Review F5: at a normal team start the gateway reads the records before the lead has written its own. The start
    waits for the lead instead of marking the saved route unavailable and the lead available again."""

    reader = Reader()
    async with Following(monkeypatch, reader, saved="lead_from_last_run") as following:
        starting = asyncio.create_task(following.follower.start(limit=5))
        await until(lambda: following.view.ready.is_set())
        await asyncio.sleep(0.05)
        assert not starting.done()
        reader.publish(member("lead_1", lead=True))
        await asyncio.wait_for(starting, timeout=1)
        assert following.client.manager_id == "lead_1" and following.writes == ["lead_1"]
        assert following.unavailable == [] and [item.became_available for item in following.available] == [False]


@pytest.mark.asyncio
async def test_records_without_a_lead_at_the_start_limit_clear_the_saved_route_once(monkeypatch) -> None:
    reader = Reader()
    async with Following(monkeypatch, reader, saved="lead_from_last_run") as following:
        await following.follower.start(limit=0.1)
        assert following.writes == [None] and following.transitions() == [("unavailable", "lead_from_last_run", False)]
        following.follow()
        reader.publish(member("lead_1", lead=True))
        await until(lambda: following.client.manager_id == "lead_1")
        assert following.transitions()[-1] == ("available", "lead_1", True)  # it was missing: the recovery notice


@pytest.mark.asyncio
@pytest.mark.parametrize("lead", [None, "lead_new"])
async def test_records_read_after_the_start_limit_still_correct_a_dead_route(monkeypatch, caplog, lead) -> None:
    """Review F3: the records are read only after the start's wait; the saved route of the previous run is dead."""

    reader = Reader()
    reader.catches_up = False
    async with Following(monkeypatch, reader, saved="lead_from_last_run") as following:
        await following.follower.start(limit=0.05)
        assert following.writes == [] and "event=gateway_team_view_not_ready" in caplog.text
        assert "follows them" not in caplog.text
        following.follow()
        if lead is not None:
            reader.publish(member(lead, lead=True))
        reader.ready.set()  # the reader catches up late
        expected = [("unavailable", "lead_from_last_run", False)] + ([("available", lead, True)] if lead else [])
        await until(lambda: following.transitions() == expected)
        await asyncio.sleep(0.1)
        assert following.transitions() == expected and following.client.manager_id == lead


@pytest.mark.asyncio
async def test_a_lead_whose_record_is_older_than_the_unavailable_mark_is_still_followed(monkeypatch) -> None:
    """Review F12: after the wall clock stepped back, a lead's arrival carried an older time than the unavailable
    mark and was dropped as stale. The lead path compares no times now (the manager client keeps no clock); a
    record dated an hour before the mark is followed."""

    reader = Reader()
    async with Following(monkeypatch, reader, saved="lead_from_last_run") as following:
        await following.follower.start(limit=0.05)
        following.follow()
        assert following.client.manager_id is None
        older = member("lead_1", lead=True)
        reader.publish(TeamAgent(**{**older.__dict__, "since": older.since - timedelta(hours=1)}))
        await until(lambda: following.client.manager_id == "lead_1")


@pytest.mark.asyncio
async def test_two_leads_at_the_first_reading_never_leave_the_route_cleared(monkeypatch, caplog) -> None:
    """Review F13: a crashed run's lead record beside the new one. The new one (its record changed last) is followed."""

    reader = Reader()
    reader.publish(member("lead_old_run", lead=True, minute=1), member("lead_new", lead=True, minute=7))
    async with Following(monkeypatch, reader, saved="lead_from_last_run") as following:
        await following.follower.start(limit=1)
        assert following.writes == ["lead_new"] and following.unavailable == []
    assert caplog.text.count("event=gateway_multiple_leads leads=lead_new,lead_old_run kept=lead_new") == 1


@pytest.mark.asyncio
async def test_a_second_lead_never_takes_the_chats_over(monkeypatch, caplog) -> None:
    """Review F14: a second lead-role session; the route stays on the current lead while it is live."""

    reader = Reader()
    reader.publish(member("lead_1", lead=True))
    async with Following(monkeypatch, reader) as following:
        await following.follower.start(limit=1)
        following.follow()
        for minute, state in enumerate(("idle", "busy", "idle", "busy"), start=10):
            reader.publish(member("lead_1", lead=True, state=state),
                           member("lead_2", lead=True, state=state, minute=minute))
            await asyncio.sleep(0.05)
        assert following.client.manager_id == "lead_1" and following.writes == ["lead_1"]
    assert caplog.text.count("event=gateway_multiple_leads leads=lead_1,lead_2 kept=lead_1") == 1


@pytest.mark.asyncio
async def test_a_lead_on_another_contract_keeps_the_waiting_replies_until_it_leaves(monkeypatch) -> None:
    """Review F16 and critique C01: no new chats, but people's waiting replies stay while it finishes its tasks; a
    reply that arrives is delivered, and those still waiting when it leaves are failed then."""

    reader = Reader()
    reader.publish(member("lead_1", lead=True))
    async with Following(monkeypatch, reader) as following:
        await following.follower.start(limit=1)
        following.follow()
        answered = following.client.register_response_channel("corr_answered")
        following.client.register_response_channel("corr_waiting")

        reader.publish(member("lead_1", lead=True, other_contract="2" * 64))
        await until(lambda: following.unavailable != [])
        assert following.transitions() == [("unavailable", "lead_1", True), ("available", "lead_1", True)]
        assert following.client.manager_id is None
        await asyncio.sleep(0.1)
        assert following.waiting.failed == []

        reply = SimpleNamespace(msg_content=None)
        assert await following.client.queue_response("corr_answered", reply)
        following.client.unregister_response_channel("corr_answered")  # the person got the lead's answer
        assert answered.get_nowait() is reply

        reader.publish(reasons={"lead_1": "exiting"})
        await until(lambda: following.waiting.failed != [])
        assert following.waiting.failed == [["corr_waiting"]]
        assert len(following.unavailable) == 1  # no second notice


@pytest.mark.asyncio
async def test_a_refused_join_clears_the_route_whenever_it_comes_until_the_host_joins_again(monkeypatch) -> None:
    """Review F4 and critique C02: a final refusal after the start limit still stops chats to a lead nobody sees."""

    reader = Reader()
    reader.catches_up = False
    async with Following(monkeypatch, reader, saved="lead_from_last_run") as following:
        await following.follower.start(limit=0.05)
        following.follow()
        assert following.client.manager_id == "lead_from_last_run"
        following.view.refuse("protocol_mismatch", "This gateway runs another message contract than the installation.")
        await until(lambda: following.client.manager_id is None)
        assert following.transitions() == [("unavailable", "lead_from_last_run", False)]

        following.view.joining()
        reader.publish(member("lead_1", lead=True))
        reader.ready.set()
        await until(lambda: following.client.manager_id == "lead_1")
        assert following.view.refusal is None


@pytest.mark.asyncio
@pytest.mark.parametrize("lead_stays", [False, True])
async def test_a_lead_that_leaves_while_the_reader_starts_again_is_seen_gone(monkeypatch, lead_stays) -> None:
    """Critique C07: the restarted reader's view no longer has the lead and nothing names its departure; the check
    against the records still marks it unavailable, once. A restart that finds the lead again changes nothing."""

    reader = Reader()
    reader.publish(member("lead_1", lead=True))
    async with Following(monkeypatch, reader) as following:
        await following.follower.start(limit=1)
        following.follow()
        reader.found = (member("lead_1", lead=True),) if lead_stays else ()
        reader.crash.set()
        await until(lambda: reader.runs == 2 and following.view.ready.is_set())
        await asyncio.sleep(0.1)
        expected = [("available", "lead_1", True)] + ([] if lead_stays else [("unavailable", "lead_1", False)])
        assert sorted(following.transitions()) == sorted(expected)



# -- the gateway's own start-up and wiring (UserGateway) ------------------------------------------------


class GatewayHost:
    """Stands in for GatewayMessageHost: once 'joined' it runs the gateway's team view on a stand-in host, or it
    reports a refused join to the view as the real one does."""

    def __init__(self, refusal: Optional[tuple[str, str]] = None) -> None:
        self.team_view: Optional[GatewayTeamView] = None
        self.refusal = refusal
        self.stop = asyncio.Event()
        self.task: Optional[asyncio.Task] = None

    def observe_team(self, view: GatewayTeamView) -> None:
        self.team_view = view

    def start(self) -> None:
        assert self.team_view is not None
        self.team_view.joining()
        if self.refusal is not None:
            self.team_view.refuse(*self.refusal)
            return
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self.team_view.run(Host(), self.stop))

    async def close(self) -> None:
        self.stop.set()
        if self.task is not None:
            await asyncio.wait_for(self.task, timeout=2)


def _gateway(tmp_path, monkeypatch, reader: Reader, *, saved: Optional[str] = "manager_1234",
             refusal: Optional[tuple[str, str]] = None, start_limit: float = 5.0):
    """A UserGateway whose runtime has a message host, as the gateway service's has
    (``NatsGatewayManagerRuntime.for_gateway_service``); its transitions are recorded as the manager client
    hands them on."""

    from assistant_gateway.gateway import UserGateway
    from assistant_gateway.store import GatewayStateStore
    from tests.test_gateway_phase1 import _build_test_config, _FakeGatewayManagerRuntime

    class HostedRuntime(_FakeGatewayManagerRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.connected = False
            self.message_host = GatewayHost(refusal)

        async def connect(self, **_kwargs: Any) -> None:
            self.connected = True
            self.message_host.start()

        async def close(self) -> None:
            await self.message_host.close()
            self.connected = False

    monkeypatch.setattr(team_view, "TeamAgents", reader)
    monkeypatch.setattr(team_view, "LEAD_START_SECONDS", start_limit)
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = saved
    delivered: list = []
    gateway = UserGateway(config, output_sink=None, event_sink=delivered.append,
                          state_store=GatewayStateStore(str(tmp_path / "state.db")), manager_runtime=HostedRuntime())
    seen: list[tuple] = []
    hooks: list[str] = []

    async def available(transition) -> None:
        seen.append(("available", transition.manager_id, transition.became_available))
        await gateway.manager_transitions.handle_available(transition)

    async def unavailable(transition) -> None:
        seen.append(("unavailable", transition.manager_id))
        await gateway.manager_transitions.handle_unavailable(transition)

    gateway._manager_client.bind_inbound_handlers(on_unsolicited=gateway.inbound_messages.handle,
                                                  on_manager_available=available,
                                                  on_manager_unavailable=unavailable)
    gateway.manager_transitions.set_hooks(on_manager_unavailable=lambda: hooks.append("pause"),
                                          on_manager_available=lambda: hooks.append("resume"))
    gateway._maintenance_schedules.ensure_all = AsyncMock()
    return gateway, SimpleNamespace(seen=seen, hooks=hooks, delivered=delivered)


async def _publish_when_read(reader: Reader, *members: TeamAgent) -> None:
    """The lead writes its record a moment after the gateway has read the team's records (a normal start)."""

    await until(reader.ready.is_set)
    await asyncio.sleep(0.05)
    reader.publish(*members)


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", ["lead_from_last_run", None])
async def test_a_normal_team_start_routes_to_the_lead_with_no_unavailable_notice_or_pause(tmp_path, monkeypatch,
                                                                                          saved) -> None:
    """Review F5 through the gateway's own start: the hooks and notices are already set, and still nothing fires
    (the follower decides before the transitions start); the route is the lead's when connect returns, which is
    what assistant_cli reads to decide whether to pause Telegram polling."""

    reader = Reader()
    gateway, recorded = _gateway(tmp_path, monkeypatch, reader, saved=saved)
    gateway.manager_transitions.enable_notices()
    arriving = asyncio.create_task(_publish_when_read(reader, member("lead_1", lead=True)))
    await gateway.connect(start_services=False)
    try:
        await arriving
        assert gateway.config.manager.manager_agent_id == "lead_1"
        assert recorded.seen == [("available", "lead_1", saved is None)]
        assert recorded.hooks == [] and recorded.delivered == []
        assert "gateway_team_lead" in {task.get_name() for task in gateway._background_tasks}
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_a_start_with_no_lead_in_the_records_clears_the_saved_route_once(tmp_path, monkeypatch) -> None:
    reader = Reader()
    gateway, recorded = _gateway(tmp_path, monkeypatch, reader, start_limit=0.1)
    await gateway.connect(start_services=False)
    try:
        assert gateway.config.manager.manager_agent_id is None
        assert recorded.seen == [("unavailable", "manager_1234")]
        reply = await gateway.application.send_user_message("Are you there?")
        assert reply.message_type == "system_status"

        # assistant_cli turns notices on after connect; the lead's later arrival is a real recovery.
        gateway.manager_transitions.enable_notices()
        reader.publish(member("lead_1", lead=True))
        await until(lambda: gateway.config.manager.manager_agent_id == "lead_1")
        assert recorded.seen[-1] == ("available", "lead_1", True) and "resume" in recorded.hooks
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_records_read_after_the_start_limit_correct_the_saved_route(tmp_path, monkeypatch, caplog) -> None:
    """Review F3 through the gateway: the view is ready after the start's wait, with no lead."""

    reader = Reader()
    reader.catches_up = False
    gateway, recorded = _gateway(tmp_path, monkeypatch, reader, start_limit=0.1)
    with caplog.at_level(logging.WARNING, logger="assistant_gateway.gateway"):
        await gateway.connect(start_services=False)
    try:
        assert gateway.config.manager.manager_agent_id == "manager_1234" and recorded.seen == []
        assert "event=gateway_team_view_not_ready" in caplog.text
        reader.ready.set()
        await until(lambda: gateway.config.manager.manager_agent_id is None)
        assert recorded.seen == [("unavailable", "manager_1234")]
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_a_refused_join_stops_chats_to_a_lead_nobody_can_see(tmp_path, monkeypatch) -> None:
    """Review F4: the installation refused the gateway's join for good."""

    reader = Reader()
    gateway, recorded = _gateway(tmp_path, monkeypatch, reader,
                                 refusal=("denied", "The installation does not take this gateway as it runs."))
    await asyncio.wait_for(gateway.connect(start_services=False), timeout=2)
    try:
        assert gateway.config.manager.manager_agent_id is None
        assert recorded.seen == [("unavailable", "manager_1234")]
        reply = await gateway.application.send_user_message("Are you there?")
        assert reply.message_type == "system_status"
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_a_change_to_the_leads_record_wakes_held_guidance(tmp_path, monkeypatch) -> None:
    """Held guidance is sent again when the lead's record changes, as the lead's heartbeat used to wake it."""

    class Wake(asyncio.Event):
        def __init__(self) -> None:
            super().__init__()
            self.times = 0

        def set(self) -> None:
            self.times += 1
            super().set()

    reader = Reader()
    reader.publish(member("lead_1", lead=True))
    gateway, _ = _gateway(tmp_path, monkeypatch, reader, saved="lead_1")
    gateway._thread_guidance._wake = wake = Wake()
    await gateway.connect(start_services=False)
    try:
        before = wake.times
        reader.publish(member("lead_1", lead=True, state="busy"))
        reader.publish(member("lead_1", lead=True, state="busy"), member("worker_1"))  # not the lead: no wake
        assert wake.times == before + 1
    finally:
        await gateway.close()
