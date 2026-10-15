"""Workspace configuration and contracts."""

from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field, field_validator
from custom_agents.external_skills import normalize_external_skill_repository_urls
from default_values import WORKSPACE_DEFAULTS


class WorkspaceSkillInstallationSettings(BaseModel):
    enabled: bool = WORKSPACE_DEFAULTS.skill_installation.value["enabled"]
    scanner_executable: str = WORKSPACE_DEFAULTS.skill_installation.value[
        "scanner_executable"
    ]
    scanner_timeout_seconds: int = WORKSPACE_DEFAULTS.skill_installation.value[
        "scanner_timeout_seconds"
    ]
    safe_max_score: int = WORKSPACE_DEFAULTS.skill_installation.value["safe_max_score"]
    plan_ttl_seconds: int = WORKSPACE_DEFAULTS.skill_installation.value[
        "plan_ttl_seconds"
    ]
    max_repository_bytes: int = WORKSPACE_DEFAULTS.skill_installation.value[
        "max_repository_bytes"
    ]

    @field_validator("scanner_executable", mode="before")
    @classmethod
    def _normalize_scanner_executable(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(
                "workspace.skill_installation.scanner_executable must not be empty"
            )
        return normalized

    @field_validator("scanner_timeout_seconds", mode="before")
    @classmethod
    def _validate_scanner_timeout(cls, value: Any) -> int:
        parsed = int(value)
        if parsed < 10 or parsed > 900:
            raise ValueError(
                "workspace.skill_installation.scanner_timeout_seconds must be between 10 and 900"
            )
        return parsed

    @field_validator("safe_max_score", mode="before")
    @classmethod
    def _validate_safe_max_score(cls, value: Any) -> int:
        parsed = int(value)
        if parsed < 0 or parsed > 20:
            raise ValueError(
                "workspace.skill_installation.safe_max_score must be between 0 and 20"
            )
        return parsed

    @field_validator("plan_ttl_seconds", mode="before")
    @classmethod
    def _validate_plan_ttl(cls, value: Any) -> int:
        parsed = int(value)
        if parsed < 60 or parsed > 86400:
            raise ValueError(
                "workspace.skill_installation.plan_ttl_seconds must be between 60 and 86400"
            )
        return parsed

    @field_validator("max_repository_bytes", mode="before")
    @classmethod
    def _validate_repository_limit(cls, value: Any) -> int:
        parsed = int(value)
        if parsed < 1024 * 1024 or parsed > 1024 * 1024 * 1024:
            raise ValueError(
                "workspace.skill_installation.max_repository_bytes must be between 1 MiB and 1 GiB"
            )
        return parsed


class WorkspaceSettings(BaseModel):
    """Workspace behavior. Locations are derived from the instance root
    (``runtime_settings.instance_layout``) and are not configurable."""

    objective_execution_enabled: bool = (
        WORKSPACE_DEFAULTS.objective_execution_enabled.value
    )
    objective_execution_paths: list[str] = Field(
        default_factory=lambda: list(WORKSPACE_DEFAULTS.objective_execution_paths.value)
    )
    skill_installation: WorkspaceSkillInstallationSettings = Field(
        default_factory=WorkspaceSkillInstallationSettings
    )
    external_skill_repositories: list[str] = Field(default_factory=list)
    refresh_external_skills_on_start: bool = False

    @field_validator("objective_execution_paths", mode="before")
    @classmethod
    def _normalize_objective_execution_paths(cls, value: Any) -> list[str]:
        if value is None:
            return list(WORKSPACE_DEFAULTS.objective_execution_paths.value)
        if not isinstance(value, list):
            raise ValueError("workspace.objective_execution_paths must be a YAML array")
        normalized = [str(item or "").strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError(
                "workspace.objective_execution_paths entries must not be blank"
            )
        return normalized

    @field_validator("external_skill_repositories", mode="before")
    @classmethod
    def _normalize_external_skill_repositories(cls, value: Any) -> list[str]:
        return normalize_external_skill_repository_urls(value)
