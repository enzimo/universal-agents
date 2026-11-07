"""A person's own controls over schedules: which they see, pause, resume, turn off or remove.

The team services answer people's ``schedules.*`` commands with these
(``team_services.schedules``); until phase 2 the team lead's process did, for a durable
``ManagerScheduleCommand``. The rules are unchanged:

- **Whose schedules.** A person sees and changes only schedules they own
  (``owner_canonical_user_id_for_schedule``, ``access_control.ownership``); others' are
  "not found".
- **System schedules** (memory reviews, heartbeats and the others) change only for people
  who could approve a ``system_schedule`` change, decided by the access service with the
  person's current roles (``may_change_system_schedules``).
- **The listed version.** A change names the listing digest the person saw; the store
  checks it, and the permit, in the same write transaction as the change
  (``custom_agents.schedule_store``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from access_control.ownership import PRIVATE_READ, RESOURCE_SCHEDULE, private_access
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    ScheduleMutationPermit,
    build_schedule_mutation_permit,
)
from custom_agents.schedule_store import ScheduleStore, owner_canonical_user_id_for_schedule
from custom_messages.contracts.schedule_v1 import ScheduledMessage, ScheduleWorkerAssignmentV1
from storage.operation_outcomes import CommandOutcome

SCHEDULE_ACTIONS = ("pause", "resume", "disable", "remove")


def schedule_owned_by(schedule: Any, *, person_id: str, action: str) -> bool:
    """Whether the person owns the schedule for this action (a missing schedule is nobody's)."""

    if schedule is None:
        return False
    return private_access(
        person_id=person_id,
        owner_id=owner_canonical_user_id_for_schedule(schedule),
        resource_type=RESOURCE_SCHEDULE,
        resource_id=schedule.schedule_id,
        action=action,
    ).allowed


def person_schedule_permit(
    schedule: Any, *, person_id: str, team_id: str, operation: ScheduleMutationOperation, candidate: Any = None,
) -> Optional[ScheduleMutationPermit]:
    """The permit a person's own change carries (for a system schedule; None is fine for theirs)."""

    return build_schedule_mutation_permit(
        schedule=schedule,
        operation=operation,
        authority_mode=ScheduleMutationAuthorityMode.AUTHENTICATED_GATEWAY_USER,
        actor_principal_id=person_id,
        owner_canonical_user_id=person_id,
        team_id=team_id,
        candidate=candidate,
    )


def schedule_worker_assignment(store: ScheduleStore, schedule: ScheduledMessage, owner: str) -> ScheduleWorkerAssignmentV1:
    """Project a schedule already authorized for this owner, in its saved team.

    Owner-wide inventory can include another team in shared storage. The viewing
    side's team must not replace the schedule's persisted binding identity.
    """

    from custom_agents.local_schedule_policy import local_system_timer_spec
    from custom_agents.schedule_repair_guidance import person_explanation
    from custom_agents.stuck_schedules import not_running

    # A system timer an on-demand worker runs names its agent type (memory review: MemReviewAgent).
    system_timer = local_system_timer_spec(schedule)
    stuck = not_running(store, schedule, now=datetime.now(timezone.utc))
    binding = (
        store.worker_records.get_binding(schedule.schedule_id, team_id=schedule.claim_team_id, owner_id=owner)
        if schedule.claim_team_id else None
    )
    return ScheduleWorkerAssignmentV1(
        agent_type=schedule.claim_named_agent_type,
        agent_id=schedule.claim_agent_id,
        review_agent_type=system_timer.agent_type if system_timer else None,
        upgrade_required=system_timer is not None and schedule.claim_named_agent_type != system_timer.agent_type,
        automatic_creation=(
            "not_configured" if binding is None
            else "disabled" if not binding.policy.enabled
            else "default_policy" if binding.authority_mode == "runtime_default"
            else "enabled"
        ),
        desired_agent_type=binding.target_agent_type if binding else None,
        reassignment_pending=bool(binding and (
            schedule.claim_agent_id or schedule.claim_named_agent_type != binding.target_agent_type
        )),
        not_running_since=stuck[0] if stuck else None,
        not_running_explanation=person_explanation(stuck[1]) if stuck else None,
    )


def apply_person_schedule_change(
    store: ScheduleStore,
    action: str,
    schedule: ScheduledMessage,
    *,
    person_id: str,
    team_id: str,
    expected_record_digest: Optional[str],
    paused_until: Optional[str] = None,
    command_outcome: Optional[CommandOutcome] = None,
) -> bool:
    """Pause, resume, turn off or remove one schedule for the person; False when it no longer exists.

    The listed version and the permit are checked in the store's write transaction
    (``ScheduleWriteConflict``, ``SystemScheduleMutationDenied``).
    """

    schedule_id = schedule.schedule_id

    def permit(operation: ScheduleMutationOperation, **changes: Any) -> Optional[ScheduleMutationPermit]:
        candidate = schedule.model_copy(deep=True, update=changes) if changes else None
        return person_schedule_permit(schedule, person_id=person_id, team_id=team_id, operation=operation,
                                      candidate=candidate)

    if action == "pause":
        return store.set_paused_until(
            schedule_id, paused_until, operation=ScheduleMutationOperation.PAUSE,
            system_mutation_permit=permit(ScheduleMutationOperation.PAUSE, paused_until=paused_until),
            expected_record_digest=expected_record_digest, command_outcome=command_outcome,
        )
    if action == "resume":
        return store.resume(
            schedule_id, system_mutation_permit=permit(ScheduleMutationOperation.RESUME, paused_until=None, enabled=True),
            expected_record_digest=expected_record_digest, command_outcome=command_outcome,
        )
    if action == "disable":
        return store.set_enabled(
            schedule_id, False, operation=ScheduleMutationOperation.DISABLE,
            system_mutation_permit=permit(ScheduleMutationOperation.DISABLE, enabled=False),
            expected_record_digest=expected_record_digest, command_outcome=command_outcome,
        )
    if action == "remove":
        return store.remove(
            schedule_id, system_mutation_permit=permit(ScheduleMutationOperation.DELETE),
            expected_record_digest=expected_record_digest, command_outcome=command_outcome,
        )
    raise ValueError(f"Unknown schedule action: {action}")


__all__ = [
    "PRIVATE_READ",
    "SCHEDULE_ACTIONS",
    "apply_person_schedule_change",
    "person_schedule_permit",
    "schedule_owned_by",
    "schedule_worker_assignment",
]
