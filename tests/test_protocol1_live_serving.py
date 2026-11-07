"""How a protocol 1 host serves an address, sends requests and reads records, without a NATS server.

custom_transport.protocol1.live, .replay and .state_records; the message contract's
README, "Replies" ("Answering only the requester's inbox"), "Refused requests",
"Delivery, expiry and idempotency" and "State records". The same paths on a real
server are in tests/test_live_protocol1_trust_kernel.py.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nats.errors import TimeoutError as NatsTimeoutError

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import Receiver, reply_audience, sign_frame, verify_frame
from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS
from custom_messages.wire.models import Frame
from custom_messages.wire.refusals import REFUSAL_HEADER
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1 import live
from custom_transport.protocol1.live import LiveHost, ServiceUnavailable
from custom_transport.protocol1.replay import LiveReplayRecord, accepted_until
from custom_transport.protocol1.state_records import StateRecords
from custom_transport.protocol1.trust_state import TRUST_STATE_FILENAME, PersistentTrustStore
from tests import message_contract_support as fixtures
from tests.test_protocol1_trust_kernel import example_bundle, example_identity

TEAM = "team_example"
AUDIENCE = {"installation": "install_example", "team": TEAM, "target": "team_services", "id": TEAM}
RECEIVER = Receiver("install_example", TEAM, "team_services", TEAM, "team-services-1")
WORKER_INBOX = "_INBOX.worker-host-1.r1"


class FakeConnection:
    """What a host does with its NATS connection: the subscriptions it makes and what it publishes."""

    def __init__(self, runtime: str) -> None:
        self.inbox_prefix = f"_INBOX.{runtime}"
        self.callbacks: dict[str, Any] = {}
        self.published: list[tuple[str, bytes, dict[str, str]]] = []
        self.request = AsyncMock()

    def new_inbox(self) -> str:
        return f"{self.inbox_prefix}.inbox1"

    async def subscribe(self, subject: str, cb: Any) -> Any:
        self.callbacks[subject] = cb
        return SimpleNamespace(subject=subject)

    async def publish(self, subject: str, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.published.append((subject, data, dict(headers or {})))


class Clock:
    def __init__(self) -> None:
        self.now = fixtures.NOW

    def __call__(self):
        return self.now


def host(runtime: str, key: str, epoch: int, clock: Any = None) -> LiveHost:
    return LiveHost(FakeConnection(runtime), example_identity(runtime, key, epoch),
                    PersistentTrustStore(fixtures.trust_store(), Path("/nonexistent")),
                    clock=clock or (lambda: fixtures.NOW))


def worker() -> LiveHost:
    return host("worker-host-1", "test-worker-key-1", 3)


def services(clock: Any = None) -> LiveHost:
    return host("team-services-1", "test-services-key-1", 2, clock)


def close_request(sender: LiveHost, request_id: str = "close_1", *, reply: str = WORKER_INBOX,
                  data: bytes | None = None) -> SimpleNamespace:
    frame = sender.sign("sessions.close", {"request_id": request_id, "session": "s-researcher-2-1", "reason": "exited"},
                        AUDIENCE, team=TEAM)
    encoded, headers = sender.encode(frame)
    return SimpleNamespace(data=encoded if data is None else data, headers=headers, subject=frame["subject"], reply=reply)


CLOSED = {"outcome": "ok", "ended_at": "2026-01-01T00:00:00.000Z"}


async def serving(receiver_host: LiveHost, handler: Any, **options: Any) -> Any:
    """Serve the team services' address; returns the callback NATS would call for each message."""

    await receiver_host.serve("svc.team", RECEIVER, handler, **options)
    (callback,) = receiver_host.nc.callbacks.values()
    return callback


# -- answering only the requester's inbox --------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    "$JS.API.STREAM.DELETE.UA1_WORK",
    "$KV.UA1_CERTS.revocations.install_example",
    "_INBOX.lead-host-1.r1",  # another host's inbox
    "_INBOX.worker-host-1",  # no token after the slot
    "_INBOX.worker-host-1.*",
])
@pytest.mark.parametrize("content", ["valid", "garbage"])
async def test_a_request_naming_another_reply_subject_is_dropped_before_its_handler_runs(caplog, reply, content) -> None:
    receiver_host = services()
    handled: list[Any] = []

    async def handler(verified):
        handled.append(verified)
        return CLOSED

    callback = await serving(receiver_host, handler)
    await callback(close_request(worker(), reply=reply, data=b"not a frame" if content == "garbage" else None))
    await receiver_host.drain()
    assert receiver_host.nc.published == [] and handled == []
    assert "protocol1_reply_subject_refused" in caplog.text


@pytest.mark.asyncio
async def test_a_request_from_the_senders_inbox_is_answered_there_and_a_refusal_too() -> None:
    receiver_host = services()
    callback = await serving(receiver_host, AsyncMock(return_value=CLOSED))
    await callback(close_request(worker()))
    await callback(close_request(worker(), "close_2", data=b"not a frame"))
    await receiver_host.drain()
    # The refusal goes out as the request arrives; the reply once its handler ran.
    (refused, data, refusal), (answered, _, headers) = receiver_host.nc.published
    assert answered == WORKER_INBOX and REFUSAL_HEADER not in headers
    assert refused == WORKER_INBOX and data == b"" and refusal[REFUSAL_HEADER] == "malformed"


@pytest.mark.asyncio
async def test_a_host_never_sends_requests_on_a_connection_with_another_reply_inbox() -> None:
    sender = worker()
    sender.nc.inbox_prefix = "_INBOX.someone-else"
    with pytest.raises(RuntimeError, match="not this host's own"):
        await sender.request("sessions.close", {"request_id": "close_1", "session": "s-researcher-2-1",
                                                "reason": "exited"}, AUDIENCE, team=TEAM)
    sender.nc.request.assert_not_awaited()


# -- handling requests ----------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("side_by_side", [False, True])
async def test_requests_run_in_order_unless_their_kind_may_run_side_by_side(side_by_side: bool) -> None:
    receiver_host = services()
    started: list[str] = []
    release = asyncio.Event()

    async def handler(verified):
        started.append(verified.body.request_id)
        await release.wait()
        return CLOSED

    callback = await serving(receiver_host, handler, side_by_side_kinds={"sessions.close"} if side_by_side else ())
    for request_id in ("close_1", "close_2", "close_3"):
        await callback(close_request(worker(), request_id))
    await asyncio.sleep(0.05)
    # The subscription kept reading while the first handler waited.
    assert started == (["close_1", "close_2", "close_3"] if side_by_side else ["close_1"])
    release.set()
    await receiver_host.drain()
    assert started == ["close_1", "close_2", "close_3"] and len(receiver_host.nc.published) == 3


@pytest.mark.asyncio
async def test_a_misspelt_side_by_side_kind_is_refused_when_serving_starts() -> None:
    with pytest.raises(ValueError, match="not a kind"):
        await services().serve("svc.team", RECEIVER, AsyncMock(), side_by_side_kinds={"sessions.clsoe"})


@pytest.mark.asyncio
async def test_an_address_holding_too_many_requests_answers_handler_failed_at_once(monkeypatch, caplog) -> None:
    monkeypatch.setattr(live, "MAX_OPEN_REQUESTS", 1)
    receiver_host = services()
    release = asyncio.Event()

    async def handler(verified):
        await release.wait()
        return CLOSED

    callback = await serving(receiver_host, handler)
    await callback(close_request(worker(), "close_1"))
    await callback(close_request(worker(), "close_2"))
    assert [headers.get(REFUSAL_HEADER) for _, _, headers in receiver_host.nc.published] == ["handler_failed"]
    assert "protocol1_address_busy" in caplog.text
    release.set()
    await receiver_host.drain()
    assert len(receiver_host.nc.published) == 2


@pytest.mark.asyncio
async def test_a_request_that_waited_past_its_expiry_is_not_handled(caplog) -> None:
    clock = Clock()
    receiver_host = services(clock)
    handled: list[str] = []
    release = asyncio.Event()

    async def handler(verified):
        handled.append(verified.body.request_id)
        await release.wait()
        return CLOSED

    callback = await serving(receiver_host, handler)
    await callback(close_request(worker(), "close_1"))
    await callback(close_request(worker(), "close_2"))
    while not handled:
        await asyncio.sleep(0)  # the first handler has started; the second waits behind it
    clock.now = fixtures.NOW + timedelta(minutes=5)
    release.set()
    await receiver_host.drain()
    assert handled == ["close_1"] and "protocol1_request_ended_waiting" in caplog.text


@pytest.mark.asyncio
async def test_a_check_that_fails_unexpectedly_still_ends_the_requesters_wait(monkeypatch, caplog) -> None:
    def broken(*_args, **_options):
        raise RuntimeError("defect")

    monkeypatch.setattr(live, "verify_frame", broken)
    receiver_host = services()
    callback = await serving(receiver_host, AsyncMock(return_value=CLOSED))
    await callback(close_request(worker()))
    assert [headers[REFUSAL_HEADER] for _, _, headers in receiver_host.nc.published] == ["handler_failed"]
    assert "protocol1_check_failed" in caplog.text


@pytest.mark.asyncio
async def test_a_frame_with_a_far_expiry_is_answered_and_kept_only_as_long_as_its_kind_lasts() -> None:
    """A sender cannot keep a replay entry by naming a far expires_at; it used to overflow the record and go unanswered."""

    sender, receiver_host = worker(), services()
    message = close_request(sender)
    frame = json.loads(message.data)
    frame["expires_at"] = "9999-12-31T23:59:59.999Z"
    message.data = json.dumps(sign_frame({key: value for key, value in frame.items() if key != "signature"},
                                         fixtures.private_key("test-worker-key-1"))).encode()
    callback = await serving(receiver_host, AsyncMock(return_value=CLOSED))
    await callback(message)
    await receiver_host.drain()
    (answered, _, headers), = receiver_host.nc.published
    assert answered == WORKER_INBOX and REFUSAL_HEADER not in headers
    kind_lasts = timedelta(seconds=live.KINDS["sessions.close"].expiry_seconds + CLOCK_SKEW_SECONDS)
    assert accepted_until(Frame.model_validate(frame)) == fixtures.NOW + kind_lasts


def test_the_replay_record_lets_go_of_frames_that_can_be_accepted_no_more() -> None:
    record, sender = LiveReplayRecord(), worker()
    for request_id, now in (("close_1", fixtures.NOW), ("close_2", fixtures.NOW)):
        message = close_request(sender, request_id)
        verified = verify_frame(message.data, trust=fixtures.trust_store(), receiver=RECEIVER, arrived_on=message.subject,
                                now=now, replay=record.lookup)
        record.admit(verified, now)
    assert len(record) == 2
    later = LiveHost(sender.nc, sender.identity, sender.trust, clock=lambda: fixtures.NOW + timedelta(minutes=10))
    message = close_request(later, "close_3")
    verified = verify_frame(message.data, trust=fixtures.trust_store(), receiver=RECEIVER, arrived_on=message.subject,
                            now=fixtures.NOW + timedelta(minutes=10), replay=record.lookup)
    record.admit(verified, fixtures.NOW + timedelta(minutes=10))
    assert len(record) == 1


# -- requesting -----------------------------------------------------------------------------------------

def answering(receiver_host: LiveHost, *, tamper: bool = False):
    """A request/reply exchange where ``receiver_host`` signs the reply; ``tamper`` changes it after signing."""

    async def exchange(subject: str, data: bytes, timeout: float, headers: dict[str, str]) -> Any:
        request = Frame.model_validate(json.loads(data))
        reply = receiver_host.sign("sessions.close.reply", CLOSED, reply_audience(request), team=TEAM,
                                   subject=WORKER_INBOX, answering=request)
        if tamper:
            reply["body"]["ended_at"] = format_timestamp(fixtures.NOW)
        encoded, reply_headers = receiver_host.encode(reply)
        return SimpleNamespace(data=encoded, headers=reply_headers, subject=reply["subject"])

    return exchange


@pytest.mark.asyncio
async def test_a_reply_is_used_only_after_it_verifies() -> None:
    body = {"request_id": "close_1", "session": "s-researcher-2-1", "reason": "exited"}
    sender = worker()
    sender.nc.request.side_effect = answering(services())
    assert (await sender.request("sessions.close", body, AUDIENCE, team=TEAM)).body.outcome == "ok"
    forged = worker()
    forged.nc.request.side_effect = answering(services(), tamper=True)
    with pytest.raises(FrameRefused) as refused:
        await forged.request("sessions.close", body, AUDIENCE, team=TEAM)
    assert refused.value.code == "bad_signature"


@pytest.mark.asyncio
async def test_no_answer_in_time_is_service_unavailable() -> None:
    sender = worker()
    sender.nc.request.side_effect = NatsTimeoutError
    with pytest.raises(ServiceUnavailable) as unavailable:
        await sender.request("sessions.close", {"request_id": "close_1", "session": "s-researcher-2-1",
                                                "reason": "exited"}, AUDIENCE, team=TEAM, timeout=0.1)
    assert unavailable.value.reason == "no answer in time"


# -- signed records ----------------------------------------------------------------------------------------

def bucket_holding(value: bytes, revision: int = 7) -> Any:
    return SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(value=value, revision=revision)))


@pytest.mark.asyncio
async def test_a_report_is_read_only_under_its_own_key_and_only_as_signed() -> None:
    record = (fixtures.EXAMPLES / "state" / "valid" / "agent" / "lead-busy.json").read_bytes()
    reader = worker()
    js = SimpleNamespace(key_value=AsyncMock(return_value=bucket_holding(record)))
    records = StateRecords(js, lambda: reader.identity, reader.trust)
    assert (await records.read_report("agent", "team_example.lead", now=fixtures.NOW)).revision == 7
    with pytest.raises(FrameRefused) as refused:
        await records.read_report("agent", "team_example.researcher-2", now=fixtures.NOW)
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/key")
    tampered = json.loads(record)
    tampered["sequence"] += 1
    js.key_value.return_value = bucket_holding(json.dumps(tampered).encode())
    with pytest.raises(FrameRefused) as refused:
        await records.read_report("agent", "team_example.lead", now=fixtures.NOW)
    assert refused.value.code == "bad_signature"


@pytest.mark.asyncio
async def test_a_trust_record_read_from_its_bucket_is_kept_on_disk(tmp_path: Path) -> None:
    reader = worker()
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    revocations = fixtures.current_revocations(fixtures.NOW, sequence=500)
    js = SimpleNamespace(key_value=AsyncMock(return_value=bucket_holding(revocations)))
    records = StateRecords(js, lambda: reader.identity, trust)
    assert await records.take_trust_record("revocations", "revocations.install_example", now=fixtures.NOW)
    kept = json.loads((tmp_path / TRUST_STATE_FILENAME).read_text())["revocations"]["install_example"]
    assert [record["sequence"] for record in kept] == [500]


@pytest.mark.asyncio
async def test_a_watch_reports_a_deleted_key_as_none() -> None:
    class Watcher:
        def __init__(self) -> None:
            self.entries = [SimpleNamespace(key="team_example.lead", value=b"{}"), None,
                            SimpleNamespace(key="team_example.lead", value=b"")]
            self.stop = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.entries:
                raise StopAsyncIteration
            return self.entries.pop(0)

    watcher = Watcher()
    reader = worker()
    js = SimpleNamespace(key_value=AsyncMock(return_value=SimpleNamespace(watch=AsyncMock(return_value=watcher))))
    seen = [change async for change in StateRecords(js, lambda: reader.identity, reader.trust).watch("agent", team=TEAM)]
    assert seen == [("team_example.lead", b"{}"), ("team_example.lead", None)]
    watcher.stop.assert_awaited_once()


def test_records_are_signed_with_the_identity_the_host_has_when_it_signs() -> None:
    """After a renewal the host's frames and records switch certificates together."""

    identities = [example_identity("worker-host-1", "test-worker-key-1", 3),
                  example_identity("team-services-1", "test-services-key-1", 2)]
    current = {"identity": identities[0]}
    records = StateRecords(None, lambda: current["identity"], PersistentTrustStore(fixtures.trust_store(), Path("/x")))
    body = json.loads((fixtures.EXAMPLES / "state" / "valid" / "agent" / "lead-busy.json").read_text())["body"]
    first = records.signed("agent", "team_example.lead", body, sequence=1, issued_at=fixtures.NOW)
    current["identity"] = identities[1]
    second = records.signed("agent", "team_example.lead", body, sequence=2, issued_at=fixtures.NOW)
    assert (first["writer"]["runtime"], second["writer"]["runtime"]) == ("worker-host-1", "team-services-1")
