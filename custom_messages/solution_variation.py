"""Durable records for optional, evaluated solution variation."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from id_generation import new_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SolutionVariationStatus(StrEnum):
    """Lifecycle of one advisory solution comparison."""

    ACTIVE = "active"
    COMPLETED = "completed"
    NO_SELECTION = "no_selection"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"


class SolutionEvaluationOutcome(StrEnum):
    """Evaluator-authored disposition; this is not canonical verification."""

    SUPPORTS = "supports"
    REJECTS = "rejects"
    INCONCLUSIVE = "inconclusive"


class SolutionMetricDirection(StrEnum):
    """How an optional quantitative metric should be interpreted."""

    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"
    MEET = "meet"


class SolutionEvaluationMetric(BaseModel):
    """One bounded qualitative or quantitative comparison result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion: str
    result: str
    score: Optional[float] = None
    unit: Optional[str] = None
    direction: Optional[SolutionMetricDirection] = None


class SolutionVariationRun(BaseModel):
    """Manager-owned sidecar for one optional comparison of solution paths."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default_factory=lambda: new_id("solvar_run"))
    purpose: str
    evaluation_criteria: List[str]
    constraints: List[str] = Field(default_factory=list)
    candidate_limit: int = 3
    resource_budget: Optional[str] = None
    stop_condition: Optional[str] = None
    owner_agent_id: str
    owner_agent_name: Optional[str] = None
    owner_access_scope: str
    team_id: Optional[str] = None
    source_task_id: str
    scope_key: str
    plan_id: Optional[str] = None
    plan_step_id: Optional[str] = None
    objective_id: Optional[str] = None
    status: SolutionVariationStatus = SolutionVariationStatus.ACTIVE
    selected_candidate_key: Optional[str] = None
    selected_evaluation_key: Optional[str] = None
    selection_rationale: Optional[str] = None
    close_summary: Optional[str] = None
    revision: int = 1
    created_at: str = Field(default_factory=_utc_now)
    updated_at: str = Field(default_factory=_utc_now)


class SolutionCandidate(BaseModel):
    """Immutable candidate hypothesis within a solution variation run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(default_factory=lambda: new_id("solvar_cand"))
    run_id: str
    candidate_key: str
    parent_candidate_key: Optional[str] = None
    title: str
    hypothesis: str
    approach: str
    artifact_refs: List[str] = Field(default_factory=list)
    created_by_agent_id: Optional[str] = None
    created_by_agent_name: Optional[str] = None
    created_at: str = Field(default_factory=_utc_now)


class SolutionEvaluation(BaseModel):
    """Immutable agent-authored evaluation and its supporting references."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: str = Field(default_factory=lambda: new_id("solvar_eval"))
    run_id: str
    candidate_key: str
    evaluation_key: str
    outcome: SolutionEvaluationOutcome
    summary: str
    hard_constraints_passed: bool
    metrics: List[SolutionEvaluationMetric] = Field(default_factory=list)
    evidence_refs: List[str] = Field(default_factory=list)
    limitations: Optional[str] = None
    evaluated_by_agent_id: Optional[str] = None
    evaluated_by_agent_name: Optional[str] = None
    created_at: str = Field(default_factory=_utc_now)
