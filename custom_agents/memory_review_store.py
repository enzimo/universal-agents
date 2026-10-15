"""Runtime-facing store for pinned-memory review jobs."""

from __future__ import annotations

import logging
from typing import Any, Optional

from storage.backends import STORAGE_BACKEND_SQLITE
from default_values import STORAGE_DEFAULTS
from storage.config import StorageSettings, resolve_storage_settings
from storage.memory_review_repository import (
    MemoryReviewJobRow,
    MemoryReviewRepository,
    create_memory_review_repository,
)


class MemoryReviewStore:
    """Wrapper for durable MemReview job persistence."""

    def __init__(
        self,
        db_path: str = STORAGE_DEFAULTS.task_history_db_path.value,
        logger: Optional[logging.Logger] = None,
        storage_settings: Optional[StorageSettings] = None,
    ):
        self._db_path = storage_settings.sqlite_path if storage_settings and storage_settings.sqlite_path else db_path
        self._logger = logger or logging.getLogger(__name__)
        self._storage_settings = storage_settings or resolve_storage_settings(
            {"backend": STORAGE_DEFAULTS.backend.value, STORAGE_BACKEND_SQLITE: {"path": db_path}}
        )
        self._repository: MemoryReviewRepository = create_memory_review_repository(
            self._storage_settings,
            logger=self._logger,
        )

    def enqueue_review_job(
        self,
        *,
        owner_team_id: str,
        owner_named_agent_type: str,
        task_id: str,
        conversation_id: str,
        turn_index: int,
        source_payload: dict[str, Any],
    ) -> MemoryReviewJobRow:
        return self._repository.enqueue_review_job(
            owner_team_id=owner_team_id,
            owner_named_agent_type=owner_named_agent_type,
            task_id=task_id,
            conversation_id=conversation_id,
            turn_index=turn_index,
            source_payload=source_payload,
        )

    def get_review_job(self, review_job_id: str) -> Optional[MemoryReviewJobRow]:
        return self._repository.get_review_job(review_job_id)

    def list_review_jobs(self, *, limit: int = 100, owner_team_id: str | None = None, owner_named_agent_type: str | None = None, pending_only: bool = False, offset: int = 0) -> list[MemoryReviewJobRow]:
        return self._repository.list_review_jobs(limit=limit, owner_team_id=owner_team_id, owner_named_agent_type=owner_named_agent_type, pending_only=pending_only, offset=offset)

    def claim_review_jobs_for_role(
        self,
        *,
        owner_team_id: str,
        owner_named_agent_type: str,
        claimer_agent_id: str,
        limit: int = 10,
        claim_ttl_seconds: int = 300,
    ) -> list[MemoryReviewJobRow]:
        return self._repository.claim_review_jobs_for_role(
            owner_team_id=owner_team_id,
            owner_named_agent_type=owner_named_agent_type,
            claimer_agent_id=claimer_agent_id,
            limit=limit,
            claim_ttl_seconds=claim_ttl_seconds,
        )

    def claim_review_job(
        self,
        review_job_id: str,
        *,
        owner_team_id: str,
        owner_named_agent_type: str,
        claimer_agent_id: str,
        claim_ttl_seconds: int = 300,
    ) -> Optional[MemoryReviewJobRow]:
        return self._repository.claim_review_job(
            review_job_id,
            owner_team_id=owner_team_id,
            owner_named_agent_type=owner_named_agent_type,
            claimer_agent_id=claimer_agent_id,
            claim_ttl_seconds=claim_ttl_seconds,
        )

    def mark_review_job_completed(self, review_job_id: str, *, claimer_agent_id: str) -> None:
        self._repository.mark_review_job_completed(review_job_id, claimer_agent_id=claimer_agent_id)

    def mark_review_job_failed(self, review_job_id: str, *, claimer_agent_id: str, error_text: str) -> None:
        self._repository.mark_review_job_failed(
            review_job_id,
            claimer_agent_id=claimer_agent_id,
            error_text=error_text,
        )

    def mark_review_job_skipped(
        self,
        review_job_id: str,
        *,
        claimer_agent_id: str,
        error_text: Optional[str] = None,
    ) -> None:
        self._repository.mark_review_job_skipped(
            review_job_id,
            claimer_agent_id=claimer_agent_id,
            error_text=error_text,
        )
