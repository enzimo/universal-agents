"""Governance configuration: tool policy and authorization settings.

Only the settings the access model reads remain (``authorization.enabled``,
``cedar_binary_path``, ``starter_profile`` and ``presentation.mode``). The old
permission system's settings were removed in access Phase 4d, the old tool
switches in Phase 5, and ``tool_policy.data_folders`` when data folders moved
into the access store (decision D16: administrators register them without a
restart). Both configuration loaders refuse them by name
(``refuse_removed_access_settings``) because these models ignore unknown keys,
and a removed setting that is silently ignored would let an operator believe
it still has an effect.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from custom_messages.contracts.authorization_presentation_mode import (
    AuthorizationPresentationMode,
    normalize_authorization_presentation_mode,
)
from default_values import AUTHORIZATION_DEFAULTS, TOOL_POLICY_DEFAULTS
from runtime_settings.access_workflows import MOVED_WORKFLOW_KEYS
from runtime_settings.common import _normalized_optional_string

# Ten years; larger numbers are almost certainly typing mistakes (and a period
# that reaches before the year 1 cannot be turned into a date). 0 keeps files.
MAX_RETENTION_DAYS = 3650


class ToolPolicySettings(BaseModel):
    """Tool settings that are not access (access is given with grants)."""

    # Trusted maintenance deletes files older than this from every scratch/
    # folder (custom_agents.scratch_retention); 0 turns the cleanup off.
    scratch_retention_days: int = Field(
        default=TOOL_POLICY_DEFAULTS.scratch_retention_days.value, ge=0
    )
    # Trusted maintenance deletes saved tool outputs written longer ago than
    # this (custom_agents.output_retention); 0 turns the cleanup off.
    output_retention_days: int = Field(
        default=TOOL_POLICY_DEFAULTS.output_retention_days.value, ge=0, le=MAX_RETENTION_DAYS
    )


class AuthorizationPresentationSettings(BaseModel):
    """How much detail access refusals and review messages show."""

    mode: AuthorizationPresentationMode = Field(
        default=normalize_authorization_presentation_mode(
            AUTHORIZATION_DEFAULTS.presentation.value["mode"]
        )
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: Any) -> AuthorizationPresentationMode:
        return normalize_authorization_presentation_mode(value)


class AuthorizationSettings(BaseModel):
    enabled: bool = AUTHORIZATION_DEFAULTS.enabled.value
    cedar_binary_path: Optional[str] = AUTHORIZATION_DEFAULTS.cedar_binary_path.value
    # Applied as the instance's user when the gateway starts
    # (access_control.starter_profiles); ua_definitions/access/starter_profiles.yaml.
    starter_profile: Literal["trusted", "careful", "manual"] = AUTHORIZATION_DEFAULTS.starter_profile.value  # type: ignore[assignment]
    presentation: AuthorizationPresentationSettings = Field(
        default_factory=AuthorizationPresentationSettings
    )

    @field_validator("cedar_binary_path", mode="before")
    @classmethod
    def _normalize_binary_path(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)


# Settings of the old permission system (removed in access Phase 4d). The
# access service decides with grants, so none of them has a replacement key.
REMOVED_AUTHORIZATION_KEYS = (
    "engine",
    "fail_closed",
    "policy_path",
    "schema_path",
    "cedar_request_timeout_seconds",
    "user_approval",
    "admin_elevation",
    "emergency_deny",
    "permission_delegation",
    "shell_command_profiles",
)
# Top-level blocks removed with it: the old review Inbox's settings.
REMOVED_ACCESS_BLOCKS = ("human_interactions",)
# Tool switches removed in Phase 5 (file and code access follow grants), and
# the data folder list, which moved into the access store (decision D16).
REMOVED_TOOL_POLICY_KEYS = ("restrict_to_workspace", "allow_web_search", "allow_shell_commands", "data_folders")
REMOVED_TOOL_POLICY_GUIDANCE = (
    "Folders outside the workspace are registered by an administrator with "
    "/access folders add <name> <path> in chat or assistant_cli access folders add, "
    "and given to agents with folder grants; shell and web tools are given with grants too."
)
# Environment variables of the old file-path switches: file tools reach only the
# task's folders and registered data folders, so there is nothing to deny.
REMOVED_FILE_ACCESS_ENV_VARS = ("TOOL_RESTRICT_TO_WORKSPACE", "TOOL_DENY_PATHS", "TOOL_DENIED_PATHS")
REMOVED_ACCESS_SETTINGS_GUIDANCE = (
    "Access is now given with grants. Turn access "
    "checks on or off with authorization.enabled, choose the team lead's first "
    "access with authorization.starter_profile, and give, revoke or block access "
    "with /access in chat or assistant_cli access (documentation: "
    "/docs/permissions/#removed-settings)."
)


def removed_access_settings(document: Mapping[str, Any]) -> list[str]:
    """Return the dotted names of removed access settings present in a config file.

    Checks ``authorization.<key>``, the removed top-level blocks, and the same
    blocks inside ``agent_types.<name>`` overrides (config.yaml merged them).
    """

    found: list[str] = []
    authorization = document.get("authorization")
    if isinstance(authorization, Mapping):
        found += [f"authorization.{key}" for key in REMOVED_AUTHORIZATION_KEYS if key in authorization]
    tool_policy = document.get("tool_policy")
    if isinstance(tool_policy, Mapping):
        found += [f"tool_policy.{key}" for key in REMOVED_TOOL_POLICY_KEYS if key in tool_policy]
    found += [key for key in REMOVED_ACCESS_BLOCKS if key in document]
    agent_types = document.get("agent_types")
    if isinstance(agent_types, Mapping):
        for agent_type, overrides in agent_types.items():
            if isinstance(overrides, Mapping):
                found += [
                    f"agent_types.{agent_type}.{key}" for key in REMOVED_ACCESS_BLOCKS if key in overrides
                ]
    return found


def _moved_workflow_notes(document: Mapping[str, Any]) -> list[str]:
    """Name the new keys for the two workflow limits that moved out of human_interactions."""

    section = document.get("human_interactions")
    if not isinstance(section, Mapping):
        return []
    return [f"human_interactions.{old} moved to {new}" for old, new in MOVED_WORKFLOW_KEYS.items() if old in section]


def reject_removed_file_access_env_vars(env: Mapping[str, str] | None = None) -> None:
    """Refuse to start while a removed file-access environment variable is set."""

    present = sorted(key for key in REMOVED_FILE_ACCESS_ENV_VARS if key in (os.environ if env is None else env))
    if present:
        raise ValueError(
            f"Removed environment variable(s) are set: {', '.join(present)}. Unset them. "
            + REMOVED_TOOL_POLICY_GUIDANCE
        )


def refuse_removed_access_settings(document: Any) -> None:
    """Refuse a configuration that still sets removed access settings, by name."""

    if not isinstance(document, Mapping):
        return
    removed = removed_access_settings(document)
    if not removed:
        return
    verb, pronoun = ("has", "it") if len(removed) == 1 else ("have", "them")
    message = (
        f"{', '.join(removed)} {verb} been removed; delete {pronoun} from the file. "
        f"{REMOVED_ACCESS_SETTINGS_GUIDANCE}"
    )
    if any(name.startswith("tool_policy.") for name in removed):
        message += " " + REMOVED_TOOL_POLICY_GUIDANCE
    moved = _moved_workflow_notes(document)
    if moved:
        message += " Keep the workflow limits under access_workflows: " + "; ".join(moved) + "."
    raise ValueError(message)


__all__ = [
    "AuthorizationPresentationSettings",
    "AuthorizationSettings",
    "REMOVED_ACCESS_BLOCKS",
    "REMOVED_AUTHORIZATION_KEYS",
    "REMOVED_FILE_ACCESS_ENV_VARS",
    "REMOVED_TOOL_POLICY_KEYS",
    "reject_removed_file_access_env_vars",
    "ToolPolicySettings",
    "refuse_removed_access_settings",
    "removed_access_settings",
]
