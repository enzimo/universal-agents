"""Parse and normalize schedule tool inputs and compact schedule memory.

Shared by the schedule tools (``custom_tools.providers.schedules``) and the
schedule change path (``custom_agents.schedule_changes``), which rebuilds a
staged change from the same inputs when a person approves it. Everything
here is pure: no store, no agent, no access decisions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from custom_messages.contracts.schedule_v1 import (
    ScheduleMemoryPolicy,
    ScheduleTaskMemory,
)
from custom_agents.schedule_observations import SCHEDULE_MEMORY_RECENT_RUN_LIMIT

SCHEDULE_MEMORY_RECENT_NOTIFICATION_LIMIT = 5
SCHEDULE_NOTIFY_MODES = {"on_change", "always", "never"}
_TRUE_WORDS = {"1", "true", "yes", "on", "enabled", "enable"}
_FALSE_WORDS = {"0", "false", "no", "off", "disabled", "disable"}


def normalize_schedule_utc_iso(value: str, *, field_name: str) -> str:
    """An ISO 8601 timestamp as UTC; a timestamp without a zone is read as UTC."""

    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} is required.")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def parse_schedule_bool(value: str, *, field_name: str) -> Optional[bool]:
    """true/false words, or None for an empty value (leave unchanged)."""

    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    raise ValueError(f"{field_name} must be true, false, or empty for unchanged.")


def schedule_memory_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_schedule_json_object(value: str, *, field_name: str) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be a valid JSON object.") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    return parsed


def trim_schedule_memory_ledger(memory: ScheduleTaskMemory) -> None:
    if len(memory.ledger.recent_runs) > SCHEDULE_MEMORY_RECENT_RUN_LIMIT:
        memory.ledger.recent_runs = memory.ledger.recent_runs[-SCHEDULE_MEMORY_RECENT_RUN_LIMIT:]
    if len(memory.ledger.recent_notifications) > SCHEDULE_MEMORY_RECENT_NOTIFICATION_LIMIT:
        memory.ledger.recent_notifications = memory.ledger.recent_notifications[
            -SCHEDULE_MEMORY_RECENT_NOTIFICATION_LIMIT:
        ]


def validate_schedule_notify_mode(value: str, *, field_name: str = "notify") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw not in SCHEDULE_NOTIFY_MODES:
        raise ValueError(f"{field_name} must be one of: always, never, on_change.")
    return raw


def coerce_schedule_memory(
    *,
    schedule_memory_json: str = "",
    schedule_kind: str = "",
    notify: str = "",
    evidence_required: str = "",
    notify_on_failure: Optional[bool] = None,
) -> ScheduleTaskMemory:
    raw_memory = parse_schedule_json_object(schedule_memory_json, field_name="schedule_memory_json")
    memory = ScheduleTaskMemory.model_validate(raw_memory) if raw_memory else ScheduleTaskMemory()
    if schedule_kind.strip():
        memory.schedule_kind = schedule_kind.strip()
    notify_mode = validate_schedule_notify_mode(notify)
    if notify_mode:
        memory.policy.notify = notify_mode  # type: ignore[assignment]
    if evidence_required.strip():
        memory.policy.evidence_required = evidence_required.strip()
    if notify_on_failure is not None:
        memory.policy.notify_on_failure = bool(notify_on_failure)
    memory.updated_at = schedule_memory_now()
    trim_schedule_memory_ledger(memory)
    return memory


def apply_schedule_policy_update(
    memory: ScheduleTaskMemory,
    *,
    policy_json: str = "",
    notify: str = "",
    evidence_required: str = "",
    notify_on_failure: Optional[bool] = None,
) -> dict[str, Any]:
    """Change the memory's notification policy in place; returns what changed."""

    changes: dict[str, Any] = {}
    raw_policy = parse_schedule_json_object(policy_json, field_name="policy_json")
    if raw_policy:
        memory.policy = ScheduleMemoryPolicy.model_validate({**memory.policy.model_dump(mode="json"), **raw_policy})
        changes["policy"] = "updated"
    notify_mode = validate_schedule_notify_mode(notify)
    if notify_mode:
        memory.policy.notify = notify_mode  # type: ignore[assignment]
        changes["policy.notify"] = notify_mode
    if evidence_required.strip():
        memory.policy.evidence_required = evidence_required.strip()
        changes["policy.evidence_required"] = memory.policy.evidence_required
    if notify_on_failure is not None:
        memory.policy.notify_on_failure = bool(notify_on_failure)
        changes["policy.notify_on_failure"] = memory.policy.notify_on_failure
    return changes


__all__ = [
    "SCHEDULE_MEMORY_RECENT_NOTIFICATION_LIMIT",
    "SCHEDULE_NOTIFY_MODES",
    "apply_schedule_policy_update",
    "coerce_schedule_memory",
    "normalize_schedule_utc_iso",
    "parse_schedule_bool",
    "parse_schedule_json_object",
    "schedule_memory_now",
    "trim_schedule_memory_ledger",
    "validate_schedule_notify_mode",
]
