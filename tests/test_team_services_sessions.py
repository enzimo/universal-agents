"""Session service decisions that need no NATS server (the live cases are in test_live_team_services.py)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from custom_messages.wire.models import SessionsOpenReplyAdapter
from custom_messages.wire.refusals import FrameRefused
from team_services.sessions import AGENT_SESSION, SessionService, session_key

TEAM, INSTALLATION = "team_example", "install_example"
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
BAD_KEY = session_key(INSTALLATION, TEAM, "s-lead-1")
# The longest ids the contract allows: installation 62, team 24 and session 64 characters.
LONG_INSTALLATION, LONG_TEAM = "i" * 62, "t" * 24


class _Records:
    """The bucket reads and writes ``SessionService`` uses: no placements, session records that do not verify."""

    def __init__(self, bad_keys: list[str]) -> None:
        self.bad_keys = bad_keys
        self.puts: list[tuple[str, str]] = []

    async def keys(self, record: str, prefix: str, *, team: Optional[str] = None) -> list[str]:
        return [key for key in self.bad_keys if key.startswith(prefix)] if record == AGENT_SESSION else []

    async def take_trust_record(self, record: str, key: str, *, now: datetime) -> bool:
        raise FrameRefused("invalid_body", "a trust record is stored under another key", at="/key")

    async def read_report(self, record: str, key: str, *, now: datetime) -> None:
        return None

    async def revision(self, record: str, key: str) -> int:
        return 0

    def signed(self, record: str, key: str, body: dict[str, Any], *, sequence: int, issued_at: datetime) -> dict:
        return {"key": key, "body": body}

    async def put(self, record: str, key: str, document: dict, *, last_revision: Optional[int] = None) -> int:
        self.puts.append((record, key))
        return len(self.puts)


def _service(records: _Records, installation: str = INSTALLATION) -> SessionService:
    trust = SimpleNamespace(add=lambda *_args, **_kwargs: True,
                            store=SimpleNamespace(agent_session=lambda *_args: None, contract_sha256="0" * 64))
    return SessionService(records, trust, installation, lambda: NOW)  # type: ignore[arg-type]


def _open(agent: str, agent_type: str, team: str = TEAM) -> SimpleNamespace:
    certificate = SimpleNamespace(runtime="lead-host", agent_types=frozenset({"TeamArchitect", "researcher"}),
                                  roles=("lead_host", "worker_host"), session_limit=4, membership_epoch=1)
    return SimpleNamespace(certificate=certificate, frame=SimpleNamespace(issuer=SimpleNamespace(team=team)),
                           raw={"body": {"agent": agent, "agent_type": agent_type, "definition_sha256": "0" * 64}})


def _sent_refusal(reply: dict[str, Any]) -> dict[str, Any]:
    """The refusal as the host would send it: it must fit the reply schema, or the host answers ``handler_failed``."""

    assert reply["outcome"] == "runtime_error"
    SessionsOpenReplyAdapter.validate_python(reply)
    return reply


@pytest.mark.asyncio
async def test_an_unverified_session_record_refuses_a_lead_by_key_and_leaves_workers_alone(caplog) -> None:
    records = _Records([BAD_KEY])
    service = _service(records)

    with caplog.at_level(logging.WARNING, logger="team_services.sessions"):
        refused = _sent_refusal(await service.open(_open("lead", "TeamArchitect")))  # type: ignore[arg-type]
    assert f"{BAD_KEY} (invalid_body)." in refused["message"]  # one record: nothing counted after it
    assert records.puts == []  # nothing placed or signed
    lines = [record.getMessage() for record in caplog.records]
    assert any("event=lead_session_record_unreadable" in line and f"key={BAD_KEY} code=invalid_body" in line
               for line in lines)
    assert any("event=agent_session_refused kind=sessions.open outcome=runtime_error" in line for line in lines)
    # On this path the record blocks; it is not reported as counting as absent.
    assert not any("event=team_services_record_refused" in line for line in lines)

    worker = await service.open(_open("researcher-1", "researcher"))  # type: ignore[arg-type]
    assert worker["outcome"] == "ok"
    assert worker["session"]["body"]["agent_role"] == "worker"


@pytest.mark.asyncio
async def test_many_unverified_session_records_with_the_longest_ids_still_fit_the_lead_refusal(caplog) -> None:
    keys = [session_key(LONG_INSTALLATION, LONG_TEAM, f"s-{letter * 32}-{'9' * 29}") for letter in "abcdef"]
    assert all(len(key.rsplit(".", 1)[1]) == 64 for key in keys)  # session ids at their longest
    records = _Records(keys)

    with caplog.at_level(logging.ERROR, logger="team_services.sessions"):
        reply = await _service(records, LONG_INSTALLATION).open(_open("lead", "TeamArchitect", LONG_TEAM))  # type: ignore[arg-type]
    message = _sent_refusal(reply)["message"]
    assert f"{keys[0]} (invalid_body) and 5 more, each logged as event=lead_session_record_unreadable." in message
    assert message.endswith("deletes it from the UA1_CERTS bucket.")
    assert records.puts == []
    # The log still names every key, one line each.
    logged = [record.getMessage() for record in caplog.records
              if "event=lead_session_record_unreadable" in record.getMessage()]
    assert all(any(f"key={key} code=invalid_body" in line for line in logged) for key in keys)


@pytest.mark.asyncio
async def test_a_session_key_longer_than_any_id_is_cut_so_the_lead_refusal_still_fits() -> None:
    # Any process with the team's NATS login can write a key under the prefix; keys are not checked ids.
    long_key = session_key(INSTALLATION, TEAM, "s-" + "x" * 600)
    reply = await _service(_Records([long_key, BAD_KEY])).open(_open("lead", "TeamArchitect"))  # type: ignore[arg-type]
    message = _sent_refusal(reply)["message"]
    assert len(message) == 500
    assert message.startswith(f"Stored session records do not verify: {long_key[:100]}")
    assert "… and 1 more, each logged as event=lead_session_record_unreadable." in message
    assert message.endswith("deletes it from the UA1_CERTS bucket.")
