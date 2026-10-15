"""One message must never stop a receiver, checked against a real JetStream server.

Reproduced 2026-10-05 before the fix: a message of a type the receiving release
did not know ended the pull loop of an agent mailbox (later valid messages waited
forever), and on a shared worker queue the redelivered message stopped every
worker in turn while their processes still looked healthy.

Since 2026-10-07 a message ended on its last delivery is first copied, bytes
unchanged, to the dead-letter stream, where the team lead's reader finds it
(``custom_agents.dead_letter_watch``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_messages.transport_codec import encode_envelope
from custom_transport.base import ConsumerScope, FetchPolicy
from custom_transport.dead_letters import REASON_HANDLER_FAILED, REASON_UNDECODABLE, WATCH_CONSUMER_ROUTE
from custom_transport.nats_adapter import NAMED_WORKER_CONSUMER_ROLE, NatsTransportAdapter
from custom_transport.subjects import NatsSubjectMapper
from id_generation import new_id
from live_nats_client import nats_transport_config_from_env

# Short enough for a test; production returns a failed message after 10 seconds.
_TEST_RETURN_DELAY_SECONDS = 0.2


def _task_bytes(label: str) -> bytes:
    return encode_envelope(
        MessageEnvelope(
            msg_header=MessageHeader(from_agent_id="live_sender", to_routing_path="agent_x"),
            msg_meta=MessageMeta(),
            msg_content=Task(task_type=MessageMetaType.REQUEST, problem_statement=label),
        )
    )


def _unknown_type_bytes() -> bytes:
    document = json.loads(_task_bytes("unknown"))
    document["msg_content"] = {"message_class": "FutureMessageV9", "detail": 1}
    return json.dumps(document).encode()


def _adapter(team_id: str, *, max_deliver: int) -> NatsTransportAdapter:
    config = nats_transport_config_from_env(
        subject_prefix=team_id,
        storage="memory",
        ack_wait_seconds=5.0,
        pool_ack_wait_seconds=5.0,
        max_deliver=max_deliver,
    )
    return NatsTransportAdapter(config=config, mapper=NatsSubjectMapper(team_id=team_id))


async def _wait_until(condition, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await condition():
            return True
        await asyncio.sleep(0.1)
    return await condition()


async def _consumer_settled(js, stream: str, durable: str) -> bool:
    info = await js.consumer_info(stream, durable)
    return info.num_pending == 0 and info.num_ack_pending == 0


async def _delete_streams(adapter: NatsTransportAdapter) -> None:
    for stream in (
        adapter.mapper.work_stream_name(),
        adapter.mapper.presence_stream_name(),
        adapter.mapper.dead_letter_stream_name(),
    ):
        try:
            await adapter._js.delete_stream(stream)
        except Exception:
            pass


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.nats
async def test_a_mailbox_keeps_receiving_after_an_unknown_type_and_a_failed_handling(
    monkeypatch, caplog
):
    monkeypatch.setattr(
        "custom_transport.nats_pull.UNHANDLED_RETURN_DELAY_SECONDS", _TEST_RETURN_DELAY_SECONDS
    )
    team_id = new_id("undecodable")
    adapter = _adapter(team_id, max_deliver=3)
    await adapter.connect(reconcile_topology=True)
    handled: list[str] = []
    failed_once: set[str] = set()

    async def handler(delivery) -> None:
        label = delivery.envelope.msg_content.problem_statement
        if label == "always fails":
            raise RuntimeError("handling this message always fails")
        if label == "fails once" and label not in failed_once:
            failed_once.add(label)
            raise RuntimeError("handler failed outside its own error handling")
        handled.append(label)
        await delivery.ack()

    durable = adapter.mapper.durable_name(f"{team_id}_agent_x")
    try:
        handle = await adapter.subscribe_route(
            name="mailbox",
            routing_key="agent_x",
            handler=handler,
            durable_name=durable,
            consumer_scope=ConsumerScope.ROUTE,
            fetch_policy=FetchPolicy.CONTINUOUS,
        )
        subject = adapter.mapper.agent_work_subject("agent_x")
        unknown = _unknown_type_bytes()
        always_fails = _task_bytes("always fails")
        with caplog.at_level(logging.ERROR):
            for payload in (
                _task_bytes("first"),
                unknown,
                _task_bytes("fails once"),
                always_fails,
                _task_bytes("last"),
            ):
                await adapter._js.publish(subject, payload)
            stream = adapter.mapper.work_stream_name()
            settled = await _wait_until(
                lambda: _consumer_settled(adapter._js, stream, durable), timeout=15
            )

        assert settled, "the unknown message was not ended after its last delivery"
        assert sorted(handled) == ["fails once", "first", "last"]
        assert not handle.raw_subscription.task.done(), "the mailbox stopped asking"
        logged = [record.getMessage() for record in caplog.records]
        assert any(
            "event=nats_message_undecodable" in text
            and "message_class=FutureMessageV9" in text
            and "outcome=ended" in text
            for text in logged
        )
        assert any("event=nats_delivery_handler_failed" in text for text in logged)

        # Both ended messages were set aside, unchanged, for the team lead's reader.
        copies = await adapter.fetch_dead_letters(consumer_route=WATCH_CONSUMER_ROUTE)
        assert sorted((copy.reason, copy.message_class) for copy in copies) == [
            (REASON_HANDLER_FAILED, "Task"),
            (REASON_UNDECODABLE, "FutureMessageV9"),
        ]
        assert {copy.receiver for copy in copies} == {"mailbox"}
        assert {copy.original_subject for copy in copies} == {subject}
        dead_letter_stream = adapter.mapper.dead_letter_stream_name()
        stored = {
            copy.reason: (await adapter._js.get_last_msg(dead_letter_stream, copy.raw_subject)).data
            for copy in copies
        }
        assert stored == {REASON_UNDECODABLE: unknown, REASON_HANDLER_FAILED: always_fails}
        for copy in copies:
            await copy.ack()
        assert await adapter.fetch_dead_letters(consumer_route=WATCH_CONSUMER_ROUTE) == []
        assert adapter.stopped_receivers() == ()
    finally:
        await _delete_streams(adapter)
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.nats
async def test_a_worker_queue_keeps_serving_every_worker_after_an_unknown_type(
    monkeypatch, caplog
):
    monkeypatch.setattr(
        "custom_transport.nats_pull.UNHANDLED_RETURN_DELAY_SECONDS", _TEST_RETURN_DELAY_SECONDS
    )
    team_id = new_id("undecodable")
    workers = [_adapter(team_id, max_deliver=5) for _ in range(2)]
    await workers[0].connect(reconcile_topology=True)
    await workers[1].connect()
    handled: dict[int, list[str]] = {0: [], 1: []}
    handles = []
    try:
        for index, adapter in enumerate(workers):

            async def handler(delivery, index=index) -> None:
                handled[index].append(delivery.envelope.msg_content.problem_statement)
                await delivery.ack()

            handles.append(
                await adapter.subscribe_subject(
                    name=f"worker{index}",
                    subject=adapter.mapper.named_type_work_subject("Researcher"),
                    handler=handler,
                    durable_name=adapter.worker_queue_durable_name("Researcher"),
                    consumer_scope=ConsumerScope.ROUTE,
                    consumer_role=NAMED_WORKER_CONSUMER_ROLE,
                    fetch_policy=FetchPolicy.WHEN_READY,
                    ready=lambda: True,
                )
            )
        js = workers[0]._js
        subject = workers[0].mapper.named_type_work_subject("Researcher")
        with caplog.at_level(logging.ERROR):
            await js.publish(subject, _unknown_type_bytes())
            await asyncio.sleep(1.0)  # both workers meet the unknown message first
            for number in range(4):
                await js.publish(subject, _task_bytes(f"task {number}"))
            stream = workers[0].mapper.work_stream_name()
            durable = workers[0].worker_queue_durable_name("Researcher")
            settled = await _wait_until(
                lambda: _consumer_settled(js, stream, durable), timeout=30
            )

        assert settled, "tasks or the unknown message were left in the queue"
        assert sorted(handled[0] + handled[1]) == [f"task {n}" for n in range(4)]
        assert [handle.raw_subscription.task.done() for handle in handles] == [False, False]
        logged = [record.getMessage() for record in caplog.records]
        assert any(
            "event=nats_message_undecodable" in text and "outcome=returned" in text
            for text in logged
        )
        assert any(
            "event=nats_message_undecodable" in text and "outcome=ended" in text
            for text in logged
        )
        # Set aside once, by the worker that met it on its last delivery.
        copies = await workers[0].fetch_dead_letters(consumer_route=WATCH_CONSUMER_ROUTE)
        assert [(copy.reason, copy.message_class) for copy in copies] == [
            (REASON_UNDECODABLE, "FutureMessageV9")
        ]
    finally:
        await _delete_streams(workers[0])
        for adapter in workers:
            await adapter.close()
