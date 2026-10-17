"""A task NATS delivers again after its holder saved it runs once (real NATS, fake models,
one shared database).

Reproduces the 2026-10-17 finding: an agent checked only its own records for
duplicates, so a task delivered again while its first holder (or the
replacement taking over that holder's saved work) still held it ran twice.
The pool acknowledgement wait is shortened to observe redelivery quickly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import custom_agents.task_message_runtime as task_message_runtime
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from custom_transport.nats_adapter import NatsTransportAdapter
from id_generation import new_id
from test_live_multi_agent_state_boundaries import (
    DelayedFakeStrandsAgent,
    _agent,
    _connect_nats,
    _publish_envelope,
    _start_agent,
    _stop_agents,
    _task_response,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

WORKER_TYPE = "ResearchComparisonAgent"
ACK_WAIT_SECONDS = 3.0


def _worker(name: str, team_id: str, db_path: Path, workspace: Path):
    worker = _agent(
        agent_id=new_id(name), name=name, team_id=team_id, named_agent_type=WORKER_TYPE,
        db_path=db_path, workspace_dir=workspace,
    )
    worker.named_agent_type_queue_map.update({WORKER_TYPE: f"{team_id}_{WORKER_TYPE}"})
    adapter = worker._ensure_nats_transport_adapter()
    adapter.config = dataclasses.replace(adapter.config, pool_ack_wait_seconds=ACK_WAIT_SECONDS)
    return worker


def _delegated_task(team_id: str, reply_route: str) -> MessageEnvelope:
    queue = f"{team_id}_{WORKER_TYPE}"
    return MessageEnvelope(
        msg_content=Task(
            task_id=new_id("delegated"), parent_task_id="parent_x",
            task_type=MessageMetaType.REQUEST, problem_statement="Do this once.",
        ),
        msg_header=MessageHeader(from_agent_id="requester_x", to_routing_path=queue, response_routing_key=reply_route),
        msg_meta=MessageMeta(
            from_agent_name="requester", to_named_agent_type=WORKER_TYPE,
            requester_type=RequesterType.AGENT_REQUESTER, is_response_requested=True,
            msg_correlation_id=new_id("corr"),
        ),
    )


async def _answers(reply_queue, seconds: float) -> list[tuple[str, str]]:
    answers = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        message = await reply_queue.get(timeout=0.5, fail=False)
        if message is not None and isinstance(message.msg_content, Task) and message.msg_content.answer:
            answers.append((message.msg_header.from_agent_id, message.msg_content.answer))
    return answers


async def test_live_a_copy_delivered_after_its_holder_stopped_runs_once() -> None:
    """Worker A saves the task and stops before acknowledging it. B takes the copy
    NATS sends again (A is gone); replacement C takes over A's saved work but
    leaves that task out because B now holds it."""

    team_id = new_id("team_custody")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        db_path = tmp_path / "instance.db"
        llms = [DelayedFakeStrandsAgent(0.1, _task_response(f"{name}_RESULT")) for name in ("A", "B", "C")]
        saved = asyncio.Event()
        real_claim = task_message_runtime.send_task_claim

        async def claim(agent, **kwargs):
            if str(agent.name).startswith("worker-a"):
                saved.set()
                await asyncio.Event().wait()  # A stops here: the task is saved, not acknowledged
            return await real_claim(agent, **kwargs)

        with (
            patch("custom_agents.strands_agent.Agent", side_effect=llms),
            patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
            patch.object(task_message_runtime, "send_task_claim", claim),
        ):
            a, b, c = (_worker(f"worker-{n}", team_id, db_path, workspace) for n in "abc")
            connection = await _connect_nats(team_id)
            running = []
            try:
                channel = await connection.channel()
                reply = await channel.declare_queue(new_id("reply"), durable=False, auto_delete=True)
                running.append((a, await _start_agent(a)))
                envelope = _delegated_task(team_id, reply.name)
                await _publish_envelope(channel, envelope.msg_header.to_routing_path, envelope)
                await asyncio.wait_for(saved.wait(), 10)
                running.append((b, await _start_agent(b)))
                await _stop_agents([running.pop(0)])
                answers = await _answers(reply, ACK_WAIT_SECONDS + 6)
                running.append((c, await _start_agent(c)))
                answers += await _answers(reply, 4)
            finally:
                await _stop_agents(running)
                await connection.close()
    model_calls = [len(llm.invocations) for llm in llms]
    assert answers == [(b.id, "B_RESULT")]
    assert model_calls == [0, 1, 0]


async def test_live_a_copy_delivered_while_its_holder_runs_is_dropped() -> None:
    """A's acknowledgement is lost, so NATS sends the task again while A runs it;
    B finds A holding it and drops the copy."""

    team_id = new_id("team_custody")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        db_path = tmp_path / "instance.db"
        llms = [DelayedFakeStrandsAgent(ACK_WAIT_SECONDS + 4, _task_response("A_RESULT")),
                DelayedFakeStrandsAgent(0.1, _task_response("B_RESULT"))]
        real_delivery = NatsTransportAdapter._delivery_from_msg
        lost_ack_adapters: set[int] = set()

        def delivery_without_ack(self, msg, **kwargs):
            delivery = real_delivery(self, msg, **kwargs)
            if id(self) in lost_ack_adapters and isinstance(delivery.envelope.msg_content, Task):
                async def lost() -> None:
                    return None

                return dataclasses.replace(delivery, _ack=lost)
            return delivery

        with (
            patch("custom_agents.strands_agent.Agent", side_effect=llms),
            patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
            patch.object(NatsTransportAdapter, "_delivery_from_msg", delivery_without_ack),
        ):
            a, b = (_worker(f"worker-{n}", team_id, db_path, workspace) for n in "ab")
            lost_ack_adapters.add(id(a._ensure_nats_transport_adapter()))
            connection = await _connect_nats(team_id)
            running = []
            try:
                channel = await connection.channel()
                reply = await channel.declare_queue(new_id("reply"), durable=False, auto_delete=True)
                running.append((a, await _start_agent(a)))
                envelope = _delegated_task(team_id, reply.name)
                await _publish_envelope(channel, envelope.msg_header.to_routing_path, envelope)
                await asyncio.sleep(1.0)  # A has taken it; its acknowledgement never arrives
                running.append((b, await _start_agent(b)))
                answers = await _answers(reply, ACK_WAIT_SECONDS + 8)
            finally:
                await _stop_agents(running)
                await connection.close()
    assert answers == [(a.id, "A_RESULT")]
    assert [len(llm.invocations) for llm in llms] == [1, 0]
