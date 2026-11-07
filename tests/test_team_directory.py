"""An agent's directory of its team, kept from the team's agent records (custom_agents.team_directory).

The records' view (custom_transport.protocol1.team_agents) is given as plain ``TeamAgent``
values; tests/test_live_agent_controls.py covers the whole path over NATS.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import agent_requests, team_directory
from custom_agents.team_directory import apply_team_change, live_agent_status
from custom_messages.contracts.control_v1 import AgentStatusMap
from custom_transport.protocol1.team_agents import TeamAgent

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)


def member(agent: str, name: str, agent_type: str = "Researcher", *, state: str = "idle", accepting: bool = True,
           lead: bool = False) -> TeamAgent:
    return TeamAgent(agent=agent, agent_type=agent_type, display_name=name, state=state, accepting_work=accepting,
                     since=NOW, host="lead", session=f"s-{agent}", is_lead=lead)


def directory_owner(*, is_lead: bool = True, pending: list | None = None) -> SimpleNamespace:
    subtasks = list(pending or [])
    operations = SimpleNamespace(run=AsyncMock(side_effect=lambda function, *args: function(*args)))
    return SimpleNamespace(
        id="lead_1", team_id="team", is_team_lead=is_lead, known_agents=AgentStatusMap(), _agent_id_by_name={},
        _logger=logging.getLogger("test.team_directory"),
        task_manager=SimpleNamespace(operations_runtime=operations, get_pending_subtasks=lambda: subtasks,
                                     get_parent_task_id=lambda task_id: f"parent_of_{task_id}"),
        _fail_subtask_and_reconcile_parent=AsyncMock(),
    )


def subtask(task_id: str, to_agent_name: str) -> SimpleNamespace:
    return SimpleNamespace(task=SimpleNamespace(task_id=task_id), meta=SimpleNamespace(to_agent_name=to_agent_name))


@pytest.mark.asyncio
async def test_the_directory_follows_the_view_with_names_types_and_the_lead() -> None:
    owner = directory_owner()
    view = {"lead_1": member("lead_1", "TeamLead", "TeamArchitect", lead=True),
            "worker_1": member("worker_1", "Researcher-1", state="busy"),
            "worker_2": member("worker_2", "Researcher-2", state="paused", accepting=False)}

    await apply_team_change(owner, {}, view, frozenset(view), frozenset())

    # Its own record is not in its directory.
    assert set(owner.known_agents.agent_map) == {"worker_1", "worker_2"}
    busy = owner.known_agents.agent_map["worker_1"]
    assert (busy.name, busy.named_agent_type, busy.is_executing, busy.is_team_lead, busy.accepting_work) == (
        "Researcher-1", "Researcher", True, False, True)
    paused = owner.known_agents.agent_map["worker_2"]
    assert (paused.is_paused, paused.accepting_work) == (True, False)
    assert owner._agent_id_by_name == {"Researcher-1": "worker_1", "Researcher-2": "worker_2"}

    # A worker's view of the same team names the lead.
    worker = directory_owner(is_lead=False)
    worker.id = "worker_1"
    await apply_team_change(worker, {}, view, frozenset(view), frozenset())
    assert worker.known_agents.agent_map["lead_1"].is_team_lead is True


@pytest.mark.asyncio
async def test_an_agent_that_says_it_is_leaving_fails_what_the_lead_asked_it_by_name() -> None:
    owner = directory_owner(pending=[subtask("sub_1", "Researcher-1"), subtask("sub_2", "Researcher-2")])
    before = {"worker_1": member("worker_1", "Researcher-1"), "worker_2": member("worker_2", "Researcher-2")}
    await apply_team_change(owner, {}, before, frozenset(before), frozenset())

    after = {"worker_2": before["worker_2"]}
    await apply_team_change(owner, before, after, frozenset({"worker_1"}), frozenset({"worker_1"}))

    assert "worker_1" not in owner.known_agents.agent_map and "Researcher-1" not in owner._agent_id_by_name
    [call] = owner._fail_subtask_and_reconcile_parent.await_args_list
    assert call.kwargs["parent_task_id"] == "parent_of_sub_1"
    assert call.kwargs["subtask_info"].task.task_id == "sub_1"
    assert "Researcher-1 exited before replying" in call.kwargs["error_message"]


@pytest.mark.asyncio
async def test_an_agent_that_only_went_quiet_is_not_given_up_for() -> None:
    owner = directory_owner(pending=[subtask("sub_1", "Researcher-1")])
    before = {"worker_1": member("worker_1", "Researcher-1")}
    await apply_team_change(owner, {}, before, frozenset(before), frozenset())

    # Its host stopped reporting: it leaves the directory, but nothing it was asked is failed.
    await apply_team_change(owner, before, {}, frozenset({"worker_1"}), frozenset())

    assert owner.known_agents.agent_map == {}
    owner._fail_subtask_and_reconcile_parent.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_the_lead_fails_subtasks_when_an_agent_exits() -> None:
    worker = directory_owner(is_lead=False, pending=[subtask("sub_1", "Researcher-1")])
    before = {"worker_9": member("worker_9", "Researcher-1")}
    await apply_team_change(worker, before, {}, frozenset({"worker_9"}), frozenset({"worker_9"}))
    worker._fail_subtask_and_reconcile_parent.assert_not_awaited()


class _View:
    def __init__(self, *members: TeamAgent) -> None:
        self._members = {item.agent: item for item in members}

    def named(self, name: str):
        return next((item for item in self._members.values() if item.display_name == name), None)

    def of_type(self, agent_type: str):
        return sorted((item for item in self._members.values() if item.agent_type == agent_type), key=lambda a: a.agent)


def test_a_live_agent_is_found_by_type_or_name_from_the_view(monkeypatch: pytest.MonkeyPatch) -> None:
    asker = directory_owner()
    view = _View(member("lead_1", "TeamLead", "TeamArchitect", lead=True),
                 member("worker_1", "Researcher-1", accepting=False),
                 member("worker_2", "Researcher-2"))
    monkeypatch.setattr(agent_requests, "team_view", lambda: view)

    # A worker that takes no more work (winding down after losing its lease) is passed over.
    assert live_agent_status(asker, "Researcher").self_id == "worker_2"
    assert live_agent_status(asker, None, "Researcher-2").self_id == "worker_2"
    assert live_agent_status(asker, "Researcher", "Researcher-1") is None
    assert live_agent_status(asker, "Writer", "Researcher-2") is None
    # Asking for its own type finds others only.
    assert live_agent_status(asker, "TeamArchitect") is None


@pytest.mark.asyncio
async def test_an_agent_on_a_host_of_another_contract_stays_listed_and_gets_no_new_work(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract README "Versions": control requests about what it holds still go to it; delegation passes it over."""

    other = TeamAgent(agent="worker_1", agent_type="Researcher", display_name="Researcher-1", state="idle",
                      accepting_work=True, since=NOW, host="agent-Researcher", session="s-worker_1", is_lead=False,
                      other_contract="0" * 64)
    owner = directory_owner()
    await apply_team_change(owner, {}, {"worker_1": other}, frozenset({"worker_1"}), frozenset())
    entry = owner.known_agents.agent_map["worker_1"]
    assert entry.accepting_work is False and owner._agent_id_by_name == {"Researcher-1": "worker_1"}

    view = _View(other, member("worker_2", "Researcher-2"))
    monkeypatch.setattr(agent_requests, "team_view", lambda: view)
    assert live_agent_status(owner, "Researcher").self_id == "worker_2"
    assert live_agent_status(owner, "Researcher", "Researcher-1") is None


def test_without_a_view_nothing_is_found_and_that_is_logged(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr(agent_requests, "team_view", lambda: None)
    with caplog.at_level(logging.WARNING):
        assert team_directory.live_agent_status(directory_owner(), "Researcher") is None
    assert "event=team_directory_unavailable" in caplog.text
