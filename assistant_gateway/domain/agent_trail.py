"""Web Chat's agent trail: who worked on a thread's requests, as a short row of triangles.

Each thread row shows one triangle per turn, oldest first: the person's turn
(a request that started work), then each agent type that worked on that
request, in the order the gateway first learned of it (the team lead first:
the gateway records it when it hands the request over). A row shows at most
``TRAIL_LIMIT`` triangles; ``has_older`` says the thread has more history,
which the page shows by drawing the leftmost triangle hollow and dotted
(owner's rule, 2026-11-20).

Where the facts come from: the agent types of one request are stored with the
request (``gateway_requests.agent_types_json``), learned from the agents' own
signed records through the gateway's request records only
(``assistant_gateway.application.request_agents_service``). A request with no
recorded agent (one from before this feature, or one never handed to the
team) is not part of the trail.

Why a stored copy per thread (``gateway_sessions.agent_trail_json``): open
pages read the thread list every two seconds, and that read may only use
primary-key and index lookups (``list_web_chat_threads``). The copy is a pure
function of the thread's requests (``thread_trail``) and is rebuilt from them
whenever a request's agents change and after history cleanup, so it can always
be recomputed and never shows deleted history.

This module is pure: the store and the service call it with what they read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# At most this many triangles per row, counting the ones working now (owner's rule, 2026-11-20).
TRAIL_LIMIT = 6
# Each recorded request gives at least one entry (the person's), so the newest
# TRAIL_LIMIT + 1 requests decide the last TRAIL_LIMIT entries and whether older ones exist.
TRAIL_REQUESTS_READ = TRAIL_LIMIT + 1
AGENT_TYPE_MAX_CHARACTERS = 120
_BY_PERSON = "person"
_BY_AGENT = "agent"


class AgentTrailUnreadable(ValueError):
    """A stored request agent list or thread trail is not in the form this module writes."""


@dataclass(frozen=True)
class TrailAgent:
    """An agent type that worked on a request; ``lead`` when it is the team lead."""

    agent_type: str
    lead: bool = False


@dataclass(frozen=True)
class TrailEntry:
    """One triangle: the person's turn (``agent`` is None) or an agent type's."""

    agent: TrailAgent | None = None

    @property
    def is_person(self) -> bool:
        return self.agent is None


PERSON = TrailEntry()


@dataclass(frozen=True)
class ThreadTrail:
    """A thread's last entries (oldest first), whether older ones exist, and every agent type that worked in it
    (first appearance first)."""

    entries: tuple[TrailEntry, ...] = ()
    has_older: bool = False
    agents: tuple[TrailAgent, ...] = ()


EMPTY_TRAIL = ThreadTrail()


def _agent(value: Any) -> TrailAgent:
    if not isinstance(value, dict) or set(value) - {"agent_type", "lead"}:
        raise AgentTrailUnreadable("an agent must be an object with agent_type and lead")
    agent_type, lead = value.get("agent_type"), value.get("lead", False)
    if not isinstance(agent_type, str) or not agent_type.strip() or len(agent_type) > AGENT_TYPE_MAX_CHARACTERS:
        raise AgentTrailUnreadable("an agent type must be a non-empty string of at most 120 characters")
    if not isinstance(lead, bool):
        raise AgentTrailUnreadable("lead must be true or false")
    return TrailAgent(agent_type, lead)


def trail_agent(agent_type: str, lead: bool) -> TrailAgent:
    """An agent to record, checked with the rule stored lists are read with (plan review C14).

    Raises ``AgentTrailUnreadable`` (a ``ValueError``) before anything is written, so the store never keeps a
    list it would then refuse to read, which would lose the request's agents for good.
    """

    return _agent({"agent_type": agent_type, "lead": lead})


def _agent_json(agent: TrailAgent) -> dict[str, Any]:
    return {"agent_type": agent.agent_type, "lead": agent.lead}


def decode_request_agents(text: str | None) -> tuple[TrailAgent, ...]:
    """The agent types stored with one request, in recorded order; none for ``None``."""

    if text is None:
        return ()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise AgentTrailUnreadable(f"not JSON: {error.msg}") from error
    if not isinstance(value, list):
        raise AgentTrailUnreadable("a request's agents must be a list")
    agents = tuple(_agent(item) for item in value)
    if len({agent.agent_type for agent in agents}) != len(agents):
        raise AgentTrailUnreadable("an agent type appears twice")
    return agents


def encode_request_agents(agents: Sequence[TrailAgent]) -> str:
    return json.dumps([_agent_json(agent) for agent in agents], separators=(",", ":"))


def with_agent(agents: Sequence[TrailAgent], agent: TrailAgent) -> tuple[TrailAgent, ...] | None:
    """``agents`` with ``agent`` added last, or None when its type is already there (recording is idempotent)."""

    if any(item.agent_type == agent.agent_type for item in agents):
        return None
    return (*agents, agent)


def thread_trail(
    newest_requests: Sequence[Sequence[TrailAgent]], all_requests: Iterable[Sequence[TrailAgent]]
) -> ThreadTrail:
    """The trail of a thread from its recorded requests (module docstring).

    ``newest_requests`` is the agent list of each of the thread's newest ``TRAIL_REQUESTS_READ`` recorded
    requests, newest first: fewer means that is all of them, and seven give at least seven entries, so more than
    ``TRAIL_LIMIT`` entries is exactly "older entries exist". ``all_requests`` is every recorded request's list,
    oldest request first, which orders the thread's agent types by first appearance.
    """

    entries: list[TrailEntry] = []
    for agents in reversed(newest_requests):
        entries.append(PERSON)
        entries.extend(TrailEntry(agent) for agent in agents)
    has_older = len(entries) > TRAIL_LIMIT
    seen: dict[str, TrailAgent] = {}
    for agents in all_requests:
        for agent in agents:
            seen.setdefault(agent.agent_type, agent)
    return ThreadTrail(tuple(entries[-TRAIL_LIMIT:]), has_older, tuple(seen.values()))


def encode_thread_trail(trail: ThreadTrail) -> str:
    return json.dumps(
        {
            "entries": [
                {"by": _BY_PERSON} if entry.agent is None else {"by": _BY_AGENT, **_agent_json(entry.agent)}
                for entry in trail.entries
            ],
            "has_older": trail.has_older,
            "agents": [_agent_json(agent) for agent in trail.agents],
        },
        separators=(",", ":"),
    )


def decode_thread_trail(text: str | None) -> ThreadTrail:
    """A thread's stored trail; the empty trail for ``None`` (no request recorded yet)."""

    if text is None:
        return EMPTY_TRAIL
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise AgentTrailUnreadable(f"not JSON: {error.msg}") from error
    if not isinstance(value, dict) or set(value) != {"entries", "has_older", "agents"}:
        raise AgentTrailUnreadable("a thread trail must have entries, has_older and agents")
    raw_entries, has_older, raw_agents = value["entries"], value["has_older"], value["agents"]
    if not isinstance(raw_entries, list) or len(raw_entries) > TRAIL_LIMIT or not isinstance(has_older, bool):
        raise AgentTrailUnreadable("a thread trail has at most six entries and a true or false has_older")
    if not isinstance(raw_agents, list):
        raise AgentTrailUnreadable("a thread trail's agents must be a list")
    return ThreadTrail(tuple(_entry(item) for item in raw_entries), has_older, tuple(_agent(item) for item in raw_agents))


def _entry(item: Any) -> TrailEntry:
    """One stored entry: the person's (nothing else in it) or an agent's."""

    if not isinstance(item, dict) or item.get("by") not in (_BY_PERSON, _BY_AGENT):
        raise AgentTrailUnreadable("each entry is the person's or an agent's")
    if item["by"] == _BY_AGENT:
        return TrailEntry(_agent({key: item[key] for key in item if key != "by"}))
    if set(item) != {"by"}:
        raise AgentTrailUnreadable("a person's entry carries nothing else")
    return PERSON


__all__ = [
    "AgentTrailUnreadable",
    "EMPTY_TRAIL",
    "PERSON",
    "TRAIL_LIMIT",
    "TRAIL_REQUESTS_READ",
    "ThreadTrail",
    "TrailAgent",
    "TrailEntry",
    "decode_request_agents",
    "decode_thread_trail",
    "encode_request_agents",
    "encode_thread_trail",
    "thread_trail",
    "trail_agent",
    "with_agent",
]
