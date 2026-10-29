"""Resolve the skills required by an Objective work session."""

from __future__ import annotations

from typing import TYPE_CHECKING
from custom_agents.sdk.strands.skills import list_agent_skill_names
from custom_agents.strands_parts import turn_strands_agent

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent


def missing_objective_work_skills(agent: "UniversalAgent") -> list[str]:
    required_skills = {
        name.casefold(): name
        for name in agent.task_manager.objective_work_config.required_skills
    }
    available_skills = {
        name.casefold()
        for plugin in (getattr(agent, "plugins", None) or [])
        for name in list_agent_skill_names(
            plugin,
            turn_strands_agent(agent),
        )
    }
    return sorted(
        required_skills[key] for key in required_skills.keys() - available_skills
    )
