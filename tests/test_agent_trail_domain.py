"""Web Chat's agent trail rules (assistant_gateway.domain.agent_trail): pure, so every count is checked here."""

from __future__ import annotations

import pytest

from assistant_gateway.domain.agent_trail import (
    EMPTY_TRAIL,
    PERSON,
    TRAIL_LIMIT,
    TRAIL_REQUESTS_READ,
    AgentTrailUnreadable,
    ThreadTrail,
    TrailAgent,
    TrailEntry,
    decode_request_agents,
    decode_thread_trail,
    encode_request_agents,
    encode_thread_trail,
    thread_trail,
    with_agent,
)

LEAD = TrailAgent("TeamArchitect", lead=True)
WEB = TrailAgent("WebResearchAgent")
MEM = TrailAgent("MemReviewAgent")


def _trail(requests: list[tuple[TrailAgent, ...]]) -> ThreadTrail:
    """The trail as the store builds it: oldest request first in ``requests``."""

    return thread_trail(list(reversed(requests[-TRAIL_REQUESTS_READ:])), requests)


def _shape(trail: ThreadTrail) -> list[str]:
    return ["P" if entry.is_person else entry.agent.agent_type[0] for entry in trail.entries]


def test_no_recorded_request_gives_an_empty_trail() -> None:
    assert _trail([]) == EMPTY_TRAIL


def test_one_request_gives_the_person_then_its_agents_in_recorded_order() -> None:
    trail = _trail([(LEAD, WEB)])
    assert _shape(trail) == ["P", "T", "W"] and not trail.has_older
    assert trail.agents == (LEAD, WEB)


def test_six_entries_fit_without_older_ones() -> None:
    trail = _trail([(LEAD, WEB), (LEAD, MEM)])
    assert _shape(trail) == ["P", "T", "W", "P", "T", "M"] and not trail.has_older


def test_a_seventh_entry_drops_the_oldest_and_says_older_ones_exist() -> None:
    trail = _trail([(LEAD,), (LEAD,), (LEAD, WEB)])
    assert len(trail.entries) == TRAIL_LIMIT and trail.has_older
    assert _shape(trail) == ["T", "P", "T", "P", "T", "W"]


def test_twenty_turns_keep_the_last_six_and_every_agent_type_by_first_appearance() -> None:
    requests = [(LEAD,)] * 7 + [(LEAD, MEM), (LEAD, WEB)]
    trail = _trail(requests)
    assert sum(1 + len(agents) for agents in requests) == 20
    assert _shape(trail) == ["P", "T", "M", "P", "T", "W"] and trail.has_older
    assert trail.agents == (LEAD, MEM, WEB)


def test_a_request_with_six_or_more_agents_fills_the_row_with_its_own_agents() -> None:
    many = tuple(TrailAgent(f"Type{index}") for index in range(7))
    trail = _trail([(LEAD,), (LEAD, *many)])
    assert all(not entry.is_person for entry in trail.entries) and trail.has_older


def test_an_agent_type_is_recorded_once_per_request() -> None:
    assert with_agent((LEAD,), WEB) == (LEAD, WEB)
    assert with_agent((LEAD, WEB), TrailAgent("WebResearchAgent")) is None


def test_stored_forms_round_trip() -> None:
    assert decode_request_agents(encode_request_agents((LEAD, WEB))) == (LEAD, WEB)
    assert decode_request_agents(None) == ()
    trail = _trail([(LEAD, WEB), (LEAD,)])
    assert decode_thread_trail(encode_thread_trail(trail)) == trail
    assert decode_thread_trail(None) == EMPTY_TRAIL
    assert trail.entries[0] == PERSON and trail.entries[1] == TrailEntry(LEAD)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "{}",
        '[{"agent_type": ""}]',
        '[{"agent_type": "A", "lead": "yes"}]',
        '[{"agent_type": "A", "extra": 1}]',
        '[{"agent_type": "A"}, {"agent_type": "A"}]',
        '[{"agent_type": "' + "x" * 121 + '"}]',
    ],
)
def test_an_unreadable_request_agent_list_is_refused(text: str) -> None:
    with pytest.raises(AgentTrailUnreadable):
        decode_request_agents(text)


@pytest.mark.parametrize(
    "text",
    [
        '{"entries": [], "has_older": false}',
        '{"entries": [{"by": "robot"}], "has_older": false, "agents": []}',
        '{"entries": [{"by": "person", "agent_type": "A"}], "has_older": false, "agents": []}',
        '{"entries": [' + ",".join(['{"by": "person"}'] * 7) + '], "has_older": true, "agents": []}',
        '{"entries": [], "has_older": "no", "agents": []}',
    ],
)
def test_an_unreadable_thread_trail_is_refused(text: str) -> None:
    with pytest.raises(AgentTrailUnreadable):
        decode_thread_trail(text)
