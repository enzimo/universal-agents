"""What an agent answers about itself (custom_agents.live_controls), and the team lead's side of it.

Handlers get stand-ins for what they read from a verified frame (the body and the sender's
session); every reply is checked against the contract's reply model. The watchdog's task
probe and the lead's control tool are given a stand-in ``ask_agent``.
tests/test_live_agent_controls.py runs the same over NATS.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import agent_requests, live_controls
from custom_agents.agent_requests import AgentUnreachable
from custom_agents.subtask_watchdog_runtime import _probe_subtask_task_status
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.status_v1 import StatusFindingV1, StatusSeverity
from custom_messages.enums import ControlType, MessageMetaType, ProgressState
from custom_messages.wire.frame import body_model
from custom_tools.providers._runtime import _send_control_instruction_payload
from tests.team_services_support import checked_reply


def frame(body: dict, *, role: str = "lead") -> SimpleNamespace:
    issuer = SimpleNamespace(runtime="lead", agent="lead_1")
    return SimpleNamespace(raw={"body": body}, session=SimpleNamespace(agent_role=role),
                           frame=SimpleNamespace(issuer=issuer))


def stand_in_agent(**overrides) -> SimpleNamespace:
    asked: list[ControlType] = []

    def get_self_status(info_type=ControlType.STATUS_REQUEST_BASIC):
        asked.append(info_type)
        return AgentStatus(
            self_id="worker_1", team_id="team", name="Researcher-1", named_agent_type="Researcher",
            agent_role="worker", is_paused=False, is_executing=True, is_busy=True, has_execution_capacity=False,
            accepting_work=True, status_type=MessageMetaType.RESPONSE, top_level_queue_depth=2,
            waiting_top_level_task_count=1, deferred_message_count=0, current_task_id="task_42",
            current_task_age_seconds=12, context_window_tokens=200000, estimated_context_tokens=50000,
            context_fill_ratio=0.25, context_pressure_level="moderate", tool_call_budget_fill_ratio=0.1234,
            available_skills=["grill-me"],
            status_findings=[StatusFindingV1(finding_id="mcp:search", severity=StatusSeverity.WARNING,
                                             title="A connection is not answering", source="Researcher",
                                             since=datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc))],
            diagnostic={"llm_model": "model-x"},
        )

    agent = SimpleNamespace(
        id="worker_1", paused=False, is_executing=True, _should_exit=asyncio.Event(),
        get_self_status=get_self_status, asked=asked, _logger=logging.getLogger("test.live_controls"),
        pause=AsyncMock(), resume=AsyncMock(), exit_agent=AsyncMock(),
    )
    agent.__dict__.update(overrides)
    return agent


@pytest.mark.asyncio
async def test_a_basic_status_says_what_the_agent_does_and_a_full_one_adds_diagnostics(monkeypatch) -> None:
    from custom_agents import skill_catalog_runtime

    repaired = AsyncMock(return_value=True)
    monkeypatch.setattr(skill_catalog_runtime, "repair_agent_skill_catalog", repaired)
    agent = stand_in_agent()

    basic = checked_reply("agent.status", await live_controls.agent_status(agent, frame({"level": "basic"},
                                                                                         role="worker")))
    full = checked_reply("agent.status", await live_controls.agent_status(agent, frame({"level": "full"})))

    assert {key: basic[key] for key in ("outcome", "agent_type", "executing", "has_capacity", "queued", "waiting")} == {
        "outcome": "ok", "agent_type": "Researcher", "executing": True, "has_capacity": False, "queued": 2,
        "waiting": 1}
    assert (basic["current_task_id"], basic["current_task_age_seconds"]) == ("task_42", 12)
    assert basic["context"] == {"window_tokens": 200000, "estimated_tokens": 50000, "fill_per_mille": 250,
                                "pressure": "moderate"}
    assert basic["tool_call_budget_per_mille"] == 123 and basic["skills"] == ["grill-me"]
    assert basic["findings"] == [{"finding_id": "mcp:search", "severity": "warning",
                                  "title": "A connection is not answering", "source": "Researcher",
                                  "since": "2026-11-04T09:00:00.000Z"}]
    assert "diagnostic_json" not in basic
    assert json.loads(full["diagnostic_json"]) == {"llm_model": "model-x"}
    assert agent.asked == [ControlType.STATUS_REQUEST_BASIC, ControlType.STATUS_REQUEST_FULL]
    # A skill change the agent missed is picked up before it answers, as the status request did.
    assert repaired.await_count == 2


@pytest.mark.asyncio
async def test_only_the_team_lead_gets_a_full_status() -> None:
    reply = await live_controls.agent_status(stand_in_agent(), frame({"level": "full"}, role="worker"))
    assert checked_reply("agent.status", reply)["outcome"] == "not_allowed"


@pytest.mark.asyncio
async def test_pause_resume_and_exit_say_what_they_did() -> None:
    agent = stand_in_agent()
    request = frame({"request_id": "control-1"})

    assert await live_controls.agent_pause(agent, request) == {"outcome": "ok", "state": "paused"}
    agent.pause.assert_awaited_once()
    agent.paused = True
    assert await live_controls.agent_pause(agent, request) == {"outcome": "ok", "state": "already_paused"}
    assert await live_controls.agent_resume(agent, request) == {"outcome": "ok", "state": "resumed"}
    agent.paused = False
    assert await live_controls.agent_resume(agent, request) == {"outcome": "ok", "state": "not_paused"}
    assert await live_controls.agent_exit(agent, request) == {"outcome": "ok", "state": "exiting"}
    agent._should_exit.set()
    assert await live_controls.agent_exit(agent, request) == {"outcome": "ok", "state": "already_exiting"}
    assert (await live_controls.agent_pause(agent, request))["outcome"] == "agent_exiting"
    for kind, handler in (("agent.pause", live_controls.agent_pause), ("agent.exit", live_controls.agent_exit)):
        checked_reply(kind, await handler(agent, request))


@pytest.mark.asyncio
async def test_a_task_probe_says_whether_the_agent_holds_the_task(monkeypatch) -> None:
    from custom_agents import turn_registry

    task = SimpleNamespace(task=SimpleNamespace(task_state=[(ProgressState.IN_PROGRESS, "2026-11-04T09:00:00+00:00")]),
                           state=ProgressState.IN_PROGRESS)
    operations = SimpleNamespace(run=AsyncMock(side_effect=lambda function, *args: function(*args)))
    agent = stand_in_agent(task_manager=SimpleNamespace(
        operations_runtime=operations, get_task_info=lambda task_id: task if task_id == "task_42" else None))
    monkeypatch.setattr(turn_registry, "executing_task_ids", lambda _agent: {"task_42"})

    held = checked_reply("task.probe", await live_controls.task_probe(agent, frame({"task_id": "task_42"})))
    unknown = checked_reply("task.probe", await live_controls.task_probe(agent, frame({"task_id": "task_7"})))

    assert held == {"outcome": "ok", "known": True, "current": True, "executing": True, "exiting": False,
                    "state": "in_progress", "last_update_at": "2026-11-04T09:00:00.000Z"}
    assert unknown == {"outcome": "ok", "known": False, "current": False, "executing": True, "exiting": False}


# -- the team lead's side ---------------------------------------------------------------


class _View:
    def __init__(self, *members) -> None:
        self.members = {item.agent: item for item in members}

    def agent(self, agent_id):
        return self.members.get(agent_id)

    def named(self, name):
        return next((item for item in self.members.values() if item.display_name == name), None)

    def of_type(self, agent_type):
        return sorted((item for item in self.members.values() if item.agent_type == agent_type), key=lambda a: a.agent)


def _member(agent: str, name: str):
    return SimpleNamespace(agent=agent, display_name=name, agent_type="Researcher")


def _watchdog(**overrides) -> SimpleNamespace:
    lead = SimpleNamespace(id="lead_1", team_id="team", subtask_status_probe_timeout_seconds=2.0,
                           _logger=logging.getLogger("test.watchdog"))
    lead.__dict__.update(overrides)
    return lead


def _subtask(to_agent_name=None) -> SimpleNamespace:
    return SimpleNamespace(task=SimpleNamespace(task_id="sub_1", parent_task_id="root"),
                           meta=SimpleNamespace(to_agent_name=to_agent_name, to_named_agent_type="Researcher"))


@pytest.mark.asyncio
async def test_the_watchdog_asks_the_holder_or_else_each_worker_of_the_type(monkeypatch) -> None:
    asked: list[str] = []

    async def ask_agent(asker, kind, body, target, *, timeout=None):
        assert (asker.id, kind, body) == ("lead_1", "task.probe", {"task_id": "sub_1"})
        asked.append(target)
        if target == "worker_3":
            raise AgentUnreachable("no session")
        known = target == "worker_2"
        return {"outcome": "ok", "known": known, "current": known, "executing": known, "exiting": False,
                **({"state": "in_progress"} if known else {})}

    monkeypatch.setattr(agent_requests, "ask_agent", ask_agent)
    monkeypatch.setattr(agent_requests, "team_view", lambda: _View(_member("worker_1", "Researcher-1"),
                                                                _member("worker_2", "Researcher-2"),
                                                                _member("worker_3", "Researcher-3")))

    held = await _probe_subtask_task_status(_watchdog(), subtask_info=_subtask(), holder_agent_id="worker_2")
    assert asked == ["worker_2"]
    assert (held.responder_agent_id, held.responder_agent_name, held.is_known_task, held.task_state) == (
        "worker_2", "Researcher-2", True, ProgressState.IN_PROGRESS)

    asked.clear()
    found = await _probe_subtask_task_status(_watchdog(), subtask_info=_subtask())
    # Every worker of the type is asked; only the one that knows the task counts.
    assert sorted(asked) == ["worker_1", "worker_2", "worker_3"]
    assert found.responder_agent_id == "worker_2"

    asked.clear()
    named = await _probe_subtask_task_status(_watchdog(), subtask_info=_subtask("Researcher-1"))
    assert asked == ["worker_1"] and named is None


@pytest.mark.asyncio
async def test_the_leads_control_tool_reports_what_the_agent_answered(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []

    async def ask_agent(asker, kind, body, target, *, timeout=None):
        body_model(kind)[0].validate_python(body)
        sent.append((kind, target))
        if target == "worker_gone":
            raise AgentUnreachable("no responders")
        return {"outcome": "ok", "state": "already_paused"}

    monkeypatch.setattr(agent_requests, "ask_agent", ask_agent)
    lead = SimpleNamespace(id="lead_1", _agent_id_by_name={"Researcher-1": "worker_1"})

    answered = await _send_control_instruction_payload(lead, "Researcher-1", "pause")
    gone = await _send_control_instruction_payload(lead, "worker_gone", "exit")

    assert sent == [("agent.pause", "worker_1"), ("agent.exit", "worker_gone")]
    assert answered == "Researcher-1: already paused."
    assert gone.startswith("worker_gone did not answer") and "nothing was changed" in gone
