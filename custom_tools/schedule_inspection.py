"""Read-only schedule projections shared by the agent inspection tools.

Keep full details for exact-ID excerpts and exclude history/context from summaries.
Ownership is checked before either projection can leave the tool boundary: the
caller decides the ``ScheduleView`` through ``access_control.ownership``.
"""

from dataclasses import dataclass
from typing import Any, Optional

from custom_agents.schedule_store import ScheduleStore, owner_canonical_user_id_for_schedule
from custom_messages.contracts.schedule_v1 import ScheduledMessage


@dataclass(frozen=True)
class ScheduleView:
    """The schedules one task may see.

    Work for a person sees that person's schedules (``owner``). System work
    (no person) sees only the one schedule its signed context names
    (``schedule_id``; ``access_control.ownership.system_work_schedule_access``).
    """

    owner: Optional[str] = None
    schedule_id: Optional[str] = None

    def may_show_row(self, row: dict[str, Any]) -> bool:
        """A cheap check on a stored row before its schedule is loaded."""

        if self.schedule_id is not None:
            return row.get("schedule_id") == self.schedule_id
        row_owner = str(row.get("owner_canonical_user_id") or "").strip()
        return not row_owner or row_owner == self.owner

    def shows(self, schedule: Optional[ScheduledMessage]) -> bool:
        if schedule is None:
            return False
        if self.schedule_id is not None:
            return schedule.schedule_id == self.schedule_id
        return bool(self.owner) and owner_canonical_user_id_for_schedule(schedule) == self.owner


def list_schedule_payloads(
    store: ScheduleStore, *, view: ScheduleView, schedule_id: str = ""
) -> list[dict[str, Any]]:
    schedules: list[dict[str, Any]] = []
    for row in store.get_all():
        row_id = row.get("schedule_id")
        if not row_id or (schedule_id and row_id != schedule_id) or not view.may_show_row(row):
            continue
        scheduled_message = store.get_scheduled_message(row_id)
        if view.shows(scheduled_message):
            schedules.append(schedule_payload(scheduled_message))
    return schedules


def schedule_payload(scheduled_message: ScheduledMessage) -> dict[str, Any]:
    """Everything an exact-ID detail page may show about one schedule."""

    return {
        "schedule_id": scheduled_message.schedule_id,
        "schedule_name": scheduled_message.schedule_name,
        "management_class": scheduled_message.management_class.value,
        "system_schedule_kind": (
            scheduled_message.effective_system_schedule_kind.value
            if scheduled_message.effective_system_schedule_kind is not None
            else None
        ),
        "assigned_agent_type": scheduled_message.claim_named_agent_type,
        "enabled": scheduled_message.enabled,
        "paused_until": scheduled_message.paused_until,
        "expires_at_utc": scheduled_message.expires_at_utc,
        "next_fire_at": scheduled_message.next_fire_at,
        "interval_ms": scheduled_message.interval_ms,
        "cron_expression": scheduled_message.cron_expression,
        "cron_timezone": scheduled_message.cron_timezone,
        "repeat_count": scheduled_message.repeat_count,
        "objective_id": scheduled_message.objective_id,
        "owner_canonical_user_id": owner_canonical_user_id_for_schedule(scheduled_message),
        "problem_statement": scheduled_message.payload.problem_statement,
        "schedule_memory": scheduled_message.schedule_memory.model_dump(mode="json")
        if scheduled_message.schedule_memory is not None
        else None,
        "gateway_context": scheduled_message.payload_meta.gateway_context.model_dump(mode="json")
        if scheduled_message.payload_meta and scheduled_message.payload_meta.gateway_context
        else None,
    }


def compact_schedule(item: dict[str, Any]) -> dict[str, Any]:
    """Keep control metadata, never the unbounded run ledger or gateway context."""
    compact = {
        key: value
        for key, value in item.items()
        if key not in {"problem_statement", "schedule_memory", "gateway_context"}
    }
    if "problem_statement" in item:
        compact["problem_preview"] = str(item["problem_statement"] or "")[:240]
    compact["schedule_name"] = str(item.get("schedule_name") or "")[:160]
    memory = item.get("schedule_memory") or {}
    compact["notify_policy"] = (memory.get("policy") or {}).get("notify")
    return compact
