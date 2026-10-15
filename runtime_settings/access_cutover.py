"""Detect saved data written for credential bindings, which account links replaced.

Phase 3 of the unified access model removed credential bindings: a tool's
credential now comes from its account link (``AccountLinkV1``). Installed MCP
connection records saved by earlier versions still carry binding fields
(``descriptor.credential_binding_ids``, usually empty lists) that the current
models reject.

The runtime does not rewrite these files (project rule: no automatic
migration). Startup refuses while any remains and prints the exact commands,
as it does for the old workspace layout (``runtime_settings.legacy_layout``).

Approved attachments in ``TEAM_AGENTS.yaml`` and generated specialists that
name capability ids also carried binding fields; Phase 7 removed both, and
their loaders refuse them with the steps to take
(``custom_agents.team_composition``, ``custom_agents.agent_catalog``).
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime_settings.instance_layout import InstanceLayout

OBSOLETE_BINDING_DATA_PRESENT = "obsolete_credential_binding_data_present"
_BINDING_KEYS = frozenset({"credential_bindings", "credential_binding_ids"})


@dataclass
class ObsoleteBindingData:
    mcp_registry: Path | None = None

    @property
    def present(self) -> bool:
        return self.mcp_registry is not None


def _has_binding_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(key in _BINDING_KEYS or _has_binding_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_binding_key(item) for item in value)
    return False


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # unreadable files are reported by their own loaders


def obsolete_binding_data(root: str | Path) -> ObsoleteBindingData:
    layout = InstanceLayout(Path(root).expanduser().resolve())
    found = ObsoleteBindingData()
    registry = layout.mcp_connections_dir
    records = sorted((registry / "records").glob("*.json")) if (registry / "records").is_dir() else []
    if any(_has_binding_key((_load(path) or {}).get("descriptor")) for path in records):
        found.mcp_registry = registry
    return found


def obsolete_binding_message(found: ObsoleteBindingData) -> str:
    """Numbered steps; each step's explanation is one line starting with its
    number, followed by the exact commands, unindented so they can be pasted."""

    lines = [
        "Saved data from credential bindings is still present. Account links replaced credential "
        f"bindings, so it no longer loads (reason_code={OBSOLETE_BINDING_DATA_PRESENT}). Stop Universal "
        "Agents, run these steps, then start it again:",
    ]
    step = 0
    if found.mcp_registry is not None:
        step += 1
        target = found.mcp_registry.with_name(found.mcp_registry.name + ".before-account-links")
        lines += [
            f"{step}. Move the installed MCP connection records aside. Connections from the configuration are "
            "recreated at the next start and wait for approval (mcp.static_reconciliation.upgrade_mode); ask "
            "TeamLead to propose again any connection it had proposed. A connection that needs a stored "
            "credential declares credential_injections and gets an account with /link mcp:<name>.",
            f"mv {shlex.quote(str(found.mcp_registry))} {shlex.quote(str(target))}",
        ]
    return "\n".join(lines)


__all__ = [
    "OBSOLETE_BINDING_DATA_PRESENT",
    "ObsoleteBindingData",
    "obsolete_binding_data",
    "obsolete_binding_message",
]
