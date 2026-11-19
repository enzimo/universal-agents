"""The team services relay a person's command only to an agent whose placement verifies (team_services.relays).

Every process shares one NATS login, so anything in ``UA1_PLACEMENT`` may be forged, stale or
malformed; tests/test_live_agent_hosts.py relays through real hosts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.state_records import StoredRecord
from custom_transport.protocol1.bucket_reads import BucketReadTimedOut
from team_services.relays import AGENT_UNAVAILABLE_TEXT, LEAD_LOOKUP_FAILED_TEXT, Relays

TEAM = "team_one"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)


def _placed(agent: str, agent_type: str) -> StoredRecord:
    return StoredRecord(3, SimpleNamespace(body=SimpleNamespace(agent=agent, agent_type=agent_type, host="lead", placed_at="2026-11-06T09:00:00.000Z")))  # type: ignore[arg-type]


def _relays(stored: dict[str, Any]) -> tuple[Relays, list[tuple[str, dict[str, Any]]]]:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def read_report(record: str, key: str, *, now: datetime) -> Optional[StoredRecord]:
        if record == "host":
            return StoredRecord(1, SimpleNamespace(other_contract=False, body=SimpleNamespace(state="accepting")))
        assert record == "placement"
        found = stored.get(key)
        if isinstance(found, Exception):
            raise found
        return found

    sessions = {key.rsplit(".", 1)[-1]: SimpleNamespace(
        agent=key.rsplit(".", 1)[-1], runtime="lead",
        agent_type="TeamArchitect",
        agent_role="worker" if key.endswith("worker_1") else "lead", started_at=NOW,
        ends_at=NOW + timedelta(days=1)) for key in stored}

    async def keys(record, prefix):
        return [prefix + key for key in sessions]

    async def take(record, key, **kwargs):
        return True

    class _Live:
        identity = SimpleNamespace(installation="install_one")
        trust = SimpleNamespace(store=SimpleNamespace(agent_session=lambda installation, team, session: sessions.get(session)))

        async def request(self, kind: str, body: dict[str, Any], audience: dict[str, Any], **_: Any) -> Any:
            sent.append((audience["id"], body))
            return SimpleNamespace(raw={"body": {"outcome": "ok", "enabled": True}})

    host = SimpleNamespace(live=_Live(), records=SimpleNamespace(read_report=read_report, keys=keys, take_trust_record=take), clock=lambda: NOW)
    return Relays(host, TEAM, "TeamArchitect", logging.getLogger("test.relays")), sent  # type: ignore[arg-type]


def _frame(kind: str, body: dict[str, Any]) -> Any:
    return SimpleNamespace(frame=SimpleNamespace(kind=kind), raw={"body": body, "authority": {"token": "t"}})


@pytest.mark.asyncio
async def test_the_leads_context_dump_setting_goes_only_to_the_lead_the_team_services_placed(caplog) -> None:
    relays, sent = _relays({
        f"{TEAM}.forged": FrameRefused("bad_signature", at="/signature"),  # names a worker as the lead
        f"{TEAM}.removed": None,  # removed between listing and reading
        f"{TEAM}.broken": RuntimeError("nats: timeout"),
        f"{TEAM}.worker_1": _placed("worker_1", "Researcher"),
        f"{TEAM}.lead_1": _placed("lead_1", "TeamArchitect"),
    })
    with caplog.at_level(logging.WARNING):
        reply = await relays.handle(_frame("debug.dump", {"request_id": "r1", "enabled": True}))
    assert reply == {"outcome": "ok", "enabled": True} and [agent for agent, _ in sent] == ["lead_1"]
    assert "event=team_services_record_refused record=placement key=team_one.forged code=bad_signature" in caplog.text
    assert "event=team_relay_placement_unreadable key=session.install_one.team_one.broken" in caplog.text


@pytest.mark.asyncio
async def test_a_command_for_an_agent_whose_placement_does_not_verify_is_answered_plainly(caplog) -> None:
    relays, sent = _relays({f"{TEAM}.worker_1": FrameRefused("wrong_issuer", at="/writer")})
    with caplog.at_level(logging.WARNING):
        reply = await relays.handle(_frame("tasks.list", {"request_id": "r2", "agent": "worker_1", "offset": 0,
                                                          "limit": 20}))
    assert reply == {"outcome": "runtime_error", "message": AGENT_UNAVAILABLE_TEXT} and sent == []
    assert "code=wrong_issuer" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [
    ("slow", "did not answer in time"),
    ("refused", "could not handle this request"),
    ("handler_failed", "may have finished"),
    ("invalid_reply", "may have finished"),
])
async def test_relay_failures_explain_the_outcome_without_protocol_codes(failure, expected):
    from custom_transport.protocol1.live import ServiceUnavailable, RequestRefused, NO_ANSWER_IN_TIME
    relays, sent = _relays({f"{TEAM}.worker_1": _placed("worker_1", "Researcher")})

    async def request(*args, **kwargs):
        assert kwargs["timeout"] < 15
        if failure == "slow":
            raise ServiceUnavailable("tasks.remove", "agent", NO_ANSWER_IN_TIME)
        if failure == "invalid_reply":
            raise FrameRefused("bad_signature", at="/signature")
        if failure == "handler_failed":
            raise RequestRefused("tasks.remove", "handler_failed", "/body")
        raise RequestRefused("tasks.remove", "wrong_authority", "/authority")

    relays._host.live.request = request
    reply = await relays.handle(_frame("tasks.remove", {"agent": "worker_1"}))
    assert reply["outcome"] == "runtime_error" and expected in reply["message"]
    assert "wrong_authority" not in reply["message"]


@pytest.mark.asyncio
async def test_a_failed_session_listing_is_answered_as_a_failed_lookup_not_as_no_lead(caplog) -> None:
    # The lead is placed and running; only listing the team's sessions fails.
    relays, sent = _relays({f"{TEAM}.lead_1": _placed("lead_1", "TeamArchitect")})

    async def keys(record, prefix):
        raise BucketReadTimedOut("UA1_CERTS", f"$KV.UA1_CERTS.{prefix}>", [f"{prefix}lead_1"], 0)

    relays._host.records.keys = keys
    with caplog.at_level(logging.WARNING):
        reply = await relays.handle(_frame("debug.dump", {"request_id": "r3", "enabled": True}))
    assert reply == {"outcome": "runtime_error", "message": LEAD_LOOKUP_FAILED_TEXT} and sent == []
    failed = [record for record in caplog.records if "event=team_relay_lead_lookup_failed" in record.getMessage()]
    assert len(failed) == 1 and "team=team_one" in failed[0].getMessage()
    assert failed[0].exc_info is not None and isinstance(failed[0].exc_info[1], BucketReadTimedOut)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing,expected", [
    ("session", LEAD_LOOKUP_FAILED_TEXT),  # the lead's session record could not be read
    ("placement", LEAD_LOOKUP_FAILED_TEXT),  # its placement could not be read
    ("forged", AGENT_UNAVAILABLE_TEXT),  # its placement does not verify: it counts as absent
])
async def test_a_lead_whose_records_could_not_be_read_is_not_answered_as_not_running(caplog, failing,
                                                                                     expected) -> None:
    # The listing works and names the lead; a read after it fails, so the lead may be running.
    placement: Any = _placed("lead_1", "TeamArchitect")
    if failing == "placement":
        placement = RuntimeError("nats: timeout")
    elif failing == "forged":
        placement = FrameRefused("bad_signature", at="/signature")
    relays, sent = _relays({f"{TEAM}.lead_1": placement})

    async def take(record, key, **kwargs):
        raise TimeoutError("nats: timeout")

    if failing == "session":
        relays._host.records.take_trust_record = take
    with caplog.at_level(logging.WARNING):
        reply = await relays.handle(_frame("debug.dump", {"request_id": "r4", "enabled": True}))
    assert reply == {"outcome": "runtime_error", "message": expected} and sent == []
    unreadable = "event=team_relay_placement_unreadable key=session.install_one.team_one.lead_1" in caplog.text
    assert unreadable == (failing != "forged")
