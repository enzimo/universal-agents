"""Bounded metadata review and archival of memory-review jobs for internal maintenance.

Who may inspect, archive or purge is decided per call by the access hook
(``custom_agents.access_runtime``): the tools are in the ``system_duties`` set
that only system agents hold (MemReviewAgent, and InternalImprovementAgent,
whose maintenance runs work for no person); any other agent needs a grant
(purge needs ``full``). This service keeps the bounded, digest-checked batch
rules and records the calling agent as the actor.
"""

from datetime import datetime, timezone
from typing import Any

from custom_agents.access_runtime import agent_principal
from custom_agents.memory_review_cleanup_runtime import (
    cleanup_settings,
    protected_live_worker_ids,
)
from custom_messages.contracts.common_v1 import contract_sha256
from storage.memory_review_cleanup_repository import MemoryReviewCleanupRepository


class MemoryReviewCleanupService:
    def __init__(self, agent: Any):
        self.agent = agent
        self.actor_id = agent_principal(agent).agent_id
        self.repository = MemoryReviewCleanupRepository(
            agent.task_manager.storage_settings
        )
        self.settings = cleanup_settings(agent)

    def inspect(self, limit: int) -> dict[str, Any]:
        rows = self.repository.inspect(
            self.agent.team_id, limit=min(max(1, limit), self.settings.batch_size)
        )
        return {
            "team_id": self.agent.team_id,
            "jobs": rows,
            "archive_retention_days": self.settings.archive_retention_days,
            "outcome": "unknown_unless_verified",
            "next_step": "Review these metadata records. Archive exact expired IDs; purge archived content only after retention. If a call is refused with access_denied, follow the refusal's next_step. No memory facts are included.",
        }

    def maintain(
        self, ids: list[str], operation: str, reason: str, task_id: str
    ) -> dict[str, Any]:
        if not task_id or not reason.strip() or len(reason) > 2000:
            raise ValueError("An active task and a brief cleanup reason are required.")
        if (
            operation not in {"archive", "purge"}
            or not 1 <= len(ids) <= self.settings.batch_size
        ):
            raise ValueError(
                "Choose archive or purge and a bounded list of review IDs."
            )
        rows = self.repository.selected(self.agent.team_id, ids)
        now = datetime.now(timezone.utc)
        for row in rows:
            self.repository.validate_operation(
                row, operation, now, self.settings.archive_retention_days
            )
        state_digest = contract_sha256({"jobs": rows})
        changed = self.repository.maintain(
            self.agent.team_id,
            ids,
            operation=operation,
            expected_digest=state_digest,
            actor_id=self.actor_id,
            reason=reason,
            now=now,
            retention_days=self.settings.archive_retention_days,
            protected_workers=protected_live_worker_ids(self.agent),
        )
        return {
            "status": "ok",
            "operation": operation,
            "review_job_ids": changed,
            "outcome": "not_marked_complete",
        }
