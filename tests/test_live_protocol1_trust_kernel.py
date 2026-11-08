"""The protocol 1 trust kernel on a real NATS server: signed requests and replies, refusals, records.

custom_transport.protocol1.live and .state_records; the message contract's README,
"Replies", "Refused requests", "Agent sessions" and "State records". The server here
has no per-host permissions; tests/test_live_message_contract.py checks those.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

import nats
import pytest
from nats.js.errors import KeyWrongLastSequenceError

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import Receiver, VerifiedFrame
from custom_messages.wire.refusals import REFUSAL_AT_HEADER, REFUSAL_HEADER
from custom_transport.protocol1.host_keys import HostKeys
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.live import LiveHost, RequestRefused, ServiceUnavailable, Unsupported
from custom_transport.protocol1.state_records import StateRecords
from custom_transport.protocol1.trust_state import PersistentTrustStore
from tests import message_contract_support as fixtures
from tests.own_nats_server import own_nats_server
from tests.test_live_message_contract import nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
TEAM = "team_example"
SERVICES_RECEIVER = Receiver("install_example", TEAM, "team_services", TEAM, "team-services-1")
TO_SERVICES = {"installation": "install_example", "team": TEAM, "target": "team_services", "id": TEAM}


@pytest.fixture
def broker(tmp_path: Path):
    with own_nats_server(nats_server(), tmp_path) as url:
        yield url


def identity(runtime: str, key_id: str, epoch: int, sessions: dict[str, str] | None = None) -> HostIdentity:
    certificate = fixtures.trust_store().host_certificate("install_example", runtime, epoch)
    assert certificate is not None
    keys = HostKeys(runtime=runtime, key_id=key_id, application_key=fixtures.private_key(key_id), nats_seed=bytes(32))
    return HostIdentity(keys, certificate, sessions or {})


async def host(stack: AsyncExitStack, url: str, who: HostIdentity) -> LiveHost:
    """A host connected with its own reply inbox prefix, checking with the examples' trust at their time."""

    connection = await nats.connect(url, inbox_prefix=f"_INBOX.{who.runtime}".encode(), allow_reconnect=False)
    stack.push_async_callback(connection.close)
    trust = PersistentTrustStore(fixtures.trust_store(), Path("/nonexistent"), {}, {})
    return LiveHost(connection, who, trust, clock=lambda: fixtures.NOW)


def session_body(agent: str, session: str) -> dict[str, Any]:
    return {"session": session, "installation": "install_example", "team": TEAM, "agent": agent,
            "agent_type": "researcher", "agent_role": "worker", "runtime": "worker-host-1", "placement_revision": 14,
            "started_at": format_timestamp(fixtures.NOW), "ends_at": format_timestamp(fixtures.NOW + timedelta(days=1))}


@pytest.mark.asyncio
async def test_a_host_gets_a_session_and_then_signs_for_its_agent(broker: str) -> None:
    async with AsyncExitStack() as stack:
        services = await host(stack, broker, identity("team-services-1", "test-services-key-1", 2))
        worker = await host(stack, broker, identity("worker-host-1", "test-worker-key-1", 3))
        records = StateRecords(None, lambda: services.identity, services.trust)

        async def answer(verified: VerifiedFrame) -> dict[str, Any]:
            if verified.frame.kind != "sessions.open":
                raise Unsupported(verified.frame.kind)
            body = verified.body
            document = records.signed("agent_session", f"session.install_example.{TEAM}.s-{body.agent}-2",
                                      session_body(body.agent, f"s-{body.agent}-2"), sequence=1, issued_at=fixtures.NOW)
            return {"outcome": "ok", "session": document}

        await services.serve("svc.team", SERVICES_RECEIVER, answer)
        await services.nc.flush()
        reply = await worker.request("sessions.open", {
            "request_id": "open_researcher_3", "agent": "researcher-3", "agent_type": "researcher",
            "definition_sha256": "DQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0"}, TO_SERVICES, team=TEAM)
        assert reply.frame.audience.target == "host" and reply.frame.audience.id == "worker-host-1"
        session = reply.raw["body"]["session"]
        assert worker.trust.add(json.dumps(session).encode(), now=fixtures.NOW)
        worker.identity = worker.identity.with_session("researcher-3", "s-researcher-3-2")
        # The identity can now sign for researcher-3, but session lifecycle requests
        # belong to its host. Holding a session does not let an agent renew it.
        assert services.trust.add(json.dumps(session).encode(), now=fixtures.NOW)
        progress = worker.sign("sessions.renew", {"request_id": "renew_1", "session": "s-researcher-3-2"}, TO_SERVICES,
                               team=TEAM, agent="researcher-3")
        assert progress["issuer"]["session"] == "s-researcher-3-2"
        with pytest.raises(RequestRefused) as refused:
            await worker.request("sessions.renew", {"request_id": "renew_2", "session": "s-researcher-3-2"}, TO_SERVICES,
                                 team=TEAM, agent="researcher-3")
        assert refused.value.code == "wrong_issuer"
        # The host-signed request passes verification and reaches the handler,
        # which serves only sessions.open in this test.
        with pytest.raises(RequestRefused) as host_refused:
            await worker.request("sessions.renew", {"request_id": "renew_3", "session": "s-researcher-3-2"},
                                 TO_SERVICES, team=TEAM)
        assert host_refused.value.code == "unsupported"


@pytest.mark.asyncio
async def test_refused_failed_and_unserved_requests_end_the_wait_at_once(broker: str) -> None:
    async with AsyncExitStack() as stack:
        services = await host(stack, broker, identity("team-services-1", "test-services-key-1", 2))
        worker = await host(stack, broker, identity("worker-host-1", "test-worker-key-1", 3))

        async def fail(_verified: VerifiedFrame) -> dict[str, Any]:
            raise RuntimeError("the store is down")

        # Nobody serves the address yet: "no responders", not a 30-second wait.
        started = time.monotonic()
        with pytest.raises(ServiceUnavailable, match="no responders"):
            await worker.request("sessions.close", {"request_id": "close_1", "session": "s-researcher-2-1",
                                                    "reason": "exited"}, TO_SERVICES, team=TEAM)
        await services.serve("svc.team", SERVICES_RECEIVER, fail)
        await services.nc.flush()
        with pytest.raises(RequestRefused) as refused:
            await worker.request("sessions.close", {"request_id": "close_1", "session": "s-researcher-2-1",
                                                    "reason": "exited"}, TO_SERVICES, team=TEAM)
        assert refused.value.code == "handler_failed"
        # A reply the schema refuses is the server's defect; the requester still stops waiting.
        async def bad_reply(_verified: VerifiedFrame) -> dict[str, Any]:
            return {"outcome": "ok"}

        broken = await host(stack, broker, identity("team-services-1", "test-services-key-1", 2))
        await services.nc.drain()
        await broken.serve("svc.team", SERVICES_RECEIVER, bad_reply)
        await broken.nc.flush()
        with pytest.raises(RequestRefused) as refused:
            await worker.request("sessions.close", {"request_id": "close_5", "session": "s-researcher-2-1",
                                                    "reason": "exited"}, TO_SERVICES, team=TEAM)
        assert refused.value.code == "handler_failed"
        # A frame changed after signing is refused with its code and known pointer only; the
        # signature covers the body, so it fails before the body digest (registry order).
        frame = worker.sign("sessions.close", {"request_id": "close_2", "session": "s-researcher-2-1", "reason": "exited"},
                            TO_SERVICES, team=TEAM)
        frame["body"]["reason"] = "failed"
        data, headers = worker.encode(frame)
        answer = await worker.nc.request(frame["subject"], data, timeout=5, headers=headers)
        assert answer.data == b"" and answer.headers == {REFUSAL_HEADER: "bad_signature", REFUSAL_AT_HEADER: "/signature"}
        assert time.monotonic() - started < 10


@pytest.mark.asyncio
async def test_a_retried_request_is_answered_and_other_content_under_its_id_is_refused(broker: str) -> None:
    async with AsyncExitStack() as stack:
        services = await host(stack, broker, identity("team-services-1", "test-services-key-1", 2))
        worker = await host(stack, broker, identity("worker-host-1", "test-worker-key-1", 3))
        answered: list[str] = []

        async def close(verified: VerifiedFrame) -> dict[str, Any]:
            answered.append(verified.frame.message_id)
            return {"outcome": "ok", "ended_at": format_timestamp(fixtures.NOW)}

        await services.serve("svc.team", SERVICES_RECEIVER, close)
        await services.nc.flush()
        frame = worker.sign("sessions.close", {"request_id": "close_3", "session": "s-researcher-2-1", "reason": "exited"},
                            TO_SERVICES, team=TEAM)
        data, headers = worker.encode(frame)
        for _ in range(2):
            assert (await worker.nc.request(frame["subject"], data, timeout=5, headers=headers)).data
        changed = worker.sign("sessions.close", {"request_id": "close_4", "session": "s-researcher-2-1",
                                                 "reason": "failed"}, TO_SERVICES, team=TEAM)
        changed["message_id"] = frame["message_id"]
        from custom_messages.wire.frame import sign_frame

        changed = sign_frame(changed, worker.identity.keys.application_key)
        data, headers = worker.encode(changed)
        answer = await worker.nc.request(frame["subject"], data, timeout=5, headers=headers)
        assert answer.headers[REFUSAL_HEADER] == "replayed"
        assert answered == [frame["message_id"]] * 2


@pytest.mark.asyncio
async def test_signed_records_are_written_with_compare_and_set_and_verified_when_read(broker: str) -> None:
    async with AsyncExitStack() as stack:
        services = await host(stack, broker, identity("team-services-1", "test-services-key-1", 2))
        worker = await host(stack, broker, identity("worker-host-1", "test-worker-key-1", 3))
        js = services.nc.jetstream()
        for bucket in ("UA1_AGENTS", "UA1_PLACEMENT"):
            await js.create_key_value(bucket=bucket)
        placements = StateRecords(js, lambda: services.identity, services.trust)
        key = f"{TEAM}.researcher-2"
        body = {"team": TEAM, "agent": "researcher-2", "agent_type": "researcher", "host": "worker-host-1", "host_epoch": 3,
                "definition_sha256": "CwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCws", "placed_at": format_timestamp(fixtures.NOW),
                "reason": "initial"}
        first = await placements.put("placement", key, placements.signed("placement", key, body, sequence=1,
                                                                          issued_at=fixtures.NOW), last_revision=0)
        await placements.put("placement", key, placements.signed("placement", key, body, sequence=2,
                                                                 issued_at=fixtures.NOW), last_revision=first)
        with pytest.raises(KeyWrongLastSequenceError):
            await placements.put("placement", key, placements.signed("placement", key, body, sequence=3,
                                                                     issued_at=fixtures.NOW), last_revision=first)
        reader = StateRecords(worker.nc.jetstream(), lambda: worker.identity, worker.trust)
        stored = await reader.read_report("placement", key, now=fixtures.NOW)
        assert stored is not None and stored.record.document.sequence == 2
        # A host writes its own agent's state; a reader verifies who signed it.
        agents = StateRecords(worker.nc.jetstream(), lambda: worker.identity, worker.trust)
        state = {"team": TEAM, "agent": "researcher-2", "agent_type": "researcher", "session": "s-researcher-2-1",
                 "state": "idle", "accepting_work": True, "since": format_timestamp(fixtures.NOW)}
        await agents.put("agent", key, agents.signed("agent", key, state, sequence=1, issued_at=fixtures.NOW,
                                                   expires_at=fixtures.NOW + timedelta(seconds=180)))
        read = await StateRecords(js, lambda: services.identity, services.trust).read_report("agent", key, now=fixtures.NOW)
        assert read is not None and read.record.signer.runtime == "worker-host-1"
        assert await reader.read_report("agent", f"{TEAM}.nobody", now=fixtures.NOW) is None
        changes: list[tuple[str, bool]] = []

        async def first_change() -> None:
            async for changed_key, raw in agents.watch("agent", f"{TEAM}.>"):
                changes.append((changed_key, raw is not None))
                return

        await asyncio.wait_for(first_change(), timeout=5)
        assert changes == [(key, True)]


@pytest.mark.asyncio
async def test_the_transport_adapter_gives_a_host_its_own_reply_inbox(broker: str) -> None:
    """transport.yaml reply_inbox_prefix: replies to a host arrive under _INBOX.<runtime>."""

    from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
    from custom_transport.subjects import NatsSubjectMapper

    adapter = NatsTransportAdapter(config=NatsTransportConfig(servers=[broker], jetstream_enabled=False,
                                                              reply_inbox_runtime="worker-host-1"),
                                   mapper=NatsSubjectMapper(team_id=TEAM))
    await adapter.connect()
    try:
        assert adapter.nats_connection.new_inbox().startswith("_INBOX.worker-host-1.")
    finally:
        await adapter.close()
    with pytest.raises(RuntimeError, match="not connected"):
        adapter.nats_connection
