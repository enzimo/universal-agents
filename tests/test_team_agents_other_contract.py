"""Readers of a team's records and a host on another message contract (custom_transport.protocol1.team_agents).

Contract README "Versions": a host record that names another contract digest is authentic,
so the reader keeps its host's agents, for the control messages about the tasks they hold,
but sends them no new work and places no agent there. The records here are stand-ins for
verified ones; tests/test_live_agent_records.py reads real ones.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pydantic import MISSING

from custom_agents.agent_host import agent_record_state
from custom_messages.wire.encoding import format_timestamp
from custom_transport.protocol1.team_agents import TeamAgents, _Held

TEAM = "team_one"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
OURS, THEIRS = "1" * 64, "2" * 64


def _reader() -> TeamAgents:
    def session(installation, team, name):
        agent = name.removeprefix("s-").removesuffix("-1")
        return SimpleNamespace(agent=agent, runtime=f"agent-{agent.upper()}", agent_type="Researcher",
                               agent_role="worker", covers=lambda now: True, ends_at=NOW + timedelta(days=1))

    host = SimpleNamespace(clock=lambda: NOW, identity=SimpleNamespace(installation="installation_one"),
                           live=SimpleNamespace(trust=SimpleNamespace(
                               store=SimpleNamespace(contract_sha256=OURS, agent_session=session))))
    return TeamAgents(host, TEAM, logger=logging.getLogger("test.team_agents"))


def _signer(runtime: str, epoch: int = 2) -> SimpleNamespace:
    return SimpleNamespace(runtime=runtime, membership_epoch=epoch, teams=frozenset({TEAM}))


def _host_record(runtime: str, *, theirs: str | None) -> _Held:
    """A host record as ``TrustStore.verify`` returns it; one of another contract has no body."""

    document = SimpleNamespace(expires_at=format_timestamp(NOW + timedelta(seconds=20)), sequence=1)
    body = None if theirs else SimpleNamespace(certificate=SimpleNamespace(membership_epoch=2), components=[])
    record = SimpleNamespace(document=document, body=body, signer=_signer(runtime), other_contract=theirs is not None,
                             their_contract_sha256=theirs)
    return _Held(1, "digest", record)  # type: ignore[arg-type]


def _agent_record(agent: str, runtime: str) -> _Held:
    body = SimpleNamespace(state="idle", session=f"s-{agent}-1", agent_type="Researcher", display_name=MISSING,
                           accepting_work=True, since=format_timestamp(NOW))
    return _Held(1, "digest", SimpleNamespace(body=body, signer=_signer(runtime), document=SimpleNamespace(
        expires_at=format_timestamp(NOW + timedelta(minutes=3)))))  # type: ignore[arg-type]


def _placement(runtime: str) -> _Held:
    return _Held(1, "digest", SimpleNamespace(body=SimpleNamespace(host=runtime, host_epoch=2,
                                                                 agent_type="Researcher")))  # type: ignore[arg-type]


def test_an_agent_on_a_host_of_another_contract_stays_in_the_view_and_takes_no_new_work(caplog) -> None:
    reader = _reader()
    reader._hosts.update({"agent-A": _host_record("agent-A", theirs=None), "agent-B": _host_record("agent-B", theirs=THEIRS)})
    reader._agents.update({"a": _agent_record("a", "agent-A"), "b": _agent_record("b", "agent-B")})
    reader._placements.update({"a": _placement("agent-A"), "b": _placement("agent-B")})

    reader._rebuild()  # read the host record's body before: AttributeError, and the whole view stopped

    a, b = reader.agent("a"), reader.agent("b")
    assert a is not None and a.other_contract is None and a.takes_work
    # Still there, so control messages about what it holds reach it; no new work goes to it.
    assert b is not None and b.other_contract == THEIRS and b.accepting_work and not b.takes_work
    assert reader.other_contract_hosts() == {"agent-B": THEIRS}
    assert reader.host_components() == []

    with caplog.at_level(logging.WARNING):
        for _ in range(3):  # every record the host writes, every 10 seconds
            reader._log_other_contract("agent-B", THEIRS)
    logged = [record.getMessage() for record in caplog.records
              if "event=team_agents_host_other_contract" in record.getMessage()]
    assert len(logged) == 1 and THEIRS in logged[0] and OURS in logged[0] and "host=agent-B" in logged[0]


def test_a_host_record_of_another_contract_that_expired_lists_nothing() -> None:
    reader = _reader()
    held = _host_record("agent-B", theirs=THEIRS)
    held.record.document.expires_at = format_timestamp(NOW - timedelta(seconds=1))
    reader._hosts["agent-B"] = held
    assert reader.other_contract_hosts() == {}


def test_a_draining_agents_record_says_it_takes_no_work() -> None:
    agent = SimpleNamespace(paused=False, is_executing=True)
    assert agent_record_state(agent) == ("busy", True)
    agent._contract_drain = SimpleNamespace(holds_back_unstarted=False)
    assert agent_record_state(agent) == ("busy", False)
