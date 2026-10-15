from custom_messages.contracts.runtime_v1 import (
    INTERNAL_IMPROVEMENT_AGENT_TYPE,
    PERSONAL_ADMIN_AGENT_TYPE,
    RESEARCH_COMPARISON_AGENT_TYPE,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_agents.strands_team import AGENT_TYPE_MAP


def _agent_tool_names() -> dict[str, set[str]]:
    return {
        agent_type: {
            str(getattr(tool, "__name__", getattr(tool, "name", str(tool))))
            for tool in config.get("tools", [])
        }
        for agent_type, config in AGENT_TYPE_MAP.items()
    }


def test_catalog_contains_practical_agents_and_removes_old_engineering_roles():
    assert PERSONAL_ADMIN_AGENT_TYPE in AGENT_TYPE_MAP
    assert RESEARCH_COMPARISON_AGENT_TYPE in AGENT_TYPE_MAP
    assert INTERNAL_IMPROVEMENT_AGENT_TYPE in AGENT_TYPE_MAP
    assert "CodebaseAnalysisAgent" not in AGENT_TYPE_MAP
    assert "IntegrationAgent" not in AGENT_TYPE_MAP
    assert "TestVerificationAgent" not in AGENT_TYPE_MAP


def test_specialists_do_not_expose_shared_conversation_or_memory_tools():
    tools_by_agent = _agent_tool_names()
    shared_state_tools = {
        "get_conversation_context_slice",
        "get_conversation_summary",
        "get_conversation_turns",
        "list_conversations",
    }

    assert shared_state_tools.issubset(tools_by_agent[TEAM_ARCHITECT_AGENT_TYPE])
    for agent_type, tool_names in tools_by_agent.items():
        if agent_type == TEAM_ARCHITECT_AGENT_TYPE:
            continue
        assert tool_names.isdisjoint(shared_state_tools), agent_type


def test_internal_improvement_agent_can_build_rust_tools_with_shell_access():
    tool_names = _agent_tool_names()[INTERNAL_IMPROVEMENT_AGENT_TYPE]

    assert "shell" in tool_names
    assert "file_write" in tool_names
    assert "python_env" in tool_names
