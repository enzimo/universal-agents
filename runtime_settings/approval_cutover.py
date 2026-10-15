"""Detect approvals saved in the removed permission system's format.

Phase 4c of the unified access model records how configuration was approved
with one shared record (``ChangeApprovalRecordV1``: mode, who approved, when,
the approved access request). Configuration saved earlier names decision and
challenge ids from the removed permission store instead:

- MCP connection records in the MCP registry (schema version 1, whose
  approval named the old kinds; Phase 4c-3). Servers the registry first
  recorded from the operator's configuration keep that origin
  (``operator_configuration``); every other approval is re-approved by the
  administrator who runs the command. Per-agent-type MCP attachment records
  were retired in access plan 7d (grants on ``mcp:<name>`` decide use), so
  old attachment files are neither read nor re-approved;
- saved scheduled-worker settings in the manager database (binding schema
  version 1, which kept the removed permission system's policy and request
  ids; Phase 4c-4). The command rewrites those rows: it drops the removed
  fields and records ``held_access`` where the old record said ``authorized``
  (``storage.schedule_worker_repository``).

The runtime does not rewrite these (project rule: no automatic migration).
Startup refuses while any remains and names ``assistant_cli access
approve-existing``, with which an administrator re-approves them in place as
themselves; ``reapprove`` is that command's file transform.

Capability attachments in ``TEAM_AGENTS.yaml`` and generated specialists that
name capability ids were removed in Phase 7 and are not re-approved: the
roster loader and the agent catalog refuse them with the steps to take
(``custom_agents.team_composition``, ``custom_agents.agent_catalog``).
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp_control.registry import (
    MCP_CONNECTION_RECORD_SCHEMA_VERSION,
    OPERATOR_CONFIGURATION_APPROVER,
    MCPConnectionRecordV1,
    resolve_mcp_connection_paths,
)
from runtime_settings.instance_layout import InstanceLayout
from storage.config import StorageSettings

OLD_APPROVAL_RECORDS_PRESENT = "old_approval_records_present"
MCP_RECORD_SCHEMA_VERSION = MCP_CONNECTION_RECORD_SCHEMA_VERSION
_OLD_MCP_STATIC_KIND = "grandfathered_upgrade_v1"  # the old approval kind for configured servers


@dataclass
class OldApprovalRecords:
    mcp_connections: list[Path] = field(default_factory=list)
    # Schedule ids whose saved worker settings are in the old format, and the
    # manager database they are in.
    schedule_worker_bindings: list[str] = field(default_factory=list)
    storage_settings: StorageSettings | None = None

    @property
    def present(self) -> bool:
        return bool(
            self.mcp_connections
            or self.schedule_worker_bindings
        )


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # unreadable files are reported by their own loaders


def _old_mcp(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("schema_version") != MCP_RECORD_SCHEMA_VERSION


def old_approval_records(
    root: str | Path,
    *,
    storage_settings: StorageSettings | None = None,
) -> OldApprovalRecords:
    """Old-format records in the instance's files and, given its settings, the manager database."""

    layout = InstanceLayout(Path(root).expanduser().resolve())
    found = OldApprovalRecords(storage_settings=storage_settings)
    if storage_settings is not None:
        from storage.schedule_worker_repository import old_schedule_worker_bindings

        found.schedule_worker_bindings = list(old_schedule_worker_bindings(storage_settings))
    mcp = resolve_mcp_connection_paths(layout.root)
    found.mcp_connections = [path for path in sorted(mcp.records.glob("mcp_conn_*.json")) if _old_mcp(_load(path))]
    return found


def old_approval_message(found: OldApprovalRecords, *, config_path: str | Path) -> str:
    lines = [
        "Some saved configuration records its approval in the removed permission system's format "
        f"(reason_code={OLD_APPROVAL_RECORDS_PRESENT}):",
    ]
    if found.mcp_connections:
        lines.append(f"- {len(found.mcp_connections)} MCP connection record(s)")
    if found.schedule_worker_bindings:
        lines.append(
            f"- saved scheduled-worker settings of {len(found.schedule_worker_bindings)} schedule(s): "
            + ", ".join(found.schedule_worker_bindings[:10])
            + (" ..." if len(found.schedule_worker_bindings) > 10 else "")
        )
    lines += [
        "To keep them, an administrator re-approves them as themselves (Universal Agents may stay stopped;"
        " MCP servers from the configuration file keep that origin):",
        f"assistant_cli.py access approve-existing --config {config_path} --by <your user id>",
        "To drop an MCP connection instead, move its record out of the MCP registry folder. "
        "Then start Universal Agents again.",
    ]
    return "\n".join(lines)


def _new_approval(person_id: str, now: datetime) -> dict[str, Any]:
    return {"mode": "person_approval", "approved_by": person_id, "approved_at_utc": now.isoformat(), "access_request_id": None}


def _write(path: Path, text: str) -> None:
    """Replace a file that may be read-only (receipts are written read-only)."""

    mode = path.stat().st_mode
    if not mode & stat.S_IWUSR:
        path.chmod(mode | stat.S_IWUSR)
    temporary = path.with_name(f".{path.name}.reapprove.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    path.chmod(mode)


def _mcp_approval(old: Any, person_id: str, now: datetime) -> dict[str, Any] | None:
    if not isinstance(old, dict):
        return None
    if old.get("kind") == _OLD_MCP_STATIC_KIND or old.get("mode") == "operator_configuration":
        return {
            "mode": "operator_configuration",
            "approved_by": OPERATOR_CONFIGURATION_APPROVER,
            "approved_at_utc": old.get("approved_at_utc") or now.isoformat(),
        }
    return _new_approval(person_id, now)


def _reapproved_mcp(path: Path, person_id: str, now: datetime) -> str:
    """The record in schema version 2 with its approval re-recorded and digests recomputed."""

    payload = dict(_load(path))
    payload["schema_version"] = MCP_RECORD_SCHEMA_VERSION
    approval = _mcp_approval(payload.get("approval"), person_id, now)
    for key in ("record_digest", "operation_catalog_sha256"):
        payload.pop(key, None)
    if approval is None:
        payload.pop("approval", None)
    else:
        payload["approval"] = approval
    record = MCPConnectionRecordV1.build(**payload)
    return json.dumps(record.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True, default=str) + "\n"


def reapprove(found: OldApprovalRecords, *, person_id: str, now: datetime) -> list[str]:
    """Rewrite every old record with ``person_id`` as the approver; returns what changed."""

    changed: list[str] = []
    for path in found.mcp_connections:
        _write(path, _reapproved_mcp(path, person_id, now))
        changed.append(f"MCP registry record {path.name}")
    if found.schedule_worker_bindings and found.storage_settings is not None:
        from storage.schedule_worker_repository import ScheduleWorkerRepository

        for schedule_id in ScheduleWorkerRepository(found.storage_settings).rewrite_old_bindings():
            changed.append(f"scheduled-worker settings of schedule {schedule_id}")
    return changed


__all__ = [
    "OLD_APPROVAL_RECORDS_PRESENT",
    "OldApprovalRecords",
    "old_approval_message",
    "old_approval_records",
    "reapprove",
]
