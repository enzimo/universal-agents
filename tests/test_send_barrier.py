"""Nothing leaves an agent process before what it recorded is on the disk (decision 82).

``custom_transport.send_barrier`` and ``storage.sqlite_connection`` ("Flushing before
sending"): the NATS adapter awaits the installed barrier before every publish, dead-letter
copy, object-store upload and queued-message withdrawal, and before acknowledging or ending
a delivery. ``custom_agents.commit_flushes`` installs the SQLite flush as that barrier.
"""

from __future__ import annotations

import json
import logging
import threading
from types import SimpleNamespace

import pytest

from custom_agents import commit_flushes
from custom_transport import send_barrier
from custom_transport.base import TransportPublishOptions
from storage import sqlite_connection
from tests.test_nats_pull_delivery import _adapter, _valid_envelope_json


@pytest.fixture
def events(monkeypatch) -> list[str]:
    """What happened, in order: the barrier and each NATS call."""

    happened: list[str] = []

    async def barrier() -> None:
        happened.append("barrier")

    monkeypatch.setattr(send_barrier, "_BEFORE_SEND", barrier)
    return happened


class _JetStream:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def publish(self, subject, payload, headers=None):
        self.events.append("js.publish")

    async def delete_msg(self, stream, sequence):
        self.events.append("js.delete_msg")
        return True

    async def object_store(self, bucket):
        events = self.events

        class _Store:
            async def put(self, name, handle, meta=None):
                events.append("object.put")

        return _Store()


class _Core:
    max_payload = 1024 * 1024

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def publish(self, subject, body, headers=None):
        self.events.append("core.publish")


class _Message:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.data = json.dumps(_valid_envelope_json()).encode()
        self.subject = "team_t.agent.agent_a.work"
        self.headers = {}
        self.metadata = SimpleNamespace(num_delivered=1, sequence=SimpleNamespace(stream=7), num_pending=0)

    async def ack(self):
        self.events.append("msg.ack")

    async def term(self, reason=None):
        self.events.append("msg.term")

    async def nak(self, delay=None):
        self.events.append("msg.nak")


def _connected(events: list[str]):
    adapter = _adapter()
    adapter._js = _JetStream(events)
    adapter._nc = _Core(events)
    return adapter


def _envelope():
    from custom_messages.contracts.envelope_v1 import MessageEnvelope

    return MessageEnvelope.model_validate(_valid_envelope_json())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "call"),
    [
        (TransportPublishOptions(durable=True), "js.publish"),
        (TransportPublishOptions(durable=False), "core.publish"),
        (TransportPublishOptions(operational=True), "core.publish"),
    ],
)
async def test_every_publish_waits_for_the_barrier(events, options, call):
    adapter = _connected(events)

    await adapter.publish_subject(subject="team_t.agent.agent_b.work", envelope=_envelope(), options=options)

    assert events == ["barrier", call]


@pytest.mark.asyncio
async def test_settling_a_delivery_waits_for_the_barrier_except_returning_it(events):
    adapter = _connected(events)

    await adapter._delivery_from_msg(_Message(events), logical_route="agent_a").ack()
    await adapter._delivery_from_msg(_Message(events), logical_route="agent_a").term("done")
    # A returned message comes back for another try; nothing it recorded is relied on.
    await adapter._delivery_from_msg(_Message(events), logical_route="agent_a").nak()

    assert events == ["barrier", "msg.ack", "barrier", "msg.term", "msg.nak"]


@pytest.mark.asyncio
async def test_withdrawals_uploads_and_set_aside_copies_wait_for_the_barrier(events, tmp_path):
    adapter = _connected(events)
    upload = tmp_path / "file.bin"
    upload.write_bytes(b"content")

    await adapter.delete_work_message(5)
    await adapter.put_object(bucket="b", object_name="o", path=str(upload))
    await adapter._publish_dead_letter("team_t.dead_letter.x", b"{}", headers=None)

    assert events == ["barrier", "js.delete_msg", "barrier", "object.put", "barrier", "js.publish"]


@pytest.mark.asyncio
async def test_acknowledging_a_reported_set_aside_message_waits_for_the_barrier(events):
    from custom_transport.dead_letters import HEADER_COPY_ID

    adapter = _connected(events)
    message = _Message(events)
    message.headers = {HEADER_COPY_ID: "copy-1"}

    async def fetch(batch, timeout=None):
        return [message]

    adapter._dead_letter_subscriptions[adapter.mapper.durable_name("dead_letter_watch")] = SimpleNamespace(
        fetch=fetch
    )

    async def consumer_info(stream, durable):
        return SimpleNamespace(num_pending=1, num_ack_pending=0)

    adapter._js.consumer_info = consumer_info
    [record] = await adapter.fetch_dead_letters(consumer_route="dead_letter_watch")
    await record.ack()

    assert events == ["barrier", "msg.ack"]


@pytest.mark.asyncio
async def test_without_a_barrier_the_process_sends_at_once(monkeypatch):
    monkeypatch.setattr(send_barrier, "_BEFORE_SEND", None)
    await send_barrier.before_send()  # the gateway, the broker: commits were flushed when made


def test_a_second_different_barrier_is_refused(monkeypatch):
    async def first() -> None: ...

    async def second() -> None: ...

    monkeypatch.setattr(send_barrier, "_BEFORE_SEND", None)
    send_barrier.install_before_send(first)
    send_barrier.install_before_send(first)
    with pytest.raises(RuntimeError):
        send_barrier.install_before_send(second)


# -- An agent process switches both halves on together ------------------------------------


@pytest.fixture
def fresh_process(monkeypatch):
    monkeypatch.setattr(send_barrier, "_BEFORE_SEND", None)
    monkeypatch.setattr(sqlite_connection, "_FLUSHES_DEFERRED", False)
    monkeypatch.setattr(sqlite_connection, "_UNFLUSHED", set())


def test_an_agent_process_flushes_before_sending(fresh_process):
    commit_flushes.flush_commits_before_sending()

    assert sqlite_connection.flushes_deferred()
    assert send_barrier._BEFORE_SEND is commit_flushes._flush_before_send


@pytest.mark.asyncio
async def test_the_flush_runs_off_the_event_loop_and_only_when_needed(fresh_process, monkeypatch):
    threads: list[str] = []
    monkeypatch.setattr(
        sqlite_connection, "flush_unflushed_commits", lambda **_: threads.append(threading.current_thread().name)
    )

    await commit_flushes._flush_before_send()
    assert threads == [], "nothing recorded since the last send: no flush"

    sqlite_connection._note_unflushed_commit("/instance/dbs/task_history.db")
    await commit_flushes._flush_before_send()
    assert len(threads) == 1 and threads[0] != threading.current_thread().name


def test_a_clean_exit_flushes_what_was_recorded_after_the_last_send(fresh_process, monkeypatch, caplog):
    flushed: list[bool] = []
    monkeypatch.setattr(sqlite_connection, "flush_unflushed_commits", lambda **_: flushed.append(True))
    sqlite_connection._note_unflushed_commit("/instance/dbs/task_history.db")

    commit_flushes.flush_commits_at_exit(logging.getLogger("test.exit"))
    assert flushed == [True]

    def failing(**_):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(sqlite_connection, "flush_unflushed_commits", failing)
    with caplog.at_level(logging.WARNING, logger="test.exit"):
        commit_flushes.flush_commits_at_exit(logging.getLogger("test.exit"))
    assert "event=sqlite_flush_at_exit_failed" in caplog.text
