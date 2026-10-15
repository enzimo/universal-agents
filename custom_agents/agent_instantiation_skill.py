"""
Runtime rendering for the manager-only agent-instantiation skill.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from default_values import AGENT_DEFAULTS
from custom_agents.default_tools import (
    DEFAULT_TOOLS_WITH_AUTHORIZATION,
    DEFAULT_TOOLS_WITHOUT_AUTHORIZATION,
    default_tool_labels,
)
from custom_agents.team_templates import summarize_team_templates
from runtime_settings.instance_layout import InstanceLayout

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_PATH = (
    _PROJECT_ROOT
    / "custom_agents"
    / "skill_templates"
    / "agent-instantiation-skill"
    / "SKILL.md"
)
RUNTIME_SKILLS_ROOT = _PROJECT_ROOT / ".runtime_skills"
_PATH_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def _sanitize_path_component(value: str) -> str:
    sanitized = _PATH_SAFE_PATTERN.sub("-", value.strip())
    sanitized = sanitized.strip("-._")
    return sanitized or "unknown"


def _format_agent_types_section(
    agent_type_map: Mapping[str, Any],
    *,
    authorization_enabled: bool,
) -> str:
    lines: list[str] = []
    for agent_type in sorted(agent_type_map):
        config = agent_type_map[agent_type]
        display_name = str(config.get("display_name") or agent_type)
        role = str(config.get("role", "No role configured"))
        description = str(config.get("description") or "").strip()
        labels = default_tool_labels(config.get("default_tools") or ())
        lines.append(f"### `{agent_type}`")
        lines.append(f"- display_name: {display_name}")
        lines.append(f"- role: {role}")
        if description:
            lines.append(f"- description: {description}")
        lines.append(
            "- default tools: "
            + (", ".join(labels) if labels else "none beyond its role's standard tools")
        )
        if config.get("skills_enabled"):
            lines.append("- skills plugin: enabled")
    if not lines:
        return "No agent types are currently configured."
    lines.append("")
    lines.append(
        DEFAULT_TOOLS_WITH_AUTHORIZATION
        if authorization_enabled
        else DEFAULT_TOOLS_WITHOUT_AUTHORIZATION
    )
    return "\n".join(lines)


def _redact_mapping_values(mapping: Mapping[str, Any]) -> str:
    if not mapping:
        return "none"
    return ", ".join(f"`{key}`" for key in sorted(str(key) for key in mapping))


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True)


def _format_profile_lines(
    profile_name: str, profile_config: Mapping[str, Any]
) -> list[str]:
    lines = [f"### `{profile_name}`"]
    description = profile_config.get("description")
    if description:
        lines.append(f"- description: {description}")
    for field_name in (
        "transport",
        "server_url",
        "server_url_env",
        "command",
        "cwd",
        "tool_name_prefix",
        "startup_timeout",
        "timeout",
        "sse_read_timeout",
        "tool_timeout_seconds",
        "terminate_on_close",
        "max_message_bytes",
    ):
        if field_name in profile_config:
            lines.append(
                f"- {field_name}: `{_format_scalar(profile_config.get(field_name))}`"
            )

    args = list(profile_config.get("args") or [])
    if args:
        lines.append(f"- args: `{json.dumps(args)}`")
    include_tools = list(profile_config.get("include_tools") or [])
    if include_tools:
        lines.append(f"- include_tools: `{json.dumps(include_tools)}`")
    exclude_tools = list(profile_config.get("exclude_tools") or [])
    if exclude_tools:
        lines.append(f"- exclude_tools: `{json.dumps(exclude_tools)}`")
    headers = dict(profile_config.get("headers") or {})
    if headers:
        lines.append(
            f"- header keys: {_redact_mapping_values(headers)} (values redacted)"
        )
    env = dict(profile_config.get("env") or {})
    if env:
        lines.append(f"- env keys: {_redact_mapping_values(env)} (values redacted)")
    return lines


def _format_mcp_profiles_section(mcp_profiles: Mapping[str, Any] | None) -> str:
    if not mcp_profiles:
        return (
            "No trusted MCP profiles are configured at startup.\n\n"
            "Leave `mcp_profile_names` empty unless config is updated and the manager restarts."
        )

    enabled_names: list[str] = []
    disabled_names: list[str] = []
    lines: list[str] = []
    for profile_name in sorted(mcp_profiles):
        raw_profile = mcp_profiles[profile_name]
        if not isinstance(raw_profile, Mapping):
            continue
        if raw_profile.get("enabled", True) is False:
            disabled_names.append(profile_name)
            continue
        enabled_names.append(profile_name)
        lines.extend(_format_profile_lines(profile_name, raw_profile))

    if not enabled_names:
        lines.append("No enabled trusted MCP profiles are configured at startup.")
    if disabled_names:
        lines.append(
            "\nDisabled profiles omitted from the usable list: "
            + ", ".join(f"`{name}`" for name in disabled_names)
        )
    return "\n".join(lines)


def _format_team_templates_section(
    instance_root: str | None,
    team_templates_path: str | None = None,
) -> str:
    if not instance_root and not team_templates_path:
        return "This agent has no instance root configured, so team templates are unavailable."
    try:
        summary = summarize_team_templates(instance_root, team_templates_path)
    except Exception as exc:
        return f"Team templates unavailable: {exc}"

    templates = summary.get("templates") or {}
    if not templates:
        return f"No team templates are currently defined in `{summary['path']}`."

    lines = [f"Team templates file: `{summary['path']}`", ""]
    for template_name in sorted(templates):
        template = templates[template_name]
        lines.append(f"### `{template_name}`")
        if template.get("description"):
            lines.append(f"- description: {template['description']}")
        for member in template.get("members", []):
            suffix = ""
            if member.get("mcp_profile_names"):
                suffix = f" with MCP profiles {member['mcp_profile_names']}"
            lines.append(f"- {member['count']} x `{member['agent_type']}`{suffix}")
    return "\n".join(lines)


def _format_team_composition_section(
    instance_root: str | None,
    team_composition_path: str | None = None,
) -> str:
    if not instance_root and not team_composition_path:
        return "This agent has no instance root configured, so team composition is unavailable."
    try:
        from custom_agents.team_composition import (
            generated_agent_bundle_root,
            load_team_composition,
            resolve_team_composition_path,
        )

        control_dir = (
            InstanceLayout.at(instance_root).control_dir
            if instance_root
            else Path(str(team_composition_path)).expanduser().resolve().parent
        )
        path = resolve_team_composition_path(
            control_dir=control_dir,
            explicit_path=team_composition_path,
        )
        composition = load_team_composition(path)
        root = generated_agent_bundle_root(control_dir=control_dir, config=composition)
    except Exception as exc:
        return f"Team composition unavailable: {exc}"

    startup_agents = composition.startup_agents.agents
    lines = [
        f"Team composition file: `{path}`",
        f"Generated agent bundle directory: `{root}`",
        f"Startup override mode: `{composition.startup_agents.mode}`",
        f"Default ephemeral idle TTL: `{composition.ephemeral_agents.default_idle_ttl_seconds}` seconds",
        "A generated specialist can be started, delegated to and scheduled as soon as it is created; "
        "no restart. Startup-agent overrides apply on the next headless supervisor launch.",
    ]
    if startup_agents:
        lines.append("")
        lines.append("Startup overrides:")
        for entry in startup_agents:
            state = "enabled" if entry.enabled else "disabled"
            lines.append(f"- {entry.count} x `{entry.named_agent_type}` ({state})")
    return "\n".join(lines)


def render_agent_instantiation_skill(
    *,
    team_id: str,
    agent_type_map: Mapping[str, Any],
    mcp_profiles: Mapping[str, Any] | None,
    instance_root: str | None = None,
    team_templates_path: str | None = None,
    team_composition_path: str | None = None,
    authorization_enabled: bool = True,
) -> str:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("{{current_team_id}}", team_id)
        .replace(
            "{{available_agent_types}}",
            _format_agent_types_section(
                agent_type_map,
                authorization_enabled=authorization_enabled,
            ),
        )
        .replace(
            "{{available_mcp_profiles}}", _format_mcp_profiles_section(mcp_profiles)
        )
        .replace(
            "{{available_team_templates}}",
            _format_team_templates_section(instance_root, team_templates_path),
        )
        .replace(
            "{{team_composition}}",
            _format_team_composition_section(instance_root, team_composition_path),
        )
    )


def ensure_runtime_agent_instantiation_skill(
    *,
    team_id: str,
    agent_type_map: Mapping[str, Any],
    mcp_profiles: Mapping[str, Any] | None,
    current_agent_type: str = AGENT_DEFAULTS.named_agent_type.value,
    instance_root: str | None = None,
    team_templates_path: str | None = None,
    team_composition_path: str | None = None,
    authorization_enabled: bool = True,
) -> Path:
    rendered = render_agent_instantiation_skill(
        team_id=team_id,
        agent_type_map=agent_type_map,
        mcp_profiles=mcp_profiles,
        instance_root=instance_root,
        team_templates_path=team_templates_path,
        team_composition_path=team_composition_path,
        authorization_enabled=authorization_enabled,
    )
    target_dir = (
        RUNTIME_SKILLS_ROOT
        / _sanitize_path_component(current_agent_type)
        / _sanitize_path_component(team_id)
        / "agent-instantiation-skill"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    skill_path = target_dir / "SKILL.md"
    if not skill_path.exists() or skill_path.read_text(encoding="utf-8") != rendered:
        skill_path.write_text(rendered, encoding="utf-8")
    return target_dir
