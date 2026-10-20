"""Objective lifecycle coordination for TaskManager."""

from __future__ import annotations

from custom_agents.task_manager_ports import ObjectiveChange, ObjectiveCoordinatorOperations
from custom_agents.task_runtime_state import RuntimeOwnerIdentity
from custom_agents.task_runtime_state import ObjectiveRuntimeState
from custom_agents.task_runtime_state import PlanRuntimeState
from custom_agents.task_runtime_state import TaskRuntimeState
from custom_agents.task_runtime_state import cache_plan_objective, cache_task_objective
import logging
from storage.change_ledger import ChangeRefused
from storage.durable_owner_scope_repository import DurableOwnerScopeRepository
from storage.objective_repository import ObjectiveRepository, ObjectiveView
from storage.objective_work_repository import ObjectiveWorkRepository
from custom_agents.task_manager_plans import PlanCoordinator

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from custom_agents.task_records import TaskInfo
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ObjectiveCompletionPolicy, ObjectiveCriterionStatus, ObjectiveEventType, ObjectiveHorizon, ObjectivePriority, ObjectiveStatus, PlanStatus, ProgressState, RequesterType, TaskActivityStage, TaskActivityType, TaskHistoryImportance, TaskHistorySource, TaskHistoryType
from custom_messages.records.objectives import ObjectiveEventRecord, ObjectiveHeartbeatLink, ObjectiveRecord, ObjectiveScheduleLink, ObjectiveSuccessCriterion
from custom_messages.records.plans import PlanRecord
from id_generation import new_id


class ObjectiveSaveFailed(RuntimeError):
    """An Objective change was not written to the store.

    Raised by ``ObjectiveCoordinator`` writes for changes that a tool
    call or a person's command asked for, so the reply says the change was
    not saved instead of returning the changed record as if it were.
    """

    def __init__(self, objective_id: str) -> None:
        super().__init__(
            f"The change to Objective {objective_id} was not saved. "
            "Try again later; if it keeps failing, tell the person."
        )
        self.objective_id = objective_id


# Statuses a person set that a task-end review keeps, with the review's
# conclusion. Before this, a task that ended while its Objective was paused or
# being deleted set the Objective active or blocked again, with its schedules
# and work policy still off and Resume no longer offered.
_HELD_REVIEW_CONCLUSIONS = {
    ObjectiveStatus.PAUSED: "objective_paused",
    ObjectiveStatus.DELETING: "objective_deletion_pending",
}


@dataclass
class _TaskEndReview:
    """One task-end review: what was read before the store transaction, and what it concluded."""

    task_id: str
    terminal_state: str
    timestamp: str
    summary: Optional[str]
    active_task_ids: List[str]
    active_plan_ids: List[str]
    conclusion: str = ""
    decision_summary: str = ""
    # The review decided the Objective's status (its last linked work ended).
    decided_status: bool = False


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

    def _stamp(self, objective: ObjectiveRecord) -> None:
        """The team lead's fields and the write time, set on every record this coordinator writes."""
        objective.owner_agent_name = objective.owner_agent_name or self.identity.owner_agent_name
        objective.team_id = objective.team_id or self.identity.team_id
        objective.owner_named_agent_type = objective.owner_named_agent_type or self.identity.owner_named_agent_type
        objective.updated_at = datetime.now(timezone.utc).isoformat()

    def _remember_links(self, objective: ObjectiveRecord) -> None:
        """Point the plan and task lookups at a stored Objective (only after the write)."""
        # Only links of tasks and plans this agent holds: the Objective lists
        # finished ones too (task_runtime_state.TASK_SCOPED_FIELDS).
        for plan_id in [objective.current_plan_id, *objective.plan_ids]:
            if plan_id:
                cache_plan_objective(self.plan_state, self.objective_state, plan_id, objective.objective_id)
        for task_id in objective.related_task_ids + objective.recovery_task_ids:
            if task_id:
                cache_task_objective(self.task_state, self.objective_state, task_id, objective.objective_id)

    def _save_failed(self, objective_id: str, exc: BaseException) -> ObjectiveSaveFailed:
        self._logger.error(
            "An Objective change was not saved. event=objective_save_failed objective_id=%s error=%s",
            objective_id,
            exc,
        )
        return ObjectiveSaveFailed(objective_id)

    def _insert_objective(self, objective: ObjectiveRecord) -> None:
        """Store a new Objective, or raise ``ObjectiveSaveFailed``."""
        if not self.operations.objectives_enabled():
            self._logger.error(
                "An Objective change was not saved because Objectives are off. "
                "event=objective_save_failed objective_id=%s reason=objectives_disabled",
                objective.objective_id,
            )
            raise ObjectiveSaveFailed(objective.objective_id)
        objective.owner_agent_id = self.operations.durable_manager_scope_id()
        self._stamp(objective)
        try:
            self.objective_repository.insert_objective(objective)
        except Exception as exc:
            raise self._save_failed(objective.objective_id, exc) from exc
        self._remember_links(objective)

    def _commit_change(
        self,
        objective_id: Optional[str],
        change: ObjectiveChange,
        *,
        expected_version: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        """Apply ``change`` to the stored Objective in one store transaction.

        Returns the record after the change (unchanged when ``change`` returns
        False), or None when there is no such Objective. A failed write raises
        ``ObjectiveSaveFailed``. With ``expected_version`` (an agent's edit)
        the store raises ``ChangeRefused`` (``changed_since_read``) when the
        Objective changed since the agent read it; that passes through to the
        caller, which reports it. ``change`` runs inside the transaction, so
        it only edits the record: it reports a refusal through its own state,
        never by writing anything else.
        """
        if not objective_id or not self.operations.objectives_enabled() or not self.identity.owner_agent_id:
            return None
        scope = self.operations.durable_manager_scope_id()
        if not scope:
            return None

        def stamped(objective: ObjectiveRecord) -> bool:
            if not change(objective):
                return False
            self._stamp(objective)
            return True

        try:
            objective = self.objective_repository.change_objective(
                objective_id,
                owner_agent_id=scope,
                change=stamped,
                expected_version=expected_version,
            )
        except ChangeRefused:
            raise
        except Exception as exc:
            raise self._save_failed(objective_id, exc) from exc
        if objective is not None:
            self._remember_links(objective)
        return objective

    def change_objective(
        self,
        objective_id: str,
        change: ObjectiveChange,
        *,
        step: Optional[str] = None,
        expected_version: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        """Change the stored Objective in one store transaction; the way to change one from outside this class.

        Why: a copy of the record kept and saved later would erase whatever
        was stored in between (a work-policy event, a progress update, a
        schedule or task link), also from another team lead process. The
        store applies ``change`` to the stored row inside its write
        transaction (``ObjectiveRepository.change_objective``), so nothing a
        change does not touch is ever written back from an older copy.
        Callers still run this inside a task operation, so their own steps
        happen in order.

        ``change`` edits the record in place and returns False when there is
        nothing to save. Returns the record after the change, or None when the
        Objective does not exist. Without ``step`` a failed save raises
        ``ObjectiveSaveFailed``. With ``step`` (trusted bookkeeping nobody
        waits on: a task starting, ending or gaining a subtask, a plan
        following its task, a work session recording progress) the failure is
        logged with the step and None is returned: their own records are
        stored elsewhere, so only the Objective's copy of the change is lost.
        Bookkeeping passes no ``expected_version``, so it is never refused.
        """
        if step is None:
            return self._commit_change(objective_id, change, expected_version=expected_version)
        try:
            return self._commit_change(objective_id, change, expected_version=expected_version)
        except ObjectiveSaveFailed:
            self._log_skipped(objective_id, step)
            return None

    def _log_skipped(self, objective_id: Optional[str], step: str) -> None:
        self._logger.warning(
            "Went on without the Objective change. event=objective_save_skipped objective_id=%s step=%s",
            objective_id,
            step,
        )

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
            cache_plan_objective(self.plan_state, self.objective_state, plan_id, objective.objective_id)
        return objective

    def _get_task_objective_id_from_context(self, task_id: Optional[str]) -> Optional[str]:
        """The task's Objective from its links, else from searching Objective records."""
        if not task_id or not self.operations.objectives_enabled():
            return None
        return self._objective_id_from_task_links(task_id) or self._objective_id_from_records(task_id)

    def _parent_of(self, task_id: str, task_info: Optional[TaskInfo]) -> Optional[str]:
        parent_task_id = task_info.task.parent_task_id if task_info is not None else None
        return parent_task_id or self.task_state.subtask_to_parent_map.get(task_id)

    def _objective_id_from_task_links(self, task_id: Optional[str]) -> Optional[str]:
        """The task's Objective from links this agent holds: its own, its parent's or its plan's.

        Never searches Objective records by task id text; that is
        ``_objective_id_from_records``.
        """
        if not task_id or not self.operations.objectives_enabled():
            return None
        existing_objective_id = self.objective_state.task_to_objective_id.get(task_id)
        if existing_objective_id:
            return existing_objective_id
        task_info = self.operations.get_task_info(task_id)
        objective_id = getattr(task_info.meta, "objective_id", None) if task_info else None
        parent_task_id = self._parent_of(task_id, task_info)
        if not objective_id and parent_task_id:
            objective_id = self._objective_id_from_task_links(parent_task_id)
        if not objective_id:
            objective_id = self._objective_id_from_plan_link(task_id)
        if objective_id:
            cache_task_objective(self.task_state, self.objective_state, task_id, objective_id)
        return objective_id

    def _objective_id_from_plan_link(self, task_id: str) -> Optional[str]:
        plan_id = self.plan_state.task_to_plan_id.get(task_id)
        if not plan_id:
            return None
        objective_id = self.objective_state.plan_to_objective_id.get(plan_id)
        if objective_id:
            return objective_id
        objective = self._get_objective_for_plan_id(plan_id)
        return objective.objective_id if objective is not None else None

    def _objective_id_from_records(self, task_id: str) -> Optional[str]:
        """Search Objective records for the task (its parent first) and remember the links."""
        parent_task_id = self._parent_of(task_id, self.operations.get_task_info(task_id))
        objective_id = self._objective_id_from_records(parent_task_id) if parent_task_id else None
        if not objective_id and self.identity.owner_agent_id:
            objective_id = self._search_objective_by_task(task_id)
        if objective_id:
            cache_task_objective(self.task_state, self.objective_state, task_id, objective_id)
        return objective_id

    def _search_objective_by_task(self, task_id: str) -> Optional[str]:
        try:
            objective = self.objective_repository.get_objective_by_task(
                task_id,
                owner_agent_id=self.operations.durable_manager_scope_id(),
            )
        except Exception as exc:
            self._logger.error("Failed to load objective for task %s: %s", task_id, exc)
            return None
        if objective is None:
            return None
        for objective_plan_id in [objective.current_plan_id, *objective.plan_ids]:
            if objective_plan_id:
                cache_plan_objective(self.plan_state, self.objective_state, objective_plan_id, objective.objective_id)
        return objective.objective_id

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
            existing_plan = self.operations.get_plan(existing_plan_id)
            if existing_plan is not None and existing_plan.objective_id:
                # One Plan belongs to one Objective. Even an administrator
                # must not turn a team Plan into their personal Plan by
                # promoting a task that already shares it.
                self._logger.warning(
                    "Cannot promote a task whose Plan already belongs to an Objective. "
                    "event=objective_creation_plan_already_owned task_id=%s plan_id=%s objective_id=%s",
                    task_id, existing_plan_id, existing_plan.objective_id,
                )
                raise ValueError(
                    "This task's Plan already belongs to an Objective. "
                    "Use creation_mode='spawn_task' to create separate Objective work."
                )
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

    def objective_id_for_task_end(self, task_id: str) -> Optional[str]:
        """The Objective a task works for, read from its links before it is archived.

        The terminal paths call this before archiving, because archiving
        forgets the task's links (task_runtime_state.forget_task_records), and
        pass the id to ``review_objective_after_task_terminal``. It never
        searches Objective records by task id text.
        """
        return self._objective_id_from_task_links(task_id)

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

        def link_task(stored: ObjectiveRecord) -> bool:
            if task_id in stored.related_task_ids:
                return False
            stored.related_task_ids.append(task_id)
            self._append_objective_event(
                stored,
                ObjectiveEventType.TASK_LINKED,
                f"Linked incoming task {task_id} to objective.",
                task_id=task_id,
                user_visible=False,
            )
            return True

        # Merged into the stored record: an agent's edit made meanwhile stays.
        objective = self.change_objective(objective.objective_id, link_task, step="incoming_task_linked") or objective

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

    def link_schedule_to_objective(
        self,
        objective_id: str,
        schedule_id: str,
        *,
        managed_by_objective: bool = True,
        user_visible: bool = False,
        reason: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        def link(objective: ObjectiveRecord) -> bool:
            existing = next((item for item in objective.schedule_links if item.schedule_id == schedule_id), None)
            if existing is not None:
                if existing.managed_by_objective == managed_by_objective:
                    return False
                existing.managed_by_objective = managed_by_objective
                return True
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
            return True

        objective = self._commit_change(objective_id, link)
        if objective is None:
            self._logger.warning("Cannot link schedule %s to unknown objective %s.", schedule_id, objective_id)
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
        def link(objective: ObjectiveRecord) -> bool:
            existing = next(
                (item for item in objective.heartbeat_links if item.heartbeat_item_id == heartbeat_item_id),
                None,
            )
            if existing is not None:
                if existing.managed_by_objective == managed_by_objective:
                    return False
                existing.managed_by_objective = managed_by_objective
                return True
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
            return True

        objective = self._commit_change(objective_id, link)
        if objective is None:
            self._logger.warning(
                "Cannot link heartbeat item %s to unknown objective %s.",
                heartbeat_item_id,
                objective_id,
            )
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

        self._insert_objective(objective)
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
        objective_id: str,
        *,
        source_task_id: str,
        continuation_task_id: str,
        work_brief: str,
        current_focus: Optional[str],
        user_visible: bool,
    ) -> None:
        """Record a continuation Task on the stored Objective, or raise ``ObjectiveSaveFailed``."""

        def start(stored: ObjectiveRecord) -> bool:
            previous_status = stored.status
            stored.status = ObjectiveStatus.ACTIVE
            if current_focus is not None:
                stored.current_focus = current_focus.strip() or stored.current_focus
            stored.progress_summary = f"Started continuation Task {continuation_task_id}."
            self._append_objective_event(
                stored,
                ObjectiveEventType.PROGRESS_UPDATED,
                "Started a new Task to continue Objective work.",
                task_id=continuation_task_id,
                plan_id=stored.current_plan_id,
                user_visible=user_visible,
                details={
                    "source_task_id": source_task_id,
                    "previous_status": previous_status.value,
                    "work_brief": work_brief,
                },
            )
            return True

        objective = self._commit_change(objective_id, start)
        if objective is None:
            # Deleted since the continuation began: nothing can record it.
            raise ObjectiveSaveFailed(objective_id)
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
        try:
            self._record_objective_continuation_start(
                objective.objective_id,
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
        expected_version: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        """Record progress and criterion updates on the stored Objective.

        ``expected_version`` is the version the agent read (``objective_version``);
        the store refuses the update with ``changed_since_read`` when the
        Objective changed since. A closed Objective records nothing.
        """
        outcome = {"closed": False, "changed": False}

        def progress(objective: ObjectiveRecord) -> bool:
            if objective.status in self._objective_terminal_statuses():
                outcome["closed"] = True
                return False
            if not self._apply_progress_fields(objective, progress_summary, current_focus, criterion_updates):
                return False
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
            outcome["changed"] = True
            return True

        objective = self._commit_change(objective_id, progress, expected_version=expected_version)
        if objective is None:
            return None
        if outcome["closed"]:
            self._logger.warning("Ignoring progress update for terminal objective %s.", objective_id)
        if not outcome["changed"]:
            return objective
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

    def _apply_progress_fields(
        self,
        objective: ObjectiveRecord,
        progress_summary: Optional[str],
        current_focus: Optional[str],
        criterion_updates: Optional[List[Dict[str, Any]]],
    ) -> bool:
        """Set the progress, focus and criterion updates on a stored record; False when nothing changed."""
        changed = False
        if progress_summary is not None and progress_summary != objective.progress_summary:
            objective.progress_summary = progress_summary.strip() or None
            changed = True
        if current_focus is not None and current_focus != objective.current_focus:
            objective.current_focus = current_focus.strip() or None
            changed = True

        criteria_by_id = {criterion.criterion_id: criterion for criterion in objective.success_criteria}
        for raw_update in criterion_updates or []:
            if isinstance(raw_update, dict) and self._apply_criterion_update(objective, criteria_by_id, raw_update):
                changed = True
        return changed

    def _apply_criterion_update(
        self,
        objective: ObjectiveRecord,
        criteria_by_id: Dict[str, ObjectiveSuccessCriterion],
        raw_update: Dict[str, Any],
    ) -> bool:
        """One criterion update: a known criterion by id, or a new one by description; False when nothing changed."""
        criterion = criteria_by_id.get(str(raw_update.get("criterion_id") or ""))
        changed = False
        if criterion is None:
            description = str(raw_update.get("description") or "").strip()
            if not description:
                return False
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
        return changed

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
        deletion_operation_id: Optional[str] = None,
        expected_version: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        """Set an Objective's lifecycle status on its stored record, in one store transaction.

        A request that changes nothing (a second pause, a retried deletion
        step) records no event and saves nothing. Entering ``deleting``
        records ``deletion_operation_id`` and the request time in the same
        write as the status, so a deletion never exists without them.
        ``expected_version`` is the version an agent read; a person's
        controls and trusted steps pass none.
        """
        target_status = self._coerce_objective_status(status)
        now_ts = datetime.now(timezone.utc).isoformat()
        outcome: Dict[str, Any] = {"refusal": None, "previous": None}

        def set_status(objective: ObjectiveRecord) -> bool:
            # Status is runtime bookkeeping, so it is outside the definition
            # version. Check the person's deletion on the locked row instead
            # of trusting the tool's earlier read from another connection.
            if expected_version is not None and objective.status in {ObjectiveStatus.DELETING, ObjectiveStatus.DELETED}:
                state = "was deleted" if objective.status == ObjectiveStatus.DELETED else "is being deleted"
                outcome["refusal"] = f"Objective {objective_id} {state} by its person, so it can no longer change."
                return False
            refusal = self._satisfaction_refusal(objective, target_status, user_confirmation_summary)
            if refusal:
                outcome["refusal"] = refusal
                return False
            previous_status = objective.status
            before = objective.model_dump(mode="json")
            self._apply_objective_status_fields(
                objective,
                target_status,
                summary=summary,
                user_confirmation_summary=user_confirmation_summary,
                satisfied_by_task_id=satisfied_by_task_id,
                task_id=task_id,
                plan_id=plan_id,
                deletion_operation_id=deletion_operation_id,
                timestamp=now_ts,
            )
            if previous_status == target_status and objective.model_dump(mode="json") == before:
                return False
            if previous_status != target_status:
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
            outcome["previous"] = previous_status
            return True

        # Saved before its Plan closes, so a failed save leaves both unchanged.
        objective = self._commit_change(objective_id, set_status, expected_version=expected_version)
        if objective is None:
            return None
        if outcome["refusal"]:
            raise ValueError(outcome["refusal"])
        # A terminal status closes a Plan left open, also when nothing else
        # changed (the step does nothing when it is closed).
        self._transition_current_plan_for_objective_status(
            objective,
            target_status,
            summary=summary,
            task_id=task_id or satisfied_by_task_id,
            timestamp=now_ts,
        )
        previous_status = outcome["previous"]
        if previous_status is not None:
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

    @staticmethod
    def _satisfaction_refusal(
        objective: ObjectiveRecord,
        target_status: ObjectiveStatus,
        user_confirmation_summary: Optional[str],
    ) -> Optional[str]:
        """Why the stored Objective cannot be marked satisfied yet, or None."""
        if target_status != ObjectiveStatus.SATISFIED:
            return None
        if objective.requires_user_confirmation and not (
            user_confirmation_summary or objective.user_confirmation_summary
        ):
            return "Objective requires user confirmation before it can be marked satisfied."
        if objective.completion_policy == ObjectiveCompletionPolicy.SUCCESS_CRITERIA_MET and any(
            criterion.status != ObjectiveCriterionStatus.MET for criterion in objective.success_criteria
        ):
            return "Objective success criteria must all be met before it can be marked satisfied."
        return None

    def _apply_objective_status_fields(
        self,
        objective: ObjectiveRecord,
        target_status: ObjectiveStatus,
        *,
        summary: Optional[str],
        user_confirmation_summary: Optional[str],
        satisfied_by_task_id: Optional[str],
        task_id: Optional[str],
        plan_id: Optional[str],
        deletion_operation_id: Optional[str],
        timestamp: str,
    ) -> None:
        """Set the status and the fields recorded with it on a loaded record."""
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
                timestamp=timestamp,
            )
        if target_status == ObjectiveStatus.SATISFIED:
            objective.satisfied_by_task_id = satisfied_by_task_id or task_id or objective.satisfied_by_task_id
            objective.satisfied_at = objective.satisfied_at or timestamp
        if target_status == ObjectiveStatus.DELETING:
            objective.deletion_operation_id = objective.deletion_operation_id or deletion_operation_id
            objective.deletion_requested_at = objective.deletion_requested_at or timestamp
        if target_status == ObjectiveStatus.DELETED:
            # Commit the completed tombstone in one write. Once DELETED is
            # stored, repeated deletion commands return without changing it.
            objective.deleted_at = objective.deleted_at or timestamp
            objective.deletion_summary = (summary or "").strip() or "Objective deletion completed."

    def review_objective_after_task_terminal(
        self,
        task_id: str,
        terminal_state: ProgressState | str,
        *,
        objective_id: Optional[str],
        timestamp: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> Optional[ObjectiveRecord]:
        """Review the Objective after one of its tasks ended (trusted bookkeeping).

        Which of its tasks and plans are still active is read first, from the
        team lead's own records; the review itself is then applied to the
        stored Objective inside one store transaction
        (``_apply_task_end_review``), so an agent's edit saved meanwhile stays
        and the review is never refused for it. A failed save is logged with
        its conclusion and the task end goes on.
        """
        objective = self._get_objective_record(objective_id)
        if objective is None:
            return None
        if not self._reviews_task_end(objective, task_id):
            return objective
        review = _TaskEndReview(
            task_id=task_id,
            terminal_state=terminal_state.value if isinstance(terminal_state, ProgressState) else str(terminal_state),
            timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
            summary=summary,
            active_task_ids=self._active_task_ids_for_objective(objective),
            active_plan_ids=self._active_plan_ids_for_objective(objective),
        )

        def apply_review(stored: ObjectiveRecord) -> bool:
            if not self._reviews_task_end(stored, task_id):
                return False
            self._apply_task_end_review(stored, review)
            return True

        try:
            reviewed = self._commit_change(objective.objective_id, apply_review)
        except ObjectiveSaveFailed:
            self._log_skipped(objective.objective_id, f"task_end_review:{review.conclusion or 'not_recorded'}")
            reviewed = None
        if reviewed is not None and review.decided_status:
            self.operations.emit_activity(
                activity_type=TaskActivityType.OBJECTIVE_UPDATED,
                stage=(
                    TaskActivityStage.COMPLETED
                    if reviewed.status == ObjectiveStatus.SATISFIED
                    else TaskActivityStage.RUNNING
                ),
                task_id=task_id,
                summary=f"Objective review completed: {reviewed.title}",
                detail=review.decision_summary,
                user_visible=True,
                details=reviewed.model_dump(mode="json"),
            )
        self.operations.persist_runtime_state()
        return reviewed or objective

    def _reviews_task_end(self, objective: ObjectiveRecord, task_id: str) -> bool:
        """Whether a task's end is reviewed on this Objective: one of its own tasks, while it is open."""
        owned = set(objective.related_task_ids or []) | set(objective.recovery_task_ids or [])
        return task_id in owned and objective.status not in self._objective_terminal_statuses()

    def _apply_task_end_review(self, objective: ObjectiveRecord, review: "_TaskEndReview") -> None:
        """Record the review on the stored record; sets ``review.conclusion``.

        Runs inside the store transaction, so it only edits the record.
        """
        self._append_objective_event(
            objective,
            ObjectiveEventType.REVIEW_REQUESTED,
            "Reviewing objective after linked task reached a terminal state.",
            task_id=review.task_id,
            plan_id=objective.current_plan_id,
            user_visible=False,
            details={
                "terminal_task_id": review.task_id,
                "terminal_state": review.terminal_state,
                "summary": review.summary,
            },
            timestamp=review.timestamp,
        )
        held_conclusion = _HELD_REVIEW_CONCLUSIONS.get(objective.status)
        if held_conclusion is not None:
            # A person's pause or pending deletion outranks the review: the
            # Objective keeps its status and its links stay off. Nothing
            # re-reviews it on resume; the next task, schedule or work
            # session that ends does.
            self._complete_review(
                objective,
                review,
                held_conclusion,
                f"Objective review deferred because the Objective is {objective.status.value}.",
                active_task_ids=review.active_task_ids,
                active_plan_ids=review.active_plan_ids,
            )
            return
        if review.active_task_ids:
            objective.progress_summary = (
                review.summary.strip()
                if review.summary
                else "A linked task reached a terminal state; another Objective-linked Task is still active."
            )
            self._complete_review(
                objective,
                review,
                "active_work_remains",
                "Objective remains active because another linked Task is still running.",
                active_task_ids=review.active_task_ids,
                active_plan_ids=review.active_plan_ids,
            )
            return
        if review.active_plan_ids:
            self._review_with_active_plan(objective, review)
            return
        if self._objective_continues_after_tasks(objective):
            self._review_standing_objective(objective, review)
            return
        self._review_closing_objective(objective, review)

    def _complete_review(
        self,
        objective: ObjectiveRecord,
        review: "_TaskEndReview",
        conclusion: str,
        decision_summary: str,
        *,
        active_task_ids: Sequence[str] = (),
        active_plan_ids: Sequence[str] = (),
    ) -> None:
        review.conclusion = conclusion
        review.decision_summary = decision_summary
        self._append_objective_review_completed(
            objective,
            task_id=review.task_id,
            timestamp=review.timestamp,
            conclusion=conclusion,
            summary=decision_summary,
            terminal_state=review.terminal_state,
            active_task_ids=list(active_task_ids),
            active_plan_ids=list(active_plan_ids),
        )

    def _set_reviewed_status(
        self, objective: ObjectiveRecord, review: "_TaskEndReview", status: ObjectiveStatus, reason: str
    ) -> None:
        """Set the status a review decided, with its event when it changes."""
        previous_status = objective.status
        objective.status = status
        if previous_status == status:
            return
        self._append_objective_event(
            objective,
            ObjectiveEventType.STATUS_CHANGED,
            f"Objective status changed from {previous_status.value} to {status.value}.",
            task_id=review.task_id,
            plan_id=objective.current_plan_id,
            user_visible=True,
            details={
                "previous_status": previous_status.value,
                "new_status": status.value,
                "summary": objective.progress_summary,
                "reason": reason,
            },
            timestamp=review.timestamp,
        )

    def _review_with_active_plan(self, objective: ObjectiveRecord, review: "_TaskEndReview") -> None:
        if review.terminal_state == ProgressState.COMPLETED.value:
            status = ObjectiveStatus.ACTIVE
            conclusion = "plan_work_remains"
            decision_summary = "The execution Task completed, but the Objective-owned Plan still has work remaining."
        else:
            status = ObjectiveStatus.BLOCKED
            conclusion = "task_failed_plan_remains_active"
            decision_summary = (
                f"The execution Task ended with state {review.terminal_state}; the Objective and its Plan remain "
                "available for a replacement Task."
            )
        objective.progress_summary = review.summary.strip() if review.summary else decision_summary
        self._set_reviewed_status(objective, review, status, conclusion)
        self._complete_review(
            objective, review, conclusion, decision_summary, active_plan_ids=review.active_plan_ids
        )

    def _objective_continues_after_tasks(self, objective: ObjectiveRecord) -> bool:
        return (
            objective.completion_policy == ObjectiveCompletionPolicy.RECURRING_INDEFINITE
            or objective.horizon in {ObjectiveHorizon.RECURRING, ObjectiveHorizon.STANDING}
            or self._objective_has_continuing_links(objective)
        )

    def _review_standing_objective(self, objective: ObjectiveRecord, review: "_TaskEndReview") -> None:
        conclusion = "standing_or_recurring_objective_remains_active"
        if objective.status == ObjectiveStatus.PROPOSED:
            previous_status = objective.status
            objective.status = ObjectiveStatus.ACTIVE
            self._append_objective_event(
                objective,
                ObjectiveEventType.STATUS_CHANGED,
                f"Objective status changed from {previous_status.value} to active.",
                task_id=review.task_id,
                plan_id=objective.current_plan_id,
                user_visible=True,
                details={
                    "previous_status": previous_status.value,
                    "new_status": objective.status.value,
                    "summary": "Recurring or standing Objective has terminal task activity.",
                    "reason": conclusion,
                },
                timestamp=review.timestamp,
            )
        objective.progress_summary = (
            review.summary.strip()
            if review.summary
            else "Latest linked task finished; Objective remains active for recurring or standing work."
        )
        self._complete_review(objective, review, conclusion, objective.progress_summary)

    @staticmethod
    def _closing_decision(objective: ObjectiveRecord, terminal_state: str) -> tuple[ObjectiveStatus, str, str]:
        """The status, conclusion and summary for an Objective whose last linked work ended."""
        if terminal_state != ProgressState.COMPLETED.value:
            return (
                ObjectiveStatus.BLOCKED,
                "terminal_task_did_not_satisfy_objective",
                f"Linked task ended with state {terminal_state}; Objective needs follow-up before it can close.",
            )
        if objective.requires_user_confirmation or objective.completion_policy == ObjectiveCompletionPolicy.USER_CONFIRMS:
            return (
                ObjectiveStatus.BLOCKED,
                "awaiting_user_confirmation",
                "Linked work finished; Objective requires user confirmation before it can be marked satisfied.",
            )
        if objective.completion_policy != ObjectiveCompletionPolicy.SUCCESS_CRITERIA_MET:
            return (
                ObjectiveStatus.SATISFIED,
                "manager_decides_task_satisfied_objective",
                "Linked task completed and no active Objective-linked work remains.",
            )
        if objective.success_criteria and all(
            criterion.status == ObjectiveCriterionStatus.MET for criterion in objective.success_criteria
        ):
            return (
                ObjectiveStatus.SATISFIED,
                "success_criteria_met",
                "All Objective success criteria are met and no linked work remains.",
            )
        return (
            ObjectiveStatus.BLOCKED,
            "unmet_success_criteria",
            "Linked work finished, but Objective success criteria are still open.",
        )

    def _review_closing_objective(self, objective: ObjectiveRecord, review: "_TaskEndReview") -> None:
        target_status, conclusion, decision_summary = self._closing_decision(objective, review.terminal_state)
        objective.progress_summary = review.summary.strip() if review.summary else decision_summary
        if target_status == ObjectiveStatus.SATISFIED:
            objective.satisfied_by_task_id = objective.satisfied_by_task_id or review.task_id
            objective.satisfied_at = objective.satisfied_at or review.timestamp
        self._set_reviewed_status(objective, review, target_status, conclusion)
        self._complete_review(objective, review, conclusion, decision_summary)
        review.decided_status = True

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
