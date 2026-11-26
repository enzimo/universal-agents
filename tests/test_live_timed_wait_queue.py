"""NATS-backed integration tests for timed waits on a real UniversalAgent (C02).

Runs a real ``UniversalAgent`` connected to a real NATS server
(``agent.connect()``: its real subscriptions, scheduler loop, pending-work
drain, turn runtime, and ``timed_wait_runtime``). Only the Strands ``Model`` is
scripted, so the real Strands ``Agent`` and the real ``wait`` tool execute end
to end.

Covers two scenarios:
1. Task A calls ``wait(5, ...)``, freeing the worker; task B arrives and is
   answered while A waits; the clock passes A's resume time, the real scheduler
   pass resumes A through the real drain, A reports the waited time, and task C
   starts afterwards.
2. Task B is still executing when A's wait comes due: A's answer is held
   (``answer_held`` on its blocker record) until B finishes, then the real
   drain resumes A right after B, and task C runs afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from strands.models import Model

from custom_agents import timed_waits as timed_waits_rules
from custom_agents.structured_llm_messages import (
    ModelAgentTurnDecision,
    ModelTaskResponseAction,
    ModelTaskResponsePayload,
)
from custom_agents.task_human_interaction_blockers import answer_held, get_human_interaction_blocker
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from custom_tools.providers.timed_waits import wait
from custom_tools.runtime_context import set_agent_ref
from id_generation import new_id
from test_live_multi_agent_state_boundaries import (
    _agent,
    _connect_nats,
    _start_agent,
    _stop_agents,
    _wait_for,
    _wait_for_response,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

BASE_TIME = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)


def _extract_wait_result(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for msg in reversed(messages or ()):
        if msg.get("role") != "user":
            continue
        for block in msg.get("content") or ():
            result = block.get("toolResult")
            if not isinstance(result, dict):
                continue
            for part in result.get("content") or ():
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict) and parsed.get("status") == "waited":
                    return parsed
    return None


def _prompt_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages or ()):
        if msg.get("role") != "user":
            continue
        parts = [block["text"] for block in (msg.get("content") or ()) if isinstance(block.get("text"), str)]
        if parts:
            return "\n".join(parts)
    return ""


def _model_response(answer: str) -> ModelAgentTurnDecision:
    return ModelAgentTurnDecision(
        actions=[ModelTaskResponseAction(task=ModelTaskResponsePayload(answer=answer))]
    )


class _ScriptedLiveWaitModel(Model):
    """Real Strands Model that calls the real ``wait`` tool for task A and answers all other turns."""

    def __init__(
        self,
        *,
        hold_b_started: asyncio.Event | None = None,
        release_b: asyncio.Event | None = None,
    ) -> None:
        self.hold_b_started = hold_b_started
        self.release_b = release_b
        self.seen_events: list[str] = []
        self._a_called_wait = False
        self._b_started = False
        self._decision_seq = 0

    def update_config(self, **config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted-live-wait"}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        decision = self._decision_for(prompt)
        yield {"output": decision}

    def _decision_for(self, messages: Any) -> ModelAgentTurnDecision:
        wait_res = _extract_wait_result(messages if isinstance(messages, list) else [])
        if wait_res is not None:
            self.seen_events.append("answer_A_after_wait")
            return _model_response(
                f"Done waiting {wait_res['waited_minutes']}m at {wait_res['now_utc']}."
            )
        text = _prompt_text(messages if isinstance(messages, list) else [])
        if "TASK_C_WORK" in text:
            self.seen_events.append("answer_C")
            return _model_response("C answered.")
        if "TASK_B_WORK" in text:
            self.seen_events.append("answer_B")
            return _model_response("B answered.")
        return _model_response("Answered.")

    async def stream(self, messages, tool_specs=None, system_prompt=None, *, structured_output_model=None, **kwargs):
        wait_res = _extract_wait_result(messages)
        text = _prompt_text(messages)
        if wait_res is None and "TASK_A_WAIT" in text and not self._a_called_wait:
            self._a_called_wait = True
            self.seen_events.append("call_wait_A")
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "tu_live_wait_a", "name": "wait"}}}}
            yield {
                "contentBlockDelta": {
                    "delta": {"toolUse": {"input": json.dumps({"minutes": 5, "reason": "Portainer redeploy"})}}
                }
            }
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return

        if "TASK_C_WORK" in text:
            self.seen_events.append("start_C")
        elif "TASK_B_WORK" in text and not self._b_started:
            self._b_started = True
            self.seen_events.append("start_B")
            if self.hold_b_started is not None:
                self.hold_b_started.set()
            if self.release_b is not None:
                await self.release_b.wait()

        decision = self._decision_for(messages)
        tool_name = (
            structured_output_model.__name__
            if structured_output_model is not None
            else ModelAgentTurnDecision.__name__
        )
        self._decision_seq += 1
        yield {"messageStart": {"role": "assistant"}}
        yield {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": f"tu_decision_{self._decision_seq}", "name": tool_name}}
            }
        }
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": decision.model_dump_json()}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def _chat(task_id: str, text: str, reply_queue: str, manager_id: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=text),
        msg_header=MessageHeader(from_agent_id="gateway", to_routing_path=manager_id, response_routing_key=reply_queue),
        msg_meta=MessageMeta(
            from_agent_name="person",
            msg_correlation_id=f"corr_{task_id}",
            conversation_id="conv_live_tw",
            requester_type=RequesterType.USER_REQUESTER,
            is_response_requested=True,
            gateway_context=GatewayContext(canonical_user_id="owner", session_key="session_live_tw"),
        ),
    )


async def _reply_for(replies: Any, task_id: str) -> Task:
    while True:
        envelope = await _wait_for_response(replies, timeout=15.0)
        content = envelope.msg_content
        if isinstance(content, Task) and content.task_id == task_id:
            return content


async def test_live_timed_wait_allows_b_meanwhile_resumes_a_and_runs_c() -> None:
    """A pauses via real wait tool -> B answered meanwhile -> clock passes -> A resumes & reports -> C runs."""

    now_box = [BASE_TIME]
    team_id = new_id("team_live_tw")
    manager_id = new_id("manager")
    task_a, task_b, task_c = new_id("task_a"), new_id("task_b"), new_id("task_c")
    model = _ScriptedLiveWaitModel()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "workspace").mkdir()
        with (
            patch.object(timed_waits_rules, "utc_now", side_effect=lambda: now_box[0]),
            patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=model),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=tmp_path / "manager.db",
                workspace_dir=tmp_path / "workspace",
                is_lead=True,
            )
            manager.authorization_config = {"enabled": False}
            manager.tools = [wait]
            manager._strands_parts.replace_before_next_turn = True
            agent_tasks = []
            connection = await _connect_nats(team_id)
            set_agent_ref(manager)
            try:
                channel = await connection.channel()
                replies = await channel.declare_queue(new_id("person_chat"), durable=False, auto_delete=True)
                agent_tasks.append((manager, await _start_agent(manager)))
                tasks = manager.task_manager

                # 1. Task A runs real wait(5, "Portainer redeploy") and frees the worker slot.
                await channel.publish(manager_id, _chat(task_a, "TASK_A_WAIT: wait 5 minutes", replies.name, manager_id))
                # Task operations run on their own thread: the record is saved first,
                # then the state shows WAITING, and the slot is released only after
                # that state change's history entry commits. Wait for all three; the
                # timeout fails the test if the slot is never freed.
                await _wait_for(
                    lambda: (get_human_interaction_blocker(tasks, task_a) or {}).get("kind") == "timed_wait"
                    and getattr(tasks.get_task_info(task_a), "state", None) == ProgressState.WAITING
                    and tasks.task_state.current_in_progress_task_id is None,
                    timeout=15.0,
                )

                # 2. Task B arrives while A waits and is answered.
                await channel.publish(manager_id, _chat(task_b, "TASK_B_WORK: quick question", replies.name, manager_id))
                reply_b = await _reply_for(replies, task_b)
                assert reply_b.answer == "B answered."

                # 3. Clock passes A's resume time; wake scheduler so real pass resumes A through real drain.
                now_box[0] = BASE_TIME + timedelta(minutes=5, seconds=6)
                if manager._scheduler_wake is not None:
                    manager._scheduler_wake.set()
                reply_a = await _reply_for(replies, task_a)
                assert "Done waiting 5.1m" in (reply_a.answer or "")
                assert now_box[0].isoformat() in (reply_a.answer or "")
                await _wait_for(lambda: tasks.get_task_info(task_a) is None, timeout=10.0)

                # 4. Task C arrives afterwards and runs cleanly.
                await channel.publish(manager_id, _chat(task_c, "TASK_C_WORK: follow-up", replies.name, manager_id))
                reply_c = await _reply_for(replies, task_c)
                assert reply_c.answer == "C answered."
                await _wait_for(lambda: not tasks.task_state.task_queue and tasks.can_start_new_task(), timeout=10.0)
            finally:
                logging.getLogger(__name__).info("live wait events: %s", model.seen_events)
                with contextlib.suppress(Exception):
                    await _stop_agents(agent_tasks)
                set_agent_ref(None)
                await connection.close()


async def test_live_timed_wait_due_while_b_is_running_holds_answer_then_resumes_a() -> None:
    """B is still running when A comes due: A's answer is held until B ends, then A resumes, then C runs."""

    now_box = [BASE_TIME]
    team_id = new_id("team_tw_busy")
    manager_id = new_id("manager")
    task_a, task_b, task_c = new_id("task_a"), new_id("task_b"), new_id("task_c")
    b_started = asyncio.Event()
    release_b = asyncio.Event()
    model = _ScriptedLiveWaitModel(hold_b_started=b_started, release_b=release_b)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "workspace").mkdir()
        with (
            patch.object(timed_waits_rules, "utc_now", side_effect=lambda: now_box[0]),
            patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=model),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=tmp_path / "manager.db",
                workspace_dir=tmp_path / "workspace",
                is_lead=True,
            )
            manager.authorization_config = {"enabled": False}
            manager.tools = [wait]
            manager._strands_parts.replace_before_next_turn = True
            agent_tasks = []
            connection = await _connect_nats(team_id)
            set_agent_ref(manager)
            try:
                channel = await connection.channel()
                replies = await channel.declare_queue(new_id("person_chat"), durable=False, auto_delete=True)
                agent_tasks.append((manager, await _start_agent(manager)))
                tasks = manager.task_manager

                # 1. Task A pauses on real wait(5, ...).
                await channel.publish(manager_id, _chat(task_a, "TASK_A_WAIT: wait 5 minutes", replies.name, manager_id))
                await _wait_for(
                    lambda: (get_human_interaction_blocker(tasks, task_a) or {}).get("kind") == "timed_wait",
                    timeout=15.0,
                )

                # 2. Task B starts and holds the execution lane.
                await channel.publish(manager_id, _chat(task_b, "TASK_B_WORK: slow task", replies.name, manager_id))
                await asyncio.wait_for(b_started.wait(), timeout=15.0)

                # 3. While B is still running, A's wait comes due: scheduler marks answer_held and queues it.
                now_box[0] = BASE_TIME + timedelta(minutes=5, seconds=12)
                if manager._scheduler_wake is not None:
                    manager._scheduler_wake.set()
                await _wait_for(
                    lambda: answer_held(get_human_interaction_blocker(tasks, task_a), manager_id),
                    timeout=15.0,
                )
                assert task_a in manager._pending_interrupt_responses

                # 4. Release B: B answers, then the drain resumes A right after B.
                release_b.set()
                reply_b = await _reply_for(replies, task_b)
                assert reply_b.answer == "B answered."

                reply_a = await _reply_for(replies, task_a)
                assert "Done waiting 5.2m" in (reply_a.answer or "")
                await _wait_for(lambda: tasks.get_task_info(task_a) is None, timeout=10.0)

                # 5. Task C still runs cleanly afterwards.
                await channel.publish(manager_id, _chat(task_c, "TASK_C_WORK: after busy", replies.name, manager_id))
                reply_c = await _reply_for(replies, task_c)
                assert reply_c.answer == "C answered."
            finally:
                release_b.set()
                logging.getLogger(__name__).info("live wait busy events: %s", model.seen_events)
                with contextlib.suppress(Exception):
                    await _stop_agents(agent_tasks)
                set_agent_ref(None)
                await connection.close()
