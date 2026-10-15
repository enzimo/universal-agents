"""
Team template helpers.

Team templates (TEAM_TEMPLATES.yaml) live in the instance's control/ folder. The
manager lists them with list_team_templates and instantiates a named starting
team with one runtime tool call. Complete catalog maintenance is operator-owned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from config_loader import resolve_team_templates_path


def get_team_templates_path(
    instance_root: str | None,
    explicit_templates_path: str | None = None,
) -> Path | None:
    if not instance_root and not explicit_templates_path:
        return None
    return resolve_team_templates_path(
        instance_root=instance_root,
        explicit_templates_path=explicit_templates_path,
    )


def load_team_templates(
    instance_root: str | None,
    explicit_templates_path: str | None = None,
) -> dict[str, Any]:
    path = get_team_templates_path(instance_root, explicit_templates_path)
    if path is None:
        raise ValueError(
            "This agent has no instance root configured, so team templates are unavailable."
        )
    if not path.exists():
        raise ValueError(f"Team templates file does not exist: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Team templates file must contain a YAML mapping: {path}")

    templates = raw.get("templates") or {}
    if not isinstance(templates, dict):
        raise ValueError(f"`templates` must be a mapping in {path}")

    normalized_templates: dict[str, Any] = {}
    for template_name, raw_template in templates.items():
        if not isinstance(raw_template, dict):
            raise ValueError(f"Template `{template_name}` must be a mapping in {path}")
        members = raw_template.get("members") or []
        if not isinstance(members, list):
            raise ValueError(
                f"Template `{template_name}` members must be a list in {path}"
            )

        normalized_members: list[dict[str, Any]] = []
        for index, raw_member in enumerate(members, start=1):
            if not isinstance(raw_member, dict):
                raise ValueError(
                    f"Template `{template_name}` member #{index} must be a mapping in {path}"
                )
            agent_type = str(raw_member.get("agent_type") or "").strip()
            if not agent_type:
                raise ValueError(
                    f"Template `{template_name}` member #{index} is missing `agent_type` in {path}"
                )
            count = raw_member.get("count", 1)
            if not isinstance(count, int) or count < 1:
                raise ValueError(
                    f"Template `{template_name}` member `{agent_type}` must have integer count >= 1 in {path}"
                )
            mcp_profile_names = raw_member.get("mcp_profile_names") or []
            if not isinstance(mcp_profile_names, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in mcp_profile_names
            ):
                raise ValueError(
                    f"Template `{template_name}` member `{agent_type}` has invalid `mcp_profile_names` in {path}"
                )
            normalized_members.append(
                {
                    "agent_type": agent_type,
                    "count": count,
                    "mcp_profile_names": [item.strip() for item in mcp_profile_names],
                    "notes": str(raw_member.get("notes") or "").strip(),
                }
            )

        normalized_templates[str(template_name)] = {
            "description": str(raw_template.get("description") or "").strip(),
            "members": normalized_members,
        }

    return {
        "path": str(path),
        "version": raw.get("version", 1),
        "templates": normalized_templates,
    }


def summarize_team_templates(
    instance_root: str | None,
    explicit_templates_path: str | None = None,
) -> dict[str, Any]:
    loaded = load_team_templates(instance_root, explicit_templates_path)
    summaries: dict[str, Any] = {}
    for template_name, template in loaded["templates"].items():
        summaries[template_name] = {
            "description": template.get("description") or "",
            "members": [
                {
                    "agent_type": member["agent_type"],
                    "count": member["count"],
                    "mcp_profile_names": member["mcp_profile_names"],
                }
                for member in template["members"]
            ],
        }
    return {
        "path": loaded["path"],
        "version": loaded["version"],
        "templates": summaries,
    }
