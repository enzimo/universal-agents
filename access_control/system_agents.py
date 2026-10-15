"""Which agent types are built-in system agents, read from the release definitions.

A system agent (``access_role: system``) runs fixed maintenance duties, often
for no person, under its role's standard access. Only definitions shipped with
the release may declare it (``custom_agents.agent_catalog`` refuses it in
team-written definitions), so the release files under ``ua_definitions`` are
the whole answer. Every access service reads the answer here, through
``entities.agent_role_resolver``, so the agents, the tool broker, the
gateway's Inbox and the operator commands cannot disagree about an agent's
role. This module reads the YAML directly instead of importing the agent
catalog, which pulls in the tool registry and is too heavy for the broker and
gateway.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

RELEASE_DEFINITION_ROOT = Path(__file__).resolve().parents[1] / "ua_definitions"
_CATALOG_FILE = RELEASE_DEFINITION_ROOT / "agent_definitions" / "builtins.yaml"
_BUNDLE_ROOT = RELEASE_DEFINITION_ROOT / "agent_bundles"
SYSTEM_ACCESS_ROLE = "system"


def _definition_files() -> list[Path]:
    files = [_CATALOG_FILE] if _CATALOG_FILE.is_file() else []
    if _BUNDLE_ROOT.is_dir():
        files.extend(sorted(path for path in _BUNDLE_ROOT.rglob("*") if path.suffix.lower() in {".yaml", ".yml"}))
    return files


def _system_types_in(path: Path) -> set[str]:
    try:
        document: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        # A broken release file would also stop the agent catalog; say which.
        logger.warning("system_agent_definition_unreadable path=%s error=%s", path, exc)
        return set()
    agent_types = document.get("agent_types") if isinstance(document, dict) else None
    if not isinstance(agent_types, dict):
        return set()
    return {
        str(name)
        for name, definition in agent_types.items()
        if isinstance(definition, dict) and str(definition.get("access_role") or "") == SYSTEM_ACCESS_ROLE
    }


@lru_cache(maxsize=1)
def builtin_system_agent_types() -> frozenset[str]:
    """The agent types the release definitions declare as system agents."""

    found: set[str] = set()
    for path in _definition_files():
        found |= _system_types_in(path)
    return frozenset(found)


__all__ = ["RELEASE_DEFINITION_ROOT", "SYSTEM_ACCESS_ROLE", "builtin_system_agent_types"]
