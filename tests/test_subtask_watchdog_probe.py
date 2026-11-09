"""The subtask watchdog's task probe when asking goes wrong (custom_agents.subtask_watchdog_runtime).

- One target's error never escapes the probe or loses another target's answer.
- "This process cannot ask yet" is not "the other agent did not answer": a reason that
  clears by itself (join, session, view) defers the decision, up to one more timeout; one
  that never clears counts as no answer, as before.
- A failing watchdog step does not end the scheduler pass.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import agent_requests, scheduled_runtime
from custom_agents.agent_requests import AskBlocked
from custom_agents.subtask_watchdog_runtime import TaskProbeNotPossible, _probe_subtask_task_status
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.live import ServiceUnavailable


class _View:
    def __init__(self, *agent_ids: str) -> None:
        self.members = {agent: SimpleNamespace(agent=agent, display_name=f"Researcher-{agent}",
                                               agent_type="Researcher") for agent in agent_ids}
        self.ready = SimpleNamespace(is_set=lambda: True)

    def agent(self, agent_id):
        return self.members.get(agent_id)

    def named(self, name):
        return next((item for item in self.members.values() if item.display_name == name), None)

    def of_type(self, agent_type):
        return sorted((item for item in self.members.values() if item.agent_type == agent_type), key=lambda a: a.agent)


@pytest.fixture
def host():
    """A process host that can ask, with three live workers in its view."""

    stand_in = SimpleNamespace(team_agents=_View("w1", "w2", "w3"), blocked=None)
    stand_in.cannot_ask = lambda agent_id: stand_in.blocked
    agent_requests.configure_process_host(stand_in)
    yield stand_in
    agent_requests.configure_process_host(None)


def _watchdog() -> SimpleNamespace:
    return SimpleNamespace(id="lead_1", team_id="team", subtask_status_probe_timeout_seconds=1.0,
                           _logger=logging.getLogger("test.watchdog"))


def _subtask() -> SimpleNamespace:
    return SimpleNamespace(task=SimpleNamespace(task_id="sub_1", parent_task_id="root"),
                           meta=SimpleNamespace(to_agent_name=None, to_named_agent_type="Researcher"))


def _answers(errors: dict[str, Exception], known: str):
    async def ask_agent(asker, kind, body, target, *, timeout=None):
        if target in errors:
            raise errors[target]
        return {"outcome": "ok", "known": target == known, "current": target == known, "executing": True,
                "exiting": False, **({"state": "in_progress"} if target == known else {})}

    return ask_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [FrameRefused("bad_signature", at="/signature"), RuntimeError("decoder broke")])
async def test_one_targets_error_neither_escapes_nor_loses_another_targets_answer(host, monkeypatch, caplog, error):
    monkeypatch.setattr(agent_requests, "ask_agent", _answers({"w1": error}, known="w2"))

    with caplog.at_level(logging.WARNING):
        status = await _probe_subtask_task_status(_watchdog(), subtask_info=_subtask())

    assert status is not None and status.responder_agent_id == "w2" and status.is_known_task
    assert status.task_state == ProgressState.IN_PROGRESS
    logged = [record.getMessage() for record in caplog.records if "event=subtask_task_probe_failed" in record.getMessage()]
    assert len(logged) == 1
    assert "task_id=sub_1" in logged[0] and "target_agent_id=w1" in logged[0]
    assert f"error_type={type(error).__name__}" in logged[0]


@pytest.mark.asyncio
async def test_the_holders_error_counts_as_no_answer(host, monkeypatch) -> None:
    monkeypatch.setattr(agent_requests, "ask_agent", _answers({"w2": FrameRefused("bad_signature")}, known="w2"))

    assert await _probe_subtask_task_status(_watchdog(), subtask_info=_subtask(), holder_agent_id="w2") is None


@pytest.mark.asyncio
async def test_a_target_that_does_not_answer_counts_as_no_answer(host, monkeypatch) -> None:
    unanswered = ServiceUnavailable("task.probe", "w2", "no responders")
    monkeypatch.setattr(agent_requests, "ask_agent", _answers({"w2": unanswered}, known="w2"))

    assert await _probe_subtask_task_status(_watchdog(), subtask_info=_subtask()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("blocked", "holder"), [
    (AskBlocked("this process has not joined the team's messaging yet", clears=True), "w2"),
    (AskBlocked("agent lead_1 has no session yet", clears=True), "w2"),
    (agent_requests.VIEW_NOT_READY, None),
])
async def test_when_this_process_cannot_ask_yet_the_probe_says_so(host, monkeypatch, blocked, holder) -> None:
    asked = AsyncMock()
    monkeypatch.setattr(agent_requests, "ask_agent", asked)
    if blocked is agent_requests.VIEW_NOT_READY:
        host.team_agents.ready = SimpleNamespace(is_set=lambda: False)
    else:
        host.blocked = blocked

    with pytest.raises(TaskProbeNotPossible) as raised:
        await _probe_subtask_task_status(_watchdog(), subtask_info=_subtask(), holder_agent_id=holder)

    assert (raised.value.reason, raised.value.clears) == (blocked.reason, True) and asked.await_count == 0


@pytest.mark.asyncio
async def test_without_a_message_host_the_probe_cannot_ask_and_that_does_not_clear(monkeypatch) -> None:
    agent_requests.configure_process_host(None)

    with pytest.raises(TaskProbeNotPossible) as raised:
        await _probe_subtask_task_status(_watchdog(), subtask_info=_subtask(), holder_agent_id="w2")

    assert raised.value.clears is False


# -- the watchdog's decision on an overdue subtask ----------------------------------------


def _lead_with_overdue_subtask(tmp_path, *, overdue_seconds: float, timeout_seconds: int = 1200):
    from tests.test_runtime_pause_recovery import _manager
    from tests.test_turn_action_outbox import _make_agent

    manager = _manager(tmp_path)
    root = Task(task_id="root", task_type=MessageMetaType.REQUEST, problem_statement="Wait for delegated work")
    manager.add_task(root, MessageHeader(from_agent_id="gateway"), MessageMeta(), owner_person_id=None)
    child = Task(task_id="child", task_type=MessageMetaType.REQUEST, parent_task_id=root.task_id)
    manager.add_subtask(root.task_id, child, MessageHeader(to_routing_path="worker"), MessageMeta())
    since = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds + overdue_seconds)).isoformat()
    manager.update_main_task_state(child.task_id, ProgressState.WAITING, since)
    lead = _make_agent(tmp_path, task_manager=manager, subtask_timeout_seconds=timeout_seconds)
    decided = AsyncMock()
    object.__setattr__(lead, "_fail_subtask_and_reconcile_parent", decided)
    object.__setattr__(lead, "_send_runtime_pause_query", decided)
    return lead, manager, decided


@pytest.mark.asyncio
async def test_an_overdue_subtask_waits_while_this_process_cannot_ask_yet(tmp_path, caplog) -> None:
    lead, manager, decided = _lead_with_overdue_subtask(tmp_path, overdue_seconds=300)
    probe = AsyncMock(side_effect=TaskProbeNotPossible("agent lead_1 has no session yet", clears=True))
    object.__setattr__(lead, "_probe_subtask_task_status", probe)
    history = len(manager.get_task_history_entries("root", limit=None))

    with caplog.at_level(logging.DEBUG):
        await lead._recover_stalled_subtasks()
        await lead._recover_stalled_subtasks()

    assert probe.await_count == 2 and decided.await_count == 0
    assert manager.get_task_info("child").state == ProgressState.WAITING
    assert len(manager.get_task_history_entries("root", limit=None)) == history  # no probe recorded
    warnings = [record.getMessage() for record in caplog.records
                if record.levelno == logging.WARNING and "event=subtask_task_probe_deferred" in record.getMessage()]
    assert len(warnings) == 1  # once per subtask and reason
    assert f"agent_id={lead.id} task_id=root subtask_id=child" in warnings[0] and "no session yet" in warnings[0]


@pytest.mark.asyncio
async def test_past_the_limit_the_watchdog_decides_as_if_nobody_answered(tmp_path, caplog) -> None:
    lead, _manager, decided = _lead_with_overdue_subtask(tmp_path, overdue_seconds=1300)
    object.__setattr__(lead, "_probe_subtask_task_status",
                       AsyncMock(side_effect=TaskProbeNotPossible("agent lead_1 has no session yet", clears=True)))

    with caplog.at_level(logging.WARNING):
        await lead._recover_stalled_subtasks()

    assert decided.await_count == 1
    assert "event=subtask_task_probe_not_possible" in caplog.text


@pytest.mark.asyncio
async def test_an_agent_without_a_message_host_still_times_out_its_subtasks(tmp_path, caplog) -> None:
    agent_requests.configure_process_host(None)
    lead, _manager, decided = _lead_with_overdue_subtask(tmp_path, overdue_seconds=60)

    with caplog.at_level(logging.WARNING):
        await lead._recover_stalled_subtasks()

    assert decided.await_count == 1
    assert "event=subtask_task_probe_not_possible" in caplog.text and "no message host" in caplog.text


@pytest.mark.asyncio
async def test_a_failing_watchdog_step_does_not_end_the_scheduler_pass(monkeypatch, caplog) -> None:
    from tests.test_schedule_tools import _LoopAgent

    agent = _LoopAgent(can_start=False)
    agent._recover_stalled_subtasks = AsyncMock(side_effect=RuntimeError("history write failed"))
    handbacks = AsyncMock()
    monkeypatch.setattr(scheduled_runtime, "process_pending_handbacks", handbacks)

    async def sleep(_seconds: float) -> None:
        return None

    with caplog.at_level(logging.ERROR):
        await scheduled_runtime._scheduler_pass(agent, {}, sleep)

    assert handbacks.await_count == 1
    assert "event=subtask_watchdog_pass_failed agent_id=manager_id" in caplog.text
