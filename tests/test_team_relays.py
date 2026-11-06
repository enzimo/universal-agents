"""The team services relay a person's command only to an agent whose placement verifies (team_services.relays).

Every process shares one NATS login, so anything in ``UA1_PLACEMENT`` may be forged, stale or
malformed; tests/test_live_agent_hosts.py relays through real hosts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.state_records import StoredRecord
from team_services.relays import AGENT_UNAVAILABLE_TEXT, Relays

TEAM = "team_one"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)


def _placed(agent: str, agent_type: str) -> StoredRecord:
    return StoredRecord(3, SimpleNamespace(body=SimpleNamespace(agent=agent, agent_type=agent_type, host="lead")))  # type: ignore[arg-type]


def _relays(stored: dict[str, Any]) -> tuple[Relays, list[tuple[str, dict[str, Any]]]]:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def read_report(record: str, key: str, *, now: datetime) -> Optional[StoredRecord]:
        assert record == "placement"
        found = stored.get(key)
        if isinstance(found, Exception):
            raise found
        return found

    class _Bucket:
        async def keys(self) -> list[str]:
            return list(stored)

    class _JetStream:
        async def key_value(self, bucket: str) -> _Bucket:
            return _Bucket()

    class _Live:
        identity = SimpleNamespace(installation="install_one")

        async def request(self, kind: str, body: dict[str, Any], audience: dict[str, Any], **_: Any) -> Any:
            sent.append((audience["id"], body))
            return SimpleNamespace(raw={"body": {"outcome": "ok", "enabled": True}})

    host = SimpleNamespace(live=_Live(), records=SimpleNamespace(read_report=read_report), clock=lambda: NOW,
                           nc=SimpleNamespace(jetstream=lambda: _JetStream()))
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
    assert "event=team_relay_placement_unreadable key=team_one.broken" in caplog.text


@pytest.mark.asyncio
async def test_a_command_for_an_agent_whose_placement_does_not_verify_is_answered_plainly(caplog) -> None:
    relays, sent = _relays({f"{TEAM}.worker_1": FrameRefused("wrong_issuer", at="/writer")})
    with caplog.at_level(logging.WARNING):
        reply = await relays.handle(_frame("tasks.list", {"request_id": "r2", "agent": "worker_1", "offset": 0,
                                                          "limit": 20}))
    assert reply == {"outcome": "runtime_error", "message": AGENT_UNAVAILABLE_TEXT} and sent == []
    assert "code=wrong_issuer" in caplog.text
