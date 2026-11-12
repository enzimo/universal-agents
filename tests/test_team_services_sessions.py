"""Session service decisions that need no NATS server (the live cases are in test_live_team_services.py)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from custom_messages.wire.refusals import FrameRefused
from team_services.sessions import AGENT_SESSION, SessionService, session_key

TEAM, INSTALLATION = "team_example", "install_example"
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
BAD_KEY = session_key(INSTALLATION, TEAM, "s-lead-1")


class _Records:
    """The bucket reads and writes ``SessionService`` uses: no placements, one session record that does not verify."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []

    async def keys(self, record: str, prefix: str, *, team: Optional[str] = None) -> list[str]:
        return [BAD_KEY] if record == AGENT_SESSION and BAD_KEY.startswith(prefix) else []

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


def _open(agent: str, agent_type: str) -> SimpleNamespace:
    certificate = SimpleNamespace(runtime="lead-host", agent_types=frozenset({"TeamArchitect", "researcher"}),
                                  roles=("lead_host", "worker_host"), session_limit=4, membership_epoch=1)
    return SimpleNamespace(certificate=certificate, frame=SimpleNamespace(issuer=SimpleNamespace(team=TEAM)),
                           raw={"body": {"agent": agent, "agent_type": agent_type, "definition_sha256": "0" * 64}})


@pytest.mark.asyncio
async def test_an_unverified_session_record_refuses_a_lead_by_key_and_leaves_workers_alone(caplog) -> None:
    records = _Records()
    trust = SimpleNamespace(add=lambda *_args, **_kwargs: True,
                            store=SimpleNamespace(agent_session=lambda *_args: None, contract_sha256="0" * 64))
    service = SessionService(records, trust, INSTALLATION, lambda: NOW)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="team_services.sessions"):
        refused = await service.open(_open("lead", "TeamArchitect"))  # type: ignore[arg-type]
    assert refused["outcome"] == "runtime_error"
    assert f"{BAD_KEY} (invalid_body)" in refused["message"]
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
