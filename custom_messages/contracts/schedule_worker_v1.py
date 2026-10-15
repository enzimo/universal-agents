"""Portable worker startup, schedule readiness, and occurrence custody contracts.

These records describe intent and evidence. None is a bearer authorization grant.
The owning services derive identities and validate current authority before effects.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from default_values import AGENT_DEFAULTS
from custom_messages.contracts.schedule_execution_v1 import (
    ScheduleExecutionScopeV1 as ScheduleExecutionScopeV1,
)


class WorkerContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


# Saved scheduled-worker settings (ScheduleWorkerBindingV1) in this format.
# Version 2 (access plan Phase 4c-4) dropped the removed permission system's
# fields; storage refuses older rows until ``assistant_cli access
# approve-existing`` rewrites them (storage.schedule_worker_repository).
SCHEDULE_WORKER_BINDING_SCHEMA_VERSION = 2
# How a worker policy was allowed: the agent held the tool through the access
# service (authorization on), the person agreed in conversation (off), or the
# trusted default startup policy for shipped worker types.
ScheduleWorkerAuthorityMode = Literal["held_access", "agreement", "runtime_default"]
# Who lacks the access a scheduled worker needs (ScheduleReadinessCheckV1).
AccessHolder = Literal["worker_type", "owner"]
# Why the launch store refused a new worker start (storage.agent_launch_repository);
# a schedule blocked by one of them says so in its notice
# (custom_agents.schedule_repair_guidance).
TEAM_WORKER_CAPACITY_LIMIT = "team_worker_capacity_limit"
AUTOMATIC_WORKER_CAPACITY_LIMIT = "automatic_worker_capacity_limit"
WORKER_LAUNCH_RATE_LIMIT = "worker_launch_rate_limit"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def worker_digest(value: Any) -> str:
    payload = (
        value.model_dump(mode="json", exclude_none=True)
        if isinstance(value, BaseModel)
        else value
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def occurrence_identity(schedule_id: str, due_at: str) -> str:
    due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    if due.tzinfo is None:
        raise ValueError("An occurrence requires a timezone-aware due time.")
    return (
        "sched_occ_"
        + worker_digest([schedule_id, due.astimezone(timezone.utc).isoformat()])[:32]
    )


class ScheduleWorkerRulesV1(WorkerContract):
    claim_wait_seconds: float = Field(
        default=AGENT_DEFAULTS.schedule_workers.value["defaults"]["claim_wait_seconds"],
        ge=0,
        le=86400,
    )
    startup_timeout_seconds: float = Field(
        default=AGENT_DEFAULTS.schedule_workers.value["defaults"][
            "startup_timeout_seconds"
        ],
        gt=0,
        le=3600,
    )
    admission_timeout_seconds: float = Field(
        default=AGENT_DEFAULTS.schedule_workers.value["defaults"][
            "admission_timeout_seconds"
        ],
        gt=0,
        le=3600,
    )
    max_launch_attempts_per_occurrence: int = Field(
        default=AGENT_DEFAULTS.schedule_workers.value["defaults"][
            "max_launch_attempts_per_occurrence"
        ],
        ge=1,
        le=10,
    )
    retry_backoff_seconds: float = Field(
        default=AGENT_DEFAULTS.schedule_workers.value["defaults"][
            "retry_backoff_seconds"
        ],
        gt=0,
        le=300,
    )
    launch_when_busy: bool = False
    idle_ttl_seconds: float | None = Field(default=None, gt=0, le=86400)
    # An interrupted run is not rerun when the schedule's next run is due
    # within this window; that run does the work instead of overlapping it.
    interrupted_run_skip_window_seconds: float = Field(
        default=AGENT_DEFAULTS.schedule_workers.value["defaults"][
            "interrupted_run_skip_window_seconds"
        ],
        ge=0,
        le=86400,
    )


class ScheduleWorkerPolicySpecV1(WorkerContract):
    enabled: bool = True
    profile_id: Literal["local"] = "local"
    rules: dict[str, Any] = Field(default_factory=dict)
    expires_at_utc: datetime | None = None

    @field_validator("expires_at_utc")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Policy expiry requires a timezone.")
        return value.astimezone(timezone.utc) if value else None

    @field_validator("rules")
    @classmethod
    def validate_rules(cls, value: dict[str, Any]) -> dict[str, Any]:
        ScheduleWorkerRulesV1.model_validate(value)
        return value


class ScheduleCapabilityRequirementV1(WorkerContract):
    """One tool a schedule needs: a tool catalog id and one of its operations.

    ``capability_id`` holds the catalog tool id (for example ``fn:web_research``,
    ``cli:google`` or ``mcp:github``) and ``operation`` one of that tool's
    catalog operations (a function tool's operation is the function name).
    The key keeps its name because every saved schedule stores it. Tool groups
    and capability ids such as ``builtin:web_research`` were removed (access
    plan Phase 7): a saved schedule that still names one is blocked with
    ``requirement_not_a_catalog_tool`` until TeamLead re-saves it
    (``custom_agents.schedule_repair_guidance``).
    """

    capability_id: str = Field(min_length=1, max_length=240)
    operation: str = Field(min_length=1, max_length=240)
    necessity: Literal["required", "conditional"] = "required"
    resource_constraints: dict[str, str] = Field(default_factory=dict)
    source: Literal["declared", "observed", "approved"] = "declared"


class ScheduleCapabilityRequirementsV1(WorkerContract):
    requirements: tuple[ScheduleCapabilityRequirementV1, ...] = Field(
        default=(), max_length=64
    )
    runtime_discovery: bool = True


class ScheduleReadinessCheckV1(WorkerContract):
    capability_id: str
    operation: str
    status: Literal["ready", "deferred", "blocked"]
    reason: str
    required: bool = True
    # The tool catalog operation this requirement needs, when the check could
    # resolve it, and who does not hold it yet (access that outlasts one task).
    tool_id: str | None = None
    access_operation: str | None = None
    access_level: Literal["read", "write", "full"] | None = None
    missing_access_for: tuple[AccessHolder, ...] = ()
    # For a requirement the checks cannot use: what is wrong in plain words and
    # the valid names to use instead (custom_agents.schedule_access).
    detail: str | None = None
    suggestions: tuple[str, ...] = ()


class ScheduleReadinessV1(WorkerContract):
    status: Literal["ready", "provisional", "blocked"]
    checks: tuple[ScheduleReadinessCheckV1, ...] = ()
    checked_at_utc: datetime = Field(default_factory=utc_now)
    execution_digest: str = ""
    policy_snapshot_digest: str | None = None


class ScheduleWorkerBindingV1(WorkerContract):
    schema_version: Literal[2] = SCHEDULE_WORKER_BINDING_SCHEMA_VERSION
    schedule_id: str
    team_id: str
    owner_canonical_user_id: str
    target_agent_type: str
    definition_digest: str = Field(
        description=(
            "Authored-definition fingerprint saved with this policy. Only a "
            "team-written definition is compared with it; a shipped definition "
            "changes only with a deploy and is never compared. The value is part "
            "of execution_digest and therefore of schedule-scoped approvals."
        )
    )
    execution_digest: str
    revision: int = Field(ge=1)
    policy: ScheduleWorkerPolicySpecV1
    capabilities: ScheduleCapabilityRequirementsV1 = Field(
        default_factory=ScheduleCapabilityRequirementsV1
    )
    observed_capabilities: tuple[ScheduleCapabilityRequirementV1, ...] = ()
    actor_principal_id: str
    authority_mode: ScheduleWorkerAuthorityMode
    created_at_utc: datetime = Field(default_factory=utc_now)
    blocked_reason: str | None = None
    blocked_since_utc: datetime | None = None
    # Unused since 2026-10-04: they paced the removed TeamLead review task
    # (stuck schedules are now reported by custom_agents.stuck_schedules).
    # Stored bindings still contain them; drop both in the next binding
    # schema version.
    next_review_at_utc: datetime | None = None
    review_generation: int = 0


AgentDefinitionSource = Literal["release", "team"]
# A definition file shipped inside the installed code (ua_definitions/).
DEFINITION_SOURCE_RELEASE: AgentDefinitionSource = "release"
# Any other definition file: team folders, generated specialists, operator paths.
DEFINITION_SOURCE_TEAM: AgentDefinitionSource = "team"


class ReleaseDefinitionChangeV1(WorkerContract):
    """What a deploy changed in a scheduled worker's tools or prompt."""

    changed_at_utc: datetime
    tools_added: tuple[str, ...] = ()
    tools_removed: tuple[str, ...] = ()
    prompt_changed: bool = False
    # Role, default tools or skills flag of a shipped definition.
    declaration_changed: bool = False


class WorkerDefinitionRecordV1(WorkerContract):
    """The target definition as saved with a schedule's worker policy.

    Kept beside the binding so a paused schedule can show what changed. It is
    evidence for review, never authority. Team-written definitions keep their
    own prompt text; shipped definitions keep only a fingerprint of theirs.
    """

    schedule_id: str
    team_id: str
    # The binding revision this record describes; a stale writer cannot replace
    # the record of a newer policy.
    policy_revision: int = Field(ge=1)
    agent_type: str
    source: AgentDefinitionSource
    source_path: str
    authored_digest: str
    launch_digest: str
    role: str
    own_prompt: str | None = None
    own_prompt_digest: str
    prompt_digest: str
    include_shared_prompt: bool
    backstory: str
    # Tool catalog ids or tool set ids, as the definition declares them.
    default_tools: tuple[str, ...] = ()
    skills_enabled: bool
    tool_names: tuple[str, ...] = ()
    recorded_at_utc: datetime = Field(default_factory=utc_now)
    last_release_change: ReleaseDefinitionChangeV1 | None = None


class WorkerDefinitionChangesV1(WorkerContract):
    """Differences between a saved definition record and the current definition."""

    requires_review: bool
    saved_record_available: bool
    source_before: AgentDefinitionSource | None = None
    source_after: AgentDefinitionSource
    role_before: str | None = None
    role_after: str
    own_prompt_changed: bool = False
    own_prompt_before: str | None = None
    own_prompt_after: str | None = None
    prompt_changed: bool = False
    include_shared_prompt_changed: bool = False
    backstory_changed: bool = False
    skills_enabled_changed: bool = False
    default_tools_added: tuple[str, ...] = ()
    default_tools_removed: tuple[str, ...] = ()
    tools_added: tuple[str, ...] = ()
    tools_removed: tuple[str, ...] = ()


class AgentLaunchState(StrEnum):
    RESERVED = "reserved"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RETIRED = "retired"


class AgentLaunchV1(WorkerContract):
    launch_id: str
    agent_id: str
    team_id: str
    owner_canonical_user_id: str
    named_agent_type: str
    definition_digest: str = Field(
        description=(
            "Full launch fingerprint, including tools and prompt text expanded by "
            "the running code. It stops one start if the definition changes "
            "between reservation and construction."
        )
    )
    profile_id: Literal["local"] = "local"
    host_id: str
    # manual: TeamArchitect's instantiate_agent; delegation: trusted runtime
    # starts (takeover of a gone worker's work, maintenance); schedule: a
    # scheduled job's worker; pool: the worker pool controller, for delegated
    # tasks waiting in a type's queue.
    source: Literal["manual", "delegation", "schedule", "pool"]
    schedule_id: str | None = None
    occurrence_id: str | None = None
    policy_revision: int | None = None
    idle_ttl_seconds: float | None = Field(default=None, gt=0)
    state: AgentLaunchState = AgentLaunchState.RESERVED
    fence: int = Field(default=1, ge=1)
    created_at_utc: datetime = Field(default_factory=utc_now)
    updated_at_utc: datetime = Field(default_factory=utc_now)
    lease_expires_at_utc: datetime
    ready_at_utc: datetime | None = None
    reason: str | None = None


class AgentLaunchResultV1(WorkerContract):
    status: Literal["starting", "ready", "denied", "failed", "waiting"]
    agent_id: str | None = None
    launch_id: str | None = None
    reason: str | None = None
    created: bool = False

    @property
    def accepted(self) -> bool:
        return self.status in {"starting", "ready"}


class AgentCreationResultV1(WorkerContract):
    request_id: str
    results: tuple[AgentLaunchResultV1, ...]

    @property
    def accepted(self) -> bool:
        return bool(self.results) and all(result.accepted for result in self.results)


class ScheduleOccurrenceV1(WorkerContract):
    occurrence_id: str
    schedule_id: str
    due_at: str
    task_id: str
    team_id: str
    owner_canonical_user_id: str
    named_agent_type: str
    execution_digest: str
    agent_id: str
    claim_token: str
    # "superseded": an interrupted run that was not rerun because the schedule
    # already ran again or its next run was due soon (worker_handback_runtime).
    state: Literal[
        "reserved", "admitted", "coalesced", "blocked", "finished", "superseded"
    ] = "reserved"
    created_at_utc: datetime = Field(default_factory=utc_now)
    admitted_at_utc: datetime | None = None
    finished_at_utc: datetime | None = None
    outcome: str | None = None
    policy_revision: int | None = None
    coalesced_into_task_id: str | None = None
    launch_id: str | None = None
    reason: str | None = None
