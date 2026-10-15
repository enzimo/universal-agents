"""Plan and recovery workflow coordination for TaskManager."""

from __future__ import annotations

from custom_agents.task_manager_ports import PlanCoordinatorOperations
from custom_agents.task_runtime_state import RuntimeOwnerIdentity
from custom_agents.task_runtime_state import ObjectiveRuntimeState
from custom_agents.task_runtime_state import PlanRuntimeState
from custom_agents.task_runtime_state import TaskRuntimeState
import logging
from custom_agents.objective_work_sessions import ObjectiveWorkSessionCoordinator
from storage.plan_repository import PlanRepository

from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from custom_messages.contracts.runtime_snapshot_v3 import PlanStepTaskLink, RecoveryTaskLink
from custom_agents.task_records import TaskInfo
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import ExecutionDigest, Task, TaskAnswer
from custom_messages.enums import ExecutionPlanItemStatus, ExecutionUnknownStatus, MessageMetaType, ObjectiveEventType, PlanActionStatus, PlanActionType, PlanStatus, PlanStepFailurePolicy, ProgressState, RequesterType, TaskActivityStage, TaskActivityType, TaskHistoryImportance, TaskHistorySource, TaskHistoryType
from custom_messages.records.plans import PlanActionRecord, PlanRecord, PlanStepRecord
from id_generation import new_id


class PlanCoordinator:
    """Coordinate execution plans, plan-step tasks, and isolated recovery workflows."""

    def __init__(
        self,
        *,
        operations: PlanCoordinatorOperations,
        identity: RuntimeOwnerIdentity,
        objective_state: ObjectiveRuntimeState,
        plan_state: PlanRuntimeState,
        task_state: TaskRuntimeState,
        logger: logging.Logger,
        objective_work_coordinator: ObjectiveWorkSessionCoordinator,
        plan_repository: PlanRepository,
    ) -> None:
        self.operations = operations
        self.identity = identity
        self.objective_state = objective_state
        self.plan_state = plan_state
        self.task_state = task_state
        self._logger = logger
        self.objective_work_coordinator = objective_work_coordinator
        self.plan_repository = plan_repository


    def _ensure_task_plan_link(
        self,
        task_info: TaskInfo,
        parent_task_info: Optional[TaskInfo] = None,
        *,
        create: bool = False,
    ) -> Optional[str]:
        if not self.operations.plans_enabled():
            return None

        task_id = task_info.task.task_id
        existing_plan_id = self.plan_state.task_to_plan_id.get(task_id)
        if existing_plan_id:
            return existing_plan_id

        if parent_task_info:
            parent_plan_id = self._ensure_task_plan_link(parent_task_info, create=False)
            if parent_plan_id:
                self.plan_state.task_to_plan_id[task_id] = parent_plan_id
                return parent_plan_id

        parent_task_id = task_info.task.parent_task_id
        if parent_task_id and parent_task_id in self.task_state.tasks:
            parent_plan_id = self._ensure_task_plan_link(self.task_state.tasks[parent_task_id], create=False)
            if parent_plan_id:
                self.plan_state.task_to_plan_id[task_id] = parent_plan_id
                return parent_plan_id

        if not create:
            return None

        plan_id = self.operations.new_plan_id()
        self.plan_state.task_to_plan_id[task_id] = plan_id
        return plan_id

    @staticmethod
    def _plan_status_for_progress_state(state: Optional[ProgressState]) -> Optional[PlanStatus]:
        if state == ProgressState.COMPLETED:
            return PlanStatus.COMPLETED
        if state == ProgressState.FAILED:
            return PlanStatus.FAILED
        if state == ProgressState.CANCELLED:
            return PlanStatus.CANCELLED
        if state == ProgressState.ABANDONED:
            return PlanStatus.ABANDONED
        return None

    def _related_task_ids_for_plan(self, plan_id: str, existing: Optional[PlanRecord] = None) -> List[str]:
        related: List[str] = list(dict.fromkeys(existing.related_task_ids if existing else []))
        for task_id, task_plan_id in self.plan_state.task_to_plan_id.items():
            if task_plan_id == plan_id and task_id in self.task_state.tasks and task_id not in related:
                related.append(task_id)
        for link in self.plan_state.recovery_links.values():
            if link.original_plan_id != plan_id:
                continue
            for task_id in (link.original_task_id, link.recovery_task_id):
                if task_id and task_id not in related:
                    related.append(task_id)
        for link in self.plan_state.plan_step_task_links.values():
            if link.plan_id != plan_id:
                continue
            for task_id in (link.root_task_id, link.step_task_id):
                if task_id and task_id in self.task_state.tasks and task_id not in related:
                    related.append(task_id)
        return related

    @staticmethod
    def _apply_plan_digest_to_task(task: Task, plan: PlanRecord) -> None:
        """Hydrate one replaceable execution Task from Objective-owned Plan state."""
        task.task_plan = plan.plan_summary
        task.execution_digest = ExecutionDigest(
            goal_summary=plan.goal_summary,
            plan_summary=plan.plan_summary,
            current_focus=plan.current_focus,
            plan_items=[item.model_copy(deep=True) for item in plan.plan_items],
            unknowns=[item.model_copy(deep=True) for item in plan.unknowns],
            last_updated_at=plan.updated_at,
        )

    def link_task_to_plan(self, task_id: str, plan_id: str, *, hydrate: bool = True) -> Optional[PlanRecord]:
        """Link a manager-local Task to a durable Plan without transferring lifecycle ownership."""
        if not self.operations.plans_enabled():
            return None
        task_info = self.operations.get_task_info(task_id)
        plan = self.get_plan(plan_id)
        if task_info is None or plan is None:
            return None
        self.plan_state.task_to_plan_id[task_id] = plan.plan_id
        if task_id not in plan.related_task_ids:
            plan.related_task_ids.append(task_id)
        if hydrate:
            self._apply_plan_digest_to_task(task_info.task, plan)
        plan.updated_at = datetime.now(timezone.utc).isoformat()
        if not self._save_plan(plan):
            self.plan_state.task_to_plan_id.pop(task_id, None)
            return None
        return plan

    def _task_materializes_plan_state(
        self,
        task_id: str,
        task_info: TaskInfo,
        existing: Optional[PlanRecord],
    ) -> bool:
        if existing is None or task_id == existing.root_task_id:
            return True
        if self.is_plan_step_task(task_id) or self.is_isolated_recovery_task(task_id):
            return False
        return task_info.task.parent_task_id is None and bool(existing.objective_id)

    def _status_for_plan_sync(
        self,
        task_id: str,
        task_info: TaskInfo,
        plan: PlanRecord,
        *,
        objective_owned: bool,
    ) -> PlanStatus:
        if objective_owned:
            return plan.status
        root_info = self.operations.get_task_info(plan.root_task_id) if plan.root_task_id else None
        root_state = root_info.state if root_info is not None else None
        if root_state is None and plan.root_task_id == task_id:
            root_state = task_info.state
        return self._plan_status_for_progress_state(root_state) or plan.status

    def _build_plan_for_task_sync(
        self,
        *,
        plan_id: str,
        task_id: str,
        task_info: TaskInfo,
        existing: Optional[PlanRecord],
        objective: Any,
        now_ts: str,
    ) -> PlanRecord:
        plan = existing.model_copy(deep=True) if existing is not None else PlanRecord(
            plan_id=plan_id,
            root_task_id=self.operations.get_top_level_task_id(task_id),
            root_conversation_id=getattr(task_info.meta, "conversation_id", None),
            created_at=now_ts,
            updated_at=now_ts,
        )
        plan.owner_agent_id = self.operations.durable_manager_scope_id()
        plan.owner_agent_name = self.identity.owner_agent_name
        plan.team_id = self.identity.team_id
        plan.owner_named_agent_type = self.identity.owner_named_agent_type
        if not plan.root_task_id:
            plan.root_task_id = self.operations.get_top_level_task_id(task_id)
        if not plan.root_conversation_id:
            plan.root_conversation_id = getattr(task_info.meta, "conversation_id", None)
        plan.source_task_id = task_id
        plan.related_task_ids = self._related_task_ids_for_plan(plan_id, existing)

        if objective is not None:
            plan.objective_id = objective.objective_id
            plan.objective_title = objective.title
        plan.status = self._status_for_plan_sync(
            task_id,
            task_info,
            plan,
            objective_owned=objective is not None or bool(plan.objective_id),
        )
        if self._task_materializes_plan_state(task_id, task_info, existing):
            digest = self.operations.ensure_execution_digest(task_info.task)
            plan.goal_summary = digest.goal_summary
            plan.plan_summary = digest.plan_summary
            plan.current_focus = digest.current_focus
            plan.plan_items = list(digest.plan_items)
            plan.unknowns = list(digest.unknowns)
            plan.updated_at = digest.last_updated_at or plan.updated_at or now_ts
        return plan

    def _sync_plan_from_task(self, task_id: str, *, create: bool = False) -> Optional[PlanRecord]:
        if not self.operations.plans_enabled():
            return None
        task_info = self.operations.get_task_info(task_id)
        if not task_info:
            return None

        plan_id = self._ensure_task_plan_link(task_info, create=create)
        if not plan_id:
            return None
        now_ts = datetime.now(timezone.utc).isoformat()

        existing: Optional[PlanRecord] = None
        try:
            existing = self.plan_repository.get_plan(
                plan_id,
                owner_agent_id=self.operations.durable_manager_scope_id(),
            )
        except Exception as exc:
            self._logger.error("Failed to load plan %s before sync: %s", plan_id, exc)

        objective = self.operations.get_objective_for_plan_id(plan_id)
        if objective is None:
            objective = self.operations.get_task_objective_record(task_id)
        if objective is not None:
            self.objective_state.plan_to_objective_id[plan_id] = objective.objective_id
        plan = self._build_plan_for_task_sync(
            plan_id=plan_id,
            task_id=task_id,
            task_info=task_info,
            existing=existing,
            objective=objective,
            now_ts=now_ts,
        )

        try:
            self.plan_repository.save_plan(plan)
        except Exception as exc:
            self._logger.error("Failed to persist plan %s for task %s: %s", plan_id, task_id, exc)
            return None
        if objective is not None:
            changed = False
            if plan_id not in objective.plan_ids:
                objective.plan_ids.append(plan_id)
                changed = True
                self.operations.append_objective_event(
                    objective,
                    ObjectiveEventType.PLAN_LINKED,
                    f"Linked plan {plan_id} to objective.",
                    task_id=task_id,
                    plan_id=plan_id,
                    user_visible=False,
                )
            if objective.current_plan_id != plan_id:
                objective.current_plan_id = plan_id
                changed = True
            related_task_ids = self._related_task_ids_for_plan(plan_id, plan)
            for related_task_id in related_task_ids:
                if related_task_id not in objective.related_task_ids:
                    objective.related_task_ids.append(related_task_id)
                    self.objective_state.task_to_objective_id[related_task_id] = objective.objective_id
                    changed = True
            if changed:
                self.operations.save_objective(objective)
        return plan

    def get_plan(self, plan_id: str) -> Optional[PlanRecord]:
        if not self.operations.plans_enabled():
            return None
        try:
            return self.plan_repository.get_plan(
                plan_id,
                owner_agent_id=self.operations.durable_manager_scope_id(),
            )
        except Exception as exc:
            self._logger.error("Failed to load plan %s: %s", plan_id, exc)
            return None

    def update_plan_status(
        self,
        plan_id: str,
        status: PlanStatus | str,
        *,
        summary: str,
        task_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Optional[PlanRecord]:
        """Apply an explicit Plan lifecycle transition; Task terminal state is never used here."""
        if not self.operations.plans_enabled():
            return None
        if not summary or not summary.strip():
            raise ValueError("Plan status transitions require a summary.")
        target_status = status if isinstance(status, PlanStatus) else PlanStatus(str(status))
        plan = self.get_plan(plan_id)
        if plan is None:
            return None
        terminal_statuses = {
            PlanStatus.COMPLETED,
            PlanStatus.FAILED,
            PlanStatus.CANCELLED,
            PlanStatus.ABANDONED,
        }
        if plan.status in terminal_statuses and plan.status != target_status:
            raise ValueError("A terminal Plan cannot be reactivated or changed to another terminal state.")
        previous_status = plan.status
        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        plan.status = target_status
        if target_status in terminal_statuses:
            plan.active_execution_task_id = None
        plan.updated_at = now_ts
        if not self._save_plan(plan):
            return None

        history_task_id = task_id or plan.source_task_id or plan.root_task_id
        if history_task_id and self.operations.get_task_info(history_task_id) is not None:
            self.operations.append_history_entry(
                history_task_id,
                f"Plan status changed from {previous_status.value} to {target_status.value}: {summary.strip()}",
                entry_type=TaskHistoryType.PLAN_EVENT,
                importance=TaskHistoryImportance.HIGH,
                timestamp=now_ts,
                agent_name=agent_name,
                agent_id=agent_id,
                tracks_to_plan=True,
                details={
                    "action": "update_plan_status",
                    "previous_status": previous_status.value,
                    "new_status": target_status.value,
                    "summary": summary.strip(),
                },
            )
        activity_stage = {
            PlanStatus.COMPLETED: TaskActivityStage.COMPLETED,
            PlanStatus.FAILED: TaskActivityStage.FAILED,
            PlanStatus.CANCELLED: TaskActivityStage.CANCELLED,
            PlanStatus.ABANDONED: TaskActivityStage.CANCELLED,
        }.get(target_status, TaskActivityStage.PLANNING)
        self.operations.emit_activity(
            activity_type=TaskActivityType.PLAN_UPDATED,
            stage=activity_stage,
            task_id=history_task_id,
            summary=f"Plan status changed to {target_status.value}.",
            detail=summary.strip(),
            details={
                "plan_id": plan.plan_id,
                "previous_status": previous_status.value,
                "new_status": target_status.value,
                "objective_id": plan.objective_id,
            },
        )
        return plan

    def get_task_plan_id(self, task_id: str) -> Optional[str]:
        if not self.operations.plans_enabled():
            return None
        existing_plan_id = self.plan_state.task_to_plan_id.get(task_id)
        if existing_plan_id:
            return existing_plan_id
        recovery_link = self.plan_state.recovery_links.get(task_id)
        if recovery_link:
            return recovery_link.original_plan_id
        task_info = self.operations.get_task_info(task_id)
        if task_info:
            return self._ensure_task_plan_link(task_info, create=False)

        if not self.identity.owner_agent_id:
            return None
        try:
            plan = self.plan_repository.get_plan_by_task(
                task_id,
                owner_agent_id=self.operations.durable_manager_scope_id(),
            )
        except Exception as exc:
            self._logger.error("Failed to load plan id for archived task %s: %s", task_id, exc)
            return None
        return plan.plan_id if plan else None

    @staticmethod
    def _plan_action_importance(action_type: PlanActionType, user_visible: bool) -> TaskHistoryImportance:
        if action_type in {
            PlanActionType.RECORD_FAILURE,
            PlanActionType.START_PLAN_EXECUTION,
            PlanActionType.START_INLINE_RECOVERY,
            PlanActionType.START_ISOLATED_RECOVERY,
            PlanActionType.RECOVERY_RESULT_READY,
            PlanActionType.ASK_USER,
            PlanActionType.ABANDON_BRANCH,
        }:
            return TaskHistoryImportance.HIGH
        if user_visible:
            return TaskHistoryImportance.HIGH
        return TaskHistoryImportance.NORMAL

    def get_plan_actions(self, task_id: str, *, limit: int = 10) -> List[PlanActionRecord]:
        if not self.operations.plans_enabled():
            return []
        plan_id = self.get_task_plan_id(task_id)
        if not plan_id:
            return []
        plan = self.get_plan(plan_id)
        if plan is None:
            return []
        safe_limit = max(1, min(int(limit or 10), 100))
        return list(plan.plan_actions)[-safe_limit:]

    def record_plan_action(
        self,
        task_id: str,
        action_type: PlanActionType,
        summary: str,
        *,
        status: PlanActionStatus = PlanActionStatus.RECORDED,
        reason: Optional[str] = None,
        related_task_id: Optional[str] = None,
        recovery_task_id: Optional[str] = None,
        plan_step_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        user_visible: bool = False,
        details: Optional[Dict[str, Any]] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Optional[PlanActionRecord]:
        if not self.operations.plans_enabled():
            self._logger.info("Ignoring plan action for task %s because first-class plans are manager-only.", task_id)
            return None
        if not summary or not summary.strip():
            raise ValueError("Plan action summary is required.")

        task_info = self.operations.get_task_info(task_id)
        archived_task = None
        if task_info is None:
            archived = self.operations.get_task_from_db(task_id)
            archived_task = archived["main_task"] if archived else None
            if archived_task is None:
                self._logger.warning("Cannot record plan action for unknown task %s.", task_id)
                return None

        plan = self._sync_plan_from_task(task_id, create=True) if task_info is not None else None
        if plan is None:
            plan_id = self.get_task_plan_id(task_id)
            plan = self.get_plan(plan_id) if plan_id else None
        if plan is None:
            self._logger.warning("Cannot record plan action for task %s without a manager plan.", task_id)
            return None

        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        action = PlanActionRecord(
            action_type=action_type,
            status=status,
            summary=summary.strip(),
            reason=reason or None,
            task_id=task_id,
            related_task_id=related_task_id or None,
            recovery_task_id=recovery_task_id or None,
            plan_step_id=plan_step_id or None,
            branch_id=branch_id or None,
            user_visible=bool(user_visible),
            details=details or {},
            created_at=now_ts,
            updated_at=now_ts,
        )

        plan.plan_actions.append(action)
        if recovery_task_id and recovery_task_id not in plan.recovery_task_ids:
            plan.recovery_task_ids.append(recovery_task_id)
        plan.source_task_id = task_id
        plan.updated_at = now_ts
        try:
            self.plan_repository.save_plan(plan)
        except Exception as exc:
            self._logger.error("Failed to persist plan action %s for task %s: %s", action.action_id, task_id, exc)
            return None

        history_summary = f"Plan action `{action.action_type.value}` recorded: {action.summary}"
        history_details = {
            "action": "record_plan_action",
            "plan_action": action.model_dump(mode="json"),
        }
        history_importance = self._plan_action_importance(action.action_type, action.user_visible)
        history_writer = self.operations.append_history_entry if task_info is not None else self.operations.append_history_entry_to_archived_task
        history_writer(
            task_id,
            history_summary,
            source=TaskHistorySource.AGENT if agent_id or agent_name else TaskHistorySource.SYSTEM,
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=history_importance,
            timestamp=now_ts,
            agent_name=agent_name,
            agent_id=agent_id,
            related_task_id=related_task_id,
            plan_step_id=plan_step_id,
            tracks_to_plan=True,
            details=history_details,
        )
        self.operations.emit_activity(
            activity_type=TaskActivityType.PLAN_UPDATED,
            stage=TaskActivityStage.PLANNING,
            task_id=task_id,
            summary=f"Plan action recorded: {action.action_type.value}.",
            detail=action.summary,
            user_visible=bool(user_visible),
            details=action.model_dump(mode="json"),
        )
        return action

    def is_isolated_recovery_task(self, task_id: str) -> bool:
        """Return whether a task is an internally linked isolated recovery task."""
        return task_id in self.plan_state.recovery_links

    def get_recovery_link(self, recovery_task_id: str) -> Optional[RecoveryTaskLink]:
        return self.plan_state.recovery_links.get(recovery_task_id)

    def get_recovery_links_for_original(self, original_task_id: str) -> List[RecoveryTaskLink]:
        return [
            link
            for link in self.plan_state.recovery_links.values()
            if link.original_task_id == original_task_id
        ]

    def is_plan_step_task(self, task_id: str) -> bool:
        """Return whether a task is an internal manager PlanStep execution task."""
        return task_id in self.plan_state.plan_step_task_links

    def get_plan_step_task_link(self, step_task_id: str) -> Optional[PlanStepTaskLink]:
        return self.plan_state.plan_step_task_links.get(step_task_id)

    def is_plan_execution_root_active(self, root_task_id: str) -> bool:
        """Return whether the root task has active runtime-owned plan steps."""
        plan_id = self.get_task_plan_id(root_task_id)
        if not plan_id:
            return False
        plan = self.get_plan(plan_id)
        if plan is None or not plan.plan_steps:
            return False
        if plan.active_execution_task_id != root_task_id:
            return False
        return any(
            step.status in {ExecutionPlanItemStatus.PLANNED, ExecutionPlanItemStatus.IN_PROGRESS}
            for step in plan.plan_steps
        )

    def is_final_review_pending(self, task_id: str) -> bool:
        """Return whether a suppressed terminal response queued a bounded final review turn."""
        return task_id in self.plan_state.final_review_pending_task_ids

    def _collect_unresolved_final_review_items(self, task_id: str) -> Dict[str, Any]:
        digest = self.operations.get_execution_digest(task_id)
        open_plan_items: List[Dict[str, Any]] = []
        open_unknowns: List[Dict[str, Any]] = []
        if digest is not None:
            open_plan_items = [
                {
                    "step_id": item.step_id,
                    "title": item.title,
                    "status": item.status.value,
                    "status_detail": item.status_detail,
                    "resolution_summary": item.resolution_summary,
                }
                for item in digest.plan_items
                if item.status not in {ExecutionPlanItemStatus.COMPLETED, ExecutionPlanItemStatus.DROPPED}
            ]
            open_unknowns = [
                {
                    "unknown_id": item.unknown_id,
                    "summary": item.summary,
                    "status": item.status.value,
                    "resolution_plan": item.resolution_plan,
                    "resolution_summary": item.resolution_summary,
                }
                for item in digest.unknowns
                if item.status == ExecutionUnknownStatus.OPEN
            ]

        plan_id = self.get_task_plan_id(task_id)
        plan = self.get_plan(plan_id) if plan_id else None
        unresolved_plan_steps: List[Dict[str, Any]] = []
        if plan is not None:
            unresolved_plan_steps = [
                {
                    "step_id": step.step_id,
                    "title": step.title,
                    "status": step.status.value,
                    "status_detail": step.status_detail,
                    "failure_type": step.failure_type,
                    "failure_summary": step.failure_summary,
                }
                for step in plan.plan_steps
                if step.status not in {ExecutionPlanItemStatus.COMPLETED, ExecutionPlanItemStatus.DROPPED}
            ]

        return {
            "open_plan_items": open_plan_items,
            "open_unknowns": open_unknowns,
            "unresolved_plan_steps": unresolved_plan_steps,
        }

    @staticmethod
    def _has_unresolved_final_review_items(items: Dict[str, Any]) -> bool:
        return bool(
            items.get("open_plan_items")
            or items.get("open_unknowns")
            or items.get("unresolved_plan_steps")
        )

    def begin_final_review_if_needed(
        self,
        task_id: str,
        *,
        attempted_answer: Any = None,
        max_attempts: int = 1,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Suppress one terminal response and requeue a bounded final review turn when plan state is unresolved."""
        if not self.operations.plans_enabled():
            return None
        task_info = self.operations.get_task_info(task_id)
        if task_info is None or task_id in self.task_state.subtask_to_parent_map:
            return None
        if self.is_plan_step_task(task_id) or self.is_isolated_recovery_task(task_id):
            return None
        if self.is_plan_execution_root_active(task_id):
            return None
        plan_id = self.get_task_plan_id(task_id)
        plan = self.get_plan(plan_id) if plan_id else None
        if plan is not None and plan.objective_id:
            return None

        unresolved = self._collect_unresolved_final_review_items(task_id)
        if not self._has_unresolved_final_review_items(unresolved):
            return None

        attempts = int(self.plan_state.final_review_attempt_counts.get(task_id, 0) or 0)
        if attempts >= max(1, int(max_attempts or 1)):
            return None
        if task_id in self.plan_state.final_review_pending_task_ids:
            return {
                "task_id": task_id,
                "attempt": attempts,
                "max_attempts": max_attempts,
                "already_pending": True,
                **unresolved,
            }

        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        self.plan_state.final_review_attempt_counts[task_id] = attempts + 1
        self.plan_state.final_review_pending_task_ids.add(task_id)
        # Review budget/pending state is an explicit workflow commit. Appending
        # history persists the affected task, not unrelated coordinator state.
        self.operations.persist_runtime_state()
        attempted_answer_preview = self._compact_answer_text(attempted_answer, max_chars=1200)

        self.operations.append_history_entry(
            task_id,
            (
                "Final review required before completion: unresolved plan items, "
                "unknowns, or runtime plan steps remain. Decide whether to continue, "
                "ask the user, or complete with explicit caveats."
            ),
            source=TaskHistorySource.SYSTEM,
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.HIGH,
            timestamp=now_ts,
            agent_name=agent_name,
            agent_id=agent_id,
            tracks_to_plan=True,
            details={
                "action": "begin_final_review_loop",
                "attempt": attempts + 1,
                "max_attempts": max_attempts,
                "attempted_answer_preview": attempted_answer_preview,
                **unresolved,
            },
        )
        self.record_plan_action(
            task_id,
            PlanActionType.CHECKPOINT,
            "Final review loop started because unresolved plan state remains.",
            status=PlanActionStatus.ACTIVE,
            user_visible=False,
            details={
                "action": "begin_final_review_loop",
                "attempt": attempts + 1,
                "max_attempts": max_attempts,
                **unresolved,
            },
            agent_name=agent_name,
            agent_id=agent_id,
            timestamp=now_ts,
        )
        self.operations.update_main_task_state(task_id, ProgressState.PARTIAL, now_ts)
        self.operations.enqueue_task(task_id, front=True)
        return {
            "task_id": task_id,
            "attempt": attempts + 1,
            "max_attempts": max_attempts,
            "attempted_answer_preview": attempted_answer_preview,
            **unresolved,
        }

    def _save_plan(self, plan: PlanRecord) -> bool:
        try:
            if plan.objective_id:
                self.objective_state.plan_to_objective_id[plan.plan_id] = plan.objective_id
            self.plan_repository.save_plan(plan)
            self.operations.emit_activity(
                activity_type=TaskActivityType.PLAN_UPDATED,
                stage=TaskActivityStage.PLANNING,
                task_id=plan.active_execution_task_id or plan.root_task_id,
                summary="Objective plan snapshot updated.",
                user_visible=True,
                details={"plan": plan.model_dump(mode="json")},
            )
            return True
        except Exception as exc:
            self._logger.error("Failed to persist plan %s: %s", plan.plan_id, exc)
            return False

    def _next_runnable_plan_step(self, plan: PlanRecord) -> Optional[PlanStepRecord]:
        completed = {
            step.step_id
            for step in plan.plan_steps
            if step.status in {ExecutionPlanItemStatus.COMPLETED, ExecutionPlanItemStatus.DROPPED}
        }
        for step in sorted(plan.plan_steps, key=lambda item: item.order_index):
            if step.status != ExecutionPlanItemStatus.PLANNED:
                continue
            if all(dep in completed for dep in step.depends_on_step_ids):
                return step
        return None

    def _record_plan_progress_notice(
        self,
        root_task_id: str,
        summary: str,
        *,
        reason: Optional[str] = None,
        related_task_id: Optional[str] = None,
        recovery_task_id: Optional[str] = None,
        plan_step_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Optional[PlanActionRecord]:
        """Record a user-visible plan progress notice through the plan activity path."""
        return self.record_plan_action(
            root_task_id,
            PlanActionType.USER_NOTICE,
            summary,
            status=PlanActionStatus.COMPLETED,
            reason=reason,
            related_task_id=related_task_id,
            recovery_task_id=recovery_task_id,
            plan_step_id=plan_step_id,
            branch_id=branch_id,
            user_visible=True,
            details={"notice_type": "plan_progress", **(details or {})},
            agent_name=agent_name,
            agent_id=agent_id,
            timestamp=timestamp,
        )

    def _enqueue_plan_step_task(
        self,
        plan: PlanRecord,
        next_step: PlanStepRecord,
        *,
        root_task_info: TaskInfo,
        timestamp: Optional[str] = None,
    ) -> Optional[TaskInfo]:
        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        step_task_id = new_id("plan_step_task")
        step_task = Task(
            task_id=step_task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement=self.operations.build_plan_step_problem_statement(
                root_problem_statement=root_task_info.task.problem_statement,
                step=next_step,
                plan_steps=plan.plan_steps,
            ),
        )
        requester_route = (
            root_task_info.header.response_routing_key
            or root_task_info.header.from_agent_id
        )
        step_header = root_task_info.header.model_copy(deep=True)
        step_header.from_agent_id = self.identity.owner_agent_id or step_header.from_agent_id
        step_header.to_routing_path = self.identity.owner_agent_id or step_header.to_routing_path
        # Plan-step execution stays local, but requester-bound actions created by
        # the step must still reach the root task's requester.
        step_header.response_routing_key = requester_route
        step_meta = root_task_info.meta.model_copy(deep=True)
        step_meta.msg_correlation_id = f"plan_step_corr_{step_task_id}"
        step_meta.conversation_id = new_id("plan_step_conv")
        step_meta.followup_to_task_id = None
        step_meta.is_response_requested = False
        step_meta.to_agent_name = self.identity.owner_agent_name
        step_meta.to_named_agent_type = self.identity.owner_named_agent_type
        step_meta.objective_id = plan.objective_id or self.operations.get_task_objective_id(root_task_info.task.task_id)

        link = PlanStepTaskLink(
            root_task_id=root_task_info.task.task_id,
            plan_id=plan.plan_id,
            step_id=next_step.step_id,
            step_task_id=step_task_id,
            created_at=now_ts,
        )
        self.plan_state.task_to_plan_id[step_task_id] = plan.plan_id
        if step_meta.objective_id:
            self.objective_state.task_to_objective_id[step_task_id] = step_meta.objective_id
        self.plan_state.plan_step_task_links[step_task_id] = link
        # A plan step works for its root task's person (TaskInfo.owner_person_id).
        if not self.operations.add_task(
            step_task, step_header, step_meta,
            owner_person_id=root_task_info.owner_person_id,
        ):
            self.plan_state.task_to_plan_id.pop(step_task_id, None)
            self.objective_state.task_to_objective_id.pop(step_task_id, None)
            self.plan_state.plan_step_task_links.pop(step_task_id, None)
            return None

        next_step.status = ExecutionPlanItemStatus.IN_PROGRESS
        next_step.status_detail = "Internal manager step task is queued."
        next_step.current_step_task_id = step_task_id
        next_step.step_task_ids.append(step_task_id)
        next_step.attempt_count = max(int(next_step.attempt_count or 0), 0) + 1
        next_step.updated_at = now_ts
        plan.updated_at = now_ts
        plan.related_task_ids = self._related_task_ids_for_plan(plan.plan_id, plan)
        self._save_plan(plan)
        self.record_plan_action(
            root_task_info.task.task_id,
            PlanActionType.PLAN_STEP_STARTED,
            f"Started plan step: {next_step.title}",
            status=PlanActionStatus.ACTIVE,
            plan_step_id=next_step.step_id,
            related_task_id=step_task_id,
            user_visible=False,
            details={"step_order": next_step.order_index},
            agent_name=self.identity.owner_agent_name,
            agent_id=self.identity.owner_agent_id,
            timestamp=now_ts,
        )
        self.operations.append_history_entry(
            step_task_id,
            "Internal plan-step task linked to root plan execution by runtime metadata.",
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.HIGH,
            related_task_id=root_task_info.task.task_id,
            plan_step_id=next_step.step_id,
            tracks_to_plan=True,
            details={"action": "plan_step_task_created"},
        )
        self.operations.persist_runtime_state()
        return self.operations.get_task_info(step_task_id)

    def _enqueue_next_plan_step_task(
        self,
        plan: PlanRecord,
        *,
        root_task_info: TaskInfo,
        timestamp: Optional[str] = None,
    ) -> Optional[TaskInfo]:
        if not self.objective_work_coordinator.admits_plan_step(plan, root_task_info):
            self._logger.info(
                "Deferring plan step for Objective %s until its next bounded work session.",
                plan.objective_id,
            )
            self.objective_work_coordinator._emit_snapshot(
                plan.objective_id,
                "Plan is checkpointed until the next Objective work session.",
                task_id=root_task_info.task.task_id,
            )
            return None
        next_step = self._next_runnable_plan_step(plan)
        if next_step is None:
            return None
        return self._enqueue_plan_step_task(
            plan,
            next_step,
            root_task_info=root_task_info,
            timestamp=timestamp,
        )

    def activate_plan_execution(
        self,
        root_task_id: str,
        *,
        steps: List[Dict[str, Any]],
        final_review: bool = True,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[PlanRecord]:
        """Promote a manager plan into runtime-owned sequential PlanStep execution."""
        if not self.operations.plans_enabled():
            self._logger.info("Ignoring plan execution activation because first-class plans are manager-only.")
            return None
        root_info = self.operations.get_task_info(root_task_id)
        if root_info is None:
            self._logger.warning("Cannot activate plan execution for unknown root task %s.", root_task_id)
            return None
        plan = self._sync_plan_from_task(root_task_id, create=True)
        if plan is None:
            return None
        if plan.plan_steps and self.is_plan_execution_root_active(root_task_id):
            self._logger.warning("Plan execution is already active for root task %s.", root_task_id)
            return plan

        semantic_steps: List[Dict[str, Any]] = []
        for raw_step in steps:
            if not isinstance(raw_step, dict):
                continue
            title = self.operations.compact_plan_step_text(raw_step.get("title"), max_chars=180)
            objective = self.operations.compact_plan_step_text(raw_step.get("objective") or raw_step.get("description"), max_chars=1000)
            rationale = self.operations.compact_plan_step_text(raw_step.get("rationale"), max_chars=1000) or None
            if not title or not objective:
                continue
            semantic_steps.append({"title": title, "objective": objective, "rationale": rationale})

        if final_review:
            titles = {step["title"].strip().lower() for step in semantic_steps}
            if "final review and response" not in titles:
                semantic_steps.append(
                    {
                        "title": "Final review and response",
                        "objective": "Review completed step outcomes and prepare the final user-facing response.",
                        "rationale": "The manager should synthesize the result only after bounded plan execution is complete.",
                        "final_response_step": True,
                    }
                )
        if not semantic_steps:
            raise ValueError("At least one plan step with title and objective is required.")

        now_ts = datetime.now(timezone.utc).isoformat()
        plan.plan_steps = []
        plan.active_execution_task_id = root_task_id
        previous_step_id: Optional[str] = None
        for index, raw_step in enumerate(semantic_steps):
            step = PlanStepRecord(
                step_id=new_id("plan_step"),
                title=raw_step["title"],
                objective=raw_step["objective"],
                rationale=raw_step.get("rationale") or None,
                order_index=index,
                depends_on_step_ids=[previous_step_id] if previous_step_id else [],
                final_response_step=bool(raw_step.get("final_response_step")),
                created_at=now_ts,
                updated_at=now_ts,
            )
            plan.plan_steps.append(step)
            previous_step_id = step.step_id
        plan.current_focus = plan.plan_steps[0].title
        plan.updated_at = now_ts
        if not self._save_plan(plan):
            return None
        self.record_plan_action(
            root_task_id,
            PlanActionType.START_PLAN_EXECUTION,
            "Activated runtime plan-step execution.",
            status=PlanActionStatus.ACTIVE,
            user_visible=False,
            details={"step_count": len(plan.plan_steps), "final_review": bool(final_review)},
            agent_name=agent_name,
            agent_id=agent_id,
            timestamp=now_ts,
        )
        self._record_plan_progress_notice(
            root_task_id,
            f"Created a runtime plan with {len(plan.plan_steps)} steps. Starting: {plan.plan_steps[0].title}",
            reason="plan_created",
            plan_step_id=plan.plan_steps[0].step_id,
            details={
                "step_count": len(plan.plan_steps),
                "first_step_title": plan.plan_steps[0].title,
                "final_review": bool(final_review),
            },
            agent_name=agent_name,
            agent_id=agent_id,
            timestamp=now_ts,
        )
        latest_plan = self.get_plan(plan.plan_id) or plan
        admitted = self._enqueue_next_plan_step_task(latest_plan, root_task_info=root_info, timestamp=now_ts)
        if admitted is not None:
            self.task_state.task_queue = deque(task_id for task_id in self.task_state.task_queue if task_id != root_task_id)
        else:
            latest_plan.active_execution_task_id = None
            self._save_plan(latest_plan)
        self.operations.persist_runtime_state()
        return self.get_plan(plan.plan_id) or latest_plan

    def _resume_checkpointed_plan_root(self, plan: PlanRecord, root_info: TaskInfo, timestamp: str) -> bool:
        """Finish this execution window while retaining the Objective's plan."""
        session = self.objective_work_coordinator.session_for_task(root_info.task.task_id)
        if session is None or not session.checkpoint_id:
            return False
        if any(step.status == ExecutionPlanItemStatus.IN_PROGRESS for step in plan.plan_steps):
            return False
        plan.active_execution_task_id = None
        plan.updated_at = timestamp
        self._save_plan(plan)
        self.operations.update_main_task_state(root_info.task.task_id, ProgressState.PARTIAL, timestamp)
        return self.operations.enqueue_task(root_info.task.task_id, front=True)

    def _build_plan_step_result_summary(self, step_task: Task, terminal_state: ProgressState) -> str:
        answer_text = self._compact_answer_text(getattr(step_task, "answer", None))
        errors = list(getattr(step_task, "errors", None) or [])
        if terminal_state == ProgressState.COMPLETED and answer_text:
            return answer_text
        if terminal_state == ProgressState.COMPLETED:
            return "Plan step completed without a textual result."
        if errors:
            compact_errors = " ".join(str(error).strip() for error in errors if str(error).strip())
            if len(compact_errors) > 1800:
                compact_errors = compact_errors[:1797].rstrip() + "..."
            return f"Plan step ended in {terminal_state.value}: {compact_errors}"
        return f"Plan step ended in {terminal_state.value} without additional detail."

    @staticmethod
    def _classify_plan_step_failure(step_task: Task, terminal_state: ProgressState, result_summary: str) -> str:
        text_parts = [
            terminal_state.value,
            result_summary,
            getattr(step_task, "problem_statement", "") or "",
            " ".join(str(error) for error in (getattr(step_task, "errors", None) or [])),
        ]
        haystack = " ".join(text_parts).lower()
        if terminal_state == ProgressState.CANCELLED:
            return "cancelled"
        if any(marker in haystack for marker in ("context window", "context length", "context_limit", "context limit")):
            return "context_pressure"
        if any(marker in haystack for marker in ("tool-call", "tool call", "max_tool", "tool result", "tool budget", "budget exhausted")):
            return "tool_budget_exhausted"
        if any(marker in haystack for marker in ("timeout", "timed out", "subtask_timeout", "watchdog")):
            return "specialist_timeout"
        if any(marker in haystack for marker in ("subtask failed", "delegated task failed", "specialist failed")):
            return "delegated_subtask_failed"
        if any(marker in haystack for marker in ("clarification", "ambiguous", "missing user", "need user", "user input")):
            return "missing_user_input"
        return "transient_runtime_error"

    @staticmethod
    def _resolve_plan_step_failure_policy(
        *,
        failure_type: str,
        target_step: PlanStepRecord,
    ) -> PlanStepFailurePolicy:
        if failure_type in {"context_pressure", "tool_budget_exhausted"}:
            return PlanStepFailurePolicy.START_ISOLATED_RECOVERY
        if failure_type in {"missing_user_input", "cancelled"}:
            return PlanStepFailurePolicy.RESUME_ROOT_FOR_REPLAN
        if int(target_step.attempt_count or 0) < max(int(target_step.max_attempts or 1), 1):
            return PlanStepFailurePolicy.RETRY_STEP
        return PlanStepFailurePolicy.RESUME_ROOT_FOR_REPLAN

    def _build_plan_step_failure_recovery_context(
        self,
        *,
        root_info: TaskInfo,
        step_info: TaskInfo,
        plan: PlanRecord,
        target_step: PlanStepRecord,
        failure_type: str,
        result_summary: str,
    ) -> str:
        completed_steps = [
            step
            for step in sorted(plan.plan_steps, key=lambda item: item.order_index)
            if step.status == ExecutionPlanItemStatus.COMPLETED and step.result_summary
        ]
        recent_history = list(root_info.task.execution_history)[-5:]
        lines = [
            "Original user goal:",
            self.operations.compact_plan_step_text(root_info.task.problem_statement, max_chars=1400) or "N/A",
            "",
            "Failed plan step:",
            f"- Title: {target_step.title}",
            f"- Objective: {target_step.objective}",
            f"- Attempt: {target_step.attempt_count} of {max(target_step.max_attempts, 1)}",
            f"- Failure type: {failure_type}",
            f"- Failure summary: {self.operations.compact_plan_step_text(result_summary, max_chars=1200)}",
        ]
        if completed_steps:
            lines.extend(["", "Completed step outcomes:"])
            for step in completed_steps[-4:]:
                lines.append(f"- {step.title}: {self.operations.compact_plan_step_text(step.result_summary, max_chars=500)}")
        if recent_history:
            lines.extend(["", "Recent root task history:"])
            for entry in recent_history:
                lines.append(f"- {self.operations.compact_plan_step_text(entry.summary, max_chars=500)}")
        step_errors = getattr(step_info.task, "errors", None) or []
        if step_errors:
            lines.extend(["", "Failed step errors:"])
            for error in step_errors[-3:]:
                lines.append(f"- {self.operations.compact_plan_step_text(error, max_chars=700)}")
        return "\n".join(lines)

    def _start_isolated_recovery_for_plan_step_failure(
        self,
        *,
        root_info: TaskInfo,
        step_info: TaskInfo,
        plan: PlanRecord,
        target_step: PlanStepRecord,
        failure_type: str,
        result_summary: str,
        timestamp: str,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[TaskInfo]:
        recovery_task_id = new_id("recovery_task")
        response_route = root_info.header.response_routing_key or root_info.header.from_agent_id or self.identity.owner_agent_id
        recovery_header = root_info.header.model_copy(deep=True)
        recovery_header.from_agent_id = root_info.header.from_agent_id or self.identity.owner_agent_id
        recovery_header.to_routing_path = self.identity.owner_agent_id or recovery_header.to_routing_path
        recovery_header.response_routing_key = response_route

        recovery_meta = root_info.meta.model_copy(deep=True)
        recovery_meta.msg_correlation_id = f"recovery_corr_{recovery_task_id}"
        recovery_meta.conversation_id = new_id("recov_conv")
        recovery_meta.followup_to_task_id = None
        recovery_meta.requester_type = recovery_meta.requester_type or RequesterType.AGENT_REQUESTER
        recovery_meta.is_response_requested = True
        recovery_meta.to_agent_name = self.identity.owner_agent_name
        recovery_meta.to_named_agent_type = self.identity.owner_named_agent_type

        recovery_brief = (
            "Investigate why this internal plan step failed and identify a practical way to continue. "
            "Return a concise recovery result the manager can use to replan, retry safely, ask the user, "
            "or complete with caveats."
        )
        return self.start_isolated_recovery_task(
            root_info.task.task_id,
            failure_type=failure_type,
            failure_summary=result_summary,
            recovery_brief=recovery_brief,
            initial_recovery_context=self._build_plan_step_failure_recovery_context(
                root_info=root_info,
                step_info=step_info,
                plan=plan,
                target_step=target_step,
                failure_type=failure_type,
                result_summary=result_summary,
            ),
            header=recovery_header,
            meta=recovery_meta,
            affected_step_id=target_step.step_id,
            affected_subtask_id=step_info.task.task_id,
            recovery_task_id=recovery_task_id,
            notify_user=True,
            details={"policy": PlanStepFailurePolicy.START_ISOLATED_RECOVERY.value, "source": "plan_step_failure_policy"},
            agent_name=agent_name,
            agent_id=agent_id,
        )

    def merge_plan_step_result_and_continue(
        self,
        step_task_id: str,
        terminal_state: ProgressState,
        timestamp: str,
        *,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        link = self.plan_state.plan_step_task_links.get(step_task_id)
        if link is None:
            return None
        step_info = self.operations.get_task_info(step_task_id)
        if step_info is None:
            self._logger.warning("Cannot merge plan-step task %s because it is no longer active.", step_task_id)
            return None
        root_info = self.operations.get_task_info(link.root_task_id)
        plan = self.get_plan(link.plan_id)
        if root_info is None or plan is None:
            link.status = "skipped_missing_root_or_plan"
            link.completed_at = timestamp
            self.operations.persist_runtime_state()
            return {"step_task_id": step_task_id, "merge_status": link.status}

        target_step = next((step for step in plan.plan_steps if step.step_id == link.step_id), None)
        if target_step is None:
            link.status = "skipped_missing_step"
            link.completed_at = timestamp
            self.operations.persist_runtime_state()
            return {"step_task_id": step_task_id, "merge_status": link.status}

        result_summary = self._build_plan_step_result_summary(step_info.task, terminal_state)
        resumed = False
        next_step_info = None
        recovery_task_started = False
        selected_policy: Optional[PlanStepFailurePolicy] = None

        if "objective_work_session_yield" in list(getattr(step_info.task, "errors", None) or []):
            target_step.status = ExecutionPlanItemStatus.PLANNED
            target_step.status_detail = "Checkpointed at the bounded Objective work-session boundary."
            target_step.current_step_task_id = None
            target_step.failure_type = None
            target_step.failure_summary = None
            target_step.last_failure_policy = None
            target_step.updated_at = timestamp
            plan.current_focus = target_step.title
            plan.updated_at = timestamp
            link.status = "checkpointed_for_next_work_session"
            link.completed_at = timestamp
            self._save_plan(plan)
            resumed = self._resume_checkpointed_plan_root(plan, root_info, timestamp)
            self.operations.persist_runtime_state()
            return {
                "step_task_id": step_task_id,
                "merge_status": link.status,
                "plan_id": plan.plan_id,
                "plan_step_id": target_step.step_id,
                "resumed_root": resumed,
            }

        if terminal_state == ProgressState.COMPLETED:
            target_step.status = ExecutionPlanItemStatus.COMPLETED
            target_step.status_detail = "Internal step task completed."
            target_step.result_summary = result_summary
            target_step.failure_type = None
            target_step.failure_summary = None
            target_step.last_failure_policy = None
            target_step.current_step_task_id = None
            target_step.updated_at = timestamp
            link.status = "completed"
            link.completed_at = timestamp
            plan.current_focus = target_step.title
            plan.updated_at = timestamp
            self._save_plan(plan)

            self.operations.append_history_entry(
                link.root_task_id,
                f"Plan step completed: {target_step.title}. Result: {result_summary}",
                source=TaskHistorySource.SYSTEM,
                entry_type=TaskHistoryType.PLAN_EVENT,
                importance=TaskHistoryImportance.HIGH,
                timestamp=timestamp,
                agent_name=agent_name,
                agent_id=agent_id,
                related_task_id=step_task_id,
                plan_step_id=target_step.step_id,
                tracks_to_plan=True,
                details={"action": "plan_step_result_merged", "terminal_state": terminal_state.value},
            )
            self.record_plan_action(
                link.root_task_id,
                PlanActionType.PLAN_STEP_COMPLETED,
                f"Completed plan step: {target_step.title}",
                status=PlanActionStatus.COMPLETED,
                related_task_id=step_task_id,
                plan_step_id=target_step.step_id,
                user_visible=False,
                details={"terminal_state": terminal_state.value},
                agent_name=agent_name,
                agent_id=agent_id,
                timestamp=timestamp,
            )
            self._record_plan_progress_notice(
                link.root_task_id,
                f"Plan milestone completed: {target_step.title}",
                reason="plan_step_completed",
                related_task_id=step_task_id,
                plan_step_id=target_step.step_id,
                details={"terminal_state": terminal_state.value, "result_summary": result_summary},
                agent_name=agent_name,
                agent_id=agent_id,
                timestamp=timestamp,
            )

            latest_plan = self.get_plan(link.plan_id) or plan
            next_step_info = self._enqueue_next_plan_step_task(
                latest_plan,
                root_task_info=root_info,
                timestamp=timestamp,
            )
            if next_step_info is None:
                resumed = self._resume_checkpointed_plan_root(
                    self.get_plan(link.plan_id) or latest_plan, root_info, timestamp,
                )
        else:
            failure_type = self._classify_plan_step_failure(step_info.task, terminal_state, result_summary)
            target_step.status = ExecutionPlanItemStatus.BLOCKED
            target_step.status_detail = f"Internal step task ended in {terminal_state.value}."
            target_step.result_summary = result_summary
            target_step.failure_type = failure_type
            target_step.failure_summary = result_summary
            selected_policy = self._resolve_plan_step_failure_policy(
                failure_type=failure_type,
                target_step=target_step,
            )
            target_step.last_failure_policy = selected_policy
            target_step.current_step_task_id = None
            target_step.updated_at = timestamp
            link.status = terminal_state.value
            link.completed_at = timestamp
            plan.current_focus = target_step.title
            plan.updated_at = timestamp
            self._save_plan(plan)

            self.operations.append_history_entry(
                link.root_task_id,
                f"Plan step failed: {target_step.title}. Failure type: {failure_type}. Result: {result_summary}",
                source=TaskHistorySource.SYSTEM,
                entry_type=TaskHistoryType.PLAN_EVENT,
                importance=TaskHistoryImportance.HIGH,
                timestamp=timestamp,
                agent_name=agent_name,
                agent_id=agent_id,
                related_task_id=step_task_id,
                plan_step_id=target_step.step_id,
                tracks_to_plan=True,
                details={
                    "action": "plan_step_result_merged",
                    "terminal_state": terminal_state.value,
                    "failure_type": failure_type,
                    "failure_policy": selected_policy.value,
                },
            )
            self.record_plan_action(
                link.root_task_id,
                PlanActionType.PLAN_STEP_COMPLETED,
                f"Failed plan step: {target_step.title}",
                status=PlanActionStatus.FAILED,
                reason=failure_type,
                related_task_id=step_task_id,
                plan_step_id=target_step.step_id,
                user_visible=False,
                details={"terminal_state": terminal_state.value, "failure_type": failure_type},
                agent_name=agent_name,
                agent_id=agent_id,
                timestamp=timestamp,
            )
            self.record_plan_action(
                link.root_task_id,
                PlanActionType.RECORD_FAILURE,
                f"Plan step failure policy selected: {selected_policy.value}",
                status=PlanActionStatus.RECORDED,
                reason=failure_type,
                related_task_id=step_task_id,
                plan_step_id=target_step.step_id,
                user_visible=False,
                details={
                    "terminal_state": terminal_state.value,
                    "failure_type": failure_type,
                    "failure_summary": result_summary,
                    "failure_policy": selected_policy.value,
                    "attempt_count": target_step.attempt_count,
                    "max_attempts": target_step.max_attempts,
                },
                agent_name=agent_name,
                agent_id=agent_id,
                timestamp=timestamp,
            )
            self._record_plan_progress_notice(
                link.root_task_id,
                f"Plan milestone failed: {target_step.title}. Failure type: {failure_type}. Next action: {selected_policy.value}.",
                reason=failure_type,
                related_task_id=step_task_id,
                plan_step_id=target_step.step_id,
                details={
                    "terminal_state": terminal_state.value,
                    "failure_type": failure_type,
                    "failure_policy": selected_policy.value,
                    "attempt_count": target_step.attempt_count,
                    "max_attempts": target_step.max_attempts,
                },
                agent_name=agent_name,
                agent_id=agent_id,
                timestamp=timestamp,
            )

            if selected_policy == PlanStepFailurePolicy.RETRY_STEP:
                latest_plan = self.get_plan(link.plan_id) or plan
                retry_step = next((step for step in latest_plan.plan_steps if step.step_id == link.step_id), target_step)
                retry_step.status = ExecutionPlanItemStatus.PLANNED
                retry_step.status_detail = f"Retrying after {failure_type}."
                retry_step.current_step_task_id = None
                retry_step.updated_at = timestamp
                latest_plan.updated_at = timestamp
                self._save_plan(latest_plan)
                self.operations.update_main_task_state(link.root_task_id, ProgressState.WAITING, timestamp)
                next_step_info = self._enqueue_plan_step_task(
                    latest_plan,
                    retry_step,
                    root_task_info=root_info,
                    timestamp=timestamp,
                )
            elif selected_policy == PlanStepFailurePolicy.START_ISOLATED_RECOVERY:
                self.operations.update_main_task_state(link.root_task_id, ProgressState.WAITING, timestamp)
                recovery_info = self._start_isolated_recovery_for_plan_step_failure(
                    root_info=root_info,
                    step_info=step_info,
                    plan=self.get_plan(link.plan_id) or plan,
                    target_step=target_step,
                    failure_type=failure_type,
                    result_summary=result_summary,
                    timestamp=timestamp,
                    agent_name=agent_name,
                    agent_id=agent_id,
                )
                recovery_task_started = recovery_info is not None
                if recovery_info is None:
                    self.operations.update_main_task_state(link.root_task_id, ProgressState.PARTIAL, timestamp)
                    resumed = self.operations.enqueue_task(link.root_task_id, front=True)
                    self.record_plan_action(
                        link.root_task_id,
                        PlanActionType.RESUME_TASK,
                        "Root task queued to replan because isolated recovery could not be started.",
                        status=PlanActionStatus.COMPLETED if resumed else PlanActionStatus.FAILED,
                        reason="isolated_recovery_start_failed",
                        related_task_id=step_task_id,
                        plan_step_id=target_step.step_id,
                        user_visible=False,
                        details={"resume_enqueued": resumed, "failure_type": failure_type},
                        agent_name=agent_name,
                        agent_id=agent_id,
                        timestamp=timestamp,
                    )
            else:
                self.operations.update_main_task_state(link.root_task_id, ProgressState.PARTIAL, timestamp)
                resumed = self.operations.enqueue_task(link.root_task_id, front=True)

        all_terminal = all(
            step.status in {
                ExecutionPlanItemStatus.COMPLETED,
                ExecutionPlanItemStatus.DROPPED,
            }
            for step in (self.get_plan(link.plan_id) or plan).plan_steps
        )
        if all_terminal:
            latest_terminal_plan = self.get_plan(link.plan_id) or plan
            latest_terminal_plan.active_execution_task_id = None
            latest_terminal_plan.updated_at = timestamp
            self._save_plan(latest_terminal_plan)
        if terminal_state == ProgressState.COMPLETED and next_step_info is None and all_terminal:
            self.operations.update_main_task_state(link.root_task_id, ProgressState.PARTIAL, timestamp)
            resumed = self.operations.enqueue_task(link.root_task_id, front=True)
            self._record_plan_progress_notice(
                link.root_task_id,
                "All runtime plan milestones are complete. Preparing the final response.",
                reason="plan_steps_terminal",
                related_task_id=step_task_id,
                plan_step_id=target_step.step_id,
                details={"all_steps_terminal": True},
                agent_name=agent_name,
                agent_id=agent_id,
                timestamp=timestamp,
            )
            self.record_plan_action(
                link.root_task_id,
                PlanActionType.RESUME_TASK,
                "Root task queued to continue after runtime plan-step execution.",
                status=PlanActionStatus.COMPLETED if resumed else PlanActionStatus.FAILED,
                reason="plan_steps_terminal",
                related_task_id=step_task_id,
                plan_step_id=target_step.step_id,
                user_visible=False,
                details={"resume_enqueued": resumed, "all_steps_terminal": all_terminal},
                agent_name=agent_name,
                agent_id=agent_id,
                timestamp=timestamp,
            )
        elif selected_policy == PlanStepFailurePolicy.RESUME_ROOT_FOR_REPLAN:
            self.record_plan_action(
                link.root_task_id,
                PlanActionType.RESUME_TASK,
                "Root task queued to replan after a failed runtime plan step.",
                status=PlanActionStatus.COMPLETED if resumed else PlanActionStatus.FAILED,
                reason="plan_step_needs_replanning",
                related_task_id=step_task_id,
                plan_step_id=target_step.step_id,
                user_visible=False,
                details={"resume_enqueued": resumed, "all_steps_terminal": all_terminal},
                agent_name=agent_name,
                agent_id=agent_id,
                timestamp=timestamp,
            )

        self.operations.persist_runtime_state()
        return {
            "step_task_id": step_task_id,
            "root_task_id": link.root_task_id,
            "plan_id": link.plan_id,
            "terminal_state": terminal_state.value,
            "merge_status": link.status,
            "next_step_enqueued": next_step_info is not None,
            "recovery_task_started": recovery_task_started,
            "root_resumed": resumed,
            "failure_policy": selected_policy.value if selected_policy else None,
        }

    def set_recovery_checkpoint_schedule_id(
        self,
        recovery_task_id: str,
        checkpoint_schedule_id: str,
    ) -> bool:
        link = self.plan_state.recovery_links.get(recovery_task_id)
        if link is None:
            return False
        link.checkpoint_schedule_id = checkpoint_schedule_id
        self.operations.persist_runtime_state()
        return True

    @staticmethod
    def _compact_answer_text(answer: Any, *, max_chars: int = 1800) -> str:
        if answer is None:
            return ""
        if isinstance(answer, TaskAnswer):
            text = answer.answer
        else:
            text = str(answer)
        text = " ".join(text.split())
        if len(text) > max_chars:
            return text[: max_chars - 3].rstrip() + "..."
        return text

    def _build_recovery_result_summary(self, recovery_task: Task, terminal_state: ProgressState) -> str:
        answer_text = self._compact_answer_text(getattr(recovery_task, "answer", None))
        errors = list(getattr(recovery_task, "errors", None) or [])
        if terminal_state == ProgressState.COMPLETED and answer_text:
            return answer_text
        if terminal_state == ProgressState.COMPLETED:
            return "Recovery completed without a textual recommendation."
        if errors:
            compact_errors = " ".join(str(error).strip() for error in errors if str(error).strip())
            if len(compact_errors) > 1800:
                compact_errors = compact_errors[:1797].rstrip() + "..."
            return f"Recovery ended in {terminal_state.value}: {compact_errors}"
        return f"Recovery ended in {terminal_state.value} without additional detail."

    def _mark_recovery_start_action_terminal(
        self,
        link: RecoveryTaskLink,
        status: PlanActionStatus,
        timestamp: str,
    ) -> None:
        plan = self.get_plan(link.original_plan_id)
        if plan is None:
            return
        changed = False
        for action in plan.plan_actions:
            if (
                action.action_type == PlanActionType.START_ISOLATED_RECOVERY
                and action.recovery_task_id == link.recovery_task_id
                and action.status == PlanActionStatus.ACTIVE
            ):
                action.status = status
                action.updated_at = timestamp
                changed = True
        if not changed:
            return
        plan.updated_at = timestamp
        try:
            self.plan_repository.save_plan(plan)
        except Exception as exc:
            self._logger.error("Failed to update recovery start action status for %s: %s", link.recovery_task_id, exc)

    def merge_recovery_result_and_resume_original(
        self,
        recovery_task_id: str,
        terminal_state: ProgressState,
        timestamp: str,
        *,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Merge a completed isolated recovery result back into the original task.

        The merge stores raw linkage only in runtime-owned metadata/action
        details. Prompt-visible history receives a compact semantic summary.
        """
        link = self.plan_state.recovery_links.get(recovery_task_id)
        if link is None:
            return None

        recovery_info = self.operations.get_task_info(recovery_task_id)
        if recovery_info is None:
            self._logger.warning("Cannot merge recovery task %s because it is no longer active.", recovery_task_id)
            return None

        original_info = self.operations.get_task_info(link.original_task_id)
        original_is_active = original_info is not None
        original_waiting_on_user = bool(
            original_is_active and self.operations.is_waiting_on_task_query(link.original_task_id)
        )
        original_can_resume = bool(
            original_info is not None
            and original_info.state == ProgressState.WAITING
            and not original_waiting_on_user
        )
        result_summary = self._build_recovery_result_summary(recovery_info.task, terminal_state)
        if terminal_state == ProgressState.COMPLETED:
            history_summary = f"Isolated recovery completed. Recovery guidance: {result_summary}"
        else:
            history_summary = f"Isolated recovery did not complete cleanly. Recovery guidance: {result_summary}"

        merge_status = "merged_resumed" if original_can_resume else "merged_not_resumed"
        skip_reason = None
        if not original_is_active:
            merge_status = "skipped_original_not_active"
            skip_reason = "original_task_not_active"
        elif original_waiting_on_user:
            merge_status = "merged_waiting_on_user"
            skip_reason = "original_task_waiting_on_user"
        elif original_info and original_info.state != ProgressState.WAITING:
            skip_reason = f"original_task_state_{original_info.state.value}"

        details = {
            "merge_status": merge_status,
            "terminal_state": terminal_state.value,
            "recovery_task_id": recovery_task_id,
            "original_task_id": link.original_task_id,
            "original_plan_id": link.original_plan_id,
            "branch_id": link.branch_id,
            "failure_type": link.failure_type,
            "failure_summary": link.failure_summary,
            "affected_step_id": link.affected_step_id,
            "affected_subtask_id": link.affected_subtask_id,
            "checkpoint_schedule_id": link.checkpoint_schedule_id,
            "result_summary": result_summary,
            "skip_reason": skip_reason,
        }

        action = self.record_plan_action(
            link.original_task_id,
            PlanActionType.RECOVERY_RESULT_READY,
            "Isolated recovery produced a compact result for the original task.",
            status=PlanActionStatus.COMPLETED if terminal_state == ProgressState.COMPLETED else PlanActionStatus.FAILED,
            reason=link.failure_type,
            related_task_id=link.affected_subtask_id,
            recovery_task_id=recovery_task_id,
            plan_step_id=link.affected_step_id,
            branch_id=link.branch_id,
            user_visible=False,
            details=details,
            agent_name=agent_name,
            agent_id=agent_id,
            timestamp=timestamp,
        )
        self._mark_recovery_start_action_terminal(
            link,
            PlanActionStatus.COMPLETED if terminal_state == ProgressState.COMPLETED else PlanActionStatus.FAILED,
            timestamp,
        )

        if original_info is not None:
            self.operations.append_history_entry(
                link.original_task_id,
                history_summary,
                source=TaskHistorySource.SYSTEM,
                entry_type=TaskHistoryType.PLAN_EVENT,
                importance=TaskHistoryImportance.HIGH,
                timestamp=timestamp,
                agent_name=agent_name,
                agent_id=agent_id,
                tracks_to_plan=True,
                details=details,
            )

        resumed = False
        if original_can_resume:
            self.operations.update_main_task_state(link.original_task_id, ProgressState.PARTIAL, timestamp)
            resumed = self.operations.enqueue_task(link.original_task_id, front=True)
            self.record_plan_action(
                link.original_task_id,
                PlanActionType.RESUME_TASK,
                "Original task queued to continue from the isolated recovery result.",
                status=PlanActionStatus.COMPLETED if resumed else PlanActionStatus.FAILED,
                reason="isolated_recovery_result_ready",
                related_task_id=link.affected_subtask_id,
                recovery_task_id=recovery_task_id,
                plan_step_id=link.affected_step_id,
                branch_id=link.branch_id,
                user_visible=False,
                details={**details, "resume_enqueued": resumed},
                agent_name=agent_name,
                agent_id=agent_id,
                timestamp=timestamp,
            )

        link.merge_status = "resumed" if resumed else merge_status
        link.merged_at = timestamp
        self.operations.persist_runtime_state()
        self._logger.info(
            "Merged isolated recovery result. original_active=%s resumed=%s merge_status=%s",
            original_is_active,
            resumed,
            link.merge_status,
        )
        return {
            "recovery_task_id": recovery_task_id,
            "original_task_id": link.original_task_id,
            "plan_id": link.original_plan_id,
            "terminal_state": terminal_state.value,
            "merge_status": link.merge_status,
            "resumed": resumed,
            "plan_action_id": action.action_id if action else None,
        }

    @staticmethod
    def _build_isolated_recovery_problem_statement(
        *,
        original_task_id: str,
        failure_type: str,
        failure_summary: str,
        recovery_brief: str,
        initial_recovery_context: Optional[str] = None,
        affected_step_id: Optional[str] = None,
        affected_subtask_id: Optional[str] = None,
        branch_id: Optional[str] = None,
    ) -> str:
        lines = [
            "Isolated recovery task.",
            "",
            "Use only this compact recovery brief.",
            "Do not assume the full original task context is available.",
            "Investigate the failure, identify a practical recovery path, and return a concise result.",
            "Runtime-owned metadata links this recovery to the original task; do not ask for or infer transport, task, conversation, or plan identifiers.",
            "",
            f"Failure type: {failure_type}",
            f"Failure summary: {failure_summary}",
        ]
        if initial_recovery_context and initial_recovery_context.strip():
            lines.extend(
                [
                    "",
                    "Initial sanitized recovery context:",
                    initial_recovery_context.strip(),
                ]
            )
        lines.extend(["", "Recovery brief:", recovery_brief.strip()])
        return "\n".join(lines)

    def _isolated_recovery_origin(self, original_task_id: str) -> Optional[tuple[TaskInfo, str]]:
        """The active original task and its plan id, or None (logged) when recovery cannot start.

        The recovery task works for the original task's person, recorded at its
        admission (``TaskInfo.owner_person_id``), so the original must be active.
        """
        original_plan_id = self.get_task_plan_id(original_task_id)
        if not original_plan_id:
            self._logger.warning("Cannot start isolated recovery for task %s without a manager plan.", original_task_id)
            return None
        original_info = self.operations.get_task_info(original_task_id)
        if original_info is None:
            self._logger.warning(
                "Cannot start isolated recovery for a task that is not active. "
                "event=isolated_recovery_original_inactive original_task_id=%s",
                original_task_id,
            )
            return None
        return original_info, original_plan_id

    def start_isolated_recovery_task(
        self,
        original_task_id: str,
        *,
        failure_type: str,
        failure_summary: str,
        recovery_brief: str,
        initial_recovery_context: Optional[str] = None,
        header: MessageHeader,
        meta: MessageMeta,
        affected_step_id: Optional[str] = None,
        affected_subtask_id: Optional[str] = None,
        recovery_task_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        notify_user: bool = True,
        details: Optional[Dict[str, Any]] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[TaskInfo]:
        """
        Queue a fresh-context manager recovery task linked through local plan state.

        The recovery task remains a normal transport `Task` payload. Correlation to
        the original work is kept in manager-owned plan sidecar state and
        `PlanActionRecord`, not by embedding `plan_id` in the task.
        """
        if not self.operations.plans_enabled():
            self._logger.info("Ignoring isolated recovery request because first-class plans are manager-only.")
            return None
        if not failure_type or not failure_type.strip():
            raise ValueError("failure_type is required.")
        if not failure_summary or not failure_summary.strip():
            raise ValueError("failure_summary is required.")
        if not recovery_brief or not recovery_brief.strip():
            raise ValueError("recovery_brief is required.")

        origin = self._isolated_recovery_origin(original_task_id)
        if origin is None:
            return None
        original_info, original_plan_id = origin

        recovery_task_id = recovery_task_id or new_id("recovery_task")
        branch_id = branch_id or new_id("recovery_branch")
        if recovery_task_id in self.task_state.tasks:
            self._logger.warning("Cannot start isolated recovery task %s because it already exists.", recovery_task_id)
            return None

        now_ts = datetime.now(timezone.utc).isoformat()
        recovery_link = RecoveryTaskLink(
            original_task_id=original_task_id,
            original_plan_id=original_plan_id,
            recovery_task_id=recovery_task_id,
            branch_id=branch_id,
            failure_type=failure_type.strip(),
            failure_summary=failure_summary.strip(),
            affected_step_id=affected_step_id or None,
            affected_subtask_id=affected_subtask_id or None,
            created_at=now_ts,
        )
        recovery_task = Task(
            task_id=recovery_task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement=self._build_isolated_recovery_problem_statement(
                original_task_id=original_task_id,
                failure_type=failure_type.strip(),
                failure_summary=failure_summary.strip(),
                recovery_brief=recovery_brief.strip(),
                initial_recovery_context=initial_recovery_context,
                affected_step_id=affected_step_id,
                affected_subtask_id=affected_subtask_id,
                branch_id=branch_id,
            ),
        )
        self.plan_state.task_to_plan_id[recovery_task_id] = original_plan_id
        original_objective_id = self.operations.get_task_objective_id(original_task_id)
        if original_objective_id:
            self.objective_state.task_to_objective_id[recovery_task_id] = original_objective_id
            meta.objective_id = original_objective_id
        self.plan_state.recovery_links[recovery_task_id] = recovery_link
        if not self.operations.add_task(
            recovery_task, header, meta, owner_person_id=original_info.owner_person_id
        ):
            self.plan_state.task_to_plan_id.pop(recovery_task_id, None)
            self.objective_state.task_to_objective_id.pop(recovery_task_id, None)
            self.plan_state.recovery_links.pop(recovery_task_id, None)
            return None
        task_info = self.operations.get_task_info(recovery_task_id)
        if task_info is None:
            self.plan_state.task_to_plan_id.pop(recovery_task_id, None)
            self.objective_state.task_to_objective_id.pop(recovery_task_id, None)
            self.plan_state.recovery_links.pop(recovery_task_id, None)
            return None

        action_details = {
            "failure_type": failure_type.strip(),
            "failure_summary": failure_summary.strip(),
            "initial_recovery_context_included": bool(initial_recovery_context and initial_recovery_context.strip()),
            "affected_step_id": affected_step_id,
            "affected_subtask_id": affected_subtask_id,
            "recovery_task_id": recovery_task_id,
            "branch_id": branch_id,
            "notify_user": bool(notify_user),
            **(details or {}),
        }
        self.record_plan_action(
            original_task_id,
            PlanActionType.START_ISOLATED_RECOVERY,
            f"Started isolated recovery: {failure_summary.strip()}",
            status=PlanActionStatus.ACTIVE,
            reason=failure_type.strip(),
            related_task_id=affected_subtask_id or recovery_task_id,
            recovery_task_id=recovery_task_id,
            plan_step_id=affected_step_id,
            branch_id=branch_id,
            user_visible=bool(notify_user),
            details=action_details,
            agent_name=agent_name,
            agent_id=agent_id,
        )
        if notify_user:
            notice = (
                f"The task hit `{failure_type.strip()}` during execution. "
                "I am starting isolated recovery with a compact recovery brief so "
                "the original task context does not keep growing. You can ask me to stop and return partial "
                "findings at any time."
            )
            self.record_plan_action(
                original_task_id,
                PlanActionType.USER_NOTICE,
                notice,
                status=PlanActionStatus.COMPLETED,
                reason=failure_type.strip(),
                related_task_id=affected_subtask_id or recovery_task_id,
                recovery_task_id=recovery_task_id,
                plan_step_id=affected_step_id,
                branch_id=branch_id,
                user_visible=True,
                details={
                    "failure_type": failure_type.strip(),
                    "failure_summary": failure_summary.strip(),
                    "recovery_task_id": recovery_task_id,
                    "notice_type": "isolated_recovery_started",
                },
                agent_name=agent_name,
                agent_id=agent_id,
            )
        self.operations.append_history_entry(
            recovery_task_id,
            "Isolated recovery task linked to the original task through runtime metadata.",
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.HIGH,
            agent_name=agent_name,
            agent_id=agent_id,
            related_task_id=original_task_id,
            plan_step_id=affected_step_id,
            tracks_to_plan=True,
            details={
                "action": "isolated_recovery_task_created",
                "original_task_id": original_task_id,
                "branch_id": branch_id,
                "failure_type": failure_type.strip(),
            },
        )
        self._sync_plan_from_task(recovery_task_id, create=False)
        self.operations.persist_runtime_state()
        return task_info
