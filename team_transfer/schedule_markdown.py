"""The editable schedule representation used by exports and reviewed imports."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import yaml
from croniter import croniter

from custom_messages.contracts.schedule_v1 import ScheduledMessage
from team_transfer.editable_yaml import load_editable_yaml
from team_transfer.errors import TransferError

_BLOCK = re.compile(r"^```yaml\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)
_EDITABLE = frozenset(
    {
        "schedule_name",
        "interval_ms",
        "cron_expression",
        "cron_timezone",
        "repeat_count",
        "next_fire_at",
        "paused_until",
        "expires_at_utc",
        "enabled",
        "claim_named_agent_type",
    }
)


def render_schedule(document: dict[str, Any]) -> str:
    schedule = ScheduledMessage.model_validate(document)
    return (
        "# Schedule review\n\n"
        "Edit the YAML block below. It controls the imported schedule. IDs, owner,\n"
        "delivery route, and system-management provenance must stay unchanged.\n"
        "Recurring schedules compute their next run at import and skip missed runs.\n\n"
        "The remaining run count is preserved unless you change repeat_count;\n"
        "a changed repeat_count becomes the new number of remaining runs.\n\n"
        "```yaml\n"
        + yaml.safe_dump(
            schedule.model_dump(mode="json"), sort_keys=False, allow_unicode=True
        )
        + "```\n"
    )


def parse_schedule(text: str) -> dict[str, Any]:
    blocks = _BLOCK.findall(text)
    if len(blocks) != 1:
        raise TransferError(
            "A schedule Markdown file must contain exactly one fenced YAML block."
        )
    raw = load_editable_yaml(blocks[0])
    if not isinstance(raw, dict):
        raise TransferError("The schedule YAML block must contain a mapping.")
    unknown = set(raw) - set(ScheduledMessage.model_fields)
    if unknown:
        raise TransferError("Unknown schedule settings: " + ", ".join(sorted(unknown)))
    schedule = ScheduledMessage.model_validate(raw)
    normalized = schedule.model_dump(mode="json")
    unknown_paths = _unknown_paths(raw, normalized)
    if unknown_paths:
        raise TransferError("Unknown schedule fields: " + ", ".join(unknown_paths))
    if schedule.interval_ms is not None and (
        schedule.interval_ms <= 0 or schedule.cron_expression
    ):
        raise TransferError(
            "Use a positive interval or a cron expression, with only one of them set."
        )
    if schedule.cron_expression and not croniter.is_valid(schedule.cron_expression):
        raise TransferError("The schedule's cron expression is invalid.")
    if schedule.repeat_count is not None and schedule.repeat_count < 1:
        raise TransferError(
            "The schedule's repeat count must be positive or null for unlimited runs."
        )
    for value in (
        schedule.next_fire_at,
        schedule.paused_until,
        schedule.expires_at_utc,
    ):
        if value:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
    return normalized


def _unknown_paths(raw: Any, normalized: Any, prefix: str = "") -> list[str]:
    found = []
    if isinstance(raw, dict) and isinstance(normalized, dict):
        for key, value in raw.items():
            path = f"{prefix}.{key}" if prefix else key
            if key not in normalized:
                found.append(path)
            else:
                found.extend(_unknown_paths(value, normalized[key], path))
    elif isinstance(raw, list) and isinstance(normalized, list):
        for index, value in enumerate(raw):
            if index < len(normalized):
                found.extend(
                    _unknown_paths(value, normalized[index], f"{prefix}.{index}")
                )
    return found


def edited_schedule(original: str, edited: str) -> dict[str, Any]:
    """An operator may edit behavior, but cannot reuse a signature for a new owner."""
    before = parse_schedule(original)
    after = parse_schedule(edited)
    for key in set(before) | set(after):
        if key in _EDITABLE:
            continue
        if key == "payload":
            old_task, new_task = dict(before[key]), dict(after[key])
            old_task.pop("problem_statement", None)
            new_task.pop("problem_statement", None)
            if old_task == new_task:
                continue
        if before.get(key) != after.get(key):
            raise TransferError(
                f"Schedule review cannot change {key}; create that schedule in the new instance instead."
            )
    return after
