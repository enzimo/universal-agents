"""Pure helper functions for Strands team configuration."""

from __future__ import annotations

from typing import Iterable, Optional

from custom_agents.agent_instructions import MESSAGE_FILL_NAME
from custom_agents.default_tools import (
    DEFAULT_TOOLS_WITH_AUTHORIZATION,
    DEFAULT_TOOLS_WITHOUT_AUTHORIZATION,
    default_tool_labels,
)
from custom_tools.provider_contract import dedupe_tools, tool_name
from custom_tools.providers.access import ACCESS_TOOL_NAMES
from custom_agents.access_workflows import AUTHORIZATION_ONLY_WORKFLOW_TOOL_NAMES

# Tools that make no sense with authorization off (decision D8, trusted
# single-person mode): there is nothing to ask for or hand on. Publishing and
# following an access workflow still works then; only asking a person to
# approve its changes together does not (changes apply after the person agrees
# in conversation, with apply_change and confirmed=true).
HIDDEN_WITHOUT_AUTHORIZATION = ACCESS_TOOL_NAMES | AUTHORIZATION_ONLY_WORKFLOW_TOOL_NAMES

# Team skill install and removal tools (custom_tools.providers.capabilities),
# left out when skill installation is off (``skill_installation.enabled``).
SKILL_INSTALLATION_TOOL_NAMES = frozenset(
    {
        "plan_skill_installation",
        "plan_team_skill_change",
        "get_skill_installation_status",
        "plan_skill_uninstallation",
        "get_skill_uninstallation_status",
    }
)


def filter_agent_tools(
    tools: Iterable[object],
    *,
    authorization_enabled: bool = True,
    skill_installation_enabled: bool = True,
    tool_allowlist: Optional[Iterable[object]] = None,
    tool_blocklist: Optional[Iterable[object]] = None,
) -> list[object]:
    filtered_tools = list(dedupe_tools(tools or []))
    kept = filter_agent_tool_names(
        (tool_name(tool) for tool in filtered_tools),
        authorization_enabled=authorization_enabled,
        skill_installation_enabled=skill_installation_enabled,
        tool_allowlist=tool_allowlist,
        tool_blocklist=tool_blocklist,
    )
    return [tool for tool in filtered_tools if tool_name(tool) in kept]


def filter_agent_tool_names(
    names: Iterable[str],
    *,
    authorization_enabled: bool = True,
    skill_installation_enabled: bool = True,
    tool_allowlist: Optional[Iterable[object]] = None,
    tool_blocklist: Optional[Iterable[object]] = None,
) -> set[str]:
    """The tool names the operator's settings keep (``filter_agent_tools`` by name)."""

    kept = {str(name) for name in names}
    if tool_allowlist is not None:
        kept &= {str(name) for name in (tool_allowlist or [])}
    if tool_blocklist:
        kept -= {str(name) for name in (tool_blocklist or [])}
    if not skill_installation_enabled:
        kept -= SKILL_INSTALLATION_TOOL_NAMES
    if not authorization_enabled:
        kept -= HIDDEN_WITHOUT_AUTHORIZATION
    return kept


def create_agent_description(
    agent_config: dict,
    *,
    authorization_enabled: bool = True,
) -> str:
    """Describe an agent type: role, default tools, and how its access works."""
    display_name = agent_config.get("display_name")
    role = agent_config.get("role", "No role defined")
    description = agent_config.get("description")
    labels = default_tool_labels(agent_config.get("default_tools") or ())
    tools_str = (
        "\n".join(f"  - {label}" for label in labels)
        if labels
        else "  none beyond its role's standard tools"
    )

    lines = []
    if display_name:
        lines.append(f"Display name: {display_name}")
    lines.append(f"Role: {role}")
    if description:
        lines.append(f"Description: {description}")
    lines.append(f"Default tools:\n{tools_str}")
    lines.append(
        DEFAULT_TOOLS_WITH_AUTHORIZATION
        if authorization_enabled
        else DEFAULT_TOOLS_WITHOUT_AUTHORIZATION
    )
    return "\n".join(lines).strip()


def get_all_named_agents_summary(
    config_map: dict,
    *,
    authorization_enabled: bool = True,
) -> dict:
    """Return available named agent types mapped to role and default-tool descriptions."""
    try:
        return {
            agent_type: create_agent_description(
                agent_config=config,
                authorization_enabled=authorization_enabled,
            )
            for agent_type, config in config_map.items()
        }
    except Exception as exc:
        return {"error": f"An error occurred while getting named agent types: {exc}"}


def fill_backstory_template(backstory: str, team_id: str) -> str:
    """Fill stable placeholders in an agent backstory template."""
    return backstory.replace(MESSAGE_FILL_NAME.TEAM_ID, team_id)


def build_system_prompt(
    role: str,
    goal: str,
    backstory: str,
    team_id: str,
) -> str:
    """Build a complete system prompt from role, goal, and backstory."""
    filled_backstory = fill_backstory_template(backstory, team_id)
    return f"""Your Role: {role}

Your Goal: {goal}

{filled_backstory}"""
