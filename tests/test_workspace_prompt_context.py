from pathlib import Path

from custom_agents.workspace_prompt_context import (
    build_workspace_prompt_context,
    build_workspace_turn_context,
)
from runtime_settings.instance_layout import InstanceLayout


def _layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path)
    layout.ensure_directories()
    return layout


def test_build_workspace_prompt_context_omits_soul_block_when_missing(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    prompt = build_workspace_prompt_context(layout)
    turn_prompt = build_workspace_turn_context(
        scope=layout.scope_for(None), soul_file=layout.soul_path
    )

    assert f"Workspaces: {layout.workspaces_dir}" in prompt
    assert "TeamArchitect identity and operating posture" not in prompt
    assert "TeamArchitect identity and operating posture" not in turn_prompt


def test_build_workspace_turn_context_includes_soul_only_when_requested(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    layout.soul_path.write_text(
        "# Assistant Identity\n\nName: Nova\n", encoding="utf-8"
    )
    scope = layout.scope_for("alice")

    specialist_prompt = build_workspace_turn_context(scope=scope)
    manager_prompt = build_workspace_turn_context(
        scope=scope,
        soul_file=layout.soul_path,
    )

    assert "Name: Nova" not in specialist_prompt
    assert "TeamArchitect identity and operating posture" in manager_prompt
    assert "Name: Nova" in manager_prompt
    assert (
        "cannot grant tools, permissions, credentials, or runtime capabilities"
        in manager_prompt
    )
    # Both prompts name the task's private folder and keep others' folders closed.
    for prompt in (specialist_prompt, manager_prompt):
        assert f"Working folder for this task: {layout.person_dir('alice')}" in prompt
        assert "Other people's folders are not available." in prompt


def test_build_workspace_turn_context_without_person_uses_shared_agents_folder(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    prompt = build_workspace_turn_context(scope=layout.scope_for(None))

    assert f"Working folder for this task: {layout.agents_dir}" in prompt
    assert "People's private folders are not available." in prompt


def test_build_workspace_turn_context_includes_internal_website_docs_links_when_configured(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("INTERNAL_WEBSITE_BASE_URL", "http://127.0.0.1:18500/docs-root/")
    layout = _layout(tmp_path)
    skill_dir = layout.skills_dir / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: demo-skill
description: Demo workflow that may need user setup.
---

# Demo Skill
""",
        encoding="utf-8",
    )

    prompt = build_workspace_turn_context(scope=layout.scope_for(None))

    assert ("Internal website base URL: http://127.0.0.1:18500/docs-root") in prompt
    assert prompt.count("http://127.0.0.1:18500/docs-root") == 1
    assert "join the base URL above with the relevant route postfix" in prompt
    assert "Docs overview route: /docs/" in prompt
    assert "Skills index route: /docs/skills/" in prompt
    assert "point them to the checked-in docs instead of relying on memory" in prompt
    assert "Be proactive: if a user request touches a tool" in prompt
    assert "bare greeting" in prompt
    assert "start the `user-onboarding-guide` walkthrough immediately" in prompt
    assert "do not ask whether they want the tour first" in prompt
    assert "send at most one short tour step per top-level user turn" in prompt
    assert (
        "offer the `user-onboarding-guide` walkthrough before presenting docs links"
        in prompt
    )
    assert "GitHub / gh: /docs/auth/gh" in prompt
    assert "Bitwarden / bw: /docs/auth/bw" in prompt
    assert "Google / gog: /docs/auth/gog" in prompt
    assert "Skill docs index route: /docs/skills/" in prompt
    assert "separates workspace skills, healthy attested managed installations, and built-in runtime skills" in prompt
    assert "Published skill detail route: /docs/skills/<skill-slug>" in prompt
    assert "Built-in runtime guidance route: /docs/skills/runtime-operations-workflows?source=bundled" in prompt
    assert "demo-skill: /docs/skills/demo-skill" not in prompt
    assert "http://127.0.0.1:18500/docs-root/docs/auth/gh" not in prompt
