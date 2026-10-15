"""Operator-control and durable replay guards shared by TaskManager."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from custom_agents.durable_owner_scope import manager_durable_scope_id
from custom_agents.runtime_state_store import RuntimeStateStore
from custom_agents.task_manager_runtime_operations import RuntimeOperationsCoordinator
from custom_agents.task_records import TaskInfo
from custom_messages.enums import ProgressState


class TaskManagerRuntimeControlMixin:
    """Keep runtime-control behavior out of the already-large TaskManager facade."""

    _tasks: Dict[str, TaskInfo]
    _team_id: Optional[str]
    _owner_agent_id: Optional[str]
    _owner_named_agent_type: Optional[str]
    _logger: logging.Logger
    runtime_state_store: RuntimeStateStore
    runtime_operations_coordinator: RuntimeOperationsCoordinator

    def durable_manager_scope_id(self) -> Optional[str]:
        """Return the stable persistence owner for manager-owned durable intent."""

        return manager_durable_scope_id(
            team_id=self.identity.team_id,
            named_agent_type=self.identity.owner_named_agent_type,
            fallback_agent_id=self.identity.owner_agent_id,
        )

    def get_execution_graph_task_infos(self, task_id: str) -> List[TaskInfo]:
        """Return every tracked task attached to the same execution root."""

        root_task_id = (
            self.runtime_operations_coordinator.resolve_root_task_id(task_id)
            or task_id
        )
        return [
            task_info
            for candidate_id, task_info in self.task_state.tasks.items()
            if (
                self.runtime_operations_coordinator.resolve_root_task_id(
                    candidate_id
                )
                or candidate_id
            )
            == root_task_id
        ]

    def get_root_task_queue_positions(self) -> dict[str, int]:
        """Return execution order without exposing the mutable task queue."""
        positions: dict[str, int] = {}
        for task_id in self.task_state.task_queue:
            root_id = self.resolve_root_task_id(task_id) or task_id
            if root_id not in positions:
                positions[root_id] = len(positions) + 1
        return positions

    def can_transition_task_state(self, task_id: str, state: ProgressState) -> bool:
        """Reject execution selected before a concurrent stop persisted its barrier."""
        if not self.is_executing_state(state) or not self.is_execution_cancelled(task_id):
            return True
        self._logger.info(
            "Cancelled task cannot start. event=cancelled_task_start_rejected task_id=%s",
            task_id,
        )
        return False

    def record_execution_graph_cancelled(
        self,
        *,
        root_task_id: str,
        task_ids: List[str],
        canonical_user_id: Optional[str],
        command_id: str,
        reason: str,
        cancelled_at: str,
    ) -> None:
        """Persist cancellation before signalling or terminalizing local tasks."""

        if not self.identity.team_id:
            raise RuntimeError("Runtime cancellation requires a configured team id.")
        self.runtime_state_store.record_task_cancellations(
            team_id=self.identity.team_id,
            root_task_id=root_task_id,
            task_ids=task_ids,
            canonical_user_id=canonical_user_id,
            command_id=command_id,
            reason=reason,
            cancelled_at=cancelled_at,
        )

    def is_execution_cancelled(self, task_id: Optional[str]) -> bool:
        """Fail closed when durable cancellation lookup says this work was stopped."""

        if not task_id or not self.identity.team_id:
            return False
        root_task_id = (
            self.runtime_operations_coordinator.resolve_root_task_id(task_id)
            or task_id
        )
        try:
            return any(
                self.runtime_state_store.get_task_cancellation(
                    team_id=self.identity.team_id,
                    task_id=candidate_id,
                )
                is not None
                for candidate_id in dict.fromkeys((str(task_id), root_task_id))
            )
        except Exception:
            self._logger.exception(
                "Cancellation admission check failed closed. "
                "task_id=%s root_task_id=%s team_id=%s",
                task_id,
                root_task_id,
                self.identity.team_id,
            )
            return True


__all__ = ["TaskManagerRuntimeControlMixin"]
