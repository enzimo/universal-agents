"""The ``schedules`` section's records under a new team id (owner's decision 103, ``team_transfer.team_ids``).

Kept apart from ``team_transfer.schedules`` (the records, export and plan) and its writer so each
file stays focused. ``renamed_schedules`` is the rename the section registers in
``sections.TEAM_ID_RENAMES``; it runs on every read of the section, so the review, record keys,
the plan, the writer and the receipt all see the same records.

What follows the new id, and why:

- **A schedule's team and routes.** ``claim_team_id`` (only the team's agents claim a run), and
  in the saved task's header ``from_agent_id``, ``to_routing_path`` and ``response_routing_key``
  when they are the team id or start with ``<team>_``: a person's schedule replies to the
  gateway's route ``<team>_<instance>_user_gateway`` (``gateway.response_queue_name``, which
  the settings section renames by the same rule).
- **The team's maintenance schedule** (internal improvement). Its id, name and launch task id
  are built from the team id, and the runtime checks the id and name, so they are computed
  again for the new id with the runtime's own functions (``internal_improvement_runtime``).
  Every record naming that schedule follows: its delivery target, the ``scheduled_job_id`` in
  its gateway context, its worker binding, and grants to it (``team_transfer.access``). The new
  instance's manager then finds it as its own and installs no second one.
- **Signed contexts.** A context saved with a schedule names the old team and carries the old
  gateway's signature, so under a new id only that field (``gateway_context.task_context``) is
  dropped; the rest of the gateway context stays, because the gateway's refresh of a system
  schedule needs it (``custom_agents.schedule_context_refresh``). The writer signs a person's
  schedule again for the new team when the old key vouched for it, as without a new id, and the
  gateway signs system schedules again at its first start.
- **Worker bindings and pool sizes.** ``team_id``; a binding's ``schedule_id`` follows its
  schedule. A binding's ``execution_digest`` covers the schedule's team, task and reply route, so
  it is computed again for the renamed schedule, but only when it matched the schedule as
  exported: a binding that did not match keeps its digest and waits for review, as it does today.
- **Automation jobs and managed services** are only listed (never imported); a team owner is
  shown under the new id.

Sessions, the heartbeat list and the rest of each schedule (people, instance names, agent ids,
conversations) hold no team id and stay. A value of another team keeps its id.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from team_transfer.errors import TransferError
from team_transfer.schedule_records import ScheduleRecordKind
from team_transfer.sections import SectionRecords
from team_transfer.team_ids import TeamIds

if TYPE_CHECKING:
    from custom_messages.contracts.schedule_v1 import ScheduledMessage

logger = logging.getLogger(__name__)

# Header routes that hold a team route (``<team>_<agent or gateway>``) or the team id itself.
_HEADER_ROUTES: tuple[str, ...] = ("from_agent_id", "to_routing_path", "response_routing_key")
_ROUTE_SEPARATOR = "_"


def renamed_schedule_id(team_ids: TeamIds, schedule_id: Any) -> Any:
    """The schedule id under the new team id: only the team's maintenance schedule's id changes."""

    from custom_agents.internal_improvement_runtime import internal_improvement_schedule_id

    return team_ids.rename_derived(schedule_id, internal_improvement_schedule_id)


def _changed(record: dict[str, Any], name: str, value: Any) -> dict[str, Any]:
    """``record`` with ``name`` set to ``value`` when it holds the field; absent fields stay absent."""

    return {**record, name: value} if name in record else record


def _renamed_document(document: dict[str, Any], team_ids: TeamIds) -> dict[str, Any]:
    """A schedule document (``ScheduledMessage`` as JSON) under the new team id (module docstring)."""

    from custom_agents.internal_improvement_runtime import (
        internal_improvement_schedule_name,
        internal_improvement_task_id,
    )

    changed = _changed(document, "schedule_id", renamed_schedule_id(team_ids, document.get("schedule_id")))
    changed = _changed(
        changed, "schedule_name", team_ids.rename_derived(document.get("schedule_name"), internal_improvement_schedule_name)
    )
    changed = team_ids.renamed_fields(changed, ("claim_team_id",))
    payload = changed.get("payload")
    if isinstance(payload, dict):
        task_id = team_ids.rename_derived(payload.get("task_id"), internal_improvement_task_id)
        changed["payload"] = _changed(payload, "task_id", task_id)
    header = changed.get("payload_header")
    if isinstance(header, dict):
        changed["payload_header"] = team_ids.renamed_fields(header, _HEADER_ROUTES, _ROUTE_SEPARATOR)
    meta = changed.get("payload_meta")
    gateway = meta.get("gateway_context") if isinstance(meta, dict) else None
    if isinstance(meta, dict) and isinstance(gateway, dict):
        gateway = _changed(gateway, "scheduled_job_id", renamed_schedule_id(team_ids, gateway.get("scheduled_job_id")))
        # Signed by the old gateway for the old team; signed again later (module docstring).
        gateway = _changed(gateway, "task_context", None)
        changed["payload_meta"] = {**meta, "gateway_context": gateway}
    return changed


class _Schedules:
    """Each schedule as exported and as renamed, so bindings can check their digest against both."""

    def __init__(self) -> None:
        self.exported: dict[str, "ScheduledMessage"] = {}
        self.renamed: dict[str, "ScheduledMessage"] = {}

    def renamed_schedule(self, raw: dict[str, Any], team_ids: TeamIds) -> dict[str, Any]:
        from custom_messages.contracts.schedule_v1 import ScheduledMessage
        from team_transfer.schedule_markdown import parse_schedule, render_schedule

        entry = raw.get("schedule")
        if not isinstance(entry, dict) or not isinstance(entry.get("markdown"), str):
            return raw
        try:
            document = parse_schedule(entry["markdown"])
            exported = ScheduledMessage.model_validate(document)
            renamed_document = _renamed_document(document, team_ids)
            markdown = render_schedule(renamed_document)
        except (TransferError, ValueError) as exc:
            # Left as it is: the section's planner refuses it with the model's reason, as today.
            logger.warning(
                "A schedule the import cannot read keeps its exported team id. "
                "event=team_transfer_schedule_rename_skipped record_id=%s error=%s",
                raw.get("record_id"),
                exc,
            )
            return raw
        self.exported[exported.schedule_id] = exported
        self.renamed[exported.schedule_id] = ScheduledMessage.model_validate(renamed_document)
        return {
            **raw,
            "record_id": renamed_schedule_id(team_ids, raw.get("record_id")),
            "schedule": {**entry, "markdown": markdown},
        }

    def binding_digest(self, document: dict[str, Any]) -> Optional[str]:
        """The binding's digest for its renamed schedule; None to keep the carried one.

        Only a digest that matched the schedule as exported is computed again, so a binding
        waiting for review before the move still waits after it.
        """

        from custom_agents.schedule_execution_identity import execution_digest

        schedule_id, definition = document.get("schedule_id"), document.get("definition_digest")
        if not isinstance(schedule_id, str) or not isinstance(definition, str):
            return None
        exported, renamed = self.exported.get(schedule_id), self.renamed.get(schedule_id)
        if exported is None or renamed is None:
            return None
        if execution_digest(exported, definition) != document.get("execution_digest"):
            return None
        return execution_digest(renamed, definition)


def _renamed_binding(raw: dict[str, Any], schedules: _Schedules, team_ids: TeamIds) -> dict[str, Any]:
    document = raw.get("document")
    if not isinstance(document, dict):
        return raw
    digest = schedules.binding_digest(document)
    changed = team_ids.renamed_fields(document, ("team_id",))
    changed = _changed(changed, "schedule_id", renamed_schedule_id(team_ids, document.get("schedule_id")))
    if digest is not None:
        changed = _changed(changed, "execution_digest", digest)
    return {**raw, "record_id": renamed_schedule_id(team_ids, raw.get("record_id")), "document": changed}


def _renamed_target(raw: dict[str, Any], team_ids: TeamIds) -> dict[str, Any]:
    target = raw.get("target")
    if not isinstance(target, dict):
        return raw
    schedule_id = renamed_schedule_id(team_ids, target.get("schedule_id"))
    return {
        **raw,
        "record_id": renamed_schedule_id(team_ids, raw.get("record_id")),
        "target": _changed(target, "schedule_id", schedule_id),
    }


def _renamed_execution(raw: dict[str, Any], team_ids: TeamIds) -> dict[str, Any]:
    from runtime_hosting.execution_contracts import OwnerScopeKind

    listed = raw.get("execution")
    if not isinstance(listed, dict) or listed.get("owner_kind") != OwnerScopeKind.TEAM.value:
        return raw
    return {**raw, "execution": team_ids.renamed_fields(listed, ("owner_id",))}


def renamed_schedules(records: SectionRecords, team_ids: TeamIds) -> SectionRecords:
    """The section's records under the new team id (module docstring); the input is not changed.

    Schedules are renamed first, in any order the file holds them, so each binding is checked
    against its own schedule.
    """

    schedules = _Schedules()
    renamed: list[dict[str, Any]] = [
        schedules.renamed_schedule(raw, team_ids) if raw.get("kind") == ScheduleRecordKind.SCHEDULE.value else raw
        for raw in records
    ]
    for index, raw in enumerate(renamed):
        kind = raw.get("kind")
        if kind == ScheduleRecordKind.WORKER_BINDING.value:
            renamed[index] = _renamed_binding(raw, schedules, team_ids)
        elif kind == ScheduleRecordKind.POOL_LIMIT.value and isinstance(raw.get("document"), dict):
            renamed[index] = {**raw, "document": team_ids.renamed_fields(raw["document"], ("team_id",))}
        elif kind == ScheduleRecordKind.SCHEDULED_TARGET.value:
            renamed[index] = _renamed_target(raw, team_ids)
        elif kind in (ScheduleRecordKind.EXECUTION_JOB.value, ScheduleRecordKind.MANAGED_SERVICE.value):
            renamed[index] = _renamed_execution(raw, team_ids)
    return renamed


__all__ = ["renamed_schedule_id", "renamed_schedules"]
