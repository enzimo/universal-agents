"""Runtime-facing wrapper around the storage-backed runtime snapshot repository."""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence
from custom_messages.contracts.runtime_snapshot_v3 import TaskSnapshotRecord
from custom_messages.contracts.schedule_worker_v1 import ScheduleOccurrenceV1

from default_values import STORAGE_DEFAULTS
from storage.backends import STORAGE_BACKEND_SQLITE
from storage.config import StorageSettings, resolve_storage_settings
from storage.runtime_state_repository import (
    DeferredMessageSnapshotRow,
    RuntimeTaskCancellationRow,
    TaskRuntimeSnapshotRow,
    create_runtime_state_repository,
)


class RuntimeStateStore:
    """Runtime-facing live snapshot store backed by the selected repository."""

    def __init__(
        self,
        db_path: str = STORAGE_DEFAULTS.task_history_db_path.value,
        logger: logging.Logger = None,
        storage_settings: Optional[StorageSettings] = None,
    ):
        self._db_path = storage_settings.sqlite_path if storage_settings and storage_settings.sqlite_path else db_path
        self._logger = logger or logging.getLogger(__name__)
        self._storage_settings = storage_settings or resolve_storage_settings(
            {"backend": STORAGE_DEFAULTS.backend.value, STORAGE_BACKEND_SQLITE: {"path": db_path}}
        )
        self._repository = create_runtime_state_repository(
            self._storage_settings,
            logger=self._logger,
        )

    def save_task_runtime(
        self,
        *,
        owner_agent_id: str,
        owner_agent_name: Optional[str],
        team_id: Optional[str],
        owner_named_agent_type: Optional[str],
        runtime_state_json: str,
        expect_existing: bool,
        schedule_admission: ScheduleOccurrenceV1 | None = None,
        deferred_handoff: Optional[str] = None,
    ) -> None:
        self._repository.save_task_runtime(
            owner_agent_id=owner_agent_id,
            owner_agent_name=owner_agent_name,
            team_id=team_id,
            owner_named_agent_type=owner_named_agent_type,
            runtime_state_json=runtime_state_json,
            expect_existing=expect_existing,
            schedule_admission=schedule_admission,
            deferred_handoff=deferred_handoff,
        )

    def save_task_runtime_task(
        self, *, owner_agent_id: str, task: TaskSnapshotRecord,
        task_plan_links: dict[str, str], task_objective_links: dict[str, str],
        plan_objective_links: dict[str, str],
    ) -> None:
        self._repository.save_task_runtime_task(
            owner_agent_id=owner_agent_id, task=task, task_plan_links=task_plan_links,
            task_objective_links=task_objective_links, plan_objective_links=plan_objective_links,
        )

    def clear_task_runtime(self, owner_agent_id: str) -> None:
        self._repository.clear_task_runtime(owner_agent_id)

    def commit_task_runtime_recovery(
        self, *, sources: Sequence[TaskRuntimeSnapshotRow], replacement: TaskRuntimeSnapshotRow,
    ) -> None:
        self._repository.commit_task_runtime_recovery(sources=sources, replacement=replacement)

    def commit_deferred_messages_recovery(
        self, *, sources: Sequence[DeferredMessageSnapshotRow], replacement: DeferredMessageSnapshotRow,
    ) -> None:
        self._repository.commit_deferred_messages_recovery(sources=sources, replacement=replacement)

    def get_task_runtime_team_id(self, owner_agent_id: str) -> Optional[str]:
        return self._repository.get_task_runtime_team_id(owner_agent_id)

    def load_task_runtime_by_owner(self, owner_agent_id: str) -> List[TaskRuntimeSnapshotRow]:
        return self._repository.load_task_runtime_by_owner(owner_agent_id)


    def peek_task_runtime_by_role(
        self,
        team_id: str,
        owner_named_agent_type: str,
    ) -> List[TaskRuntimeSnapshotRow]:
        return self._repository.peek_task_runtime_by_role(team_id, owner_named_agent_type)

    def peek_task_runtime_by_team(self, team_id: str) -> List[TaskRuntimeSnapshotRow]:
        return self._repository.peek_task_runtime_by_team(team_id)

    def save_deferred_messages(
        self,
        *,
        owner_agent_id: str,
        owner_agent_name: Optional[str],
        team_id: Optional[str],
        owner_named_agent_type: Optional[str],
        messages_json: str,
        expect_existing: bool,
    ) -> None:
        self._repository.save_deferred_messages(
            owner_agent_id=owner_agent_id,
            owner_agent_name=owner_agent_name,
            team_id=team_id,
            owner_named_agent_type=owner_named_agent_type,
            messages_json=messages_json,
            expect_existing=expect_existing,
        )

    def clear_deferred_messages(self, owner_agent_id: str) -> None:
        self._repository.clear_deferred_messages(owner_agent_id)

    def add_deferred_message(
        self,
        *,
        owner_agent_id: str,
        owner_agent_name: Optional[str],
        team_id: Optional[str],
        owner_named_agent_type: Optional[str],
        message_id: str,
        envelope_json: str,
        first: bool,
        expect_existing: bool,
    ) -> None:
        self._repository.add_deferred_message(
            owner_agent_id=owner_agent_id,
            owner_agent_name=owner_agent_name,
            team_id=team_id,
            owner_named_agent_type=owner_named_agent_type,
            message_id=message_id,
            envelope_json=envelope_json,
            first=first,
            expect_existing=expect_existing,
        )

    def remove_deferred_messages(
        self, *, owner_agent_id: str, message_ids: Sequence[str], expect_existing: bool,
    ) -> None:
        self._repository.remove_deferred_messages(
            owner_agent_id=owner_agent_id, message_ids=message_ids, expect_existing=expect_existing,
        )

    def get_deferred_messages_team_id(self, owner_agent_id: str) -> Optional[str]:
        return self._repository.get_deferred_messages_team_id(owner_agent_id)

    def load_deferred_messages_by_owner(self, owner_agent_id: str) -> List[DeferredMessageSnapshotRow]:
        return self._repository.load_deferred_messages_by_owner(owner_agent_id)


    def peek_deferred_messages_by_role(
        self,
        team_id: str,
        owner_named_agent_type: str,
    ) -> List[DeferredMessageSnapshotRow]:
        return self._repository.peek_deferred_messages_by_role(team_id, owner_named_agent_type)

    def record_task_cancellations(
        self,
        *,
        team_id: str,
        root_task_id: str,
        task_ids: List[str],
        canonical_user_id: Optional[str],
        command_id: str,
        reason: str,
        cancelled_at: str,
    ) -> None:
        self._repository.record_task_cancellations(
            team_id=team_id,
            root_task_id=root_task_id,
            task_ids=task_ids,
            canonical_user_id=canonical_user_id,
            command_id=command_id,
            reason=reason,
            cancelled_at=cancelled_at,
        )

    def get_task_cancellation(
        self,
        *,
        team_id: str,
        task_id: str,
    ) -> Optional[RuntimeTaskCancellationRow]:
        return self._repository.get_task_cancellation(
            team_id=team_id,
            task_id=task_id,
        )
