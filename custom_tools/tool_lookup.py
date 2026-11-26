"""Trusted tool functions by name, for code the tool registry itself imports.

Why: ``custom_tools.registry`` imports every tool provider. Tools that need to
look other tools up by name (``describe_tools``, ``find_tools``, tool
scripts, the tool index and the on-request hook) would import the registry
back and close an import cycle (``tests/test_runtime_architecture_boundaries.py``).
The registry fills this table once, when it loads, and those tools read it
here. The registry is always loaded before an agent runs a tool, because the
agent's tool list is built from it (``custom_agents.tool_sync.provider_tools``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from custom_tools.provider_contract import tool_name

logger = logging.getLogger(__name__)

_TOOLS: dict[str, Any] = {}


def register_trusted_tools(tools: Iterable[Any]) -> None:
    """Called by ``custom_tools.registry`` with every trusted function tool."""

    _TOOLS.update({tool_name(tool): tool for tool in tools})


def trusted_tools() -> Mapping[str, Any]:
    """Every trusted function tool by name (empty, with an error logged, before the registry loaded)."""

    if not _TOOLS:
        logger.error(
            "Trusted tools were looked up before the tool registry loaded. event=tool_lookup_before_registry"
        )
    return _TOOLS


__all__ = ["register_trusted_tools", "trusted_tools"]
