import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from custom_agents.memory_review_store import MemoryReviewStore
from custom_tools.providers.memory_review_inspection import (
    get_memory_review_job,
    list_pending_memory_review_jobs,
)
from custom_tools.runtime_context import set_agent_ref
from storage.config import resolve_storage_settings
from tests.change_approval_support import SignedTaskManager, use_signed_contexts

PERSON = "alice"
OTHER_PERSON = "bob"


@pytest.fixture
def reviews(tmp_path, monkeypatch):
    use_signed_contexts(monkeypatch)
    path = tmp_path / "reviews.db"
    store = MemoryReviewStore(str(path), logger=logging.getLogger("test"))
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    set_agent_ref(
        SimpleNamespace(
            id="worker",
            team_id="team",
            named_agent_type="MemReviewAgent",
            _memory_review_store=store,
            # The task works for alice (a member), from its signed context.
            task_manager=SignedTaskManager(team_id="team", person_id=PERSON, storage_settings=storage, roles=("member",)),
            authorization_config={"enabled": False},
        )
    )
    yield store, path
    set_agent_ref(None)


def enqueue(store, index, team="team", role="MemReviewAgent", person=PERSON):
    return store.enqueue_review_job(
        owner_team_id=team,
        owner_named_agent_type=role,
        task_id=f"task_{index}",
        conversation_id="conversation",
        turn_index=index,
        source_payload={"canonical_user_id": person, "text": "private source " * 10000},
    )


def test_filters_before_limit_and_pages_all_pending_jobs(reviews):
    store, path = reviews
    for index in range(95):
        enqueue(store, index)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE memory_review_jobs SET status = 'expired'")
    enqueue(store, 998, team="other")
    enqueue(store, 999, role="OtherAgent")
    expected = set()
    for index in range(100, 302):
        expected.add(enqueue(store, index).review_job_id)
        if index % 2:
            # Another person's jobs sit between alice's and are never listed.
            enqueue(store, 1000 + index, person=OTHER_PERSON)
    found = []
    offset = 0
    while offset is not None:
        raw = list_pending_memory_review_jobs(limit=20, offset=offset)
        page = json.loads(raw)
        assert page["returned"] == min(20, 202 - len(found))
        assert "private source" not in raw
        assert len(raw) < 12000
        found.extend(item["review_job_id"] for item in page["jobs"])
        offset = page["next_offset"]
    assert len(found) == len(set(found)) == 202
    assert set(found) == expected


def test_exact_inspection_is_bounded_private_and_does_not_claim(reviews):
    store, _ = reviews
    job = enqueue(store, 1)
    other = enqueue(store, 2, team="other")
    wrong_role = enqueue(store, 3, role="OtherAgent")
    someone_elses = enqueue(store, 4, person=OTHER_PERSON)
    denied = json.loads(get_memory_review_job("missing"))
    assert json.loads(get_memory_review_job(other.review_job_id)) == denied
    assert json.loads(get_memory_review_job(wrong_role.review_job_id)) == denied
    assert json.loads(get_memory_review_job(someone_elses.review_job_id)) == denied
    first = json.loads(get_memory_review_job(job.review_job_id, max_chars=999999))
    assert len(first["detail"]["text"]) == 4000
    second = json.loads(
        get_memory_review_job(job.review_job_id, offset=first["detail"]["next_offset"])
    )
    assert first["detail"]["sha256"] == second["detail"]["sha256"]
    assert store.get_review_job(job.review_job_id).status == "queued"
    assert "error" in json.loads(get_memory_review_job(job.review_job_id, offset=-1))
    assert "error" in json.loads(list_pending_memory_review_jobs(limit=0))


def test_work_for_no_person_sees_no_review_jobs(reviews, tmp_path):
    store, _ = reviews
    job = enqueue(store, 1)
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    set_agent_ref(
        SimpleNamespace(
            id="worker",
            team_id="team",
            named_agent_type="MemReviewAgent",
            _memory_review_store=store,
            task_manager=SignedTaskManager(team_id="team", person_id=None, storage_settings=storage, roles=()),
            authorization_config={"enabled": False},
        )
    )

    assert json.loads(list_pending_memory_review_jobs())["jobs"] == []
    assert "error" in json.loads(get_memory_review_job(job.review_job_id))
