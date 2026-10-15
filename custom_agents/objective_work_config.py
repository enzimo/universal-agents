"""Strict configuration for explicitly activated bounded Objective work sessions.

The runtime loads the built-in policy by default so agents can suggest this workflow
at any point. Loading profiles never time-boxes an Objective: an authenticated user
must explicitly accept a profile for that Objective first. Operators can instead
point at a separate customized policy or disable availability in the main config.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from custom_messages.records.objectives import ObjectiveWorkBudget, ObjectiveWorkProfile


LONG_HORIZON_PLAN_EXECUTION_SKILL = "long-horizon-plan-execution"


class ObjectiveWorkSuggestionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    eligible_horizons: List[str] = Field(
        default_factory=lambda: ["long_horizon", "recurring", "standing"]
    )
    minimum_estimated_sessions: int = Field(default=3, ge=2)


class ObjectiveWorkBudgetBounds(BaseModel):
    """Operator-owned inclusive bounds for an agent-authored budget."""

    model_config = ConfigDict(extra="forbid")

    minimum: ObjectiveWorkBudget = Field(
        default_factory=lambda: ObjectiveWorkBudget(
            active_seconds=300,
            checkpoint_reserve_seconds=60,
            checkpoint_reserve_turns=1,
            checkpoint_reserve_tokens=10_000,
            checkpoint_reserve_tool_calls=1,
            turns=2,
            total_tokens=25_000,
            tool_calls=2,
        )
    )
    maximum: ObjectiveWorkBudget = Field(
        default_factory=lambda: ObjectiveWorkBudget(
            active_seconds=28_800,
            checkpoint_reserve_seconds=3_600,
            checkpoint_reserve_turns=20,
            checkpoint_reserve_tokens=500_000,
            checkpoint_reserve_tool_calls=5,
            turns=200,
            total_tokens=3_000_000,
            tool_calls=500,
        )
    )

    @model_validator(mode="after")
    def _validate_bounds(self) -> "ObjectiveWorkBudgetBounds":
        for field_name in ObjectiveWorkBudget.model_fields:
            if field_name == "message_class":
                continue
            if getattr(self.minimum, field_name) > getattr(self.maximum, field_name):
                raise ValueError(
                    f"custom_profiles.budget_bounds.minimum.{field_name} must not exceed maximum.{field_name}"
                )
        return self


class ObjectiveWorkCustomProfilesConfig(BaseModel):
    """Limits for conversational one-time and reusable profile proposals."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    allow_reusable_installation: bool = True
    proposal_ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)
    minimum_cadence_interval_seconds: int = Field(
        default=3_600,
        ge=60,
        le=604_800,
    )
    max_installed_profiles_per_owner: int = Field(default=50, ge=1, le=500)
    budget_bounds: ObjectiveWorkBudgetBounds = Field(
        default_factory=ObjectiveWorkBudgetBounds
    )


class ObjectiveWorkVisibilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emit_activity_snapshots: bool = True
    checkpoint_history_limit: int = Field(default=50, ge=1, le=500)


class ObjectiveWorkExecutionConfig(BaseModel):
    """Versioned operator policy loaded from its own file."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    suggestions: ObjectiveWorkSuggestionsConfig = Field(
        default_factory=ObjectiveWorkSuggestionsConfig
    )
    required_skills: List[str] = Field(
        default_factory=lambda: [LONG_HORIZON_PLAN_EXECUTION_SKILL]
    )
    default_profile: str
    profiles: Dict[str, ObjectiveWorkProfile]
    custom_profiles: ObjectiveWorkCustomProfilesConfig = Field(
        default_factory=ObjectiveWorkCustomProfilesConfig
    )
    visibility: ObjectiveWorkVisibilityConfig = Field(
        default_factory=ObjectiveWorkVisibilityConfig
    )

    @model_validator(mode="after")
    def _validate_policy(self) -> "ObjectiveWorkExecutionConfig":
        if self.default_profile not in self.profiles:
            raise ValueError("default_profile must name an entry in profiles")
        required = {item.strip() for item in self.required_skills if item.strip()}
        if LONG_HORIZON_PLAN_EXECUTION_SKILL not in required:
            raise ValueError(
                "required_skills must include long-horizon-plan-execution for bounded Objective runs"
            )
        self.required_skills = sorted(required)
        for name, profile in self.profiles.items():
            if not name.strip():
                raise ValueError("profile names must not be blank")
            for reserve_field in (
                "checkpoint_reserve_seconds",
                "checkpoint_reserve_turns",
                "checkpoint_reserve_tokens",
                "checkpoint_reserve_tool_calls",
            ):
                if getattr(profile.budget, reserve_field) <= 0:
                    raise ValueError(
                        f"profiles.{name}.budget.{reserve_field} must be greater than zero"
                    )
            if (
                profile.budget.checkpoint_reserve_seconds
                >= profile.budget.active_seconds
            ):
                raise ValueError(
                    f"profiles.{name}.budget.checkpoint_reserve_seconds must be less than active_seconds"
                )
            if profile.budget.checkpoint_reserve_turns >= profile.budget.turns:
                raise ValueError(
                    f"profiles.{name}.budget.checkpoint_reserve_turns must be less than turns"
                )
            if profile.budget.checkpoint_reserve_tokens >= profile.budget.total_tokens:
                raise ValueError(
                    f"profiles.{name}.budget.checkpoint_reserve_tokens must be less than total_tokens"
                )
            if (
                profile.budget.checkpoint_reserve_tool_calls
                >= profile.budget.tool_calls
            ):
                raise ValueError(
                    f"profiles.{name}.budget.checkpoint_reserve_tool_calls must be less than tool_calls"
                )
        if len({name.casefold() for name in self.profiles}) != len(self.profiles):
            raise ValueError(
                "configured profile names must be unique case-insensitively"
            )
        return self


def validate_agent_profile(
    profile: ObjectiveWorkProfile,
    *,
    settings: ObjectiveWorkCustomProfilesConfig,
) -> ObjectiveWorkProfile:
    """Validate an agent-authored profile against the operator envelope."""

    if not settings.enabled:
        raise ValueError("Agent-authored Objective work profiles are disabled.")
    cron_parts = profile.cadence_cron.split()
    if len(cron_parts) != 5:
        raise ValueError("cadence_cron must be a five-field cron expression.")
    try:
        from croniter import croniter  # type: ignore[import-untyped]

        cursor = croniter(profile.cadence_cron, datetime.now(timezone.utc))
        previous = cursor.get_next(datetime)
        for _ in range(255):
            current = cursor.get_next(datetime)
            interval_seconds = (current - previous).total_seconds()
            if interval_seconds < settings.minimum_cadence_interval_seconds:
                raise ValueError(
                    "cadence_cron fires more frequently than the configured "
                    f"{settings.minimum_cadence_interval_seconds}-second minimum interval."
                )
            previous = current
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            "cadence_cron is invalid or cannot produce future runs."
        ) from exc

    bounds = settings.budget_bounds
    for field_name in ObjectiveWorkBudget.model_fields:
        if field_name == "message_class":
            continue
        value = getattr(profile.budget, field_name)
        minimum = getattr(bounds.minimum, field_name)
        maximum = getattr(bounds.maximum, field_name)
        if not minimum <= value <= maximum:
            raise ValueError(
                f"budget.{field_name} must be between {minimum} and {maximum}."
            )
    if profile.budget.checkpoint_reserve_seconds >= profile.budget.active_seconds:
        raise ValueError(
            "budget.checkpoint_reserve_seconds must be less than active_seconds."
        )
    if profile.budget.checkpoint_reserve_turns >= profile.budget.turns:
        raise ValueError("budget.checkpoint_reserve_turns must be less than turns.")
    if profile.budget.checkpoint_reserve_tokens >= profile.budget.total_tokens:
        raise ValueError(
            "budget.checkpoint_reserve_tokens must be less than total_tokens."
        )
    if profile.budget.checkpoint_reserve_tool_calls >= profile.budget.tool_calls:
        raise ValueError(
            "budget.checkpoint_reserve_tool_calls must be less than tool_calls."
        )
    return profile


class ObjectiveWorkSuggestionsLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    eligible_horizons: Optional[List[str]] = None
    minimum_estimated_sessions: Optional[int] = Field(default=None, ge=2)


class ObjectiveWorkBudgetLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_seconds: Optional[int] = Field(default=None, gt=0)
    checkpoint_reserve_seconds: Optional[int] = Field(default=None, ge=0)
    checkpoint_reserve_turns: Optional[int] = Field(default=None, ge=0)
    checkpoint_reserve_tokens: Optional[int] = Field(default=None, ge=0)
    checkpoint_reserve_tool_calls: Optional[int] = Field(default=None, ge=0)
    turns: Optional[int] = Field(default=None, gt=0)
    total_tokens: Optional[int] = Field(default=None, gt=0)
    tool_calls: Optional[int] = Field(default=None, gt=0)


class ObjectiveWorkBudgetBoundsLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum: Optional[ObjectiveWorkBudgetLayer] = None
    maximum: Optional[ObjectiveWorkBudgetLayer] = None


class ObjectiveWorkCustomProfilesLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    allow_reusable_installation: Optional[bool] = None
    proposal_ttl_seconds: Optional[int] = Field(default=None, ge=300, le=604_800)
    minimum_cadence_interval_seconds: Optional[int] = Field(
        default=None, ge=60, le=604_800
    )
    max_installed_profiles_per_owner: Optional[int] = Field(default=None, ge=1, le=500)
    budget_bounds: Optional[ObjectiveWorkBudgetBoundsLayer] = None


class ObjectiveWorkVisibilityLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emit_activity_snapshots: Optional[bool] = None
    checkpoint_history_limit: Optional[int] = Field(default=None, ge=1, le=500)


class ObjectiveWorkExecutionConfigLayer(BaseModel):
    """One strict, ordered policy layer; later files override earlier files."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    suggestions: Optional[ObjectiveWorkSuggestionsLayer] = None
    required_skills: Optional[List[str]] = None
    default_profile: Optional[str] = None
    profiles: Dict[str, ObjectiveWorkProfile] = Field(default_factory=dict)
    custom_profiles: Optional[ObjectiveWorkCustomProfilesLayer] = None
    visibility: Optional[ObjectiveWorkVisibilityLayer] = None


def _merge_policy_layer(base: Dict[str, Any], layer: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in layer.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_policy_layer(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def load_objective_work_execution_config(
    paths: Optional[Sequence[str]],
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[ObjectiveWorkExecutionConfig]:
    """Load and merge ordered policy files, skipping only files absent on disk."""

    event_logger = logger or logging.getLogger(__name__)
    if isinstance(paths, (str, bytes)):
        raise TypeError(
            "Objective execution configuration requires an ordered path sequence, not a singular path."
        )
    rendered_paths = [str(path or "").strip() for path in (paths or [])]
    rendered_paths = [path for path in rendered_paths if path]
    if not rendered_paths:
        return None

    merged: Dict[str, Any] = {
        "schema_version": 2,
        "suggestions": ObjectiveWorkSuggestionsConfig().model_dump(mode="json"),
        "custom_profiles": ObjectiveWorkCustomProfilesConfig().model_dump(mode="json"),
        "visibility": ObjectiveWorkVisibilityConfig().model_dump(mode="json"),
    }
    loaded_paths: List[Path] = []
    profile_sources: Dict[str, Path] = {}
    for rendered in rendered_paths:
        config_path = Path(rendered).expanduser().resolve()
        if not config_path.is_file():
            event_logger.warning(
                "Objective execution policy file is unavailable; continuing without this layer. "
                "event=objective_execution_policy_file_missing configured_path=%s resolved_path=%s",
                rendered,
                config_path,
            )
            continue
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(
                f"Objective execution config {config_path} must contain a YAML mapping"
            )
        layer = ObjectiveWorkExecutionConfigLayer.model_validate(raw)
        layer_payload = layer.model_dump(
            mode="json", exclude_none=True, exclude_unset=True
        )
        for profile_name in layer.profiles:
            previous_source = profile_sources.get(profile_name.casefold())
            if previous_source is not None:
                event_logger.info(
                    "A later Objective execution policy layer replaced a configured profile. "
                    "event=objective_execution_profile_layer_replaced profile_name=%s "
                    "previous_source=%s replacement_source=%s",
                    profile_name,
                    previous_source,
                    config_path,
                )
            existing_profiles = dict(merged.get("profiles") or {})
            previous_name = next(
                (
                    name
                    for name in existing_profiles
                    if name.casefold() == profile_name.casefold()
                ),
                None,
            )
            if previous_name is not None and previous_name != profile_name:
                existing_profiles.pop(previous_name)
                merged["profiles"] = existing_profiles
            profile_sources[profile_name.casefold()] = config_path
        merged = _merge_policy_layer(merged, layer_payload)
        loaded_paths.append(config_path)

    if not loaded_paths:
        event_logger.warning(
            "No configured Objective execution policy files were available; bounded Objective execution remains unavailable. "
            "event=objective_execution_policy_stack_empty configured_path_count=%s",
            len(rendered_paths),
        )
        return None
    config = ObjectiveWorkExecutionConfig.model_validate(merged)
    event_logger.info(
        "Loaded ordered Objective execution policy stack. "
        "event=objective_execution_policy_stack_loaded layer_count=%s profile_count=%s paths=%s",
        len(loaded_paths),
        len(config.profiles),
        [str(path) for path in loaded_paths],
    )
    return config
