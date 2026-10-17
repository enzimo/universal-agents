"""Runtime snapshot export and restore coordination for TaskManager."""

from __future__ import annotations

from custom_agents.runtime_snapshot_codec import (
    FINISHED_BY_ANOTHER_AGENT,
    HELD_BY_ANOTHER_AGENT,
    SnapshotSource,
    capture_runtime_snapshot,
    capture_task,
    restore_snapshot_state,
)
from custom_messages.contracts.runtime_snapshot_v3 import decode_runtime_snapshot

from custom_agents.task_manager_ports import RuntimeSnapshotCoordinatorOperations
from custom_agents.task_runtime_state import ExecutionRuntimeState
from custom_agents.task_runtime_state import RuntimeOwnerIdentity
from custom_agents.task_runtime_state import ObjectiveRuntimeState
from custom_agents.task_runtime_state import PlanRuntimeState
from custom_agents.task_runtime_state import TaskRuntimeState
import logging
from storage.runtime_state_repository import RuntimeStateRepository
from custom_agents.task_runtime_state import WorkflowRuntimeState

import json
from typing import Any, Dict, List, Optional
from custom_messages.contracts.schedule_worker_v1 import ScheduleOccurrenceV1

from custom_agents.runtime_state_store import TaskRuntimeSnapshotRow
from storage.task_custody_repository import TaskCustodyTakeover
from custom_messages.enums import TaskActivityStage, TaskActivityType




class RuntimeSnapshotCoordinator:
    """Persist and restore TaskManager active-runtime snapshots."""

    def __init__(
        self,
        *,
        operations: RuntimeSnapshotCoordinatorOperations,
        execution_state: ExecutionRuntimeState,
        identity: RuntimeOwnerIdentity,
        objective_state: ObjectiveRuntimeState,
        plan_state: PlanRuntimeState,
        task_state: TaskRuntimeState,
        logger: logging.Logger,
        runtime_state_store: RuntimeStateRepository,
    ) -> None:
        self.operations = operations
        self.execution_state = execution_state
        self.identity = identity
        self.objective_state = objective_state
        self.plan_state = plan_state
        self.task_state = task_state
        self._logger = logger
        self.runtime_state_store = runtime_state_store
        self.state = WorkflowRuntimeState(
            task_state, plan_state, objective_state, execution_state
        )


    def _export_runtime_state(self) -> Dict[str, Any]:
        return capture_runtime_snapshot(
            self.state,
            plans_enabled=self.operations.plans_enabled(),
            objectives_enabled=self.operations.objectives_enabled(),
        ).model_dump(mode="json")

    def _persist_runtime_state(
        self,
        *,
        schedule_admission: "ScheduleOccurrenceV1 | None" = None,
        custody_takeover: TaskCustodyTakeover | None = None,
    ) -> None:
        if not self.identity.owner_agent_id:
            if schedule_admission is not None:
                raise ValueError("Schedule admission requires durable runtime identity")
            return

        try:
            if not self.task_state.tasks:
                self.runtime_state_store.clear_task_runtime(self.identity.owner_agent_id)
                return

            runtime_state_json = json.dumps(self._export_runtime_state())
            # Only the options in use are passed, so stores without them still work.
            options: Dict[str, Any] = {}
            if schedule_admission is not None:
                options["schedule_admission"] = schedule_admission
            if custody_takeover is not None:
                options["custody_takeover"] = custody_takeover
            self.runtime_state_store.save_task_runtime(
                owner_agent_id=self.identity.owner_agent_id,
                owner_agent_name=self.identity.owner_agent_name,
                team_id=self.identity.team_id,
                owner_named_agent_type=self.identity.owner_named_agent_type,
                runtime_state_json=runtime_state_json,
                **options,
            )
        except Exception as e:
            self._logger.error(f"Failed to persist active runtime state: {e}")
            raise

    def persist_task_history(self, task_id: str) -> None:
        if not self.identity.owner_agent_id:
            return
        self.runtime_state_store.save_task_runtime_task(
            owner_agent_id=self.identity.owner_agent_id,
            task=capture_task(self.task_state.tasks[task_id]),
            task_plan_links=self.plan_state.task_to_plan_id if self.operations.plans_enabled() else {},
            task_objective_links=self.objective_state.task_to_objective_id if self.operations.objectives_enabled() else {},
            plan_objective_links=self.objective_state.plan_to_objective_id if self.operations.objectives_enabled() else {},
        )

    def get_stored_runtime_team_id(self) -> Optional[str]:
        if not self.identity.owner_agent_id:
            return None
        return self.runtime_state_store.get_task_runtime_team_id(self.identity.owner_agent_id)



    def _restore_runtime_rows(self, rows: List[TaskRuntimeSnapshotRow]) -> Optional[Dict[str, Any]]:
        if not rows:
            return None
        if not self.identity.owner_agent_id:
            self._logger.warning("Cannot restore runtime state before owner identity is configured.")
            return None
        rows = list(rows)
        if self.identity.owner_agent_id not in {row.owner_agent_id for row in rows}:
            rows.extend(self.runtime_state_store.load_task_runtime_by_owner(self.identity.owner_agent_id))
        previous_state = self.state.checkpoint()
        committed = False
        try:
            sources = [SnapshotSource(row.owner_agent_id, row.updated_at, decode_runtime_snapshot(row.runtime_state_json)) for row in rows]
            cancelled = {
                item.task_id for source in sources for item in source.snapshot.tasks
                if self.operations.is_execution_cancelled(item.task_id)
            }
            restored = restore_snapshot_state(
                sources, cancelled=cancelled, plans_enabled=self.operations.plans_enabled(),
                objectives_enabled=self.operations.objectives_enabled(), logger=self._logger,
                taken_elsewhere=self._taken_by_other_agents(sources, {row.owner_agent_id for row in rows}),
            )
            self.state.restore(restored)
            self.runtime_state_store.commit_task_runtime_recovery(
                sources=rows,
                replacement=TaskRuntimeSnapshotRow(
                    owner_agent_id=self.identity.owner_agent_id,
                    owner_agent_name=self.identity.owner_agent_name,
                    team_id=self.identity.team_id,
                    owner_named_agent_type=self.identity.owner_named_agent_type,
                    runtime_state_json=json.dumps(self._export_runtime_state()), updated_at=None,
                ),
            )
            committed = True
            self._reconcile_restored_plans()
            self._persist_runtime_state()
            self._logger.info("Restored %s active tasks for %s", len(restored.task_state.tasks), self.identity.owner_agent_id)
            return {
                "stored_team_id": rows[0].team_id,
                "task_count": len(restored.task_state.tasks),
                "source_owner_agent_ids": [row.owner_agent_id for row in rows],
                "top_level_task_ids": sorted(task_id for task_id, info in restored.task_state.tasks.items() if info.task.parent_task_id is None),
            }
        except Exception:
            if not committed:
                self.state.restore(previous_state)
            self._logger.exception(
                "Active runtime restoration did not finish",
                extra={"event": "runtime_restore_failed", "owner_agent_id": self.identity.owner_agent_id,
                       "custody_committed": committed},
            )
            raise

    def _taken_by_other_agents(self, sources: List[SnapshotSource], source_owner_ids: set[str]) -> dict[str, str]:
        """Saved tasks another agent of the team holds or finished; recovery must not adopt them.

        That agent took the task over after this snapshot's owner stopped
        (``custom_agents.task_custody``), and runs or ran it; adopting the
        saved copy as well ran it twice (2026-10-17). Returns task id -> reason.
        """
        team_id = self.identity.team_id
        if not team_id:
            return {}
        executed = sorted({
            item.task_id for source in sources for item in source.snapshot.tasks
            if item.task_id not in source.snapshot.subtask_to_parent_map
        })
        allowed = source_owner_ids | {self.identity.owner_agent_id}
        taken = {
            task_id: HELD_BY_ANOTHER_AGENT
            for task_id, row in self.runtime_state_store.get_task_custody(team_id, executed).items()
            if row.holder_agent_id not in allowed
        }
        for task_id in executed:
            if task_id in taken:
                continue
            archived = self.operations.get_task_from_db(task_id, include_all_agents=True)
            if archived is not None and archived.get("team_id") == team_id and archived.get("owner_agent_id") not in allowed:
                taken[task_id] = FINISHED_BY_ANOTHER_AGENT
        return taken

    def _reconcile_restored_plans(self) -> None:
        if not self.operations.plans_enabled():
            return
        for task_id, info in self.task_state.tasks.items():
            parent_id = self.task_state.subtask_to_parent_map.get(task_id) or info.task.parent_task_id
            self.operations.ensure_task_plan_link(
                info, parent_task_info=self.task_state.tasks.get(parent_id) if parent_id else None, create=False,
            )
        if self.operations.objectives_enabled():
            for task_id, plan_id in self.plan_state.task_to_plan_id.items():
                objective_id = self.objective_state.plan_to_objective_id.get(plan_id)
                if objective_id:
                    self.objective_state.task_to_objective_id.setdefault(task_id, objective_id)
        for task_id in list(self.task_state.tasks):
            self.operations.sync_plan_from_task(task_id, create=False)

    def restore_runtime_state(self) -> Optional[Dict[str, Any]]:
        if not self.identity.owner_agent_id:
            self._logger.warning(
                "Cannot restore runtime state before owner identity is configured."
            )
            return None
        rows = self.runtime_state_store.load_task_runtime_by_owner(
            self.identity.owner_agent_id
        )
        restored = self._restore_runtime_rows(rows)
        if restored and restored.get("task_count"):
            for task_id in restored.get("top_level_task_ids", []):
                self.operations.emit_activity(
                    activity_type=TaskActivityType.WORKER_CLAIMED,
                    stage=TaskActivityStage.RUNNING,
                    task_id=task_id,
                    summary="Worker claimed persisted task state after restart.",
                    detail="Recovered tracked task state owned by this worker from durable runtime storage.",
                    details={
                        "source_owner_agent_ids": restored.get(
                            "source_owner_agent_ids", []
                        ),
                        "restored_task_count": restored.get("task_count"),
                    },
                )
        return restored

    def restore_runtime_snapshot_rows(
        self, rows: List[TaskRuntimeSnapshotRow]
    ) -> Optional[Dict[str, Any]]:
        """Restore read runtime snapshot rows and commit their custody into in-memory task state."""
        return self._restore_runtime_rows(rows)

    def restore_runtime_state_for_role_takeover(self) -> Optional[Dict[str, Any]]:
        if not self.identity.team_id or not self.identity.owner_named_agent_type:
            return None
        rows = self.runtime_state_store.peek_task_runtime_by_role(
            self.identity.team_id,
            self.identity.owner_named_agent_type,
        )
        restored = self._restore_runtime_rows(rows)
        if restored and restored.get("task_count"):
            for task_id in restored.get("top_level_task_ids", []):
                self.operations.emit_activity(
                    activity_type=TaskActivityType.WORKER_RECLAIMED,
                    stage=TaskActivityStage.RUNNING,
                    task_id=task_id,
                    summary="Recovered work from another worker instance.",
                    detail="This worker reclaimed persisted runtime state during same-role takeover.",
                    details={
                        "source_owner_agent_ids": restored.get(
                            "source_owner_agent_ids", []
                        ),
                        "restored_task_count": restored.get("task_count"),
                    },
                )
        return restored

    def restore_runtime_state_for_owner_ids(
        self, owner_agent_ids: List[str]
    ) -> Optional[Dict[str, Any]]:
        rows: List[TaskRuntimeSnapshotRow] = []
        for owner_agent_id in owner_agent_ids:
            rows.extend(
                self.runtime_state_store.load_task_runtime_by_owner(owner_agent_id)
            )
        restored = self._restore_runtime_rows(rows)
        if restored and restored.get("task_count"):
            for task_id in restored.get("top_level_task_ids", []):
                self.operations.emit_activity(
                    activity_type=TaskActivityType.WORKER_RECLAIMED,
                    stage=TaskActivityStage.RUNNING,
                    task_id=task_id,
                    summary="Recovered work from stale worker runtime state.",
                    detail="This worker reclaimed persisted task state that was previously owned by another worker id.",
                    details={
                        "source_owner_agent_ids": restored.get(
                            "source_owner_agent_ids", []
                        ),
                        "restored_task_count": restored.get("task_count"),
                    },
                )
        return restored
