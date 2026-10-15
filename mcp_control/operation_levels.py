"""Access levels for the operations of installed MCP connections.

An MCP server describes its tools with annotations (``readOnlyHint``,
``destructiveHint``, ...). The MCP specification says clients must not make
access decisions from the annotations of untrusted servers, and its defaults
assume a tool may be destructive. So annotations are kept only as hints: they
give a *suggested* level, and an operator *confirms* the level of each
operation. Until then, and whenever an operation changes, it needs ``full``.

Hints and confirmations live beside the pinned connection records
(``<registry>/levels/<connection_id>.json``) rather than inside them: the
records are digest-pinned, and adding a field to every stored operation would
mark every installed connection as drifted. Each entry is keyed by the
operation's digest, so a changed operation loses its confirmation.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from custom_messages.contracts.access_v2 import AccessLevel
from mcp_control.registry import MCPConnectionPaths, MCPOperationDefinitionV1


OPERATION_LEVELS_SCHEMA_VERSION = 1
HINT_KEYS = ("title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


class MCPOperationLevelV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    hints: dict[str, bool | str] = Field(default_factory=dict)
    suggested_level: AccessLevel
    confirmed_level: AccessLevel | None = None
    confirmed_by: str | None = Field(default=None, max_length=240)
    confirmed_at_utc: datetime | None = None


class MCPOperationLevelsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = OPERATION_LEVELS_SCHEMA_VERSION
    connection_id: str
    operations: dict[str, MCPOperationLevelV1] = Field(default_factory=dict)


def normalize_hints(raw: Any) -> dict[str, bool | str]:
    """Known annotation keys only; anything else a server sends is dropped."""

    if raw is None:
        return {}
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(by_alias=True, exclude_none=True)
    if not isinstance(raw, Mapping):
        return {}
    hints: dict[str, bool | str] = {}
    for key in HINT_KEYS:
        value = raw.get(key)
        if key == "title" and isinstance(value, str):
            hints[key] = value[:200]
        elif key != "title" and isinstance(value, bool):
            hints[key] = value
    return hints


def suggest_level(hints: Mapping[str, Any]) -> AccessLevel:
    """Read-only hint -> read; explicitly not destructive -> write; else full.

    Missing hints use the MCP defaults (not read-only, destructive).
    """

    if hints.get("readOnlyHint") is True:
        return AccessLevel.READ
    if hints.get("destructiveHint") is False:
        return AccessLevel.WRITE
    return AccessLevel.FULL


def operation_levels_dir(paths: MCPConnectionPaths) -> Path:
    """Folder of confirmed operation levels, one file per connection."""

    return paths.root / "levels"


def _levels_path(paths: MCPConnectionPaths, connection_id: str) -> Path:
    return operation_levels_dir(paths) / f"{connection_id}.json"


def load_operation_levels(paths: MCPConnectionPaths, connection_id: str) -> MCPOperationLevelsV1:
    path = _levels_path(paths, connection_id)
    if not path.is_file():
        return MCPOperationLevelsV1(connection_id=connection_id)
    return MCPOperationLevelsV1.model_validate_json(path.read_text(encoding="utf-8"))


def _save(paths: MCPConnectionPaths, levels: MCPOperationLevelsV1) -> None:
    path = _levels_path(paths, levels.connection_id)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".levels-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            writer.write(levels.model_dump_json(indent=2))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def record_discovered_hints(
    paths: MCPConnectionPaths,
    connection_id: str,
    operations: Sequence[MCPOperationDefinitionV1],
    hints_by_operation: Mapping[str, Mapping[str, bool | str]],
) -> MCPOperationLevelsV1:
    """Store the hints of a discovered catalog, keeping still-valid confirmations."""

    previous = load_operation_levels(paths, connection_id).operations
    entries: dict[str, MCPOperationLevelV1] = {}
    for operation in operations:
        hints = dict(hints_by_operation.get(operation.operation_name) or {})
        kept = previous.get(operation.operation_name)
        confirmed = kept if kept is not None and kept.operation_digest == operation.operation_digest else None
        entries[operation.operation_name] = MCPOperationLevelV1(
            operation_digest=operation.operation_digest,
            hints=hints,
            suggested_level=suggest_level(hints),
            confirmed_level=confirmed.confirmed_level if confirmed else None,
            confirmed_by=confirmed.confirmed_by if confirmed else None,
            confirmed_at_utc=confirmed.confirmed_at_utc if confirmed else None,
        )
    levels = MCPOperationLevelsV1(connection_id=connection_id, operations=entries)
    _save(paths, levels)
    return levels


def confirm_operation_levels(
    paths: MCPConnectionPaths,
    connection_id: str,
    levels: Mapping[str, AccessLevel],
    *,
    confirmed_by: str,
) -> MCPOperationLevelsV1:
    """An operator confirms the level of discovered operations."""

    current = load_operation_levels(paths, connection_id)
    unknown = sorted(set(levels) - set(current.operations))
    if unknown:
        raise KeyError(f"These operations were not discovered for {connection_id}: {', '.join(unknown)}")
    now = datetime.now(timezone.utc)
    updated = dict(current.operations)
    for name, level in levels.items():
        updated[name] = updated[name].model_copy(
            update={"confirmed_level": AccessLevel(level), "confirmed_by": confirmed_by, "confirmed_at_utc": now}
        )
    result = current.model_copy(update={"operations": updated})
    _save(paths, result)
    return result


__all__ = [
    "HINT_KEYS",
    "MCPOperationLevelV1",
    "MCPOperationLevelsV1",
    "confirm_operation_levels",
    "load_operation_levels",
    "normalize_hints",
    "operation_levels_dir",
    "record_discovered_hints",
    "suggest_level",
]
