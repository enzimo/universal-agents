"""Task-sized writes and snapshot custody on the real persisted layout."""

from dataclasses import replace
import json
import os
from uuid import uuid4

import pytest

from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry
from custom_messages.enums import MessageMetaType
from custom_messages.records.task_results import TaskResultUpdate
from storage.config import resolve_storage_settings
from storage.runtime_custody import RuntimeRecoveryConflict


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])])
def manager(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("LIVE_TEST_POSTGRES_DSN required")
        settings = resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": dsn}}
        )
    else:
        settings = resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "state.db")}}
        )
    manager = TaskManager(
        storage_settings=settings, db_path=settings.sqlite_path or "unused.db"
    )
    owner = f"write_unit_{uuid4().hex}"
    manager.set_owner_identity(owner, "Writer", owner, "Worker")
    for task_id, size in (("small", 20), ("large", 500_000)):
        task = Task(
            task_id=task_id,
            task_type=MessageMetaType.REQUEST,
            execution_history=[TaskHistoryEntry(summary="x" * size)],
        )
        manager.add_task(task, MessageHeader(), MessageMeta(), owner_person_id=None)
    yield manager
    manager.runtime_state_store.clear_task_runtime(owner)
    manager.runtime_state_store.clear_task_runtime(owner + "_replacement")


def test_history_write_does_not_write_other_task_payloads(manager, monkeypatch):
    repo = manager.runtime_state_store._repository
    write = repo._write_task_records
    written = []

    def observe(conn, owner, records, *, replace_all):
        written.append((records, replace_all))
        return write(conn, owner, records, replace_all=replace_all)

    monkeypatch.setattr(repo, "_write_task_records", observe)
    manager.append_history_entry("small", "one new event")
    assert len(written) == 1
    records, replace_all = written[0]
    assert replace_all is False
    assert {key for section, key in records if section == "tasks"} == {"small"}
    assert sum(len(payload) for payload in records.values()) < 10_000
    restored = json.loads(
        repo.load_task_runtime_by_owner(manager.identity.owner_agent_id)[
            0
        ].runtime_state_json
    )
    tasks = {task["task_id"]: task["task"] for task in restored["tasks"]}
    assert tasks["small"]["execution_history"][-1]["summary"] == "one new event"
    assert len(tasks["large"]["execution_history"][0]["summary"]) == 500_000


def test_failed_partial_write_rolls_back_database_and_history(manager, monkeypatch):
    repo = manager.runtime_state_store._repository
    owner = manager.identity.owner_agent_id
    before = repo.load_task_runtime_by_owner(owner)
    history = list(manager.get_task_info("small").task.execution_history)
    write = repo._write_task_records

    def fail_after_write(*args, **kwargs):
        write(*args, **kwargs)
        raise RuntimeError("failure before commit")

    monkeypatch.setattr(repo, "_write_task_records", fail_after_write)
    with pytest.raises(RuntimeError, match="failure before commit"):
        manager.append_history_entry("small", "must roll back")
    assert repo.load_task_runtime_by_owner(owner) == before
    assert manager.get_task_info("small").task.execution_history == history


def test_failed_result_write_restores_owned_record_and_database(manager, monkeypatch):
    repo = manager.runtime_state_store._repository
    before = repo.load_task_runtime_by_owner(manager.identity.owner_agent_id)
    task = manager.get_task_info("small").task.model_copy(deep=True)
    write = repo._write_task_records

    def fail_after_write(*args, **kwargs):
        write(*args, **kwargs)
        raise RuntimeError("result commit failed")

    monkeypatch.setattr(repo, "_write_task_records", fail_after_write)
    with pytest.raises(RuntimeError, match="result commit failed"):
        manager.update_task_result(
            "small", TaskResultUpdate(answer="must roll back", errors=["failed"])
        )
    assert manager.get_task_info("small").task == task
    assert repo.load_task_runtime_by_owner(manager.identity.owner_agent_id) == before


def test_durable_cancellation_wins_over_late_result(manager):
    from datetime import datetime, timezone

    before = manager.get_task_info("small").task.model_copy(deep=True)
    manager.record_execution_graph_cancelled(
        root_task_id="small",
        task_ids=["small"],
        canonical_user_id=None,
        command_id="stop-result-test",
        reason="stopped",
        cancelled_at=datetime.now(timezone.utc).isoformat(),
    )
    assert (
        manager.update_task_result("small", TaskResultUpdate(answer="too late")) is None
    )
    assert manager.get_task_info("small").task == before


def test_partial_write_invalidates_a_previously_read_recovery_version(manager):
    repo = manager.runtime_state_store._repository
    owner = manager.identity.owner_agent_id
    before = repo.load_task_runtime_by_owner(owner)
    manager.append_history_entry("small", "new durable information")
    with pytest.raises(RuntimeRecoveryConflict):
        repo.commit_task_runtime_recovery(
            sources=before,
            replacement=replace(before[0], owner_agent_id=owner + "_replacement"),
        )
    assert repo.load_task_runtime_by_owner(owner)
    assert repo.load_task_runtime_by_owner(owner + "_replacement") == []


def test_former_owner_cannot_recreate_custody_with_a_history_write(manager):
    repo = manager.runtime_state_store._repository
    owner = manager.identity.owner_agent_id
    before = repo.load_task_runtime_by_owner(owner)
    repo.commit_task_runtime_recovery(
        sources=before,
        replacement=replace(before[0], owner_agent_id=owner + "_replacement"),
    )
    with pytest.raises(RuntimeRecoveryConflict):
        manager.append_history_entry("small", "stale owner")
    assert repo.load_task_runtime_by_owner(owner) == []
    assert (
        repo.load_task_runtime_by_owner(owner + "_replacement")[0].runtime_state_json
        == before[0].runtime_state_json
    )
