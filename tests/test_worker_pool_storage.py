"""Delegated-task records on real SQLite and PostgreSQL; worker pool limits, incidents and events on real SQLite."""

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
from tests.delegated_task_support import BACKENDS, delegated_task_storage


@pytest.fixture()
def settings(tmp_path):
    return resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "pools.db")}})


@pytest.fixture(params=BACKENDS)
def repository(tmp_path, request):
    with delegated_task_storage(request.param, tmp_path) as storage:
        yield DelegatedTaskRepository(storage)


def _publish(repository, task_id="a", parent="p", sequence=5, requester="lead"):
    return repository.record_published(
        team_id="t",
        task_id=task_id,
        parent_task_id=parent,
        root_task_id="root",
        requester_agent_id=requester,
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


def test_a_delegated_task_is_queued_then_running_then_closed(repository):
    record = _publish(repository)
    assert (record.status, record.publish_sequence) == ("queued", 5)
    outcome = _claim(repository, "w1")
    assert (outcome.record.status, outcome.record.holder_agent_id, outcome.moved) == ("running", "w1", False)
    closed = repository.mark_closed(team_id="t", task_id="a", status="failed", failure_category="provider_limited")
    assert (closed.status, closed.failure_category) == ("failed", "provider_limited")
    # The first ending wins; a late claim reports the task as closed.
    assert repository.mark_closed(team_id="t", task_id="a", status="finished").status == "failed"
    assert _claim(repository, "w2").already_closed


def test_a_claim_from_another_worker_counts_one_move(repository):
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


def test_recording_a_publication_never_undoes_a_claim(repository):
    """Regression (2026-10-17): a worker's claim handled before the requester wrote
    the publication record (the claim handler creates it) was reset to "queued"
    with no holder when the record was written; stops then never reached the worker."""

    _publish(repository, sequence=None)  # the claim handler's record for a claim that came first
    _claim(repository, "w1")
    again = _publish(repository, sequence=None)  # the outbox's own write, or an outbox retry
    assert (again.status, again.holder_agent_id, again.holders) == ("running", "w1", ["w1"])
    filled = repository.set_publish_sequence(team_id="t", task_id="a", publish_sequence=7)
    assert (filled.status, filled.holder_agent_id, filled.publish_sequence) == ("running", "w1", 7)
    closed = repository.mark_closed(team_id="t", task_id="a", status="finished")
    assert _publish(repository).status == closed.status == "finished"


def test_requeue_after_a_handback_keeps_a_newer_claim(repository):
    _publish(repository)
    _claim(repository, "w1")
    requeued = repository.mark_requeued(team_id="t", task_id="a", publish_sequence=None, from_holder_agent_id="w1")
    assert (requeued.status, requeued.holder_agent_id) == ("queued", None)
    _claim(repository, "w2")  # the re-sent task's new worker claims it
    # A retried handback of w1's task must not undo w2's claim.
    assert repository.mark_requeued(team_id="t", task_id="a", publish_sequence=None, from_holder_agent_id="w1") is None
    assert repository.get("t", "a").holder_agent_id == "w2"


def test_a_claim_for_an_unknown_task_returns_no_record(repository):
    assert _claim(repository, "w1", task_id="missing").record is None


def test_listing_by_parent_and_root(repository):
    for index in range(3):
        _publish(repository, task_id=f"t{index}")
    _publish(repository, task_id="other", parent="p2")
    repository.mark_closed(team_id="t", task_id="t0", status="finished")
    assert [r.task_id for r in repository.list_for_parent("t", "p")] == ["t0", "t1", "t2"]
    assert len(repository.list_for_root("t", "root")) == 4
    assert status_counts(list(repository.list_for_parent("t", "p"))) == {
        "queued": 2,
        "running": 0,
        "finished": 1,
        "failed": 0,
        "cancelled": 0,
    }


def test_open_records_are_read_by_parent_whoever_requested_them(repository):
    """After recovery a parent belongs to an agent with a new id; its records still
    name the earlier requester and must be found by parent (review R2)."""

    _publish(repository, task_id="before_restart", parent="p", requester="lead_before")
    _publish(repository, task_id="running", parent="p", requester="lead_now")
    _claim(repository, "w1", task_id="running")
    _publish(repository, task_id="done", parent="p")
    repository.mark_closed(team_id="t", task_id="done", status="finished")
    _publish(repository, task_id="second", parent="p2")
    _publish(repository, task_id="not_mine", parent="someone_elses")
    found = repository.list_open_for_parents("t", ["p2", "p", "p", "unknown"])
    assert [(r.parent_task_id, r.task_id, r.status) for r in found] == [
        ("p", "before_restart", "queued"),
        ("p", "running", "running"),
        ("p2", "second", "queued"),
    ]
    assert repository.list_open_for_parents("other_team", ["p"]) == ()
    assert repository.list_open_for_parents("t", []) == ()


def test_waits_hold_check_back_times_and_last_progress(repository):
    at = datetime.now(timezone.utc) + timedelta(minutes=5)
    repository.set_check_back(team_id="t", parent_task_id="p", requester_agent_id="lead", at=at)
    repository.record_progress(
        team_id="t", parent_task_id="p", requester_agent_id="lead", counts={"queued": 2}, at=at
    )
    wait = repository.get_wait("t", "p")
    assert wait.check_back_at_utc == at
    assert wait.last_progress_counts == {"queued": 2}


def test_waits_are_read_by_parent_whoever_set_them(repository):
    at = datetime.now(timezone.utc)
    repository.set_check_back(team_id="t", parent_task_id="p", requester_agent_id="lead_before", at=at)
    repository.record_progress(team_id="t", parent_task_id="p2", requester_agent_id="lead_now", counts={}, at=at)
    repository.set_check_back(team_id="t", parent_task_id="not_mine", requester_agent_id="lead_now", at=at)
    waits = repository.waits_for_parents("t", {"p2", "p", "unknown"})
    assert [(w.parent_task_id, w.requester_agent_id, w.check_back_at_utc) for w in waits] == [
        ("p", "lead_before", at),
        ("p2", "lead_now", None),
    ]
    assert repository.waits_for_parents("other_team", ["p"]) == ()
    assert repository.waits_for_parents("t", ()) == ()


def test_a_check_back_time_is_taken_only_while_due_and_only_once(repository):
    at = datetime.now(timezone.utc)
    repository.set_check_back(team_id="t", parent_task_id="p", requester_agent_id="lead_before", at=at)
    repository.record_progress(team_id="t", parent_task_id="p", requester_agent_id="lead_before",
                               counts={"queued": 2}, at=at)

    assert not repository.take_due_check_back("t", "p", now=at - timedelta(seconds=1))
    assert repository.get_wait("t", "p").check_back_at_utc == at
    assert not repository.take_due_check_back("other_team", "p", now=at)
    assert repository.take_due_check_back("t", "p", now=at)
    wait = repository.get_wait("t", "p")
    assert (wait.check_back_at_utc, wait.requester_agent_id, wait.last_progress_counts) == (
        None, "lead_before", {"queued": 2}
    )
    assert not repository.take_due_check_back("t", "p", now=at + timedelta(hours=1))
    # No wait: nothing is taken and none is created.
    assert not repository.take_due_check_back("t", "unknown", now=at)
    assert repository.get_wait("t", "unknown") is None


def test_prune_keeps_open_work(repository):
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
