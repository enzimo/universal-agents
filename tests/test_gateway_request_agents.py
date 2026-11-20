"""The gateway's agent trail service (assistant_gateway.application.request_agents_service), A1 and A3.

A request is driven through the hand-over to the team lead, the lead's record,
a worker's record and the end of the work, against the real store and the
session application's thread list: at each step the list payload, its
revision and the live thread states say what the page must draw. Ids the
gateway does not hold change nothing, and nobody else's page sees the work.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from assistant_gateway.application.request_agents_service import AgentReport, GatewayRequestAgents
from assistant_gateway.application.web_chat_threads import thread_list_payload
from assistant_gateway.application.website_application import web_chat_threads_revision
from assistant_gateway.application.website_services import WebsiteSessionApplication
from assistant_gateway.domain.request_state import REQUEST_KIND_INTERACTIVE, RUN_STATE_RUNNING
from assistant_gateway.runtime_ports import OwnershipPrivateResourceAuthorizer
from assistant_gateway.store import GatewayStateStore

OWNER = "user_alpha"
OTHER = "user_beta"
THREAD = "website:web-chat:alpha"
LEAD = "TeamArchitect"
WEB = "WebResearchAgent"
NAMES = {WEB: "Web Research Agent", LEAD: "Team Architect"}


class _Clock:
    def now_utc(self) -> datetime:
        return datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


class _Team:
    """The live members' records as the team view would give them."""

    def __init__(self) -> None:
        self.members: dict[str, AgentReport] = {}

    def report(self, agents: GatewayRequestAgents, agent_id: str, agent_type: str, *requests: str,
               lead: bool = False) -> None:
        report = AgentReport(agent_id, agent_type, lead, tuple(requests))
        self.members[agent_id] = report
        agents.observe(report)


@pytest.fixture
def team(tmp_path: Path):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    live = _Team()
    agents = GatewayRequestAgents(repository=store.request_repository, live_reports=lambda: live.members.values(),
                                  lead_agent_type=LEAD, agent_names=lambda: NAMES)
    agents.start()
    unused = cast(Any, SimpleNamespace())
    application = WebsiteSessionApplication(
        sessions=unused, session_repository=store.session_repository, delivery_repository=unused,
        request_context=unused, private_authorizer=OwnershipPrivateResourceAuthorizer(), manager_agent_id="manager_1",
        team_id="team_1", awaiting_reply_session_keys=frozenset, proactive_session_key="heartbeat", clock=_Clock(),
        request_agents=agents,
    )
    for owner, key in ((OWNER, THREAD), (OTHER, "website:web-chat:beta")):
        store.session_repository.create_session(
            session_key=key, conversation_id=f"conv_{key}", manager_agent_id="manager_1", channel_name="website",
            user_id=owner, chat_id="web-chat", owner_canonical_user_id=owner,
        )
    store.request_repository.record_request(
        correlation_id="corr_1", session_key=THREAD, request_kind=REQUEST_KIND_INTERACTIVE, request_text="compare",
        conversation_id=f"conv_{THREAD}", followup_to_task_id=None, owner_canonical_user_id=OWNER,
        active_task_id="task_1", run_state=RUN_STATE_RUNNING,
    )
    yield SimpleNamespace(store=store, agents=agents, live=live, application=application)
    agents.close()


def _settle(agents: GatewayRequestAgents) -> None:
    """Let the writer finish what it was handed: a running writer drains its queue before it closes."""

    agents.start()
    agents.close()
    agents.start()


def _row(team, owner: str = OWNER) -> dict[str, Any]:
    threads = team.application.list_web_chat_threads(owner_canonical_user_id=owner, include_last_message=True)
    [thread] = [thread for thread in threads if thread.session_key == THREAD] or [None]
    return thread_list_payload(thread) if thread is not None else {}


def _shape(row: dict[str, Any]) -> list[str]:
    return ["P" if entry["by"] == "person" else entry["name"] for entry in row["trail"]["entries"]]


def _states(team, owner: str = OWNER):
    return team.application.web_chat_thread_states(owner_canonical_user_id=owner)


def _working(team, owner: str = OWNER) -> list[str]:
    return [agent.name for item in _states(team, owner).working if item.session_key == THREAD for agent in item.agents]


def test_a_request_through_the_lead_and_a_worker_reaches_the_list_and_the_live_states(team) -> None:
    # Handed to the team lead: its triangle shows at once, nobody works yet.
    team.agents.request_sent("task_1")
    _settle(team.agents)
    row = _row(team)
    assert _shape(row) == ["P", "Team lead"] and row["working"] == [] and _working(team) == []
    revision = _states(team).threads_revision

    # The lead's record names the request: it works, and nothing new is recorded.
    team.live.report(team.agents, "lead_1", LEAD, "task_1", lead=True)
    _settle(team.agents)
    assert _working(team) == ["Team lead"] and _states(team).threads_revision == revision
    assert _row(team)["working"] == [{"agent_type": LEAD, "lead": True, "name": "Team lead"}]

    # It hands the work to a worker: the trail gains the worker and the revision changes, so open pages reload it.
    team.live.report(team.agents, "lead_1", LEAD, lead=True)
    team.live.report(team.agents, "worker_1", WEB, "task_1")
    _settle(team.agents)
    assert _shape(_row(team)) == ["P", "Team lead", "Web Research Agent"]
    assert _states(team).threads_revision != revision and _working(team) == ["Web Research Agent"]
    assert _row(team)["trail"]["agents"] == [
        {"agent_type": LEAD, "lead": True, "name": "Team lead"},
        {"agent_type": WEB, "lead": False, "name": "Web Research Agent"},
    ]

    # The work ends and the request finishes: the trail stays, nobody works.
    team.live.report(team.agents, "worker_1", WEB)
    team.store.request_repository.finish_request("corr_1")
    assert _working(team) == [] and _shape(_row(team)) == ["P", "Team lead", "Web Research Agent"]


def test_nobody_elses_page_and_no_unknown_id_sees_or_changes_the_work(team) -> None:
    team.agents.request_sent("task_1")
    team.live.report(team.agents, "worker_1", WEB, "task_1", "task_unknown")
    team.live.report(team.agents, "worker_2", "Other", "task_unknown")
    _settle(team.agents)
    assert _working(team, OTHER) == [] and _row(team, OTHER) == {}
    assert [item.session_key for item in _states(team, OTHER).working] == []
    assert _shape(_row(team)) == ["P", "Team lead", "Web Research Agent"]


def test_each_pair_is_written_once_and_a_failed_write_is_tried_again_on_the_next_report(caplog) -> None:
    written: list[tuple[str, str, bool]] = []
    failures = [OSError("database is locked")]

    class Store:
        def record_request_agent(self, *, active_task_id: str, agent_type: str, lead: bool) -> str | None:
            if failures:
                raise failures.pop()
            written.append((active_task_id, agent_type, lead))
            return THREAD

    agents = GatewayRequestAgents(repository=Store(), live_reports=list, lead_agent_type=LEAD, agent_names=dict)
    agents.start()
    report = AgentReport("worker_1", WEB, False, ("task_1",))
    with caplog.at_level(logging.WARNING):
        agents.observe(report)
        _settle(agents)
    assert written == [] and "event=gateway_request_agent_record_failed task_id=task_1 agent_type=WebResearchAgent" in caplog.text
    for _ in range(3):  # the record refreshes; the pair is written once
        agents.observe(report)
    agents.observe(AgentReport("worker_1", WEB, False, ("task_2",), exiting=True))
    agents.observe(AgentReport("worker_9", "", False, ("task_3",)))
    agents.request_sent("task_2")
    agents.close()
    assert written == [("task_1", WEB, False), ("task_2", LEAD, True)]


@pytest.mark.asyncio
async def test_the_writer_runs_until_the_gateway_stops_and_finishes_what_it_was_handed() -> None:
    import asyncio

    written: list[str] = []
    store = SimpleNamespace(record_request_agent=lambda **kw: written.append(kw["active_task_id"]) or THREAD)
    agents = GatewayRequestAgents(repository=store, live_reports=list, lead_agent_type=LEAD, agent_names=dict)
    agents.request_sent("task_early")  # handed before the gateway started it: it waits in order
    stop = asyncio.Event()
    running = asyncio.create_task(agents.run(stop))
    agents.request_sent("task_late")
    stop.set()
    await asyncio.wait_for(running, 5)
    assert written == ["task_early", "task_late"]


def test_working_puts_the_lead_first_and_names_from_the_catalog_or_the_type(caplog) -> None:
    calls: list[int] = []

    def names() -> dict[str, str]:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("catalog unreadable")
        return NAMES

    now = [0.0]
    members = [
        AgentReport("w2", "Zeta", False, ("task_1",)),
        AgentReport("w1", WEB, False, ("task_1",)),
        AgentReport("lead", LEAD, True, ("task_1",)),
        AgentReport("gone", "Gone", False, ("task_1",), exiting=True),
        AgentReport("other", "Other", False, ("task_9",)),
    ]
    agents = GatewayRequestAgents(repository=SimpleNamespace(), live_reports=lambda: members, lead_agent_type=LEAD,
                                  agent_names=names, clock=lambda: now[0])
    with caplog.at_level(logging.WARNING):
        first = agents.working({THREAD: "task_1", "idle": None})
    assert [agent.name for agent in first[THREAD]] == ["Team lead", "WebResearchAgent", "Zeta"]
    assert "event=gateway_agent_names_unreadable" in caplog.text and set(first) == {THREAD}
    now[0] = 61.0  # read again after a minute
    assert [agent.name for agent in agents.working({THREAD: "task_1"})[THREAD]] == ["Team lead", "Web Research Agent",
                                                                                     "Zeta"]
    assert agents.working({THREAD: None}) == {}


def test_the_list_revision_follows_the_trail_but_not_who_works(team) -> None:
    team.agents.close()  # the writer waits, so the report is live but not recorded yet
    before = web_chat_threads_revision(team.application.list_web_chat_threads(owner_canonical_user_id=OWNER))
    team.live.report(team.agents, "lead_1", LEAD, "task_1", lead=True)
    assert _working(team) == ["Team lead"]
    assert web_chat_threads_revision(team.application.list_web_chat_threads(owner_canonical_user_id=OWNER)) == before
    _settle(team.agents)
    assert web_chat_threads_revision(team.application.list_web_chat_threads(owner_canonical_user_id=OWNER)) != before
