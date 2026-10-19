from custom_agents.task_runtime_state import (
    TaskRuntimeState,
    PlanRuntimeState,
    ObjectiveRuntimeState,
    ExecutionRuntimeState,
    RuntimeOwnerIdentity,
    forget_task_records,
    TurnStartNotes,
    release_turn_start_notes,
)
from custom_agents.task_manager_ports import (
    AfterActionReportCoordinatorOperations,
    ObjectiveChange,
    PlanCoordinatorOperations,
    RuntimeOperationsCoordinatorOperations,
    RuntimeSnapshotCoordinatorOperations,
    ObjectiveCoordinatorOperations,
    ObjectiveWorkSessionCoordinatorOperations,
    SolutionVariationCoordinatorOperations,
)
from custom_agents.objective_work_config import ObjectiveWorkExecutionConfig
from storage.objective_work_repository import ObjectiveWorkRepository
import json
import logging
from collections import deque
from functools import partial
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from custom_messages.contracts.schedule_worker_v1 import ScheduleOccurrenceV1
from storage.task_custody_repository import TaskCustodyConflict, TaskCustodyTakeover

from storage.backends import STORAGE_BACKEND_SQLITE
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_agents.prompt_context_sanitization import (
    extract_display_request_text,
    render_replied_message_lines,
    sanitize_history_summary_for_prompt,
)
from custom_agents.runtime_state_store import RuntimeStateStore, TaskRuntimeSnapshotRow
from custom_agents.task_manager_after_action_reports import AfterActionReportCoordinator
from custom_agents.task_manager_objectives import ObjectiveCoordinator
from custom_agents.objective_work_config import load_objective_work_execution_config
from custom_agents.objective_work_sessions import ObjectiveWorkSessionCoordinator
from custom_agents.solution_variations import SolutionVariationCoordinator
from custom_agents.task_manager_plans import PlanCoordinator
from custom_agents.task_manager_runtime_operations import RuntimeOperationsCoordinator
from custom_agents.task_manager_runtime_control import TaskManagerRuntimeControlMixin
from custom_agents.task_manager_runtime_snapshots import RuntimeSnapshotCoordinator
from custom_agents.task_manager_terminalization import TaskManagerTerminalizationMixin
from custom_messages.contracts.runtime_snapshot_v3 import (
    PlanStepTaskLink,
    RecoverablePauseRecord,
    RecoveryTaskLink,
    RuntimeMailboxEventRecord,
    TaskRuntimeLeaseRecord,
    TurnActionBatchRecord,
    TurnActionOutboxRecord,
)
from custom_agents.task_records import AfterActionReportRecordResult, ReportedWorkIdentity, TaskInfo
from custom_agents.turn_work_notes import note_summary, render_earlier_turn_work, turn_work_notes
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import (
    AfterActionReport,
    ExecutionDigest,
    ExecutionPlanItem,
    ExecutionUnknown,
    Task,
    TaskHistoryEntry,
    TaskQuery,
)
from custom_messages.enums import (
    AfterActionReportReviewStatus,
    ExecutionPlanItemStatus,
    ExecutionUnknownStatus,
    MessageMetaType,
    ObjectiveCompletionPolicy,
    ObjectiveCriterionStatus,
    ObjectiveEventType,
    ObjectiveHorizon,
    ObjectivePriority,
    ObjectiveStatus,
    PlanActionStatus,
    PlanActionType,
    PlanStatus,
    PlanStepFailurePolicy,
    ProgressState,
    RecoverablePauseTrigger,
    TaskActivityStage,
    TaskActivityType,
    TaskHistoryImportance,
    TaskHistorySource,
    TaskHistoryType,
)
from custom_messages.records.objectives import (
    ObjectiveEventRecord,
    ObjectiveRecord,
    ObjectiveSuccessCriterion,
)
from custom_messages.records.plans import PlanActionRecord, PlanRecord, PlanStepRecord
from custom_messages.records.reviews import (
    AfterActionReportReviewBatchRecord,
    AfterActionReportReviewRecord,
)
from custom_messages.records.task_results import TaskResultUpdate
from default_values import STORAGE_DEFAULTS
from id_generation import new_id
from storage.after_action_report_repository import create_after_action_report_repository
from storage.config import StorageSettings, resolve_storage_settings
from storage.runtime_custody import CustodyStore, RuntimeCustodyLost
from storage.durable_owner_scope_repository import (
    create_durable_owner_scope_repository,
)
from storage.objective_repository import ObjectiveView, create_objective_repository
from storage.objective_work_repository import create_objective_work_repository
from storage.plan_repository import create_plan_repository
from storage.solution_variation_repository import create_solution_variation_repository
from storage.task_archive_repository import create_task_archive_repository


def _create_objective_work_runtime(
    *,
    objective_execution_paths: Optional[List[str]],
    storage_settings: StorageSettings,
    logger: logging.Logger,
) -> tuple[ObjectiveWorkExecutionConfig | None, ObjectiveWorkRepository | None]:
    config = load_objective_work_execution_config(
        objective_execution_paths,
        logger=logger,
    )
    repository = (
        create_objective_work_repository(storage_settings, logger=logger)
        if config is not None
        else None
    )
    return config, repository



def _task_row_taken_over(error: BaseException) -> bool:
    """Whether a save failed because another agent took this agent's task row over."""
    return isinstance(error, RuntimeCustodyLost) and error.store is CustodyStore.TASK_RUNTIME

class TaskManager(
    TaskManagerRuntimeControlMixin,
    TaskManagerTerminalizationMixin,
):
    """
    Manages tasks assigned to an agent and subtasks generated by the agent.

    The agent runs one top-level task at a time: its single execution slot is
    ``task_state.current_in_progress_task_id``. The task manager also limits
    how many top-level tasks it tracks at once (``max_queued_top_level_tasks``).
    """

    def __init__(
        self,
        max_queued_top_level_tasks: int = 15,
        queue_warning_threshold: int = 5,
        db_path: str = STORAGE_DEFAULTS.task_history_db_path.value,
        logger: logging.Logger = None,
        storage_settings: Optional[StorageSettings] = None,
        objective_execution_paths: Optional[List[str]] = None,
    ):
        from custom_agents.task_operation_runtime import TaskOperationRuntime

        self.operations_runtime = TaskOperationRuntime()
        self.task_state = TaskRuntimeState()
        self.plan_state = PlanRuntimeState()
        self.objective_state = ObjectiveRuntimeState()
        self.execution_state = ExecutionRuntimeState()
        self.identity = RuntimeOwnerIdentity()
        self._max_queued_top_level_tasks = max_queued_top_level_tasks
        self._queue_warning_threshold = max(0, int(queue_warning_threshold or 0))
        self._queue_backlog_warning_active = False
        self._task_terminal_callbacks: list[
            Callable[[str, ProgressState, str], None]
        ] = []
        # Called for a task another agent took over, before it is dropped
        # (``add_task_release_callback``).
        self._task_release_callbacks: list[Callable[[str], None]] = []
        self._db_path = (
            storage_settings.sqlite_path
            if storage_settings and storage_settings.sqlite_path
            else db_path
        )
        self._logger = logger if logger else logging.getLogger(__name__)
        self._storage_settings = storage_settings or resolve_storage_settings(
            {
                "backend": STORAGE_DEFAULTS.backend.value,
                STORAGE_BACKEND_SQLITE: {"path": db_path},
            }
        )
        self.runtime_state_store = RuntimeStateStore(
            db_path=self._db_path,
            logger=self._logger,
            storage_settings=self._storage_settings,
        )
        self.task_archive_repository = create_task_archive_repository(
            self._storage_settings,
            logger=self._logger,
        )
        self.plan_repository = create_plan_repository(
            self._storage_settings,
            logger=self._logger,
        )
        self.solution_variation_repository = create_solution_variation_repository(
            self._storage_settings,
            logger=self._logger,
        )
        self.objective_repository = create_objective_repository(
            self._storage_settings,
            logger=self._logger,
        )
        (
            self.objective_work_config,
            self.objective_work_repository,
        ) = _create_objective_work_runtime(
            objective_execution_paths=objective_execution_paths,
            storage_settings=self._storage_settings,
            logger=self._logger,
        )
        self.durable_owner_scope_repository = create_durable_owner_scope_repository(
            self._storage_settings,
            logger=self._logger,
        )
        self.after_action_report_repository = create_after_action_report_repository(
            self._storage_settings,
            logger=self._logger,
        )
        self.objective_work_coordinator = ObjectiveWorkSessionCoordinator(
            operations=ObjectiveWorkSessionCoordinatorOperations(
                append_objective_event=self._append_objective_event,
                emit_activity=self._emit_activity,
                enqueue_next_plan_step_task=self._enqueue_next_plan_step_task,
                objectives_enabled=self._objectives_enabled,
                persist_runtime_state=self._persist_runtime_state,
                change_objective=self._change_objective,
                save_plan=self._save_plan,
                durable_manager_scope_id=self.durable_manager_scope_id,
                get_objective=self.get_objective,
                get_plan=self.get_plan,
                get_task_info=self.get_task_info,
            ),
            identity=self.identity,
            plan_state=self.plan_state,
            task_state=self.task_state,
            activity_enabled=lambda: self._activity_emitter is not None,
            logger=self._logger,
            objective_repository=self.objective_repository,
            objective_work_config=self.objective_work_config,
            objective_work_repository=self.objective_work_repository,
        )
        self.plan_coordinator = PlanCoordinator(
            operations=PlanCoordinatorOperations(
                append_objective_event=self._append_objective_event,
                build_plan_step_problem_statement=self._build_plan_step_problem_statement,
                compact_plan_step_text=self._compact_plan_step_text,
                emit_activity=self._emit_activity,
                ensure_execution_digest=self._ensure_execution_digest,
                get_objective_for_plan_id=self._get_objective_for_plan_id,
                get_task_objective_record=self._get_task_objective_record,
                get_top_level_task_id=self._get_top_level_task_id,
                new_plan_id=partial(new_id, "plan_id"),
                persist_runtime_state=self._persist_runtime_state,
                plans_enabled=self._plans_enabled,
                change_objective=self._change_objective,
                add_task=self.add_task,
                append_history_entry=self.append_history_entry,
                append_history_entry_to_archived_task=self.append_history_entry_to_archived_task,
                durable_manager_scope_id=self.durable_manager_scope_id,
                enqueue_task=self.enqueue_task,
                get_execution_digest=self.get_execution_digest,
                get_task_from_db=self.get_task_from_db,
                get_task_info=self.get_task_info,
                get_task_objective_id=self.get_task_objective_id,
                is_waiting_on_task_query=self.is_waiting_on_task_query,
                update_main_task_state=self.update_main_task_state,
            ),
            identity=self.identity,
            objective_state=self.objective_state,
            plan_state=self.plan_state,
            task_state=self.task_state,
            logger=self._logger,
            objective_work_coordinator=self.objective_work_coordinator,
            plan_repository=self.plan_repository,
        )
        self.objective_coordinator = ObjectiveCoordinator(
            operations=ObjectiveCoordinatorOperations(
                emit_activity=self._emit_activity,
                new_objective_id=partial(new_id, "objective_id"),
                objectives_enabled=self._objectives_enabled,
                persist_runtime_state=self._persist_runtime_state,
                save_plan=self._save_plan,
                sync_plan_from_task=self._sync_plan_from_task,
                add_task=self.add_task,
                append_history_entry=self.append_history_entry,
                durable_manager_scope_id=self.durable_manager_scope_id,
                fail_task=self.fail_task,
                get_plan=self.get_plan,
                get_task_from_db=self.get_task_from_db,
                get_task_info=self.get_task_info,
                get_task_plan_id=self.get_task_plan_id,
                is_terminal_state=self.is_terminal_state,
                set_task_plan=self.set_task_plan,
            ),
            identity=self.identity,
            objective_state=self.objective_state,
            plan_state=self.plan_state,
            task_state=self.task_state,
            logger=self._logger,
            durable_owner_scope_repository=self.durable_owner_scope_repository,
            objective_repository=self.objective_repository,
            objective_work_repository=self.objective_work_repository,
            plan_coordinator=self.plan_coordinator,
        )
        self.solution_variation_coordinator = SolutionVariationCoordinator(
            operations=SolutionVariationCoordinatorOperations(
                get_top_level_task_id=self._get_top_level_task_id,
                plans_enabled=self._plans_enabled,
                append_history_entry=self.append_history_entry,
                append_history_entry_to_archived_task=self.append_history_entry_to_archived_task,
                durable_manager_scope_id=self.durable_manager_scope_id,
                get_plan=self.get_plan,
                get_task_info=self.get_task_info,
                get_task_objective_id=self.get_task_objective_id,
                get_task_plan_id=self.get_task_plan_id,
            ),
            identity=self.identity,
            plan_state=self.plan_state,
            logger=self._logger,
            solution_variation_repository=self.solution_variation_repository,
        )
        self.after_action_report_coordinator = AfterActionReportCoordinator(
            operations=AfterActionReportCoordinatorOperations(
                persist_runtime_state=self._persist_runtime_state,
                append_history_entry=self.append_history_entry,
                append_history_entry_to_archived_task=self.append_history_entry_to_archived_task,
                get_task_from_db=self.get_task_from_db,
                get_task_info=self.get_task_info,
            ),
            identity=self.identity,
            logger=self._logger,
            after_action_report_repository=self.after_action_report_repository,
        )
        self.runtime_operations_coordinator = RuntimeOperationsCoordinator(
            operations=RuntimeOperationsCoordinatorOperations(
                persist_runtime_state=self._persist_runtime_state,
                append_history_entry=self.append_history_entry,
                get_pending_recoverable_pause=self.get_pending_recoverable_pause,
                get_task_info=self.get_task_info,
                get_task_plan_id=self.get_task_plan_id,
                has_pending_subtasks=self.has_pending_subtasks,
                is_final_review_pending=self.is_final_review_pending,
                is_plan_execution_root_active=self.is_plan_execution_root_active,
                is_terminal_state=self.is_terminal_state,
                is_waiting_on_task_query=self.is_waiting_on_task_query,
                update_main_task_state=self.update_main_task_state,
            ),
            execution_state=self.execution_state,
            plan_state=self.plan_state,
            task_state=self.task_state,
            logger=self._logger,
        )
        self.runtime_snapshot_coordinator = RuntimeSnapshotCoordinator(
            operations=RuntimeSnapshotCoordinatorOperations(
                emit_activity=self._emit_activity,
                ensure_task_plan_link=self._ensure_task_plan_link,
                forget_task=self._forget_task,
                notify_task_released=self._run_task_release_callbacks,
                objectives_enabled=self._objectives_enabled,
                plans_enabled=self._plans_enabled,
                sync_plan_from_task=self._sync_plan_from_task,
                is_execution_cancelled=self.is_execution_cancelled,
                get_task_from_db=self.get_task_from_db,
            ),
            execution_state=self.execution_state,
            identity=self.identity,
            objective_state=self.objective_state,
            plan_state=self.plan_state,
            task_state=self.task_state,
            logger=self._logger,
            runtime_state_store=self.runtime_state_store,
        )
        self._activity_emitter: Optional[Callable[..., None]] = None

    @property
    def storage_settings(self) -> StorageSettings:
        return self._storage_settings

    def update_task_result(
        self, task_id: str, update: TaskResultUpdate
    ) -> Optional[TaskInfo]:
        """Commit response fields while retaining terminal cancellation authority."""
        info = self.get_task_info(task_id)
        if info is None or self.is_execution_cancelled(task_id):
            return None
        previous = info.task
        info.task = previous.model_copy(
            update={name: getattr(update, name) for name in update.model_fields_set},
            deep=True,
        )
        try:
            if not self.runtime_snapshot_coordinator.persist_task_history(task_id):
                info.task = previous
                return None
        except Exception:
            info.task = previous
            raise
        return info

    @property
    def current_task_id(self) -> Optional[str]:
        """Expose the current task without granting callers the queue's mutable state."""
        return self.task_state.current_in_progress_task_id

    def list_task_infos(self) -> list[TaskInfo]:
        """List owned task records; the runtime detaches them at its boundary."""
        return list(self.task_state.tasks.values())

    def set_owner_identity(
        self,
        agent_id: str,
        agent_name: Optional[str] = None,
        team_id: Optional[str] = None,
        named_agent_type: Optional[str] = None,
    ) -> None:
        """Attach the owning agent identity used for archived task scoping."""
        if self.task_state.tasks and self.identity.owner_agent_id != agent_id:
            raise ValueError(
                "A populated TaskManager cannot change owner identity; use snapshot recovery to transfer custody"
            )
        self.identity.owner_agent_id = agent_id
        self.identity.owner_agent_name = agent_name
        self.identity.team_id = team_id
        self.identity.owner_named_agent_type = named_agent_type
        if team_id and named_agent_type == TEAM_ARCHITECT_AGENT_TYPE:
            durable_owner_agent_id = self.durable_manager_scope_id()
            if durable_owner_agent_id and durable_owner_agent_id != agent_id:
                try:
                    reconciliation = (
                        self.durable_owner_scope_repository.reconcile_manager_scope(
                            team_id=team_id,
                            named_agent_type=named_agent_type,
                            durable_owner_agent_id=durable_owner_agent_id,
                            include_objective_work=(
                                self.objective_work_repository is not None
                            ),
                        )
                    )
                except Exception:
                    self._logger.exception(
                        "Failed to reconcile durable TeamArchitect workflow ownership. "
                        "event=durable_manager_scope_reconcile_failed team_id=%s "
                        "named_agent_type=%s durable_owner_agent_id=%s runtime_agent_id=%s",
                        team_id,
                        named_agent_type,
                        durable_owner_agent_id,
                        agent_id,
                    )
                    raise
                if reconciliation.total_records:
                    self._logger.info(
                        "Reconciled durable TeamArchitect workflow ownership. "
                        "event=durable_manager_scope_reconciled team_id=%s "
                        "named_agent_type=%s durable_owner_agent_id=%s "
                        "prior_owner_agent_ids=%s objectives=%d plans=%d policies=%d "
                        "sessions=%d checkpoints=%d profile_proposals=%d "
                        "installed_profiles=%d archived_conflicts=%d",
                        team_id,
                        named_agent_type,
                        durable_owner_agent_id,
                        list(reconciliation.prior_owner_agent_ids),
                        reconciliation.objectives,
                        reconciliation.plans,
                        reconciliation.policies,
                        reconciliation.sessions,
                        reconciliation.checkpoints,
                        reconciliation.profile_proposals,
                        reconciliation.installed_profiles,
                        reconciliation.archived_conflicts,
                    )
        if self._plans_enabled():
            for task_id in list(self.task_state.tasks.keys()):
                self._get_task_objective_id_from_context(task_id)
                self._sync_plan_from_task(task_id, create=False)
        if self._activity_emitter is not None:
            self.objective_work_coordinator.publish_inventory()

    def set_activity_emitter(self, emitter: Optional[Callable[..., None]]) -> None:
        """Attach a best-effort activity emitter used for collector/dashboard updates."""
        self._activity_emitter = emitter
        if emitter is not None:
            self.objective_work_coordinator.publish_inventory()

    def _get_top_level_task_id(self, task_id: Optional[str]) -> Optional[str]:
        current = task_id
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            parent = self.task_state.subtask_to_parent_map.get(current)
            if not parent:
                return current
            current = parent
        return task_id

    def _emit_activity(
        self,
        *,
        activity_type: TaskActivityType,
        stage: TaskActivityStage,
        summary: str,
        detail: Optional[str] = None,
        task_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        top_level_task_id: Optional[str] = None,
        meta: Optional[MessageMeta] = None,
        user_visible: bool = True,
        details: Optional[Dict[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> None:
        if self._activity_emitter is None:
            return
        try:
            task_info = self.get_task_info(task_id) if task_id else None
            effective_meta = meta or (task_info.meta if task_info else None)
            effective_parent_task_id = parent_task_id
            if effective_parent_task_id is None and task_info is not None:
                effective_parent_task_id = task_info.task.parent_task_id
            effective_top_level_task_id = (
                top_level_task_id or self._get_top_level_task_id(task_id)
            )
            objective_context = self._objective_activity_context(
                task_id or effective_top_level_task_id
            )
            self._activity_emitter(
                activity_type=activity_type,
                stage=stage,
                summary=summary,
                detail=detail,
                task_id=task_id,
                parent_task_id=effective_parent_task_id,
                top_level_task_id=effective_top_level_task_id,
                objective_id=objective_context["objective_id"],
                objective_title=objective_context["objective_title"],
                meta=effective_meta,
                user_visible=user_visible,
                details=details or {},
                created_at=created_at,
            )
        except Exception as exc:
            self._logger.warning(
                "Failed to emit task activity for task %s: %s", task_id, exc
            )

    @staticmethod
    def is_terminal_state(state: ProgressState) -> bool:
        return state in {
            ProgressState.COMPLETED,
            ProgressState.FAILED,
            ProgressState.CANCELLED,
            ProgressState.ABANDONED,
        }

    @staticmethod
    def is_executing_state(state: ProgressState) -> bool:
        return state in {
            ProgressState.FINALIZING_PLAN,
            ProgressState.IN_PROGRESS,
            ProgressState.PREPARING,
            ProgressState.RETRYING,
        }

    @staticmethod
    def is_blocked_state(state: ProgressState) -> bool:
        return state == ProgressState.WAITING

    @staticmethod
    def is_runnable_state(state: ProgressState) -> bool:
        return state in {
            ProgressState.ACCEPTED,
            ProgressState.NOT_STARTED,
            ProgressState.PARTIAL,
            ProgressState.UNDEFINED,
        }

    def _build_archive_id(self, task_id: str) -> str:
        owner_agent_id = self.identity.owner_agent_id or "unknown_agent"
        return f"{owner_agent_id}::{task_id}"

    def _plans_enabled(self) -> bool:
        return self.identity.owner_named_agent_type == TEAM_ARCHITECT_AGENT_TYPE

    def _objectives_enabled(self) -> bool:
        return self._plans_enabled()

    def _change_objective(
        self, objective_id: str, change: ObjectiveChange, *, step: str
    ) -> Optional[ObjectiveRecord]:
        return self.objective_coordinator.change_objective(objective_id, change, step=step)

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
        return self.objective_coordinator._append_objective_event(
            objective,
            event_type,
            summary,
            task_id=task_id,
            plan_id=plan_id,
            schedule_id=schedule_id,
            heartbeat_item_id=heartbeat_item_id,
            user_visible=user_visible,
            details=details,
            timestamp=timestamp,
        )

    def _get_objective_record(
        self, objective_id: Optional[str]
    ) -> Optional[ObjectiveRecord]:
        return self.objective_coordinator._get_objective_record(objective_id)

    def _get_objective_for_plan_id(
        self, plan_id: Optional[str]
    ) -> Optional[ObjectiveRecord]:
        return self.objective_coordinator._get_objective_for_plan_id(plan_id)

    def _get_task_objective_id_from_context(
        self, task_id: Optional[str]
    ) -> Optional[str]:
        return self.objective_coordinator._get_task_objective_id_from_context(task_id)

    def _get_task_objective_record(
        self, task_id: Optional[str]
    ) -> Optional[ObjectiveRecord]:
        return self.objective_coordinator._get_task_objective_record(task_id)

    def _objective_activity_context(
        self, task_id: Optional[str], plan_id: Optional[str] = None
    ) -> Dict[str, Optional[str]]:
        return self.objective_coordinator._objective_activity_context(
            task_id, plan_id=plan_id
        )

    def _ensure_task_plan_link(
        self,
        task_info: TaskInfo,
        parent_task_info: Optional[TaskInfo] = None,
        *,
        create: bool = False,
    ) -> Optional[str]:
        return self.plan_coordinator._ensure_task_plan_link(
            task_info,
            parent_task_info,
            create=create,
        )

    @staticmethod
    def _plan_status_for_progress_state(
        state: Optional[ProgressState],
    ) -> Optional[PlanStatus]:
        return PlanCoordinator._plan_status_for_progress_state(state)

    def _related_task_ids_for_plan(
        self, plan_id: str, existing: Optional[PlanRecord] = None
    ) -> List[str]:
        return self.plan_coordinator._related_task_ids_for_plan(plan_id, existing)

    def _sync_plan_from_task(
        self, task_id: str, *, create: bool = False
    ) -> Optional[PlanRecord]:
        return self.plan_coordinator._sync_plan_from_task(task_id, create=create)

    def get_plan(self, plan_id: str) -> Optional[PlanRecord]:
        return self.plan_coordinator.get_plan(plan_id)

    def get_task_plan_id(self, task_id: str) -> Optional[str]:
        return self.plan_coordinator.get_task_plan_id(task_id)

    @staticmethod
    def _coerce_objective_status(status: ObjectiveStatus | str) -> ObjectiveStatus:
        return ObjectiveCoordinator._coerce_objective_status(status)

    @staticmethod
    def _coerce_objective_horizon(horizon: ObjectiveHorizon | str) -> ObjectiveHorizon:
        return ObjectiveCoordinator._coerce_objective_horizon(horizon)

    @staticmethod
    def _coerce_objective_priority(
        priority: ObjectivePriority | str,
    ) -> ObjectivePriority:
        return ObjectiveCoordinator._coerce_objective_priority(priority)

    @staticmethod
    def _coerce_objective_completion_policy(
        completion_policy: ObjectiveCompletionPolicy | str,
    ) -> ObjectiveCompletionPolicy:
        return ObjectiveCoordinator._coerce_objective_completion_policy(
            completion_policy
        )

    @staticmethod
    def _coerce_objective_criterion_status(
        status: ObjectiveCriterionStatus | str,
    ) -> ObjectiveCriterionStatus:
        return ObjectiveCoordinator._coerce_objective_criterion_status(status)

    @staticmethod
    def _build_objective_success_criteria(
        success_criteria: Optional[List[Any]],
    ) -> List[ObjectiveSuccessCriterion]:
        return ObjectiveCoordinator._build_objective_success_criteria(success_criteria)

    def get_objective(self, objective_id: str) -> Optional[ObjectiveRecord]:
        return self.objective_coordinator.get_objective(objective_id)

    def get_user_owned_objective(
        self,
        objective_id: str,
        *,
        owner_canonical_user_id: str,
        team_id: str,
        adopt_live_custody: bool = True,
    ) -> Optional[ObjectiveRecord]:
        return self.objective_coordinator.get_user_owned_objective(
            objective_id,
            owner_canonical_user_id=owner_canonical_user_id,
            team_id=team_id,
            adopt_live_custody=adopt_live_custody,
        )

    def list_user_owned_objectives(
        self,
        *,
        owner_canonical_user_id: str,
        team_id: str,
        limit: int = 100,
        include_closed: bool = False,
    ) -> List[ObjectiveRecord]:
        return self.objective_coordinator.list_user_owned_objectives(
            owner_canonical_user_id=owner_canonical_user_id,
            team_id=team_id,
            limit=limit,
            include_closed=include_closed,
        )

    def get_task_objective_id(self, task_id: str) -> Optional[str]:
        return self.objective_coordinator.get_task_objective_id(task_id)

    def get_plan_objective_id(self, plan_id: str) -> Optional[str]:
        return self.objective_coordinator.get_plan_objective_id(plan_id)

    def list_objectives(
        self,
        *,
        view: ObjectiveView,
        limit: int = 20,
        statuses: Optional[List[str]] = None,
        horizons: Optional[List[str]] = None,
        include_closed: bool = False,
    ):
        return self.objective_coordinator.list_objectives(
            view=view,
            limit=limit,
            statuses=statuses,
            horizons=horizons,
            include_closed=include_closed,
        )

    def link_schedule_to_objective(
        self,
        objective_id: str,
        schedule_id: str,
        *,
        managed_by_objective: bool = True,
        user_visible: bool = False,
        reason: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        return self.objective_coordinator.link_schedule_to_objective(
            objective_id,
            schedule_id,
            managed_by_objective=managed_by_objective,
            user_visible=user_visible,
            reason=reason,
        )

    def link_heartbeat_to_objective(
        self,
        objective_id: str,
        heartbeat_item_id: str,
        *,
        managed_by_objective: bool = True,
        user_visible: bool = False,
        reason: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        return self.objective_coordinator.link_heartbeat_to_objective(
            objective_id,
            heartbeat_item_id,
            managed_by_objective=managed_by_objective,
            user_visible=user_visible,
            reason=reason,
        )

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
        completion_policy: ObjectiveCompletionPolicy
        | str = ObjectiveCompletionPolicy.MANAGER_DECIDES,
        success_criteria: Optional[List[Any]] = None,
        constraints: Optional[List[str]] = None,
        non_goals: Optional[List[str]] = None,
        requires_user_confirmation: bool = False,
        current_focus: Optional[str] = None,
        progress_summary: Optional[str] = None,
        root_conversation_id: Optional[str] = None,
        user_visible: bool = True,
    ) -> Optional[ObjectiveRecord]:
        return self.objective_coordinator.create_objective(
            title=title,
            desired_outcome=desired_outcome,
            why=why,
            task_id=task_id,
            creation_mode=creation_mode,
            horizon=horizon,
            priority=priority,
            completion_policy=completion_policy,
            success_criteria=success_criteria,
            constraints=constraints,
            non_goals=non_goals,
            requires_user_confirmation=requires_user_confirmation,
            current_focus=current_focus,
            progress_summary=progress_summary,
            root_conversation_id=root_conversation_id,
            user_visible=user_visible,
        )

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
        return self.objective_coordinator.update_objective_progress(
            objective_id,
            progress_summary=progress_summary,
            current_focus=current_focus,
            criterion_updates=criterion_updates,
            task_id=task_id,
            plan_id=plan_id,
            user_visible=user_visible,
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
        deletion_operation_id: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        return self.objective_coordinator.update_objective_status(
            objective_id,
            status,
            summary=summary,
            user_confirmation_summary=user_confirmation_summary,
            satisfied_by_task_id=satisfied_by_task_id,
            task_id=task_id,
            plan_id=plan_id,
            user_visible=user_visible,
            deletion_operation_id=deletion_operation_id,
        )

    def review_objective_after_task_terminal(
        self,
        task_id: str,
        terminal_state: ProgressState | str,
        *,
        objective_id: Optional[str],
        timestamp: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        return self.objective_coordinator.review_objective_after_task_terminal(
            task_id,
            terminal_state,
            objective_id=objective_id,
            timestamp=timestamp,
            summary=summary,
        )

    def objective_id_for_task_end(self, task_id: str) -> Optional[str]:
        return self.objective_coordinator.objective_id_for_task_end(task_id)

    def render_objective_markdown(
        self, objective_id: str, *, max_events: int = 8
    ) -> str:
        return self.objective_coordinator.render_objective_markdown(
            objective_id, max_events=max_events
        )

    @staticmethod
    def _plan_action_importance(
        action_type: PlanActionType, user_visible: bool
    ) -> TaskHistoryImportance:
        return PlanCoordinator._plan_action_importance(action_type, user_visible)

    def get_plan_actions(
        self, task_id: str, *, limit: int = 10
    ) -> List[PlanActionRecord]:
        return self.plan_coordinator.get_plan_actions(task_id, limit=limit)

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
        return self.plan_coordinator.record_plan_action(
            task_id,
            action_type,
            summary,
            status=status,
            reason=reason,
            related_task_id=related_task_id,
            recovery_task_id=recovery_task_id,
            plan_step_id=plan_step_id,
            branch_id=branch_id,
            user_visible=user_visible,
            details=details,
            agent_name=agent_name,
            agent_id=agent_id,
            timestamp=timestamp,
        )

    def is_isolated_recovery_task(self, task_id: str) -> bool:
        return self.plan_coordinator.is_isolated_recovery_task(task_id)

    def get_recovery_link(self, recovery_task_id: str) -> Optional[RecoveryTaskLink]:
        return self.plan_coordinator.get_recovery_link(recovery_task_id)

    def is_plan_step_task(self, task_id: str) -> bool:
        return self.plan_coordinator.is_plan_step_task(task_id)

    def get_plan_step_task_link(self, step_task_id: str) -> Optional[PlanStepTaskLink]:
        return self.plan_coordinator.get_plan_step_task_link(step_task_id)

    def is_plan_execution_root_active(self, root_task_id: str) -> bool:
        return self.plan_coordinator.is_plan_execution_root_active(root_task_id)

    def is_final_review_pending(self, task_id: str) -> bool:
        return self.plan_coordinator.is_final_review_pending(task_id)

    def _collect_unresolved_final_review_items(self, task_id: str) -> Dict[str, Any]:
        return self.plan_coordinator._collect_unresolved_final_review_items(task_id)

    @staticmethod
    def _has_unresolved_final_review_items(items: Dict[str, Any]) -> bool:
        return PlanCoordinator._has_unresolved_final_review_items(items)

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
        return self.plan_coordinator.begin_final_review_if_needed(
            task_id,
            attempted_answer=attempted_answer,
            max_attempts=max_attempts,
            agent_name=agent_name,
            agent_id=agent_id,
            timestamp=timestamp,
        )

    @staticmethod
    def _compact_plan_step_text(value: Any, *, max_chars: int = 1200) -> str:
        text = " ".join(str(value or "").split())
        if len(text) > max_chars:
            return text[: max_chars - 3].rstrip() + "..."
        return text

    @staticmethod
    def _build_plan_step_problem_statement(
        *,
        root_problem_statement: Optional[str],
        step: PlanStepRecord,
        plan_steps: List[PlanStepRecord],
    ) -> str:
        lines = [
            "Internal manager plan-step task.",
            "",
            "Execute only the current plan step. Use normal task execution behavior for this bounded step.",
            "You may delegate to specialists or update semantic plan notes when useful, but do not invent, request, or manage runtime ids.",
            "Runtime metadata links this step to the root user task and plan.",
            "",
            "Root user goal:",
            TaskManager._compact_plan_step_text(root_problem_statement, max_chars=1600)
            or "N/A",
            "",
            "Current step:",
            f"- Title: {step.title}",
            f"- Objective: {step.objective}",
        ]
        if step.rationale:
            lines.append(f"- Rationale: {step.rationale}")
        if step.failure_summary:
            lines.extend(
                [
                    "",
                    "Previous attempt context:",
                    f"- Failure type: {step.failure_type or 'unknown'}",
                    f"- Failure summary: {TaskManager._compact_plan_step_text(step.failure_summary, max_chars=900)}",
                    f"- Retry attempt: {step.attempt_count + 1} of {max(step.max_attempts, 1)}",
                ]
            )

        lines.extend(["", "Plan state so far:"])
        for idx, item in enumerate(plan_steps, start=1):
            marker = "current" if item.step_id == step.step_id else item.status.value
            lines.append(f"{idx}. [{marker}] {item.title}")
            if item.result_summary:
                lines.append(
                    f"   Result: {TaskManager._compact_plan_step_text(item.result_summary, max_chars=900)}"
                )
        lines.extend(
            [
                "",
                "Return a normal Task response for this internal step when the step objective is satisfied.",
                "The runtime will merge your compact result into the plan and decide the next step.",
            ]
        )
        return "\n".join(lines)

    def _save_plan(self, plan: PlanRecord) -> bool:
        return self.plan_coordinator._save_plan(plan)

    def _next_runnable_plan_step(self, plan: PlanRecord) -> Optional[PlanStepRecord]:
        return self.plan_coordinator._next_runnable_plan_step(plan)

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
        return self.plan_coordinator._record_plan_progress_notice(
            root_task_id,
            summary,
            reason=reason,
            related_task_id=related_task_id,
            recovery_task_id=recovery_task_id,
            plan_step_id=plan_step_id,
            branch_id=branch_id,
            details=details,
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
        return self.plan_coordinator._enqueue_plan_step_task(
            plan,
            next_step,
            root_task_info=root_task_info,
            timestamp=timestamp,
        )

    def _enqueue_next_plan_step_task(
        self,
        plan: PlanRecord,
        *,
        root_task_info: TaskInfo,
        timestamp: Optional[str] = None,
    ) -> Optional[TaskInfo]:
        return self.plan_coordinator._enqueue_next_plan_step_task(
            plan,
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
        return self.plan_coordinator.activate_plan_execution(
            root_task_id,
            steps=steps,
            final_review=final_review,
            agent_name=agent_name,
            agent_id=agent_id,
        )

    def _build_plan_step_result_summary(
        self, step_task: Task, terminal_state: ProgressState
    ) -> str:
        return self.plan_coordinator._build_plan_step_result_summary(
            step_task, terminal_state
        )

    @staticmethod
    def _classify_plan_step_failure(
        step_task: Task, terminal_state: ProgressState, result_summary: str
    ) -> str:
        return PlanCoordinator._classify_plan_step_failure(
            step_task,
            terminal_state,
            result_summary,
        )

    @staticmethod
    def _resolve_plan_step_failure_policy(
        *,
        failure_type: str,
        target_step: PlanStepRecord,
    ) -> PlanStepFailurePolicy:
        return PlanCoordinator._resolve_plan_step_failure_policy(
            failure_type=failure_type, target_step=target_step
        )

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
        return self.plan_coordinator._build_plan_step_failure_recovery_context(
            root_info=root_info,
            step_info=step_info,
            plan=plan,
            target_step=target_step,
            failure_type=failure_type,
            result_summary=result_summary,
        )

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
        return self.plan_coordinator._start_isolated_recovery_for_plan_step_failure(
            root_info=root_info,
            step_info=step_info,
            plan=plan,
            target_step=target_step,
            failure_type=failure_type,
            result_summary=result_summary,
            timestamp=timestamp,
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
        return self.plan_coordinator.merge_plan_step_result_and_continue(
            step_task_id,
            terminal_state,
            timestamp,
            agent_name=agent_name,
            agent_id=agent_id,
        )

    def set_recovery_checkpoint_schedule_id(
        self,
        recovery_task_id: str,
        checkpoint_schedule_id: str,
    ) -> bool:
        return self.plan_coordinator.set_recovery_checkpoint_schedule_id(
            recovery_task_id, checkpoint_schedule_id
        )

    @staticmethod
    def _compact_answer_text(answer: Any, *, max_chars: int = 1800) -> str:
        return PlanCoordinator._compact_answer_text(answer, max_chars=max_chars)

    def _build_recovery_result_summary(
        self, recovery_task: Task, terminal_state: ProgressState
    ) -> str:
        return self.plan_coordinator._build_recovery_result_summary(
            recovery_task, terminal_state
        )

    def _mark_recovery_start_action_terminal(
        self,
        link: RecoveryTaskLink,
        status: PlanActionStatus,
        timestamp: str,
    ) -> None:
        return self.plan_coordinator._mark_recovery_start_action_terminal(
            link,
            status,
            timestamp,
        )

    def merge_recovery_result_and_resume_original(
        self,
        recovery_task_id: str,
        terminal_state: ProgressState,
        timestamp: str,
        *,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return self.plan_coordinator.merge_recovery_result_and_resume_original(
            recovery_task_id,
            terminal_state,
            timestamp,
            agent_name=agent_name,
            agent_id=agent_id,
        )

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
        return PlanCoordinator._build_isolated_recovery_problem_statement(
            original_task_id=original_task_id,
            failure_type=failure_type,
            failure_summary=failure_summary,
            recovery_brief=recovery_brief,
            initial_recovery_context=initial_recovery_context,
            affected_step_id=affected_step_id,
            affected_subtask_id=affected_subtask_id,
            branch_id=branch_id,
        )

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
        return self.plan_coordinator.start_isolated_recovery_task(
            original_task_id,
            failure_type=failure_type,
            failure_summary=failure_summary,
            recovery_brief=recovery_brief,
            initial_recovery_context=initial_recovery_context,
            header=header,
            meta=meta,
            affected_step_id=affected_step_id,
            affected_subtask_id=affected_subtask_id,
            recovery_task_id=recovery_task_id,
            branch_id=branch_id,
            notify_user=notify_user,
            details=details,
            agent_name=agent_name,
            agent_id=agent_id,
        )

    def create_turn_action_batch(
        self,
        source_task_id: str,
        actions: List[Dict[str, Any]],
        *,
        plan_id: Optional[str] = None,
        plan_step_id: Optional[str] = None,
        release_notes: Optional[TurnStartNotes] = None,
    ) -> Optional[TurnActionBatchRecord]:
        """Create durable outbox records for one ordered runtime-action batch.

        ``release_notes`` (what the deciding turn read) are released in the same
        snapshot save as the batch (``TurnStartNotes``).
        """
        return self.runtime_operations_coordinator.create_turn_action_batch(
            source_task_id,
            actions,
            release_notes=release_notes,
            plan_id=plan_id,
            plan_step_id=plan_step_id,
        )

    def get_turn_action_batch(self, batch_id: str) -> Optional[TurnActionBatchRecord]:
        return self.runtime_operations_coordinator.get_turn_action_batch(batch_id)

    def get_turn_action_record(
        self, action_id: str
    ) -> Optional[TurnActionOutboxRecord]:
        return self.runtime_operations_coordinator.get_turn_action_record(action_id)

    def list_turn_action_records(self, batch_id: str) -> List[TurnActionOutboxRecord]:
        return self.runtime_operations_coordinator.list_turn_action_records(batch_id)

    def mark_turn_action_batch_status(
        self,
        batch_id: str,
        status: str,
        *,
        current_action_index: Optional[int] = None,
        increment_inline_recovery: bool = False,
    ) -> None:
        self.runtime_operations_coordinator.mark_turn_action_batch_status(
            batch_id,
            status,
            current_action_index=current_action_index,
            increment_inline_recovery=increment_inline_recovery,
        )

    def mark_turn_action_record(
        self,
        action_id: str,
        *,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        recoverability: Optional[str] = None,
        target_route: Optional[str] = None,
        message_id: Optional[str] = None,
        side_effects: Optional[Dict[str, Any]] = None,
        next_retry_at: Optional[str] = None,
        last_error: Optional[str] = None,
        failure_report: Optional[Dict[str, Any]] = None,
        increment_attempt: bool = False,
    ) -> Optional[TurnActionOutboxRecord]:
        return self.runtime_operations_coordinator.mark_turn_action_record(
            action_id,
            status=status,
            phase=phase,
            recoverability=recoverability,
            target_route=target_route,
            message_id=message_id,
            side_effects=side_effects,
            next_retry_at=next_retry_at,
            last_error=last_error,
            failure_report=failure_report,
            increment_attempt=increment_attempt,
        )

    def block_turn_actions_after(
        self, batch_id: str, failed_action_index: int, *, reason: str
    ) -> None:
        self.runtime_operations_coordinator.block_turn_actions_after(
            batch_id,
            failed_action_index,
            reason=reason,
        )

    def resolve_root_task_id(self, task_id: Optional[str]) -> Optional[str]:
        """Resolve the full local execution root for a task id."""
        return self.runtime_operations_coordinator.resolve_root_task_id(task_id)

    def get_active_runtime_lease(
        self, root_task_id: Optional[str]
    ) -> Optional[TaskRuntimeLeaseRecord]:
        return self.runtime_operations_coordinator.get_active_runtime_lease(
            root_task_id
        )

    def begin_runtime_batch_lease(
        self,
        *,
        root_task_id: str,
        source_task_id: str,
        batch_id: Optional[str],
        owner_agent_id: str,
        current_action_index: int = 0,
        ttl_seconds: Optional[float] = None,
        timestamp: Optional[str] = None,
    ) -> TaskRuntimeLeaseRecord:
        return self.runtime_operations_coordinator.begin_runtime_batch_lease(
            root_task_id=root_task_id,
            source_task_id=source_task_id,
            batch_id=batch_id,
            owner_agent_id=owner_agent_id,
            current_action_index=current_action_index,
            ttl_seconds=ttl_seconds,
            timestamp=timestamp,
        )

    def update_runtime_batch_lease(
        self,
        lease_id: str,
        *,
        current_action_index: Optional[int] = None,
        status: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Optional[TaskRuntimeLeaseRecord]:
        return self.runtime_operations_coordinator.update_runtime_batch_lease(
            lease_id,
            current_action_index=current_action_index,
            status=status,
            timestamp=timestamp,
        )

    def interrupt_runtime_batch_lease(
        self,
        *,
        root_task_id: str,
        event_id: Optional[str],
        reason: str,
        timestamp: Optional[str] = None,
    ) -> Optional[TaskRuntimeLeaseRecord]:
        return self.runtime_operations_coordinator.interrupt_runtime_batch_lease(
            root_task_id=root_task_id,
            event_id=event_id,
            reason=reason,
            timestamp=timestamp,
        )

    def complete_runtime_batch_lease(
        self,
        lease_id: str,
        *,
        status: str = "completed",
        timestamp: Optional[str] = None,
    ) -> Optional[TaskRuntimeLeaseRecord]:
        return self.runtime_operations_coordinator.complete_runtime_batch_lease(
            lease_id,
            status=status,
            timestamp=timestamp,
        )

    def mark_stale_runtime_leases(
        self,
        *,
        stale_after_seconds: float,
        reason: str = "Runtime lease became stale.",
    ) -> List[TaskRuntimeLeaseRecord]:
        return self.runtime_operations_coordinator.mark_stale_runtime_leases(
            stale_after_seconds=stale_after_seconds,
            reason=reason,
        )

    def record_runtime_mailbox_event(
        self,
        *,
        root_task_id: str,
        event_type: str,
        event_class: str,
        priority: int,
        idempotency_key: str,
        payload_json: Dict[str, Any],
        header_json: Optional[Dict[str, Any]] = None,
        meta_json: Optional[Dict[str, Any]] = None,
        local_task_id: Optional[str] = None,
        related_task_id: Optional[str] = None,
        message_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        schedule_id: Optional[str] = None,
        blocked_by_lease_id: Optional[str] = None,
        status: str = "received",
        timestamp: Optional[str] = None,
    ) -> RuntimeMailboxEventRecord:
        return self.runtime_operations_coordinator.record_runtime_mailbox_event(
            root_task_id=root_task_id,
            event_type=event_type,
            event_class=event_class,
            priority=priority,
            idempotency_key=idempotency_key,
            payload_json=payload_json,
            header_json=header_json,
            meta_json=meta_json,
            local_task_id=local_task_id,
            related_task_id=related_task_id,
            message_id=message_id,
            correlation_id=correlation_id,
            schedule_id=schedule_id,
            blocked_by_lease_id=blocked_by_lease_id,
            status=status,
            timestamp=timestamp,
        )

    def find_runtime_mailbox_event_by_idempotency(
        self, idempotency_key: Optional[str]
    ) -> Optional[RuntimeMailboxEventRecord]:
        return self.runtime_operations_coordinator.find_runtime_mailbox_event_by_idempotency(
            idempotency_key
        )

    def list_deferred_runtime_events(
        self,
        root_task_id: str,
        *,
        limit: Optional[int] = None,
    ) -> List[RuntimeMailboxEventRecord]:
        return self.runtime_operations_coordinator.list_deferred_runtime_events(
            root_task_id, limit=limit
        )

    def list_deferred_runtime_event_root_ids(self) -> List[str]:
        return (
            self.runtime_operations_coordinator.list_deferred_runtime_event_root_ids()
        )

    def mark_runtime_event_status(
        self,
        event_id: str,
        status: str,
        *,
        last_error: Optional[str] = None,
        processed: bool = False,
    ) -> Optional[RuntimeMailboxEventRecord]:
        return self.runtime_operations_coordinator.mark_runtime_event_status(
            event_id,
            status,
            last_error=last_error,
            processed=processed,
        )

    def prune_runtime_mailbox_events(self, *, retention_seconds: float) -> int:
        """Delete old mailbox ledger events that can no longer affect runtime ordering."""
        return self.runtime_operations_coordinator.prune_runtime_mailbox_events(
            retention_seconds=retention_seconds
        )

    def try_transition_task_state(
        self,
        task_id: str,
        *,
        allowed_from: set[ProgressState],
        to_state: ProgressState,
        timestamp: str,
        reason: str,
        event_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        source: str = "coordinator",
    ) -> bool:
        return self.runtime_operations_coordinator.try_transition_task_state(
            task_id,
            allowed_from=allowed_from,
            to_state=to_state,
            timestamp=timestamp,
            reason=reason,
            event_id=event_id,
            batch_id=batch_id,
            source=source,
        )

    def project_task_state_after_batch(
        self,
        root_task_id: str,
        *,
        batch_id: Optional[str],
        reason: str,
        timestamp: Optional[str] = None,
    ) -> Optional[ProgressState]:
        return self.runtime_operations_coordinator.project_task_state_after_batch(
            root_task_id,
            batch_id=batch_id,
            reason=reason,
            timestamp=timestamp,
        )

    def due_turn_action_retry_records(
        self, *, now: Optional[datetime] = None
    ) -> List[TurnActionOutboxRecord]:
        return self.runtime_operations_coordinator.due_turn_action_retry_records(
            now=now
        )

    def mark_stale_dispatching_turn_actions(
        self,
        *,
        stale_after_seconds: float,
        now: Optional[datetime] = None,
    ) -> List[TurnActionOutboxRecord]:
        """Move live dispatching records that exceeded the stale window back to retry."""
        return self.runtime_operations_coordinator.mark_stale_dispatching_turn_actions(
            stale_after_seconds=stale_after_seconds,
            now=now,
        )

    def build_turn_action_recovery_brief(self, batch_id: str) -> str:
        return self.runtime_operations_coordinator.build_turn_action_recovery_brief(
            batch_id
        )

    def _export_runtime_state(self) -> Dict[str, Any]:
        return self.runtime_snapshot_coordinator._export_runtime_state()

    def _persist_runtime_state(
        self, *, schedule_admission: "ScheduleOccurrenceV1 | None" = None,
        deferred_admission_task_id: Optional[str] = None,
        custody_takeover: "TaskCustodyTakeover | None" = None,
    ) -> None:
        self.runtime_snapshot_coordinator._persist_runtime_state(
            schedule_admission=schedule_admission,
            deferred_admission_task_id=deferred_admission_task_id,
            custody_takeover=custody_takeover,
        )

    def set_custody_lost_listener(self, listener: Optional[Callable[[tuple[str, ...]], None]]) -> None:
        """Hear about saved work another agent took over (``custom_agents.custody_loss``)."""
        self.runtime_snapshot_coordinator.set_custody_lost_listener(listener)

    def get_stored_runtime_team_id(self) -> Optional[str]:
        return self.runtime_snapshot_coordinator.get_stored_runtime_team_id()

    def restore_runtime_state(self) -> Optional[Dict[str, Any]]:
        return self.runtime_snapshot_coordinator.restore_runtime_state()

    def restore_runtime_snapshot_rows(
        self, rows: List[TaskRuntimeSnapshotRow]
    ) -> Optional[Dict[str, Any]]:
        """Restore already-claimed runtime snapshot rows into in-memory task state."""
        return self.runtime_snapshot_coordinator.restore_runtime_snapshot_rows(rows)

    def restore_runtime_state_for_role_takeover(self) -> Optional[Dict[str, Any]]:
        return (
            self.runtime_snapshot_coordinator.restore_runtime_state_for_role_takeover()
        )

    def restore_runtime_state_for_owner_ids(
        self, owner_agent_ids: List[str]
    ) -> Optional[Dict[str, Any]]:
        return self.runtime_snapshot_coordinator.restore_runtime_state_for_owner_ids(
            owner_agent_ids
        )

    def _save_completed_task(self, task_id: str, status: str):
        """Archives a task and removes it from active memory."""
        task_info = self.get_task_info(task_id)
        if not task_info:
            return

        try:
            self._sync_plan_from_task(task_id, create=False)
            task_json = json.dumps(task_info.task.model_dump())
            self.task_archive_repository.save_completed_task(
                archive_id=self._build_archive_id(task_id),
                task_id=task_id,
                owner_agent_id=self.identity.owner_agent_id,
                owner_agent_name=self.identity.owner_agent_name,
                team_id=self.identity.team_id,
                # The person recorded at admission (TaskInfo.owner_person_id).
                owner_canonical_user_id=task_info.owner_person_id,
                task_json=task_json,
                status=status,
            )
            self._logger.info(
                f"Task {task_id} gracefully archived to database with status {status}."
            )
            self.remove_task(task_id)
        except Exception as e:
            self._logger.error(f"Failed to save task {task_id} to database: {e}")

    def _build_history_entry(
        self,
        summary: str,
        *,
        source: TaskHistorySource = TaskHistorySource.SYSTEM,
        entry_type: TaskHistoryType = TaskHistoryType.TASK_EVENT,
        importance: TaskHistoryImportance = TaskHistoryImportance.NORMAL,
        timestamp: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        related_task_id: Optional[str] = None,
        plan_step_id: Optional[str] = None,
        tracks_to_plan: Optional[bool] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> TaskHistoryEntry:
        return TaskHistoryEntry(
            timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
            source=source,
            entry_type=entry_type,
            importance=importance,
            summary=summary,
            agent_name=agent_name,
            agent_id=agent_id,
            related_task_id=related_task_id,
            plan_step_id=plan_step_id,
            tracks_to_plan=tracks_to_plan,
            details=details or {},
        )

    def append_history_entry(
        self,
        task_id: str,
        summary: str,
        *,
        source: TaskHistorySource = TaskHistorySource.SYSTEM,
        entry_type: TaskHistoryType = TaskHistoryType.TASK_EVENT,
        importance: TaskHistoryImportance = TaskHistoryImportance.NORMAL,
        timestamp: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        related_task_id: Optional[str] = None,
        plan_step_id: Optional[str] = None,
        tracks_to_plan: Optional[bool] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[TaskHistoryEntry]:
        task_info = self.get_task_info(task_id)
        if not task_info:
            self._logger.warning(
                f"Cannot append history entry for unknown task {task_id}."
            )
            return None

        plan_id = self._ensure_task_plan_link(task_info, create=False)
        objective_id = self._get_task_objective_id_from_context(task_id)
        if objective_id is None and plan_id:
            objective = self._get_objective_for_plan_id(plan_id)
            objective_id = objective.objective_id if objective else None
        entry = self._build_history_entry(
            summary,
            source=source,
            entry_type=entry_type,
            importance=importance,
            timestamp=timestamp,
            agent_name=agent_name,
            agent_id=agent_id,
            related_task_id=related_task_id,
            plan_step_id=plan_step_id,
            tracks_to_plan=tracks_to_plan,
            details=details,
        )
        entry.objective_id = objective_id
        entry.plan_id = plan_id
        task_info.task.execution_history.append(entry)
        try:
            if not self.runtime_snapshot_coordinator.persist_task_history(task_id):
                task_info.task.execution_history.remove(entry)
                return None
        except Exception:
            task_info.task.execution_history.remove(entry)
            raise
        return entry

    def append_history_entry_to_archived_task(
        self,
        task_id: str,
        summary: str,
        *,
        source: TaskHistorySource = TaskHistorySource.SYSTEM,
        entry_type: TaskHistoryType = TaskHistoryType.TASK_EVENT,
        importance: TaskHistoryImportance = TaskHistoryImportance.NORMAL,
        timestamp: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        related_task_id: Optional[str] = None,
        plan_step_id: Optional[str] = None,
        tracks_to_plan: Optional[bool] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[TaskHistoryEntry]:
        """
        Append history to an already archived task and rewrite the archive row.

        This is used for non-blocking follow-up context such as
        `AfterActionReport`, which may arrive after the parent task has already
        been completed and removed from active memory.
        """
        archived = self.get_task_from_db(task_id)
        if not archived:
            self._logger.warning(
                "Cannot append archived history entry for unknown task %s.", task_id
            )
            return None

        task = archived["main_task"]
        entry = self._build_history_entry(
            summary,
            source=source,
            entry_type=entry_type,
            importance=importance,
            timestamp=timestamp,
            agent_name=agent_name,
            agent_id=agent_id,
            related_task_id=related_task_id,
            plan_step_id=plan_step_id,
            tracks_to_plan=tracks_to_plan,
            details=details,
        )
        entry.objective_id = self.get_task_objective_id(task_id)
        entry.plan_id = self.get_task_plan_id(task_id)
        task.execution_history.append(entry)
        self.task_archive_repository.save_completed_task(
            archive_id=self._build_archive_id(task_id),
            task_id=task_id,
            owner_agent_id=self.identity.owner_agent_id,
            owner_agent_name=self.identity.owner_agent_name,
            team_id=self.identity.team_id,
            owner_canonical_user_id=archived.get("owner_canonical_user_id"),
            task_json=json.dumps(task.model_dump()),
            status=archived["status"],
        )
        self._logger.info("Appended history entry to archived task %s.", task_id)
        return entry

    def record_after_action_report(
        self,
        report: AfterActionReport,
        *,
        identity: ReportedWorkIdentity,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Optional[AfterActionReportRecordResult]:
        """
        Record an AfterActionReport for active or already archived manager-owned work.

        ``identity`` says whom the reported work was for, from verified
        identity only (``custom_agents.after_action_report_identity``).
        """
        return self.after_action_report_coordinator.record_after_action_report(
            report,
            identity=identity,
            agent_name=agent_name,
            agent_id=agent_id,
            timestamp=timestamp,
        )

    def _build_after_action_report_review(
        self,
        report: AfterActionReport,
        timestamp: str,
        identity: ReportedWorkIdentity,
    ) -> Optional[AfterActionReportReviewRecord]:
        return self.after_action_report_coordinator._build_after_action_report_review(
            report, timestamp, identity
        )

    def _parse_utc_timestamp(self, timestamp: Optional[str]) -> datetime:
        return self.after_action_report_coordinator._parse_utc_timestamp(timestamp)

    def _record_late_after_action_report_for_archived_task(
        self,
        *,
        report: AfterActionReport,
        review: AfterActionReportReviewRecord,
        target_task_id: str,
        now: str,
        identity: ReportedWorkIdentity,
    ) -> AfterActionReportRecordResult:
        return self.after_action_report_coordinator._record_late_after_action_report_for_archived_task(
            report=report,
            review=review,
            target_task_id=target_task_id,
            now=now,
            identity=identity,
        )

    def _get_or_create_after_action_report_batch_for_active_task(
        self,
        target_task_id: str,
        *,
        now: str,
        identity: ReportedWorkIdentity,
    ) -> AfterActionReportReviewBatchRecord:
        return self.after_action_report_coordinator._get_or_create_after_action_report_batch_for_active_task(
            target_task_id,
            now=now,
            identity=identity,
        )

    def begin_after_action_report_completion_window(
        self,
        task_id: str,
        *,
        timestamp: str,
    ) -> List[AfterActionReportReviewBatchRecord]:
        return self.after_action_report_coordinator.begin_after_action_report_completion_window(
            task_id,
            timestamp=timestamp,
        )

    def mark_after_action_report_batch_scheduled(
        self,
        batch_id: str,
        *,
        scheduled_message_id: str,
        scheduled_review_task_id: str,
        timestamp: Optional[str] = None,
    ) -> Optional[AfterActionReportReviewBatchRecord]:
        return self.after_action_report_coordinator.mark_after_action_report_batch_scheduled(
            batch_id,
            scheduled_message_id=scheduled_message_id,
            scheduled_review_task_id=scheduled_review_task_id,
            timestamp=timestamp,
        )

    def build_after_action_report_batch_review_task(
        self,
        batch_id: str,
        *,
        task_id: Optional[str] = None,
    ) -> Optional[Task]:
        return self.after_action_report_coordinator.build_after_action_report_batch_review_task(
            batch_id,
            task_id=task_id,
        )

    def count_pending_after_action_report_reviews_for_batch(self, batch_id: str) -> int:
        return self.after_action_report_coordinator.count_pending_after_action_report_reviews_for_batch(
            batch_id
        )

    def close_after_action_report_batch_collection_for_review(
        self,
        batch_id: str,
        *,
        scheduled_review_task_id: str,
        timestamp: Optional[str] = None,
    ) -> Optional[AfterActionReportReviewBatchRecord]:
        return self.after_action_report_coordinator.close_after_action_report_batch_collection_for_review(
            batch_id,
            scheduled_review_task_id=scheduled_review_task_id,
            timestamp=timestamp,
        )

    def list_after_action_report_reviews(
        self,
        *,
        person_id: Optional[str],
        status: Optional[
            AfterActionReportReviewStatus
        ] = AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW,
        limit: int = 20,
    ) -> List[AfterActionReportReviewRecord]:
        """Reviews of reports about ``person_id``'s work (None: work for nobody)."""
        return self.after_action_report_coordinator.list_after_action_report_reviews(
            person_id=person_id, status=status, limit=limit
        )

    def get_after_action_report_review(
        self, review_id: str, *, person_id: Optional[str]
    ) -> Optional[AfterActionReportReviewRecord]:
        return self.after_action_report_coordinator.get_after_action_report_review(
            review_id, person_id=person_id
        )

    def get_after_action_report_review_by_report_id(
        self, report_id: str, *, person_id: Optional[str]
    ) -> Optional[AfterActionReportReviewRecord]:
        return self.after_action_report_coordinator.get_after_action_report_review_by_report_id(
            report_id, person_id=person_id
        )

    def resolve_after_action_report_review(
        self,
        review_id: Optional[str],
        *,
        person_id: Optional[str],
        report_id: Optional[str] = None,
        status: AfterActionReportReviewStatus,
        decision_summary: str,
        review_rationale: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[AfterActionReportReviewRecord]:
        return self.after_action_report_coordinator.resolve_after_action_report_review(
            review_id,
            person_id=person_id,
            report_id=report_id,
            status=status,
            decision_summary=decision_summary,
            review_rationale=review_rationale,
            agent_name=agent_name,
            agent_id=agent_id,
        )

    def record_turn_work(
        self,
        task_id: str,
        details: Dict[str, Any],
        *,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[TaskHistoryEntry]:
        """Save what a turn did before its task waited (``custom_agents.turn_work_notes``)."""
        return self.append_history_entry(
            task_id,
            note_summary(details),
            source=TaskHistorySource.SYSTEM,
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            agent_name=agent_name,
            agent_id=agent_id,
            details=details,
        )

    def render_earlier_turn_work_markdown(self, task_id: Optional[str]) -> str:
        """The "Work already done in this task" prompt section ("" on a task's first turn)."""
        if not task_id:
            return ""
        return render_earlier_turn_work(turn_work_notes(self.get_task(task_id)))

    def log_agent_note(
        self,
        task_id: str,
        note: str,
        *,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        importance: TaskHistoryImportance = TaskHistoryImportance.NORMAL,
        plan_step_id: Optional[str] = None,
        tracks_to_plan: Optional[bool] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[TaskHistoryEntry]:
        return self.append_history_entry(
            task_id,
            note,
            source=TaskHistorySource.AGENT,
            entry_type=TaskHistoryType.AGENT_NOTE,
            importance=importance,
            agent_name=agent_name,
            agent_id=agent_id,
            plan_step_id=plan_step_id,
            tracks_to_plan=tracks_to_plan,
            details=details,
        )

    def _ensure_execution_digest(self, task: Task) -> ExecutionDigest:
        digest = getattr(task, "execution_digest", None)
        if digest is None:
            digest = ExecutionDigest(plan_summary=task.task_plan)
            task.execution_digest = digest
        elif task.task_plan and not digest.plan_summary:
            digest.plan_summary = task.task_plan
        elif digest.plan_summary and not task.task_plan:
            task.task_plan = digest.plan_summary
        return digest

    def _touch_execution_digest(
        self, task: Task, timestamp: Optional[str] = None
    ) -> ExecutionDigest:
        digest = self._ensure_execution_digest(task)
        digest.last_updated_at = timestamp or datetime.now(timezone.utc).isoformat()
        if digest.plan_summary:
            task.task_plan = digest.plan_summary
        return digest

    def set_task_plan(
        self,
        task_id: str,
        task_plan: str,
        *,
        goal_summary: Optional[str] = None,
        current_focus: Optional[str] = None,
        rationale: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        source: TaskHistorySource = TaskHistorySource.AGENT,
        log_history: bool = True,
    ) -> Optional[ExecutionDigest]:
        task_info = self.get_task_info(task_id)
        if not task_info:
            self._logger.warning(
                "Cannot set execution plan for unknown task %s.", task_id
            )
            return None

        task = task_info.task
        digest = self._ensure_execution_digest(task)
        previous_plan = task.task_plan or digest.plan_summary
        changed = False

        if task_plan and task_plan != digest.plan_summary:
            digest.plan_summary = task_plan
            changed = True
        if task_plan and task_plan != task.task_plan:
            task.task_plan = task_plan
            changed = True
        if goal_summary is not None and goal_summary != digest.goal_summary:
            digest.goal_summary = goal_summary or None
            changed = True
        if current_focus is not None and current_focus != digest.current_focus:
            digest.current_focus = current_focus or None
            changed = True

        if not changed:
            self._sync_plan_from_task(task_id, create=True)
            return digest

        now_ts = datetime.now(timezone.utc).isoformat()
        self._ensure_task_plan_link(task_info, create=True)
        self._touch_execution_digest(task, now_ts)
        if log_history:
            self.append_history_entry(
                task_id,
                "Updated execution plan summary."
                if previous_plan
                else "Recorded initial execution plan summary.",
                source=source,
                entry_type=TaskHistoryType.PLAN_EVENT,
                importance=TaskHistoryImportance.HIGH,
                timestamp=now_ts,
                agent_name=agent_name,
                agent_id=agent_id,
                tracks_to_plan=True,
                details={
                    "action": "set_plan_summary",
                    "plan_summary": digest.plan_summary,
                    "goal_summary": digest.goal_summary,
                    "current_focus": digest.current_focus,
                    "rationale": rationale,
                    "previous_plan_summary": previous_plan,
                },
            )
        else:
            self._persist_runtime_state()
        self._sync_plan_from_task(task_id, create=True)
        self._emit_activity(
            activity_type=TaskActivityType.PLAN_UPDATED,
            stage=TaskActivityStage.PLANNING,
            task_id=task_id,
            summary="Execution plan updated.",
            detail=digest.plan_summary,
            details={
                "goal_summary": digest.goal_summary,
                "current_focus": digest.current_focus,
                "rationale": rationale,
                "source": source.value,
            },
        )
        return digest

    def upsert_execution_plan_item(
        self,
        task_id: str,
        *,
        title: str,
        rationale: str,
        step_id: Optional[str] = None,
        status: ExecutionPlanItemStatus = ExecutionPlanItemStatus.PLANNED,
        status_detail: Optional[str] = None,
        resolution_summary: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        source: TaskHistorySource = TaskHistorySource.AGENT,
    ) -> Optional[ExecutionPlanItem]:
        if not rationale or not rationale.strip():
            raise ValueError("Execution plan items require a rationale.")

        task_info = self.get_task_info(task_id)
        if not task_info:
            self._logger.warning(
                "Cannot update execution plan item for unknown task %s.", task_id
            )
            return None

        task = task_info.task
        digest = self._ensure_execution_digest(task)
        item: Optional[ExecutionPlanItem] = None
        previous: Dict[str, Any] = {}
        for existing in digest.plan_items:
            if existing.step_id == step_id:
                item = existing
                previous = existing.model_dump(mode="json")
                break

        now_ts = datetime.now(timezone.utc).isoformat()
        self._ensure_task_plan_link(task_info, create=True)
        if item is None:
            item = ExecutionPlanItem(
                step_id=step_id
                or ("plan_step_" + title.lower().replace(" ", "_")[:24]),
                title=title,
                rationale=rationale.strip(),
                status=status,
                status_detail=status_detail or None,
                resolution_summary=resolution_summary or None,
                created_at=now_ts,
                updated_at=now_ts,
            )
            digest.plan_items.append(item)
        else:
            item.title = title
            item.rationale = rationale.strip()
            item.status = status
            item.status_detail = status_detail or None
            item.resolution_summary = resolution_summary or None
            item.updated_at = now_ts

        self._touch_execution_digest(task, now_ts)
        self.append_history_entry(
            task_id,
            f"Updated plan item `{item.step_id}` ({item.status.value}).",
            source=source,
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.HIGH,
            timestamp=now_ts,
            agent_name=agent_name,
            agent_id=agent_id,
            plan_step_id=item.step_id,
            tracks_to_plan=True,
            details={
                "action": "upsert_plan_item",
                "step_id": item.step_id,
                "title": item.title,
                "status": item.status.value,
                "rationale": item.rationale,
                "status_detail": item.status_detail,
                "resolution_summary": item.resolution_summary,
                "previous": previous,
            },
        )
        self._sync_plan_from_task(task_id, create=True)
        return item

    def upsert_execution_unknown(
        self,
        task_id: str,
        *,
        summary: str,
        unknown_id: Optional[str] = None,
        status: ExecutionUnknownStatus = ExecutionUnknownStatus.OPEN,
        why_it_matters: Optional[str] = None,
        resolution_plan: Optional[str] = None,
        resolution_summary: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        source: TaskHistorySource = TaskHistorySource.AGENT,
    ) -> Optional[ExecutionUnknown]:
        task_info = self.get_task_info(task_id)
        if not task_info:
            self._logger.warning(
                "Cannot update execution unknown for unknown task %s.", task_id
            )
            return None

        task = task_info.task
        digest = self._ensure_execution_digest(task)
        item: Optional[ExecutionUnknown] = None
        previous: Dict[str, Any] = {}
        for existing in digest.unknowns:
            if existing.unknown_id == unknown_id:
                item = existing
                previous = existing.model_dump(mode="json")
                break

        now_ts = datetime.now(timezone.utc).isoformat()
        self._ensure_task_plan_link(task_info, create=True)
        if item is None:
            item = ExecutionUnknown(
                unknown_id=unknown_id
                or ("unknown_" + summary.lower().replace(" ", "_")[:24]),
                summary=summary,
                why_it_matters=why_it_matters or None,
                resolution_plan=resolution_plan or None,
                status=status,
                resolution_summary=resolution_summary or None,
                created_at=now_ts,
                updated_at=now_ts,
            )
            digest.unknowns.append(item)
        else:
            item.summary = summary
            item.why_it_matters = why_it_matters or None
            item.resolution_plan = resolution_plan or None
            item.status = status
            item.resolution_summary = resolution_summary or None
            item.updated_at = now_ts

        self._touch_execution_digest(task, now_ts)
        self.append_history_entry(
            task_id,
            f"Updated tracked unknown `{item.unknown_id}` ({item.status.value}).",
            source=source,
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.NORMAL,
            timestamp=now_ts,
            agent_name=agent_name,
            agent_id=agent_id,
            tracks_to_plan=False,
            details={
                "action": "upsert_unknown",
                "unknown_id": item.unknown_id,
                "summary": item.summary,
                "status": item.status.value,
                "why_it_matters": item.why_it_matters,
                "resolution_plan": item.resolution_plan,
                "resolution_summary": item.resolution_summary,
                "previous": previous,
            },
        )
        self._sync_plan_from_task(task_id, create=True)
        return item

    def get_execution_digest(self, task_id: str) -> Optional[ExecutionDigest]:
        task_info = self.get_task_info(task_id)
        if task_info is not None:
            digest = self._ensure_execution_digest(task_info.task)
            self._sync_plan_from_task(task_id, create=False)
            return digest

        archived = self.get_task_from_db(task_id)
        if not archived:
            return None
        task = archived["main_task"]
        digest = getattr(task, "execution_digest", None)
        if digest is None and task.task_plan:
            digest = ExecutionDigest(plan_summary=task.task_plan)
            task.execution_digest = digest
        elif digest and digest.plan_summary and not task.task_plan:
            task.task_plan = digest.plan_summary
        return digest

    def has_active_execution_digest(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        history_entries: List[TaskHistoryEntry] = []
        if task is None:
            archived = self.get_task_from_db(task_id)
            if not archived:
                return False
            task = archived["main_task"]
            history_entries = list(getattr(task, "execution_history", None) or [])
        else:
            history_entries = self.get_task_history_entries(task_id, limit=None)

        digest = getattr(task, "execution_digest", None)
        if digest is None:
            return False
        if digest.plan_items or digest.unknowns:
            return True
        if digest.goal_summary or digest.current_focus:
            return True
        plan_event_count = sum(
            1
            for entry in history_entries
            if entry.entry_type == TaskHistoryType.PLAN_EVENT
        )
        return plan_event_count > 1

    def render_execution_digest_markdown(
        self,
        task_id: str,
        *,
        max_plan_events: int = 8,
        only_if_active: bool = True,
    ) -> str:
        if only_if_active and not self.has_active_execution_digest(task_id):
            return ""
        digest = self.get_execution_digest(task_id)
        if digest is None:
            return "## Execution Digest\n- No execution digest recorded."

        lines = ["## Execution Digest"]
        objective = self._get_task_objective_record(task_id)
        if objective is not None:
            lines.append(
                f"- **Objective:** {objective.title} ({objective.status.value}, {objective.horizon.value})"
            )
            if objective.current_focus:
                lines.append(f"- **Objective Focus:** {objective.current_focus}")
            if objective.progress_summary:
                lines.append(f"- **Objective Progress:** {objective.progress_summary}")
        lines.append(f"- **Goal:** {digest.goal_summary or 'N/A'}")
        lines.append(f"- **Plan Summary:** {digest.plan_summary or 'N/A'}")
        lines.append(f"- **Current Focus:** {digest.current_focus or 'N/A'}")
        lines.append(f"- **Last Updated:** {digest.last_updated_at}")

        lines.append("\n### Plan Items")
        if digest.plan_items:
            for item in digest.plan_items:
                lines.append(f"- **{item.step_id}** [{item.status.value}] {item.title}")
                lines.append(f"  Rationale: {item.rationale}")
                if item.status_detail:
                    lines.append(f"  Status Detail: {item.status_detail}")
                if item.resolution_summary:
                    lines.append(f"  Resolution: {item.resolution_summary}")
        else:
            lines.append("- None recorded.")

        plan_id = self.get_task_plan_id(task_id)
        plan = self.get_plan(plan_id) if plan_id else None
        lines.append("\n### Runtime Plan Steps")
        if plan and plan.plan_steps:
            for index, step in enumerate(
                sorted(plan.plan_steps, key=lambda item: item.order_index), start=1
            ):
                lines.append(f"- {index}. [{step.status.value}] {step.title}")
                lines.append(f"  Objective: {step.objective}")
                if step.status_detail:
                    lines.append(f"  Status Detail: {step.status_detail}")
                if step.result_summary:
                    lines.append(f"  Result: {step.result_summary}")
        else:
            lines.append("- None recorded.")

        lines.append("\n### Unknowns")
        if digest.unknowns:
            for item in digest.unknowns:
                lines.append(
                    f"- **{item.unknown_id}** [{item.status.value}] {item.summary}"
                )
                if item.why_it_matters:
                    lines.append(f"  Why It Matters: {item.why_it_matters}")
                if item.resolution_plan:
                    lines.append(f"  Resolution Path: {item.resolution_plan}")
                if item.resolution_summary:
                    lines.append(f"  Resolution: {item.resolution_summary}")
        else:
            lines.append("- None recorded.")

        plan_actions = self.get_plan_actions(task_id, limit=max_plan_events)
        lines.append("\n### Recent Plan Actions")
        if plan_actions:
            for action in plan_actions:
                visibility = "user-visible" if action.user_visible else "internal"
                lines.append(
                    f"- [{action.created_at}] {action.action_type.value} "
                    f"({action.status.value}, {visibility}): {action.summary}"
                )
                if action.reason:
                    lines.append(f"  Reason: {action.reason}")
        else:
            lines.append("- None recorded.")

        plan_events = [
            entry
            for entry in self.get_task_history_entries(task_id, limit=None)
            if entry.entry_type == TaskHistoryType.PLAN_EVENT
        ]
        lines.append("\n### Recent Plan Evolution")
        if plan_events:
            for entry in plan_events[-max_plan_events:]:
                lines.append(f"- [{entry.timestamp}] {entry.summary}")
        else:
            lines.append("- No plan evolution recorded.")
        return "\n".join(lines)

    def get_task_history_entries(
        self,
        task_id: str,
        *,
        start_index: int = 0,
        limit: Optional[int] = 20,
        include_system: bool = True,
        include_agent: bool = True,
    ) -> List[TaskHistoryEntry]:
        task = self.get_task(task_id)
        if task is None:
            archived = self.get_task_from_db(task_id)
            if not archived:
                return []
            task = archived["main_task"]

        entries = list(task.execution_history or [])
        filtered = []
        for entry in entries:
            if entry.source == TaskHistorySource.SYSTEM and not include_system:
                continue
            if entry.source == TaskHistorySource.AGENT and not include_agent:
                continue
            filtered.append(entry)

        if start_index < 0:
            start_index = 0
        if limit is None or limit < 0:
            return filtered[start_index:]
        return filtered[start_index : start_index + limit]

    def build_compact_history(
        self,
        task_id: str,
        *,
        max_entries: int = 12,
        include_system: bool = True,
        include_agent: bool = True,
    ) -> str:
        entries = self.get_task_history_entries(
            task_id,
            start_index=0,
            limit=None,
            include_system=include_system,
            include_agent=include_agent,
        )
        if not entries:
            return "No execution history available."

        recent_entries = entries[-max_entries:]
        lines = []
        for entry in recent_entries:
            actor = entry.agent_name or entry.agent_id or entry.source.value
            summary = sanitize_history_summary_for_prompt(entry.summary)
            if summary.startswith("Task received and queued:"):
                continue
            lines.append(
                f"- [{entry.timestamp}] ({entry.entry_type.value}/{entry.importance.value}) {actor}: {summary}"
            )
        return (
            "\n".join(lines)
            if lines
            else "No prompt-relevant execution history available."
        )

    def _top_level_task_count(self) -> int:
        """Return the number of tracked top-level tasks currently held in memory."""
        return sum(
            1
            for task_info in self.task_state.tasks.values()
            if task_info.task.parent_task_id is None
        )

    def is_buffer_full(self) -> bool:
        """Checks whether tracked top-level tasks have reached the configured limit."""
        return self._top_level_task_count() >= self._max_queued_top_level_tasks

    def get_tracked_top_level_task_count(self) -> int:
        """Return the number of admitted top-level tasks currently tracked in memory."""
        return self._top_level_task_count()

    def get_runnable_top_level_queue_depth(self) -> int:
        """Return the number of runnable top-level tasks currently queued for execution."""
        return sum(
            1
            for task_id in self.task_state.task_queue
            if (
                task_id in self.task_state.tasks
                and self.task_state.tasks[task_id].task.parent_task_id is None
                and self.is_runnable_state(self.task_state.tasks[task_id].state)
            )
        )

    def get_waiting_top_level_task_count(self) -> int:
        """Return the number of top-level tasks currently blocked in WAITING."""
        return len(self.task_state.waiting_top_level_task_ids)

    def get_max_queued_top_level_tasks(self) -> int:
        return self._max_queued_top_level_tasks

    def _check_queue_backlog_warning(self) -> None:
        if self._queue_warning_threshold <= 0:
            return
        queue_depth = len(self.task_state.task_queue)
        if queue_depth >= self._queue_warning_threshold:
            if not self._queue_backlog_warning_active:
                self._logger.warning(
                    "Top-level task queue backlog threshold reached: queue_depth=%s threshold=%s tracked_top_level=%s waiting=%s",
                    queue_depth,
                    self._queue_warning_threshold,
                    self.get_tracked_top_level_task_count(),
                    self.get_waiting_top_level_task_count(),
                )
                self._queue_backlog_warning_active = True
            return
        if self._queue_backlog_warning_active:
            self._logger.info(
                "Top-level task queue backlog recovered: queue_depth=%s threshold=%s",
                queue_depth,
                self._queue_warning_threshold,
            )
            self._queue_backlog_warning_active = False

    def can_start_new_task(self) -> bool:
        """Return whether the agent's single execution slot is free."""
        return self.task_state.current_in_progress_task_id is None

    def get_waiting_top_level_task_ids(self) -> List[str]:
        """Return tracked top-level task ids currently in WAITING state."""
        return sorted(self.task_state.waiting_top_level_task_ids)

    def get_top_level_task_infos(self) -> List[TaskInfo]:
        """Return all tracked top-level tasks."""
        return [
            task_info
            for task_info in self.task_state.tasks.values()
            if task_info.task.parent_task_id is None
        ]

    def get_locally_executable_task_infos(self) -> List[TaskInfo]:
        """Return tracked tasks that this agent can execute locally after recovery."""
        return [
            task_info
            for task_id, task_info in self.task_state.tasks.items()
            if task_id not in self.task_state.subtask_to_parent_map
        ]

    def is_locally_executable_task(self, task_id: str) -> bool:
        """
        Return whether this task is owned by this agent's local execution loop.

        A delegated specialist task may carry a `parent_task_id` from another
        runtime, but it is still locally executable for the specialist that
        received it through `add_task()`. Tasks created with `add_subtask()` are
        tracked delegation records owned by this agent's parent task and are not
        executed locally here.
        """
        return (
            task_id in self.task_state.tasks
            and task_id not in self.task_state.subtask_to_parent_map
        )

    def enqueue_task(self, task_id: str, *, front: bool = False) -> bool:
        """Ensure a runnable locally executable task is present in the queue."""
        from custom_agents.task_queue_safety import is_ready

        if not is_ready(self, task_id):
            return False
        if task_id in self.task_state.task_queue:
            return True
        if front:
            self.task_state.task_queue.appendleft(task_id)
        else:
            self.task_state.task_queue.append(task_id)
        self._persist_runtime_state()
        return True

    def is_waiting_on_task_query(self, parent_task_id: str) -> bool:
        """Return True when the latest tracked TaskQuery is still awaiting a response."""
        queries = self.task_state.task_to_taskquery_list.get(parent_task_id, [])
        if not queries:
            return False
        return queries[-1].query_type == MessageMetaType.REQUEST

    def create_recoverable_pause(
        self,
        task_id: str,
        *,
        trigger: RecoverablePauseTrigger | str,
        friendly_reason: str,
        task_query_id: Optional[str] = None,
        affected_subtask_id: Optional[str] = None,
        affected_agent_id: Optional[str] = None,
        affected_named_agent_type: Optional[str] = None,
        base_tool_call_limit: Optional[int] = None,
        partial_tool_call_limit: Optional[int] = None,
        timestamp: Optional[str] = None,
    ) -> RecoverablePauseRecord:
        """Create a persisted recoverable runtime pause record for a task."""
        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        trigger_value = (
            trigger.value
            if isinstance(trigger, RecoverablePauseTrigger)
            else str(trigger)
        )
        prior_count = sum(
            1
            for record in self.task_state.recoverable_pause_records.values()
            if record.task_id == task_id
        )
        record = RecoverablePauseRecord(
            task_id=task_id,
            pause_id=new_id("runtime_pause"),
            trigger=trigger_value,
            status="pending",
            created_at=now_ts,
            task_query_id=task_query_id,
            affected_subtask_id=affected_subtask_id,
            affected_agent_id=affected_agent_id,
            affected_named_agent_type=affected_named_agent_type,
            friendly_reason=friendly_reason,
            base_tool_call_limit=base_tool_call_limit,
            partial_tool_call_limit=partial_tool_call_limit,
            pause_count_for_task=prior_count + 1,
        )
        self.task_state.recoverable_pause_records[record.pause_id] = record
        self.append_history_entry(
            task_id,
            f"Runtime paused task for requester guidance: {friendly_reason}",
            source=TaskHistorySource.SYSTEM,
            entry_type=TaskHistoryType.QUERY_EVENT,
            importance=TaskHistoryImportance.HIGH,
            timestamp=now_ts,
            related_task_id=task_query_id,
            details={
                "reason": "recoverable_runtime_pause",
                "pause_id": record.pause_id,
                "trigger": trigger_value,
                "affected_subtask_id": affected_subtask_id,
                "affected_agent_id": affected_agent_id,
                "affected_named_agent_type": affected_named_agent_type,
                "base_tool_call_limit": base_tool_call_limit,
                "partial_tool_call_limit": partial_tool_call_limit,
                "pause_count_for_task": record.pause_count_for_task,
            },
        )
        self._persist_runtime_state()
        return record

    def get_pending_recoverable_pause(
        self, task_id: str
    ) -> Optional[RecoverablePauseRecord]:
        pending = [
            record
            for record in self.task_state.recoverable_pause_records.values()
            if record.task_id == task_id and record.status == "pending"
        ]
        if not pending:
            return None
        pending.sort(key=lambda item: item.created_at)
        return pending[-1]

    def count_recoverable_pauses(self, task_id: str) -> int:
        return sum(
            1
            for record in self.task_state.recoverable_pause_records.values()
            if record.task_id == task_id
        )

    def resolve_recoverable_pause(
        self,
        task_id: str,
        *,
        decision: str,
        response_text: str,
        timestamp: Optional[str] = None,
    ) -> Optional[RecoverablePauseRecord]:
        record = self.get_pending_recoverable_pause(task_id)
        if record is None:
            return None
        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        record.status = {
            "resume": "resumed",
            "partial": "partial_requested",
            "cancel": "cancelled",
            "retry": "retrying",
            "retry_in_5_minutes": "retry_scheduled",
            "retry_in_10_minutes": "retry_scheduled",
        }.get(decision, decision)
        record.user_decision = decision
        record.user_decision_text = response_text
        self.append_history_entry(
            task_id,
            f"Requester selected runtime pause option: {decision}.",
            source=TaskHistorySource.SYSTEM,
            entry_type=TaskHistoryType.QUERY_EVENT,
            importance=TaskHistoryImportance.HIGH,
            timestamp=now_ts,
            related_task_id=record.task_query_id,
            details={
                "reason": "recoverable_runtime_pause_decision",
                "pause_id": record.pause_id,
                "trigger": record.trigger,
                "decision": decision,
            },
        )
        self._persist_runtime_state()
        return record

    def record_partial_tool_call_limit(
        self, task_id: str, limit: Optional[int]
    ) -> None:
        if limit is None or limit <= 0:
            self.task_state.partial_tool_call_limit_overrides.pop(task_id, None)
        else:
            self.task_state.partial_tool_call_limit_overrides[task_id] = int(limit)
        self._persist_runtime_state()

    def read_partial_tool_call_limit(self, task_id: Optional[str]) -> Optional[int]:
        """The limit the task's next turn uses; released with the turn's note (``TurnStartNotes``)."""
        if not task_id:
            return None
        return self.task_state.partial_tool_call_limit_overrides.get(task_id)

    def append_runtime_resume_directive(self, task_id: str, directive: str) -> None:
        """Add to the note the task's next turn reads (the only way to leave one).

        Notes are added, never replaced: several things can tell a task
        something before its next turn (restart recovery, an access decision,
        a workflow hold, a failed action's recovery brief), and replacing
        would silently drop what an earlier one said, such as that a restart
        the task asked for is done. A complete note already waiting is not
        added again (another restart or retry before the turn). A shorter
        note contained in earlier text can be a correction, so it stays.
        """
        text = str(directive or "").strip()
        earlier = self.task_state.runtime_resume_directives.get(task_id, "").strip()
        # Match the entire note between the separators, including all of its
        # paragraphs, rather than dropping a prefix, suffix or substring.
        if not text or f"\n\n{text}\n\n" in f"\n\n{earlier}\n\n":
            return
        self.task_state.runtime_resume_directives[task_id] = f"{earlier}\n\n{text}" if earlier else text
        self._persist_runtime_state()

    def read_runtime_resume_directive(self, task_id: Optional[str]) -> Optional[str]:
        """The note for the task's next turn; released once the turn's decision is recorded.

        A turn cancelled before then keeps it, so the task's turn after a
        restart reads it again (``TurnStartNotes``).
        """
        if not task_id:
            return None
        return self.task_state.runtime_resume_directives.get(task_id)

    def release_turn_start_notes(self, notes: TurnStartNotes) -> None:
        """Remove what a turn read once its result is saved (``TurnStartNotes``)."""
        if release_turn_start_notes(self.task_state, notes, self._logger):
            self._persist_runtime_state()

    def get_latest_pending_task_query(self, parent_task_id: str) -> Optional[TaskQuery]:
        """Return the latest unanswered TaskQuery request for the task, if any."""
        queries = self.task_state.task_to_taskquery_list.get(parent_task_id, [])
        if not queries:
            return None
        latest = queries[-1]
        if latest.query_type != MessageMetaType.REQUEST:
            return None
        return latest

    def has_unfinished_local_tasks(self) -> bool:
        """True while this agent holds any unfinished task of its own.

        Counts every non-terminal task this agent works on, waiting or paused
        ones and tasks delegated to it from another agent included; subtasks it
        delegated out are not its own work. A worker's idle timer uses it
        (``strands_team``): replies to a waiting task are addressed to this
        agent, so it must not exit while it holds one.
        """
        delegated_out = self.task_state.subtask_to_parent_map
        return any(
            task_id not in delegated_out and not self.is_terminal_state(info.state)
            for task_id, info in self.task_state.tasks.items()
        )

    def has_queued_top_level_tasks(self) -> bool:
        """Return True when there is at least one runnable top-level task in the queue."""
        return any(
            (
                task_id in self.task_state.tasks
                and self.task_state.tasks[task_id].task.parent_task_id is None
                and self.is_runnable_state(self.task_state.tasks[task_id].state)
            )
            for task_id in self.task_state.task_queue
        )

    def add_task(
        self,
        task: Task,
        header: MessageHeader,
        meta: MessageMeta,
        *,
        owner_person_id: Optional[str],
        handled_at: Optional[str] = None,
        handled_queue_name: Optional[str] = None,
        schedule_admission: "ScheduleOccurrenceV1 | None" = None,
        custody_takeover_from: Optional[str] = None,
    ) -> bool:
        """
        Adds a new task to be managed.

        ``owner_person_id`` is the person the task verifiably works for (None
        for work for nobody), decided by the caller from verified identity
        only (``TaskInfo``); it has no default so no caller can leave it to
        the person the message names.

        The first save claims the task's team-wide custody
        (``storage.task_custody_repository``). It raises
        ``TaskCustodyConflict`` when another agent holds the task, unless
        ``custody_takeover_from`` names that holder (``custom_agents.task_custody``
        decided it may be replaced).
        """
        if self.is_execution_cancelled(task.task_id):
            self._logger.warning(
                "Rejected cancelled task admission. event=cancelled_task_admission_rejected "
                "task_id=%s root_task_id=%s team_id=%s",
                task.task_id,
                self.resolve_root_task_id(task.task_id),
                self.identity.team_id,
            )
            return False
        if self.is_buffer_full():
            self._logger.warning(
                f"Top-level task capacity full ({self._max_queued_top_level_tasks}). "
                f"Cannot add task {task.task_id}."
            )
            return False
        if task.task_id in self.task_state.tasks:
            self._logger.warning(
                f"Task {task.task_id} already exists. Ignoring add request."
            )
            return False

        task_info = TaskInfo(task, header, meta, owner_person_id=owner_person_id)
        self.task_state.tasks[task.task_id] = task_info
        self.task_state.task_queue.append(task.task_id)
        self.task_state.task_to_taskquery_list[task.task_id] = list(
            getattr(task, "task_queries", None) or []
        )
        try:
            # Establish durable custody before history/Plan/Objective side effects.
            # A failed initial admission must not become an in-memory duplicate.
            self._persist_runtime_state(
                schedule_admission=schedule_admission, deferred_admission_task_id=task.task_id,
                custody_takeover=(
                    TaskCustodyTakeover(task.task_id, custody_takeover_from)
                    if custody_takeover_from
                    else None
                ),
            )
        except Exception as error:
            if _task_row_taken_over(error):
                # Another agent took this agent's task row over: the snapshot
                # coordinator already dropped every task from memory, this one
                # included. A NATS delivery is not acknowledged and comes back as
                # a new admission. A deferred message stays in this agent's own
                # deferred row and is admitted again from it; if that row was
                # taken over too, its new owner runs it (deferred_message_runtime).
                raise
            # Any other failure, a refused deferred row included, leaves the
            # other tasks in memory: undo this admission only.
            del self.task_state.tasks[task.task_id]
            self.task_state.task_queue.remove(task.task_id)
            del self.task_state.task_to_taskquery_list[task.task_id]
            if isinstance(error, TaskCustodyConflict):
                raise  # an expected refusal; the admitting caller logs it
            self._logger.exception(
                "Initial task admission did not commit",
                extra={
                    "event": "task_admission_failed",
                    "task_id": task.task_id,
                    "owner_agent_id": self.identity.owner_agent_id,
                },
            )
            raise
        incoming_objective_id = getattr(meta, "objective_id", None)
        if incoming_objective_id and self._objectives_enabled():
            self.objective_coordinator.link_incoming_task_to_objective(
                task.task_id,
                incoming_objective_id,
            )
        if handled_at:
            self.append_history_entry(
                task.task_id,
                "Message accepted from NATS for local task handling.",
                entry_type=TaskHistoryType.TASK_EVENT,
                importance=TaskHistoryImportance.NORMAL,
                timestamp=handled_at,
                agent_name=getattr(meta, "from_agent_name", None),
                agent_id=getattr(header, "from_agent_id", None),
                details={
                    "event": "message_handled",
                    "message_id": getattr(header, "message_id", None),
                    "message_created_at": getattr(header, "timestamp", None),
                    "handled_at": handled_at,
                    "queue_name": handled_queue_name,
                    "handler_agent_id": self.identity.owner_agent_id,
                    "handler_agent_name": self.identity.owner_agent_name,
                    "handler_named_agent_type": self.identity.owner_named_agent_type,
                },
            )
        request_preview = (
            extract_display_request_text(task.problem_statement)
            or "No problem statement provided."
        )
        self.append_history_entry(
            task.task_id,
            f"Task received and queued: {request_preview}",
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            agent_name=getattr(meta, "from_agent_name", None),
            agent_id=getattr(header, "from_agent_id", None),
            details={
                "requester_type": str(getattr(meta, "requester_type", "")),
                "to_named_agent_type": getattr(meta, "to_named_agent_type", None),
            },
        )
        self.append_history_entry(
            task.task_id,
            "Task accepted by task manager.",
            entry_type=TaskHistoryType.STATE_CHANGE,
            importance=TaskHistoryImportance.NORMAL,
            agent_name=getattr(meta, "to_agent_name", None),
            related_task_id=task.task_id,
            details={"state": ProgressState.ACCEPTED.value},
        )
        if task.task_plan:
            self.set_task_plan(
                task.task_id,
                task.task_plan,
                rationale="Seeded from incoming task payload.",
                agent_name=getattr(meta, "from_agent_name", None),
                agent_id=getattr(header, "from_agent_id", None),
                source=TaskHistorySource.SYSTEM,
            )
        self._emit_activity(
            activity_type=TaskActivityType.TASK_STARTED,
            stage=TaskActivityStage.QUEUED,
            task_id=task.task_id,
            meta=meta,
            summary=request_preview,
            detail="Task accepted by task manager and queued for execution.",
            details={
                "requester_type": str(getattr(meta, "requester_type", "") or ""),
                "to_named_agent_type": getattr(meta, "to_named_agent_type", None),
            },
        )
        self._logger.info(
            f"Task {task.task_id} added and queued. Current queue size: {len(self.task_state.task_queue)}"
        )
        self._check_queue_backlog_warning()
        self._sync_plan_from_task(task.task_id, create=False)
        self._persist_runtime_state()
        return True

    @staticmethod
    def _coerce_state_value(state: Any) -> str:
        return state.value if isinstance(state, ProgressState) else str(state)

    def get_task_runtime_timing(self, task_id: str) -> Dict[str, Optional[str]]:
        """Return compact runtime timing derived from the task header, history, and state."""
        task_info = self.get_task_info(task_id)
        if not task_info:
            archived = self.get_task_from_db(task_id)
            if not archived:
                return {}
            task = archived["main_task"]
            header = None
        else:
            task = task_info.task
            header = task_info.header

        handled_at: Optional[str] = None
        message_created_at: Optional[str] = getattr(header, "timestamp", None)
        for entry in getattr(task, "execution_history", []) or []:
            details = getattr(entry, "details", {}) or {}
            if details.get("event") == "message_handled":
                handled_at = details.get("handled_at") or getattr(
                    entry, "timestamp", None
                )
                message_created_at = message_created_at or details.get(
                    "message_created_at"
                )
                break

        processing_started_at: Optional[str] = None
        processing_finished_at: Optional[str] = None
        for state, timestamp in getattr(task, "task_state", []) or []:
            state_value = self._coerce_state_value(state)
            if (
                state_value == ProgressState.IN_PROGRESS.value
                and processing_started_at is None
            ):
                processing_started_at = timestamp
            if state_value in {
                ProgressState.COMPLETED.value,
                ProgressState.FAILED.value,
                ProgressState.CANCELLED.value,
                ProgressState.ABANDONED.value,
            }:
                processing_finished_at = timestamp

        return {
            "message_created_at": message_created_at,
            "handled_at": handled_at,
            "processing_started_at": processing_started_at,
            "processing_finished_at": processing_finished_at,
        }

    def render_task_runtime_timing_markdown(
        self,
        task_id: str,
        *,
        include_message_created_at: bool = True,
        include_handled_at: bool = True,
        include_processing_started_at: bool = True,
        include_processing_finished_at: bool = False,
    ) -> str:
        timing = self.get_task_runtime_timing(task_id)
        if not timing:
            return ""

        fields = [
            ("Message created", "message_created_at", include_message_created_at),
            ("Accepted by this agent", "handled_at", include_handled_at),
            (
                "Processing started",
                "processing_started_at",
                include_processing_started_at,
            ),
            (
                "Processing finished",
                "processing_finished_at",
                include_processing_finished_at,
            ),
        ]
        lines = [
            f"- **{label}:** {timing.get(key)}"
            for label, key, enabled in fields
            if enabled and timing.get(key)
        ]
        if not lines:
            return ""
        return "Task Runtime Timing:\n" + "\n".join(lines)

    def add_task_query(
        self,
        parent_task_id: str,
        task_query: TaskQuery,
        *,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ):
        """Adds a TaskQuery to the parent task's query history and execution timeline."""
        if parent_task_id not in self.task_state.task_to_taskquery_list:
            self.task_state.task_to_taskquery_list[parent_task_id] = []
        self.task_state.task_to_taskquery_list[parent_task_id].append(task_query)
        summary = f"TaskQuery {task_query.query_type.value}: {task_query.message}"
        self.append_history_entry(
            parent_task_id,
            summary,
            source=TaskHistorySource.SYSTEM,
            entry_type=TaskHistoryType.QUERY_EVENT,
            importance=TaskHistoryImportance.HIGH
            if task_query.query_type.value == "request"
            else TaskHistoryImportance.NORMAL,
            timestamp=timestamp,
            agent_name=agent_name,
            agent_id=agent_id,
            related_task_id=task_query.task_query_id,
            details={"query_type": task_query.query_type.value},
        )
        if task_query.query_type == MessageMetaType.REQUEST:
            self._emit_activity(
                activity_type=TaskActivityType.WAITING_ON_USER,
                stage=TaskActivityStage.WAITING,
                task_id=parent_task_id,
                summary="Waiting on clarification from the user.",
                detail=task_query.message,
                details={"task_query_id": task_query.task_query_id},
            )
        else:
            self._emit_activity(
                activity_type=TaskActivityType.RESUMED,
                stage=TaskActivityStage.RUNNING,
                task_id=parent_task_id,
                summary="Clarification received; work resumed.",
                detail=task_query.message,
                details={"task_query_id": task_query.task_query_id},
            )
        self._persist_runtime_state()

    def get_task_queries(self, parent_task_id: str) -> list:
        """Returns the list of TaskQuery objects for a given parent task."""
        return self.task_state.task_to_taskquery_list.get(parent_task_id, [])

    def render_task_query_history_markdown(
        self, task_id: str, task_query_list: Optional[list] = None, *, latest_only: bool = False
    ) -> str:
        """Clarifications for this task, separate from its general execution history.

        Focused plan-step and recovery turns omit general history, but still
        need the person's answers to questions this exact task asked.
        """
        queries = self.get_task_queries(task_id) if task_query_list is None else task_query_list
        if latest_only:
            # One question/answer exchange keeps a focused turn bounded.
            queries = queries[-2:]
        if not queries:
            return ""
        lines = ["", "## TaskQuery History"]
        for query in queries:
            lines.append(f"- `{getattr(query, 'query_type', 'N/A')}`: {getattr(query, 'message', query)}")
            replied = getattr(query, "replied_message", None)
            if replied is not None:
                lines.extend(render_replied_message_lines(replied, subject="clarification answer"))
        return "\n".join(lines) + "\n"

    def get_task_info(self, task_id: str) -> Optional[TaskInfo]:
        """Retrieves the TaskInfo object for a given task ID."""
        return self.task_state.tasks.get(task_id)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Retrieves the Task object for a given task ID."""
        task_info = self.get_task_info(task_id)
        return task_info.task if task_info else None

    def get_parent_task(self, parent_task_id: str) -> Optional[Task]:
        """Retrieves the Task object for a given parent task ID."""
        return self.get_task(parent_task_id)

    def get_subtask(self, subtask_id: str) -> Optional[Task]:
        """Retrieves a subtask Task object."""
        if subtask_id in self.task_state.subtask_to_parent_map:
            return self.get_task(subtask_id)
        self._logger.warning(
            f"Attempted to get subtask {subtask_id}, but it's not registered as a subtask."
        )
        return None

    def add_subtask(
        self,
        parent_task_id: str,
        subtask: Task,
        header: MessageHeader,
        meta: MessageMeta,
    ) -> Optional[TaskInfo]:
        """
        Adds a subtask, linking it to its parent.
        """
        if self.is_execution_cancelled(parent_task_id) or self.is_execution_cancelled(
            subtask.task_id
        ):
            self._logger.warning(
                "Rejected subtask admission for cancelled execution graph. "
                "event=cancelled_subtask_admission_rejected parent_task_id=%s subtask_id=%s",
                parent_task_id,
                subtask.task_id,
            )
            return None
        if parent_task_id not in self.task_state.tasks:
            self._logger.error(
                f"Cannot add subtask {subtask.task_id}. Parent task {parent_task_id} not found."
            )
            return None
        if subtask.task_id in self.task_state.tasks:
            self._logger.warning(
                f"Subtask {subtask.task_id} already exists. Ignoring add request."
            )
            return None

        parent_task_info = self.task_state.tasks[parent_task_id]
        # A subtask works for its parent's verified person, never for a person
        # its own message names.
        task_info = TaskInfo(
            subtask, header, meta, owner_person_id=parent_task_info.owner_person_id
        )
        self.task_state.tasks[subtask.task_id] = task_info
        self.task_state.subtask_to_parent_map[subtask.task_id] = parent_task_id
        siblings = self.task_state.task_to_subtask_list.setdefault(parent_task_id, [])
        siblings.append(task_info)
        try:
            # Subtasks have their own admission boundary before history or
            # Plan/Objective side effects can describe them as accepted work.
            self._persist_runtime_state()
        except Exception as error:
            if _task_row_taken_over(error):
                # The snapshot coordinator already dropped every task, the parent too.
                raise
            self.task_state.tasks.pop(subtask.task_id)
            self.task_state.subtask_to_parent_map.pop(subtask.task_id)
            siblings.remove(task_info)
            if not siblings:
                self.task_state.task_to_subtask_list.pop(parent_task_id)
            raise
        self._ensure_task_plan_link(
            task_info, parent_task_info=parent_task_info, create=False
        )
        parent_objective_id = self._get_task_objective_id_from_context(parent_task_id)
        incoming_objective_id = getattr(meta, "objective_id", None)
        objective_id = incoming_objective_id or parent_objective_id
        if objective_id and self._objectives_enabled():
            self.objective_state.task_to_objective_id[subtask.task_id] = objective_id

            def link_subtask(objective: ObjectiveRecord) -> bool:
                if subtask.task_id in objective.related_task_ids:
                    return False
                objective.related_task_ids.append(subtask.task_id)
                self._append_objective_event(
                    objective,
                    ObjectiveEventType.TASK_LINKED,
                    f"Linked subtask {subtask.task_id} to objective.",
                    task_id=subtask.task_id,
                    user_visible=False,
                    details={"parent_task_id": parent_task_id},
                )
                return True

            self._change_objective(objective_id, link_subtask, step="subtask_linked")
        self._logger.info(
            f"Subtask {subtask.task_id} added for parent {parent_task_id}."
        )

        self.append_history_entry(
            subtask.task_id,
            f"Subtask created for parent {parent_task_id}.",
            entry_type=TaskHistoryType.SUBTASK_EVENT,
            importance=TaskHistoryImportance.NORMAL,
            agent_name=getattr(meta, "to_agent_name", None),
            related_task_id=parent_task_id,
        )
        self.append_history_entry(
            parent_task_id,
            f"Delegated subtask {subtask.task_id} to {getattr(meta, 'to_agent_name', None) or getattr(meta, 'to_named_agent_type', None) or 'another agent'}.",
            entry_type=TaskHistoryType.SUBTASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            related_task_id=subtask.task_id,
            agent_name=getattr(meta, "to_agent_name", None),
            details={"to_named_agent_type": getattr(meta, "to_named_agent_type", None)},
        )
        self._emit_activity(
            activity_type=TaskActivityType.DELEGATED,
            stage=TaskActivityStage.DELEGATING,
            task_id=subtask.task_id,
            parent_task_id=parent_task_id,
            meta=meta,
            summary=(
                f"Subtask created and sent to "
                f"{getattr(meta, 'to_agent_name', None) or getattr(meta, 'to_named_agent_type', None) or 'another agent'}."
            ),
            detail=subtask.problem_statement,
            details={
                "subtask_id": subtask.task_id,
                "to_named_agent_type": getattr(meta, "to_named_agent_type", None),
            },
        )
        self._sync_plan_from_task(parent_task_id, create=False)
        self._sync_plan_from_task(subtask.task_id, create=False)
        self._persist_runtime_state()
        return task_info

    def _bubble_subtask_entries(self, parent_task_id: str, subtask_task: Task):
        parent_task = self.get_task(parent_task_id)
        if not parent_task:
            return

        existing_bubbles = {
            entry.details.get("bubbled_entry_id")
            for entry in parent_task.execution_history
            if entry.details.get("bubbled_entry_id")
        }

        for entry in subtask_task.execution_history:
            if entry.entry_id in existing_bubbles:
                continue
            should_bubble = (
                entry.importance
                in {TaskHistoryImportance.HIGH, TaskHistoryImportance.CRITICAL}
                or bool(entry.tracks_to_plan)
                or entry.source == TaskHistorySource.AGENT
            )
            if not should_bubble:
                continue
            self.append_history_entry(
                parent_task_id,
                f"[Subtask {subtask_task.task_id}] {entry.summary}",
                source=entry.source,
                entry_type=TaskHistoryType.SUBTASK_EVENT,
                importance=entry.importance,
                timestamp=entry.timestamp,
                agent_name=entry.agent_name,
                agent_id=entry.agent_id,
                related_task_id=subtask_task.task_id,
                plan_step_id=entry.plan_step_id,
                tracks_to_plan=entry.tracks_to_plan,
                details={**entry.details, "bubbled_entry_id": entry.entry_id},
            )

    def bubble_subtask_result(self, parent_task_id: str, subtask_id: str) -> None:
        """Bubble the owned result; detached caller snapshots cannot replace task state."""
        updated_subtask_info = self.get_task_info(subtask_id)
        if updated_subtask_info is None:
            return
        if parent_task_id not in self.task_state.task_to_subtask_list:
            self._logger.warning(
                f"Parent task {parent_task_id} not found in subtask list for update."
            )
            return

        subtasks = self.task_state.task_to_subtask_list[parent_task_id]
        for subtask in subtasks:
            if subtask.task.task_id != updated_subtask_info.task.task_id:
                continue

            self._logger.info(
                f"Subtask {updated_subtask_info.task.task_id} for parent {parent_task_id} updated."
            )

            parent_task_info = self.get_task_info(parent_task_id)
            if parent_task_info:
                if updated_subtask_info.state == ProgressState.COMPLETED and getattr(
                    updated_subtask_info.task, "answer", None
                ):
                    self.append_history_entry(
                        parent_task_id,
                        f"Subtask {updated_subtask_info.task.task_id} completed successfully.",
                        entry_type=TaskHistoryType.SUBTASK_EVENT,
                        importance=TaskHistoryImportance.HIGH,
                        related_task_id=updated_subtask_info.task.task_id,
                        agent_name=updated_subtask_info.meta.to_agent_name,
                    )
                elif updated_subtask_info.state == ProgressState.FAILED and getattr(
                    updated_subtask_info.task, "errors", None
                ):
                    if not parent_task_info.task.errors:
                        parent_task_info.task.errors = []
                    parent_task_info.task.errors.extend(
                        [
                            f"Subtask {updated_subtask_info.task.task_id} Error: {err}"
                            for err in updated_subtask_info.task.errors
                        ]
                    )
                    self.append_history_entry(
                        parent_task_id,
                        f"Subtask {updated_subtask_info.task.task_id} failed.",
                        entry_type=TaskHistoryType.ERROR_EVENT,
                        importance=TaskHistoryImportance.CRITICAL,
                        related_task_id=updated_subtask_info.task.task_id,
                        agent_name=updated_subtask_info.meta.to_agent_name,
                        details={"errors": updated_subtask_info.task.errors},
                    )
                elif updated_subtask_info.state == ProgressState.CANCELLED:
                    cancellation_errors = list(
                        updated_subtask_info.task.errors or []
                    ) or ["Delegated subtask was cancelled."]
                    if not parent_task_info.task.errors:
                        parent_task_info.task.errors = []
                    parent_task_info.task.errors.extend(
                        [
                            f"Subtask {updated_subtask_info.task.task_id} Cancelled: {err}"
                            for err in cancellation_errors
                        ]
                    )
                    self.append_history_entry(
                        parent_task_id,
                        f"Subtask {updated_subtask_info.task.task_id} was cancelled.",
                        entry_type=TaskHistoryType.SUBTASK_EVENT,
                        importance=TaskHistoryImportance.HIGH,
                        related_task_id=updated_subtask_info.task.task_id,
                        agent_name=updated_subtask_info.meta.to_agent_name,
                        details={"errors": cancellation_errors},
                    )
                self._bubble_subtask_entries(parent_task_id, updated_subtask_info.task)
            self._persist_runtime_state()
            break

    def get_subtasks(self, parent_task_id: str) -> List[TaskInfo]:
        """Retrieves the list of subtasks for a given parent task."""
        return self.task_state.task_to_subtask_list.get(parent_task_id, [])

    def get_pending_subtasks(
        self, parent_task_id: Optional[str] = None
    ) -> List[TaskInfo]:
        """Return pending subtasks for one parent task or across all parents."""
        if parent_task_id is not None:
            subtasks = self.task_state.task_to_subtask_list.get(parent_task_id, [])
            return [
                subtask
                for subtask in subtasks
                if not self.is_terminal_state(subtask.state)
            ]

        pending: List[TaskInfo] = []
        for subtasks in self.task_state.task_to_subtask_list.values():
            pending.extend(
                subtask
                for subtask in subtasks
                if not self.is_terminal_state(subtask.state)
            )
        return pending

    def find_task_info_by_correlation_id(
        self, correlation_id: Optional[str]
    ) -> Optional[TaskInfo]:
        if not correlation_id:
            return None
        for task_info in self.task_state.tasks.values():
            if getattr(task_info.meta, "msg_correlation_id", None) == correlation_id:
                return task_info
        return None

    def get_parent_task_id(self, subtask_id: str) -> Optional[str]:
        """Gets the parent task ID for a given subtask ID."""
        return self.task_state.subtask_to_parent_map.get(subtask_id)

    def get_parent_task_header(self, parent_task_id: str) -> Optional[MessageHeader]:
        """Retrieves the header for a given parent task ID."""
        task_info = self.get_task_info(parent_task_id)
        return task_info.header if task_info else None

    def get_parent_task_meta(self, parent_task_id: str) -> Optional[MessageMeta]:
        """Retrieves the meta for a given parent task ID."""
        task_info = self.get_task_info(parent_task_id)
        return task_info.meta if task_info else None

    def update_main_task_state(
        self, task_id: str, new_state: ProgressState, timestamp: str
    ) -> bool:
        """
        Updates a task's state and manages local execution tracking.

        `parent_task_id is None` means top-level from this agent's perspective
        only for queue-capacity and waiting metrics. It is not the same as local
        execution ownership: a specialist may receive a delegated task with a
        parent id from another runtime, and that task still needs its own local
        execution context and execution digest.
        """
        task_info = self.get_task_info(task_id)
        if not task_info:
            self._logger.warning(f"Cannot update state for unknown task {task_id}.")
            return False

        if not self.can_transition_task_state(task_id, new_state):
            return False

        if not self.is_runnable_state(new_state):
            from custom_agents.task_queue_safety import remove_queue_entry

            remove_queue_entry(self, task_id)
        previous_state = task_info.state
        task_info.update_state(new_state, timestamp)
        self.append_history_entry(
            task_id,
            f"State changed from {previous_state.value} to {new_state.value}.",
            entry_type=TaskHistoryType.STATE_CHANGE,
            importance=TaskHistoryImportance.NORMAL,
            timestamp=timestamp,
            details={"from_state": previous_state.value, "to_state": new_state.value},
        )
        if self.get_task_info(task_id) is not task_info:
            return False

        is_top_level = task_info.task.parent_task_id is None
        is_locally_executable = self.is_locally_executable_task(task_id)
        if is_locally_executable and self.is_executing_state(new_state):
            if new_state == ProgressState.IN_PROGRESS:
                self.plan_state.final_review_pending_task_ids.discard(task_id)
            if (
                self.task_state.current_in_progress_task_id is not None
                and self.task_state.current_in_progress_task_id != task_id
            ):
                old_task_id = self.task_state.current_in_progress_task_id
                self._logger.warning(
                    f"Preempting task {old_task_id} to start {task_id}."
                )
                old_task_info = self.get_task_info(old_task_id)
                if old_task_info:
                    old_task_info.update_state(ProgressState.PAUSED, timestamp)
                    self.append_history_entry(
                        old_task_id,
                        f"Task paused because task {task_id} was preempted.",
                        entry_type=TaskHistoryType.STATE_CHANGE,
                        importance=TaskHistoryImportance.NORMAL,
                        timestamp=timestamp,
                        related_task_id=task_id,
                        details={"to_state": ProgressState.PAUSED.value},
                    )
                    # Paused work stays tracked until an explicit resume makes
                    # it runnable; it must not block the next queue selection.
                    from custom_agents.task_queue_safety import remove_queue_entry

                    remove_queue_entry(self, old_task_id)
            self.task_state.current_in_progress_task_id = task_id
            if is_top_level:
                self.task_state.waiting_top_level_task_ids.discard(task_id)
        elif is_locally_executable and self.is_blocked_state(new_state):
            if is_top_level:
                self.task_state.waiting_top_level_task_ids.add(task_id)
            if task_id == self.task_state.current_in_progress_task_id:
                self.task_state.current_in_progress_task_id = None
                self._logger.info(
                    f"Task {task_id} is {new_state.value.upper()}. Execution slot released."
                )
        elif (
            is_locally_executable
            and task_id == self.task_state.current_in_progress_task_id
            and not self.is_executing_state(new_state)
        ):
            self.task_state.current_in_progress_task_id = None
            self._logger.info(
                f"Task {task_id} finished processing. No task is currently IN_PROGRESS."
            )
            if not self.is_blocked_state(new_state):
                self.task_state.waiting_top_level_task_ids.discard(task_id)
        elif is_top_level and not self.is_blocked_state(new_state):
            self.task_state.waiting_top_level_task_ids.discard(task_id)

        self._sync_plan_from_task(task_id, create=False)
        self._persist_runtime_state()
        # Saving may release just this task while successfully saving the rest.
        # Its old holder must then skip terminal effects and report no transition.
        if self.get_task_info(task_id) is not task_info:
            return False
        if self.is_terminal_state(new_state) and not self.is_terminal_state(
            previous_state
        ):
            for callback in tuple(self._task_terminal_callbacks):
                try:
                    callback(task_id, new_state, timestamp)
                except Exception:
                    self._logger.exception(
                        "Task terminal callback failed. event=task_terminal_callback_failed "
                        "task_id=%s state=%s",
                        task_id,
                        new_state.value,
                    )
        if new_state == ProgressState.WAITING:
            waiting_on_user = self.is_waiting_on_task_query(task_id)
            self._emit_activity(
                activity_type=(
                    TaskActivityType.WAITING_ON_USER
                    if waiting_on_user
                    else TaskActivityType.WAITING_ON_AGENT
                ),
                stage=TaskActivityStage.WAITING,
                task_id=task_id,
                summary=(
                    "Waiting on clarification from the user."
                    if waiting_on_user
                    else "Waiting on delegated work to complete."
                ),
                detail=(
                    "An unanswered TaskQuery is blocking progress."
                    if waiting_on_user
                    else "The task is blocked on one or more delegated subtasks."
                ),
                details={
                    "from_state": previous_state.value,
                    "to_state": new_state.value,
                },
            )
        elif previous_state == ProgressState.WAITING and self.is_executing_state(
            new_state
        ):
            self._emit_activity(
                activity_type=TaskActivityType.RESUMED,
                stage=TaskActivityStage.RUNNING,
                task_id=task_id,
                summary="Task resumed after a blocking wait.",
                detail=f"State transitioned from {previous_state.value} to {new_state.value}.",
                details={
                    "from_state": previous_state.value,
                    "to_state": new_state.value,
                },
            )
        return True

    def add_task_terminal_callback(
        self,
        callback: Callable[[str, ProgressState, str], None],
    ) -> None:
        """Register one in-process cleanup callback for successful terminal transitions."""

        if callback not in self._task_terminal_callbacks:
            self._task_terminal_callbacks.append(callback)

    def add_task_release_callback(self, callback: Callable[[str], None]) -> None:
        """Register one in-process cleanup callback for a task another agent took over.

        It runs on the task lane before the task is dropped, so the task's
        pending subtasks and waits can still be read. The task does not end:
        its new holder runs it, so nothing may record an outcome or a
        cancellation for the task itself (``RuntimeSnapshotCoordinator._release_taken_tasks``).
        """

        if callback not in self._task_release_callbacks:
            self._task_release_callbacks.append(callback)

    def _run_task_release_callbacks(self, task_id: str) -> None:
        for callback in tuple(self._task_release_callbacks):
            try:
                callback(task_id)
            except Exception:
                # The task is still dropped: its new holder runs it.
                self._logger.exception(
                    "Task release callback failed. event=task_release_callback_failed task_id=%s",
                    task_id,
                )

    def get_task_from_db(
        self,
        task_id: str,
        *,
        owner_agent_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
        include_all_agents: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieves a task and all of its associated subtasks from archival storage.
        """
        try:
            archived = self.task_archive_repository.get_archived_task(
                task_id,
                owner_agent_id=owner_agent_id or self.identity.owner_agent_id,
                include_all_agents=include_all_agents
                or (owner_agent_id is None and self.identity.owner_agent_id is None),
                owner_canonical_user_id=owner_canonical_user_id,
            )
            if not archived:
                return None

            main_task = Task.model_validate_json(archived.task_json)
            subtask_rows = self.task_archive_repository.get_subtask_task_jsons(task_id)
            subtasks = []
            for s_data in subtask_rows:
                try:
                    subtasks.append(Task.model_validate_json(s_data))
                except Exception as e:
                    self._logger.warning(
                        f"Failed to parse a subtask from db for parent {task_id}: {e}"
                    )

            return {
                "main_task": main_task,
                "status": archived.status,
                "subtasks": subtasks,
                "timestamp": archived.timestamp,
                "owner_agent_id": archived.owner_agent_id,
                "owner_agent_name": archived.owner_agent_name,
                "team_id": archived.team_id,
                "owner_canonical_user_id": archived.owner_canonical_user_id,
            }
        except Exception as e:
            self._logger.error(f"Error fetching task {task_id} from database: {e}")
            return None

    def list_archived_tasks(
        self,
        *,
        limit: int = 10,
        status: Optional[str] = None,
        owner_agent_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
        include_all_agents: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        List recently archived tasks from persistent history storage.

        This is the discovery path for historical work when no task is currently
        active in memory and callers need archived task IDs first.
        """
        try:
            rows = self.task_archive_repository.list_archived_tasks(
                limit=limit,
                status=status,
                owner_agent_id=None
                if include_all_agents
                else (owner_agent_id or self.identity.owner_agent_id),
                include_all_agents=include_all_agents,
                owner_canonical_user_id=owner_canonical_user_id,
            )
        except Exception as e:
            self._logger.error(f"Failed to list archived tasks from database: {e}")
            return []

        archived_tasks: List[Dict[str, Any]] = []
        for row in rows:
            try:
                task_data = json.loads(row.task_json)
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to decode archived task payload for {row.task_id}"
                )
                task_data = {}

            answer = task_data.get("answer")
            if isinstance(answer, dict):
                answer_preview = answer.get("answer") or json.dumps(answer, default=str)
            elif answer is None:
                answer_preview = None
            else:
                answer_preview = str(answer)

            if answer_preview and len(answer_preview) > 160:
                answer_preview = answer_preview[:157] + "..."

            archived_tasks.append(
                {
                    "task_id": row.task_id,
                    "owner_agent_id": row.owner_agent_id,
                    "owner_agent_name": row.owner_agent_name,
                    "team_id": row.team_id,
                    "owner_canonical_user_id": row.owner_canonical_user_id,
                    "status": row.status,
                    "timestamp": row.timestamp,
                    "task_type": task_data.get("task_type"),
                    "parent_task_id": task_data.get("parent_task_id"),
                    "problem_statement": task_data.get("problem_statement"),
                    "answer_preview": answer_preview,
                    "conversation_id": row.conversation_id,
                    "turn_index": row.turn_index,
                    "followup_to_task_id": row.followup_to_task_id,
                }
            )

        return archived_tasks

    def get_next_task_to_process(self) -> Optional[str]:
        """Select ready work without allowing a stale blocked entry to stall it."""
        from custom_agents.task_queue_safety import next_ready_task

        return next_ready_task(self)

    def remove_task(self, task_id: str) -> bool:
        """
        Completely removes a task and its associated subtasks (if it's a parent).
        """
        if not self._forget_task(task_id):
            return False
        self._persist_runtime_state()
        return True

    def _forget_task(self, task_id: str) -> bool:
        """Remove a task, its delegated subtasks and its records from memory, without saving.

        ``remove_task`` saves afterwards; the snapshot coordinator calls this
        directly while it saves, for tasks another agent took over.
        """
        if task_id not in self.task_state.tasks:
            self._logger.warning(f"Attempted to remove non-existent task {task_id}.")
            return False

        if self.task_state.current_in_progress_task_id == task_id:
            self.task_state.current_in_progress_task_id = None
            self._logger.info(
                f"Resetting current in-progress task as {task_id} was removed."
            )
        self.task_state.waiting_top_level_task_ids.discard(task_id)

        original_queue_len = len(self.task_state.task_queue)
        self.task_state.task_queue = deque(
            [tid for tid in self.task_state.task_queue if tid != task_id]
        )
        if len(self.task_state.task_queue) < original_queue_len:
            self._logger.info(f"Task {task_id} removed from queue.")
            self._check_queue_backlog_warning()

        subtasks_to_remove = [
            sub_id
            for sub_id, parent_id in self.task_state.subtask_to_parent_map.items()
            if parent_id == task_id
        ]
        for sub_id in subtasks_to_remove:
            self._forget_task(sub_id)

        if task_id in self.task_state.task_to_subtask_list:
            del self.task_state.task_to_subtask_list[task_id]
            self._logger.info(f"Subtask list for parent {task_id} removed.")

        if task_id in self.task_state.subtask_to_parent_map:
            parent_id = self.task_state.subtask_to_parent_map.pop(task_id)
            if parent_id in self.task_state.task_to_subtask_list:
                self.task_state.task_to_subtask_list[parent_id] = [
                    ti
                    for ti in self.task_state.task_to_subtask_list[parent_id]
                    if ti.task.task_id != task_id
                ]
                if not self.task_state.task_to_subtask_list[parent_id]:
                    del self.task_state.task_to_subtask_list[parent_id]
            self._logger.info(
                f"Subtask {task_id} removed from parent map (parent: {parent_id}) and parent's subtask list."
            )

        # Every task-scoped record goes through the one table in
        # task_runtime_state, also on paths that remove a task without ending
        # it (a lease-loss handback, an overtaken scheduled rerun).
        forget_task_records(self.runtime_snapshot_coordinator.state, task_id)

        del self.task_state.tasks[task_id]
        self._logger.debug("Task %s removed from active tracking.", task_id)
        self._check_queue_backlog_warning()
        return True

    def complete_task(
        self, task_id: str, timestamp: str
    ) -> List[AfterActionReportReviewBatchRecord]:
        """Marks a task as completed and archives it to DB.

        Returns the task's after-action report batches whose review should now
        be scheduled (normally at most one: a task works for one person).
        """
        if self.try_transition_task_state(
            task_id,
            allowed_from={
                ProgressState.ACCEPTED,
                ProgressState.IN_PROGRESS,
                ProgressState.WAITING,
                ProgressState.PAUSED,
                ProgressState.PREPARING,
                ProgressState.NOT_STARTED,
                ProgressState.PARTIAL,
                ProgressState.RETRYING,
                ProgressState.UNDEFINED,
                ProgressState.FINALIZING_PLAN,
            },
            to_state=ProgressState.COMPLETED,
            timestamp=timestamp,
            reason="Task completed successfully.",
            source="terminal",
        ):
            self.append_history_entry(
                task_id,
                "Task completed successfully.",
                entry_type=TaskHistoryType.TASK_EVENT,
                importance=TaskHistoryImportance.HIGH,
                timestamp=timestamp,
            )
            self._emit_activity(
                activity_type=TaskActivityType.COMPLETED,
                stage=TaskActivityStage.COMPLETED,
                task_id=task_id,
                summary="Task completed.",
                detail="Task execution finished successfully.",
                created_at=timestamp,
            )
            self.merge_recovery_result_and_resume_original(
                task_id,
                ProgressState.COMPLETED,
                timestamp,
                agent_name=self.identity.owner_agent_name,
                agent_id=self.identity.owner_agent_id,
            )
            self.merge_plan_step_result_and_continue(
                task_id,
                ProgressState.COMPLETED,
                timestamp,
                agent_name=self.identity.owner_agent_name,
                agent_id=self.identity.owner_agent_id,
            )
            self.objective_work_coordinator.finish_for_task(
                task_id,
                ProgressState.COMPLETED,
                timestamp=timestamp,
            )
            objective_id = self.objective_id_for_task_end(task_id)
            self._save_completed_task(task_id, ProgressState.COMPLETED)
            self.review_objective_after_task_terminal(
                task_id,
                ProgressState.COMPLETED,
                objective_id=objective_id,
                timestamp=timestamp,
                summary="Linked task completed successfully.",
            )
            return self.begin_after_action_report_completion_window(
                task_id, timestamp=timestamp
            )
        return []

    def has_pending_subtasks(self, parent_task_id: str) -> bool:
        """Use the same terminal-state rule as pending-subtask enumeration."""
        return bool(self.get_pending_subtasks(parent_task_id))

    def summarize_task_history_markdown(
        self,
        task_id: str,
        task_query_list: Optional[list] = None,
        *,
        max_history_entries: int = 12,
        include_system: bool = True,
        include_agent: bool = True,
        include_execution_digest: bool = True,
    ) -> str:
        """
        Generates a condensed markdown summary of task execution history.

        Args:
            task_id: Task to summarize.
            task_query_list: Optional query list override.
            max_history_entries: Maximum number of history entries to include in the rolling section.
            include_system: Whether to include system-generated history entries.
            include_agent: Whether to include agent-authored history entries.
            include_execution_digest: Whether to embed the standalone execution digest in this summary.
        """
        task_info = self.get_task_info(task_id)
        archived = None
        if not task_info:
            archived = self.get_task_from_db(task_id)
            if not archived:
                return f"## Task {task_id} not found.\n"
            task = archived["main_task"]
            status = archived["status"]
            subtasks = archived["subtasks"]
            header = None
            meta = None
        else:
            task = task_info.task
            status = task_info.state.value
            subtasks = [item.task for item in self.get_subtasks(task_id)]
            header = task_info.header
            meta = task_info.meta

        def _metadata_value(value: object) -> str:
            if value is None:
                return ""
            text = str(value).strip()
            return "" if text in {"", "None", "N/A"} else text

        def format_metadata(
            task_header: Optional[MessageHeader], task_meta: Optional[MessageMeta]
        ) -> str:
            if task_header is None and task_meta is None:
                return ""
            lines: list[str] = []
            from_agent = _metadata_value(getattr(task_meta, "from_agent_name", None))
            if from_agent:
                lines.append(f"- **From Agent:** {from_agent}")
            to_agent = _metadata_value(getattr(task_meta, "to_agent_name", None))
            to_named_agent_type = _metadata_value(
                getattr(task_meta, "to_named_agent_type", None)
            )
            if to_agent:
                lines.append(f"- **To Agent:** {to_agent}")
            if to_named_agent_type:
                lines.append(f"- **To Named Agent Type:** {to_named_agent_type}")
            requester_type = _metadata_value(getattr(task_meta, "requester_type", None))
            if requester_type:
                lines.append(f"- **Requester Type:** {requester_type}")
            timestamp = _metadata_value(getattr(task_header, "timestamp", None))
            if timestamp:
                lines.append(f"- **Envelope Timestamp:** {timestamp}")
            return "\n".join(lines) + ("\n" if lines else "")

        history_entries = self.get_task_history_entries(
            task_id,
            start_index=0,
            limit=None,
            include_system=include_system,
            include_agent=include_agent,
        )
        md = f"# Task Execution Summary: `{task_id}`\n\n"
        md += "## Top Level Task\n"
        md += f"- **Current State:** {status}\n"
        if task.parent_task_id:
            md += "- **Is Subtask:** True\n"
            md += f"- **Parent Task ID:** {task.parent_task_id}\n"
        md += format_metadata(header, meta)
        plan_text = task.task_plan or ""
        if (
            getattr(task, "execution_digest", None)
            and task.execution_digest.plan_summary
            and not plan_text
        ):
            plan_text = task.execution_digest.plan_summary
        if plan_text:
            md += f"- **Plan:** {plan_text}\n"
        history_sources = []
        if include_system:
            history_sources.append("system")
        if include_agent:
            history_sources.append("agent")
        md += (
            f"- **History Entries:** {len(history_entries)}"
            f" ({', '.join(history_sources) if history_sources else 'no sources'})\n"
        )
        if getattr(task, "errors", None):
            md += "- **Errors:**\n"
            for err in task.errors:
                md += f"  - {err}\n"
        if getattr(task, "answer", None):
            md += f"- **Answer:** {task.answer}\n"

        digest_markdown = (
            self.render_execution_digest_markdown(task_id)
            if include_execution_digest
            else ""
        )
        if digest_markdown:
            md += "\n"
            md += digest_markdown
            md += "\n"

        md += "\n## Rolling History\n"
        md += self.build_compact_history(
            task_id,
            max_entries=max_history_entries,
            include_system=include_system,
            include_agent=include_agent,
        )
        md += "\n"

        md += self.render_task_query_history_markdown(task_id, task_query_list)

        if subtasks:
            md += "\n## Subtasks\n"
            for subtask in subtasks:
                state = subtask.task_state[-1][0] if subtask.task_state else "unknown"
                md += f"- **{subtask.task_id}** ({state})"
                if getattr(subtask, "answer", None):
                    md += f" Answer: {subtask.answer}"
                md += "\n"

        return md
