"""Manager coordination for optional evaluated solution variation."""

from __future__ import annotations

from custom_agents.task_manager_ports import SolutionVariationCoordinatorOperations
from custom_agents.task_runtime_state import RuntimeOwnerIdentity
from custom_agents.task_runtime_state import PlanRuntimeState
import logging
from storage.solution_variation_repository import SolutionVariationRepository

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from custom_messages.enums import (
    TaskHistoryImportance,
    TaskHistorySource,
    TaskHistoryType,
)
from custom_messages.solution_variation import (
    SolutionCandidate,
    SolutionEvaluation,
    SolutionEvaluationMetric,
    SolutionEvaluationOutcome,
    SolutionVariationRun,
    SolutionVariationStatus,
)
from storage.solution_variation_repository import SolutionVariationBundle


_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_MAX_CRITERIA = 12
_MAX_CONSTRAINTS = 12
_MAX_REFS = 20
_MAX_METRICS = 12
_MAX_EVALUATIONS_PER_CANDIDATE = 10


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class _VariationContext:
    owner_agent_id: str
    owner_access_scope: str
    source_task_id: str
    task_scope_key: str
    plan_id: Optional[str]
    plan_scope_key: Optional[str]
    objective_id: Optional[str]
    plan_step_id: Optional[str]

    @property
    def current_scope_key(self) -> str:
        return self.plan_scope_key or self.task_scope_key


class SolutionVariationCoordinator:
    """Own advisory comparison lineage without executing candidate effects."""

    def __init__(
        self,
        *,
        operations: SolutionVariationCoordinatorOperations,
        identity: RuntimeOwnerIdentity,
        plan_state: PlanRuntimeState,
        logger: logging.Logger,
        solution_variation_repository: SolutionVariationRepository,
    ) -> None:
        self.operations = operations
        self.identity = identity
        self.plan_state = plan_state
        self._logger = logger
        self.solution_variation_repository = solution_variation_repository


    @staticmethod
    def _bounded_text(value: str, *, field_name: str, limit: int) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field_name} is required.")
        if len(normalized) > limit:
            raise ValueError(f"{field_name} must be at most {limit} characters.")
        return normalized

    @classmethod
    def _bounded_strings(
        cls,
        values: Iterable[Any],
        *,
        field_name: str,
        limit: int,
        item_limit: int = 500,
        required: bool = False,
    ) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw or "").strip()
            if not value or value in seen:
                continue
            if len(value) > item_limit:
                raise ValueError(
                    f"Each {field_name} item must be at most {item_limit} characters."
                )
            result.append(value)
            seen.add(value)
        if required and not result:
            raise ValueError(f"{field_name} must contain at least one item.")
        if len(result) > limit:
            raise ValueError(f"{field_name} may contain at most {limit} items.")
        return result

    @staticmethod
    def _key(value: str, *, field_name: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _KEY_PATTERN.fullmatch(normalized):
            raise ValueError(
                f"{field_name} must be 1-64 lowercase letters, numbers, '.', '_', or '-', "
                "starting with a letter or number."
            )
        return normalized

    def _context(self, task_id: str) -> _VariationContext:
        if not self.operations.plans_enabled():
            raise PermissionError(
                "Solution variation is available only to TeamArchitect."
            )
        task_info = self.operations.get_task_info(task_id)
        if task_info is None:
            raise ValueError(f"Current task {task_id} is not active.")
        owner_agent_id = self.operations.durable_manager_scope_id()
        if not owner_agent_id:
            raise RuntimeError("TeamArchitect owner identity is not initialized.")
        plan_step_link = self.plan_state.plan_step_task_links.get(task_id)
        plan_id = self.operations.get_task_plan_id(task_id)
        plan = self.operations.get_plan(plan_id) if plan_id else None
        stable_task_id = (
            getattr(plan_step_link, "root_task_id", None)
            or getattr(plan, "root_task_id", None)
            or self.operations.get_top_level_task_id(task_id)
            or task_id
        )
        # The task's person, recorded at admission (TaskInfo.owner_person_id).
        canonical_user_id = task_info.owner_person_id
        owner_access_scope = (
            f"user:{canonical_user_id}"
            if canonical_user_id
            else f"task:{stable_task_id}"
        )
        objective_id = self.operations.get_task_objective_id(task_id)
        plan_step_id = getattr(plan_step_link, "step_id", None)
        return _VariationContext(
            owner_agent_id=owner_agent_id,
            owner_access_scope=owner_access_scope,
            source_task_id=task_id,
            task_scope_key=f"task:{stable_task_id}",
            plan_id=plan_id,
            plan_scope_key=f"plan:{plan_id}" if plan_id else None,
            objective_id=objective_id,
            plan_step_id=plan_step_id,
        )

    def _resolve(
        self,
        task_id: str,
        *,
        include_closed: bool = False,
    ) -> tuple[_VariationContext, Optional[SolutionVariationBundle]]:
        context = self._context(task_id)
        if context.plan_id and context.plan_scope_key:
            run, promoted = (
                self.solution_variation_repository.promote_active_task_run_to_plan(
                    owner_agent_id=context.owner_agent_id,
                    owner_access_scope=context.owner_access_scope,
                    task_scope_key=context.task_scope_key,
                    plan_scope_key=context.plan_scope_key,
                    plan_id=context.plan_id,
                )
            )
            if promoted and run is not None:
                self._logger.info(
                    "Promoted active solution variation %s from %s to %s.",
                    run.run_id,
                    context.task_scope_key,
                    context.plan_scope_key,
                )
        else:
            run = self.solution_variation_repository.get_active_run(
                owner_agent_id=context.owner_agent_id,
                owner_access_scope=context.owner_access_scope,
                scope_key=context.task_scope_key,
            )

        if run is None and include_closed:
            scope_keys = [context.task_scope_key]
            if context.plan_scope_key:
                scope_keys.insert(0, context.plan_scope_key)
            latest_runs = [
                candidate
                for scope_key in scope_keys
                if (
                    candidate := self.solution_variation_repository.get_latest_run(
                        owner_agent_id=context.owner_agent_id,
                        owner_access_scope=context.owner_access_scope,
                        scope_key=scope_key,
                    )
                )
                is not None
            ]
            run = max(
                latest_runs,
                key=lambda candidate: (
                    candidate.updated_at,
                    candidate.created_at,
                    candidate.run_id,
                ),
                default=None,
            )

        if run is None:
            return context, None
        bundle = self.solution_variation_repository.get_bundle(
            run.run_id,
            owner_agent_id=run.owner_agent_id,
            owner_access_scope=run.owner_access_scope,
        )
        if bundle is None:
            raise RuntimeError(
                f"Solution variation {run.run_id} could not be reloaded."
            )
        return context, bundle

    def _active_bundle(self, task_id: str) -> SolutionVariationBundle:
        _, bundle = self._resolve(task_id)
        if bundle is None:
            raise ValueError(
                "No active solution variation exists for the current task or plan."
            )
        return bundle

    def _history(
        self,
        run: SolutionVariationRun,
        summary: str,
        *,
        action: str,
        agent_id: Optional[str],
        agent_name: Optional[str],
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        writer = (
            self.operations.append_history_entry
            if self.operations.get_task_info(run.source_task_id) is not None
            else self.operations.append_history_entry_to_archived_task
        )
        try:
            writer(
                run.source_task_id,
                summary,
                source=TaskHistorySource.AGENT,
                entry_type=TaskHistoryType.PLAN_EVENT,
                importance=TaskHistoryImportance.NORMAL,
                agent_id=agent_id,
                agent_name=agent_name,
                plan_step_id=run.plan_step_id,
                tracks_to_plan=bool(run.plan_id),
                details={
                    "action": action,
                    "solution_variation_run_id": run.run_id,
                    **(details or {}),
                },
            )
        except Exception as exc:
            self._logger.warning(
                "Failed to project solution variation %s into task history: %s",
                run.run_id,
                exc,
            )

    @staticmethod
    def _run_contract(run: SolutionVariationRun) -> dict[str, Any]:
        return run.model_dump(
            mode="json",
            include={
                "purpose",
                "evaluation_criteria",
                "constraints",
                "candidate_limit",
                "resource_budget",
                "stop_condition",
            },
        )

    @staticmethod
    def _candidate_contract(candidate: SolutionCandidate) -> dict[str, Any]:
        return candidate.model_dump(
            mode="json",
            exclude={
                "candidate_id",
                "created_at",
                "created_by_agent_id",
                "created_by_agent_name",
            },
        )

    @staticmethod
    def _evaluation_contract(evaluation: SolutionEvaluation) -> dict[str, Any]:
        return evaluation.model_dump(
            mode="json",
            exclude={
                "evaluation_id",
                "created_at",
                "evaluated_by_agent_id",
                "evaluated_by_agent_name",
            },
        )

    def start(
        self,
        task_id: str,
        *,
        purpose: str,
        evaluation_criteria: Iterable[Any],
        constraints: Iterable[Any] = (),
        candidate_limit: int = 3,
        resource_budget: Optional[str] = None,
        stop_condition: Optional[str] = None,
        agent_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> tuple[SolutionVariationRun, bool]:
        context, active_bundle = self._resolve(task_id)
        safe_limit = int(candidate_limit)
        if safe_limit < 2 or safe_limit > 8:
            raise ValueError("candidate_limit must be between 2 and 8.")
        run = SolutionVariationRun(
            purpose=self._bounded_text(purpose, field_name="purpose", limit=2000),
            evaluation_criteria=self._bounded_strings(
                evaluation_criteria,
                field_name="evaluation_criteria",
                limit=_MAX_CRITERIA,
                required=True,
            ),
            constraints=self._bounded_strings(
                constraints,
                field_name="constraints",
                limit=_MAX_CONSTRAINTS,
            ),
            candidate_limit=safe_limit,
            resource_budget=(
                self._bounded_text(
                    resource_budget, field_name="resource_budget", limit=500
                )
                if str(resource_budget or "").strip()
                else None
            ),
            stop_condition=(
                self._bounded_text(
                    stop_condition, field_name="stop_condition", limit=500
                )
                if str(stop_condition or "").strip()
                else None
            ),
            owner_agent_id=context.owner_agent_id,
            owner_agent_name=self.identity.owner_agent_name,
            owner_access_scope=context.owner_access_scope,
            team_id=self.identity.team_id,
            source_task_id=context.source_task_id,
            scope_key=context.current_scope_key,
            plan_id=context.plan_id,
            plan_step_id=context.plan_step_id,
            objective_id=context.objective_id,
        )
        if active_bundle is None:
            stored, created = self.solution_variation_repository.start_run(run)
        else:
            stored, created = active_bundle.run, False
        if not created and self._run_contract(stored) != self._run_contract(run):
            raise ValueError(
                "A different active solution variation already exists for this task or plan. "
                "Inspect or close it before starting another."
            )
        if created:
            self._history(
                stored,
                "Started an optional evaluated solution variation.",
                action="solution_variation_started",
                agent_id=agent_id,
                agent_name=agent_name,
                details={
                    "purpose": stored.purpose,
                    "candidate_limit": stored.candidate_limit,
                },
            )
        return stored, created

    def inspect(
        self,
        task_id: str,
        *,
        include_closed: bool = False,
    ) -> SolutionVariationBundle:
        _, bundle = self._resolve(task_id, include_closed=include_closed)
        if bundle is None:
            qualifier = "" if include_closed else " active"
            raise ValueError(
                f"No{qualifier} solution variation exists for the current task or plan."
            )
        return bundle

    def record_candidate(
        self,
        task_id: str,
        *,
        candidate_key: str,
        title: str,
        hypothesis: str,
        approach: str,
        artifact_refs: Iterable[Any] = (),
        parent_candidate_key: Optional[str] = None,
        agent_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> tuple[SolutionCandidate, bool]:
        bundle = self._active_bundle(task_id)
        if bundle.run.selected_candidate_key:
            raise ValueError(
                "This variation already has a selected recommendation; close or supersede it before branching again."
            )
        key = self._key(candidate_key, field_name="candidate_key")
        existing_by_key = {item.candidate_key: item for item in bundle.candidates}
        parent_key = (
            self._key(parent_candidate_key, field_name="parent_candidate_key")
            if str(parent_candidate_key or "").strip()
            else None
        )
        if parent_key and parent_key not in existing_by_key:
            raise ValueError(
                "parent_candidate_key must identify an existing candidate in this run."
            )
        candidate = SolutionCandidate(
            run_id=bundle.run.run_id,
            candidate_key=key,
            parent_candidate_key=parent_key,
            title=self._bounded_text(title, field_name="title", limit=300),
            hypothesis=self._bounded_text(
                hypothesis, field_name="hypothesis", limit=2000
            ),
            approach=self._bounded_text(approach, field_name="approach", limit=4000),
            artifact_refs=self._bounded_strings(
                artifact_refs,
                field_name="artifact_refs",
                limit=_MAX_REFS,
                item_limit=1000,
            ),
            created_by_agent_id=agent_id,
            created_by_agent_name=agent_name,
        )
        if (
            key not in existing_by_key
            and len(bundle.candidates) >= bundle.run.candidate_limit
        ):
            raise ValueError(
                f"Candidate limit {bundle.run.candidate_limit} has been reached for this run."
            )
        stored, created = self.solution_variation_repository.record_candidate(
            candidate,
            owner_agent_id=bundle.run.owner_agent_id,
            owner_access_scope=bundle.run.owner_access_scope,
        )
        if not created and self._candidate_contract(stored) != self._candidate_contract(
            candidate
        ):
            raise ValueError(
                f"candidate_key {key!r} already exists with different content. "
                "Record a child candidate with a new key for revisions."
            )
        if created:
            self._history(
                bundle.run,
                f"Recorded solution candidate `{key}`: {candidate.title}",
                action="solution_candidate_recorded",
                agent_id=agent_id,
                agent_name=agent_name,
                details={"candidate_key": key, "parent_candidate_key": parent_key},
            )
        return stored, created

    def record_evaluation(
        self,
        task_id: str,
        *,
        candidate_key: str,
        evaluation_key: str,
        outcome: SolutionEvaluationOutcome | str,
        summary: str,
        hard_constraints_passed: bool,
        metrics: Iterable[Any] = (),
        evidence_refs: Iterable[Any] = (),
        limitations: Optional[str] = None,
        agent_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> tuple[SolutionEvaluation, bool]:
        bundle = self._active_bundle(task_id)
        if bundle.run.selected_candidate_key:
            raise ValueError(
                "This variation already has a selected recommendation; close or supersede it before evaluating revisions."
            )
        candidate_key = self._key(candidate_key, field_name="candidate_key")
        evaluation_key = self._key(evaluation_key, field_name="evaluation_key")
        if candidate_key not in {item.candidate_key for item in bundle.candidates}:
            raise ValueError(
                "candidate_key must identify an existing candidate in this run."
            )
        existing_for_candidate = [
            item for item in bundle.evaluations if item.candidate_key == candidate_key
        ]
        metric_records = [
            item
            if isinstance(item, SolutionEvaluationMetric)
            else SolutionEvaluationMetric.model_validate(item)
            for item in metrics
        ]
        if len(metric_records) > _MAX_METRICS:
            raise ValueError(f"metrics may contain at most {_MAX_METRICS} items.")
        parsed_outcome = (
            outcome
            if isinstance(outcome, SolutionEvaluationOutcome)
            else SolutionEvaluationOutcome(str(outcome).strip().lower())
        )
        evaluation = SolutionEvaluation(
            run_id=bundle.run.run_id,
            candidate_key=candidate_key,
            evaluation_key=evaluation_key,
            outcome=parsed_outcome,
            summary=self._bounded_text(summary, field_name="summary", limit=4000),
            hard_constraints_passed=bool(hard_constraints_passed),
            metrics=metric_records,
            evidence_refs=self._bounded_strings(
                evidence_refs,
                field_name="evidence_refs",
                limit=_MAX_REFS,
                item_limit=1000,
            ),
            limitations=(
                self._bounded_text(limitations, field_name="limitations", limit=2000)
                if str(limitations or "").strip()
                else None
            ),
            evaluated_by_agent_id=agent_id,
            evaluated_by_agent_name=agent_name,
        )
        existing_keys = {item.evaluation_key for item in existing_for_candidate}
        if (
            evaluation_key not in existing_keys
            and len(existing_for_candidate) >= _MAX_EVALUATIONS_PER_CANDIDATE
        ):
            raise ValueError(
                f"A candidate may have at most {_MAX_EVALUATIONS_PER_CANDIDATE} evaluations."
            )
        stored, created = self.solution_variation_repository.record_evaluation(
            evaluation,
            owner_agent_id=bundle.run.owner_agent_id,
            owner_access_scope=bundle.run.owner_access_scope,
        )
        if not created and self._evaluation_contract(
            stored
        ) != self._evaluation_contract(evaluation):
            raise ValueError(
                f"evaluation_key {evaluation_key!r} already exists with different content for candidate "
                f"{candidate_key!r}."
            )
        if created:
            self._history(
                bundle.run,
                f"Evaluated solution candidate `{candidate_key}`: {parsed_outcome.value}.",
                action="solution_candidate_evaluated",
                agent_id=agent_id,
                agent_name=agent_name,
                details={
                    "candidate_key": candidate_key,
                    "evaluation_key": evaluation_key,
                    "outcome": parsed_outcome.value,
                    "hard_constraints_passed": bool(hard_constraints_passed),
                },
            )
        return stored, created

    def select(
        self,
        task_id: str,
        *,
        candidate_key: str,
        evaluation_key: str,
        rationale: str,
        agent_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> tuple[SolutionVariationRun, bool]:
        bundle = self._active_bundle(task_id)
        candidate_key = self._key(candidate_key, field_name="candidate_key")
        evaluation_key = self._key(evaluation_key, field_name="evaluation_key")
        if bundle.run.selected_candidate_key:
            if (
                bundle.run.selected_candidate_key == candidate_key
                and bundle.run.selected_evaluation_key == evaluation_key
            ):
                return bundle.run, False
            raise ValueError(
                "This variation already has a different selected recommendation. "
                "Close it as superseded before starting another comparison."
            )
        if candidate_key not in {item.candidate_key for item in bundle.candidates}:
            raise ValueError(
                "candidate_key must identify an existing candidate in this run."
            )
        evaluation = next(
            (
                item
                for item in bundle.evaluations
                if item.candidate_key == candidate_key
                and item.evaluation_key == evaluation_key
            ),
            None,
        )
        if evaluation is None:
            raise ValueError(
                "Selection requires an evaluation recorded for the selected candidate."
            )
        if not evaluation.hard_constraints_passed:
            raise ValueError(
                "A candidate that failed hard constraints cannot be selected."
            )
        if evaluation.outcome != SolutionEvaluationOutcome.SUPPORTS:
            raise ValueError(
                "The cited selection evaluation must support the candidate."
            )
        expected_revision = bundle.run.revision
        updated = bundle.run.model_copy(
            update={
                "selected_candidate_key": candidate_key,
                "selected_evaluation_key": evaluation_key,
                "selection_rationale": self._bounded_text(
                    rationale,
                    field_name="rationale",
                    limit=4000,
                ),
                "revision": expected_revision + 1,
                "updated_at": _utc_now(),
            }
        )
        if not self.solution_variation_repository.update_run(
            updated,
            expected_revision=expected_revision,
        ):
            current = self._active_bundle(task_id).run
            if (
                current.selected_candidate_key == candidate_key
                and current.selected_evaluation_key == evaluation_key
            ):
                return current, False
            raise RuntimeError(
                "The solution variation changed concurrently; inspect it before selecting."
            )
        self._history(
            updated,
            f"Selected solution candidate `{candidate_key}` as the advisory recommendation.",
            action="solution_candidate_selected",
            agent_id=agent_id,
            agent_name=agent_name,
            details={
                "candidate_key": candidate_key,
                "evaluation_key": evaluation_key,
                "selection_is_advisory": True,
                "artifact_applied": False,
            },
        )
        return updated, True

    def close(
        self,
        task_id: str,
        *,
        outcome: SolutionVariationStatus | str,
        summary: str,
        agent_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> tuple[SolutionVariationRun, bool]:
        bundle = self._active_bundle(task_id)
        status = (
            outcome
            if isinstance(outcome, SolutionVariationStatus)
            else SolutionVariationStatus(str(outcome).strip().lower())
        )
        if status == SolutionVariationStatus.ACTIVE:
            raise ValueError("close outcome cannot be active.")
        if (
            status == SolutionVariationStatus.COMPLETED
            and not bundle.run.selected_candidate_key
        ):
            raise ValueError(
                "A completed variation requires a selected recommendation."
            )
        expected_revision = bundle.run.revision
        updated = bundle.run.model_copy(
            update={
                "status": status,
                "close_summary": self._bounded_text(
                    summary,
                    field_name="summary",
                    limit=4000,
                ),
                "revision": expected_revision + 1,
                "updated_at": _utc_now(),
            }
        )
        if not self.solution_variation_repository.update_run(
            updated,
            expected_revision=expected_revision,
        ):
            raise RuntimeError(
                "The solution variation changed concurrently; inspect it before closing."
            )
        self._history(
            updated,
            f"Closed solution variation as `{status.value}`.",
            action="solution_variation_closed",
            agent_id=agent_id,
            agent_name=agent_name,
            details={"outcome": status.value},
        )
        return updated, True
