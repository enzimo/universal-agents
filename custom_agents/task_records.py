"""Runtime record/value classes used by TaskManager."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry
from custom_messages.enums import ProgressState
from custom_messages.records.reviews import AfterActionReportReviewBatchRecord, AfterActionReportReviewRecord


class TaskInfo:
    """Helper class to store Task along with its metadata and state.

    ``owner_person_id`` is the person the task verifiably works for, decided
    once when the task is admitted and kept with the task (runtime snapshot
    included); None means work for nobody, or nothing proved a person. It
    comes only from the task's signed context or schedule run identity
    (``access_runtime.verified_meta_person_id`` at admission), never from the
    ``canonical_user_id`` a message's gateway context names. Subtasks and
    tasks the manager makes from another task (plan steps, recovery and
    Objective tasks) inherit their source task's owner. The archived task row,
    Objectives, solution variations, task controls and a person's departure
    all read this one field. Deciding at admission also keeps the owner when
    the signed context later expires (``access_control.identity``).
    """

    def __init__(
        self,
        task: Task,
        header: MessageHeader,
        meta: MessageMeta,
        *,
        owner_person_id: Optional[str],
    ):
        self.task = task
        self.header = header
        self.meta = meta
        self.owner_person_id = owner_person_id
        self.state = ProgressState.ACCEPTED
        self.restored_runtime_updated_at: Optional[str] = None
        if not self.task.task_state:
            self.task.task_state = []
        self.task.task_state.append((self.state, datetime.now(timezone.utc).isoformat()))
        if not self.task.execution_history:
            self.task.execution_history = []
        self._logger = logging.getLogger(__name__)

    @classmethod
    def from_snapshot(
        cls,
        task: Task,
        header: MessageHeader,
        meta: MessageMeta,
        state: ProgressState,
        owner_person_id: Optional[str],
        restored_runtime_updated_at: Optional[str] = None,
    ) -> "TaskInfo":
        """Rebuild TaskInfo without mutating the restored task history/state.

        The owner is the one recorded at admission; it is never re-derived
        from the restored message.
        """
        instance = cls.__new__(cls)
        instance.task = task
        instance.header = header
        instance.meta = meta
        instance.owner_person_id = owner_person_id
        instance.state = state
        instance.restored_runtime_updated_at = restored_runtime_updated_at
        if not instance.task.task_state:
            instance.task.task_state = []
        if not instance.task.execution_history:
            instance.task.execution_history = []
        instance._logger = logging.getLogger(__name__)
        return instance

    def update_state(self, state: ProgressState, timestamp: str):
        """Updates the task state and appends to history."""
        self.state = state
        self.task.task_state.append((state, timestamp))
        self._logger.info(f"Task {self.task.task_id} state updated to {state}")


@dataclass(frozen=True)
class ReportedWorkIdentity:
    """Whom the work an after-action report describes was for.

    Built only from verified identity
    (``custom_agents.after_action_report_identity.reported_work_identity``).
    ``person_id`` None means work for nobody (system work). Reviews and
    batches store it, batches are kept per person, and the scheduled review
    works for that person.
    """

    person_id: Optional[str]


@dataclass
class AfterActionReportRecordResult:
    """Runtime outcome for recording an AfterActionReport."""

    target_task_id: str
    history_entry: Optional[TaskHistoryEntry] = None
    review: Optional[AfterActionReportReviewRecord] = None
    batch: Optional[AfterActionReportReviewBatchRecord] = None
    accepted: bool = False
    stale: bool = False
    schedule_required: bool = False
    reason: Optional[str] = None

