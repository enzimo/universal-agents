"""Messages sent while a task waits on a tool setup are never stranded.

Reproduces the owner's report (2026-09-28): TeamLead's task A paused on a
``set_up_tools`` Google sign-in. The person sent message B in the same chat
while A waited. The sign-in finished, A resumed and answered, and from then on
no task started again: later messages were "added and queued" and nothing
picked them up.

A real ``UniversalAgent`` runs on real NATS (``agent.connect()``: its
subscriptions, scheduler and pending-work loop), with the real turn runtime,
turn-action outbox and ``tool_setup_run_runtime``. Only the Strands agent is
scripted: it pauses A on the setup-run interrupt and answers every other turn
with a structured task response. The gateway's ``ToolSetupResultV1`` arrives
over NATS like the real one.

Both ways the resumed turn can end are covered: it answers, or it raises (a
model stream that times out). Before the fix a raising resumed turn left task
A holding the execution slot, so later messages were never started.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_agents.tool_setup_run_runtime import run_repository
from custom_agents.tool_setup_runs import TOOL_SETUP_RUN_KIND, WAITING, RunPieceV1, ToolSetupRunV1
from custom_messages.contracts.credentials_v1 import ToolSetupResultV1
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from custom_agents.provider_error_runtime import PROVIDER_TIMEOUT_USER_MESSAGE, ModelStreamTimeoutError
from id_generation import new_id
from test_live_multi_agent_state_boundaries import (
    FakeAgentResult,
    _agent,
    _connect_nats,
    _start_agent,
    _stop_agents,
    _task_response,
    _wait_for,
    _wait_for_response,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

SETUP_REQUEST_ID = "setup_google_1"


class _Script:
    """What TeamLead's model does, shared by every Strands agent the runtime creates."""

    def __init__(self, run_id: str, *, resumed_turn_fails: bool) -> None:
        self.run_id = run_id
        self.resumed_turn_fails = resumed_turn_fails
        self.prompts: list[str] = []

    def result_for(self, prompt) -> object:
        if isinstance(prompt, list):  # the resumed turn: interruptResponse blocks
            self.prompts.append("resume A")
            if self.resumed_turn_fails:
                raise ModelStreamTimeoutError(
                    limit="idle", limit_seconds=300.0, elapsed_seconds=300.0,
                    events_received=3, model_id="gpt-test",
                )
            return FakeAgentResult(_task_response("Gmail is ready."))
        self.prompts.append(prompt)
        if len(self.prompts) == 1:  # A's first turn calls set_up_tools
            interrupt = SimpleNamespace(
                id="interrupt_setup_1",
                name=TOOL_SETUP_RUN_KIND,
                reason={"kind": TOOL_SETUP_RUN_KIND, "run_id": self.run_id, "summary": "Google sign-in"},
            )
            return SimpleNamespace(stop_reason="interrupt", interrupts=[interrupt], structured_output=None)
        return FakeAgentResult(_task_response("Answered."))


class _ScriptedStrandsAgent:
    def __init__(self, script: _Script) -> None:
        self.script = script
        self.messages: list = []

    async def invoke_async(self, prompt, **_kwargs):
        return self.script.result_for(prompt)


def _chat(task_id: str, text: str, reply_queue: str, manager_id: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=text),
        msg_header=MessageHeader(from_agent_id="gateway", to_routing_path=manager_id, response_routing_key=reply_queue),
        msg_meta=MessageMeta(
            from_agent_name="person",
            msg_correlation_id=f"corr_{task_id}",
            conversation_id="conv_1",
            requester_type=RequesterType.USER_REQUESTER,
            is_response_requested=True,
            gateway_context=GatewayContext(canonical_user_id="owner", session_key="session_1"),
        ),
    )


def _prompted(script: _Script, text: str) -> bool:
    return any(isinstance(prompt, str) and text in prompt for prompt in script.prompts)


async def _reply_for(replies, task_id: str) -> Task:
    """The next task response the person's chat receives for ``task_id``."""

    while True:
        envelope = await _wait_for_response(replies)
        content = envelope.msg_content
        if isinstance(content, Task) and content.task_id == task_id:
            return content


@pytest.mark.parametrize("resumed_turn_fails", [False, True], ids=["resumed_turn_answers", "resumed_turn_raises"])
async def test_queue_keeps_moving_after_a_task_resumes_from_a_setup_wait(resumed_turn_fails: bool) -> None:
    team_id = new_id("team_setup_wait")
    manager_id = new_id("manager")
    task_a = new_id("task_a")
    run_id = new_id("tool_setup_run")
    script = _Script(run_id, resumed_turn_fails=resumed_turn_fails)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "workspace").mkdir()
        with (
            patch("custom_agents.strands_agent.Agent", side_effect=lambda *a, **k: _ScriptedStrandsAgent(script)),
            patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
        ):
            manager = _agent(
                agent_id=manager_id, name="manager", team_id=team_id, named_agent_type="TeamArchitect",
                db_path=tmp_path / "manager.db", workspace_dir=tmp_path / "workspace", is_lead=True,
            )
            run_repository(manager).create(
                ToolSetupRunV1(
                    run_id=run_id, team_id=team_id, person_id="owner", task_id=task_a, agent_id=manager_id,
                    agent_type="TeamArchitect", tool_ids=("cli:google",),
                    pieces=(RunPieceV1(piece_id="sign_in_google", tool_id="cli:google", kind="sign_in",
                                       status=WAITING, setup_request_id=SETUP_REQUEST_ID, attempt=1),),
                )
            )
            agent_tasks = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                replies = await channel.declare_queue(new_id("person_chat"), durable=False, auto_delete=True)
                agent_tasks.append((manager, await _start_agent(manager)))
                tasks = manager.task_manager

                # A asks about Gmail and pauses on the Google sign-in.
                await channel.publish(manager_id, _chat(task_a, "Can you read my Gmail?", replies.name, manager_id))
                await _wait_for(lambda: task_a in tasks.task_state.human_interaction_blockers)

                # B, in the same chat, while A waits: it is answered.
                await channel.publish(manager_id, _chat(new_id("task_b"), "Is it set up yet?", replies.name, manager_id))
                await _wait_for(lambda: _prompted(script, "Is it set up yet?"))

                # The sign-in finishes: the gateway's result resumes A, which answers.
                result = ToolSetupResultV1(
                    setup_request_id=SETUP_REQUEST_ID, status="completed", parent_task_id=task_a,
                    tool_id="cli:google", kind="sign_in", owner="person", attempt=1, account_label="owner@gmail",
                )
                await channel.publish(
                    manager_id,
                    MessageEnvelope(
                        msg_content=result,
                        msg_header=MessageHeader(from_agent_id="gateway", to_routing_path=manager_id,
                                                 response_routing_key=replies.name),
                        msg_meta=MessageMeta(msg_correlation_id=f"corr_{task_a}", conversation_id="conv_1",
                                             requester_type=RequesterType.USER_REQUESTER, is_response_requested=True),
                    ),
                )
                await _wait_for(lambda: "resume A" in script.prompts)
                await _wait_for(lambda: tasks.get_task_info(task_a) is None)
                answer = await _reply_for(replies, task_a)
                if resumed_turn_fails:
                    # The person is told; the task no longer holds the execution slot.
                    assert answer.task_state[-1][0] == ProgressState.FAILED
                    assert PROVIDER_TIMEOUT_USER_MESSAGE in answer.errors
                else:
                    assert answer.answer == "Gmail is ready."

                # C and D, afterwards: both must run.
                for text in ("hello", "are you there?"):
                    await channel.publish(manager_id, _chat(new_id("task_c"), text, replies.name, manager_id))
                    await _wait_for(lambda text=text: _prompted(script, text), timeout=8)
                await _wait_for(lambda: not tasks.task_state.task_queue and tasks.can_start_new_task())
            finally:
                logging.getLogger(__name__).info("model prompts: %s", [str(p)[:60] for p in script.prompts])
                with contextlib.suppress(Exception):
                    await _stop_agents(agent_tasks)
                await connection.close()
