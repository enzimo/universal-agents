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
from custom_transport.protocol1.live import ServiceUnavailable
from tests.team_services_support import checked_reply


def frame(body: dict, *, role: str = "lead", agent: str = "lead_1", runtime: str = "lead",
          roles: tuple[str, ...] = ("lead_host", "worker_host"), session: bool = True) -> SimpleNamespace:
    """What a handler reads from a verified request: its body, the sender's session and certificate."""

    issuer = SimpleNamespace(runtime=runtime, agent=agent if session else None)
    return SimpleNamespace(raw={"body": body}, session=SimpleNamespace(agent_role=role, agent=agent) if session else None,
                           certificate=SimpleNamespace(roles=frozenset(roles)), frame=SimpleNamespace(issuer=issuer))


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
@pytest.mark.parametrize(("request_frame", "obeyed"), [
    # SEC-2: a worker agent in the team lead's own process, on the lead's host certificate.
    (dict(role="worker", agent="worker_7"), set()),
    # GEN-3: the agent's own session, even when it is the lead.
    (dict(role="lead", agent="worker_1"), set()),
    # The team services send without an agent session; they may pause and resume, not stop.
    (dict(session=False, runtime="team-services", roles=("team_services",)), {"pause", "resume"}),
    # Another host's request without an agent session.
    (dict(session=False, runtime="gateway", roles=("gateway",)), set()),
    # The team lead's own session.
    (dict(), {"pause", "resume", "exit"}),
])
async def test_pause_resume_and_exit_are_obeyed_only_from_the_lead_or_the_team_services(
        request_frame, obeyed, caplog) -> None:
    for action, kind, handler in (("pause", "agent.pause", live_controls.agent_pause),
                                  ("resume", "agent.resume", live_controls.agent_resume),
                                  ("exit", "agent.exit", live_controls.agent_exit)):
        agent = stand_in_agent(paused=action == "resume")
        with caplog.at_level(logging.WARNING):
            reply = checked_reply(kind, await handler(agent, frame({"request_id": "control-1"}, **request_frame)))
        done = getattr(agent, {"pause": "pause", "resume": "resume", "exit": "exit_agent"}[action])
        if action in obeyed:
            assert reply["outcome"] == "ok" and done.await_count == 1
        else:
            assert reply["outcome"] == "not_allowed" and reply["message"] and done.await_count == 0
            assert f"event=agent_control_refused agent_id=worker_1 action={action}" in caplog.text


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
            raise ServiceUnavailable("task.probe", target, "no responders")
        known = target == "worker_2"
        return {"outcome": "ok", "known": known, "current": known, "executing": known, "exiting": False,
                **({"state": "in_progress"} if known else {})}

    monkeypatch.setattr(agent_requests, "ask_agent", ask_agent)
    # This process can ask (agent_requests.cannot_ask); its view is the stand-in below.
    monkeypatch.setattr(agent_requests, "_PROCESS_HOST", SimpleNamespace(cannot_ask=lambda agent_id: None))
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
    from custom_messages.wire.refusals import FrameRefused
    from custom_tools.providers._runtime import control_reply_seconds
    from custom_transport.protocol1.live import RequestRefused

    sent: list[tuple[str, str, float]] = []
    answers = {
        "worker_1": {"outcome": "ok", "state": "already_paused"},
        "worker_2": {"outcome": "not_allowed", "message": "Only the team lead may ask this."},
        "worker_gone": ServiceUnavailable("agent.exit", "ctl.agent.worker_gone", "no responders"),
        "worker_slow": ServiceUnavailable("agent.exit", "ctl.agent.worker_slow", "no answer in time"),
        "worker_failed": RequestRefused("agent.pause", "handler_failed", ""),
        "worker_old": RequestRefused("agent.pause", "unsupported", "/kind"),
        "worker_bad": FrameRefused("bad_signature"),
        "worker_unasked": AgentUnreachable("agent lead_1 has no session yet"),
    }

    async def ask_agent(asker, kind, body, target, *, timeout=None):
        body_model(kind)[0].validate_python(body)
        sent.append((kind, target, timeout))
        answer = answers[target]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(agent_requests, "ask_agent", ask_agent)
    lead = SimpleNamespace(id="lead_1", name="TeamLead", _agent_id_by_name={"Researcher-1": "worker_1"})

    async def ask(target: str, instruction: str = "pause") -> str:
        return await _send_control_instruction_payload(lead, target, instruction)

    assert await ask("Researcher-1") == "Researcher-1: already paused."
    assert await ask("worker_2") == "worker_2 did not pause: Only the team lead may ask this.. Nothing was changed there."
    assert "nothing was changed" in await ask("worker_gone", "exit")
    assert "nothing was changed" in await ask("worker_old")
    assert "could not be asked (agent lead_1 has no session yet); nothing was changed" in await ask("worker_unasked")
    # FAIL-6: a lost reply, a handler that failed part way or an unreadable answer may still be followed by the change.
    for target, instruction in (("worker_slow", "exit"), ("worker_failed", "pause"), ("worker_bad", "pause")):
        said = await ask(target, instruction)
        assert f"whether it will {instruction} is unknown" in said and "nothing was changed" not in said, said
    # The request may take its whole lifetime.
    assert {timeout for _, _, timeout in sent} == {control_reply_seconds()} == {30.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["lead_1", "TeamLead", "Me"])
async def test_the_control_tool_never_controls_the_calling_agent(monkeypatch, target) -> None:
    """GEN-3: by id, by name, or by a name that resolves to its id; nothing is sent."""

    asked = AsyncMock()
    monkeypatch.setattr(agent_requests, "ask_agent", asked)
    lead = SimpleNamespace(id="lead_1", name="TeamLead", _agent_id_by_name={"Me": "lead_1"})

    said = await _send_control_instruction_payload(lead, target, "exit")

    assert said.startswith(f"{target} is you") and "Nothing was sent" in said and "plan_runtime_restart" in said
    asked.assert_not_awaited()


def test_the_control_tool_waits_longer_than_the_request_may_take(monkeypatch) -> None:
    """FAIL-6: the tool's own wait never ends first; when it would, it says the outcome is unknown."""

    from custom_tools.providers import agent_management
    from custom_tools.providers._runtime import control_reply_seconds
    from tests.task_work_support import set_agent_ref

    waits: list[float] = []

    def run(agent, coroutine, *, timeout=15.0):
        coroutine.close()
        waits.append(timeout)
        if len(waits) > 1:
            raise TimeoutError(f"the send did not finish within {timeout:g} s and was cancelled")
        return "Researcher-1: paused."

    monkeypatch.setattr(agent_management, "_run_agent_coroutine", run)
    set_agent_ref(SimpleNamespace(id="lead_1", name="TeamLead", _agent_id_by_name={}))
    try:
        assert agent_management.send_control_instruction("Researcher-1", "pause") == "Researcher-1: paused."
        timed_out = agent_management.send_control_instruction("Researcher-1", "exit")
    finally:
        set_agent_ref(None)

    assert waits[0] > control_reply_seconds()
    assert "whether it will exit is unknown" in timed_out


@pytest.mark.asyncio
async def test_a_worker_session_on_the_leads_host_cannot_pause_or_stop_an_agent() -> None:
    """SEC-2 with real frame verification: the contract lets the lead's host send these, but not this session."""

    import copy

    from custom_messages.wire.trust import sign_state
    from tests.message_contract_support import TRUST, example_frame, private_key, resign, trust_store, verify

    session = json.loads((TRUST / "sessions" / "lead.json").read_text())
    session["key"] = "session.install_example.team_example.s-helper-1"
    session["sequence"] = 2
    session["body"].update(session="s-helper-1", agent="helper-on-lead", agent_role="worker", placement_revision=9)
    trust = trust_store(extra_documents=[json.dumps(sign_state(session, private_key(session["writer"]["key_id"]))).encode()])
    agent = stand_in_agent()
    for kind, example, message_id in (("agent.pause", "basic", "01928f3a-7b2c-74d5-8000-00000000aa01"),
                                      ("agent.exit", "full", "01928f3a-7b2c-74d5-8000-00000000aa02")):
        frame_document = copy.deepcopy(example_frame(kind, example))
        frame_document["issuer"].update(agent="helper-on-lead", session="s-helper-1")
        frame_document["message_id"] = message_id
        verified = verify(resign(frame_document), frame_document, trust=trust)
        assert verified.session.agent_role == "worker" and "lead_host" in verified.certificate.roles

        reply = checked_reply(kind, await live_controls.LIVE_CONTROL_HANDLERS[kind](agent, verified))

        assert reply["outcome"] == "not_allowed"
    agent.pause.assert_not_awaited()
    agent.exit_agent.assert_not_awaited()


# -- the old unsigned control message (F7, A6) -------------------------------------------------------


def _unsigned_control(instruction: str, *, claimed_sender: str = "lead_1"):
    """A ControlInstruction as any process with the team's NATS login can publish one: its sender is self-declared."""

    from custom_messages.contracts.control_v1 import ControlInstruction, Instruction
    from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta

    return (ControlInstruction(type=ControlType.CONTROL_MSG, instruction=Instruction(instruction=instruction)),
            MessageHeader(from_agent_id=claimed_sender, to_routing_path="worker_1"),
            MessageMeta(from_agent_name="TeamLead"))


@pytest.mark.asyncio
@pytest.mark.parametrize("instruction", ["exit", "pause", "resume"])
async def test_an_unsigned_exit_pause_or_resume_changes_nothing_and_names_its_claimed_sender(caplog, instruction):
    """On its mailbox or on .operational, both delivered through _dispatch_message."""

    from custom_agents.strands_agent import UniversalAgent

    agent = stand_in_agent()
    agent.handle_control_message = lambda **kwargs: UniversalAgent.handle_control_message(agent, **kwargs)
    content, header, meta = _unsigned_control(instruction)

    with caplog.at_level(logging.WARNING):
        await UniversalAgent._dispatch_message(agent, msg_header=header, msg_content=content, msg_meta=meta)

    agent.pause.assert_not_awaited()
    agent.resume.assert_not_awaited()
    agent.exit_agent.assert_not_awaited()
    [dropped] = [record.getMessage() for record in caplog.records
                 if "event=agent_control_instruction_dropped" in record.getMessage()]
    assert f"instruction={instruction} " in dropped
    assert "claimed_from_agent_id=lead_1 " in dropped and "claimed_from_agent_name=TeamLead" in dropped


@pytest.mark.asyncio
async def test_a_task_stop_still_arrives_as_a_control_message() -> None:
    from custom_agents.strands_agent import UniversalAgent

    agent = stand_in_agent(_resolve_control_target_task_info=AsyncMock(return_value=None),
                           _send_control_status_response=AsyncMock(),
                           task_manager=SimpleNamespace(current_task_id=None))
    content, header, meta = _unsigned_control("stop", claimed_sender="lead_1")
    content.target_task_id = "task_9"

    await UniversalAgent.handle_control_message(agent, control_instruction=content, msg_header=header, msg_meta=meta)

    agent._resolve_control_target_task_info.assert_awaited_once()
    assert agent._send_control_status_response.await_args.kwargs["status_text"] == (
        "No active task matched the stop request.")
