from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from strands import AgentSkills, Skill
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment
from strands.types.tools import ToolContext

from custom_agents.sdk.strands.skills import (
    PromptBoundedAgentSkills,
    build_skills_plugin,
    get_skills_fingerprint,
    refresh_skills_plugin,
    resolve_skill_sources,
)
from custom_agents.strands_team import (
    XURL_SKILL_DIR,
    HF_CLI_SKILL_DIR,
    CONVERT_DOCUMENTS_TO_MARKDOWN_SKILL_DIR,
    RUNTIME_OPERATIONS_WORKFLOWS_SKILL_DIR,
    SECURE_CREDENTIAL_WORKFLOWS_SKILL_DIR,
    SHELL_EXECUTION_WORKFLOWS_SKILL_DIR,
    LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR,
    build_agent_skill_directories,
    build_standard_skill_directories,
)


class _LocalSkillAgent:
    def __init__(self) -> None:
        self.sandbox = NotASandboxLocalEnvironment()
        values: dict[str, object] = {}
        self.state = SimpleNamespace(
            get=values.get,
            set=lambda key, value: values.__setitem__(key, value),
        )


def test_standard_skill_directories_include_secure_credential_workflows_when_enabled() -> (
    None
):
    directories = build_standard_skill_directories(
        ["/tmp/custom-skill"], skills_enabled=True
    )

    assert directories == [
        "/tmp/custom-skill",
        str(XURL_SKILL_DIR),
        str(HF_CLI_SKILL_DIR),
        str(SECURE_CREDENTIAL_WORKFLOWS_SKILL_DIR),
        str(RUNTIME_OPERATIONS_WORKFLOWS_SKILL_DIR),
        str(SHELL_EXECUTION_WORKFLOWS_SKILL_DIR),
        str(CONVERT_DOCUMENTS_TO_MARKDOWN_SKILL_DIR),
    ]
    assert (SECURE_CREDENTIAL_WORKFLOWS_SKILL_DIR / "SKILL.md").is_file()
    assert (RUNTIME_OPERATIONS_WORKFLOWS_SKILL_DIR / "SKILL.md").is_file()
    assert (SHELL_EXECUTION_WORKFLOWS_SKILL_DIR / "SKILL.md").is_file()
    assert (CONVERT_DOCUMENTS_TO_MARKDOWN_SKILL_DIR / "SKILL.md").is_file()


def test_team_architect_gets_bundled_long_horizon_skill_by_default() -> None:
    directories = build_agent_skill_directories(
        [],
        skills_enabled=True,
        named_agent_type="TeamArchitect",
    )

    assert str(LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR) in directories
    assert (LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR / "SKILL.md").is_file()
    assert (
        len(list((LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR / "references").glob("*.md")))
        == 6
    )
    skill_text = (LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR / "SKILL.md").read_text(
        encoding="utf-8"
    )
    runtime_mapping = (
        LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR
        / "references"
        / "universal-agents-runtime-mapping.md"
    ).read_text(encoding="utf-8")
    assert "## Compare Alternatives Selectively" in skill_text
    assert "## Use solution variation only when it helps" in runtime_mapping

    specialist_directories = build_agent_skill_directories(
        [],
        skills_enabled=True,
        named_agent_type="Developer",
    )
    assert str(LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR) not in specialist_directories


@pytest.mark.asyncio
async def test_external_long_horizon_skill_replaces_bundled_fallback(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    external_root = tmp_path / "skills"
    external_skill_dir = external_root / "long-horizon-plan-execution"
    external_skill_dir.mkdir(parents=True)
    (external_skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: long-horizon-plan-execution\n"
        "description: Canonical external objective workflow.\n"
        "---\n\n"
        "# Canonical external workflow\n",
        encoding="utf-8",
    )
    caplog.set_level("INFO", logger="custom_agents.sdk.strands.skills")

    sources = resolve_skill_sources(
        skills_dir=str(external_root),
        additional_skill_directories=[str(LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR)],
    )
    plugin = build_skills_plugin(
        skills_dir=str(external_root),
        additional_skill_directories=[str(LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR)],
        strict=True,
    )
    agent = _LocalSkillAgent()
    await plugin.init_agent(agent)  # type: ignore[arg-type]

    available = plugin.get_available_skills(agent)
    assert sources == [str(external_skill_dir.resolve())]
    assert len(available) == 1
    assert available[0].name == "long-horizon-plan-execution"
    assert available[0].path == external_skill_dir.resolve()
    assert "event=bundled_skill_shadowed" in caplog.text

    previous_fingerprint = get_skills_fingerprint(
        skills_dir=str(external_root),
        additional_skill_directories=[str(LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR)],
    )
    (external_skill_dir / "SKILL.md").unlink()
    external_skill_dir.rmdir()
    current_fingerprint = refresh_skills_plugin(
        plugin,
        skills_dir=str(external_root),
        additional_skill_directories=[str(LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR)],
        previous_fingerprint=previous_fingerprint,
    )
    await plugin.init_agent(agent)  # type: ignore[arg-type]

    available = plugin.get_available_skills(agent)
    assert current_fingerprint != previous_fingerprint
    assert len(available) == 1
    assert available[0].path == LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR.resolve()


def test_bundled_long_horizon_skill_remains_when_external_copy_is_absent(
    tmp_path,
) -> None:
    external_root = tmp_path / "skills"
    external_root.mkdir()

    sources = resolve_skill_sources(
        skills_dir=str(external_root),
        additional_skill_directories=[str(LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR)],
    )

    assert str(LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR.resolve()) in sources


@pytest.mark.asyncio
async def test_standard_runtime_skills_load_through_strands_deferred_filesystem_path() -> (
    None
):
    directories = build_standard_skill_directories([], skills_enabled=True)
    plugin = AgentSkills(skills=directories, strict=True)
    agent = _LocalSkillAgent()

    await plugin.init_agent(agent)  # type: ignore[arg-type]

    assert {skill.name for skill in plugin.get_available_skills(agent)} == {
        "xurl",
        "hf-cli",
        "runtime-operations-workflows",
        "secure-credential-workflows",
        "shell-execution-workflows",
        "convert-documents-to-markdown",
    }


@pytest.mark.asyncio
async def test_bundled_runtime_skill_returns_complete_instructions_without_package_path() -> (
    None
):
    skill = Skill.from_file(RUNTIME_OPERATIONS_WORKFLOWS_SKILL_DIR, strict=True)
    plugin = PromptBoundedAgentSkills(skills=[skill], strict=True)
    agent = _LocalSkillAgent()
    await plugin.init_agent(agent)  # type: ignore[arg-type]

    prompt_xml = plugin._generate_skills_xml(agent)  # type: ignore[arg-type]
    activated = await plugin._format_skill_response(skill, agent.sandbox)

    assert "## Runtime Settings Changes" in activated
    assert "Bundled instructions: complete" in activated
    assert str(RUNTIME_OPERATIONS_WORKFLOWS_SKILL_DIR) not in activated
    assert str(RUNTIME_OPERATIONS_WORKFLOWS_SKILL_DIR) not in prompt_xml
    assert "do not read a package path with shell" in prompt_xml


@pytest.mark.asyncio
async def test_workspace_skill_keeps_its_model_visible_location(tmp_path) -> None:
    skill_dir = tmp_path / "workspace-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: workspace-skill\ndescription: Workspace workflow.\n---\n\n# Workspace\n",
        encoding="utf-8",
    )
    skill = Skill.from_file(skill_dir, strict=True)
    plugin = PromptBoundedAgentSkills(skills=[skill], strict=True)
    agent = _LocalSkillAgent()
    await plugin.init_agent(agent)  # type: ignore[arg-type]

    prompt_xml = plugin._generate_skills_xml(agent)  # type: ignore[arg-type]
    activated = await plugin._format_skill_response(skill, agent.sandbox)

    assert str(skill_dir / "SKILL.md") in prompt_xml
    assert f"Location: {skill_dir / 'SKILL.md'}" in activated


@pytest.mark.asyncio
async def test_deferred_filesystem_exclusion_does_not_wipe_available_skills(
    tmp_path,
) -> None:
    for name in ("visible-skill", "hidden-skill"):
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} description\n---\n\n# {name}\n",
            encoding="utf-8",
        )
    plugin = build_skills_plugin(
        skills_dir=str(tmp_path),
        excluded_skill_names=["hidden-skill"],
        prompt_skill_description_limit=50,
        strict=True,
    )
    agent = _LocalSkillAgent()

    await plugin.init_agent(agent)  # type: ignore[arg-type]
    prompt_xml = plugin._generate_skills_xml(agent)  # type: ignore[arg-type]

    assert "visible-skill" in prompt_xml
    assert "hidden-skill" not in prompt_xml
    assert {skill.name for skill in plugin.get_available_skills(agent)} == {
        "visible-skill",
        "hidden-skill",
    }


@pytest.mark.asyncio
async def test_filesystem_skill_added_mid_invocation_is_immediately_activatable(
    tmp_path,
) -> None:
    plugin = build_skills_plugin(
        skills_dir=str(tmp_path),
        prompt_skill_description_limit=50,
        strict=True,
    )
    agent = _LocalSkillAgent()
    await plugin.init_agent(agent)  # type: ignore[arg-type]
    assert plugin.get_available_skills(agent) == []

    skill_dir = tmp_path / "prototype"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: prototype\n"
        "description: Build and review one interface prototype.\n"
        "---\n\n"
        "# Prototype workflow\n\n"
        "Create a focused prototype and validate its interaction flow.\n",
        encoding="utf-8",
    )
    refresh_skills_plugin(
        plugin,
        skills_dir=str(tmp_path),
        force=True,
    )

    context = ToolContext(
        tool_use={
            "toolUseId": "skill-use-immediate-after-install",
            "name": "skills",
            "input": {"skill_name": "prototype"},
        },
        agent=agent,  # type: ignore[arg-type]
        invocation_state={},
    )
    activated = await plugin.skills._tool_func(  # type: ignore[attr-defined]
        "prototype",
        context,
    )

    assert "# Prototype workflow" in activated
    assert "Skill 'prototype' not found" not in activated
    assert {skill.name for skill in plugin.get_available_skills(agent)} == {"prototype"}


@pytest.mark.asyncio
async def test_managed_skill_mutation_eagerly_reloads_presence_capabilities(
    tmp_path,
) -> None:
    for name in ("prototype", "runtime-operations"):
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} workflow.\n---\n\n# {name}\n",
            encoding="utf-8",
        )
    plugin = build_skills_plugin(skills_dir=str(tmp_path), strict=True)
    sdk_agent = _LocalSkillAgent()
    await plugin.init_agent(sdk_agent)  # type: ignore[arg-type]

    (tmp_path / "prototype" / "SKILL.md").unlink()
    (tmp_path / "prototype").rmdir()
    refresh_skills_plugin(plugin, skills_dir=str(tmp_path), force=True)
    assert plugin.get_available_skills(sdk_agent) == []

    published_statuses: list[bool] = []
    refreshed_state: list[bool] = []

    async def publish_presence(*, is_exiting: bool) -> None:
        published_statuses.append(is_exiting)

    runtime = SimpleNamespace(
        _strands_agent=sdk_agent,
        plugins=[plugin],
        _skills_fingerprint=None,
        _refresh_self_agent_state=AsyncMock(
            side_effect=lambda: refreshed_state.append(True)
        ),
        _publish_presence_status=publish_presence,
        team_id="team_skills",
        _runtime_loop=object(),
        _logger=__import__("logging").getLogger(__name__),
        id="agent_skills",
    )
    runtime._refresh_skills_plugins = lambda force=False: setattr(
        runtime,
        "_skills_fingerprint",
        refresh_skills_plugin(
            plugin,
            skills_dir=str(tmp_path),
            force=force,
            previous_fingerprint=runtime._skills_fingerprint,
        ),
    )
    from custom_agents.team_skill_changes import reload_agent_skills

    await reload_agent_skills(runtime)

    assert {skill.name for skill in plugin.get_available_skills(sdk_agent)} == {
        "runtime-operations"
    }
    assert refreshed_state == [True]
    assert published_statuses == [False]
