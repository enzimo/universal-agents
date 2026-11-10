"""An agent's directory of its team, kept from the team's agent records (custom_agents.team_directory).

The records' view (custom_transport.protocol1.team_agents) is given as plain ``TeamAgent``
values; tests/test_live_agent_controls.py covers the whole path over NATS. Covers: the
directory takes the whole view, in order, so a later view always wins (DATA-4, FCX-3); a
name two live agents share names neither (SEC-4); and when an agent left for good, every
requester (INT-4) fails only the subtasks that wait in that agent's own mailbox and that
nobody took (DATA-1, C01, C02), here also with a real task manager and delegated-task
records.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from types import MappingProxyType, SimpleNamespace
from typing import Iterator, Optional
from unittest.mock import AsyncMock

import pytest

from custom_agents import agent_requests, team_directory
from custom_agents.team_directory import agent_exited, live_agent_status, sync_directory
from custom_messages.contracts.control_v1 import AgentStatusMap
from custom_transport.protocol1.team_agents import TeamAgent

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)


def member(agent: str, name: str, agent_type: str = "Researcher", *, state: str = "idle", accepting: bool = True,
           lead: bool = False) -> TeamAgent:
    return TeamAgent(agent=agent, agent_type=agent_type, display_name=name, state=state, accepting_work=accepting,
                     since=NOW, host="lead", session=f"s-{agent}", is_lead=lead)


class _Records:
    """The requester's delegated-task records, by subtask id (``delegated_work_runtime.delegated_tasks``)."""

    def __init__(self, records: Optional[dict] = None, *, fails: bool = False) -> None:
        self.records, self.fails = dict(records or {}), fails

    def get(self, team_id: str, task_id: str):
        if self.fails:
            raise OSError("database is locked")
        return self.records.get(task_id)


def directory_owner(*, agent_id: str = "lead_1", is_lead: bool = True, pending: list | None = None,
                    records: Optional[_Records] = None, lane_seconds: float = 0.0,
                    withdrawn: bool = True) -> SimpleNamespace:
    subtasks = list(pending or [])

    async def run(function, *args):
        if lane_seconds:
            await asyncio.sleep(lane_seconds)  # the storage lane is busy for a moment
        return function(*args)

    return SimpleNamespace(
        id=agent_id, team_id="team", is_team_lead=is_lead, known_agents=AgentStatusMap(), _agent_id_by_name={},
        _logger=logging.getLogger("test.team_directory"),
        task_manager=SimpleNamespace(operations_runtime=SimpleNamespace(run=run),
                                     get_pending_subtasks=lambda: subtasks,
                                     get_parent_task_id=lambda task_id: f"parent_of_{task_id}"),
        _fail_subtask_and_reconcile_parent=AsyncMock(), _delegated_task_repository=records or _Records(),
        team_comms=SimpleNamespace(delete_work_message=AsyncMock(return_value=withdrawn)),
    )


def subtask(task_id: str, to_agent_id: str) -> SimpleNamespace:
    """A subtask delegated by name: its route is the agent's own mailbox (its id)."""

    return SimpleNamespace(task=SimpleNamespace(task_id=task_id), header=SimpleNamespace(to_routing_path=to_agent_id),
                           meta=SimpleNamespace(to_agent_name=None))


def taken(holder: Optional[str], holders: tuple[str, ...], *, sequence: Optional[int] = 5) -> SimpleNamespace:
    return SimpleNamespace(holder_agent_id=holder, holders=list(holders), publish_sequence=sequence)


def test_the_directory_takes_the_whole_view_with_names_types_and_the_lead() -> None:
    owner = directory_owner()
    view = {"lead_1": member("lead_1", "TeamLead", "TeamArchitect", lead=True),
            "worker_1": member("worker_1", "Researcher-1", state="busy"),
            "worker_2": member("worker_2", "Researcher-2", state="paused", accepting=False)}

    sync_directory(owner, view)

    # Its own record is not in its directory.
    assert set(owner.known_agents.agent_map) == {"worker_1", "worker_2"}
    busy = owner.known_agents.agent_map["worker_1"]
    assert (busy.name, busy.named_agent_type, busy.is_executing, busy.is_team_lead, busy.accepting_work) == (
        "Researcher-1", "Researcher", True, False, True)
    paused = owner.known_agents.agent_map["worker_2"]
    assert (paused.is_paused, paused.accepting_work) == (True, False)
    assert owner._agent_id_by_name == {"Researcher-1": "worker_1", "Researcher-2": "worker_2"}

    # An unchanged entry is kept as it is (its last_seen_at says when this directory took it).
    sync_directory(owner, view)
    assert owner.known_agents.agent_map["worker_1"] is busy
    # A later view replaces the whole directory.
    sync_directory(owner, {"worker_2": member("worker_2", "Researcher-2")})
    assert set(owner.known_agents.agent_map) == {"worker_2"} and owner._agent_id_by_name == {"Researcher-2": "worker_2"}

    # A worker's view of the same team names the lead.
    worker = directory_owner(agent_id="worker_1", is_lead=False)
    sync_directory(worker, view)
    assert worker.known_agents.agent_map["lead_1"].is_team_lead is True


def test_a_name_two_live_agents_share_names_neither_and_is_logged_once(caplog) -> None:
    """SEC-4: a second agent with an existing name cannot take it over."""

    owner = directory_owner()
    first = member("worker_1", "Researcher-1")
    view = {"worker_1": first, "worker_9": member("worker_9", "Researcher-1")}

    with caplog.at_level(logging.INFO):
        sync_directory(owner, {"worker_1": first})
        sync_directory(owner, view)
        sync_directory(owner, view)
        assert "Researcher-1" not in owner._agent_id_by_name
        sync_directory(owner, {"worker_1": first})

    logged = [record.getMessage() for record in caplog.records
              if "event=team_directory_name_ambiguous" in record.getMessage()]
    assert len(logged) == 1 and "agent_ids=worker_1,worker_9" in logged[0]
    assert owner._agent_id_by_name == {"Researcher-1": "worker_1"} and "event=team_directory_name_unique" in caplog.text


# -- leaving for good -------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("is_lead", [True, False])
async def test_a_subtask_nobody_took_from_the_departed_agents_mailbox_is_failed(is_lead) -> None:
    """INT-4: any requester, not only the team lead; a subtask with no record counts as never taken."""

    owner = directory_owner(is_lead=is_lead, pending=[subtask("sub_1", "worker_1"), subtask("sub_2", "worker_2"),
                                                      subtask("sub_3", "worker_1")],
                            records=_Records({"sub_3": taken(None, ())}))

    await agent_exited(owner, member("worker_1", "Researcher-1"), "exiting")

    failed = sorted(call.kwargs["subtask_info"].task.task_id
                    for call in owner._fail_subtask_and_reconcile_parent.await_args_list)
    assert failed == ["sub_1", "sub_3"]
    # Its message was withdrawn from the work stream first (sub_1 has no record to say where it is).
    owner.team_comms.delete_work_message.assert_awaited_once_with(5)
    call = owner._fail_subtask_and_reconcile_parent.await_args_list[0]
    assert call.kwargs["parent_task_id"] == "parent_of_sub_1"
    assert "Researcher-1 left before taking it" in call.kwargs["error_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [
    taken("worker_1", ("worker_1",)),            # it holds it: a restart takes it over from saved state
    taken(None, ("worker_1",)),                  # it handed it back; another worker will take it
    taken("worker_3", ("worker_1", "worker_3")),  # another worker took it after the handback
])
async def test_a_subtask_a_worker_took_is_left_to_custody_recovery(record, caplog) -> None:
    owner = directory_owner(pending=[subtask("sub_1", "worker_1")], records=_Records({"sub_1": record}))

    with caplog.at_level(logging.INFO):
        await agent_exited(owner, member("worker_1", "Researcher-1"), "session_closed")

    owner._fail_subtask_and_reconcile_parent.assert_not_awaited()
    assert "event=team_agent_left_subtask_kept" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("record", "withdrawn", "event"), [
    # A worker took it (removing it from the stream) but its claim has not arrived (C12).
    (taken(None, ()), False, "team_agent_left_subtask_taken_unclaimed"),
    # Not published yet: the watchdog handles a delegation that never went out.
    (taken(None, (), sequence=None), True, "team_agent_left_subtask_unpublished"),
])
async def test_a_subtask_whose_message_cannot_be_withdrawn_is_not_failed(record, withdrawn, event, caplog) -> None:
    owner = directory_owner(pending=[subtask("sub_1", "worker_1")], records=_Records({"sub_1": record}),
                            withdrawn=withdrawn)

    with caplog.at_level(logging.INFO):
        await agent_exited(owner, member("worker_1", "Researcher-1"), "session_closed")

    owner._fail_subtask_and_reconcile_parent.assert_not_awaited()
    assert f"event={event}" in caplog.text


@pytest.mark.asyncio
async def test_when_the_records_cannot_be_read_nothing_is_failed(caplog) -> None:
    owner = directory_owner(pending=[subtask("sub_1", "worker_1")], records=_Records(fails=True))

    with caplog.at_level(logging.ERROR):
        await agent_exited(owner, member("worker_1", "Researcher-1"), "exiting")

    owner._fail_subtask_and_reconcile_parent.assert_not_awaited()
    assert "event=team_agent_left_subtask_unread" in caplog.text


@pytest.mark.asyncio
async def test_a_second_agent_with_the_same_name_cannot_fail_the_first_ones_subtasks() -> None:
    """SEC-4: subtasks are matched by the mailbox they were sent to, never by name."""

    owner = directory_owner(pending=[subtask("sub_1", "worker_1")])

    await agent_exited(owner, member("worker_9", "Researcher-1"), "exiting")

    owner._fail_subtask_and_reconcile_parent.assert_not_awaited()


# -- the host hands each agent its views in order (AgentProcessHost._team_changed) -----------------


class _Reader:
    """Stands in for ``TeamAgents``: the view now, and who left for good."""

    def __init__(self) -> None:
        self.view: dict[str, TeamAgent] = {}
        self.reasons: dict[str, str] = {}
        self.departed: dict[str, TeamAgent] = {}
        self.ready = SimpleNamespace(is_set=lambda: True)

    def show(self, view: dict[str, TeamAgent], **left: str) -> frozenset[str]:
        """The next view; ``left`` names agents that left and why. Returns what changed."""

        changed = frozenset(set(view) ^ set(self.view) | {a for a in view if view[a] != self.view.get(a)})
        for agent_id in set(self.view) - set(view):
            self.departed[agent_id] = self.view[agent_id]
            self.reasons[agent_id] = left.get(agent_id, "host_unavailable")
        for agent_id in view:
            self.reasons.pop(agent_id, None)
        self.view = dict(view)
        return changed

    def live(self):
        return MappingProxyType(self.view)

    def departure_reason(self, agent_id):
        return self.reasons.get(agent_id)

    def left_for_good(self, agent_id):
        return self.reasons.get(agent_id) in {"exiting", "session_closed"}

    def departed_agent(self, agent_id):
        return self.departed.get(agent_id)

    def departed_agents(self):
        return {agent: item for agent, item in self.departed.items() if self.left_for_good(agent)}


@contextmanager
def _own_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A thread worker's loop: the agent's directory and its exits run there."""

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()


def _host_with(owner: SimpleNamespace, reader: _Reader):
    from custom_agents.agent_host import AgentProcessHost
    from tests.test_agent_host_sessions import _hosted, _process

    process, _ = _process()
    process.team_agents = reader  # type: ignore[assignment]
    hosted = _hosted(owner.id)
    hosted.agent = owner
    process.agents[owner.id] = hosted
    assert isinstance(process, AgentProcessHost)
    return process


async def _settle(loop: asyncio.AbstractEventLoop, seconds: float = 0.6) -> None:
    await asyncio.sleep(seconds)
    await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop))


@pytest.mark.asyncio
async def test_a_late_requester_receives_retained_exits_once() -> None:
    reader = _Reader()
    reader.show({"worker_1": member("worker_1", "Researcher-1")})
    changed = reader.show({}, worker_1="session_closed")
    with _own_loop() as loop:
        owner = directory_owner(pending=[subtask("sub_1", "worker_1")])
        owner._runtime_loop = loop
        host = _host_with(owner, reader)
        hosted = host.agents[owner.id]
        host._sync_directory(hosted)
        # Catch-up or a repeated attachment must not launch another reconciliation.
        host._team_changed(changed)
        host._sync_directory(hosted)
        await _settle(loop)
        owner._fail_subtask_and_reconcile_parent.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_older_view_never_puts_back_an_agent_that_left() -> None:
    """DATA-4: A exits (its subtasks are read on a slow lane) and B is busy; then B drops out."""

    reader = _Reader()
    with _own_loop() as loop:
        owner = directory_owner(lane_seconds=0.3)
        owner._runtime_loop = loop
        host = _host_with(owner, reader)
        alpha, beta = member("agent_a", "alpha"), member("agent_b", "beta")
        host._team_changed(reader.show({"agent_a": alpha, "agent_b": beta}))
        host._team_changed(reader.show({"agent_b": member("agent_b", "beta", state="busy")}, agent_a="exiting"))
        host._team_changed(reader.show({}))
        await _settle(loop)

        assert owner.known_agents.agent_map == {} and owner._agent_id_by_name == {}


@pytest.mark.asyncio
async def test_an_older_view_never_removes_an_agent_that_came_back() -> None:
    """FCX-3: A exits (slow lane) and B drops out; then B is live again."""

    reader = _Reader()
    with _own_loop() as loop:
        owner = directory_owner(lane_seconds=0.3)
        owner._runtime_loop = loop
        host = _host_with(owner, reader)
        alpha, beta = member("agent_a", "alpha"), member("agent_b", "beta")
        host._team_changed(reader.show({"agent_a": alpha, "agent_b": beta}))
        host._team_changed(reader.show({}, agent_a="exiting"))
        host._team_changed(reader.show({"agent_b": beta}))
        await _settle(loop)

        assert set(owner.known_agents.agent_map) == {"agent_b"} and owner._agent_id_by_name == {"beta": "agent_b"}


@pytest.mark.asyncio
async def test_the_host_hands_on_who_left_for_good_but_not_who_went_quiet() -> None:
    reader = _Reader()
    with _own_loop() as loop:
        owner = directory_owner(is_lead=False, agent_id="worker_5",
                                pending=[subtask("sub_1", "agent_a"), subtask("sub_2", "agent_b")])
        owner._runtime_loop = loop
        host = _host_with(owner, reader)
        host._team_changed(reader.show({"agent_a": member("agent_a", "alpha"), "agent_b": member("agent_b", "beta")}))
        host._team_changed(reader.show({}, agent_a="session_closed"))  # agent_b only went quiet
        await _settle(loop, 0.2)

        failed = [call.kwargs["subtask_info"].task.task_id
                  for call in owner._fail_subtask_and_reconcile_parent.await_args_list]
        assert failed == ["sub_1"]


# -- with a real task manager and delegated-task records (DATA-1, C01, C02) ---------------------------


@pytest.fixture
def requester(tmp_path):
    """A requester with a real task manager, a parent task and a subtask sent by name to worker_b's mailbox."""

    from custom_agents.task_manager import TaskManager
    from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType
    from storage.config import resolve_storage_settings
    from storage.delegated_task_repository import DelegatedTaskRepository

    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "team.db")}})
    manager = TaskManager(storage_settings=settings, logger=logging.getLogger("test.requester"))
    manager.set_owner_identity("lead_1", "TeamArchitect", "team_one", "TeamArchitect")
    parent = Task(task_id="task_parent", task_type=MessageMetaType.REQUEST, problem_statement="parent")
    assert manager.add_task(parent, MessageHeader(from_agent_id="gateway"), MessageMeta(), owner_person_id=None)
    sub = Task(task_id="task_sub", parent_task_id="task_parent", task_type=MessageMetaType.REQUEST,
               problem_statement="part")
    assert manager.add_subtask("task_parent", sub, MessageHeader(from_agent_id="lead_1", to_routing_path="worker_b"),
                               MessageMeta(from_agent_name="TeamLead", to_agent_name="Researcher-B",
                                           to_named_agent_type="Researcher"))
    records = DelegatedTaskRepository(settings)
    records.record_published(team_id="team_one", task_id="task_sub", parent_task_id="task_parent",
                             root_task_id="task_parent", requester_agent_id="lead_1", named_agent_type="Researcher",
                             target_agent_name="Researcher-B", publish_sequence=7)
    return SimpleNamespace(id="lead_1", team_id="team_one", is_team_lead=True, task_manager=manager,
                           _delegated_task_repository=records,
                           team_comms=SimpleNamespace(delete_work_message=AsyncMock(return_value=True)),
                           _logger=logging.getLogger("test.requester"), _fail_subtask_and_reconcile_parent=AsyncMock())


WORKER_B = member("worker_b", "Researcher-B")


def _claim(requester, worker: str) -> None:
    outcome = requester._delegated_task_repository.record_claim(
        team_id="team_one", task_id="task_sub", worker_agent_id=worker, worker_agent_name=worker,
        claimed_at=datetime.now(timezone.utc))
    assert outcome.record is not None and not outcome.already_closed


@pytest.mark.asyncio
async def test_a_subtask_still_waiting_in_the_departed_workers_mailbox_is_failed(requester) -> None:
    await agent_exited(requester, WORKER_B, "exiting")

    [call] = requester._fail_subtask_and_reconcile_parent.await_args_list
    assert (call.kwargs["parent_task_id"], call.kwargs["subtask_info"].task.task_id) == ("task_parent", "task_sub")


@pytest.mark.asyncio
async def test_a_handed_back_subtask_another_worker_runs_is_not_failed_when_its_first_worker_exits(requester) -> None:
    """DATA-1: worker_b hands it back, worker_c takes it, then worker_b's record says exiting."""

    _claim(requester, "worker_b")
    assert requester._delegated_task_repository.mark_requeued(team_id="team_one", task_id="task_sub",
                                                              publish_sequence=None, from_holder_agent_id="worker_b")
    _claim(requester, "worker_c")

    await agent_exited(requester, WORKER_B, "exiting")

    requester._fail_subtask_and_reconcile_parent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("requeued", "claim_received"), [(True, True), (False, True), (True, False)])
async def test_a_handback_the_requester_has_not_finished_handling_fails_nothing(requester, requeued, claim_received) -> None:
    """C02: its handback is requeued but nobody has taken it yet, or not processed at all when the exit is seen."""

    if claim_received:
        _claim(requester, "worker_b")
    if requeued:
        requester._delegated_task_repository.mark_requeued(team_id="team_one", task_id="task_sub",
                                                           publish_sequence=57, from_holder_agent_id="worker_b")

    await agent_exited(requester, WORKER_B, "exiting")

    requester._fail_subtask_and_reconcile_parent.assert_not_awaited()
    requester.team_comms.delete_work_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_claim_that_arrives_after_the_exit_was_seen_finds_its_subtask_still_open(requester) -> None:
    """C12: worker_b saved the subtask and removed it from the stream, but its claim was lost or late."""

    requester.team_comms.delete_work_message.return_value = False  # no longer in the stream: a worker took it

    await agent_exited(requester, WORKER_B, "session_closed")
    _claim(requester, "worker_b2")  # the restarted worker reports what it holds

    requester._fail_subtask_and_reconcile_parent.assert_not_awaited()
    requester.team_comms.delete_work_message.assert_awaited_once_with(7)
    assert requester._delegated_task_repository.get("team_one", "task_sub").status == "running"


@pytest.mark.asyncio
async def test_a_handback_keeps_repeated_holders_in_order(requester) -> None:
    """The last holder still decides which later claims count as a move between workers."""

    for worker in ("worker_b", "worker_c", "worker_b"):
        _claim(requester, worker)
    record = requester._delegated_task_repository.mark_requeued(
        team_id="team_one", task_id="task_sub", publish_sequence=57, from_holder_agent_id="worker_b")
    assert record.holders == ["worker_b", "worker_c", "worker_b"]
    await agent_exited(requester, WORKER_B, "session_closed")
    requester.team_comms.delete_work_message.assert_not_awaited()
    requester._fail_subtask_and_reconcile_parent.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_crashed_workers_claimed_subtask_is_left_for_its_restart_to_take_over(requester) -> None:
    """C01: worker_b's host restarts and closes its session; the restarted worker reports the subtask it holds."""

    _claim(requester, "worker_b")

    await agent_exited(requester, WORKER_B, "session_closed")
    _claim(requester, "worker_b2")  # report_held_delegated_tasks(recovered=True) from the restarted worker

    requester._fail_subtask_and_reconcile_parent.assert_not_awaited()
    record = requester._delegated_task_repository.get("team_one", "task_sub")
    assert record.holder_agent_id == "worker_b2" and record.status == "running"


# -- finding a live agent ------------------------------------------------------------------------


class _View:
    def __init__(self, *members: TeamAgent) -> None:
        self._members = {item.agent: item for item in members}

    def named(self, name: str):
        matches = [item for item in self._members.values() if item.display_name == name]
        return matches[0] if len(matches) == 1 else None

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
    # A name two live agents share finds nobody.
    monkeypatch.setattr(agent_requests, "team_view",
                        lambda: _View(member("worker_2", "Researcher-2"), member("worker_9", "Researcher-2")))
    assert live_agent_status(asker, None, "Researcher-2") is None


def test_an_agent_on_a_host_of_another_contract_stays_listed_and_gets_no_new_work(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract README "Versions": control requests about what it holds still go to it; delegation passes it over."""

    other = TeamAgent(agent="worker_1", agent_type="Researcher", display_name="Researcher-1", state="idle",
                      accepting_work=True, since=NOW, host="agent-Researcher", session="s-worker_1", is_lead=False,
                      other_contract="0" * 64)
    owner = directory_owner()
    sync_directory(owner, {"worker_1": other})
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


# -- a named agent to give work to (live_named_agent, A12) ------------------------------------------


class _Growing(_View):
    """A view whose named agent's record arrives a moment after the lookup starts, as after a launch."""

    def __init__(self, arrives_after: float, *members: TeamAgent) -> None:
        super().__init__()
        self._later, self._at = {item.agent: item for item in members}, None
        self._arrives_after = arrives_after

    def _now(self):
        import time

        if self._at is None:
            self._at = time.monotonic()
        if time.monotonic() - self._at >= self._arrives_after:
            self._members.update(self._later)

    def named(self, name: str):
        self._now()
        return super().named(name)


def _status_answer(**changes):
    reply = {"outcome": "ok", "agent_type": "Researcher", "is_lead": False, "paused": False, "executing": False,
             "has_capacity": True, "accepting_work": True, "exiting": False, "queued": 0, "waiting": 0, "deferred": 0}
    reply.update(changes)
    return reply


@pytest.mark.asyncio
async def test_a_named_worker_that_crashed_but_is_still_listed_is_refused_at_once(monkeypatch, caplog) -> None:
    from custom_transport.protocol1.live import ServiceUnavailable

    asked = AsyncMock(side_effect=ServiceUnavailable("agent.status", "ctl.agent.worker_1", "no responders"))
    monkeypatch.setattr(agent_requests, "ask_agent", asked)
    monkeypatch.setattr(agent_requests, "team_view", lambda: _View(member("worker_1", "Researcher-1")))
    asker = directory_owner()
    asker.subtask_status_probe_timeout_seconds = 0.5

    with caplog.at_level(logging.WARNING):
        found = await asyncio.wait_for(team_directory.live_named_agent(asker, "Researcher", "Researcher-1"), 2)

    assert found is None
    assert asked.await_args.args[1:4] == ("agent.status", {"level": "basic"}, "worker_1")
    assert asked.await_args.kwargs == {"timeout": 0.5}
    assert "event=named_agent_not_answering" in caplog.text and "target_agent_id=worker_1" in caplog.text


@pytest.mark.asyncio
async def test_a_worker_started_just_before_is_found_once_its_record_arrives(monkeypatch) -> None:
    view = _Growing(0.3, member("worker_9", "Researcher-9"))
    monkeypatch.setattr(agent_requests, "team_view", lambda: view)
    monkeypatch.setattr(agent_requests, "ask_agent", AsyncMock(return_value=_status_answer()))

    found = await team_directory.live_named_agent(directory_owner(), "Researcher", "Researcher-9", start_seconds=5)

    assert found is not None and found.self_id == "worker_9"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [_status_answer(exiting=True), _status_answer(accepting_work=False),
                                    _status_answer(agent_type="Writer"),
                                    {"outcome": "not_allowed", "message": "no"}])
async def test_a_named_worker_that_cannot_take_the_task_is_refused(monkeypatch, answer) -> None:
    monkeypatch.setattr(agent_requests, "team_view", lambda: _View(member("worker_1", "Researcher-1")))
    monkeypatch.setattr(agent_requests, "ask_agent", AsyncMock(return_value=answer))

    assert await team_directory.live_named_agent(directory_owner(), "Researcher", "Researcher-1") is None


@pytest.mark.asyncio
async def test_a_name_nobody_has_is_refused_after_the_bounded_wait(monkeypatch, caplog) -> None:
    asked = AsyncMock()
    monkeypatch.setattr(agent_requests, "team_view", lambda: _View(member("worker_1", "Researcher-1")))
    monkeypatch.setattr(agent_requests, "ask_agent", asked)
    monkeypatch.setattr(team_directory, "VIEW_POLL_SECONDS", 0.01)

    with caplog.at_level(logging.INFO):
        found = await team_directory.live_named_agent(directory_owner(), "Researcher", "Researcher-7", start_seconds=0.1)

    assert found is None and asked.await_count == 0 and "event=named_agent_not_live" in caplog.text


@pytest.mark.asyncio
async def test_the_leads_delegation_by_name_asks_the_worker_and_by_type_does_not(monkeypatch) -> None:
    from custom_agents.delegation_runtime import _probe_live_named_agent_type

    asked = AsyncMock(return_value=_status_answer())
    monkeypatch.setattr(agent_requests, "ask_agent", asked)
    monkeypatch.setattr(agent_requests, "team_view", lambda: _View(member("worker_1", "Researcher-1")))
    lead = directory_owner()

    assert (await _probe_live_named_agent_type(lead, "Researcher", "Researcher-1")).self_id == "worker_1"
    assert asked.await_count == 1
    assert (await _probe_live_named_agent_type(lead, "Researcher")).self_id == "worker_1"
    assert asked.await_count == 1


@pytest.mark.asyncio
async def test_a_worker_delegating_by_name_waits_for_a_just_started_worker_without_asking(monkeypatch) -> None:
    """C14: any requester gets the bounded wait; only the lead may ask for a status."""

    from custom_agents.delegation_runtime import exact_delegation_route

    asked = AsyncMock()
    view = _Growing(0.3, member("worker_9", "Researcher-9"))
    monkeypatch.setattr(agent_requests, "team_view", lambda: view)
    monkeypatch.setattr(agent_requests, "ask_agent", asked)
    requester = directory_owner(agent_id="worker_5", is_lead=False)

    assert await exact_delegation_route(requester, "Researcher", "Researcher-9") == "worker_9"
    asked.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [member("worker_1", "Researcher-1", state="paused", accepting=False),
                                    member("worker_1", "Researcher-1", state="starting", accepting=False),
                                    member("worker_1", "Researcher-1", agent_type="Writer")])
async def test_a_worker_delegating_by_name_passes_over_one_that_takes_no_work_or_is_another_type(
        monkeypatch, caplog, target) -> None:
    from custom_agents.delegation_runtime import exact_delegation_route

    monkeypatch.setattr(agent_requests, "team_view", lambda: _View(target))
    monkeypatch.setattr(team_directory, "VIEW_POLL_SECONDS", 0.01)
    monkeypatch.setattr(team_directory, "NAMED_AGENT_START_SECONDS", 0.1)
    requester = directory_owner(agent_id="worker_5", is_lead=False)

    with caplog.at_level(logging.ERROR):
        assert await exact_delegation_route(requester, "Researcher", "Researcher-1") is None
    assert "event=exact_delegation_target_unavailable" in caplog.text
