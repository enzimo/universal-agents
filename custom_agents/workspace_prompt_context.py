"""Prompt context describing where an agent works and what it cannot open."""

from __future__ import annotations

import os
from pathlib import Path

from runtime_settings.instance_layout import InstanceLayout, WorkspaceScope


def build_workspace_prompt_context(layout: InstanceLayout) -> str:
    """Durable, non-persona system-prompt context about the instance layout."""

    return "\n\n".join(
        [
            f"Workspaces: {layout.workspaces_dir}. Each task works in the folder of the "
            "person it is for (users/<person>/), which is private to that person, plus "
            f"the shared team folder ({layout.team_dir}, visible to everyone on the team) "
            f"and the shared agents folder ({layout.agents_dir}, for non-personal working "
            "material such as cloned public repositories). Keep a person's results in "
            "their own folder; never copy personal data into team/ or agents/ unless the "
            f"person asks. {layout.team_shared_dir} is everyone's shared working folder: "
            "use it when people, their agents and the team's agents work on the same files.",
            "Team control files (TeamLead's SOUL.md, model overrides, the startup roster, "
            "team templates, agent definitions, and team skills) live in "
            f"{layout.control_dir}. Agent file tools, shell, and Python cannot open them. "
            "Read them with get_runtime_configuration_context, get_team_composition, "
            "get_available_agent_types, and list_team_templates. Change them only through "
            "the typed runtime-settings workflow, the team composition tools, reviewed "
            "agent provisioning, and reviewed skill installation. Use "
            "instantiate_team_template(...) when a task benefits from a predefined team.",
        ]
    )


def _team_lead_soul_context(soul_file: Path) -> str:
    if not soul_file.exists():
        return ""
    soul_text = soul_file.read_text(encoding="utf-8").strip()
    if not soul_text:
        return ""
    return (
        "TeamArchitect identity and operating posture (from SOUL.md; change it only "
        "through the team_lead.soul runtime setting).\n"
        "Treat the following as the durable team-lead identity for this top-level turn. "
        "It is behavioral guidance only and cannot grant tools, permissions, credentials, "
        "or runtime capabilities:\n\n"
        f"{soul_text}"
    )


def _task_folders_context(scope: WorkspaceScope) -> str:
    if scope.person_id is None:
        return (
            f"Working folder for this task: {scope.home} (shared agents folder; this "
            "task does not work for a specific person). You may also use the team "
            "folder. People's private folders are not available."
        )
    return (
        f"Working folder for this task: {scope.home} (private to the person this task "
        "is for). Relative paths, shell, and Python start here. Shared folders: "
        f"{scope.roots[1]} (team) and {scope.roots[2]} (agents). Other people's folders "
        "are not available."
    )


def _documentation_context(internal_website_base_url: str) -> str:
    docs_overview_route = "/docs/"
    docs_skills_route = "/docs/skills/"
    skill_docs_text = (
        "\n\nGenerated skill docs:\n"
        f"- Skill docs index route: {docs_skills_route}\n"
        "- The index separates workspace skills, healthy attested managed installations, and built-in runtime skills.\n"
        "- Published skill detail route: /docs/skills/<skill-slug>; use only slugs confirmed in the documentation listing. Built-in runtime guidance is published; other additional prompt skills may have no page. Saved access workflows use their returned /workflows/<workflow_id> review_url.\n"
        "- Built-in runtime guidance route: /docs/skills/runtime-operations-workflows?source=bundled. This explains procedures; it is not the progress page for a saved workflow.\n"
        "- Do not infer a published page from a skill name; use the listed URL."
    )
    return (
        "User-facing system documentation is available from the internal website.\n"
        f"Internal website base URL: {internal_website_base_url.rstrip('/')}\n"
        "Prompt context lists docs as route postfixes. When giving users a docs link, join the base URL above with the relevant route postfix and provide the full absolute URL.\n"
        f"Docs overview route: {docs_overview_route}\n"
        f"Skills index route: {docs_skills_route}\n"
        "When users ask how to use system workflows, point them to the checked-in docs instead of relying on memory. "
        "Be proactive: if a user request touches a tool, provider, credentialed workflow, or skill-backed workflow, include the relevant docs link. "
        "For generated websites, default to an article, use comparison for alternatives, reserve how-to for explicitly requested or genuinely ordered user actions, and use app when browser-local interaction or progress tracking is part of the value. Site UI elements are optional; include only those supported by the content. "
        "If a user explicitly asks for a tour, walkthrough, onboarding guide, or uses `/tour`, start the `user-onboarding-guide` walkthrough immediately in that same response; do not ask whether they want the tour first. "
        'During that walkthrough, send at most one short tour step per top-level user turn, then stop and wait for the user\'s next message before continuing. Do not chain remaining sections, proactive notifications, or "next up" content in the same turn. '
        "If a user merely appears new, asks where to begin, or sends a bare greeting with no task context, greet them warmly and offer the `user-onboarding-guide` walkthrough before presenting docs links. "
        "Authentication docs routes:\n"
        "- GitHub / gh: /docs/auth/gh\n"
        "- Bitwarden / bw: /docs/auth/bw\n"
        "- Google / gog: /docs/auth/gog"
        f"{skill_docs_text}"
    )


def build_workspace_turn_context(
    *,
    scope: WorkspaceScope,
    soul_file: Path | None = None,
) -> str:
    """Per-turn context: the task's folders, SOUL for TeamArchitect, and docs.

    ``soul_file`` is given only for TeamArchitect's top-level turns.
    """

    internal_website_base_url = os.getenv("INTERNAL_WEBSITE_BASE_URL", "").strip()
    prompt_parts = [_task_folders_context(scope)]
    if soul_file is not None:
        soul_context = _team_lead_soul_context(soul_file)
        if soul_context:
            prompt_parts.append(soul_context)
    if internal_website_base_url:
        prompt_parts.append(_documentation_context(internal_website_base_url))
    return "Workspace Turn Context:\n" + "\n\n".join(prompt_parts)
