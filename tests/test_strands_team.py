import pytest
from custom_tools.providers.changes import apply_change
from custom_tools.providers.access_workflows import (
    get_access_workflow,
    request_access_workflow_approvals,
    submit_access_workflow,
)
from custom_tools.providers.access import ACCESS_TOOLS
from unittest.mock import MagicMock, patch

from custom_agents.agent_instructions import (
    ACCESS_PROMPT_RULE,
    shared_agent_base_prompt,
)
from custom_agents.agent_instantiation_skill import render_agent_instantiation_skill
from custom_agents.runtime_paths import infer_instance_root
from custom_agents.sdk.strands.changes_first import ChangesFirstToolExecutor
from custom_agents.strands_team import create_strands_agent
from strands.tools.mcp import MCPClient
from custom_tools.providers.capabilities import (
    mcp_connections,
    specialist_definitions,
)
from custom_tools.providers.runtime_settings import (
    plan_runtime_setting_change,
)


def test_create_strands_agent_forwards_runtime_config_to_universal_agent(tmp_path) -> None:
    debug_config = {"terminal_reasoning": {"enabled": True}}
    instance_root = tmp_path / "instance"
    bootstrap_config = {
        "canonical_user_id": "user_123",
        "instance_root": str(instance_root),
    }
    schedule_maintenance = {"internal_improvement_review_interval_seconds": 90}

    with patch("custom_agents.strands_agent.Agent") as strands_agent_class, patch(
        "custom_agents.strands_agent.UniversalAgent._configure_model",
        return_value=MagicMock(),
    ):
        agent = create_strands_agent(
            {
                "team_id": "team_test",
                "role": "Manager",
                "goal": "Run the team.",
                "backstory": "Coordinates work for the team.",
                "tools": [],
                "named_agent_type": "TeamArchitect",
                "is_team_lead": True,
                "debug": debug_config,
                "bootstrap_config": bootstrap_config,
                "schedule_maintenance": schedule_maintenance,
                "openai_codex": {"base_url": "https://chatgpt.com/backend-api/codex"},
                "xai_oauth": {"base_url": "https://api.x.ai/v1", "refresh_skew_seconds": 3600},
                "instance_config_path": str(instance_root / "instance_config.yaml"),
                "workspace_dir": str(instance_root / "workspace"),
                "authorization_config": {"enabled": True},
                "nats_url": "nats://transport.test:4222",
                "nats_process_consumer_inactive_threshold_seconds": 77.0,
                "task_history_db_path": str(tmp_path / "task_history.db"),
                "storage_sqlite_path": str(tmp_path / "task_history.db"),
            }
        )

    assert agent.debug == debug_config
    assert agent.bootstrap_config == bootstrap_config
    assert agent.schedule_maintenance == schedule_maintenance
    assert agent.openai_codex["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert agent.xai_oauth["base_url"] == "https://api.x.ai/v1"
    assert infer_instance_root(agent) == str(instance_root.resolve())
    assert not hasattr(agent, "broker_credential_binding_rules")
    assert agent.nats_url == "nats://transport.test:4222"
    assert agent.nats_process_consumer_inactive_threshold_seconds == 77.0
    assert agent._access_decision_hook is not None
    # Record changes run first in each model step (custom_agents.sdk.strands.changes_first).
    executor = strands_agent_class.call_args.kwargs["tool_executor"]
    assert isinstance(executor, ChangesFirstToolExecutor)


def test_create_strands_agent_rejects_direct_mcp_without_instance_registry(
    tmp_path,
) -> None:
    runtime_profile = {
        "name": "skills_mcp",
        "transport": "sse",
        "server_url": "http://127.0.0.1:65535/sse",
        "tool_name_prefix": "skills_mcp",
        "include_tools": ["load_skill"],
    }
    with patch("custom_agents.strands_agent.Agent"), patch(
        "custom_agents.strands_agent.UniversalAgent._configure_model",
        return_value=MagicMock(),
    ):
        agent = create_strands_agent(
            {
                "team_id": "team_mcp_runtime",
                "role": "Specialist",
                "goal": "Use one reviewed MCP operation.",
                "backstory": "MCP runtime integration test.",
                "tools": [],
                "named_agent_type": "MCPRuntimeAgent",
                "external_mcp_servers": [runtime_profile],
                "authorization_config": {"enabled": True},
                "task_history_db_path": str(tmp_path / "task_history.db"),
                "storage_sqlite_path": str(tmp_path / "task_history.db"),
            }
        )

    providers = [tool for tool in agent.tools if isinstance(tool, MCPClient)]
    assert providers == [], "no MCP client is built at startup; servers load before model calls"
    assert agent.mcp_configured_connection_ids == []
    assert not hasattr(agent, "mcp_profiles"), "an agent keeps no copy of the MCP profiles"
    # Brokered MCP calls are decided by the broker's access gate; the agent
    # keeps no MCP rules of its own.
    assert not hasattr(agent, "protected_capability_invocations")
    assert agent._access_decision_hook is not None


def test_create_strands_agent_omits_optional_permission_surface_when_disabled(tmp_path) -> None:
    backstory = f"""{shared_agent_base_prompt}

## Access

- Ask with request_access.

## Protected TeamArchitect Profile

- Stage `team_lead.soul` with `plan_runtime_setting_change` and apply it with
  `apply_change` after conversational agreement.
"""
    with patch("custom_agents.strands_agent.Agent"), patch(
        "custom_agents.strands_agent.UniversalAgent._configure_model",
        return_value=MagicMock(),
    ):
        agent = create_strands_agent(
            {
                "team_id": "team_disabled",
                "role": "Manager",
                "goal": "Run the team.",
                "backstory": backstory,
                "tools": [
                    *ACCESS_TOOLS,
                    mcp_connections,
                    specialist_definitions,
                    plan_runtime_setting_change,
                    apply_change,
                ],
                "named_agent_type": "TeamArchitect",
                "is_team_lead": True,
                "authorization_config": {"enabled": False},
                "task_history_db_path": str(tmp_path / "task_history.db"),
                "storage_sqlite_path": str(tmp_path / "task_history.db"),
            }
        )

    tool_names = {getattr(tool, "__name__", getattr(tool, "name", str(tool))) for tool in agent.tools}
    assert "mcp_connections" in tool_names
    assert "specialist_definitions" in tool_names
    assert "plan_runtime_setting_change" in tool_names
    assert "apply_change" in tool_names
    assert ACCESS_PROMPT_RULE not in agent.backstory
    assert "## Access\n" not in agent.backstory
    assert not {"request_access", "restrict_access", "list_my_access"} & tool_names
    assert "team_lead.soul" in agent.backstory
    assert "request_access" not in agent.system_prompt
    assert agent.openai_codex["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert agent.xai_oauth["base_url"] == "https://api.x.ai/v1"


@pytest.mark.parametrize("enabled", [True, False])
def test_runtime_agent_catalog_lists_default_tools_and_how_access_works(enabled: bool) -> None:
    rendered = render_agent_instantiation_skill(
        team_id="team_catalog",
        agent_type_map={"ResearchAgent": {"default_tools": ["code:python", "google_workspace"]}},
        mcp_profiles={},
        authorization_enabled=enabled,
    )

    [tools_line] = [line for line in rendered.splitlines() if line.startswith("- default tools:")]
    assert "code:python (Python environments)" in tools_line
    assert "google_workspace (Google Workspace kit)" in tools_line
    if enabled:
        assert "Access follows the work" in rendered
        assert "no restart" in rendered
    else:
        assert "Authorization is off: the agent has these tools" in rendered


def test_create_strands_agent_gives_the_team_lead_access_and_planning_tools_when_on(
    tmp_path,
) -> None:
    with patch("custom_agents.strands_agent.Agent"), patch(
        "custom_agents.strands_agent.UniversalAgent._configure_model",
        return_value=MagicMock(),
    ):
        agent = create_strands_agent(
            {
                "team_id": "team_authorization_on",
                "role": "Manager",
                "goal": "Run the team.",
                "backstory": shared_agent_base_prompt,
                "tools": [
                    *ACCESS_TOOLS,
                    mcp_connections,
                    specialist_definitions,
                    plan_runtime_setting_change,
                ],
                "named_agent_type": "TeamArchitect",
                "is_team_lead": True,
                "authorization_config": {"enabled": True},
                "task_history_db_path": str(tmp_path / "task_history.db"),
                "storage_sqlite_path": str(tmp_path / "task_history.db"),
            }
        )

    tool_names = {
        getattr(tool, "__name__", getattr(tool, "name", str(tool)))
        for tool in agent.tools
    }
    # A person approves staged changes in the Inbox (apply_change), so
    # planning stays available next to the access tools.
    assert "mcp_connections" in tool_names
    assert "specialist_definitions" in tool_names
    assert "request_access" in tool_names
    assert "plan_runtime_setting_change" in tool_names


def test_workflow_tools_stay_and_only_the_batch_is_hidden_without_authorization() -> None:
    # The workflow tools are ordinary team-lead tools; only asking people to
    # approve a batch of changes needs authorization on.
    from custom_agents.team_config_helpers import filter_agent_tools

    tools = [submit_access_workflow, get_access_workflow, request_access_workflow_approvals, plan_runtime_setting_change]

    def names(enabled: bool) -> set[str]:
        return {tool.tool_name for tool in filter_agent_tools(tools, authorization_enabled=enabled)}

    assert "request_access_workflow_approvals" in names(True)
    assert "request_access_workflow_approvals" not in names(False)
    assert {"submit_access_workflow", "get_access_workflow"} <= names(False)
