"""Apply one Objective lifecycle change as one task operation.

A lifecycle change sets an Objective's status and makes its future work follow:
pausing or closing turns off its bounded work policy and the schedules and
heartbeat items it manages (``managed_by_objective``); resuming turns back on
only what an earlier control turned off. A person's Runtime controls
(``custom_agents.manager_control_runtime``) and the team lead's
``objectives(action="update_status")`` tool both use ``apply_objective_lifecycle``,
so the two behave the same.

Why one operation: these changes used to keep the copy returned by one lane
operation, switch links outside the lane, and save that copy in a later
operation, which erased whatever was stored in between: the work policy's own
event every time, and a progress update or a schedule linked by a parallel
tool call when one landed in the gap. ``apply_objective_lifecycle`` runs
inside one ``TaskOperationRuntime`` operation, so its steps happen in order
with nothing else of the team lead in between, and every Objective write in it
is applied to the stored record inside one store transaction
(``ObjectiveCoordinator.change_objective``). Only the status write checks the
version an agent read (``ObjectiveStatusChange.expected_version``); the work
policy and link records that follow it are the change's own bookkeeping.

Who decides what: the caller has already decided that its person or task may
change this Objective (``access_control.ownership``) and which statuses it may
set. The ``ObjectiveLinkSwitch`` it passes carries its rules for turning
schedules on and off (a person's gateway-signed permit, or the agent rule that
leaves system-managed schedules alone). This module only applies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Optional, Protocol

from custom_agents.task_manager_objectives import ObjectiveSaveFailed
from custom_messages.contracts.control_v1 import (
    OBJECTIVE_PART_HEARTBEAT,
    OBJECTIVE_PART_POLICY,
    OBJECTIVE_PART_RECORD,
    OBJECTIVE_PART_SCHEDULE,
)
from custom_messages.enums import ObjectiveStatus
from custom_messages.records.objectives import (
    ObjectiveHeartbeatLink,
    ObjectiveRecord,
    ObjectiveScheduleLink,
)

logger = logging.getLogger(__name__)

# Whether a status turns the Objective's future work on (True) or off (False).
# Statuses missing here (proposed, blocked, deleted) leave it as it is.
_FUTURE_WORK_ENABLED: dict[ObjectiveStatus, bool] = {
    ObjectiveStatus.ACTIVE: True,
    ObjectiveStatus.PAUSED: False,
    ObjectiveStatus.SATISFIED: False,
    ObjectiveStatus.FAILED: False,
    ObjectiveStatus.CANCELLED: False,
    ObjectiveStatus.ABANDONED: False,
    ObjectiveStatus.DELETING: False,
}



class LinkSwitchResult(StrEnum):
    """What happened when a linked schedule or heartbeat item was turned on or off."""

    SWITCHED = "switched"  # it is now in the requested state
    MISSING = "missing"  # it no longer exists
    SKIPPED = "skipped"  # the caller's rules leave it alone; reported in ``skipped``
    FAILED = "failed"  # it could not be changed


class ObjectiveLinkSwitch(Protocol):
    """Turns one linked item on or off under the caller's authority rules."""

    def switch_schedule(
        self, objective_id: str, link: ObjectiveScheduleLink, enabled: bool
    ) -> LinkSwitchResult: ...

    def switch_heartbeat(
        self, objective_id: str, link: ObjectiveHeartbeatLink, enabled: bool
    ) -> LinkSwitchResult: ...


@dataclass(frozen=True)
class ObjectiveStatusChange:
    """One requested status and what the Objective records with it.

    ``policy_person_id`` is the person the change works for, checked against
    the work policy's owner; None for work for no person. A deletion sets
    ``deletion_operation_id`` and ``tolerate_missing_links``: an item that is
    already gone counts as turned off. ``expected_version`` is the Objective
    version an agent read (``objective_version``): the status write is
    refused with ``changed_since_read`` when the Objective changed since, and
    then nothing else changes. A person's controls pass none; they check the
    digest of the page they acted on instead.
    """

    status: ObjectiveStatus
    summary: Optional[str] = None
    user_confirmation_summary: Optional[str] = None
    satisfied_by_task_id: Optional[str] = None
    task_id: Optional[str] = None
    plan_id: Optional[str] = None
    user_visible: bool = True
    deletion_operation_id: Optional[str] = None
    policy_person_id: Optional[str] = None
    tolerate_missing_links: bool = False
    expected_version: Optional[str] = None


@dataclass(frozen=True)
class ObjectiveLifecycleOutcome:
    """The stored Objective after the change, and what did not finish.

    ``incomplete`` names each control that did not finish, as
    ``policy:<id>``, ``schedule:<id>``, ``heartbeat:<id>`` or
    ``objective:<id>`` (the parts are ``control_v1.OBJECTIVE_PART_*``;
    ``objective`` means the Objective could not record which links changed,
    so a later resume would not undo them). ``skipped`` names linked items
    the caller's rules left as they were (``schedule:<id>``), so the caller
    can say so.
    """

    objective: ObjectiveRecord
    incomplete: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    links_switched: bool = False


def apply_objective_lifecycle(
    task_manager: Any,
    objective_id: str,
    change: ObjectiveStatusChange,
    links: ObjectiveLinkSwitch,
) -> Optional[ObjectiveLifecycleOutcome]:
    """Set the status, then the work policy, then the links; run inside one task operation.

    Returns None when the Objective does not exist. The status is saved
    first: when that save fails, ``ObjectiveSaveFailed`` is raised, and when
    it is refused for an older version, ``ChangeRefused``; either way nothing
    else has changed. Later steps that fail are reported in ``incomplete``
    instead.
    """

    updated = task_manager.update_objective_status(
        objective_id,
        change.status,
        summary=change.summary,
        user_confirmation_summary=change.user_confirmation_summary,
        satisfied_by_task_id=change.satisfied_by_task_id,
        task_id=change.task_id,
        plan_id=change.plan_id,
        user_visible=change.user_visible,
        deletion_operation_id=change.deletion_operation_id,
        expected_version=change.expected_version,
    )
    if updated is None:
        return None
    enabled = _FUTURE_WORK_ENABLED.get(updated.status)
    if enabled is None:
        return ObjectiveLifecycleOutcome(objective=updated)
    incomplete = _control_work_policy(
        task_manager, objective_id, enabled=enabled, person_id=change.policy_person_id
    )
    switched, failures, skipped = _switch_links(
        updated, links, enabled=enabled, tolerate_missing=change.tolerate_missing_links
    )
    incomplete.extend(failures)
    if switched and not _record_switched_links(task_manager, objective_id, switched):
        incomplete.append(f"{OBJECTIVE_PART_RECORD}:{objective_id}")
    if incomplete:
        logger.warning(
            "An Objective lifecycle change did not finish every control. "
            "event=objective_lifecycle_incomplete objective_id=%s status=%s incomplete=%s",
            objective_id,
            updated.status.value,
            ",".join(incomplete),
        )
    final = task_manager.get_objective(objective_id) or updated
    return ObjectiveLifecycleOutcome(
        objective=final,
        incomplete=tuple(incomplete),
        skipped=tuple(skipped),
        links_switched=bool(switched),
    )


def _control_work_policy(
    task_manager: Any, objective_id: str, *, enabled: bool, person_id: Optional[str]
) -> list[str]:
    """Turn the Objective's bounded work policy off, or back on after an earlier control.

    Resuming turns on only a policy that an Objective control turned off,
    never one the person turned off themselves. The policy coordinator saves
    its own Objective event; it loads the stored record to do so.
    """

    coordinator = getattr(task_manager, "objective_work_coordinator", None)
    if coordinator is None or not coordinator.enabled:
        return []
    try:
        policy = coordinator.objective_work_repository.get_policy(
            objective_id, owner_agent_id=task_manager.durable_manager_scope_id()
        )
        if policy is None:
            return []
    except Exception:
        logger.exception(
            "An Objective lifecycle change could not read the work policy. "
            "event=objective_control_policy_read_failed objective_id=%s",
            objective_id,
        )
        return [f"{OBJECTIVE_PART_POLICY}:{objective_id}"]
    try:
        if enabled:
            coordinator.enable_policy_after_objective_control(
                objective_id, owner_canonical_user_id=person_id
            )
        else:
            coordinator.disable_policy(
                objective_id, owner_canonical_user_id=person_id, by_objective_control=True
            )
    except Exception:
        logger.exception(
            "An Objective lifecycle change could not update the work policy. "
            "event=objective_control_policy_mutation_failed objective_id=%s policy_id=%s enabled=%s",
            objective_id,
            policy.policy_id,
            enabled,
        )
        return [f"{OBJECTIVE_PART_POLICY}:{policy.policy_id}"]
    return []


def _links(objective: ObjectiveRecord) -> list[tuple[str, str, Any]]:
    """Every schedule and heartbeat link of the Objective, as (kind, item id, link)."""

    return [(OBJECTIVE_PART_SCHEDULE, link.schedule_id, link) for link in objective.schedule_links] + [
        (OBJECTIVE_PART_HEARTBEAT, link.heartbeat_item_id, link) for link in objective.heartbeat_links
    ]


def _link_needs_switch(link: Any, *, enabled: bool) -> bool:
    """Only links the Objective manages, and only those not already in the requested state."""

    if not link.managed_by_objective:
        return False
    return bool(link.disabled_by_objective_pause) if enabled else not link.disabled_by_objective_pause


def _switch_links(
    objective: ObjectiveRecord,
    links: ObjectiveLinkSwitch,
    *,
    enabled: bool,
    tolerate_missing: bool,
) -> tuple[dict[tuple[str, str], bool], list[str], list[str]]:
    """Turn the managed links on or off.

    Returns the new ``disabled_by_objective_pause`` flags of the links that
    switched, the items that failed and the items the caller's rules skipped.
    """

    switched: dict[tuple[str, str], bool] = {}
    failures: list[str] = []
    skipped: list[str] = []
    for kind, item_id, link in _links(objective):
        if not _link_needs_switch(link, enabled=enabled):
            continue
        result = _switch_one(objective.objective_id, links, kind, item_id, link, enabled)
        if result is LinkSwitchResult.MISSING:
            logger.warning(
                "An Objective control found a linked item missing. "
                "event=objective_control_link_missing objective_id=%s kind=%s item_id=%s "
                "counted_as_switched=%s",
                objective.objective_id,
                kind,
                item_id,
                tolerate_missing,
            )
            result = LinkSwitchResult.SWITCHED if tolerate_missing else LinkSwitchResult.FAILED
        if result is LinkSwitchResult.SWITCHED:
            switched[(kind, item_id)] = not enabled
        elif result is LinkSwitchResult.FAILED:
            failures.append(f"{kind}:{item_id}")
        else:
            skipped.append(f"{kind}:{item_id}")
    return switched, failures, skipped


def _switch_one(
    objective_id: str,
    links: ObjectiveLinkSwitch,
    kind: str,
    item_id: str,
    link: Any,
    enabled: bool,
) -> LinkSwitchResult:
    """One switch under the caller's rules; an error counts as a failure.

    The status is already saved when links switch, so an error here must not
    escape: the item is reported in ``incomplete`` and its link keeps saying
    the control did not turn it off.
    """

    try:
        if kind == OBJECTIVE_PART_SCHEDULE:
            return links.switch_schedule(objective_id, link, enabled)
        return links.switch_heartbeat(objective_id, link, enabled)
    except Exception as exc:
        logger.warning(
            "An Objective control could not switch a linked item. "
            "event=objective_control_link_switch_failed objective_id=%s kind=%s item_id=%s "
            "enabled=%s error_type=%s",
            objective_id,
            kind,
            item_id,
            enabled,
            type(exc).__name__,
            exc_info=True,
        )
        return LinkSwitchResult.FAILED


def _record_switched_links(
    task_manager: Any, objective_id: str, switched: dict[tuple[str, str], bool]
) -> bool:
    """Record on the stored Objective which links the control turned off; False when not saved."""

    def mark(objective: ObjectiveRecord) -> bool:
        changed = False
        for kind, item_id, link in _links(objective):
            flag = switched.get((kind, item_id))
            if flag is not None and link.disabled_by_objective_pause != flag:
                link.disabled_by_objective_pause = flag
                changed = True
        return changed

    try:
        return task_manager.objective_coordinator.change_objective(objective_id, mark) is not None
    except ObjectiveSaveFailed:
        # The coordinator logged event=objective_save_failed; the caller reports it.
        return False


__all__ = [
    "LinkSwitchResult",
    "ObjectiveLifecycleOutcome",
    "ObjectiveLinkSwitch",
    "ObjectiveStatusChange",
    "apply_objective_lifecycle",
]
