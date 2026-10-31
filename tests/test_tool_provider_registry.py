"""Behavioral and structural contracts for the split Strands tool surface."""

from __future__ import annotations

import pytest
from strands import Agent

from custom_agents.agent_catalog import load_agent_catalog
from custom_tools.provider_contract import tool_name
from custom_tools.registry import ALL_PROVIDER_TOOLS, TOOL_PROVIDERS


SHIPPED_AGENT_TYPES = tuple(load_agent_catalog(include_configured=False).agent_types)


def test_tool_providers_have_unique_names_and_tool_ownership() -> None:
    provider_names = [provider.name for provider in TOOL_PROVIDERS]
    owned_tool_names = [
        tool_name(tool) for provider in TOOL_PROVIDERS for tool in provider.tools
    ]

    assert len(provider_names) == len(set(provider_names))
    assert len(owned_tool_names) == len(set(owned_tool_names))
    # give_access was removed: workers get access for each task from TeamLead ("access follows the work").
    # The four worker pool tools (fn:worker_pools) were added on 2026-10-04.
    # The fourteen memory and topic tag functions became memory and topic_tags on 2026-10-10,
    # the eleven capability functions became four action tools, and thirteen
    # Objective functions became objectives, objective_work_sessions and objective_work_profiles.
    # The knowledge base adds six operations and the curator adds twelve
    # (merging pages and lifting a freeze were added on 2026-10-11).
    # TeamLead's history overview and clean-up plan were added on 2026-10-27.
    assert len(ALL_PROVIDER_TOOLS) == 172


def test_agent_catalog_does_not_reexport_tool_registry_ownership() -> None:
    from custom_agents import agent_catalog

    assert not hasattr(agent_catalog, "TOOL_PROVIDERS")


@pytest.mark.parametrize("agent_type", SHIPPED_AGENT_TYPES)
def test_agent_tool_names_match_the_operations_registered_by_strands(agent_type):
    tools = load_agent_catalog(include_configured=False).agent_types[agent_type].tools
    sdk = Agent(tools=list(tools), callback_handler=None)
    assert set(sdk.tool_names) == {tool_name(item) for item in tools}


def test_provider_rejects_module_with_multiple_operations():
    from custom_tools import exa_tools
    from custom_tools.provider_contract import ToolProvider

    with pytest.raises(ValueError, match="concrete Strands operations"):
        ToolProvider("ambiguous", (exa_tools,))
