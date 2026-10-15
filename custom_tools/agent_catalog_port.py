"""Narrow in-process port for tool access to agent catalog metadata.

The port keeps tool implementations independent of the catalog hydrator. A
runtime composition root installs the lookup after the catalog module finishes
loading, which prevents the former catalog/tool import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol


@dataclass(frozen=True)
class AgentTypeCatalogSummary:
    agent_type: str
    display_name: str
    role: str
    description: str
    source_paths: tuple[str, ...]
    default_tools: tuple[str, ...]
    runtime_config: dict[str, Any]


class AgentCatalogLookup(Protocol):
    def __call__(
        self,
        additional_paths: Iterable[str | Path],
        *,
        agent_type: str,
        include_configured: bool,
    ) -> AgentTypeCatalogSummary | None: ...


_catalog_lookup: AgentCatalogLookup | None = None


def configure_agent_catalog_lookup(lookup: AgentCatalogLookup) -> None:
    """Install the catalog-side adapter during runtime composition."""
    global _catalog_lookup
    _catalog_lookup = lookup


def lookup_agent_type(
    additional_paths: Iterable[str | Path],
    *,
    agent_type: str,
    include_configured: bool = True,
) -> AgentTypeCatalogSummary | None:
    if _catalog_lookup is None:
        raise RuntimeError(
            "Agent catalog lookup is not configured; load the runtime agent catalog "
            "before invoking catalog-dependent tools."
        )
    return _catalog_lookup(
        additional_paths,
        agent_type=agent_type,
        include_configured=include_configured,
    )
