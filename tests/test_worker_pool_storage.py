"""Delegated-task records, worker pool limits, incidents and events on real SQLite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from storage import resolve_storage_settings
from storage.delegated_task_repository import DelegatedTaskRepository, status_counts
from storage.worker_pool_repository import (
    INCIDENT_CAPACITY,
    TEAM_SCOPE,
    ProviderLimitEventV1,
    WorkerPoolRepository,
)


@pytest.fixture()
def settings(tmp_path):
    return resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "pools.db")}})


def _publish(repository, task_id="a", parent="p", sequence=5):
    return repository.record_published(
        team_id="t",
        task_id=task_id,
        parent_task_id=parent,
        root_task_id="root",
        requester_agent_id="lead",
        named_agent_type="WebResearchAgent",
        target_agent_name=None,
        publish_sequence=sequence,
    )


def _claim(repository, worker, task_id="a"):
    return repository.record_claim(
        team_id="t",
        task_id=task_id,
        worker_agent_id=worker,
        worker_agent_name=f"name-{worker}",
        claimed_at=datetime.now(timezone.utc),
    )


def test_a_delegated_task_is_queued_then_running_then_closed(settings):
    repository = DelegatedTaskRepository(settings)
    record = _publish(repository)
    assert (record.status, record.publish_sequence) == ("queued", 5)
    outcome = _claim(repository, "w1")
    assert (outcome.record.status, outcome.record.holder_agent_id, outcome.moved) == ("running", "w1", False)
    closed = repository.mark_closed(team_id="t", task_id="a", status="failed", failure_category="provider_limited")
    assert (closed.status, closed.failure_category) == ("failed", "provider_limited")
    # The first ending wins; a late claim reports the task as closed.
    assert repository.mark_closed(team_id="t", task_id="a", status="finished").status == "failed"
    assert _claim(repository, "w2").already_closed


def test_a_claim_from_another_worker_counts_one_move(settings):
    repository = DelegatedTaskRepository(settings)
    _publish(repository)
    _claim(repository, "w1")
    # The same worker after its own restart is not a move.
    assert _claim(repository, "w1").moved is False
    repository.mark_requeued(team_id="t", task_id="a", publish_sequence=9, from_holder_agent_id="w1")
    queued = repository.get("t", "a")
    assert (queued.status, queued.holder_agent_id, queued.publish_sequence) == ("queued", None, 9)
    outcome = _claim(repository, "w2")
    assert outcome.moved
    assert outcome.record.interruptions == 1
    assert outcome.record.holders == ["w1", "w2"]
    assert _claim(repository, "w3").record.interruptions == 2


def test_recording_a_publication_never_undoes_a_claim(settings):
    """Regression (2026-10-17): a worker's claim handled before the requester wrote
    the publication record (the claim handler creates it) was reset to "queued"
    with no holder when the record was written; stops then never reached the worker."""

    repository = DelegatedTaskRepository(settings)
    _publish(repository, sequence=None)  # the claim handler's record for a claim that came first
    _claim(repository, "w1")
    again = _publish(repository, sequence=None)  # the outbox's own write, or an outbox retry
    assert (again.status, again.holder_agent_id, again.holders) == ("running", "w1", ["w1"])
    filled = repository.set_publish_sequence(team_id="t", task_id="a", publish_sequence=7)
    assert (filled.status, filled.holder_agent_id, filled.publish_sequence) == ("running", "w1", 7)
    closed = repository.mark_closed(team_id="t", task_id="a", status="finished")
    assert _publish(repository).status == closed.status == "finished"


def test_requeue_after_a_handback_keeps_a_newer_claim(settings):
    repository = DelegatedTaskRepository(settings)
    _publish(repository)
    _claim(repository, "w1")
    requeued = repository.mark_requeued(team_id="t", task_id="a", publish_sequence=None, from_holder_agent_id="w1")
    assert (requeued.status, requeued.holder_agent_id) == ("queued", None)
    _claim(repository, "w2")  # the re-sent task's new worker claims it
    # A retried handback of w1's task must not undo w2's claim.
    assert repository.mark_requeued(team_id="t", task_id="a", publish_sequence=None, from_holder_agent_id="w1") is None
    assert repository.get("t", "a").holder_agent_id == "w2"


def test_a_claim_for_an_unknown_task_returns_no_record(settings):
    assert _claim(DelegatedTaskRepository(settings), "w1", task_id="missing").record is None


def test_listing_by_parent_root_and_requester(settings):
    repository = DelegatedTaskRepository(settings)
    for index in range(3):
        _publish(repository, task_id=f"t{index}")
    _publish(repository, task_id="other", parent="p2")
    repository.mark_closed(team_id="t", task_id="t0", status="finished")
    assert [r.task_id for r in repository.list_for_parent("t", "p")] == ["t0", "t1", "t2"]
    assert len(repository.list_for_root("t", "root")) == 4
    assert {r.task_id for r in repository.list_open("t", "lead")} == {"t1", "t2", "other"}
    assert status_counts(list(repository.list_for_parent("t", "p"))) == {
        "queued": 2,
        "running": 0,
        "finished": 1,
        "failed": 0,
        "cancelled": 0,
    }


def test_waits_hold_check_back_times_and_last_progress(settings):
    repository = DelegatedTaskRepository(settings)
    at = datetime.now(timezone.utc) + timedelta(minutes=5)
    repository.set_check_back(team_id="t", parent_task_id="p", requester_agent_id="lead", at=at)
    repository.record_progress(
        team_id="t", parent_task_id="p", requester_agent_id="lead", counts={"queued": 2}, at=at
    )
    wait = repository.get_wait("t", "p")
    assert wait.check_back_at_utc == at
    assert wait.last_progress_counts == {"queued": 2}
    repository.set_check_back(team_id="t", parent_task_id="p", requester_agent_id="lead", at=None)
    assert repository.waits_for_requester("t", "lead")[0].check_back_at_utc is None


def test_prune_keeps_open_work(settings):
    repository = DelegatedTaskRepository(settings)
    _publish(repository, task_id="open")
    _publish(repository, task_id="done")
    repository.mark_closed(team_id="t", task_id="done", status="finished")
    pruned = repository.prune("t", before=datetime.now(timezone.utc) + timedelta(seconds=5))
    assert pruned == 1
    assert repository.get("t", "open") is not None


def test_pool_limits_lasting_and_temporary(settings):
    repository = WorkerPoolRepository(settings)
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    repository.set_limit(team_id="t", scope="WebResearchAgent", max_workers=6, until_utc=None, set_by="lead", reason="busy")
    record = repository.set_limit(
        team_id="t", scope="WebResearchAgent", max_workers=10, until_utc=until, set_by="lead", reason="burst"
    )
    assert (record.lasting_max_workers, record.temporary_max_workers) == (6, 10)
    assert [change.max_workers for change in record.history] == [6, 10]
    # A lasting size replaces the temporary one.
    record = repository.set_limit(team_id="t", scope="WebResearchAgent", max_workers=3, until_utc=None, set_by="lead", reason="provider")
    assert (record.lasting_max_workers, record.temporary_max_workers) == (3, None)
    repository.set_limit(team_id="t", scope=TEAM_SCOPE, max_workers=20, until_utc=None, set_by="lead", reason="team")
    assert {item.scope for item in repository.list_limits("t")} == {"WebResearchAgent", TEAM_SCOPE}


def test_incidents_open_refresh_review_and_close(settings):
    repository = WorkerPoolRepository(settings)
    now = datetime.now(timezone.utc)
    incident, created = repository.record_incident(
        team_id="t", agent_type="WebResearchAgent", kind=INCIDENT_CAPACITY, details={"a": 1}, now=now
    )
    assert created
    again, created = repository.record_incident(
        team_id="t", agent_type="WebResearchAgent", kind=INCIDENT_CAPACITY, details={"a": 2}, now=now + timedelta(seconds=5)
    )
    assert not created and again.incident_id == incident.incident_id and again.details == {"a": 2}
    reviewed = repository.mark_reviewed(again, review_task_id="review_1")
    assert reviewed.review_count == 1
    repository.close_incident(reviewed)
    assert repository.open_incident("t", "WebResearchAgent", INCIDENT_CAPACITY) is None
    assert len(repository.recent_incidents("t", since=now - timedelta(minutes=1))) == 1
    _, created = repository.record_incident(
        team_id="t", agent_type="WebResearchAgent", kind=INCIDENT_CAPACITY, details={}, now=now + timedelta(minutes=1)
    )
    assert created
    assert len(repository.recent_incidents("t", since=now - timedelta(minutes=1), agent_type="WebResearchAgent")) == 2


def test_provider_limit_events_are_counted_in_a_window(settings):
    repository = WorkerPoolRepository(settings)
    now = datetime.now(timezone.utc)
    for minutes_ago in (1, 2, 30):
        repository.record_provider_limit(
            ProviderLimitEventV1(
                team_id="t",
                agent_type="WebResearchAgent",
                agent_id="w1",
                status_code=429,
                at_utc=now - timedelta(minutes=minutes_ago),
            )
        )
    recent = repository.provider_limit_events("t", since=now - timedelta(minutes=10))
    assert len(recent) == 2
    repository.prune("t", before=now - timedelta(minutes=10))
    assert len(repository.provider_limit_events("t", since=now - timedelta(days=1))) == 2
