"""Finite callback contracts for workflow collaborators; no host attribute proxy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from custom_messages.contracts.runtime_snapshot_v3 import RecoverablePauseRecord
from custom_agents.task_records import TaskInfo
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import ExecutionDigest, Task, TaskHistoryEntry
from custom_messages.records.objectives import ObjectiveEventRecord, ObjectiveRecord
from custom_messages.records.plans import PlanRecord, PlanStepRecord
from custom_messages.enums import (
    ObjectiveEventType,
    ProgressState,
    TaskActivityStage,
    TaskActivityType,
    TaskHistoryImportance,
    TaskHistorySource,
    TaskHistoryType,
)


class AppendObjectiveEventOperation(Protocol):
    def __call__(
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
    ) -> ObjectiveEventRecord: ...


class BuildPlanStepProblemStatementOperation(Protocol):
    def __call__(
        self,
        *,
        root_problem_statement: Optional[str],
        step: PlanStepRecord,
        plan_steps: List[PlanStepRecord],
    ) -> str: ...


class CompactPlanStepTextOperation(Protocol):
    def __call__(self, value: Any, *, max_chars: int = 1200) -> str: ...


class EmitActivityOperation(Protocol):
    def __call__(
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
    ) -> None: ...


class EnqueueNextPlanStepTaskOperation(Protocol):
    def __call__(
        self,
        plan: PlanRecord,
        *,
        root_task_info: TaskInfo,
        timestamp: Optional[str] = None,
    ) -> Optional[TaskInfo]: ...


class EnsureExecutionDigestOperation(Protocol):
    def __call__(self, task: Task) -> ExecutionDigest: ...


class EnsureTaskPlanLinkOperation(Protocol):
    def __call__(
        self,
        task_info: TaskInfo,
        parent_task_info: Optional[TaskInfo] = None,
        *,
        create: bool = False,
    ) -> Optional[str]: ...


class GetObjectiveForPlanIdOperation(Protocol):
    def __call__(self, plan_id: Optional[str]) -> Optional[ObjectiveRecord]: ...


class GetTaskObjectiveRecordOperation(Protocol):
    def __call__(self, task_id: Optional[str]) -> Optional[ObjectiveRecord]: ...


class GetTopLevelTaskIdOperation(Protocol):
    def __call__(self, task_id: Optional[str]) -> Optional[str]: ...


class NewObjectiveIdOperation(Protocol):
    def __call__(self) -> str: ...


class NewPlanIdOperation(Protocol):
    def __call__(self) -> str: ...


class ObjectivesEnabledOperation(Protocol):
    def __call__(self) -> bool: ...


class PersistRuntimeStateOperation(Protocol):
    def __call__(self) -> None: ...


class PlansEnabledOperation(Protocol):
    def __call__(self) -> bool: ...


class ObjectiveChange(Protocol):
    """Edit the stored Objective in place inside the store's write transaction; False when there is nothing to save.

    It only edits the record: it never writes another store.
    """

    def __call__(self, objective: ObjectiveRecord, /) -> bool: ...


class ChangeObjectiveOperation(Protocol):
    """Change the stored Objective for a step that goes on without it.

    ``ObjectiveCoordinator.change_objective`` with ``step``: the store applies
    ``change`` to the stored record inside one write transaction, so nothing
    stored since an earlier read is lost and the step is never refused for
    another writer's change. A failed save is logged with ``step`` and returns
    None, as does a missing Objective.
    """

    def __call__(
        self, objective_id: str, change: ObjectiveChange, *, step: str
    ) -> Optional[ObjectiveRecord]: ...


class SavePlanOperation(Protocol):
    def __call__(self, plan: PlanRecord) -> bool: ...


class SyncPlanFromTaskOperation(Protocol):
    def __call__(
        self, task_id: str, *, create: bool = False
    ) -> Optional[PlanRecord]: ...


class AddTaskOperation(Protocol):
    def __call__(
        self,
        task: Task,
        header: MessageHeader,
        meta: MessageMeta,
        *,
        owner_person_id: Optional[str],
        handled_at: Optional[str] = None,
        handled_queue_name: Optional[str] = None,
    ) -> bool: ...


class AppendHistoryEntryOperation(Protocol):
    def __call__(
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
    ) -> Optional[TaskHistoryEntry]: ...


class AppendHistoryEntryToArchivedTaskOperation(Protocol):
    def __call__(
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
    ) -> Optional[TaskHistoryEntry]: ...


class DurableManagerScopeIdOperation(Protocol):
    def __call__(self) -> Optional[str]: ...


class EnqueueTaskOperation(Protocol):
    def __call__(self, task_id: str, *, front: bool = False) -> bool: ...


class FailTaskOperation(Protocol):
    def __call__(self, task_id: str, error_msg: str, timestamp: str) -> bool: ...


class GetExecutionDigestOperation(Protocol):
    def __call__(self, task_id: str) -> Optional[ExecutionDigest]: ...


class GetObjectiveOperation(Protocol):
    def __call__(self, objective_id: str) -> Optional[ObjectiveRecord]: ...


class GetPendingRecoverablePauseOperation(Protocol):
    def __call__(self, task_id: str) -> Optional[RecoverablePauseRecord]: ...


class GetPlanOperation(Protocol):
    def __call__(self, plan_id: str) -> Optional[PlanRecord]: ...


class ForgetTaskOperation(Protocol):
    def __call__(self, task_id: str) -> bool: ...


class NotifyTaskReleasedOperation(Protocol):
    def __call__(self, task_id: str) -> None: ...


class GetTaskFromDbOperation(Protocol):
    def __call__(
        self,
        task_id: str,
        *,
        owner_agent_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
        include_all_agents: bool = False,
    ) -> Optional[Dict[str, Any]]: ...


class GetTaskInfoOperation(Protocol):
    def __call__(self, task_id: str) -> Optional[TaskInfo]: ...


class GetTaskObjectiveIdOperation(Protocol):
    def __call__(self, task_id: str) -> Optional[str]: ...


class GetTaskPlanIdOperation(Protocol):
    def __call__(self, task_id: str) -> Optional[str]: ...


class HasPendingSubtasksOperation(Protocol):
    def __call__(self, parent_task_id: str) -> bool: ...


class IsExecutionCancelledOperation(Protocol):
    def __call__(self, task_id: Optional[str]) -> bool: ...


class IsFinalReviewPendingOperation(Protocol):
    def __call__(self, task_id: str) -> bool: ...


class IsPlanExecutionRootActiveOperation(Protocol):
    def __call__(self, root_task_id: str) -> bool: ...


class IsTerminalStateOperation(Protocol):
    def __call__(self, state: ProgressState) -> bool: ...


class IsWaitingOnTaskQueryOperation(Protocol):
    def __call__(self, parent_task_id: str) -> bool: ...


class SetTaskPlanOperation(Protocol):
    def __call__(
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
    ) -> Optional[ExecutionDigest]: ...


class UpdateMainTaskStateOperation(Protocol):
    def __call__(
        self, task_id: str, new_state: ProgressState, timestamp: str
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AfterActionReportCoordinatorOperations:
    persist_runtime_state: PersistRuntimeStateOperation
    append_history_entry: AppendHistoryEntryOperation
    append_history_entry_to_archived_task: AppendHistoryEntryToArchivedTaskOperation
    get_task_from_db: GetTaskFromDbOperation
    get_task_info: GetTaskInfoOperation


@dataclass(frozen=True, slots=True)
class PlanCoordinatorOperations:
    append_objective_event: AppendObjectiveEventOperation
    build_plan_step_problem_statement: BuildPlanStepProblemStatementOperation
    compact_plan_step_text: CompactPlanStepTextOperation
    emit_activity: EmitActivityOperation
    ensure_execution_digest: EnsureExecutionDigestOperation
    get_objective_for_plan_id: GetObjectiveForPlanIdOperation
    get_task_objective_record: GetTaskObjectiveRecordOperation
    get_top_level_task_id: GetTopLevelTaskIdOperation
    new_plan_id: NewPlanIdOperation
    persist_runtime_state: PersistRuntimeStateOperation
    plans_enabled: PlansEnabledOperation
    change_objective: ChangeObjectiveOperation
    add_task: AddTaskOperation
    append_history_entry: AppendHistoryEntryOperation
    append_history_entry_to_archived_task: AppendHistoryEntryToArchivedTaskOperation
    durable_manager_scope_id: DurableManagerScopeIdOperation
    enqueue_task: EnqueueTaskOperation
    get_execution_digest: GetExecutionDigestOperation
    get_task_from_db: GetTaskFromDbOperation
    get_task_info: GetTaskInfoOperation
    get_task_objective_id: GetTaskObjectiveIdOperation
    is_waiting_on_task_query: IsWaitingOnTaskQueryOperation
    update_main_task_state: UpdateMainTaskStateOperation


@dataclass(frozen=True, slots=True)
class RuntimeOperationsCoordinatorOperations:
    persist_runtime_state: PersistRuntimeStateOperation
    append_history_entry: AppendHistoryEntryOperation
    get_pending_recoverable_pause: GetPendingRecoverablePauseOperation
    get_task_info: GetTaskInfoOperation
    get_task_plan_id: GetTaskPlanIdOperation
    has_pending_subtasks: HasPendingSubtasksOperation
    is_final_review_pending: IsFinalReviewPendingOperation
    is_plan_execution_root_active: IsPlanExecutionRootActiveOperation
    is_terminal_state: IsTerminalStateOperation
    is_waiting_on_task_query: IsWaitingOnTaskQueryOperation
    update_main_task_state: UpdateMainTaskStateOperation


@dataclass(frozen=True, slots=True)
class RuntimeSnapshotCoordinatorOperations:
    emit_activity: EmitActivityOperation
    ensure_task_plan_link: EnsureTaskPlanLinkOperation
    forget_task: ForgetTaskOperation
    notify_task_released: NotifyTaskReleasedOperation
    objectives_enabled: ObjectivesEnabledOperation
    plans_enabled: PlansEnabledOperation
    sync_plan_from_task: SyncPlanFromTaskOperation
    is_execution_cancelled: IsExecutionCancelledOperation
    get_task_from_db: GetTaskFromDbOperation


@dataclass(frozen=True, slots=True)
class ObjectiveCoordinatorOperations:
    emit_activity: EmitActivityOperation
    new_objective_id: NewObjectiveIdOperation
    objectives_enabled: ObjectivesEnabledOperation
    persist_runtime_state: PersistRuntimeStateOperation
    save_plan: SavePlanOperation
    sync_plan_from_task: SyncPlanFromTaskOperation
    add_task: AddTaskOperation
    append_history_entry: AppendHistoryEntryOperation
    durable_manager_scope_id: DurableManagerScopeIdOperation
    fail_task: FailTaskOperation
    get_plan: GetPlanOperation
    get_task_from_db: GetTaskFromDbOperation
    get_task_info: GetTaskInfoOperation
    get_task_plan_id: GetTaskPlanIdOperation
    is_terminal_state: IsTerminalStateOperation
    set_task_plan: SetTaskPlanOperation


@dataclass(frozen=True, slots=True)
class ObjectiveWorkSessionCoordinatorOperations:
    append_objective_event: AppendObjectiveEventOperation
    emit_activity: EmitActivityOperation
    enqueue_next_plan_step_task: EnqueueNextPlanStepTaskOperation
    objectives_enabled: ObjectivesEnabledOperation
    persist_runtime_state: PersistRuntimeStateOperation
    change_objective: ChangeObjectiveOperation
    save_plan: SavePlanOperation
    durable_manager_scope_id: DurableManagerScopeIdOperation
    get_objective: GetObjectiveOperation
    get_plan: GetPlanOperation
    get_task_info: GetTaskInfoOperation


@dataclass(frozen=True, slots=True)
class SolutionVariationCoordinatorOperations:
    get_top_level_task_id: GetTopLevelTaskIdOperation
    plans_enabled: PlansEnabledOperation
    append_history_entry: AppendHistoryEntryOperation
    append_history_entry_to_archived_task: AppendHistoryEntryToArchivedTaskOperation
    durable_manager_scope_id: DurableManagerScopeIdOperation
    get_plan: GetPlanOperation
    get_task_info: GetTaskInfoOperation
    get_task_objective_id: GetTaskObjectiveIdOperation
    get_task_plan_id: GetTaskPlanIdOperation
