"""Explicit offline transition of existing local timers and startup policies."""

from datetime import datetime
from typing import Any

from custom_agents.agent_instantiation_service import (
    current_worker_limits,
    default_type_allowed,
)
from custom_agents.local_schedule_policy import (
    install_schedule_default,
    is_memory_review_timer,
    supports_local_worker,
)
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from custom_messages.contracts.runtime_v1 import (
    MEM_REVIEW_AGENT_TYPE,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_messages.contracts.schedule_worker_v1 import utc_now


def plan_local_schedule_upgrade(agent: Any) -> list[dict[str, Any]]:
    limits = current_worker_limits(agent)
    report = []
    for row in agent._schedule_store.get_all():
        if row.get("claim_team_id") != agent.team_id:
            continue
        schedule = agent._schedule_store.get_scheduled_message(row["schedule_id"])
        memory = is_memory_review_timer(schedule)
        if not memory and not supports_local_worker(schedule):
            continue
        target = MEM_REVIEW_AGENT_TYPE if memory else schedule.claim_named_agent_type
        binding = agent._schedule_store.worker_records.get_binding(
            schedule.schedule_id, team_id=agent.team_id
        )
        reason = _upgrade_reason(
            agent,
            schedule,
            row,
            target,
            limits,
            binding,
        )
        report.append(
            {
                "schedule_id": schedule.schedule_id,
                "target_agent_type": target,
                "move_memory_timer": memory
                and schedule.claim_named_agent_type != MEM_REVIEW_AGENT_TYPE,
                "action": "preserve"
                if reason == "existing_policy_preserved"
                else "blocked"
                if reason
                else "upgrade",
                "reason": reason,
            }
        )
    return report


def _upgrade_reason(
    agent: Any,
    schedule: Any,
    row: dict[str, Any],
    target: str | None,
    limits: Any,
    binding: Any,
) -> str | None:
    """Describe one blocker without mutating or guessing the prior intent."""
    memory = is_memory_review_timer(schedule)
    if memory and schedule.claim_named_agent_type not in {
        TEAM_ARCHITECT_AGENT_TYPE,
        MEM_REVIEW_AGENT_TYPE,
        None,
    }:
        return "custom_memory_target_requires_review"
    if binding is not None:
        if memory and schedule.claim_named_agent_type != MEM_REVIEW_AGENT_TYPE:
            return "existing_memory_policy_requires_review"
        return "existing_policy_preserved"
    if schedule.claim_agent_id:
        return "exact_agent_assignment_preserved"
    if not owner_canonical_user_id_for_schedule(schedule):
        return "schedule_owner_missing"
    if not target or agent._local_team_runtime.resolve_definition(target) is None:
        return "agent_definition_missing"
    if target in limits.denied_agent_types:
        return "agent_type_disallowed"
    if not limits.default_startup_policy_enabled:
        return "default_startup_policy_disabled"
    if not default_type_allowed(
        agent, target, agent._local_team_runtime.resolve_definition(target)
    ):
        return "agent_definition_not_in_default_policy"
    if (
        row.get("claim_token")
        and row.get("claim_expires_at")
        and datetime.fromisoformat(row["claim_expires_at"]) > utc_now()
    ):
        return "active_schedule_claim"
    return None


def apply_local_schedule_upgrade(agent: Any) -> list[dict[str, Any]]:
    report = plan_local_schedule_upgrade(agent)
    if any(item["reason"] == "active_schedule_claim" for item in report):
        raise ValueError("Wait for active schedule claims to finish before upgrading")
    for item in report:
        if item["action"] != "upgrade":
            continue
        old = agent._schedule_store.get_scheduled_message(item["schedule_id"])
        candidate = old.model_copy(deep=True)
        if is_memory_review_timer(old):
            candidate.claim_named_agent_type = MEM_REVIEW_AGENT_TYPE
            if candidate.payload_meta:
                candidate.payload_meta.to_named_agent_type = MEM_REVIEW_AGENT_TYPE
                candidate.payload_meta.to_agent_name = None
        changed = install_schedule_default(
            agent,
            candidate,
            previous_schedule=old,
            require_unclaimed=True,
        )
        item["action"] = "upgraded" if changed else "preserved"
        agent._logger.info(
            "Local schedule upgrade. event=local_schedule_upgraded schedule_id=%s target_agent_type=%s action=%s",
            item["schedule_id"],
            item["target_agent_type"],
            item["action"],
        )
    return report
