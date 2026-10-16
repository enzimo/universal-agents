"""Objective lifecycle coordination for TaskManager."""

from __future__ import annotations

from custom_agents.task_manager_ports import ObjectiveCoordinatorOperations
from custom_agents.task_runtime_state import RuntimeOwnerIdentity
from custom_agents.task_runtime_state import ObjectiveRuntimeState
from custom_agents.task_runtime_state import PlanRuntimeState
from custom_agents.task_runtime_state import TaskRuntimeState
import logging
from storage.durable_owner_scope_repository import DurableOwnerScopeRepository
from storage.objective_repository import ObjectiveRepository, ObjectiveView
from storage.objective_work_repository import ObjectiveWorkRepository
from custom_agents.task_manager_plans import PlanCoordinator

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from custom_agents.task_records import TaskInfo
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ObjectiveCompletionPolicy, ObjectiveCriterionStatus, ObjectiveEventType, ObjectiveHorizon, ObjectivePriority, ObjectiveStatus, PlanStatus, ProgressState, RequesterType, TaskActivityStage, TaskActivityType, TaskHistoryImportance, TaskHistorySource, TaskHistoryType
from custom_messages.records.objectives import ObjectiveEventRecord, ObjectiveHeartbeatLink, ObjectiveRecord, ObjectiveScheduleLink, ObjectiveSuccessCriterion
from custom_messages.records.plans import PlanRecord
from id_generation import new_id


class ObjectiveSaveFailed(RuntimeError):
    """An Objective change was not written to the store.

    Raised by ``ObjectiveCoordinator._save_objective`` for changes that a tool
    call or a person's command asked for, so the reply says the change was
    not saved instead of returning the changed record as if it were.
    """

    def __init__(self, objective_id: str) -> None:
        super().__init__(
            f"The change to Objective {objective_id} was not saved. "
            "Try again later; if it keeps failing, tell the person."
        )
        self.objective_id = objective_id


class ObjectiveCoordinator:
    """Manager-owned Objective creation, linking, progress, and lifecycle."""

    def __init__(
        self,
        *,
        operations: ObjectiveCoordinatorOperations,
        identity: RuntimeOwnerIdentity,
        objective_state: ObjectiveRuntimeState,
        plan_state: PlanRuntimeState,
        task_state: TaskRuntimeState,
        logger: logging.Logger,
        durable_owner_scope_repository: DurableOwnerScopeRepository,
        objective_repository: ObjectiveRepository,
        objective_work_repository: ObjectiveWorkRepository | None,
        plan_coordinator: PlanCoordinator,
    ) -> None:
        self.operations = operations
        self.identity = identity
        self.objective_state = objective_state
        self.plan_state = plan_state
        self.task_state = task_state
        self._logger = logger
        self.durable_owner_scope_repository = durable_owner_scope_repository
        self.objective_repository = objective_repository
        self.objective_work_repository = objective_work_repository
        self.plan_coordinator = plan_coordinator


    @staticmethod
    def _objective_terminal_statuses() -> set[ObjectiveStatus]:
        return {
            ObjectiveStatus.SATISFIED,
            ObjectiveStatus.FAILED,
            ObjectiveStatus.CANCELLED,
            ObjectiveStatus.ABANDONED,
            ObjectiveStatus.DELETED,
        }

    @staticmethod
    def _plan_terminal_statuses() -> set[PlanStatus]:
        return {
            PlanStatus.COMPLETED,
            PlanStatus.FAILED,
            PlanStatus.CANCELLED,
            PlanStatus.ABANDONED,
        }

    def _save_objective(self, objective: ObjectiveRecord) -> None:
        """Write the Objective, or raise ``ObjectiveSaveFailed``.

        For changes a tool call or a person's command asked for: the caller
        reports that nothing was saved. A step that goes on without the write,
        because its own records are already stored, uses
        ``_save_objective_logged``. The plan and task lookups change only after
        the write, so they never point at a change the store does not have.
        """
        if not self.operations.objectives_enabled():
            self._logger.error(
                "An Objective change was not saved because Objectives are off. "
                "event=objective_save_failed objective_id=%s reason=objectives_disabled",
                objective.objective_id,
            )
            raise ObjectiveSaveFailed(objective.objective_id)
        now_ts = datetime.now(timezone.utc).isoformat()
        objective.owner_agent_id = self.operations.durable_manager_scope_id()
        objective.owner_agent_name = objective.owner_agent_name or self.identity.owner_agent_name
        objective.team_id = objective.team_id or self.identity.team_id
        objective.owner_named_agent_type = objective.owner_named_agent_type or self.identity.owner_named_agent_type
        objective.updated_at = now_ts
        try:
            self.objective_repository.save_objective(objective)
        except Exception as exc:
            self._logger.error(
                "An Objective change was not saved. event=objective_save_failed objective_id=%s error=%s",
                objective.objective_id,
                exc,
            )
            raise ObjectiveSaveFailed(objective.objective_id) from exc
        if objective.current_plan_id:
            self.objective_state.plan_to_objective_id[objective.current_plan_id] = objective.objective_id
        for plan_id in objective.plan_ids:
            if plan_id:
                self.objective_state.plan_to_objective_id[plan_id] = objective.objective_id
        for task_id in objective.related_task_ids + objective.recovery_task_ids:
            if task_id:
                self.objective_state.task_to_objective_id[task_id] = objective.objective_id

    def _save_objective_logged(self, objective: ObjectiveRecord, *, step: str) -> bool:
        """Write the Objective for a step that goes on without it; False when the write failed.

        For trusted steps nobody waits on: a task starting, ending or gaining
        a subtask, a plan following its task, a work session recording its
        progress. Their own records are stored elsewhere, so only the
        Objective's copy of the change (an event, a link, a review) is lost,
        and the failure is logged with ``step``.
        """
        try:
            self._save_objective(objective)
        except ObjectiveSaveFailed:
            self._logger.warning(
                "Went on without the Objective change. event=objective_save_skipped objective_id=%s step=%s",
                objective.objective_id,
                step,
            )
            return False
        return True

    def _append_objective_event(
        self,
        objective: ObjectiveRecord,
        event_type: ObjectiveEventType,
        summary: str,
        *,
        task_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        schedule_id: Optional[str] = None,
        heartbeat_item_id: Optional[str] = None,
        user_visible: bool = False,
        details: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> ObjectiveEventRecord:
        event = ObjectiveEventRecord(
            event_type=event_type,
            summary=summary.strip(),
            task_id=task_id,
            plan_id=plan_id,
            schedule_id=schedule_id,
            heartbeat_item_id=heartbeat_item_id,
            user_visible=user_visible,
            details=details or {},
            created_at=timestamp or datetime.now(timezone.utc).isoformat(),
        )
        objective.event_log.append(event)
        if len(objective.event_log) > 200:
            objective.event_log = objective.event_log[-200:]
        return event

    def _get_objective_record(self, objective_id: Optional[str]) -> Optional[ObjectiveRecord]:
        if not objective_id or not self.operations.objectives_enabled() or not self.identity.owner_agent_id:
            return None
        try:
            return self.objective_repository.get_objective(
                objective_id,
                owner_agent_id=self.operations.durable_manager_scope_id(),
            )
        except Exception as exc:
            self._logger.error("Failed to load objective %s: %s", objective_id, exc)
            return None

    def _get_objective_for_plan_id(self, plan_id: Optional[str]) -> Optional[ObjectiveRecord]:
        if not plan_id or not self.operations.objectives_enabled() or not self.identity.owner_agent_id:
            return None
        objective_id = self.objective_state.plan_to_objective_id.get(plan_id)
        if objective_id:
            objective = self._get_objective_record(objective_id)
            if objective is not None:
                return objective
        try:
            objective = self.objective_repository.get_objective_by_plan(
                plan_id,
                owner_agent_id=self.operations.durable_manager_scope_id(),
            )
        except Exception as exc:
            self._logger.error("Failed to load objective for plan %s: %s", plan_id, exc)
            return None
        if objective is not None:
            self.objective_state.plan_to_objective_id[plan_id] = objective.objective_id
        return objective

    def _get_task_objective_id_from_context(self, task_id: Optional[str]) -> Optional[str]:
        if not task_id or not self.operations.objectives_enabled():
            return None
        existing_objective_id = self.objective_state.task_to_objective_id.get(task_id)
        if existing_objective_id:
            return existing_objective_id

        task_info = self.operations.get_task_info(task_id)
        meta_objective_id = getattr(task_info.meta, "objective_id", None) if task_info else None
        if meta_objective_id:
            self.objective_state.task_to_objective_id[task_id] = meta_objective_id
            return meta_objective_id

        parent_task_id = None
        if task_info is not None:
            parent_task_id = task_info.task.parent_task_id
        parent_task_id = parent_task_id or self.task_state.subtask_to_parent_map.get(task_id)
        if parent_task_id:
            parent_objective_id = self._get_task_objective_id_from_context(parent_task_id)
            if parent_objective_id:
                self.objective_state.task_to_objective_id[task_id] = parent_objective_id
                return parent_objective_id

        plan_id = self.plan_state.task_to_plan_id.get(task_id)
        if plan_id:
            objective_id = self.objective_state.plan_to_objective_id.get(plan_id)
            if objective_id:
                self.objective_state.task_to_objective_id[task_id] = objective_id
                return objective_id
            objective = self._get_objective_for_plan_id(plan_id)
            if objective is not None:
                self.objective_state.task_to_objective_id[task_id] = objective.objective_id
                return objective.objective_id
        if self.identity.owner_agent_id:
            try:
                objective = self.objective_repository.get_objective_by_task(
                    task_id,
                    owner_agent_id=self.operations.durable_manager_scope_id(),
                )
            except Exception as exc:
                self._logger.error("Failed to load objective for task %s: %s", task_id, exc)
                objective = None
            if objective is not None:
                self.objective_state.task_to_objective_id[task_id] = objective.objective_id
                if objective.current_plan_id:
                    self.objective_state.plan_to_objective_id[objective.current_plan_id] = objective.objective_id
                for objective_plan_id in objective.plan_ids:
                    self.objective_state.plan_to_objective_id[objective_plan_id] = objective.objective_id
                return objective.objective_id
        return None

    def _get_task_objective_record(self, task_id: Optional[str]) -> Optional[ObjectiveRecord]:
        return self._get_objective_record(self._get_task_objective_id_from_context(task_id))

    def _objective_activity_context(
        self,
        task_id: Optional[str],
        plan_id: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        objective = self._get_task_objective_record(task_id)
        if objective is None:
            objective = self._get_objective_for_plan_id(plan_id)
        if objective is None:
            return {"objective_id": None, "objective_title": None}
        return {"objective_id": objective.objective_id, "objective_title": objective.title}

    @staticmethod
    def _coerce_objective_status(status: ObjectiveStatus | str) -> ObjectiveStatus:
        return status if isinstance(status, ObjectiveStatus) else ObjectiveStatus(str(status))

    @staticmethod
    def _coerce_objective_horizon(horizon: ObjectiveHorizon | str) -> ObjectiveHorizon:
        return horizon if isinstance(horizon, ObjectiveHorizon) else ObjectiveHorizon(str(horizon))

    @staticmethod
    def _coerce_objective_priority(priority: ObjectivePriority | str) -> ObjectivePriority:
        return priority if isinstance(priority, ObjectivePriority) else ObjectivePriority(str(priority))

    @staticmethod
    def _coerce_objective_completion_policy(
        completion_policy: ObjectiveCompletionPolicy | str,
    ) -> ObjectiveCompletionPolicy:
        return (
            completion_policy
            if isinstance(completion_policy, ObjectiveCompletionPolicy)
            else ObjectiveCompletionPolicy(str(completion_policy))
        )

    @staticmethod
    def _coerce_objective_criterion_status(
        status: ObjectiveCriterionStatus | str,
    ) -> ObjectiveCriterionStatus:
        return status if isinstance(status, ObjectiveCriterionStatus) else ObjectiveCriterionStatus(str(status))

    @staticmethod
    def _build_objective_success_criteria(
        success_criteria: Optional[List[Any]],
    ) -> List[ObjectiveSuccessCriterion]:
        criteria: List[ObjectiveSuccessCriterion] = []
        for raw_criterion in success_criteria or []:
            if isinstance(raw_criterion, ObjectiveSuccessCriterion):
                criteria.append(raw_criterion)
            elif isinstance(raw_criterion, dict):
                criteria.append(ObjectiveSuccessCriterion.model_validate(raw_criterion))
            elif str(raw_criterion or "").strip():
                criteria.append(ObjectiveSuccessCriterion(description=str(raw_criterion).strip()))
        return criteria

    @staticmethod
    def _coerce_objective_creation_mode(creation_mode: str) -> str:
        normalized = str(creation_mode or "promote_task").strip().lower()
        aliases = {
            "promote": "promote_task",
            "promote_current_task": "promote_task",
            "promote_task": "promote_task",
            "spawn": "spawn_task",
            "spawn_new_task": "spawn_task",
            "spawn_task": "spawn_task",
            "create_workstream": "spawn_task",
        }
        if normalized not in aliases:
            raise ValueError("Objective creation_mode must be promote_task or spawn_task.")
        return aliases[normalized]

    @staticmethod
    def _initial_objective_plan_summary(
        *,
        title: str,
        desired_outcome: str,
        why: str,
        creation_mode: str,
    ) -> str:
        mode_summary = (
            "The source task has been promoted into durable Objective work."
            if creation_mode == "promote_task"
            else "This is a separate Objective workstream spawned from another task."
        )
        return "\n".join(
            [
                f"Objective: {title.strip()}",
                f"Desired outcome: {desired_outcome.strip()}",
                f"Rationale: {why.strip()}",
                f"Creation mode: {creation_mode}. {mode_summary}",
                "Next step: refine this plan and activate runtime plan-step execution when staged execution is needed.",
            ]
        )

    def _ensure_initial_objective_plan(
        self,
        *,
        task_id: str,
        title: str,
        desired_outcome: str,
        why: str,
        creation_mode: str,
        current_focus: Optional[str],
    ) -> Optional[str]:
        existing_plan_id = self.operations.get_task_plan_id(task_id)
        if existing_plan_id:
            self.operations.sync_plan_from_task(task_id, create=True)
            return existing_plan_id

        plan_summary = self._initial_objective_plan_summary(
            title=title,
            desired_outcome=desired_outcome,
            why=why,
            creation_mode=creation_mode,
        )
        digest = self.operations.set_task_plan(
            task_id,
            plan_summary,
            goal_summary=desired_outcome.strip(),
            current_focus=(current_focus or "Initial Objective planning").strip(),
            rationale=why.strip(),
            source=TaskHistorySource.SYSTEM,
            log_history=True,
        )
        if digest is None:
            return None
        return self.operations.get_task_plan_id(task_id)

    def _build_spawned_objective_task(
        self,
        *,
        source_task_info: TaskInfo,
        objective_id: str,
        title: str,
        desired_outcome: str,
        why: str,
        root_conversation_id: Optional[str],
    ) -> Optional[TaskInfo]:
        task_id = new_id("objective_task")
        problem_statement = "\n".join(
            [
                f"Objective workstream: {title.strip()}",
                f"Desired outcome: {desired_outcome.strip()}",
                f"Why this exists: {why.strip()}",
                "",
                "This task is the root task for a durable Objective created from another task.",
                "Refine the plan, execute near-term work, and use runtime plan-step execution when staged work is needed.",
            ]
        )
        task = Task(
            task_id=task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement=problem_statement,
        )
        header: MessageHeader = source_task_info.header.model_copy(deep=True)
        header.from_agent_id = self.identity.owner_agent_id or header.from_agent_id
        header.to_routing_path = self.identity.owner_agent_id or header.to_routing_path
        header.response_routing_key = self.identity.owner_agent_id or header.response_routing_key

        meta: MessageMeta = source_task_info.meta.model_copy(deep=True)
        meta.msg_correlation_id = f"objective_corr_{task_id}"
        meta.conversation_id = root_conversation_id or new_id("obj_conv")
        meta.followup_to_task_id = None
        meta.is_response_requested = False
        meta.to_agent_name = self.identity.owner_agent_name
        meta.to_named_agent_type = self.identity.owner_named_agent_type
        # The Objective row does not exist until its initial Plan is ready. Link
        # the accepted Task immediately afterward to avoid treating expected
        # creation-ordering as an unknown-Objective anomaly.
        meta.objective_id = None
        meta.requester_type = meta.requester_type or RequesterType.AGENT_REQUESTER

        # The Objective's root task works for its source task's person.
        if not self.operations.add_task(
            task, header, meta, owner_person_id=source_task_info.owner_person_id
        ):
            return None
        objective_task_info = self.operations.get_task_info(task_id)
        if objective_task_info is None:
            return None
        objective_task_info.meta.objective_id = objective_id
        self.operations.append_history_entry(
            task_id,
            f"Created Objective root task for `{objective_id}`: {title.strip()}",
            source=TaskHistorySource.SYSTEM,
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            related_task_id=source_task_info.task.task_id,
            tracks_to_plan=False,
            details={
                "action": "create_objective_root_task",
                "objective_id": objective_id,
                "created_by_task_id": source_task_info.task.task_id,
                "creation_mode": "spawn_task",
            },
        )
        return objective_task_info

    def get_objective(self, objective_id: str) -> Optional[ObjectiveRecord]:
        return self._get_objective_record(objective_id)

    def get_user_owned_objective(
        self,
        objective_id: str,
        *,
        owner_canonical_user_id: str,
        team_id: str,
        adopt_live_custody: bool = True,
    ) -> Optional[ObjectiveRecord]:
        """Resolve human ownership independently of the current manager process."""

        objective = self.objective_repository.get_objective_for_user(
            objective_id,
            owner_canonical_user_id=owner_canonical_user_id,
            team_id=team_id,
        )
        if objective is None or not adopt_live_custody:
            return objective
        durable_owner = self.operations.durable_manager_scope_id()
        if objective.owner_agent_id == durable_owner:
            return objective
        if not durable_owner or not self.identity.owner_named_agent_type:
            raise RuntimeError(
                "Objective recovery requires initialized TeamArchitect custody."
            )
        self.durable_owner_scope_repository.adopt_user_owned_objective(
            objective_id=objective.objective_id,
            owner_canonical_user_id=owner_canonical_user_id,
            team_id=team_id,
            named_agent_type=self.identity.owner_named_agent_type,
            durable_owner_agent_id=durable_owner,
            include_objective_work=self.objective_work_repository is not None,
        )
        return self.objective_repository.get_objective(
            objective.objective_id,
            owner_agent_id=durable_owner,
        )

    def list_user_owned_objectives(
        self,
        *,
        owner_canonical_user_id: str,
        team_id: str,
        limit: int = 100,
        include_closed: bool = False,
    ) -> List[ObjectiveRecord]:
        return self.objective_repository.list_objectives_for_user(
            owner_canonical_user_id=owner_canonical_user_id,
            team_id=team_id,
            limit=limit,
            include_closed=include_closed,
        )

    def get_task_objective_id(self, task_id: str) -> Optional[str]:
        return self._get_task_objective_id_from_context(task_id)

    def link_incoming_task_to_objective(
        self,
        task_id: str,
        objective_id: str,
    ) -> Optional[ObjectiveRecord]:
        """Link a newly accepted manager Task to an Objective and its active Plan."""
        objective = self._get_objective_record(objective_id)
        if objective is None:
            self._logger.warning(
                "Ignoring unknown Objective linkage on incoming task. task_id=%s objective_id=%s",
                task_id,
                objective_id,
            )
            return None

        self.objective_state.task_to_objective_id[task_id] = objective.objective_id
        if task_id not in objective.related_task_ids:
            objective.related_task_ids.append(task_id)
            self._append_objective_event(
                objective,
                ObjectiveEventType.TASK_LINKED,
                f"Linked incoming task {task_id} to objective.",
                task_id=task_id,
                user_visible=False,
            )
            self._save_objective_logged(objective, step="incoming_task_linked")

        current_plan = self.operations.get_plan(objective.current_plan_id) if objective.current_plan_id else None
        if current_plan is None or current_plan.status != PlanStatus.ACTIVE:
            return objective
        if self.plan_coordinator.link_task_to_plan(
            task_id,
            current_plan.plan_id,
            hydrate=True,
        ) is None:
            self._logger.warning(
                "Failed to link Objective task to its current Plan. task_id=%s objective_id=%s plan_id=%s",
                task_id,
                objective.objective_id,
                current_plan.plan_id,
            )
        return objective

    def get_plan_objective_id(self, plan_id: str) -> Optional[str]:
        objective_id = self.objective_state.plan_to_objective_id.get(plan_id)
        if objective_id:
            return objective_id
        objective = self._get_objective_for_plan_id(plan_id)
        return objective.objective_id if objective else None

    def list_objectives(
        self,
        *,
        view: ObjectiveView,
        limit: int = 20,
        statuses: Optional[List[str]] = None,
        horizons: Optional[List[str]] = None,
        include_closed: bool = False,
    ):
        """The Objectives one task may see (``view``), newest first."""
        if not self.operations.objectives_enabled() or not self.identity.owner_agent_id:
            return []
        try:
            return self.objective_repository.list_objectives(
                limit=limit,
                owner_agent_id=self.operations.durable_manager_scope_id(),
                statuses=statuses,
                horizons=horizons,
                include_closed=include_closed,
                view=view,
            )
        except Exception as exc:
            self._logger.error("Failed to list objectives: %s", exc)
            return []

    def link_task_to_objective(
        self,
        objective_id: str,
        task_id: str,
        *,
        user_visible: bool = False,
        reason: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        objective = self._get_objective_record(objective_id)
        if objective is None:
            self._logger.warning("Cannot link task %s to unknown objective %s.", task_id, objective_id)
            return None
        if task_id not in self.task_state.tasks and self.operations.get_task_from_db(task_id) is None:
            self._logger.warning("Cannot link unknown task %s to objective %s.", task_id, objective_id)
            return None

        changed = False
        self.objective_state.task_to_objective_id[task_id] = objective.objective_id
        if task_id not in objective.related_task_ids:
            objective.related_task_ids.append(task_id)
            changed = True
            self._append_objective_event(
                objective,
                ObjectiveEventType.TASK_LINKED,
                f"Linked task {task_id} to objective.",
                task_id=task_id,
                user_visible=user_visible,
                details={"reason": reason} if reason else {},
            )
        plan_id = self.operations.get_task_plan_id(task_id)
        if plan_id:
            linked = self.link_plan_to_objective(objective.objective_id, plan_id, user_visible=False, reason=reason)
            if linked is not None:
                objective = linked
        elif changed:
            self._save_objective(objective)
        self.operations.persist_runtime_state()
        return objective

    def link_plan_to_objective(
        self,
        objective_id: str,
        plan_id: str,
        *,
        user_visible: bool = False,
        reason: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        objective = self._get_objective_record(objective_id)
        if objective is None:
            self._logger.warning("Cannot link plan %s to unknown objective %s.", plan_id, objective_id)
            return None
        plan = self.operations.get_plan(plan_id)
        if plan is None:
            self._logger.warning("Cannot link unknown plan %s to objective %s.", plan_id, objective_id)
            return None

        changed = False
        self.objective_state.plan_to_objective_id[plan_id] = objective.objective_id
        if plan_id not in objective.plan_ids:
            objective.plan_ids.append(plan_id)
            changed = True
            self._append_objective_event(
                objective,
                ObjectiveEventType.PLAN_LINKED,
                f"Linked plan {plan_id} to objective.",
                task_id=plan.root_task_id,
                plan_id=plan_id,
                user_visible=user_visible,
                details={"reason": reason} if reason else {},
            )
        if objective.current_plan_id != plan_id:
            objective.current_plan_id = plan_id
            changed = True
        for related_task_id in plan.related_task_ids:
            self.objective_state.task_to_objective_id[related_task_id] = objective.objective_id
            if related_task_id not in objective.related_task_ids:
                objective.related_task_ids.append(related_task_id)
                changed = True

        plan.objective_id = objective.objective_id
        plan.objective_title = objective.title
        self.operations.save_plan(plan)
        if changed:
            self._save_objective(objective)
        self.operations.persist_runtime_state()
        return objective

    def link_schedule_to_objective(
        self,
        objective_id: str,
        schedule_id: str,
        *,
        managed_by_objective: bool = True,
        user_visible: bool = False,
        reason: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        objective = self._get_objective_record(objective_id)
        if objective is None:
            self._logger.warning("Cannot link schedule %s to unknown objective %s.", schedule_id, objective_id)
            return None
        existing = next((link for link in objective.schedule_links if link.schedule_id == schedule_id), None)
        if existing is None:
            objective.schedule_links.append(
                ObjectiveScheduleLink(
                    schedule_id=schedule_id,
                    managed_by_objective=managed_by_objective,
                )
            )
            self._append_objective_event(
                objective,
                ObjectiveEventType.SCHEDULE_LINKED,
                f"Linked schedule {schedule_id} to objective.",
                schedule_id=schedule_id,
                user_visible=user_visible,
                details={"reason": reason} if reason else {},
            )
        else:
            existing.managed_by_objective = managed_by_objective
        self._save_objective(objective)
        return objective

    def link_heartbeat_to_objective(
        self,
        objective_id: str,
        heartbeat_item_id: str,
        *,
        managed_by_objective: bool = True,
        user_visible: bool = False,
        reason: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        objective = self._get_objective_record(objective_id)
        if objective is None:
            self._logger.warning(
                "Cannot link heartbeat item %s to unknown objective %s.",
                heartbeat_item_id,
                objective_id,
            )
            return None
        existing = next(
            (link for link in objective.heartbeat_links if link.heartbeat_item_id == heartbeat_item_id),
            None,
        )
        if existing is None:
            objective.heartbeat_links.append(
                ObjectiveHeartbeatLink(
                    heartbeat_item_id=heartbeat_item_id,
                    managed_by_objective=managed_by_objective,
                )
            )
            self._append_objective_event(
                objective,
                ObjectiveEventType.HEARTBEAT_LINKED,
                f"Linked heartbeat item {heartbeat_item_id} to objective.",
                heartbeat_item_id=heartbeat_item_id,
                user_visible=user_visible,
                details={"reason": reason} if reason else {},
            )
        else:
            existing.managed_by_objective = managed_by_objective
        self._save_objective(objective)
        return objective

    def create_objective(
        self,
        *,
        title: str,
        desired_outcome: str,
        why: str,
        task_id: Optional[str] = None,
        creation_mode: str = "promote_task",
        horizon: ObjectiveHorizon | str = ObjectiveHorizon.FINITE,
        priority: ObjectivePriority | str = ObjectivePriority.NORMAL,
        completion_policy: ObjectiveCompletionPolicy | str = ObjectiveCompletionPolicy.MANAGER_DECIDES,
        success_criteria: Optional[List[Any]] = None,
        constraints: Optional[List[str]] = None,
        non_goals: Optional[List[str]] = None,
        requires_user_confirmation: bool = False,
        current_focus: Optional[str] = None,
        progress_summary: Optional[str] = None,
        root_conversation_id: Optional[str] = None,
        user_visible: bool = True,
    ) -> Optional[ObjectiveRecord]:
        if not self.operations.objectives_enabled():
            self._logger.info("Ignoring objective creation because objectives are manager-only.")
            return None
        if not title or not title.strip():
            raise ValueError("Objective title is required.")
        if not desired_outcome or not desired_outcome.strip():
            raise ValueError("Objective desired_outcome is required.")
        if not why or not why.strip():
            raise ValueError("Objective why is required.")
        mode = self._coerce_objective_creation_mode(creation_mode)
        if not task_id:
            raise ValueError("Objective creation requires a source task_id.")

        task_info: Optional[TaskInfo] = self.operations.get_task_info(task_id) if task_id else None
        if task_info is None:
            self._logger.warning("Cannot create objective from unknown or archived task %s.", task_id)
            return None

        requested_root_conversation_id = root_conversation_id
        objective_id = self.operations.new_objective_id()
        objective_task_id = task_id
        objective_task_info = task_info
        if root_conversation_id is None:
            root_conversation_id = getattr(task_info.meta, "conversation_id", None)
        if mode == "spawn_task":
            objective_task_info = self._build_spawned_objective_task(
                source_task_info=task_info,
                objective_id=objective_id,
                title=title,
                desired_outcome=desired_outcome,
                why=why,
                root_conversation_id=requested_root_conversation_id,
            )
            if objective_task_info is None:
                return None
            objective_task_id = objective_task_info.task.task_id
            root_conversation_id = getattr(objective_task_info.meta, "conversation_id", root_conversation_id)

        plan_id = self._ensure_initial_objective_plan(
            task_id=objective_task_id,
            title=title,
            desired_outcome=desired_outcome,
            why=why,
            creation_mode=mode,
            current_focus=current_focus,
        )
        if not plan_id:
            self._logger.warning(
                "Cannot create objective from task %s because an initial plan could not be created.",
                objective_task_id,
            )
            return None
        now_ts = datetime.now(timezone.utc).isoformat()
        policy = self._coerce_objective_completion_policy(completion_policy)
        if policy == ObjectiveCompletionPolicy.USER_CONFIRMS:
            requires_user_confirmation = True

        objective = ObjectiveRecord(
            objective_id=objective_id,
            title=title.strip(),
            desired_outcome=desired_outcome.strip(),
            why=why.strip(),
            status=ObjectiveStatus.ACTIVE,
            horizon=self._coerce_objective_horizon(horizon),
            priority=self._coerce_objective_priority(priority),
            completion_policy=policy,
            success_criteria=self._build_objective_success_criteria(success_criteria),
            constraints=[str(item).strip() for item in (constraints or []) if str(item).strip()],
            non_goals=[str(item).strip() for item in (non_goals or []) if str(item).strip()],
            progress_summary=progress_summary.strip() if progress_summary else None,
            current_focus=current_focus.strip() if current_focus else None,
            owner_agent_id=self.operations.durable_manager_scope_id(),
            owner_agent_name=self.identity.owner_agent_name,
            # The source task's person, recorded at admission (TaskInfo).
            owner_canonical_user_id=task_info.owner_person_id,
            team_id=self.identity.team_id,
            owner_named_agent_type=self.identity.owner_named_agent_type,
            root_conversation_id=root_conversation_id,
            created_by_task_id=task_id,
            promoted_from_task_id=task_id if mode == "promote_task" else None,
            current_plan_id=plan_id,
            plan_ids=[plan_id],
            related_task_ids=[objective_task_id],
            requires_user_confirmation=requires_user_confirmation,
            created_at=now_ts,
            updated_at=now_ts,
        )
        self._append_objective_event(
            objective,
            ObjectiveEventType.CREATED,
            f"Created objective: {objective.title}",
            task_id=task_id,
            plan_id=plan_id,
            user_visible=user_visible,
            details={"creation_mode": mode, "created_by_task_id": task_id, "objective_task_id": objective_task_id},
        )
        if mode == "promote_task":
            self._append_objective_event(
                objective,
                ObjectiveEventType.PROMOTED_FROM_TASK,
                f"Promoted task {task_id} to objective.",
                task_id=task_id,
                plan_id=plan_id,
                user_visible=user_visible,
            )
        else:
            self._append_objective_event(
                objective,
                ObjectiveEventType.TASK_LINKED,
                f"Created Objective root task {objective_task_id} from source task {task_id}.",
                task_id=objective_task_id,
                plan_id=plan_id,
                user_visible=user_visible,
                details={"created_by_task_id": task_id, "creation_mode": mode},
            )
        self._append_objective_event(
            objective,
            ObjectiveEventType.PLAN_LINKED,
            f"Linked initial plan {plan_id} to objective.",
            task_id=objective_task_id,
            plan_id=plan_id,
            user_visible=False,
            details={"creation_mode": mode},
        )
        self.objective_state.task_to_objective_id[objective_task_id] = objective.objective_id
        self.objective_state.plan_to_objective_id[plan_id] = objective.objective_id

        self._save_objective(objective)
        plan: Optional[PlanRecord] = self.operations.get_plan(plan_id)
        if plan is not None:
            plan.objective_id = objective.objective_id
            plan.objective_title = objective.title
            if objective_task_id not in plan.related_task_ids:
                plan.related_task_ids.append(objective_task_id)
            self.operations.save_plan(plan)
        history_summary = (
            f"Promoted task to objective `{objective.objective_id}`: {objective.title}"
            if mode == "promote_task"
            else f"Spawned objective `{objective.objective_id}` with root task `{objective_task_id}`: {objective.title}"
        )
        self.operations.append_history_entry(
            task_id,
            history_summary,
            source=TaskHistorySource.SYSTEM,
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            related_task_id=objective_task_id,
            tracks_to_plan=False,
            details={
                "action": "create_objective",
                "creation_mode": mode,
                "objective_id": objective.objective_id,
                "objective_task_id": objective_task_id,
                "plan_id": plan_id,
                "title": objective.title,
                "horizon": objective.horizon.value,
                "priority": objective.priority.value,
            },
        )
        self.operations.emit_activity(
            activity_type=TaskActivityType.OBJECTIVE_UPDATED,
            stage=TaskActivityStage.PLANNING,
            task_id=objective_task_id,
            summary=f"Objective created: {objective.title}",
            detail=objective.desired_outcome,
            user_visible=user_visible,
            details=objective.model_dump(mode="json"),
        )
        self.operations.persist_runtime_state()
        return objective

    def _build_objective_continuation_task(
        self,
        objective: ObjectiveRecord,
        source_task_info: TaskInfo,
        *,
        work_brief: str,
        root_conversation_id: Optional[str],
    ) -> Optional[TaskInfo]:
        task_id = new_id("objective_task")
        task = Task(
            task_id=task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="\n".join(
                [
                    f"Continue Objective: {objective.title}",
                    f"Desired outcome: {objective.desired_outcome}",
                    "",
                    "Current work brief:",
                    work_brief,
                    "",
                    "This is a replaceable execution Task. Advance the Objective's durable Plan and preserve remaining work for later Tasks.",
                ]
            ),
        )
        header = source_task_info.header.model_copy(deep=True)
        header.from_agent_id = self.identity.owner_agent_id or header.from_agent_id
        header.to_routing_path = self.identity.owner_agent_id or header.to_routing_path
        header.response_routing_key = self.identity.owner_agent_id or header.response_routing_key
        meta = source_task_info.meta.model_copy(deep=True)
        meta.msg_correlation_id = f"objective_corr_{task_id}"
        meta.conversation_id = root_conversation_id or objective.root_conversation_id or new_id("obj_conv")
        meta.followup_to_task_id = None
        meta.is_response_requested = False
        meta.to_agent_name = self.identity.owner_agent_name
        meta.to_named_agent_type = self.identity.owner_named_agent_type
        meta.objective_id = objective.objective_id
        meta.requester_type = RequesterType.AGENT_REQUESTER
        if not self.operations.add_task(
            task, header, meta, owner_person_id=source_task_info.owner_person_id
        ):
            return None
        return self.operations.get_task_info(task_id)

    def _ensure_objective_continuation_plan(
        self,
        objective: ObjectiveRecord,
        task_id: str,
        *,
        work_brief: str,
        current_focus: Optional[str],
    ) -> bool:
        current_plan = self.operations.get_plan(objective.current_plan_id) if objective.current_plan_id else None
        if current_plan is not None and current_plan.status not in self._plan_terminal_statuses():
            return True
        plan_summary = "\n".join(
            [
                f"Objective continuation plan: {objective.title}",
                f"Desired outcome: {objective.desired_outcome}",
                f"Current work: {work_brief}",
            ]
        )
        return self.operations.set_task_plan(
            task_id,
            plan_summary,
            goal_summary=objective.desired_outcome,
            current_focus=current_focus or objective.current_focus or "Objective continuation",
            rationale="The prior Plan attempt is terminal; this Task begins a new Objective Plan attempt.",
            source=TaskHistorySource.SYSTEM,
        ) is not None

    def _record_objective_continuation_start(
        self,
        objective: ObjectiveRecord,
        *,
        source_task_id: str,
        continuation_task_id: str,
        work_brief: str,
        current_focus: Optional[str],
        user_visible: bool,
    ) -> None:
        previous_status = objective.status
        objective.status = ObjectiveStatus.ACTIVE
        if current_focus is not None:
            objective.current_focus = current_focus.strip() or objective.current_focus
        objective.progress_summary = f"Started continuation Task {continuation_task_id}."
        self._append_objective_event(
            objective,
            ObjectiveEventType.PROGRESS_UPDATED,
            "Started a new Task to continue Objective work.",
            task_id=continuation_task_id,
            plan_id=objective.current_plan_id,
            user_visible=user_visible,
            details={
                "source_task_id": source_task_id,
                "previous_status": previous_status.value,
                "work_brief": work_brief,
            },
        )
        self._save_objective(objective)
        self.operations.append_history_entry(
            source_task_id,
            f"Started continuation Task for Objective `{objective.objective_id}`.",
            source=TaskHistorySource.SYSTEM,
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            related_task_id=continuation_task_id,
            tracks_to_plan=False,
            details={
                "action": "start_objective_task",
                "objective_id": objective.objective_id,
                "plan_id": objective.current_plan_id,
            },
        )
        self.operations.persist_runtime_state()

    def start_objective_task(
        self,
        objective_id: str,
        *,
        source_task_id: str,
        work_brief: str,
        current_focus: Optional[str] = None,
        root_conversation_id: Optional[str] = None,
        user_visible: bool = True,
    ) -> Optional[TaskInfo]:
        """Queue a fresh replaceable Task that advances an Objective's current Plan."""
        if not work_brief or not work_brief.strip():
            raise ValueError("Objective continuation work_brief is required.")
        objective = self._get_objective_record(objective_id)
        source_task_info = self.operations.get_task_info(source_task_id)
        if objective is None or source_task_info is None:
            return None
        if objective.status in self._objective_terminal_statuses():
            raise ValueError("Cannot start work for a terminal Objective.")
        if objective.status == ObjectiveStatus.PAUSED:
            raise ValueError("Resume the Objective before starting a continuation Task.")

        normalized_work_brief = work_brief.strip()
        task_info = self._build_objective_continuation_task(
            objective,
            source_task_info,
            work_brief=normalized_work_brief,
            root_conversation_id=root_conversation_id,
        )
        if task_info is None:
            return None
        task_id = task_info.task.task_id
        if not self._ensure_objective_continuation_plan(
            objective,
            task_id,
            work_brief=normalized_work_brief,
            current_focus=current_focus,
        ):
            self.operations.fail_task(
                task_id,
                "Failed to create a durable Plan for Objective continuation.",
                datetime.now(timezone.utc).isoformat(),
            )
            return None
        refreshed = self._get_objective_record(objective.objective_id) or objective
        try:
            self._record_objective_continuation_start(
                refreshed,
                source_task_id=source_task_id,
                continuation_task_id=task_id,
                work_brief=normalized_work_brief,
                current_focus=current_focus,
                user_visible=user_visible,
            )
        except ObjectiveSaveFailed:
            # An Objective that does not show its continuation would not
            # review or pause it, so the new task stops before it runs.
            self.operations.fail_task(
                task_id,
                "The Objective could not record this continuation Task.",
                datetime.now(timezone.utc).isoformat(),
            )
            raise
        return task_info

    def update_objective_progress(
        self,
        objective_id: str,
        *,
        progress_summary: Optional[str] = None,
        current_focus: Optional[str] = None,
        criterion_updates: Optional[List[Dict[str, Any]]] = None,
        task_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        user_visible: bool = True,
    ) -> Optional[ObjectiveRecord]:
        objective = self._get_objective_record(objective_id)
        if objective is None:
            return None
        if objective.status in self._objective_terminal_statuses():
            self._logger.warning("Ignoring progress update for terminal objective %s.", objective_id)
            return objective

        changed = False
        if progress_summary is not None and progress_summary != objective.progress_summary:
            objective.progress_summary = progress_summary.strip() or None
            changed = True
        if current_focus is not None and current_focus != objective.current_focus:
            objective.current_focus = current_focus.strip() or None
            changed = True

        criteria_by_id = {criterion.criterion_id: criterion for criterion in objective.success_criteria}
        for raw_update in criterion_updates or []:
            if not isinstance(raw_update, dict):
                continue
            criterion = criteria_by_id.get(str(raw_update.get("criterion_id") or ""))
            if criterion is None:
                description = str(raw_update.get("description") or "").strip()
                if not description:
                    continue
                criterion = ObjectiveSuccessCriterion(description=description)
                objective.success_criteria.append(criterion)
                criteria_by_id[criterion.criterion_id] = criterion
                changed = True
            if raw_update.get("status") is not None:
                criterion.status = self._coerce_objective_criterion_status(raw_update["status"])
                changed = True
            if "evidence_summary" in raw_update:
                evidence_summary = raw_update.get("evidence_summary")
                criterion.evidence_summary = str(evidence_summary).strip() if evidence_summary else None
                changed = True
            if changed:
                criterion.updated_at = datetime.now(timezone.utc).isoformat()

        if not changed:
            return objective
        self._append_objective_event(
            objective,
            ObjectiveEventType.PROGRESS_UPDATED,
            "Updated objective progress.",
            task_id=task_id,
            plan_id=plan_id or objective.current_plan_id,
            user_visible=user_visible,
            details={
                "progress_summary": objective.progress_summary,
                "current_focus": objective.current_focus,
                "criterion_updates": criterion_updates or [],
            },
        )
        self._save_objective(objective)
        self.operations.emit_activity(
            activity_type=TaskActivityType.OBJECTIVE_UPDATED,
            stage=TaskActivityStage.RUNNING,
            task_id=task_id or (objective.related_task_ids[-1] if objective.related_task_ids else None),
            summary=f"Objective progress updated: {objective.title}",
            detail=objective.progress_summary,
            user_visible=user_visible,
            details=objective.model_dump(mode="json"),
        )
        return objective

    def _transition_current_plan_for_objective_status(
        self,
        objective: ObjectiveRecord,
        target_status: ObjectiveStatus,
        *,
        summary: Optional[str],
        task_id: Optional[str],
        timestamp: str,
    ) -> None:
        terminal_plan_status = {
            ObjectiveStatus.SATISFIED: PlanStatus.COMPLETED,
            ObjectiveStatus.FAILED: PlanStatus.FAILED,
            ObjectiveStatus.CANCELLED: PlanStatus.CANCELLED,
            ObjectiveStatus.ABANDONED: PlanStatus.ABANDONED,
            ObjectiveStatus.DELETED: PlanStatus.ABANDONED,
        }.get(target_status)
        if terminal_plan_status is None or not objective.current_plan_id:
            return
        current_plan = self.operations.get_plan(objective.current_plan_id)
        if current_plan is None or current_plan.status in self._plan_terminal_statuses():
            return
        self.plan_coordinator.update_plan_status(
            current_plan.plan_id,
            terminal_plan_status,
            summary=summary or f"Objective transitioned to {target_status.value}.",
            task_id=task_id,
            timestamp=timestamp,
        )

    def update_objective_status(
        self,
        objective_id: str,
        status: ObjectiveStatus | str,
        *,
        summary: Optional[str] = None,
        user_confirmation_summary: Optional[str] = None,
        satisfied_by_task_id: Optional[str] = None,
        task_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        user_visible: bool = True,
    ) -> Optional[ObjectiveRecord]:
        objective = self._get_objective_record(objective_id)
        if objective is None:
            return None
        target_status = self._coerce_objective_status(status)
        if target_status == ObjectiveStatus.SATISFIED:
            if objective.requires_user_confirmation and not (
                user_confirmation_summary or objective.user_confirmation_summary
            ):
                raise ValueError("Objective requires user confirmation before it can be marked satisfied.")
            if (
                objective.completion_policy == ObjectiveCompletionPolicy.SUCCESS_CRITERIA_MET
                and any(criterion.status != ObjectiveCriterionStatus.MET for criterion in objective.success_criteria)
            ):
                raise ValueError("Objective success criteria must all be met before it can be marked satisfied.")

        previous_status = objective.status
        now_ts = datetime.now(timezone.utc).isoformat()
        objective.status = target_status
        if summary is not None:
            objective.progress_summary = summary.strip() or objective.progress_summary
        if user_confirmation_summary:
            objective.user_confirmation_summary = user_confirmation_summary.strip()
            self._append_objective_event(
                objective,
                ObjectiveEventType.USER_CONFIRMATION_RECORDED,
                "Recorded user confirmation for objective.",
                task_id=task_id or satisfied_by_task_id,
                plan_id=plan_id or objective.current_plan_id,
                user_visible=True,
                details={"user_confirmation_summary": objective.user_confirmation_summary},
                timestamp=now_ts,
            )
        if target_status == ObjectiveStatus.SATISFIED:
            objective.satisfied_by_task_id = satisfied_by_task_id or task_id or objective.satisfied_by_task_id
            objective.satisfied_at = objective.satisfied_at or now_ts
        if target_status == ObjectiveStatus.DELETED:
            # Commit the completed tombstone in one write. Once DELETED is
            # stored, repeated deletion commands return without changing it.
            objective.deleted_at = objective.deleted_at or now_ts
            objective.deletion_summary = (summary or "").strip() or "Objective deletion completed."

        self._append_objective_event(
            objective,
            ObjectiveEventType.STATUS_CHANGED,
            f"Objective status changed from {previous_status.value} to {target_status.value}.",
            task_id=task_id or satisfied_by_task_id,
            plan_id=plan_id or objective.current_plan_id,
            user_visible=user_visible,
            details={
                "previous_status": previous_status.value,
                "new_status": target_status.value,
                "summary": summary,
            },
            timestamp=now_ts,
        )
        # Saved before its Plan closes, so a failed save leaves both unchanged.
        self._save_objective(objective)
        self._transition_current_plan_for_objective_status(
            objective,
            target_status,
            summary=summary,
            task_id=task_id or satisfied_by_task_id,
            timestamp=now_ts,
        )
        self.operations.emit_activity(
            activity_type=TaskActivityType.OBJECTIVE_UPDATED,
            stage=TaskActivityStage.COMPLETED if target_status == ObjectiveStatus.SATISFIED else TaskActivityStage.RUNNING,
            task_id=task_id or satisfied_by_task_id or (objective.related_task_ids[-1] if objective.related_task_ids else None),
            summary=f"Objective status changed: {objective.title}",
            detail=f"{previous_status.value} -> {target_status.value}",
            user_visible=user_visible,
            details=objective.model_dump(mode="json"),
        )
        self.operations.persist_runtime_state()
        # Closing the Plan may have saved its own links on the Objective.
        return self._get_objective_record(objective.objective_id) or objective

    def _review_objective_with_active_plan(
        self,
        objective: ObjectiveRecord,
        *,
        task_id: str,
        terminal_state: str,
        active_plan_ids: List[str],
        timestamp: str,
        task_summary: Optional[str],
    ) -> ObjectiveRecord:
        previous_status = objective.status
        if terminal_state == ProgressState.COMPLETED.value:
            objective.status = ObjectiveStatus.ACTIVE
            conclusion = "plan_work_remains"
            decision_summary = (
                "The execution Task completed, but the Objective-owned Plan still has work remaining."
            )
        else:
            objective.status = ObjectiveStatus.BLOCKED
            conclusion = "task_failed_plan_remains_active"
            decision_summary = (
                f"The execution Task ended with state {terminal_state}; the Objective and its Plan remain "
                "available for a replacement Task."
            )
        objective.progress_summary = task_summary.strip() if task_summary else decision_summary
        if previous_status != objective.status:
            self._append_objective_event(
                objective,
                ObjectiveEventType.STATUS_CHANGED,
                f"Objective status changed from {previous_status.value} to {objective.status.value}.",
                task_id=task_id,
                plan_id=objective.current_plan_id,
                user_visible=True,
                details={
                    "previous_status": previous_status.value,
                    "new_status": objective.status.value,
                    "summary": objective.progress_summary,
                    "reason": conclusion,
                },
                timestamp=timestamp,
            )
        self._append_objective_review_completed(
            objective,
            task_id=task_id,
            timestamp=timestamp,
            conclusion=conclusion,
            summary=decision_summary,
            terminal_state=terminal_state,
            active_task_ids=[],
            active_plan_ids=active_plan_ids,
        )
        self._save_objective_logged(objective, step=f"task_end_review:{conclusion}")
        self.operations.persist_runtime_state()
        return objective

    def review_objective_after_task_terminal(
        self,
        task_id: str,
        terminal_state: ProgressState | str,
        *,
        timestamp: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        objective = self._get_task_objective_record(task_id)
        if objective is None:
            return None
        objective_owned_task_ids = set(objective.related_task_ids or []) | set(objective.recovery_task_ids or [])
        if task_id not in objective_owned_task_ids:
            return objective
        if objective.status in self._objective_terminal_statuses():
            return objective

        terminal_state_value = (
            terminal_state.value if isinstance(terminal_state, ProgressState) else str(terminal_state)
        )
        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        self._append_objective_event(
            objective,
            ObjectiveEventType.REVIEW_REQUESTED,
            "Reviewing objective after linked task reached a terminal state.",
            task_id=task_id,
            plan_id=objective.current_plan_id,
            user_visible=False,
            details={
                "terminal_task_id": task_id,
                "terminal_state": terminal_state_value,
                "summary": summary,
            },
            timestamp=now_ts,
        )

        active_task_ids = self._active_task_ids_for_objective(objective)
        active_plan_ids = self._active_plan_ids_for_objective(objective)
        if active_task_ids:
            objective.progress_summary = (
                summary.strip()
                if summary
                else "A linked task reached a terminal state; another Objective-linked Task is still active."
            )
            self._append_objective_review_completed(
                objective,
                task_id=task_id,
                timestamp=now_ts,
                conclusion="active_work_remains",
                summary="Objective remains active because another linked Task is still running.",
                terminal_state=terminal_state_value,
                active_task_ids=active_task_ids,
                active_plan_ids=active_plan_ids,
            )
            self._save_objective_logged(objective, step="task_end_review:active_work_remains")
            self.operations.persist_runtime_state()
            return objective

        if active_plan_ids:
            return self._review_objective_with_active_plan(
                objective,
                task_id=task_id,
                terminal_state=terminal_state_value,
                active_plan_ids=active_plan_ids,
                timestamp=now_ts,
                task_summary=summary,
            )

        if objective.status == ObjectiveStatus.PAUSED:
            self._append_objective_review_completed(
                objective,
                task_id=task_id,
                timestamp=now_ts,
                conclusion="objective_paused",
                summary="Objective review deferred because the Objective is paused.",
                terminal_state=terminal_state_value,
                active_task_ids=[],
                active_plan_ids=[],
            )
            self._save_objective_logged(objective, step="task_end_review:objective_paused")
            self.operations.persist_runtime_state()
            return objective

        continuing_links = self._objective_has_continuing_links(objective)
        if (
            objective.completion_policy == ObjectiveCompletionPolicy.RECURRING_INDEFINITE
            or objective.horizon in {ObjectiveHorizon.RECURRING, ObjectiveHorizon.STANDING}
            or continuing_links
        ):
            previous_status = objective.status
            if objective.status == ObjectiveStatus.PROPOSED:
                objective.status = ObjectiveStatus.ACTIVE
                self._append_objective_event(
                    objective,
                    ObjectiveEventType.STATUS_CHANGED,
                    f"Objective status changed from {previous_status.value} to active.",
                    task_id=task_id,
                    plan_id=objective.current_plan_id,
                    user_visible=True,
                    details={
                        "previous_status": previous_status.value,
                        "new_status": objective.status.value,
                        "summary": "Recurring or standing Objective has terminal task activity.",
                        "reason": "standing_or_recurring_objective_remains_active",
                    },
                    timestamp=now_ts,
                )
            objective.progress_summary = (
                summary.strip()
                if summary
                else "Latest linked task finished; Objective remains active for recurring or standing work."
            )
            self._append_objective_review_completed(
                objective,
                task_id=task_id,
                timestamp=now_ts,
                conclusion="standing_or_recurring_objective_remains_active",
                summary=objective.progress_summary,
                terminal_state=terminal_state_value,
                active_task_ids=[],
                active_plan_ids=[],
            )
            self._save_objective_logged(
                objective, step="task_end_review:standing_or_recurring_objective_remains_active"
            )
            self.operations.persist_runtime_state()
            return objective

        target_status: ObjectiveStatus
        conclusion: str
        decision_summary: str
        if terminal_state_value == ProgressState.COMPLETED.value:
            if (
                objective.requires_user_confirmation
                or objective.completion_policy == ObjectiveCompletionPolicy.USER_CONFIRMS
            ):
                target_status = ObjectiveStatus.BLOCKED
                conclusion = "awaiting_user_confirmation"
                decision_summary = "Linked work finished; Objective requires user confirmation before it can be marked satisfied."
            elif objective.completion_policy == ObjectiveCompletionPolicy.SUCCESS_CRITERIA_MET:
                if objective.success_criteria and all(
                    criterion.status == ObjectiveCriterionStatus.MET for criterion in objective.success_criteria
                ):
                    target_status = ObjectiveStatus.SATISFIED
                    conclusion = "success_criteria_met"
                    decision_summary = "All Objective success criteria are met and no linked work remains."
                else:
                    target_status = ObjectiveStatus.BLOCKED
                    conclusion = "unmet_success_criteria"
                    decision_summary = "Linked work finished, but Objective success criteria are still open."
            else:
                target_status = ObjectiveStatus.SATISFIED
                conclusion = "manager_decides_task_satisfied_objective"
                decision_summary = "Linked task completed and no active Objective-linked work remains."
        else:
            target_status = ObjectiveStatus.BLOCKED
            conclusion = "terminal_task_did_not_satisfy_objective"
            decision_summary = (
                f"Linked task ended with state {terminal_state_value}; Objective needs follow-up before it can close."
            )

        previous_status = objective.status
        objective.status = target_status
        objective.progress_summary = summary.strip() if summary else decision_summary
        if target_status == ObjectiveStatus.SATISFIED:
            objective.satisfied_by_task_id = objective.satisfied_by_task_id or task_id
            objective.satisfied_at = objective.satisfied_at or now_ts
        if previous_status != target_status:
            self._append_objective_event(
                objective,
                ObjectiveEventType.STATUS_CHANGED,
                f"Objective status changed from {previous_status.value} to {target_status.value}.",
                task_id=task_id,
                plan_id=objective.current_plan_id,
                user_visible=True,
                details={
                    "previous_status": previous_status.value,
                    "new_status": target_status.value,
                    "summary": objective.progress_summary,
                    "reason": conclusion,
                },
                timestamp=now_ts,
            )
        self._append_objective_review_completed(
            objective,
            task_id=task_id,
            timestamp=now_ts,
            conclusion=conclusion,
            summary=decision_summary,
            terminal_state=terminal_state_value,
            active_task_ids=[],
            active_plan_ids=[],
        )
        if self._save_objective_logged(objective, step=f"task_end_review:{conclusion}"):
            self.operations.emit_activity(
                activity_type=TaskActivityType.OBJECTIVE_UPDATED,
                stage=(
                    TaskActivityStage.COMPLETED
                    if target_status == ObjectiveStatus.SATISFIED
                    else TaskActivityStage.RUNNING
                ),
                task_id=task_id,
                summary=f"Objective review completed: {objective.title}",
                detail=decision_summary,
                user_visible=True,
                details=objective.model_dump(mode="json"),
            )
        self.operations.persist_runtime_state()
        return objective

    def _active_task_ids_for_objective(self, objective: ObjectiveRecord) -> List[str]:
        linked_task_ids = set(objective.related_task_ids or [])
        linked_task_ids.update(objective.recovery_task_ids or [])
        linked_task_ids.update(
            task_id
            for task_id, objective_id in self.objective_state.task_to_objective_id.items()
            if objective_id == objective.objective_id
        )
        active_task_ids: List[str] = []
        for linked_task_id in sorted(linked_task_ids):
            task_info = self.operations.get_task_info(linked_task_id)
            if task_info is not None and not self.operations.is_terminal_state(task_info.state):
                active_task_ids.append(linked_task_id)
        return active_task_ids

    def _active_plan_ids_for_objective(self, objective: ObjectiveRecord) -> List[str]:
        linked_plan_ids = set(objective.plan_ids or [])
        if objective.current_plan_id:
            linked_plan_ids.add(objective.current_plan_id)
        linked_plan_ids.update(
            plan_id
            for plan_id, objective_id in self.objective_state.plan_to_objective_id.items()
            if objective_id == objective.objective_id
        )
        active_plan_ids: List[str] = []
        terminal_statuses = self._plan_terminal_statuses()
        for linked_plan_id in sorted(linked_plan_ids):
            plan = self.operations.get_plan(linked_plan_id)
            if plan is not None and plan.status not in terminal_statuses:
                active_plan_ids.append(linked_plan_id)
        return active_plan_ids

    @staticmethod
    def _objective_has_continuing_links(objective: ObjectiveRecord) -> bool:
        return any(link.managed_by_objective for link in objective.schedule_links or []) or any(
            link.managed_by_objective for link in objective.heartbeat_links or []
        )

    def _append_objective_review_completed(
        self,
        objective: ObjectiveRecord,
        *,
        task_id: str,
        timestamp: str,
        conclusion: str,
        summary: str,
        terminal_state: str,
        active_task_ids: List[str],
        active_plan_ids: List[str],
    ) -> None:
        self._append_objective_event(
            objective,
            ObjectiveEventType.REVIEW_COMPLETED,
            summary,
            task_id=task_id,
            plan_id=objective.current_plan_id,
            user_visible=True,
            details={
                "conclusion": conclusion,
                "terminal_task_id": task_id,
                "terminal_state": terminal_state,
                "active_task_ids": active_task_ids,
                "active_plan_ids": active_plan_ids,
                "status": objective.status.value,
            },
            timestamp=timestamp,
        )

    def render_objective_markdown(self, objective_id: str, *, max_events: int = 8) -> str:
        objective = self.get_objective(objective_id)
        if objective is None:
            return ""
        lines = [
            "## Objective",
            f"- **ID:** {objective.objective_id}",
            f"- **Title:** {objective.title}",
            f"- **Status:** {objective.status.value}",
            f"- **Horizon:** {objective.horizon.value}",
            f"- **Priority:** {objective.priority.value}",
            f"- **Desired Outcome:** {objective.desired_outcome}",
            f"- **Why:** {objective.why}",
            f"- **Current Focus:** {objective.current_focus or 'N/A'}",
            f"- **Progress:** {objective.progress_summary or 'N/A'}",
            f"- **Current Plan:** {objective.current_plan_id or 'N/A'}",
        ]
        lines.append("\n### Success Criteria")
        if objective.success_criteria:
            for criterion in objective.success_criteria:
                lines.append(f"- [{criterion.status.value}] {criterion.description}")
                if criterion.evidence_summary:
                    lines.append(f"  Evidence: {criterion.evidence_summary}")
        else:
            lines.append("- None recorded.")
        lines.append("\n### Related Work")
        lines.append(f"- Plans: {', '.join(objective.plan_ids) if objective.plan_ids else 'None'}")
        lines.append(f"- Tasks: {', '.join(objective.related_task_ids) if objective.related_task_ids else 'None'}")
        if objective.schedule_links:
            lines.append(f"- Schedules: {', '.join(link.schedule_id for link in objective.schedule_links)}")
        if objective.heartbeat_links:
            lines.append(f"- Heartbeats: {', '.join(link.heartbeat_item_id for link in objective.heartbeat_links)}")
        lines.append("\n### Recent Objective Events")
        for event in objective.event_log[-max(1, int(max_events or 8)):]:
            lines.append(f"- [{event.created_at}] {event.event_type.value}: {event.summary}")
        if not objective.event_log:
            lines.append("- None recorded.")
        return "\n".join(lines)
