"""This process's message host, as the agents it runs reach it: asking another agent, a team event, the team's view.

The host itself (``custom_agents.agent_host.AgentProcessHost``) imports the handlers it
serves, and those handlers ask other agents; keeping what they call here, in a module that
imports neither, keeps the runtime's import graph free of loops
(``tests/test_runtime_architecture_boundaries.py``). ``strands_main.py`` sets the host
with ``configure_process_host``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

_PROCESS_HOST: Optional[Any] = None


class AgentUnreachable(Exception):
    """This process cannot reach other agents now: it has no host, has not joined, or the agent has no session."""


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


async def ask_agent(agent: Any, kind: str, body: Mapping[str, Any], target_agent: str, *,
                    timeout: Optional[float] = None) -> dict[str, Any]:
    """Ask another agent, signed as ``agent`` (``AgentProcessHost.ask``)."""

    host = process_host()
    if host is None:
        raise AgentUnreachable("this process runs no protocol 1 host (started without the launcher)")
    return await host.ask(kind, body, target_agent, sender_agent=str(agent.id), timeout=timeout)


async def announce(agent: Any, kind: str, body: Mapping[str, Any]) -> None:
    """Send a team event, signed as ``agent`` (``AgentProcessHost.announce``)."""

    host = process_host()
    if host is None:
        raise AgentUnreachable("this process runs no protocol 1 host (started without the launcher)")
    await host.announce(kind, body, sender_agent=str(agent.id))
