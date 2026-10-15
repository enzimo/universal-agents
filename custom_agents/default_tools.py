"""Default tools: the tool catalog ids an agent definition says a type works with.

An agent definition lists ``default_tools`` as tool catalog ids or tool set
ids (``ua_definitions/access/tools``). They grant nothing:

- with authorization off (decision D8) their functions, next to the role's
  standard tools, are the agent's tools (``custom_agents.agent_catalog``);
- with it on the agent starts with its role's standard tools and its tool
  list follows its grants (``custom_agents.tool_sync``); the defaults tell
  TeamLead what a new type should be given (F9) and appear in its description.

This module depends only on the catalog, so the agent catalog, team
descriptions and specialist provisioning can share it without import cycles.
Default tools name built-in catalog tools and tool sets only; an instance's
own web API tools and MCP connections are given with grants, so a definition
validates the same way in every process.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache

from access_catalog.catalog import ToolCatalog, load_catalog

# What default tools mean, for the agent types TeamLead reads about
# (get_available_agent_types and the agent-instantiation skill).
DEFAULT_TOOLS_WITH_AUTHORIZATION = (
    "Access follows the work: the agent starts with its role's standard tools, and "
    "each task it is given gets a default tool when it needs it, from what TeamLead "
    "may hand on for the person the task works for (TeamLead asks for that with "
    "request_access and hand_on=true); otherwise the agent asks with request_access. "
    "Access is usable on its next model call, with no restart."
)
DEFAULT_TOOLS_WITHOUT_AUTHORIZATION = (
    "Authorization is off: the agent has these tools and its role's standard tools."
)


@lru_cache(maxsize=1)
def builtin_tool_catalog() -> ToolCatalog:
    """The shipped tool catalog; its files change only with a deploy."""

    return load_catalog(None)


def default_tool_ids(agent_type: str, default_tools: Iterable[str], catalog: ToolCatalog) -> set[str]:
    """The catalog tool ids a definition's default tools name (tool sets expanded).

    An id that is neither a catalog tool nor a tool set is refused, naming it.
    """

    tool_ids: set[str] = set()
    for entry in default_tools:
        if entry in catalog.tools:
            tool_ids.add(entry)
        elif entry in catalog.tool_sets:
            tool_ids |= {tool.tool_id for tool in catalog.tools_in_set(entry)}
        else:
            raise ValueError(
                f"Agent {agent_type} lists unknown default tool {entry!r}. default_tools takes tool "
                "catalog ids (for example fn:publishing or code:python) or tool set ids (for example "
                "google_workspace); find_tools and ua_definitions/access/tools list them."
            )
    return tool_ids


def default_tool_labels(default_tools: Iterable[str]) -> list[str]:
    """``id (display name)`` for each default tool, from the shipped tool catalog."""

    catalog = builtin_tool_catalog()
    labels: list[str] = []
    for entry in default_tools:
        item = catalog.tools.get(entry) or catalog.tool_sets.get(entry)
        labels.append(f"{entry} ({item.display_name})" if item is not None else entry)
    return labels


__all__ = [
    "DEFAULT_TOOLS_WITHOUT_AUTHORIZATION",
    "DEFAULT_TOOLS_WITH_AUTHORIZATION",
    "builtin_tool_catalog",
    "default_tool_ids",
    "default_tool_labels",
]
