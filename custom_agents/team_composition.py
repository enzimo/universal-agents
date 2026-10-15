"""Team composition (TEAM_AGENTS.yaml) and generated-agent helpers.

Both live in the instance's control/ folder, which only trusted runtime services
write (runtime_settings.instance_layout).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from runtime_settings.instance_layout import (
    AGENT_BUNDLES_DIRNAME,
    GENERATED_AGENT_BUNDLES_DIRNAME,
    TEAM_COMPOSITION_FILENAME,
)


TEAM_COMPOSITION_SCHEMA_VERSION = 4
DEFAULT_TEAM_COMPOSITION_PATH = TEAM_COMPOSITION_FILENAME
# Relative to control/, which the agent catalog scans for agent_bundles/.
GENERATED_AGENT_BUNDLE_DIR = f"{AGENT_BUNDLES_DIRNAME}/{GENERATED_AGENT_BUNDLES_DIRNAME}"
_AGENT_TYPE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,63}")
# The removed capability-attachment section (access plan Phase 7). Tools now
# follow grants, so a roster that still has it is refused, not rewritten.
REMOVED_ATTACHMENTS_KEY = "approved_attachments"


def _attachment_entries(section: Any) -> list[str]:
    """``AgentType: capability`` for each attachment an old roster section listed."""

    agent_types = section.get("agent_types") if isinstance(section, dict) else None
    entries: list[str] = []
    for agent_type, items in (agent_types or {}).items() if isinstance(agent_types, dict) else ():
        for item in items or ():
            capability = item.get("capability_id") if isinstance(item, dict) else item
            entries.append(f"{agent_type}: {capability}")
    return entries


class RemovedRosterAttachments(ValueError):
    """TEAM_AGENTS.yaml still has the removed ``approved_attachments`` section.

    Older versions wrote an empty section into every roster they saved, so
    most rosters have one; the message says whether it held anything, because
    only a non-empty one gave access that must now be given with grants.
    """

    def __init__(self, path: Path, section: Any = None) -> None:
        held = _attachment_entries(section)
        if held:
            what = (
                "It gave these agent types these capabilities: " + "; ".join(held) + ". After deleting it, "
                "give that access with grants: workers get it for each task from what TeamLead may hand on "
                "(TeamLead asks with request_access and hand_on=true), and a person approves the rest in the "
                "access Inbox; with authorization off, list the tools in the agent definition's default_tools."
            )
        else:
            what = (
                "It is empty (older versions wrote it into every roster), so deleting it changes nothing "
                "else: the same agents start, and their access comes from grants (with authorization off, "
                "from their definitions' default_tools)."
            )
        super().__init__(
            f"{path} still has an approved_attachments section, which was removed: an agent's tools now "
            f"follow the access it holds. Delete the approved_attachments section from the file, then start "
            f"Universal Agents again. {what}"
        )
        self.path = path


class TeamCompositionStartupAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    named_agent_type: str
    count: int = 1
    enabled: bool = True

    @field_validator("named_agent_type")
    @classmethod
    def _validate_named_agent_type(cls, value: str) -> str:
        return validate_agent_type_name(value)

    @field_validator("count")
    @classmethod
    def _validate_count(cls, value: int) -> int:
        parsed = int(value)
        if parsed < 1 or parsed > 32:
            raise ValueError("startup agent count must be between 1 and 32.")
        return parsed


class TeamCompositionStartupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "merge"
    agents: list[TeamCompositionStartupAgent] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        normalized = str(value or "merge").strip().lower()
        if normalized not in {"merge", "replace"}:
            raise ValueError("startup_agents.mode must be 'merge' or 'replace'.")
        return normalized


class TeamCompositionEphemeralPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    idle_ttl_seconds: float | None = None

    @field_validator("idle_ttl_seconds")
    @classmethod
    def _validate_idle_ttl_seconds(cls, value: float | None) -> float | None:
        if value is None:
            return None
        parsed = float(value)
        if parsed <= 0:
            raise ValueError("idle_ttl_seconds must be > 0 when set.")
        if parsed > 86400:
            raise ValueError("idle_ttl_seconds must be <= 86400.")
        return parsed


class TeamCompositionEphemeralSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_idle_ttl_seconds: float | None = 600.0
    agent_types: dict[str, TeamCompositionEphemeralPolicy] = Field(default_factory=dict)

    @field_validator("default_idle_ttl_seconds")
    @classmethod
    def _validate_default_idle_ttl_seconds(cls, value: float | None) -> float | None:
        return TeamCompositionEphemeralPolicy._validate_idle_ttl_seconds(value)

    @field_validator("agent_types")
    @classmethod
    def _validate_agent_type_policy_names(
        cls,
        value: dict[str, TeamCompositionEphemeralPolicy],
    ) -> dict[str, TeamCompositionEphemeralPolicy]:
        return {validate_agent_type_name(name): policy for name, policy in value.items()}


class TeamCompositionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = TEAM_COMPOSITION_SCHEMA_VERSION
    startup_agents: TeamCompositionStartupSettings = Field(default_factory=TeamCompositionStartupSettings)
    ephemeral_agents: TeamCompositionEphemeralSettings = Field(default_factory=TeamCompositionEphemeralSettings)
    generated_agent_bundles_dir: str = GENERATED_AGENT_BUNDLE_DIR
    updated_at: str | None = None
    notes: str = ""

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        parsed = int(value)
        if parsed != TEAM_COMPOSITION_SCHEMA_VERSION:
            raise ValueError(
                f"Only TEAM_AGENTS.yaml schema_version {TEAM_COMPOSITION_SCHEMA_VERSION} is supported."
            )
        return parsed


def validate_agent_type_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not _AGENT_TYPE_PATTERN.fullmatch(normalized):
        raise ValueError(
            "agent type names must start with a letter and contain 3-64 letters, numbers, or underscores."
        )
    return normalized


def resolve_team_composition_path(
    *,
    control_dir: str | Path,
    explicit_path: str | Path | None = None,
) -> Path:
    raw_path = Path(str(explicit_path or DEFAULT_TEAM_COMPOSITION_PATH)).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return (Path(control_dir).expanduser().resolve() / raw_path).resolve()


def load_team_composition(path: str | Path) -> TeamCompositionConfig:
    composition_path = Path(path).expanduser().resolve()
    if not composition_path.exists():
        return TeamCompositionConfig()
    with composition_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"TEAM_AGENTS.yaml must contain a YAML mapping: {composition_path}")
    if REMOVED_ATTACHMENTS_KEY in raw:
        raise RemovedRosterAttachments(composition_path, raw[REMOVED_ATTACHMENTS_KEY])
    if int(raw.get("schema_version", 1)) in {1, 2, 3}:
        raw = dict(raw)
        raw["schema_version"] = TEAM_COMPOSITION_SCHEMA_VERSION
    return TeamCompositionConfig.model_validate(raw)


def save_team_composition(path: str | Path, config: TeamCompositionConfig) -> Path:
    composition_path = Path(path).expanduser().resolve()
    composition_path.parent.mkdir(parents=True, exist_ok=True)
    updated = config.model_copy(update={"updated_at": datetime.now(timezone.utc).isoformat()})
    tmp_path = composition_path.with_name(f".{composition_path.name}.tmp")
    tmp_path.write_text(
        yaml.safe_dump(updated.model_dump(mode="json", exclude_none=True), sort_keys=False),
        encoding="utf-8",
    )
    tmp_path.replace(composition_path)
    return composition_path


def generated_agent_bundle_root(
    *,
    control_dir: str | Path,
    config: TeamCompositionConfig,
) -> Path:
    raw_dir = Path(config.generated_agent_bundles_dir).expanduser()
    if raw_dir.is_absolute():
        return raw_dir.resolve()
    return (Path(control_dir).expanduser().resolve() / raw_dir).resolve()


def generated_agent_bundle_path(
    *,
    control_dir: str | Path,
    config: TeamCompositionConfig,
    agent_type: str,
) -> Path:
    return generated_agent_bundle_root(control_dir=control_dir, config=config) / validate_agent_type_name(agent_type)


def write_generated_agent_bundle(
    *,
    control_dir: str | Path,
    config: TeamCompositionConfig,
    agent_type: str,
    template_name: str,
    params: dict[str, Any],
    overrides: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> Path:
    normalized_agent_type = validate_agent_type_name(agent_type)
    normalized_template_name = str(template_name or "").strip()
    if not normalized_template_name:
        raise ValueError("template_name must be non-empty.")
    bundle_dir = generated_agent_bundle_path(
        control_dir=control_dir,
        config=config,
        agent_type=normalized_agent_type,
    )
    catalog_path = bundle_dir / "agent.yaml"
    if catalog_path.exists() and not overwrite:
        raise FileExistsError(
            f"Generated agent definition already exists for {normalized_agent_type}: {catalog_path}"
        )
    bundle_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "agent_types": {
            normalized_agent_type: {
                "extends": normalized_template_name,
                "params": dict(params or {}),
            }
        },
    }
    if overrides:
        payload["agent_types"][normalized_agent_type]["overrides"] = dict(overrides)
    tmp_path = catalog_path.with_name(f".{catalog_path.name}.tmp")
    tmp_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    tmp_path.replace(catalog_path)
    return catalog_path


def set_startup_agent(
    config: TeamCompositionConfig,
    *,
    agent_type: str,
    count: int,
    enabled: bool = True,
    mode: str | None = None,
) -> TeamCompositionConfig:
    normalized_agent_type = validate_agent_type_name(agent_type)
    startup = config.startup_agents.model_copy(deep=True)
    if mode:
        startup.mode = TeamCompositionStartupSettings(mode=mode).mode
    agents = [
        agent
        for agent in startup.agents
        if agent.named_agent_type != normalized_agent_type
    ]
    agents.append(
        TeamCompositionStartupAgent(
            named_agent_type=normalized_agent_type,
            count=count,
            enabled=enabled,
        )
    )
    startup.agents = agents
    return config.model_copy(update={"startup_agents": startup})


def set_ephemeral_agent_policy(
    config: TeamCompositionConfig,
    *,
    agent_type: str,
    idle_ttl_seconds: float | None,
    enabled: bool = True,
) -> TeamCompositionConfig:
    normalized_agent_type = validate_agent_type_name(agent_type)
    ephemeral = config.ephemeral_agents.model_copy(deep=True)
    policies = dict(ephemeral.agent_types)
    policies[normalized_agent_type] = TeamCompositionEphemeralPolicy(
        enabled=enabled,
        idle_ttl_seconds=idle_ttl_seconds,
    )
    ephemeral.agent_types = policies
    return config.model_copy(update={"ephemeral_agents": ephemeral})


def ephemeral_idle_ttl_for_agent_type(
    config: TeamCompositionConfig,
    agent_type: str,
) -> float | None:
    policy = config.ephemeral_agents.agent_types.get(str(agent_type or "").strip())
    if policy is not None:
        if not policy.enabled:
            return None
        return policy.idle_ttl_seconds if policy.idle_ttl_seconds is not None else config.ephemeral_agents.default_idle_ttl_seconds
    return config.ephemeral_agents.default_idle_ttl_seconds
