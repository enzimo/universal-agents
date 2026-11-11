"""The live view's handling of records it cannot verify yet (custom_transport.protocol1.team_agents).

The view itself runs over NATS in tests/test_live_agent_records.py, and with real signatures
in tests/test_team_agents_verification.py; here a stand-in host's trust store refuses a
record first and accepts it later, deterministically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.team_agents import TeamAgents

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
KEY = "team_one.lead_1"


class _Trust:
    def __init__(self, refusals: list[str]) -> None:
        self.refusals = list(refusals)
        self.calls = 0

    def verify(self, raw, *, record, now, held):
        self.calls += 1
        if self.refusals:
            raise FrameRefused(self.refusals.pop(0), at="/writer/key_id")
        return SimpleNamespace(document=SimpleNamespace(sequence=1, key=KEY), digest="d1",
                               signer=SimpleNamespace(runtime="team-services"), other_contract=False,
                               body=SimpleNamespace(team="team_one", host="lead", host_epoch=1))


def _view(refusals: list[str]) -> tuple[TeamAgents, _Trust]:
    trust = _Trust(refusals)

    async def fetch_trust(raw):
        return False  # the writer's certificate is not readable yet

    host = SimpleNamespace(clock=lambda: NOW, live=SimpleNamespace(trust=SimpleNamespace(store=trust)),
                           fetch_trust=fetch_trust, identity=SimpleNamespace(installation="ua-test"))
    return TeamAgents(host, "team_one", logger=logging.getLogger("test.team_agents")), trust


async def _arrives(view: TeamAgents, raw: bytes) -> None:
    """What the placement watch does with a value it reads."""

    view._pending[("placement", KEY)] = raw
    await view._accept("placement", KEY, raw, view._placements)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["unknown_key", "revocations_not_current", "not_yet_valid"])
async def test_a_record_refused_for_a_passing_reason_is_verified_again_until_it_passes(caplog, code) -> None:
    view, trust = _view([code, code])
    with caplog.at_level(logging.INFO):
        await _arrives(view, b"{}")
        await view._retry_pending()  # still refused: kept, and not logged again
        assert "lead_1" not in view._placements
        await view._retry_pending()

    assert "lead_1" in view._placements and view._pending == {}
    assert caplog.text.count("event=team_agents_record_refused") == 1 and f"code={code}" in caplog.text


@pytest.mark.asyncio
async def test_a_record_refused_for_good_is_dropped(caplog) -> None:
    view, trust = _view(["bad_signature"])
    with caplog.at_level(logging.WARNING):
        await _arrives(view, b"{}")
    await view._retry_pending()

    assert view._pending == {} and trust.calls == 1 and "lead_1" not in view._placements
    assert "code=bad_signature" in caplog.text
