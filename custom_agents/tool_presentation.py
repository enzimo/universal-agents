"""How one agent's tools are shown to its model (``agent.tool_presentation``).

The values and their meaning are in ``runtime_settings.tool_presentation_contract``.
The setting travels with the operator's other tool settings in the agent's
``tool_filter`` (``custom_agents.strands_team``). This module answers the two
questions the rest of the runtime asks:

- which catalog tools wait to be loaded for this agent (``on_request_groups``):
  the catalog's ``on_request`` list with ``full_definitions``; with an index,
  every catalog tool that has functions, except the catalog's ``always_shown``
  tools. Their kept-shown functions (``on_request`` ``keep_shown``) stay
  shown in every setting;
- whether the agent may run tool scripts (``scripts_enabled``), and which
  functions it is never shown without them (``withheld_functions``).

The groups an agent's definition names in ``default_tools`` stay shown in
every setting (``custom_agents.tool_loading.hidden_tool_ids``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from runtime_settings.tool_presentation_contract import (
    DEFAULT_TOOL_PRESENTATION,
    TOOL_PRESENTATION_FULL_DEFINITIONS,
    TOOL_PRESENTATION_INDEX_AND_SCRIPTS,
    TOOL_PRESENTATIONS,
)

logger = logging.getLogger(__name__)

# Built per catalog object and setting: the catalog is replaced, never changed in place, when it reloads.
# Each entry keeps its catalog alive, so a new catalog can never reuse a cached id.
_GROUPS_CACHE: dict[tuple[int, str], tuple[Any, Mapping[str, frozenset[str]]]] = {}


def presentation(agent: Any) -> str:
    """The agent's tool presentation; an unknown value is logged and treated as the default."""

    value = str((getattr(agent, "tool_filter", None) or {}).get("tool_presentation") or DEFAULT_TOOL_PRESENTATION)
    if value in TOOL_PRESENTATIONS:
        return value
    # Configuration validates the value, so this means a caller built the agent by hand.
    logger.warning(
        "An agent has an unknown tool presentation; its tools are shown in full. "
        "event=tool_presentation_unknown agent_id=%s value=%s",
        getattr(agent, "id", None),
        value,
    )
    return DEFAULT_TOOL_PRESENTATION


def index_enabled(agent: Any) -> bool:
    """Whether the agent's held tools beyond its core wait in a one-line index."""

    return presentation(agent) != TOOL_PRESENTATION_FULL_DEFINITIONS


def scripts_enabled(agent: Any) -> bool:
    """Whether the agent may call its read-level tools from ``run_tool_script``."""

    return presentation(agent) == TOOL_PRESENTATION_INDEX_AND_SCRIPTS


TOOL_SCRIPTS_TOOL_ID = "fn:tool_scripts"
CALL_TOOL_NAME = "call_tool"


def withheld_functions(agent: Any, catalog: Any) -> frozenset[str]:
    """Functions never shown to this agent for its tool presentation setting.

    - ``call_tool`` is shown only when the tool index is on (``index`` or
      ``index_and_scripts``); ``full_definitions`` withholds it.
    - ``fn:tool_scripts`` functions (``run_tool_script``) are shown only when
      tool scripts are on (``index_and_scripts``).

    Every agent holds ``fn:tool_loading`` and ``fn:tool_scripts`` by its role,
    so the setting, not a grant, decides whether the model sees them; both
    ``CallToolHook`` and ``run_tool_script`` also refuse when their mode is off.
    """

    withheld: set[str] = set()
    if not index_enabled(agent):
        withheld.add(CALL_TOOL_NAME)
    if not scripts_enabled(agent):
        withheld.update(
            name for name, binding in catalog.functions.items() if binding.tool_id == TOOL_SCRIPTS_TOOL_ID
        )
    return frozenset(withheld)


def on_request_groups(agent: Any, catalog: Any) -> Mapping[str, frozenset[str]]:
    """Catalog tool id -> its kept-shown functions, for every tool that waits to be loaded for this agent."""

    configured: Mapping[str, frozenset[str]] = getattr(catalog, "on_request", {}) or {}
    mode = presentation(agent)
    if mode == TOOL_PRESENTATION_FULL_DEFINITIONS:
        return configured
    key = (id(catalog), mode)
    cached = _GROUPS_CACHE.get(key)
    if cached is not None and cached[0] is catalog:
        return cached[1]
    always_shown = frozenset(getattr(catalog, "always_shown", ()) or ())
    with_functions = {binding.tool_id for binding in catalog.functions.values()}
    groups = {
        tool_id: configured.get(tool_id, frozenset())
        for tool_id in sorted(with_functions - always_shown)
    }
    if len(_GROUPS_CACHE) > 16:  # catalogs reload rarely; keep only recent ones
        _GROUPS_CACHE.clear()
    _GROUPS_CACHE[key] = (catalog, groups)
    return groups


__all__ = [
    "CALL_TOOL_NAME",
    "index_enabled",
    "on_request_groups",
    "presentation",
    "scripts_enabled",
    "withheld_functions",
]
