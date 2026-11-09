"""What an ``all`` restart interrupts in other agents, from their own answers to the team lead.

The team's records say which agents are live but not which task each runs, so the capture asks
each one for its full status (``agent.status``). Covers: answers name the work by task with its
counts; an agent that does not answer (no answer in time, a refusal, nobody serving it) is shown
as not answering and never as idle, in the snapshot and the review text; the agents are asked at
once within one short timeout; the approval digest is the same for the same work whatever the
ages, counts and clocks; a re-check keeps the approval for the same work or less and refuses work
nobody approved, including an agent that stopped answering; without the team's view, or on an
event loop, nothing is captured; and a plan from another release is refused with a plain message.
This process's host is ``tests.restart_team_support.FakeTeam``; the team lead's own tasks
and the end-to-end approval are covered in ``test_runtime_restart_service``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import custom_agents.runtime_restart_service as restart
from custom_agents.agent_host import WITHOUT_SESSION_REASONS
from custom_agents.agent_requests import AgentUnreachable, AskBlocked, SessionlessAgent, configure_process_host
from custom_agents.runtime_restart_service import (
    RuntimeRestartPlan,
    _outside_approval,
    _restart_summary,
    _review_lines,
    _state_digest,
    capture_runtime_restart_impact,
)
from custom_messages.contracts.common_v1 import utc_now
from custom_transport.protocol1.live import RequestRefused, ServiceUnavailable
from tests.restart_team_support import FakeTeam, status_answer, worker_status

LEAD_ID = "agent_lead"
ADA = "agent_ada"
LEAD = SimpleNamespace(id=LEAD_ID)  # no task manager: these tests are about the other agents
SILENT = "silent"


@pytest.fixture
def team(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(restart, "TEAM_STATUS_TIMEOUT_SECONDS", 0.5)
    team = FakeTeam()
    team.join(LEAD_ID, "TeamArchitect", "TeamArchitect", is_lead=True)
    team.join(ADA, "Ada")
    configure_process_host(team)
    yield team
    configure_process_host(None)


def _ada_runs(team: FakeTeam, task_id: str | None, **status) -> None:
    """Ada answers that she runs ``task_id`` (idle when None), or does not answer (``SILENT``)."""

    if task_id == SILENT:
        team.silence(ADA)
    else:
        team.answer(ADA, worker_status(ADA, "Ada", current_task_id=task_id, **status))


def _capture():
    return capture_runtime_restart_impact(LEAD, "all")


def _review(snapshot) -> str:
    plan = SimpleNamespace(impact_snapshot=snapshot)
    return "\n".join((_restart_summary(plan, "all"), *_review_lines(plan)))


def test_other_agents_work_is_named_by_task_from_their_answers(team: FakeTeam) -> None:
    _ada_runs(team, "task_ada", age=42, queued=2, deferred=3)
    team.join("agent_bo", "Bo")
    team.answer("agent_bo", worker_status("agent_bo", "Bo"))

    snapshot = _capture()

    ada, bo = snapshot.known_team_agents
    assert ada.model_dump() == {
        "agent_id": ADA, "named_agent_type": "CodeWritingAgent", "name": "Ada", "answered": True,
        "is_executing": True, "current_task_id": "task_ada", "current_task_age_seconds": 42,
        "top_level_queue_depth": 2, "waiting_top_level_task_count": 0, "deferred_message_count": 3,
        "host": "", "without_session": None,
    }
    assert (bo.answered, bo.is_executing, bo.current_task_id) == (True, False, "")
    assert snapshot.interrupted_work() == {"task_ada": "running on Ada"}
    # The lead asked every other live agent for its full status, signed as itself; not itself.
    assert sorted((asked.kind, asked.body, asked.target, asked.sender) for asked in team.asked) == [
        ("agent.status", {"level": "full"}, ADA, LEAD_ID),
        ("agent.status", {"level": "full"}, "agent_bo", LEAD_ID),
    ]
    team.asked.clear()
    assert capture_runtime_restart_impact(LEAD, "manager").known_team_agents == ()
    assert team.asked == [], "only an 'all' restart stops other agents"


@pytest.mark.parametrize(
    "silence",
    [
        lambda team: team.silence(ADA),
        lambda team: team.reply(ADA, {"outcome": "not_allowed", "message": "Only the team lead reads it."}),
        lambda team: team.fail(ADA, RequestRefused("agent.status", "wrong_sender", "")),
        lambda team: team.fail(ADA, ServiceUnavailable("agent.status", "ctl.agent", "no responders")),
    ],
    ids=["no answer in time", "not allowed", "refused", "nobody serves it"],
)
def test_an_agent_that_does_not_answer_is_shown_as_not_answering(team: FakeTeam, silence, caplog) -> None:
    silence(team)

    with caplog.at_level(logging.WARNING, logger="custom_agents.runtime_restart_service"):
        snapshot = _capture()

    [ada] = snapshot.known_team_agents
    # Its work is unknown, not idle: nothing the answer would fill reads as false or zero.
    assert ada.answered is False
    assert (ada.is_executing, ada.top_level_queue_depth, ada.deferred_message_count) == (None, None, None)
    assert snapshot.interrupted_work() == {f"{ADA}#not-answering": "Ada did not answer, so its work is unknown"}
    assert "event=runtime_restart_agent_not_answering" in caplog.text and f"target_agent_id={ADA}" in caplog.text
    review = _review(snapshot)
    assert "Restart all; no task is known to be running, queued or waiting; 1 agent did not answer or cannot be " \
        "asked" in review
    assert "Agents that did not answer when asked what they are running (1): Ada (agent_ada)." in review
    assert "interrupts none" not in review


def test_the_team_is_asked_at_once_within_one_short_timeout(team: FakeTeam) -> None:
    for index in range(8):
        team.join(f"agent_{index}", f"Worker {index}")
    team.join("agent_slow", "Slow")
    team.answer("agent_slow", worker_status("agent_slow", "Slow", current_task_id="task_slow"), delay=0.3)

    started = time.monotonic()
    snapshot = _capture()
    elapsed = time.monotonic() - started

    # Nine agents asked one after another would take over four seconds.
    assert 0.5 <= elapsed < 1.5, elapsed
    assert sum(not agent.answered for agent in snapshot.known_team_agents) == 9  # Ada and the eight workers
    assert [agent.agent_id for agent in snapshot.known_team_agents if agent.answered] == ["agent_slow"]
    assert snapshot.interrupted_work()["task_slow"] == "running on Slow"
    assert {asked.timeout for asked in team.asked} == {0.5}


def test_the_approval_digest_ignores_ages_counts_and_clocks(team: FakeTeam) -> None:
    team.join("agent_bo", "Bo")  # never answers
    _ada_runs(team, "task_ada", age=5, queued=0, deferred=0)
    first = _capture()
    _ada_runs(team, "task_ada", age=500, queued=4, deferred=9)
    second = _capture()

    assert first.known_team_agents != second.known_team_agents, "the snapshot records what changed"
    assert first.captured_at_utc != second.captured_at_utc
    assert first.approval_payload()["interrupted_tasks"] == ["agent_bo#not-answering", "task_ada"]
    assert _state_digest(first) == _state_digest(second)
    _ada_runs(team, "task_other")
    assert _state_digest(_capture()) != _state_digest(first)


@pytest.mark.parametrize(
    ("planned", "now", "applies"),
    [
        ("task_ada", "task_ada", True),
        ("task_ada", None, True),  # finished: less work
        ("task_ada", "task_new", False),  # a task nobody approved
        ("task_ada", SILENT, False),  # stopped answering: it may run anything now
        (SILENT, SILENT, True),  # the person agreed to interrupt whatever it runs
        (SILENT, None, True),  # answers now with no work
        (SILENT, "task_new", False),  # nobody can tell whether it started after planning
        (None, "task_new", False),
    ],
)
def test_a_recheck_refuses_work_the_approval_does_not_cover(team: FakeTeam, planned, now, applies: bool) -> None:
    _ada_runs(team, planned)
    approved = _capture()
    _ada_runs(team, now)

    why = _outside_approval(approved, _capture())

    assert (why == "") is applies, why
    if not applies:
        assert ("These agents do not answer now" in why and "Ada" in why) if now == SILENT else "task_new" in why
        assert why.endswith("The approval covers only the work listed when the restart was planned.")


def test_an_agent_that_joins_without_answering_needs_a_new_approval(team: FakeTeam) -> None:
    _ada_runs(team, None)
    approved = _capture()
    team.join("agent_new", "Newcomer")
    assert "Newcomer" in _outside_approval(approved, _capture())
    team.answer("agent_new", worker_status("agent_new", "Newcomer"))
    team.leave(ADA)
    assert _outside_approval(approved, _capture()) == "", "an idle newcomer and a departed agent add nothing"


def test_without_the_teams_view_an_all_restart_is_not_captured(caplog) -> None:
    configure_process_host(None)

    with caplog.at_level(logging.WARNING, logger="custom_agents.runtime_restart_service"):
        with pytest.raises(restart.TeamUnseen, match="cannot ask the team's other agents"):
            _capture()

    assert "event=runtime_restart_team_unseen" in caplog.text
    assert capture_runtime_restart_impact(LEAD, "manager").known_team_agents == ()


def test_the_capture_never_blocks_an_event_loop(team: FakeTeam) -> None:
    _ada_runs(team, "task_ada")

    async def on_the_loop():
        return _capture()

    with pytest.raises(RuntimeError, match="worker thread"):
        asyncio.run(on_the_loop())
    assert team.asked == []
    # From a worker thread, where tools run, the same capture gets Ada's answer.
    assert asyncio.run(asyncio.to_thread(_capture)).interrupted_work() == {"task_ada": "running on Ada"}


def test_a_plan_from_another_release_is_refused_with_a_plain_message(team: FakeTeam) -> None:
    snapshot = _capture()
    now = utc_now()
    plan = RuntimeRestartPlan(
        change_id="runtime_restart_0123456789ab", created_at_utc=now, expires_at_utc=now + timedelta(hours=1),
        actor_principal_id=LEAD_ID, actor_named_agent_type="TeamArchitect", team_id="team", owner_canonical_user_id="u",
        source_task_id="task", authority_mode="person_approval", scope="all", reason="Reload",
        impact_snapshot=snapshot, runtime_state_digest=_state_digest(snapshot),
    )
    stored = plan.model_dump(mode="json") | {"schema_version": 3}

    with pytest.raises(ValidationError, match="planned by another version of Universal Agents"):
        RuntimeRestartPlan.model_validate(stored)


# -- when the team lead cannot ask, and agents nobody can ask ------------------------------


@pytest.mark.parametrize("blocked", [
    AskBlocked("agent agent_lead has no session yet", clears=True),
    AskBlocked("this process has not joined the team's messaging yet", clears=True),
])
def test_when_the_lead_cannot_ask_yet_an_all_restart_is_refused_with_why(team: FakeTeam, caplog, blocked) -> None:
    _ada_runs(team, "task_ada")
    team.blocked = blocked

    with caplog.at_level(logging.WARNING, logger="custom_agents.runtime_restart_service"):
        with pytest.raises(restart.TeamUnseen) as refused:
            _capture()

    assert blocked.reason in str(refused.value) and "Try again in a minute" in str(refused.value)
    assert team.asked == [], "nobody is asked, so nobody is listed as not answering"
    assert f"event=runtime_restart_team_unseen agent_id={LEAD_ID}" in caplog.text


def test_a_session_lost_while_asking_refuses_the_capture(team: FakeTeam) -> None:
    team.fail(ADA, AgentUnreachable("agent agent_lead has no session yet"))

    with pytest.raises(restart.TeamUnseen, match="no session yet"):
        _capture()


def _without_session(host: str, component: str, code: str = "session_limit_reached") -> SessionlessAgent:
    return SessionlessAgent(host, component, code, WITHOUT_SESSION_REASONS[code])


def test_an_agent_a_host_runs_without_a_session_is_listed_as_not_answering(team: FakeTeam, caplog) -> None:
    _ada_runs(team, "task_ada")
    approved = _capture()
    team.sessionless = [_without_session("agent-Researcher-1", "Researcher-agent_bo")]

    with caplog.at_level(logging.WARNING, logger="custom_agents.runtime_restart_service"):
        current = _capture()

    key = "agent-Researcher-1#Researcher-agent_bo#no-session"
    assert key in current.interrupted_work() and key not in current.interrupted_tasks()
    review = _review(current)
    assert current.interrupted_work()[key] == (
        "Researcher-agent_bo on host agent-Researcher-1 runs without a session (its host already runs as many "
        "agents as it may), so nobody can ask what it is doing; its work is unknown")
    assert "Agents that run without a session, so nobody can ask what they are doing (1): Researcher-agent_bo on " \
        "host agent-Researcher-1 (its host already runs as many agents as it may)." in review
    assert "did not answer when asked" not in review, "nobody asked it"
    assert "event=runtime_restart_agent_without_session" in caplog.text
    # It appeared after planning: its work was never approved. Planned with it, it keeps the approval.
    assert "do not answer now" in _outside_approval(approved, current)
    assert _outside_approval(current, _capture()) == ""
    team.sessionless = []
    assert _outside_approval(current, _capture()) == "", "it got a session and runs nothing: fewer interruptions"


def test_two_hosts_listing_more_agents_without_a_session_keep_separate_entries(team: FakeTeam) -> None:
    _ada_runs(team, None)
    team.sessionless = [_without_session("lead", "more-agents", "sessions_refused"),
                        _without_session("agent-Researcher-1", "more-agents", "sessions_refused")]

    silent = [agent.not_answering_key for agent in _capture().agents_not_answering()]

    assert silent == ["lead#more-agents#no-session", "agent-Researcher-1#more-agents#no-session"]
    assert "more agents without a session than its host lists by name" in _review(_capture())


def test_a_component_named_like_a_live_agent_does_not_hide_that_agent_stopping(team: FakeTeam) -> None:
    _ada_runs(team, "task_ada")
    team.sessionless = [_without_session("agent-Researcher-1", ADA)]  # a host names a component like Ada's id
    approved = _capture()

    _ada_runs(team, SILENT)

    assert "Ada" in _outside_approval(approved, _capture())


def test_no_id_a_restart_key_is_built_from_can_hold_a_hash() -> None:
    """The '#' keys rely on the contract: a reported task id, an agent id, a host id or a component never holds '#'."""

    from pydantic import TypeAdapter

    from custom_messages.wire import models
    from custom_messages.wire.frame import body_model

    for id_type in (models.RecordId, models.AgentId, models.RuntimeId, models.TokenId):
        with pytest.raises(ValidationError):
            TypeAdapter(id_type).validate_python(f"{ADA}#not-answering")
    reply = status_answer(worker_status("agent_bo", "Bo", current_task_id="task_bo"))
    body_model("agent.status.reply")[0].validate_python(reply)
    for planted in (f"{ADA}#not-answering", f"{ADA}#unknown-task", f"lead#{ADA}#no-session"):
        with pytest.raises(ValidationError):
            body_model("agent.status.reply")[0].validate_python({**reply, "current_task_id": planted})


def test_a_reported_task_id_cannot_switch_off_the_recheck_for_another_agent(team: FakeTeam) -> None:
    """SEC-3: Bo reports a task id shaped like Ada's 'not answering' key, then Ada stops answering."""

    team.join("agent_bo", "Bo")
    for planted in (f"{ADA}:not-answering", f"{ADA}:unknown-task"):
        team.answer("agent_bo", worker_status("agent_bo", "Bo", current_task_id=planted))
        _ada_runs(team, "task_ada")
        approved = _capture()
        _ada_runs(team, SILENT)

        assert "Ada" in _outside_approval(approved, _capture()), planted
