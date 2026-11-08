"""The parts of a team's hosts that are not healthy, as readers take them from the host records (plan 13B.5).

An agent whose session was refused has no record of its own; its host lists it among the
components of its host record (custom_agents.agent_host), and readers of the team
(custom_transport.protocol1.team_agents) pass those on for the gateway's Status page.
tests/test_live_agent_records.py reads the host records from real hosts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pydantic import MISSING

from custom_messages.wire.encoding import format_timestamp
from custom_transport.protocol1.team_agents import HostComponent, TeamAgents, _Held

TEAM = "team_one"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)


def _host_record(components, *, teams=(TEAM,), expires_in: float = 20.0, body: bool = True) -> _Held:
    items = [SimpleNamespace(component=name, state=state, code=code) for name, state, code in components]
    record = SimpleNamespace(body=SimpleNamespace(components=items) if body else None,
                             signer=SimpleNamespace(teams=frozenset(teams)),
                             document=SimpleNamespace(expires_at=format_timestamp(NOW + timedelta(seconds=expires_in))))
    return _Held(1, "digest", record)  # type: ignore[arg-type]


def test_readers_pass_on_the_components_live_hosts_of_the_team_list_as_not_healthy() -> None:
    reader = TeamAgents(SimpleNamespace(clock=lambda: NOW), TEAM)
    reader._hosts.update({
        "lead": _host_record([("Researcher-agent_1", "unhealthy", "session_limit_reached"),
                              ("Analyst-agent_2", "degraded", MISSING), ("other", "healthy", MISSING)]),
        "agent-Writer": _host_record([("Writer-agent_3", "unhealthy", "session_not_allowed")], expires_in=-1),
        "elsewhere": _host_record([("x-agent_4", "unhealthy", "session_limit_reached")], teams=("team_two",)),
        "older-contract": _host_record([], body=False),
        "gateway": _host_record([]),
    })
    assert reader.host_components() == [
        HostComponent("lead", "Researcher-agent_1", "unhealthy", "session_limit_reached"),
        HostComponent("lead", "Analyst-agent_2", "degraded", None),
    ]
