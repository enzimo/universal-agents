"""Retire abandoned review ownership and queue bounded internal maintenance."""

from datetime import datetime, timedelta, timezone
from functools import partial
from time import monotonic
from typing import Any

from custom_agents.internal_improvement_runtime import (
    build_internal_improvement_schedule,
    prepare_internal_improvement_schedule_for_manager,
)
from runtime_settings.review_cleanup import ReviewCleanupSettings
from storage.memory_review_cleanup_repository import MemoryReviewCleanupRepository


def cleanup_settings(agent: Any) -> ReviewCleanupSettings:
    return ReviewCleanupSettings.model_validate(
        (getattr(agent, "schedule_maintenance", {}) or {}).get("memory_review_cleanup")
        or {}
    )


def protected_live_worker_ids(agent: Any) -> tuple[str, ...]:
    runtime = getattr(agent, "_local_team_runtime", None)
    if runtime is None:
        raise ValueError("Local worker registry is required for online review cleanup.")
    workers = {worker.self_id for worker in runtime.local_workers(agent.team_id)}
    # Conversation-triggered reviews can still arrive over NATS. A known peer
    # must not be treated as abandoned merely because it runs on another host.
    known = getattr(agent, "known_agents", None)
    if known is not None:
        workers.update(
            status.self_id
            for status in known.agent_map.values()
            if status.team_id == agent.team_id and not status.is_exiting
        )
    return tuple(sorted(workers))


def scan_review_cleanup(
    agent: Any, protected_workers: tuple[str, ...]
) -> dict[str, Any]:
    settings = cleanup_settings(agent)
    if not settings.enabled:
        return {"retired": [], "maintenance_pending": False}
    now = datetime.now(timezone.utc)
    repository = MemoryReviewCleanupRepository(agent.task_manager.storage_settings)
    before = now - timedelta(seconds=settings.expired_claim_grace_seconds)
    candidates = repository.candidates(
        agent.team_id, before=before, protected_workers=protected_workers
    )
    retired = (
        repository.retire_expired(
            agent.team_id,
            before=before,
            now=now,
            actor_id="runtime:memory_review_cleanup",
            limit=settings.batch_size,
            protected_workers=protected_workers,
        )
        if candidates
        else []
    )
    pending = repository.inspect(agent.team_id, limit=1)
    if pending:
        # Install the existing maintenance route even if no InternalImprovement
        # worker has run yet. Never re-enable or replace an existing schedule.
        schedule = build_internal_improvement_schedule(agent)
        if schedule is not None:
            schedule = prepare_internal_improvement_schedule_for_manager(
                agent, schedule
            )
            if schedule is not None:
                schedule.next_fire_at = now.isoformat()
                agent._schedule_store.add_if_absent(schedule)
    return {"retired": retired, "maintenance_pending": bool(pending)}


class ReviewCleanupController:
    def __init__(self, agent: Any):
        self.agent = agent
        self.next_scan = 0.0

    async def reconcile(self) -> None:
        if monotonic() < self.next_scan:
            return
        settings = cleanup_settings(self.agent)
        self.next_scan = monotonic() + settings.scan_interval_seconds
        if not settings.enabled:
            return
        protected = protected_live_worker_ids(self.agent)
        await self.agent.task_manager.operations_runtime.run(
            partial(scan_review_cleanup, self.agent),
            protected,
        )
