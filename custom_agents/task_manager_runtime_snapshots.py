"""Runtime snapshot export and restore coordination for TaskManager."""

from __future__ import annotations

from custom_agents.runtime_snapshot_codec import (
    ALREADY_FINISHED,
    FINISHED_BY_ANOTHER_AGENT,
    HELD_BY_ANOTHER_AGENT,
    LeftOutDelegation,
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
from custom_agents.deferred_message_runtime import DeferredHandoff, pending_deferred_handoff
from storage.runtime_custody import CustodyStore, RuntimeCustodyLost, RuntimeRecoveryConflict
from storage.runtime_state_repository import RuntimeStateRepository
from custom_agents.task_runtime_state import WorkflowRuntimeState

import json
from typing import Any, Callable, Dict, List, Optional
from custom_messages.contracts.schedule_worker_v1 import ScheduleOccurrenceV1

from custom_agents.runtime_state_store import TaskRuntimeSnapshotRow
from storage.task_custody_repository import TaskCustodyTakenOver, TaskCustodyTakeover
from custom_messages.enums import TaskActivityStage, TaskActivityType




class RuntimeSnapshotCoordinator:
    """Persist and restore TaskManager active-runtime snapshots.

    It remembers whether this owner holds a saved row (``_holds_custody``)
    and which tasks that row held (``_saved_task_ids``): after its first save
    or an adopted recovery it holds one, after it clears its row it does not.
    A save made while holding one must find the row; when it is gone another
    agent took the work over (``RuntimeCustodyLost``). The coordinator then
    drops every task from memory in the same task-lane operation, so nothing
    here can run or save them again, and tells the custody listener
    (``custom_agents.custody_loss``) which tasks were taken over: only those in
    the last saved row. A task added by the refused save itself was never
    saved here; its caller rolls the admission back and its delivery is
    retried as a new one.

    A save refused because another agent took over some of this agent's
    tasks (``TaskCustodyTakenOver``) drops those tasks from memory, saves the
    rest and tells the custody listener (``custom_agents.custody_loss``),
    which stops a running turn of them without a reply: their new holder runs
    them and answers.
    """

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
        self._holds_custody = False
        self._saved_task_ids: frozenset[str] = frozenset()
        self._custody_lost_listener: Optional[Callable[[tuple[str, ...]], None]] = None

    def set_custody_lost_listener(self, listener: Optional[Callable[[tuple[str, ...]], None]]) -> None:
        """Called with the released task ids when a save finds the work taken over."""
        self._custody_lost_listener = listener

    def _release_lost_custody(self, lost: RuntimeCustodyLost) -> None:
        """Forget all work in memory: its saved row now belongs to another agent."""
        released = tuple(sorted(self._saved_task_ids))
        never_saved = sorted(set(self.task_state.tasks) - self._saved_task_ids)
        self.state.restore(
            WorkflowRuntimeState(
                TaskRuntimeState(), PlanRuntimeState(), ObjectiveRuntimeState(), ExecutionRuntimeState()
            )
        )
        self._holds_custody = False
        self._saved_task_ids = frozenset()
        self._logger.warning(
            "Another agent took this agent's saved work over; dropped it from memory without "
            "saving or replying. event=runtime_custody_lost owner_agent_id=%s store=%s task_ids=%s "
            "unsaved_task_ids=%s",
            lost.owner_agent_id,
            lost.store.value,
            list(released),
            never_saved,
        )
        if self._custody_lost_listener is None:
            return
        try:
            self._custody_lost_listener(released)
        except Exception:
            # The refusal itself must still reach the caller; the work is already released.
            self._logger.exception(
                "Could not tell the agent about released work. event=custody_listener_failed "
                "owner_agent_id=%s store=%s",
                lost.owner_agent_id,
                lost.store.value,
            )

    def _export_runtime_state(self) -> Dict[str, Any]:
        return capture_runtime_snapshot(
            self.state,
            plans_enabled=self.operations.plans_enabled(),
            objectives_enabled=self.operations.objectives_enabled(),
        ).model_dump(mode="json")

    def _persist_runtime_state(
        self, *, schedule_admission: "ScheduleOccurrenceV1 | None" = None,
        deferred_admission_task_id: Optional[str] = None,
        custody_takeover: TaskCustodyTakeover | None = None,
    ) -> None:
        owner_agent_id = self.identity.owner_agent_id
        if not owner_agent_id:
            if schedule_admission is not None:
                raise ValueError("Schedule admission requires durable runtime identity")
            return

        # Every save checks the dispatch's exact message. Only the request's
        # explicit admission can remove it before the whole handler finishes.
        handoff = pending_deferred_handoff()
        try:
            if handoff is not None and isinstance(handoff.failure, RuntimeCustodyLost):
                raise handoff.failure
            while True:
                try:
                    self._write_runtime_state(
                        owner_agent_id, schedule_admission, handoff, deferred_admission_task_id, custody_takeover
                    )
                    return
                except TaskCustodyTakenOver as taken:
                    self._release_taken_tasks(taken)
        except Exception as error:
            self._note_failed_save(error, handoff)
            raise

    def _write_runtime_state(
        self, owner_agent_id: str, schedule_admission: "ScheduleOccurrenceV1 | None",
        handoff: Optional[DeferredHandoff], deferred_admission_task_id: Optional[str],
        custody_takeover: TaskCustodyTakeover | None,
    ) -> None:
        if not self.task_state.tasks:
            self.runtime_state_store.clear_task_runtime(owner_agent_id)
            self._holds_custody = False
            self._saved_task_ids = frozenset()
            return
        consume_message = (
            handoff is not None and deferred_admission_task_id is not None
            and deferred_admission_task_id == handoff.admission_task_id
        )
        self.runtime_state_store.save_task_runtime(
            owner_agent_id=owner_agent_id,
            owner_agent_name=self.identity.owner_agent_name,
            team_id=self.identity.team_id,
            owner_named_agent_type=self.identity.owner_named_agent_type,
            runtime_state_json=json.dumps(self._export_runtime_state()),
            expect_existing=self._holds_custody,
            deferred_message_id=None if handoff is None else handoff.message_id,
            consume_deferred_message=consume_message,
            custody_takeover=custody_takeover,
            **({"schedule_admission": schedule_admission} if schedule_admission is not None else {}),
        )
        self._holds_custody = True
        self._saved_task_ids = frozenset(self.task_state.tasks)
        if handoff is not None and consume_message:
            handoff.committed = True

    def _note_failed_save(self, error: Exception, handoff: Optional[DeferredHandoff]) -> None:
        if handoff is not None:
            handoff.failure = error
        if not isinstance(error, RuntimeCustodyLost):
            self._logger.error(f"Failed to persist active runtime state: {error}")
        elif error.store is CustodyStore.TASK_RUNTIME:
            self._release_lost_custody(error)
        # A refused deferred row leaves this agent's task row and tasks alone;
        # the inbox drops its messages (deferred_message_runtime).

    def _release_taken_tasks(self, taken: TaskCustodyTakenOver) -> None:
        """Drop tasks this agent no longer holds, without saving, ending or replying.

        Another agent judged this one gone and admitted a copy (2026-10-17);
        it runs the task again from the start and answers, or already gave it
        up again (``taken.released``, 2026-10-18): finished by that agent, or
        given up unfinished, for example handed back after a lost lease, when
        the lead sends it again. Either way the task is not this agent's to
        run, and claiming it afresh would run it beside its other copy, so it
        is dropped the same way and logged. Before each task is dropped, the
        release callbacks stop the work it delegated and withdraw the access
        requests it waited on (``TaskManager.add_task_release_callback``). Each
        pass drops at least one task, so the save that follows ends.
        """
        refused = {**taken.holders, **taken.released}
        dropped = tuple(sorted(task_id for task_id in refused if task_id in self.task_state.tasks))
        if not dropped:
            raise RuntimeError(f"A save was refused for tasks this agent does not hold: {sorted(refused)}")
        for task_id in dropped:
            self.operations.notify_task_released(task_id)
            self.operations.forget_task(task_id)
        self._log_released(taken, dropped)
        if self._custody_lost_listener is None:
            return
        try:
            self._custody_lost_listener(dropped)
        except Exception:
            # The tasks are already dropped; the save must still go ahead.
            self._logger.exception(
                "Could not tell the agent about released tasks. event=custody_listener_failed "
                "owner_agent_id=%s task_ids=%s",
                taken.owner_agent_id,
                list(dropped),
            )

    def _log_released(self, taken: TaskCustodyTakenOver, dropped: tuple[str, ...]) -> None:
        held = {task_id: taken.holders[task_id] for task_id in dropped if task_id in taken.holders}
        finished = {task_id: taken.released[task_id] for task_id in dropped if taken.released.get(task_id)}
        given_up = [task_id for task_id in dropped if task_id in taken.released and not taken.released[task_id]]
        if held:
            self._logger.warning(
                "Another agent took over tasks of this agent's saved work; dropped them from memory "
                "without ending or answering them. event=task_custody_taken_over owner_agent_id=%s "
                "task_ids=%s holders=%s",
                taken.owner_agent_id, sorted(held), held,
            )
        if finished:
            self._logger.warning(
                "Another agent took over tasks of this agent's saved work and already finished them; "
                "dropped them from memory without ending or answering them. "
                "event=task_custody_finished_elsewhere owner_agent_id=%s task_ids=%s finished_by=%s",
                taken.owner_agent_id, sorted(finished), finished,
            )
        if given_up:
            self._logger.warning(
                "Another agent took over tasks of this agent's saved work and gave them up unfinished; "
                "dropped them from memory instead of claiming them again (whoever sends them again runs "
                "them). event=task_custody_released_elsewhere owner_agent_id=%s task_ids=%s",
                taken.owner_agent_id, given_up,
            )

    def persist_task_history(self, task_id: str) -> None:
        if not self.identity.owner_agent_id:
            return
        handoff = pending_deferred_handoff()
        try:
            if handoff is not None and isinstance(handoff.failure, RuntimeCustodyLost):
                raise handoff.failure
            self.runtime_state_store.save_task_runtime_task(
                owner_agent_id=self.identity.owner_agent_id,
                task=capture_task(self.task_state.tasks[task_id]),
                task_plan_links=self.plan_state.task_to_plan_id if self.operations.plans_enabled() else {},
                task_objective_links=self.objective_state.task_to_objective_id if self.operations.objectives_enabled() else {},
                plan_objective_links=self.objective_state.plan_to_objective_id if self.operations.objectives_enabled() else {},
                deferred_message_id=None if handoff is None else handoff.message_id,
            )
            return
        except RuntimeCustodyLost as lost:
            if handoff is not None:
                handoff.failure = lost
            if lost.store is CustodyStore.DEFERRED_MESSAGES:
                # The task row is still ours; only this message was transferred.
                raise
            if self._holds_custody:
                self._release_lost_custody(lost)
                raise
        # Never saved: the task was not admitted durably, so nothing was taken
        # over. Raised outside the handler so no custody loss travels with it.
        self._logger.error(
            "A task history write came before this agent ever saved the task. "
            "event=task_history_before_admission owner_agent_id=%s task_id=%s",
            self.identity.owner_agent_id,
            task_id,
        )
        raise RuntimeRecoveryConflict("Task history requires existing durable task custody")

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
        left_out: list[LeftOutDelegation] = []
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
                left_out_delegations=left_out,
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
            self._holds_custody = True
            self._saved_task_ids = frozenset(self.task_state.tasks)
            self._reconcile_restored_plans()
            self._persist_runtime_state()
            self._logger.info("Restored %s active tasks for %s", len(restored.task_state.tasks), self.identity.owner_agent_id)
            return {
                "stored_team_id": rows[0].team_id,
                "task_count": len(restored.task_state.tasks),
                "source_owner_agent_ids": [row.owner_agent_id for row in rows],
                "top_level_task_ids": sorted(task_id for task_id, info in restored.task_state.tasks.items() if info.task.parent_task_id is None),
                # Their workers are told to stop after recovery (orphaned_subtasks).
                "left_out_delegations": left_out,
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
        """Saved tasks another agent of the team holds, or any agent finished; recovery must not adopt them.

        Another agent took the task over after this snapshot's owner stopped
        (``custom_agents.task_custody``), and runs or ran it; adopting the
        saved copy as well ran it twice (2026-10-17). A task in the team's
        finished-task archive is finished whoever archived it, the snapshot's
        own owner included (it stopped between archiving and saving); the
        recovery transaction refuses it too (``task_custody_repository``), so
        this read keeps a second read from conflicting again (2026-10-18).
        Returns task id -> reason.
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
            if archived is not None and archived.get("team_id") == team_id:
                taken[task_id] = (
                    ALREADY_FINISHED if archived.get("owner_agent_id") in allowed else FINISHED_BY_ANOTHER_AGENT
                )
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
