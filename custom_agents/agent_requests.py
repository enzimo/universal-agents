"""This process's message host, as the agents it runs reach it: asking another agent, a team event, the team's view.

The host itself (``custom_agents.agent_host.AgentProcessHost``) imports the handlers it
serves, and those handlers ask other agents; keeping what they call here, in a module that
imports neither, keeps the runtime's import graph free of loops
(``tests/test_runtime_architecture_boundaries.py``). ``strands_main.py`` sets the host
with ``configure_process_host``.

"Cannot ask" is not "did not answer". When this process cannot sign a request for an
agent yet (it has not joined, the agent's session is not open, the team's view is not
read yet), callers that judge other agents by their answers wait instead of counting
everyone as silent (``cannot_ask``). Some reasons never clear by themselves (no message
host at all, a process on another message contract, an agent type no certificate can
carry), and callers treat those as nobody answering, as before. ``ask_each`` asks several
agents at once under one deadline and keeps each target's answer or error apart, so one
target's failure never loses another's answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Union

_PROCESS_HOST: Optional[Any] = None


class AgentUnreachable(Exception):
    """This process cannot reach other agents now: it has no host, has not joined, or the agent has no session.

    ``clears`` says whether the reason passes by itself (``AskBlocked``).
    """

    def __init__(self, reason: str, *, clears: bool = True) -> None:
        super().__init__(reason)
        self.clears = clears


@dataclass(frozen=True)
class AskBlocked:
    """Why this process cannot ask for an agent now, and whether that passes by itself (module docstring)."""

    reason: str
    clears: bool


@dataclass(frozen=True)
class SessionlessAgent:
    """An agent a live host runs without a session, as that host's record lists it.

    ``component`` is the host's own name for it (``<agent type>-<agent id>``, cut to 64
    characters, or ``more-agents`` standing for the rest); ``code`` and ``why`` say why it
    has no session. Such an agent answers no request, so nobody can ask what it runs.
    """

    host: str
    component: str
    code: str
    why: str


NO_HOST = AskBlocked("this process runs no message host (started without the launcher)", clears=False)
VIEW_NOT_READY = AskBlocked("this process has not read the team's agents yet", clears=True)


def configure_process_host(host: Optional[Any]) -> None:
    """Set this process's host (``strands_main.py``); ``None`` when the launcher gave it no host id."""

    global _PROCESS_HOST
    _PROCESS_HOST = host


def process_host() -> Optional[Any]:
    """This process's ``AgentProcessHost``, or None without the launcher."""

    return _PROCESS_HOST


def team_view() -> Optional[Any]:
    """This process's view of the team's live agents (``TeamAgents``), once it has been read; None before."""

    host = process_host()
    if host is None or host.team_agents is None or not host.team_agents.ready.is_set():
        return None
    return host.team_agents


def cannot_ask(agent: Any, *, need_view: bool) -> Optional[AskBlocked]:
    """Why ``agent`` cannot ask other agents now, or None when it can (module docstring).

    ``need_view`` when the caller picks its targets from the team's view.
    """

    host = process_host()
    if host is None:
        return NO_HOST
    blocked = host.cannot_ask(str(agent.id))
    if blocked is not None:
        return blocked
    if need_view and team_view() is None:
        return VIEW_NOT_READY
    return None


async def ask_agent(agent: Any, kind: str, body: Mapping[str, Any], target_agent: str, *,
                    timeout: Optional[float] = None) -> dict[str, Any]:
    """Ask another agent, signed as ``agent`` (``AgentProcessHost.ask``)."""

    host = process_host()
    if host is None:
        raise AgentUnreachable(NO_HOST.reason, clears=False)
    return await host.ask(kind, body, target_agent, sender_agent=str(agent.id), timeout=timeout)


async def ask_each(agent: Any, kind: str, body: Mapping[str, Any], targets: Iterable[str], *,
                   timeout: float) -> dict[str, Union[dict[str, Any], Exception]]:
    """Ask every target at once, all within ``timeout`` seconds; each target's reply body, or its error.

    Nothing is raised for a target: an ask still running at the deadline is cancelled and
    given ``ServiceUnavailable`` (no answer in time), and any other error is returned as it
    was raised, so the caller decides per target (module docstring). When the caller is
    cancelled meanwhile, every ask still running is cancelled and awaited before the
    cancellation goes on, so none outlives it.
    """

    from custom_transport.protocol1.live import NO_ANSWER_IN_TIME, ServiceUnavailable

    asks = {target: asyncio.ensure_future(ask_agent(agent, kind, body, target, timeout=timeout))
            for target in dict.fromkeys(targets)}
    if not asks:
        return {}
    try:
        await asyncio.wait(asks.values(), timeout=timeout)
    finally:
        late = [ask for ask in asks.values() if not ask.done()]
        for ask in late:
            ask.cancel()
        await asyncio.gather(*late, return_exceptions=True)
    results: dict[str, Union[dict[str, Any], Exception]] = {}
    for target, ask in asks.items():
        if ask.cancelled():
            results[target] = ServiceUnavailable(kind, target, NO_ANSWER_IN_TIME)
            continue
        error = ask.exception()
        results[target] = ask.result() if error is None else (
            error if isinstance(error, Exception) else RuntimeError(repr(error)))
    return results


async def agents_without_session() -> list[SessionlessAgent]:
    """The agents the team's live hosts run without a session (``AgentProcessHost.agents_without_session``)."""

    host = process_host()
    if host is None:
        return []
    return list(await host.agents_without_session())


async def announce(agent: Any, kind: str, body: Mapping[str, Any]) -> None:
    """Send a team event, signed as ``agent`` (``AgentProcessHost.announce``)."""

    host = process_host()
    if host is None:
        raise AgentUnreachable(NO_HOST.reason, clears=False)
    await host.announce(kind, body, sender_agent=str(agent.id))
