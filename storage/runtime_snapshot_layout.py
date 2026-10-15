"""Split snapshot documents into independently writable, versioned records.

The wire snapshot remains version 3. This database layout is separate: owner
metadata and ordered ids live in a small header, each task or workflow record
has its own row. There is no reader for the obsolete monolithic DB layout.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3


MAP_SECTIONS = (
    "subtask_to_parent_map", "task_queries", "turn_action_batches", "turn_action_outbox",
    "runtime_leases", "runtime_mailbox_events", "task_state_projection_records",
    "task_to_plan_id", "task_to_objective_id", "plan_to_objective_id", "recovery_links",
    "plan_step_task_links", "recoverable_pause_records",
    "partial_tool_call_limit_overrides", "runtime_resume_directives", "human_interaction_blockers",
    "final_review_attempt_counts",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def payload_digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SnapshotParts:
    header_json: str
    records: dict[tuple[str, str], str]


def split_snapshot(snapshot: RuntimeSnapshotV3) -> SnapshotParts:
    document = snapshot.model_dump(mode="json")
    tasks = document.pop("tasks")
    records = {("tasks", task["task_id"]): canonical_json(task) for task in tasks}
    for section in MAP_SECTIONS:
        records.update({(section, key): canonical_json(value) for key, value in document.pop(section).items()})
    return SnapshotParts(
        canonical_json({"layout_version": 1, "task_order": [task["task_id"] for task in tasks], "header": document}),
        records,
    )


def parse_storage_header(payload: str) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get("layout_version") != 1:
        raise ValueError(
            "Unsupported active task storage layout. Drain work with the previous runtime, "
            "then stop affected runtimes. Inspect obsolete snapshots using "
            "scripts/retire_runtime_snapshots.py --dry-run. Explicit retirement archives "
            "selected snapshots but abandons their unfinished work; see the configuration "
            "guide's active runtime snapshot upgrade instructions."
        )
    return value


def assemble_snapshot(header_json: str, records: dict[tuple[str, str], str]) -> str:
    stored = parse_storage_header(header_json)
    document = dict(stored["header"])
    document["tasks"] = [json.loads(records[("tasks", task_id)]) for task_id in stored["task_order"]]
    for section in MAP_SECTIONS:
        document[section] = {key: json.loads(value) for (kind, key), value in records.items() if kind == section}
    return canonical_json(document)
