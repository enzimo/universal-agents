from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from custom_agents.memory_review_store import MemoryReviewStore


def test_memory_review_store_jobs_claim_and_complete(tmp_path: Path) -> None:
    store = MemoryReviewStore(str(tmp_path / "review.db"), logger=logging.getLogger("test"))
    job = store.enqueue_review_job(
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        task_id="task_review",
        conversation_id="conversation_review",
        turn_index=3,
        source_payload={"review_kind": "conversation_window"},
    )

    fetched = store.get_review_job(job.review_job_id)
    assert fetched is not None
    assert fetched.source_payload["review_kind"] == "conversation_window"

    claimed = store.claim_review_jobs_for_role(
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id="agent_1",
        limit=1,
    )
    assert [item.review_job_id for item in claimed] == [job.review_job_id]

    store.mark_review_job_completed(job.review_job_id, claimer_agent_id="agent_1")
    completed = store.get_review_job(job.review_job_id)
    assert completed is not None
    assert completed.status == "completed"


def test_memory_review_store_does_not_replay_expired_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "review.db"
    store = MemoryReviewStore(str(db_path), logger=logging.getLogger("test"))
    job = store.enqueue_review_job(
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        task_id="task_review",
        conversation_id="conversation_review",
        turn_index=1,
        source_payload={"review_kind": "usermem_audit"},
    )
    first_claim = store.claim_review_jobs_for_role(
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id="agent_1",
        limit=1,
        claim_ttl_seconds=1,
    )
    assert first_claim[0].review_job_id == job.review_job_id

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE memory_review_jobs SET claim_expires_at = ? WHERE review_job_id = ?",
            ("2000-01-01T00:00:00+00:00", job.review_job_id),
        )
        conn.commit()

    second_claim = store.claim_review_jobs_for_role(
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id="agent_2",
        limit=1,
    )
    assert second_claim == []
    assert store.get_review_job(job.review_job_id).claim_owner_agent_id == "agent_1"


def test_memory_review_store_claims_exact_job_without_touching_role_backlog(tmp_path: Path) -> None:
    store = MemoryReviewStore(str(tmp_path / "review.db"), logger=logging.getLogger("test"))
    older_jobs = [
        store.enqueue_review_job(
            owner_team_id="team_test",
            owner_named_agent_type="MemReviewAgent",
            task_id=f"task_backlog_{index}",
            conversation_id="conversation_review",
            turn_index=index,
            source_payload={"review_kind": "conversation_window"},
        )
        for index in range(51)
    ]
    target = store.enqueue_review_job(
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        task_id="task_target",
        conversation_id="conversation_review",
        turn_index=52,
        source_payload={"review_kind": "conversation_window"},
    )

    claimed = store.claim_review_job(
        target.review_job_id,
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id="agent_target",
    )

    assert claimed is not None
    assert claimed.review_job_id == target.review_job_id
    assert claimed.claim_owner_agent_id == "agent_target"
    assert all(store.get_review_job(job.review_job_id).status == "queued" for job in older_jobs)


def test_memory_review_store_exact_claim_enforces_role_and_preserves_expired_job(tmp_path: Path) -> None:
    db_path = tmp_path / "review.db"
    store = MemoryReviewStore(str(db_path), logger=logging.getLogger("test"))
    job = store.enqueue_review_job(
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        task_id="task_review",
        conversation_id="conversation_review",
        turn_index=1,
        source_payload={"review_kind": "usermem_audit"},
    )

    assert store.claim_review_job(
        job.review_job_id,
        owner_team_id="other_team",
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id="agent_wrong_team",
    ) is None
    assert store.get_review_job(job.review_job_id).status == "queued"

    first_claim = store.claim_review_job(
        job.review_job_id,
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id="agent_1",
    )
    assert first_claim is not None
    assert store.claim_review_job(
        job.review_job_id,
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id="agent_2",
    ) is None

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE memory_review_jobs SET claim_expires_at = ? WHERE review_job_id = ?",
            ("2000-01-01T00:00:00+00:00", job.review_job_id),
        )
        conn.commit()

    reclaimed = store.claim_review_job(
        job.review_job_id,
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id="agent_2",
    )
    assert reclaimed is None
    assert store.get_review_job(job.review_job_id).claim_owner_agent_id == "agent_1"


def test_pending_query_filters_history_and_other_teams_before_limit(tmp_path: Path) -> None:
    store = MemoryReviewStore(str(tmp_path / "pending.db"))
    def enqueue(team, name):
        return store.enqueue_review_job(owner_team_id=team, owner_named_agent_type="MemReviewAgent", task_id=name, conversation_id=name, turn_index=1, source_payload={})
    completed = enqueue("team", "completed")
    store.claim_review_job(completed.review_job_id, owner_team_id="team", owner_named_agent_type="MemReviewAgent", claimer_agent_id="worker")
    store.mark_review_job_completed(completed.review_job_id, claimer_agent_id="worker")
    enqueue("other", "other-owner")
    pending = enqueue("team", "pending")
    rows = store.list_review_jobs(limit=1, owner_team_id="team", owner_named_agent_type="MemReviewAgent", pending_only=True)
    assert [row.review_job_id for row in rows] == [pending.review_job_id]
