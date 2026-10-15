"""Stable schedule behavior and delivery identity, independent of runtime custody.

``execution_digest`` is saved with a scheduled worker's policy and compared at
every admission: a schedule whose task, owner, target or delivery changed
outside an authorized path stops until its owner confirms the change.

Run notes (``schedule_memory``, policy included) are not part of it. A run
writes them itself (``notify_user`` and ``record_scheduled_run`` create the
notes on a schedule's first notification or recorded run), and they grant
nothing, so counting them made schedules block their own next run (found
2026-10-04). The approval fingerprint (``schedule_management``) already left
them out for the same reason.

Paths that change a schedule with the authority a re-save needs (the owner's
own request, a person's approval, trusted runtime code) call
``follow_authorized_change`` so the saved policy moves with the schedule.
"""

from __future__ import annotations

import logging
from typing import Any

from custom_agents.schedule_repair_guidance import (
    SCHEDULE_EXECUTION_CHANGED,
    SCHEDULE_LAUNCH_INTENT_CHANGED,
)
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from custom_messages.contracts.schedule_worker_v1 import worker_digest

logger = logging.getLogger(__name__)

# Blocks whose cause is gone once the saved policy matches the schedule again.
_EXECUTION_CHANGE_REASONS = frozenset(
    {SCHEDULE_EXECUTION_CHANGED, SCHEDULE_LAUNCH_INTENT_CHANGED}
)


def execution_digest(schedule: Any, definition_digest: str = "") -> str:
    """Changing behavior, ownership or target invalidates schedule-bound access.

    ``definition_digest`` is the binding's saved definition value (see
    schedule_definition_identity.schedule_definition_digest). Cadence, claim
    leases, run counts, run notes and launcher tuning do not change the job's
    authority. Payload ids are retained so replacing the task is a new scope.
    """
    meta = schedule.payload_meta
    gateway = getattr(meta, "gateway_context", None)
    delivery = {
        key: getattr(gateway, key, None)
        for key in (
            "gateway_instance_name",
            "session_key",
            "channel_name",
            "user_id",
            "chat_id",
            "thread_id",
            "delivery_mode",
        )
    }
    return worker_digest(
        {
            "schedule_id": schedule.schedule_id,
            "owner": owner_canonical_user_id_for_schedule(schedule),
            "team": schedule.claim_team_id,
            "type": schedule.claim_named_agent_type,
            "agent": schedule.claim_agent_id,
            "definition": definition_digest,
            "task": schedule.payload.model_dump(mode="json", exclude_none=True),
            "delivery": delivery,
            "response_route": getattr(
                schedule.payload_header, "response_routing_key", None
            ),
        }
    )


def follow_authorized_change(
    worker_records: Any,
    team_id: str,
    before: Any,
    after: Any,
    *,
    source: str,
) -> bool:
    """Move a schedule's saved worker policy along with an authorized change.

    ``before`` and ``after`` are the schedule as stored just before and after
    the change. Only a policy that matched ``before`` moves (compare-and-set),
    and only when the owner is unchanged: a new owner is a different person's
    work and keeps the review block. Returns True when the policy moved.
    """
    if after is None:
        return False
    binding = worker_records.get_binding(before.schedule_id, team_id=team_id)
    if binding is None:
        return False
    old = execution_digest(before, binding.definition_digest)
    new = execution_digest(after, binding.definition_digest)
    if old == new:
        return False
    owner = owner_canonical_user_id_for_schedule(after)
    if not owner or owner != owner_canonical_user_id_for_schedule(before):
        logger.warning(
            "A schedule's owner changed, so its worker policy was not moved along and "
            "the schedule waits for review. event=schedule_worker_policy_not_followed "
            "schedule_id=%s source=%s reason=owner_changed",
            before.schedule_id,
            source,
        )
        return False
    moved = worker_records.rebind_execution(
        before.schedule_id,
        team_id,
        expected_digest=old,
        new_digest=new,
        owner_id=owner,
        clear_reasons=_EXECUTION_CHANGE_REASONS,
    )
    if moved:
        logger.info(
            "Moved a scheduled worker's policy along with an authorized schedule change. "
            "event=schedule_worker_policy_followed schedule_id=%s source=%s",
            before.schedule_id,
            source,
        )
    else:
        logger.warning(
            "A schedule changed but its worker policy was already out of date or was "
            "changed meanwhile, so it was left for review. "
            "event=schedule_worker_policy_not_followed schedule_id=%s source=%s "
            "reason=policy_not_current",
            before.schedule_id,
            source,
        )
    return bool(moved)
