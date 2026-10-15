"""Lead-managed scheduled-worker eligibility and rules within operator ceilings.

How many workers may run is in ``runtime_settings.worker_pools``: a worker of a
type does both scheduled and delegated work, so one limit covers both.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from custom_messages.contracts.schedule_worker_v1 import ScheduleWorkerRulesV1
from default_values import AGENT_DEFAULTS

_DEFAULTS = AGENT_DEFAULTS.schedule_workers.value
SCHEDULE_WORKER_ELIGIBILITY_PATH = "agent.schedule_workers.eligible_agent_types"


class ScheduleWorkersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    enabled: bool = _DEFAULTS["enabled"]
    default_startup_policy_enabled: bool = _DEFAULTS["default_startup_policy_enabled"]
    eligible_agent_types: list[str] = Field(
        default_factory=lambda: list(_DEFAULTS["eligible_agent_types"]),
        description="TeamLead-managed automatic-start candidates. Registered specialists still require a schedule binding and current launch authority; operator denies take precedence.",
    )
    denied_agent_types: list[str] = Field(default_factory=list)
    reconcile_interval_seconds: float = Field(
        default=_DEFAULTS["reconcile_interval_seconds"], gt=0, le=60
    )
    max_claim_wait_seconds: float = Field(
        default=_DEFAULTS["max_claim_wait_seconds"], ge=0, le=86400
    )
    max_startup_timeout_seconds: float = Field(
        default=_DEFAULTS["max_startup_timeout_seconds"], gt=0, le=3600
    )
    max_admission_timeout_seconds: float = Field(
        default=_DEFAULTS["max_admission_timeout_seconds"], gt=0, le=3600
    )
    max_launch_attempts_per_occurrence: int = Field(
        default=_DEFAULTS["max_launch_attempts_per_occurrence"], ge=1, le=10
    )
    max_idle_ttl_seconds: float = Field(
        default=_DEFAULTS["max_idle_ttl_seconds"], gt=0, le=86400
    )
    lease_seconds: float = Field(default=_DEFAULTS["lease_seconds"], ge=10, le=3600)
    lease_loss_wait_timeout_seconds: float = Field(
        default=_DEFAULTS["lease_loss_wait_timeout_seconds"],
        ge=60,
        le=86400,
        description="After a worker loses its launch lease, how long it keeps tasks that wait on a person or subtask before failing them and exiting.",
    )
    max_consecutive_interruptions: int = Field(
        default=_DEFAULTS["max_consecutive_interruptions"],
        ge=1,
        le=10,
        description="Lost-lease interruptions in a row (per task, or per schedule for scheduled work) before the runtime stops retrying and asks the TeamLead to investigate.",
    )
    review_interval_seconds: float = Field(
        default=_DEFAULTS["review_interval_seconds"],
        ge=60,
        le=86400,
        description="How long a schedule may stay stuck (overdue and not running) before its owner, and administrators when only they can fix it, are told in the Inbox and in chat.",
    )
    missed_one_time_after_seconds: float = Field(
        default=_DEFAULTS["missed_one_time_after_seconds"],
        ge=60,
        le=604800,
        description="A schedule's last run (a one-time reminder, or the last of a counted series) this late is turned off and its owner told it was missed, so it never arrives late.",
    )
    retention_days: int = Field(default=_DEFAULTS["retention_days"], ge=1, le=365)
    defaults: ScheduleWorkerRulesV1 = Field(default_factory=ScheduleWorkerRulesV1)

    @model_validator(mode="after")
    def validate_defaults(self) -> "ScheduleWorkersConfig":
        self.resolve_rules({})
        return self

    def resolve_rules(self, overrides: dict[str, object]) -> ScheduleWorkerRulesV1:
        rules = ScheduleWorkerRulesV1.model_validate(
            {**self.defaults.model_dump(), **overrides}
        )
        for field in (
            "claim_wait_seconds",
            "startup_timeout_seconds",
            "admission_timeout_seconds",
            "max_launch_attempts_per_occurrence",
            "idle_ttl_seconds",
        ):
            value = getattr(rules, field)
            limit = (
                getattr(self, "max_" + field)
                if field != "max_launch_attempts_per_occurrence"
                else self.max_launch_attempts_per_occurrence
            )
            if value is not None and value > limit:
                raise ValueError(f"schedule_worker_rule_exceeds_operator_limit:{field}")
        return rules
