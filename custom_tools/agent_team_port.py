"""Narrow port exposing the team's current agent definitions to tools.

The team runtime installs a source that reads the definition catalogs on each
call, so a specialist created while the runtime runs is listed at once, the
same way launches resolve it (``LocalAgentTeamRuntime.resolve_definition``).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping


_agent_type_source: Callable[[], Mapping[str, dict[str, Any]]] | None = None


def configure_agent_type_source(
    source: Callable[[], Mapping[str, dict[str, Any]]],
) -> None:
    """Install the team runtime's definition source during composition."""
    global _agent_type_source
    _agent_type_source = source


def current_agent_type_map() -> Mapping[str, dict[str, Any]]:
    """Every agent type the team can start now, read from the definition catalogs."""
    if _agent_type_source is None:
        raise RuntimeError(
            "Agent types are not configured; initialize the Strands team "
            "runtime before invoking team-dependent tools."
        )
    return _agent_type_source()
