from pathlib import Path

import pytest

from custom_agents.agent_catalog import (
    get_onboarding_agent_catalog,
    load_agent_catalog,
    load_agent_type_map,
)


def test_builtin_agent_catalog_hydrates_runtime_and_onboarding_roles():
    agent_map = load_agent_type_map(include_configured=False)
    onboarding_types = {
        agent["type"]
        for agent in get_onboarding_agent_catalog(include_configured=False)
    }
    internal_tool_names = {
        getattr(tool, "__name__", getattr(tool, "name", str(tool)))
        for tool in agent_map["InternalImprovementAgent"]["tools"]
    }
    memreview_tool_names = {
        getattr(tool, "__name__", getattr(tool, "name", str(tool)))
        for tool in agent_map["MemReviewAgent"]["tools"]
    }
    manager_tool_names = {
        getattr(tool, "__name__", getattr(tool, "name", str(tool)))
        for tool in agent_map["TeamArchitect"]["tools"]
    }
    catalog = load_agent_catalog(include_configured=False)
    onboarding_by_type = {
        agent["type"]: agent
        for agent in get_onboarding_agent_catalog(include_configured=False)
    }

    assert "PersonalAdminAgent" in agent_map
    assert "ResearchComparisonAgent" in agent_map
    assert "InternalImprovementAgent" in agent_map
    assert "lookup_model_capability_candidates" in internal_tool_names
    assert "list_scheduled_jobs" in internal_tool_names
    assert "update_scheduled_job" in internal_tool_names
    assert "remove_scheduled_job" in internal_tool_names
    # Schedule status is every agent's own runtime tool; changing schedules is a default tool.
    assert "list_scheduled_jobs" in memreview_tool_names
    assert "update_scheduled_job" not in memreview_tool_names
    # Every agent holds the access tools; none holds the old permission tools.
    access_tools = {"list_my_access", "find_tools", "request_access", "restrict_access", "revoke_access"}
    for names in (internal_tool_names, memreview_tool_names, manager_tool_names):
        assert access_tools <= names
        assert not {"request_permission", "review_permission_request", "decide_permission_request"} & names
    assert "update_team_lead_soul" not in internal_tool_names
    assert "update_team_lead_soul" not in manager_tool_names
    assert "create_agent_definition_from_template" not in manager_tool_names
    assert "specialist_definitions" in manager_tool_names
    assert "request_generated_agent_definition_authorization" not in manager_tool_names
    assert "apply_generated_agent_definition" not in manager_tool_names
    assert "plan_runtime_setting_change" in manager_tool_names
    assert "apply_change" in manager_tool_names
    assert {
        "start_solution_variation",
        "record_solution_candidate",
        "record_solution_evaluation",
        "inspect_solution_variation",
        "select_solution_candidate",
        "close_solution_variation",
    } <= manager_tool_names
    assert onboarding_by_type["PersonalAdminAgent"]["default_enabled"] is False
    assert (
        catalog.agent_types["ResearchComparisonAgent"].onboarding.default_enabled
        is False
    )
    assert onboarding_by_type["InternalImprovementAgent"]["default_enabled"] is False
    assert onboarding_by_type["InternalImprovementAgent"]["default_count"] == 1
    assert onboarding_by_type["MemReviewAgent"]["default_enabled"] is True
    assert onboarding_by_type["MemReviewAgent"]["default_count"] == 1
    assert "CodebaseAnalysisAgent" not in agent_map
    assert "IntegrationAgent" not in agent_map
    assert "TestVerificationAgent" not in agent_map
    assert "PersonalAdminAgent" in onboarding_types
    assert "ResearchComparisonAgent" not in onboarding_types
    assert "WebResearchAgent" in onboarding_types
    assert 'notify_user(target="auto")' in agent_map["TeamArchitect"]["goal"]
    assert (
        "do not emit an early terminal `task_response`"
        in agent_map["TeamArchitect"]["goal"]
    )
    assert (
        "multi-option research and comparison requests as complex work by default"
        in agent_map["TeamArchitect"]["goal"]
    )
    assert (
        "opening phase inventories the specialist agents"
        in agent_map["TeamArchitect"]["goal"]
    )
    assert (
        "you never start workers for delegated work"
        in agent_map["TeamArchitect"]["goal"]
    )
    assert (
        "you may open a small bounded comparison with the solution-variation tools"
        in agent_map["TeamArchitect"]["goal"]
    )
    assert "Selection is an advisory recommendation" in agent_map["TeamArchitect"]["goal"]


def test_external_agent_catalog_can_add_curated_agent(tmp_path: Path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text(
        "# Custom Agent\n\nReturn practical custom outputs.", encoding="utf-8"
    )
    catalog = tmp_path / "agents.yaml"
    catalog.write_text(
        """
schema_version: 1
agent_types:
  CustomPlannerAgent:
    display_name: "Custom Planner Agent"
    role: "Custom planning assistant"
    description: "Creates plans from an external catalog."
    prompt_ref: "prompt.md"
    backstory: "hub_and_spoke"
    default_tools:
      - "fn:publishing"
    onboarding:
      visible: true
      default_enabled: true
      default_count: 1
      summary: "Custom planning"
      description: "External catalog role."
""",
        encoding="utf-8",
    )

    hydrated = load_agent_catalog([catalog], include_configured=False)

    assert "CustomPlannerAgent" in hydrated.agent_types
    assert (
        hydrated.agent_types["CustomPlannerAgent"].display_name
        == "Custom Planner Agent"
    )
    assert any(
        agent["type"] == "CustomPlannerAgent" for agent in hydrated.onboarding_agents()
    )


def test_external_agent_bundle_directory_can_add_curated_agent(tmp_path: Path):
    bundle = tmp_path / "custom_bundle"
    prompt_dir = bundle / "prompts"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "custom.md").write_text("# Custom Bundle Agent", encoding="utf-8")
    (bundle / "agent.yaml").write_text(
        """
schema_version: 1
agent_types:
  BundlePlannerAgent:
    display_name: "Bundle Planner Agent"
    role: "Bundle planning assistant"
    description: "Creates plans from a bundle."
    prompt_ref: "prompts/custom.md"
    default_tools:
      - "fn:publishing"
""",
        encoding="utf-8",
    )

    hydrated = load_agent_catalog([bundle], include_configured=False)

    assert "BundlePlannerAgent" in hydrated.agent_types
    assert (
        hydrated.agent_types["BundlePlannerAgent"].display_name
        == "Bundle Planner Agent"
    )


def test_agent_template_rejects_missing_required_param(tmp_path: Path):
    catalog = tmp_path / "agents.yaml"
    catalog.write_text(
        """
schema_version: 1
agent_templates:
  DomainCoach:
    parameters:
      domain: {type: string}
    definition:
      display_name: "{{domain}} Coach"
      role: "{{domain}} coach"
      description: "Coach."
      prompt: "Coach {{domain}}."
      default_tools:
        - "fn:publishing"
agent_types:
  BrokenCoachAgent:
    extends: "DomainCoach"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required param 'domain'"):
        load_agent_catalog([catalog], include_configured=False)


def test_agent_template_rejects_unknown_template(tmp_path: Path):
    catalog = tmp_path / "agents.yaml"
    catalog.write_text(
        """
schema_version: 1
agent_types:
  BrokenAgent:
    extends: "MissingTemplate"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="extends unknown template"):
        load_agent_catalog([catalog], include_configured=False)


def _write_agent(tmp_path: Path, tools_yaml: str) -> Path:
    (tmp_path / "prompt.md").write_text("# Agent", encoding="utf-8")
    catalog = tmp_path / "agents.yaml"
    catalog.write_text(
        f"""
schema_version: 1
agent_types:
  ToolsAgent:
    display_name: "Tools Agent"
    role: "Tools role"
    description: "Checks default tools."
    prompt_ref: "prompt.md"
{tools_yaml}
""",
        encoding="utf-8",
    )
    return catalog


def test_unknown_default_tool_is_refused_naming_it(tmp_path: Path):
    catalog = _write_agent(tmp_path, '    default_tools:\n      - "arbitrary_python_import"\n')

    with pytest.raises(ValueError, match="ToolsAgent lists unknown default tool 'arbitrary_python_import'"):
        load_agent_catalog([catalog], include_configured=False)


@pytest.mark.parametrize("field", ["tool_capabilities", "capability_registry_ids"])
def test_removed_tool_group_fields_are_refused_with_the_fix(tmp_path: Path, field: str):
    catalog = _write_agent(tmp_path, f'    {field}:\n      - "learning_coaching"\n')

    with pytest.raises(ValueError, match=f"ToolsAgent in .* uses the removed field\\(s\\) {field}.*default_tools"):
        load_agent_catalog([catalog], include_configured=False)


def test_default_tools_add_their_functions_to_the_roles_standard_tools(tmp_path: Path):
    catalog = _write_agent(tmp_path, '    default_tools:\n      - "code:python"\n      - "google_workspace"\n')

    agent = load_agent_catalog([catalog], include_configured=False).agent_types["ToolsAgent"]
    names = {getattr(tool, "tool_name", getattr(tool, "__name__", "")) for tool in agent.tools}
    runtime = agent.to_runtime_config()

    # Declared ids are kept as written (a tool set stays a set id).
    assert agent.default_tools == ("code:python", "google_workspace")
    assert runtime["default_tools"] == ["code:python", "google_workspace"]
    assert runtime["declared_definition"]["default_tools"] == ["code:python", "google_workspace"]
    # code:python -> python_env; a command-line kit -> the broker dispatcher.
    assert {"python_env", "secure_cli"} <= names
    # The worker's standard sets: its own runtime tools and internet access.
    assert {"file_read", "list_task_queue", "request_access", "exa_search"} <= names
    # Not the team lead's tools, nor tools it did not list.
    assert not {"instantiate_agent", "apply_change", "shell", "publish_single_page_site"} & names


def test_an_agent_without_default_tools_has_only_its_roles_standard_tools(tmp_path: Path):
    catalog = _write_agent(tmp_path, "")

    agent = load_agent_catalog([catalog], include_configured=False).agent_types["ToolsAgent"]
    names = {getattr(tool, "tool_name", getattr(tool, "__name__", "")) for tool in agent.tools}

    assert agent.default_tools == ()
    assert {"file_read", "request_access", "exa_search"} <= names
    assert not {"python_env", "secure_cli", "publish_single_page_site"} & names


def test_external_agent_catalog_rejects_template_unknown_agent_type(tmp_path: Path):
    catalog = tmp_path / "agents.yaml"
    catalog.write_text(
        """
schema_version: 1
team_templates:
  broken_team:
    description: "Broken template."
    members:
      - agent_type: "MissingAgent"
        count: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="broken_team.*MissingAgent"):
        load_agent_catalog([catalog], include_configured=False)
