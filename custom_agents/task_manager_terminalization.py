"""Compare-and-set terminal Task operations shared by TaskManager."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from custom_agents.task_records import TaskInfo
from custom_messages.enums import (
    ProgressState,
    TaskActivityStage,
    TaskActivityType,
    TaskHistoryImportance,
    TaskHistoryType,
)


class TaskManagerTerminalizationMixin:
    """Apply terminal side effects only after the state transition succeeds."""

    _logger: logging.Logger
    _owner_agent_id: Optional[str]
    _owner_agent_name: Optional[str]
    objective_work_coordinator: Any
    get_task_info: Callable[[str], Optional[TaskInfo]]
    is_terminal_state: Callable[[ProgressState], bool]
    try_transition_task_state: Callable[..., bool]
    append_history_entry: Callable[..., Any]
    _emit_activity: Callable[..., Any]
    merge_recovery_result_and_resume_original: Callable[..., Any]
    merge_plan_step_result_and_continue: Callable[..., Any]
    _save_completed_task: Callable[[str, ProgressState], Any]
    review_objective_after_task_terminal: Callable[..., Any]

    def fail_task(self, task_id: str, error_msg: str, timestamp: str) -> bool:
        """Mark a task failed and emit terminal effects after a successful CAS."""

        task_info = self.get_task_info(task_id)
        if not task_info:
            self._logger.warning("Tried to fail non-existent task %s", task_id)
            return False
        if self.is_terminal_state(task_info.state):
            self._logger.warning(
                "Ignoring fail_task for terminal task %s in state %s.",
                task_id,
                task_info.state.value,
            )
            return False

        transitioned = self.try_transition_task_state(
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
            to_state=ProgressState.FAILED,
            timestamp=timestamp,
            reason=error_msg,
            source="terminal",
        )
        if not transitioned:
            self._logger.warning(
                "Task %s could not transition to failed; terminal side effects were skipped.",
                task_id,
            )
            return False

        task_info.task.errors = list(task_info.task.errors or [])
        task_info.task.errors.append(f"{timestamp}: {error_msg}")
        self.append_history_entry(
            task_id,
            f"Task failed: {error_msg}",
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.CRITICAL,
            timestamp=timestamp,
            details={"error": error_msg},
        )
        self._emit_activity(
            activity_type=TaskActivityType.FAILED,
            stage=TaskActivityStage.FAILED,
            task_id=task_id,
            summary="Task failed.",
            detail=error_msg,
            details={"error": error_msg},
            created_at=timestamp,
        )
        self.merge_recovery_result_and_resume_original(
            task_id,
            ProgressState.FAILED,
            timestamp,
            agent_name=self.identity.owner_agent_name,
            agent_id=self.identity.owner_agent_id,
        )
        self.merge_plan_step_result_and_continue(
            task_id,
            ProgressState.FAILED,
            timestamp,
            agent_name=self.identity.owner_agent_name,
            agent_id=self.identity.owner_agent_id,
        )
        self.objective_work_coordinator.finish_for_task(
            task_id,
            ProgressState.FAILED,
            timestamp=timestamp,
        )
        self._save_completed_task(task_id, ProgressState.FAILED)
        self.review_objective_after_task_terminal(
            task_id,
            ProgressState.FAILED,
            timestamp=timestamp,
            summary=error_msg,
        )
        return True

    def cancel_task(
        self,
        task_id: str,
        reason: str,
        timestamp: str,
        *,
        review_objective: bool = True,
    ) -> bool:
        """Mark a task cancelled and emit terminal effects after a successful CAS."""

        task_info = self.get_task_info(task_id)
        if not task_info:
            self._logger.warning("Tried to cancel non-existent task %s", task_id)
            return False
        if self.is_terminal_state(task_info.state):
            self._logger.warning(
                "Ignoring cancel_task for terminal task %s in state %s.",
                task_id,
                task_info.state.value,
            )
            return False

        transitioned = self.try_transition_task_state(
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
            to_state=ProgressState.CANCELLED,
            timestamp=timestamp,
            reason=reason,
            source="terminal",
        )
        if not transitioned:
            self._logger.warning(
                "Task %s could not transition to cancelled; terminal side effects were skipped.",
                task_id,
            )
            return False

        task_info.task.errors = list(task_info.task.errors or [])
        task_info.task.errors.append(f"{timestamp}: {reason}")
        self.append_history_entry(
            task_id,
            f"Task cancelled: {reason}",
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            timestamp=timestamp,
            details={"reason": reason},
        )
        self._emit_activity(
            activity_type=TaskActivityType.CANCELLED,
            stage=TaskActivityStage.CANCELLED,
            task_id=task_id,
            summary="Task cancelled.",
            detail=reason,
            details={"reason": reason},
            created_at=timestamp,
        )
        self.objective_work_coordinator.finish_for_task(
            task_id,
            ProgressState.CANCELLED,
            timestamp=timestamp,
        )
        self._save_completed_task(task_id, ProgressState.CANCELLED)
        if review_objective:
            self.review_objective_after_task_terminal(
                task_id,
                ProgressState.CANCELLED,
                timestamp=timestamp,
                summary=reason,
            )
        return True


__all__ = ["TaskManagerTerminalizationMixin"]
