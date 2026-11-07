"""Protocol 1 cannot report agent state until its deferred commits are durable.

Exercise the real signing, receiving and record-writing methods. A held flush
must hold their NATS writes, and a failed flush must prevent those writes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.errors import NoRespondersError

from custom_messages.wire.frame import Receiver
from custom_transport import send_barrier
from custom_transport.protocol1.joining import HostProfile, join
from custom_transport.protocol1.live import LiveHost, ServiceUnavailable, Unsupported
from custom_transport.protocol1.state_records import StateRecords
from custom_transport.protocol1.trust_state import PersistentTrustStore
from tests import message_contract_support as fixtures
from tests.test_protocol1_trust_kernel import example_identity

TEAM = "team_example"
AUDIENCE = {"installation": "install_example", "team": TEAM, "target": "team_services", "id": TEAM}
RECEIVER = Receiver("install_example", TEAM, "team_services", TEAM, "team-services-1")


def live_host(connection, runtime, key, epoch):
    return LiveHost(connection, example_identity(runtime, key, epoch),
                    PersistentTrustStore(fixtures.trust_store(), Path("/nonexistent")),
                    clock=lambda: fixtures.NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [
    "request", "reply", "unsupported", "handler_failed", "bad_reply", "invalid_frame",
    "put", "create", "update", "join",
])
async def test_protocol_writes_wait_for_the_flush_and_stop_when_it_fails(monkeypatch, caplog, operation):
    entered, release = asyncio.Event(), asyncio.Event()
    fail = False

    async def barrier():
        entered.set()
        await release.wait()
        if fail:
            raise OSError("disk flush failed")

    monkeypatch.setattr(send_barrier, "_BEFORE_SEND", barrier)
    callbacks = {}

    async def subscribe(subject, cb):
        callbacks[subject] = cb

    connection = SimpleNamespace(request=AsyncMock(side_effect=NoRespondersError), publish=AsyncMock(),
                                 new_inbox=lambda: "_INBOX.worker-host-1.inbox1", subscribe=subscribe)
    worker = live_host(connection, "worker-host-1", "test-worker-key-1", 3)
    services = live_host(connection, "team-services-1", "test-services-key-1", 2)
    bucket = SimpleNamespace(put=AsyncMock(return_value=1), create=AsyncMock(return_value=1),
                             update=AsyncMock(return_value=2))
    js = SimpleNamespace(key_value=AsyncMock(return_value=bucket))
    records = StateRecords(js, lambda: services.identity, services.trust)
    body = {"request_id": "close_1", "session": "s-researcher-2-1", "reason": "exited"}
    frame = worker.sign("sessions.close", body, AUDIENCE, team=TEAM)
    data, headers = worker.encode(frame)
    message = SimpleNamespace(data=b"invalid" if operation == "invalid_frame" else data,
                              headers=headers, subject=frame["subject"], reply="_INBOX.worker-host-1.reply")

    async def handler(_verified):
        if operation == "unsupported":
            raise Unsupported()
        if operation == "handler_failed":
            raise RuntimeError("store failed")
        return {} if operation == "bad_reply" else {"outcome": "ok", "ended_at": "2026-01-01T00:00:00Z"}

    async def send():
        if operation in {"put", "create", "update"}:
            await records.put("agent", f"{TEAM}.researcher-2", {},
                              last_revision={"put": None, "create": 0, "update": 1}[operation])
        elif operation == "join":
            with pytest.raises(ServiceUnavailable):
                await join(connection, worker.identity.keys, worker.trust,
                           HostProfile("worker-host-1", (TEAM,), ("worker_host",), ("researcher",), 4),
                           clock=lambda: fixtures.NOW, deadline_seconds=0)
        elif operation == "request":
            with pytest.raises(ServiceUnavailable):
                await worker.request("sessions.close", body, AUDIENCE, team=TEAM)
        else:
            if not callbacks:
                await services.serve("svc.team", RECEIVER, handler)
            (answer,) = callbacks.values()
            await answer(message)
            await services.drain()

    writes = [connection.request, connection.publish, bucket.put, bucket.create, bucket.update]
    pending = asyncio.create_task(send())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not pending.done()
        assert sum(write.await_count for write in writes) == 0
    finally:
        release.set()
        await asyncio.wait_for(pending, timeout=2)
    assert sum(write.await_count for write in writes) == 1

    fail = True
    if operation in {"put", "create", "update", "request", "join"}:
        with pytest.raises(OSError, match="disk flush failed"):
            await send()
    else:
        # A served address logs the failed answer; the subscription keeps serving.
        message.data = message.data if operation == "invalid_frame" else worker.encode(
            worker.sign("sessions.close", {**body, "request_id": "close_2"}, AUDIENCE, team=TEAM))[0]
        await send()
        assert "protocol1_answer_failed" in caplog.text
    assert sum(write.await_count for write in writes) == 1


@pytest.mark.asyncio
async def test_team_bucket_bindings_follow_the_record_key_and_require_a_team_for_watches():
    js = SimpleNamespace(key_value=AsyncMock(return_value=SimpleNamespace()))
    worker = live_host(None, "worker-host-1", "test-worker-key-1", 3)
    records = StateRecords(js, lambda: worker.identity, worker.trust)

    for record, key, name in [
        ("team_view", "team_one.task.task1", "UA1_TEAM_VIEW_team_one"),
        ("team_view", "team_two.task.task1", "UA1_TEAM_VIEW_team_two"),
        ("block_list", "team_one", "UA1_BLOCK_LIST_team_one"),
        ("agent", "team_one.agent1", "UA1_AGENTS"),
    ]:
        await records._bucket(record, key)
        js.key_value.assert_awaited_with(name)
    await records._bucket("block_list", team="team_two")
    js.key_value.assert_awaited_with("UA1_BLOCK_LIST_team_two")
    calls = js.key_value.await_count
    with pytest.raises(ValueError, match="name the team"):
        await anext(records.watch("block_list"))
    assert js.key_value.await_count == calls

