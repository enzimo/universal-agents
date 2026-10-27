"""Durable conversation store backed by the selected storage repository."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from default_values import STORAGE_DEFAULTS
from storage.backends import STORAGE_BACKEND_SQLITE
from storage.config import StorageSettings, resolve_storage_settings
from storage.conversation_repository import (
    create_conversation_repository,
    default_synopsis,
)


class ConversationStore:
    """
    Persist durable multi-turn conversation thread state.

    This class is intentionally persistence-focused. Prompt construction and
    compaction policy live in pluggable compactor implementations.
    """

    def __init__(
        self,
        db_path: str,
        *,
        artifacts_dir: str | Path,
        logger: Optional[logging.Logger] = None,
        storage_settings: Optional[StorageSettings] = None,
    ) -> None:
        """``artifacts_dir`` is ``InstanceLayout.conversation_artifacts_dir`` (``storage.conversation_repository``)."""

        resolved_db_path = storage_settings.sqlite_path if storage_settings and storage_settings.sqlite_path else db_path
        self._db_path = resolved_db_path
        self._logger = logger or logging.getLogger(__name__)
        resolved_artifacts_dir = str(Path(artifacts_dir))
        self._artifacts_dir = Path(resolved_artifacts_dir)
        self._storage_settings = storage_settings or resolve_storage_settings(
            {"backend": STORAGE_DEFAULTS.backend.value, STORAGE_BACKEND_SQLITE: {"path": db_path}}
        )
        self._repository = create_conversation_repository(
            self._storage_settings,
            logger=self._logger,
            artifacts_dir=resolved_artifacts_dir,
        )

    @staticmethod
    def default_synopsis() -> dict[str, Any]:
        return default_synopsis()

    def ensure_thread(
        self,
        *,
        conversation_id: Optional[str],
        team_id: Optional[str],
        owner_agent_id: Optional[str],
        owner_agent_name: Optional[str],
        requester_name: Optional[str],
        requester_id: Optional[str],
        root_task_id: str,
        subject_preview: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._repository.ensure_thread(
            conversation_id=conversation_id,
            team_id=team_id,
            owner_canonical_user_id=owner_canonical_user_id,
            owner_agent_id=owner_agent_id,
            owner_agent_name=owner_agent_name,
            requester_name=requester_name,
            requester_id=requester_id,
            root_task_id=root_task_id,
            subject_preview=subject_preview,
        )

    def get_thread(self, conversation_id: str) -> Optional[dict[str, Any]]:
        return self._repository.get_thread(conversation_id)

    def update_thread_compaction(
        self,
        conversation_id: str,
        *,
        synopsis_json: Optional[dict[str, Any]] = None,
        synopsis_markdown: Optional[str] = None,
        summarized_through_turn_index: Optional[int] = None,
        last_token_estimate: Optional[int] = None,
        last_compacted_at: Optional[str] = None,
        compaction_version: Optional[int] = None,
        compactor_type: Optional[str] = None,
        compaction_state: Optional[dict[str, Any]] = None,
        status: Optional[str] = None,
        latest_task_id: Optional[str] = None,
    ) -> None:
        self._repository.update_thread_compaction(
            conversation_id,
            synopsis_json=synopsis_json,
            synopsis_markdown=synopsis_markdown,
            summarized_through_turn_index=summarized_through_turn_index,
            last_token_estimate=last_token_estimate,
            last_compacted_at=last_compacted_at,
            compaction_version=compaction_version,
            compactor_type=compactor_type,
            compaction_state=compaction_state,
            status=status,
            latest_task_id=latest_task_id,
        )

    def update_thread_token_estimate(
        self,
        conversation_id: str,
        *,
        token_estimate: int,
        compactor_type: Optional[str] = None,
        compaction_state: Optional[dict[str, Any]] = None,
    ) -> None:
        self.update_thread_compaction(
            conversation_id,
            last_token_estimate=token_estimate,
            compactor_type=compactor_type,
            compaction_state=compaction_state,
        )

    def get_turn_by_task_id(self, task_id: str) -> Optional[dict[str, Any]]:
        return self._repository.get_turn_by_task_id(task_id)

    def get_latest_task_link(self, conversation_id: str) -> Optional[dict[str, Any]]:
        return self._repository.get_latest_task_link(conversation_id)

    def append_turn(
        self,
        *,
        conversation_id: str,
        task_id: str,
        followup_to_task_id: Optional[str],
        owner_agent_id: Optional[str],
        requester_name: Optional[str],
        requester_id: Optional[str],
        correlation_id: Optional[str],
        problem_text: str,
        owner_named_agent_type: Optional[str] = None,
        requester_type: Optional[str] = None,
        delivery_mode: Optional[str] = None,
        delivery_state: Optional[str] = None,
        gateway_context: Optional[dict[str, Any]] = None,
        task_status: str = "accepted",
        memory_review_status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._repository.append_turn(
            conversation_id=conversation_id,
            task_id=task_id,
            followup_to_task_id=followup_to_task_id,
            owner_agent_id=owner_agent_id,
            owner_named_agent_type=owner_named_agent_type,
            requester_name=requester_name,
            requester_id=requester_id,
            correlation_id=correlation_id,
            problem_text=problem_text,
            requester_type=requester_type,
            delivery_mode=delivery_mode,
            delivery_state=delivery_state,
            gateway_context=gateway_context,
            task_status=task_status,
            memory_review_status=memory_review_status,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def append_clarification(
        self,
        *,
        task_id: str,
        message: str,
    ) -> None:
        self._repository.append_clarification(task_id=task_id, message=message)

    def update_turn_completion(
        self,
        *,
        task_id: str,
        task_status: str,
        final_answer_text: str,
        turn_summary_text: str,
        clarification_summary_text: Optional[str] = None,
        execution_history_text: Optional[str] = None,
        tool_artifact_manifest: Optional[list[dict[str, Any]]] = None,
        user_request_summary: Optional[str] = None,
        assistant_response_summary: Optional[str] = None,
        context_summary_text: Optional[str] = None,
        artifact_summary: Optional[list[dict[str, Any]]] = None,
        summary_generator: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        return self._repository.update_turn_completion(
            task_id=task_id,
            task_status=task_status,
            final_answer_text=final_answer_text,
            turn_summary_text=turn_summary_text,
            clarification_summary_text=clarification_summary_text,
            execution_history_text=execution_history_text,
            tool_artifact_manifest=tool_artifact_manifest,
            user_request_summary=user_request_summary,
            assistant_response_summary=assistant_response_summary,
            context_summary_text=context_summary_text,
            artifact_summary=artifact_summary,
            summary_generator=summary_generator,
        )

    def update_turn_delivery(
        self,
        *,
        task_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        delivery_state: str,
        delivery_via: Optional[str] = None,
        delivered_at: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        return self._repository.update_turn_delivery(
            task_id=task_id,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            delivery_state=delivery_state,
            delivery_via=delivery_via,
            delivered_at=delivered_at,
        )

    def list_conversations(
        self,
        *,
        limit: int = 10,
        owner_canonical_user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return self._repository.list_conversations(
            limit=limit,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def get_conversation_turns(
        self,
        *,
        conversation_id: str,
        start_turn: int = 1,
        limit: int = 10,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return self._repository.get_conversation_turns(
            conversation_id=conversation_id,
            start_turn=start_turn,
            limit=limit,
            created_after=created_after,
            created_before=created_before,
        )

    def get_recent_turns(
        self,
        conversation_id: str,
        limit: Optional[int] = None,
        *,
        include_pending_delivery: bool = True,
    ) -> list[dict[str, Any]]:
        return self._repository.get_recent_turns(
            conversation_id,
            limit=limit,
            include_pending_delivery=include_pending_delivery,
        )

    def get_conversation_summary(self, conversation_id: str) -> dict[str, Any]:
        thread = self.get_thread(conversation_id)
        if not thread:
            return {"error": f"Conversation {conversation_id} not found"}
        turns = self.get_recent_turns(conversation_id)
        return {
            "thread": thread,
            "turn_count": len(turns),
            "turns": turns,
        }

    def tag_thread(
        self,
        *,
        conversation_id: str,
        tag: str,
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        source: str = "manual",
        created_by_agent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._repository.tag_thread(
            conversation_id=conversation_id,
            tag=tag,
            team_id=team_id,
            canonical_user_id=canonical_user_id,
            source=source,
            created_by_agent_id=created_by_agent_id,
        )

    def untag_thread(self, *, conversation_id: str, tag: str) -> bool:
        return self._repository.untag_thread(conversation_id=conversation_id, tag=tag)

    def list_thread_tags(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._repository.list_thread_tags(conversation_id)

    def list_known_thread_tags(
        self,
        *,
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self._repository.list_known_thread_tags(
            team_id=team_id,
            canonical_user_id=canonical_user_id,
            limit=limit,
        )

    def find_threads_by_tags(
        self,
        *,
        tags: list[str],
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return self._repository.find_threads_by_tags(
            tags=tags,
            team_id=team_id,
            canonical_user_id=canonical_user_id,
            limit=limit,
        )

    def tag_turn(
        self,
        *,
        conversation_id: str,
        turn_index: int,
        tag: str,
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        source: str = "manual",
        created_by_agent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._repository.tag_turn(
            conversation_id=conversation_id,
            turn_index=turn_index,
            tag=tag,
            team_id=team_id,
            canonical_user_id=canonical_user_id,
            source=source,
            created_by_agent_id=created_by_agent_id,
        )

    def list_turn_tags(self, *, conversation_id: str, turn_index: int) -> list[dict[str, Any]]:
        return self._repository.list_turn_tags(conversation_id=conversation_id, turn_index=turn_index)

    def get_memory_review_window(
        self,
        *,
        conversation_id: str,
        max_unreviewed_turns: int,
        overlap_turns: int = 0,
        allow_partial: bool = False,
    ) -> Optional[dict[str, Any]]:
        return self._repository.get_memory_review_window(
            conversation_id=conversation_id,
            max_unreviewed_turns=max_unreviewed_turns,
            overlap_turns=overlap_turns,
            allow_partial=allow_partial,
        )

    def list_conversation_memory_review_candidates(
        self,
        *,
        team_id: Optional[str] = None,
        stale_before_utc: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._repository.list_conversation_memory_review_candidates(
            team_id=team_id,
            stale_before_utc=stale_before_utc,
            canonical_user_id=canonical_user_id,
            limit=limit,
        )

    def mark_turns_memory_review_queued(
        self,
        *,
        conversation_id: str,
        start_turn: int,
        end_turn: int,
        review_job_id: str,
    ) -> int:
        return self._repository.mark_turns_memory_review_queued(
            conversation_id=conversation_id,
            start_turn=start_turn,
            end_turn=end_turn,
            review_job_id=review_job_id,
        )

    def mark_turns_memory_reviewed(
        self,
        *,
        conversation_id: str,
        start_turn: int,
        end_turn: int,
        review_job_id: str,
        reviewed_by_agent_id: Optional[str] = None,
    ) -> int:
        return self._repository.mark_turns_memory_reviewed(
            conversation_id=conversation_id,
            start_turn=start_turn,
            end_turn=end_turn,
            review_job_id=review_job_id,
            reviewed_by_agent_id=reviewed_by_agent_id,
        )

    def mark_turns_memory_review_failed(
        self,
        *,
        conversation_id: str,
        start_turn: int,
        end_turn: int,
        review_job_id: str,
    ) -> int:
        return self._repository.mark_turns_memory_review_failed(
            conversation_id=conversation_id,
            start_turn=start_turn,
            end_turn=end_turn,
            review_job_id=review_job_id,
        )

    def offload_large_text(
        self,
        *,
        conversation_id: str,
        task_id: str,
        artifact_type: str,
        raw_text: str,
        summary_text: str,
        extension: str = "txt",
    ) -> dict[str, Any]:
        return self._repository.offload_large_text(
            conversation_id=conversation_id,
            task_id=task_id,
            artifact_type=artifact_type,
            raw_text=raw_text,
            summary_text=summary_text,
            extension=extension,
        )

    def delete_artifacts_created_before(self, cutoff: str, *, limit: int) -> tuple[str, ...]:
        """Delete the oldest artifact records created before ``cutoff``; their files' paths (``custom_agents.output_retention``)."""

        return self._repository.delete_artifacts_created_before(cutoff, limit=limit)

    def create_context_epoch(self, **kwargs: Any) -> dict[str, Any]:
        return self._repository.create_context_epoch(**kwargs)

    def get_latest_context_epoch(self, conversation_id: str) -> Optional[dict[str, Any]]:
        return self._repository.get_latest_context_epoch(conversation_id)

    def list_context_epochs(self, conversation_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self._repository.list_context_epochs(conversation_id, limit=limit)

    def get_context_epoch(self, context_epoch_id: str) -> Optional[dict[str, Any]]:
        return self._repository.get_context_epoch(context_epoch_id)

    def mark_context_epoch_failed(self, context_epoch_id: str, *, error_text: str) -> Optional[dict[str, Any]]:
        return self._repository.mark_context_epoch_failed(context_epoch_id, error_text=error_text)
