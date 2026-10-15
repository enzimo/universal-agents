"""Pull delivery (ADR-019) and worker readiness, with fake NATS objects."""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from custom_agents.pool_intake import mailbox_has_room, may_take_pool_task
from custom_transport.base import FetchPolicy
from custom_transport.nats_pull import (
    CONSUMER_METADATA_SCHEMA,
    PullRunner,
    consumer_needs_replacement,
    ensure_pull_consumer,
)


class _NotFound(Exception):
    pass


class _FakeJetStream:
    def __init__(self, existing=None):
        self.existing = existing
        self.deleted: list[str] = []
        self.added: list = []

    async def consumer_info(self, stream, durable):
        if self.existing is None:
            from nats.js.errors import NotFoundError

            raise NotFoundError()
        return self.existing

    async def delete_consumer(self, stream, durable):
        self.deleted.append(durable)

    async def add_consumer(self, stream, config):
        self.added.append(config)


def _info(*, deliver_subject=None, schema=CONSUMER_METADATA_SCHEMA):
    return SimpleNamespace(
        config=SimpleNamespace(
            deliver_subject=deliver_subject,
            metadata={"ua.managed": "true", "ua.schema": schema},
        ),
        num_pending=3,
        num_ack_pending=0,
    )


def test_push_and_old_schema_consumers_need_replacement():
    assert consumer_needs_replacement(_info(deliver_subject="x").config) == "push_consumer"
    assert consumer_needs_replacement(_info(schema="consumer-lifecycle-v1").config) == "old_consumer_schema"
    assert consumer_needs_replacement(_info().config) is None
    assert consumer_needs_replacement(SimpleNamespace(deliver_subject=None, metadata={})) is None


@pytest.mark.asyncio
async def test_ensure_replaces_a_push_consumer_and_logs(caplog):
    jetstream = _FakeJetStream(existing=_info(deliver_subject="deliver.x"))
    config = SimpleNamespace(durable_name="workers_X")
    with caplog.at_level(logging.WARNING):
        await ensure_pull_consumer(jetstream, stream_name="S", config=config, logger=logging.getLogger("t"))
    assert jetstream.deleted == ["workers_X"]
    assert jetstream.added == [config]
    assert "nats_consumer_replaced" in caplog.text


@pytest.mark.asyncio
async def test_ensure_updates_a_current_pull_consumer_in_place():
    jetstream = _FakeJetStream(existing=_info())
    config = SimpleNamespace(durable_name="workers_X")
    await ensure_pull_consumer(jetstream, stream_name="S", config=config, logger=logging.getLogger("t"))
    assert jetstream.deleted == []
    assert jetstream.added == [config]


class _Message:
    def __init__(self, name):
        self.name = name
        self.naked = False

    async def nak(self, delay=None):
        self.naked = True


class _Subscription:
    def __init__(self, batches):
        self.batches = list(batches)
        self.fetches = 0
        self.pending_msgs = 0

    async def fetch(self, batch, timeout):
        self.fetches += 1
        if self.batches:
            return self.batches.pop(0)
        await asyncio.sleep(timeout)
        raise TimeoutError()

    async def unsubscribe(self):
        return None


def _runner(subscription, *, ready, policy, handled):
    async def callback(message):
        handled.append(message.name)

    runner = PullRunner(
        name="test",
        jetstream=None,
        stream_name="S",
        consumer_config=SimpleNamespace(durable_name="d"),
        policy=policy,
        ready=ready,
        callback=callback,
        is_closing=lambda: False,
        logger=logging.getLogger("test"),
    )
    runner._subscription = subscription
    return runner


@pytest.mark.asyncio
async def test_a_worker_that_is_not_ready_does_not_ask():
    subscription = _Subscription([[ _Message("a") ]])
    handled: list[str] = []
    runner = _runner(subscription, ready=lambda: False, policy=FetchPolicy.WHEN_READY, handled=handled)
    task = asyncio.create_task(runner._run())
    await asyncio.sleep(0.6)
    runner._stopping = True
    await asyncio.wait_for(task, timeout=2)
    assert subscription.fetches == 0
    assert handled == []


@pytest.mark.asyncio
async def test_a_task_that_arrives_after_the_worker_became_busy_goes_back():
    message = _Message("late")
    calls = {"n": 0}

    def ready():
        calls["n"] += 1
        return calls["n"] == 1  # ready when asking, busy when the task arrives

    subscription = _Subscription([[message]])
    handled: list[str] = []
    runner = _runner(subscription, ready=ready, policy=FetchPolicy.WHEN_READY, handled=handled)
    task = asyncio.create_task(runner._run())
    await asyncio.sleep(0.3)
    runner._stopping = True
    await asyncio.wait_for(task, timeout=2)
    assert message.naked
    assert handled == []


@pytest.mark.asyncio
async def test_a_ready_worker_handles_one_task_at_a_time():
    subscription = _Subscription([[_Message("a")], [_Message("b")]])
    handled: list[str] = []
    runner = _runner(subscription, ready=lambda: True, policy=FetchPolicy.WHEN_READY, handled=handled)
    task = asyncio.create_task(runner._run())
    await asyncio.sleep(0.2)
    runner._stopping = True
    await runner.unsubscribe()
    await asyncio.gather(task, return_exceptions=True)
    assert handled == ["a", "b"]


class _Inbox:
    def __init__(self, size, capacity):
        self.size = size
        self.capacity = capacity

    def __len__(self):
        return self.size


def _agent(**overrides):
    state = SimpleNamespace(current_in_progress_task_id=None, task_queue=[])
    manager = SimpleNamespace(task_state=state, is_buffer_full=lambda: False)
    agent = SimpleNamespace(
        _worker_ready=True,
        paused=False,
        is_executing=False,
        _pool_draining=False,
        _provider_cooldown_until=None,
        _lease_loss_wind_down=None,
        _pending_interrupt_responses={},
        deferred_inbox=_Inbox(0, 100),
        task_manager=manager,
        get_exit_event=lambda: asyncio.Event(),
    )
    for name, value in overrides.items():
        setattr(agent, name, value)
    return agent


def test_a_free_worker_may_take_a_task():
    assert may_take_pool_task(_agent())


@pytest.mark.parametrize(
    "overrides",
    [
        {"_worker_ready": False},
        {"paused": True},
        {"is_executing": True},
        {"_pool_draining": True},
        {"_lease_loss_wind_down": object()},
        {"_provider_cooldown_until": lambda: time.monotonic() + 60},
        {"_pending_interrupt_responses": {"t": object()}},
        {"deferred_inbox": _Inbox(1, 100)},
    ],
)
def test_a_busy_or_leaving_worker_may_not_take_a_task(overrides):
    # Times are computed when the test runs, not when it is collected.
    values = {name: value() if callable(value) else value for name, value in overrides.items()}
    assert not may_take_pool_task(_agent(**values))


def test_queued_local_work_or_a_full_queue_stops_asking():
    busy = _agent()
    busy.task_manager.task_state.task_queue = ["t1"]
    assert not may_take_pool_task(busy)
    full = _agent()
    full.task_manager.is_buffer_full = lambda: True
    assert not may_take_pool_task(full)


def test_an_expired_cooldown_no_longer_blocks():
    assert may_take_pool_task(_agent(_provider_cooldown_until=time.monotonic() - 1))


def test_a_mailbox_stops_asking_when_its_inbox_is_full():
    assert mailbox_has_room(_agent(deferred_inbox=_Inbox(99, 100)))
    assert not mailbox_has_room(_agent(deferred_inbox=_Inbox(100, 100)))


def test_an_ephemeral_push_listener_on_the_work_stream_is_refused():
    from custom_transport.base import SubscriptionDelivery
    from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
    from custom_transport.subjects import NatsSubjectMapper

    adapter = NatsTransportAdapter(
        config=NatsTransportConfig(),
        mapper=NatsSubjectMapper(team_id="team_t"),
        logger=logging.getLogger("test"),
    )
    adapter._js = object()
    work_subject = adapter.mapper.named_type_work_subject("WebResearchAgent")
    with pytest.raises(ValueError, match="ADR-019"):
        adapter._validate_subscription_request(
            name="x",
            subject=work_subject,
            delivery=SubscriptionDelivery.JETSTREAM_EPHEMERAL,
            durable_name=None,
            fetch_policy=FetchPolicy.CONTINUOUS,
            ready=None,
        )
    # Presence replay keeps its ephemeral listener.
    adapter._validate_subscription_request(
        name="presence",
        subject=f"{adapter.mapper.prefix}.presence.*",
        delivery=SubscriptionDelivery.JETSTREAM_EPHEMERAL,
        durable_name=None,
        fetch_policy=FetchPolicy.CONTINUOUS,
        ready=None,
    )


# -- one message must never stop a receiver ---------------------------------------
#
# Reproduced 2026-10-05 with real NATS: an error that escaped a message's handling
# (an undecodable message of a type another release added or removed was enough)
# ended the pull loop, so the receiver never asked again, and on a shared worker
# queue the redelivered message stopped every worker in turn.


class _JsMessage:
    """A received JetStream message: records how it was settled."""

    def __init__(self, name, *, data=b"", delivered=1, acked=False, subject="team.agent.a.work"):
        self.name = name
        self.data = data
        self.subject = subject
        self.metadata = SimpleNamespace(num_delivered=delivered, sequence=SimpleNamespace(stream=7))
        self.is_acked = acked
        self.nak_delays: list = []
        self.terminated = False

    async def nak(self, delay=None):
        self.nak_delays.append(delay)

    async def term(self):
        self.terminated = True


def _failing_runner(subscription, *, fail_on, handled, max_deliver=5):
    async def callback(message):
        if message.name in fail_on:
            raise RuntimeError(f"handling {message.name} failed")
        handled.append(message.name)

    runner = PullRunner(
        name="mailbox",
        jetstream=None,
        stream_name="S",
        consumer_config=SimpleNamespace(durable_name="d", max_deliver=max_deliver),
        policy=FetchPolicy.CONTINUOUS,
        ready=lambda: True,
        callback=callback,
        is_closing=lambda: False,
        logger=logging.getLogger("test.pull"),
    )
    runner._subscription = subscription
    return runner


async def _run_briefly(runner, seconds=0.3) -> bool:
    """Run the loop for a moment; return whether it was still asking at the end."""

    task = asyncio.create_task(runner._run())
    runner._task = task
    await asyncio.sleep(seconds)
    still_running = not task.done()
    await runner.unsubscribe()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
    return still_running


@pytest.mark.asyncio
async def test_a_handling_error_returns_the_message_and_the_receiver_keeps_asking(caplog):
    from custom_transport.nats_pull import UNHANDLED_RETURN_DELAY_SECONDS

    bad, good = _JsMessage("bad", delivered=1), _JsMessage("good")
    handled: list[str] = []
    runner = _failing_runner(_Subscription([[bad], [good]]), fail_on={"bad"}, handled=handled)
    with caplog.at_level(logging.ERROR, logger="test.pull"):
        still_running = await _run_briefly(runner)

    assert still_running
    assert handled == ["good"]
    assert bad.nak_delays == [UNHANDLED_RETURN_DELAY_SECONDS] and not bad.terminated
    record = next(r for r in caplog.records if "event=nats_delivery_handler_failed" in r.getMessage())
    assert "outcome=returned" in record.getMessage() and "error_type=RuntimeError" in record.getMessage()
    assert record.exc_info is not None


@pytest.mark.asyncio
async def test_a_message_that_fails_on_its_last_delivery_is_ended(caplog):
    last = _JsMessage("bad", delivered=5)
    runner = _failing_runner(_Subscription([[last]]), fail_on={"bad"}, handled=[], max_deliver=5)
    with caplog.at_level(logging.ERROR, logger="test.pull"):
        await _run_briefly(runner)

    assert last.terminated and last.nak_delays == []
    assert any("outcome=ended" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_message_the_handler_already_settled_is_not_returned_again(caplog):
    settled = _JsMessage("bad", acked=True)
    runner = _failing_runner(_Subscription([[settled]]), fail_on={"bad"}, handled=[])
    with caplog.at_level(logging.ERROR, logger="test.pull"):
        await _run_briefly(runner)

    assert settled.nak_delays == [] and not settled.terminated
    assert any("outcome=already_settled" in r.getMessage() for r in caplog.records)


def _adapter():
    from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
    from custom_transport.subjects import NatsSubjectMapper

    return NatsTransportAdapter(
        config=NatsTransportConfig(),
        mapper=NatsSubjectMapper(team_id="team_t"),
        logger=logging.getLogger("test.adapter"),
    )


def _valid_envelope_json() -> dict:
    import json

    from custom_messages.contracts.envelope_v1 import MessageEnvelope
    from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType
    from custom_messages.transport_codec import encode_envelope

    envelope = MessageEnvelope(
        msg_header=MessageHeader(from_agent_id="sender", to_routing_path="agent_a"),
        msg_meta=MessageMeta(),
        msg_content=Task(task_type=MessageMetaType.REQUEST, problem_statement="private request text"),
    )
    return json.loads(encode_envelope(envelope))


async def _decode(adapter, message, *, jetstream=True, max_deliver=5):
    return await adapter._decode_or_settle(
        message,
        subscription_name="mailbox",
        logical_route="agent_a",
        max_deliver=max_deliver,
        ack_wait_seconds=30.0,
        jetstream=jetstream,
    )


@pytest.mark.asyncio
async def test_a_message_of_an_unknown_type_is_logged_without_content_and_returned(caplog):
    import json

    from custom_transport.nats_pull import UNHANDLED_RETURN_DELAY_SECONDS

    document = _valid_envelope_json()
    document["msg_content"] = {"message_class": "FutureMessageV9", "secret_detail": "do-not-log-me"}
    message = _JsMessage("unknown", data=json.dumps(document).encode(), delivered=2)
    with caplog.at_level(logging.ERROR, logger="test.adapter"):
        delivery = await _decode(_adapter(), message)

    assert delivery is None
    assert message.nak_delays == [UNHANDLED_RETURN_DELAY_SECONDS] and not message.terminated
    text = next(r.getMessage() for r in caplog.records if "event=nats_message_undecodable" in r.getMessage())
    assert "message_class=FutureMessageV9" in text
    assert f"message_id={document['msg_header']['message_id']}" in text
    assert "msg_content: union_tag_invalid" in text and "outcome=returned" in text and "delivery=2" in text
    assert "do-not-log-me" not in text and "private request text" not in text


@pytest.mark.asyncio
async def test_an_unreadable_message_on_its_last_delivery_is_ended(caplog):
    message = _JsMessage("garbage", data=b"{not json", delivered=5)
    with caplog.at_level(logging.ERROR, logger="test.adapter"):
        assert await _decode(_adapter(), message, max_deliver=5) is None

    assert message.terminated and message.nak_delays == []
    text = next(r.getMessage() for r in caplog.records if "event=nats_message_undecodable" in r.getMessage())
    assert "outcome=ended" in text and "JSONDecodeError" in text and "message_id=None" in text


@pytest.mark.asyncio
async def test_an_unreadable_core_nats_message_is_dropped_and_logged(caplog):
    message = _JsMessage("core", data=b"[]")
    with caplog.at_level(logging.ERROR, logger="test.adapter"):
        assert await _decode(_adapter(), message, jetstream=False) is None

    assert message.nak_delays == [] and not message.terminated
    assert any("outcome=dropped" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_readable_message_is_decoded_and_left_for_its_handler():
    import json

    message = _JsMessage("valid", data=json.dumps(_valid_envelope_json()).encode())
    delivery = await _decode(_adapter(), message)

    assert delivery is not None
    assert delivery.envelope.msg_content.problem_statement == "private request text"
    assert delivery.max_deliver == 5 and delivery.redelivery_count == 0
    assert message.nak_delays == [] and not message.terminated
