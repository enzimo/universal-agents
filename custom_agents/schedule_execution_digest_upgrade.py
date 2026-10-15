"""One-time re-save of scheduled-worker fingerprints after run notes left them.

Until 2026-10-04 ``execution_digest`` included the schedule's run-note policy
(``schedule_memory.policy``). Runs create that policy themselves, so schedules
blocked their own next run with ``schedule_execution_changed_review_required``.
The fingerprint no longer includes it (``schedule_execution_identity``), which
changes the saved value of every existing binding.

The owner approved this one-time re-save instead of having every owner confirm
every schedule again. At the team lead's startup it recomputes each binding's
saved value with the old formula; when the old formula reproduces the saved
value for the schedule as it is now (with its current, default or absent
run-note policy), nothing but run notes changed, and the binding is moved to the
new value. Any other difference is a real change and keeps its review block.

Remove this module (and its call in ``schedule_worker_controller``) in the
release after the first one in which it ran; by then every running instance
has started once with it. It was added in 0.8.0 but failed at every start
through 0.9.1 (see ``run_execution_digest_upgrade``), so that first release is
the one carrying that fix.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from custom_agents.schedule_execution_identity import execution_digest
from custom_agents.schedule_repair_guidance import (
    SCHEDULE_EXECUTION_CHANGED,
    SCHEDULE_LAUNCH_INTENT_CHANGED,
)
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from custom_messages.contracts.schedule_v1 import ScheduleMemoryPolicy
from custom_messages.contracts.schedule_worker_v1 import worker_digest

logger = logging.getLogger(__name__)

_CLEARED_REASONS = frozenset({SCHEDULE_EXECUTION_CHANGED, SCHEDULE_LAUNCH_INTENT_CHANGED})


def _old_execution_digest(
    schedule: Any, definition_digest: str, policy: dict[str, Any] | None
) -> str:
    """The fingerprint as computed before 2026-10-04, with a given run-note policy."""

    gateway = getattr(schedule.payload_meta, "gateway_context", None)
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
            "schedule_policy": policy,
        }
    )


def _unchanged_but_for_run_notes(schedule: Any, binding: Any) -> bool:
    policies: list[dict[str, Any] | None] = [
        None,
        ScheduleMemoryPolicy().model_dump(mode="json"),
    ]
    if schedule.schedule_memory is not None:
        policies.append(schedule.schedule_memory.policy.model_dump(mode="json"))
    return any(
        _old_execution_digest(schedule, binding.definition_digest, policy)
        == binding.execution_digest
        for policy in policies
    )


def upgrade_execution_digests(agent: Any) -> tuple[str, ...]:
    """Re-save the fingerprint of every binding that only run notes changed."""

    store = agent._schedule_store
    records = store.worker_records
    moved: list[str] = []
    for binding in records.list_bindings(agent.team_id):
        schedule = store.get_scheduled_message(binding.schedule_id)
        if schedule is None:
            continue
        new = execution_digest(schedule, binding.definition_digest)
        if new == binding.execution_digest or not _unchanged_but_for_run_notes(
            schedule, binding
        ):
            continue
        if records.rebind_execution(
            binding.schedule_id,
            agent.team_id,
            expected_digest=binding.execution_digest,
            new_digest=new,
            owner_id=binding.owner_canonical_user_id,
            clear_reasons=_CLEARED_REASONS,
        ):
            moved.append(binding.schedule_id)
            logger.info(
                "Re-saved a scheduled worker's fingerprint without run notes (one-time "
                "upgrade). event=schedule_execution_digest_upgraded schedule_id=%s "
                "was_blocked=%s",
                binding.schedule_id,
                binding.blocked_reason,
            )
    return tuple(moved)


async def run_execution_digest_upgrade(agent: Any) -> tuple[str, ...]:
    """Run the upgrade at the team lead's start, on its task operation lane.

    The lane deep-copies an operation's arguments, because they must be data
    (``task_operation_runtime``). The live agent is not: it holds running
    asyncio tasks, which cannot be copied. So the agent is bound into the
    operation, as other lane callers do, instead of being passed as an
    argument; passing it made the upgrade fail on every start from 0.8.0
    through 0.9.1.
    """

    moved: tuple[str, ...] = await agent.task_manager.operations_runtime.run(
        partial(upgrade_execution_digests, agent)
    )
    return moved
