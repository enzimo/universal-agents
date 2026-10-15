"""Worker pools: how many workers of each type run, and what trusted code does about it.

Delegated tasks wait in their worker type's shared queue until a free worker
takes one (ADR-019). The worker pool controller (``custom_agents.worker_pool_controller``)
starts workers while tasks wait and no worker is free, up to the pool limit of
the type. A worker of a type does both delegated and scheduled work, so one
limit per type covers both.

Two kinds of values live here:

- Operator ceilings, which agents never change: the most workers of one type
  (``max_workers_per_type_ceiling``), the most workers in the team
  (``max_total_workers_per_team``) and starts per type per hour
  (``max_launches_per_type_per_hour``).
- Starting values for what TeamArchitect manages: the pool size of a type
  until TeamArchitect sets another one (``default_max_workers_per_type``), and
  the timings and thresholds of the controller, its reviews and its notes.

TeamArchitect's own pool sizes (temporary or lasting) are stored in the
database (``storage.worker_pool_repository``), always inside the ceilings.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from default_values import AGENT_DEFAULTS

_DEFAULTS = AGENT_DEFAULTS.worker_pools.value


class ProviderLimitSettings(BaseModel):
    """What a worker does when a model provider refuses calls (rate limit, quota, overload)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cooldown_seconds: float = Field(
        default=_DEFAULTS["provider_limit"]["cooldown_seconds"], gt=0, le=3600
    )
    max_cooldown_seconds: float = Field(
        default=_DEFAULTS["provider_limit"]["max_cooldown_seconds"], gt=0, le=86400
    )
    max_retries: int = Field(default=_DEFAULTS["provider_limit"]["max_retries"], ge=0, le=20)
    review_events: int = Field(
        default=_DEFAULTS["provider_limit"]["review_events"], ge=1, le=1000
    )
    review_window_minutes: float = Field(
        default=_DEFAULTS["provider_limit"]["review_window_minutes"], gt=0, le=1440
    )

    @model_validator(mode="after")
    def _cooldowns(self) -> "ProviderLimitSettings":
        if self.max_cooldown_seconds < self.cooldown_seconds:
            raise ValueError("provider_limit.max_cooldown_seconds must be >= cooldown_seconds")
        return self


class ProgressNoticeSettings(BaseModel):
    """Counts trusted code sends the person while delegated tasks run."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    min_tasks: int = Field(default=_DEFAULTS["progress_notices"]["min_tasks"], ge=1, le=10000)
    interval_minutes: float = Field(
        default=_DEFAULTS["progress_notices"]["interval_minutes"], gt=0, le=1440
    )


class WorkerPoolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    enabled: bool = _DEFAULTS["enabled"]
    default_max_workers_per_type: int = Field(
        default=_DEFAULTS["default_max_workers_per_type"], ge=1, le=1000
    )
    max_workers_per_type_ceiling: int = Field(
        default=_DEFAULTS["max_workers_per_type_ceiling"], ge=1, le=1000
    )
    max_total_workers_per_team: int = Field(
        default=_DEFAULTS["max_total_workers_per_team"], ge=1, le=1000
    )
    max_launches_per_type_per_hour: int = Field(
        default=_DEFAULTS["max_launches_per_type_per_hour"], ge=1, le=10000
    )
    scale_up_wait_seconds: float = Field(
        default=_DEFAULTS["scale_up_wait_seconds"], ge=0, le=3600
    )
    max_starts_per_pass: int = Field(default=_DEFAULTS["max_starts_per_pass"], ge=1, le=100)
    capacity_warning_ratio: float = Field(
        default=_DEFAULTS["capacity_warning_ratio"], ge=0.5, le=1.0
    )
    review_repeat_after_minutes: float = Field(
        default=_DEFAULTS["review_repeat_after_minutes"], gt=0, le=10080
    )
    recurring_incident_count: int = Field(
        default=_DEFAULTS["recurring_incident_count"], ge=2, le=100
    )
    recurring_incident_window_hours: float = Field(
        default=_DEFAULTS["recurring_incident_window_hours"], gt=0, le=720
    )
    max_retries_after_worker_loss: int = Field(
        default=_DEFAULTS["max_retries_after_worker_loss"], ge=0, le=20
    )
    provider_limit: ProviderLimitSettings = Field(default_factory=ProviderLimitSettings)
    progress_notices: ProgressNoticeSettings = Field(default_factory=ProgressNoticeSettings)

    @model_validator(mode="after")
    def _limits_nest(self) -> "WorkerPoolsConfig":
        if self.default_max_workers_per_type > self.max_workers_per_type_ceiling:
            raise ValueError(
                "agent.worker_pools.default_max_workers_per_type must not exceed "
                "max_workers_per_type_ceiling"
            )
        if self.max_workers_per_type_ceiling > self.max_total_workers_per_team:
            raise ValueError(
                "agent.worker_pools.max_workers_per_type_ceiling must not exceed "
                "max_total_workers_per_team"
            )
        return self


WORKER_POOLS_PATH = "agent.worker_pools"

# Scheduled-worker capacity settings of releases before worker pools, with the
# defaults they shipped with. Their values move to agent.worker_pools when
# customized; agent.schedule_workers.max_auto_workers_per_type and
# max_auto_workers_per_team have no successor (one pool size per type covers
# scheduled and delegated work).
_MOVED_CAPACITY_SETTINGS = {
    "max_total_workers_per_team": 32,
    "max_launches_per_type_per_hour": 12,
}
_REMOVED_CAPACITY_SETTINGS = ("max_auto_workers_per_type", "max_auto_workers_per_team")


def _drop_delegation_assurance(section: Any) -> bool:
    if isinstance(section, dict) and "delegation_assurance" in section:
        section.pop("delegation_assurance")
        return True
    return False


def move_worker_capacity_settings(document: dict[str, Any]) -> list[str]:
    """Upgrade a configuration document to worker pools, in place; return what changed.

    Used by the config.yaml and instance_config.yaml upgrade steps. Customized
    team capacity and start-rate values move to ``agent.worker_pools``; values
    still at their old defaults are dropped so the new defaults apply. The
    removed ``agent.delegation_assurance`` section (and its per-type copies) is
    dropped: workers now take delegated tasks from a shared queue and the pool
    controller starts workers.
    """

    notes: list[str] = []
    agent = document.get("agent")
    if not isinstance(agent, dict):
        agent = {}
    agent = dict(agent)
    schedule_workers = dict(agent.get("schedule_workers") or {})
    pools = dict(agent.get("worker_pools") or {})
    for key, old_default in _MOVED_CAPACITY_SETTINGS.items():
        if key not in schedule_workers:
            continue
        value = schedule_workers.pop(key)
        if value != old_default and key not in pools:
            pools[key] = value
            notes.append(f"agent.schedule_workers.{key} moved to agent.worker_pools.{key}")
        else:
            notes.append(f"agent.schedule_workers.{key} dropped (default value)")
    for key in _REMOVED_CAPACITY_SETTINGS:
        if schedule_workers.pop(key, None) is not None:
            notes.append(
                f"agent.schedule_workers.{key} removed (agent.worker_pools sets one pool size per type)"
            )
    team_limit = pools.get("max_total_workers_per_team")
    if isinstance(team_limit, int):
        # A team limit below the new per-type ceiling would fail validation.
        ceiling = pools.get("max_workers_per_type_ceiling", _DEFAULTS["max_workers_per_type_ceiling"])
        if isinstance(ceiling, int) and ceiling > team_limit:
            pools["max_workers_per_type_ceiling"] = team_limit
            notes.append(f"agent.worker_pools.max_workers_per_type_ceiling set to {team_limit} (the team limit)")
        default = pools.get("default_max_workers_per_type", _DEFAULTS["default_max_workers_per_type"])
        if isinstance(default, int) and default > team_limit:
            pools["default_max_workers_per_type"] = team_limit
    if schedule_workers or "schedule_workers" in agent:
        agent["schedule_workers"] = schedule_workers
    if pools:
        agent["worker_pools"] = pools
    if _drop_delegation_assurance(agent):
        notes.append("agent.delegation_assurance removed (workers take delegated tasks from a shared queue)")
    agent_types = document.get("agent_types")
    if isinstance(agent_types, dict):
        for name, overrides in agent_types.items():
            if not isinstance(overrides, dict):
                continue
            dropped = _drop_delegation_assurance(overrides) or _drop_delegation_assurance(
                overrides.get("agent")
            )
            if dropped:
                notes.append(f"agent_types.{name} delegation_assurance removed")
    if "agent" in document or agent:
        document["agent"] = agent
    return notes


__all__ = [
    "ProgressNoticeSettings",
    "ProviderLimitSettings",
    "WORKER_POOLS_PATH",
    "WorkerPoolsConfig",
    "move_worker_capacity_settings",
]
