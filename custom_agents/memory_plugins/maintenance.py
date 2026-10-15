from __future__ import annotations

import re
from typing import Any

from custom_agents.memory_review_runtime import enqueue_conversation_window_review_job
from custom_agents.memory_plugins.base import MemoryMaintenanceEvent, PruneResult, ReviewJobRef, VerifiedPersonOf
from custom_agents.memory_plugins.config import bank_automatic_creation_enabled
from storage.conversation_repository import normalize_topic_tag


class PinnedMemoryPrunerPlugin:
    plugin_id = "pinned_memory_pruner"

    def __init__(self, repository: Any = None):
        self.repository = repository

    def after_turn(self, request: Any) -> list[MemoryMaintenanceEvent]:
        return []

    def enqueue_review_jobs(self, request: Any) -> list[ReviewJobRef]:
        return []

    def compact(self, reason: Any, request: Any) -> Any:
        return None

    def prune(self, now_utc: str) -> PruneResult:
        if self.repository is None:
            return PruneResult(plugin_id=self.plugin_id, pruned_count=0)
        return PruneResult(plugin_id=self.plugin_id, pruned_count=self.repository.prune_expired(now_utc=now_utc))


class PinnedMemoryReviewPlugin:
    """Queues a conversation-window memory review after a person's turn.

    The job is made for the person the turn verifiably works for
    (``verified_person_of``, ``memory_plugins.base``): the review tools show a
    job only to that person, and the job holds their conversation turns. A turn
    for no person gets no job, since no one could review it; so does every turn
    when no ``verified_person_of`` is given (the manager always gives one).
    """

    plugin_id = "pinned_memory_review"

    def __init__(
        self,
        *,
        memory_review_store: Any = None,
        conversation_store: Any = None,
        team_id: str | None = None,
        named_agent_type: str | None = None,
        agent_id: str | None = None,
        memory_config: dict[str, Any] | None = None,
        verified_person_of: VerifiedPersonOf | None = None,
    ):
        self.memory_review_store = memory_review_store
        self.conversation_store = conversation_store
        self.team_id = team_id
        self.named_agent_type = named_agent_type
        self.agent_id = agent_id
        self.memory_config = memory_config or {}
        self.verified_person_of = verified_person_of

    def after_turn(self, request: Any) -> list[MemoryMaintenanceEvent]:
        return []

    def enqueue_review_jobs(self, request: Any) -> list[ReviewJobRef]:
        if not (
            bank_automatic_creation_enabled(self.memory_config, "coremem")
            or bank_automatic_creation_enabled(self.memory_config, "usermem")
        ):
            return []
        task_info = getattr(request, "task_info", None)
        task = getattr(task_info, "task", None)
        meta = getattr(task_info, "meta", None)
        task_id = getattr(task, "task_id", None)
        conversation_id = getattr(meta, "conversation_id", None)
        if not task_id or not conversation_id or self.verified_person_of is None:
            return []
        job = enqueue_conversation_window_review_job(
            memory_review_store=self.memory_review_store,
            conversation_store=self.conversation_store,
            team_id=self.team_id or "",
            named_agent_type=self.named_agent_type,
            agent_id=self.agent_id,
            task=task,
            msg_meta=meta,
            canonical_user_id=self.verified_person_of(meta),
            task_status=str(getattr(request, "task_status", "") or ""),
            memory_config=self.memory_config,
            trigger="turn_cadence",
            allow_partial=False,
            logger=getattr(request, "logger", None),
        )
        if job is None:
            return []
        return [job]

    def compact(self, reason: Any, request: Any) -> Any:
        return None

    def prune(self, now_utc: str) -> PruneResult:
        return PruneResult(plugin_id=self.plugin_id, pruned_count=0)


class DomainTopicTaggerPlugin(PinnedMemoryReviewPlugin):
    plugin_id = "domain_topic_tagger"

    def __init__(self, *, conversation_store: Any = None, team_id: str | None = None, agent_id: str | None = None):
        super().__init__()
        self.conversation_store = conversation_store
        self.team_id = team_id
        self.agent_id = agent_id

    def enqueue_review_jobs(self, request: Any) -> list[ReviewJobRef]:
        return []

    def after_turn(self, request: Any) -> list[MemoryMaintenanceEvent]:
        if self.conversation_store is None:
            return []
        task_info = getattr(request, "task_info", None)
        task = getattr(task_info, "task", None)
        meta = getattr(task_info, "meta", None)
        conversation_id = getattr(meta, "conversation_id", None)
        if not conversation_id:
            return []
        tags = self._extract_explicit_tags(str(getattr(task, "problem_statement", "") or ""))
        if not tags:
            return []
        rows = []
        for tag in tags:
            rows.append(
                self.conversation_store.tag_thread(
                    conversation_id=conversation_id,
                    tag=tag,
                    team_id=self.team_id,
                    canonical_user_id=None,
                    source="domain_topic_tagger",
                    created_by_agent_id=self.agent_id,
                )
            )
        return [
            MemoryMaintenanceEvent(
                event_type="thread_topic_tags_added",
                plugin_id=self.plugin_id,
                payload={
                    "conversation_id": conversation_id,
                    "tags": [row["tag"] for row in rows],
                },
            )
        ]

    @staticmethod
    def _extract_explicit_tags(text: str) -> list[str]:
        tags: list[str] = []
        seen: set[str] = set()
        for raw in re.findall(r"#([A-Za-z0-9_-]+)", text):
            tag = normalize_topic_tag(raw)
            if tag in seen:
                continue
            tags.append(tag)
            seen.add(tag)
        return tags


class MemoryNotificationPlugin(PinnedMemoryReviewPlugin):
    plugin_id = "memory_notification"

    def __init__(self, *, notification_sink: Any = None):
        super().__init__()
        self.notification_sink = notification_sink

    def enqueue_review_jobs(self, request: Any) -> list[ReviewJobRef]:
        return []
