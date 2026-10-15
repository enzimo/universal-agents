"""Validate candidate-list additions without granting launch or tool authority."""

from typing import Any

from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_agents.team_composition import validate_agent_type_name
from runtime_settings.schedule_workers import ScheduleWorkersConfig


def validate_worker_eligibility_change(
    agent: Any, limits: ScheduleWorkersConfig, proposed: list[str]
) -> None:
    """Check additions at planning and apply; stale entries may always be removed.

    A retained entry can have become denied or unavailable. Requiring its removal
    would turn unrelated repairs into policy edits. Every launch checks it again.
    """
    additions = set(proposed) - set(limits.eligible_agent_types)
    runtime = getattr(agent, "_local_team_runtime", None)
    for agent_type in sorted(additions):
        validate_agent_type_name(agent_type)
        if agent_type == TEAM_ARCHITECT_AGENT_TYPE:
            raise PermissionError("automatic_manager_launch_forbidden")
        if agent_type in limits.denied_agent_types:
            raise PermissionError(f"agent_type_disallowed:{agent_type}")
        if runtime is None:
            raise ValueError("local_launcher_unavailable")
        if runtime.resolve_definition(agent_type) is None:
            raise ValueError(f"agent_definition_unavailable:{agent_type}")
