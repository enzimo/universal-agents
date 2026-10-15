"""Run the cleanup lifecycle on each supported persistence backend."""

# ruff: noqa: F811 -- imported parameterized database fixture
from datetime import datetime, timedelta, timezone

from custom_agents.memory_review_store import MemoryReviewStore
from custom_messages.contracts.common_v1 import contract_sha256
from storage.memory_review_cleanup_repository import (
    MemoryReviewCleanupRepository,
    EVENT_TABLE,
)
from storage.runtime_snapshot_maintenance import _connection
from tests.test_runtime_snapshot_maintenance import seeded_store  # noqa: F401


def test_cleanup_transaction_and_audit_on_supported_backends(seeded_store):
    settings, _ = seeded_store
    store = MemoryReviewStore(storage_settings=settings)
    job = store.enqueue_review_job(
        owner_team_id="team",
        owner_named_agent_type="MemReviewAgent",
        task_id="source",
        conversation_id="thread",
        turn_index=0,
        source_payload={"review_kind": "coremem_audit", "private": "retained"},
    )
    store.claim_review_job(
        job.review_job_id,
        owner_team_id="team",
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id="abandoned",
    )
    now = datetime.now(timezone.utc)
    with _connection(settings) as conn:
        conn.execute(
            f"UPDATE memory_review_jobs SET claim_expires_at={conn.dialect.bind} WHERE review_job_id={conn.dialect.bind}",
            ((now - timedelta(days=90)).isoformat(), job.review_job_id),
        )
    repo = MemoryReviewCleanupRepository(settings)
    assert repo.retire_expired(
        "team", before=now, now=now, actor_id="runtime", limit=10
    ) == [job.review_job_id]
    for operation, date in (("archive", now), ("purge", now + timedelta(days=90))):
        assert repo.maintain(
            "team",
            [job.review_job_id],
            operation=operation,
            expected_digest=contract_sha256(
                {"jobs": repo.selected("team", [job.review_job_id])}
            ),
            actor_id="maintenance",
            reason="Abandoned; outcome unknown",
            now=date,
            retention_days=90,
        ) == [job.review_job_id]
    assert store.get_review_job(job.review_job_id) is None
    with _connection(settings) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {EVENT_TABLE}").fetchone()[0] == 3
