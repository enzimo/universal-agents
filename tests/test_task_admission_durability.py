import logging
import sqlite3

import pytest

from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from storage.config import resolve_storage_settings
from storage.runtime_custody import RuntimeRecoveryConflict


@pytest.fixture
def manager(tmp_path):
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "state.db")}}
    )
    manager = TaskManager(
        db_path=settings.sqlite_path,
        storage_settings=settings,
        logger=logging.getLogger(__name__),
    )
    manager.set_owner_identity(
        agent_id="worker",
        agent_name="Worker",
        team_id="team",
        named_agent_type="Worker",
    )
    return manager


def task():
    return Task(
        task_id="incoming",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Retain custody",
    )


def test_failed_initial_admission_can_be_redelivered(manager, monkeypatch):
    with monkeypatch.context() as failure:

        def unavailable(**kwargs):
            raise sqlite3.OperationalError("injected write failure")

        failure.setattr(manager.runtime_state_store, "save_task_runtime", unavailable)
        with pytest.raises(sqlite3.OperationalError):
            manager.add_task(task(), MessageHeader(), MessageMeta(), owner_person_id=None)
    assert manager.get_task_info("incoming") is None
    assert list(manager.task_state.task_queue) == []
    assert manager.runtime_state_store.load_task_runtime_by_owner("worker") == []
    assert manager.add_task(task(), MessageHeader(), MessageMeta(), owner_person_id=None) is True
    assert len(manager.runtime_state_store.load_task_runtime_by_owner("worker")) == 1


def test_failed_dequeue_keeps_task_runnable(manager, monkeypatch):
    manager.add_task(task(), MessageHeader(), MessageMeta(), owner_person_id=None)
    with monkeypatch.context() as failure:

        def unavailable(**kwargs):
            raise sqlite3.OperationalError("injected dequeue write failure")

        failure.setattr(manager.runtime_state_store, "save_task_runtime", unavailable)
        with pytest.raises(sqlite3.OperationalError):
            manager.get_next_task_to_process()
    assert list(manager.task_state.task_queue) == ["incoming"]
    assert manager.get_next_task_to_process() == "incoming"


def test_failed_subtask_admission_rolls_back_before_history(manager, monkeypatch):
    manager.add_task(task(), MessageHeader(), MessageMeta(), owner_person_id=None)
    child = Task(task_id="child", parent_task_id="incoming", task_type=MessageMetaType.REQUEST)
    with monkeypatch.context() as failure:
        def unavailable(**kwargs):
            raise sqlite3.OperationalError("subtask admission failed")

        failure.setattr(manager.runtime_state_store, "save_task_runtime", unavailable)
        with pytest.raises(sqlite3.OperationalError, match="subtask admission"):
            manager.add_subtask("incoming", child, MessageHeader(), MessageMeta())
    assert "child" not in manager.task_state.tasks
    assert "child" not in manager.task_state.subtask_to_parent_map
    assert not manager.task_state.task_to_subtask_list.get("incoming")
    assert manager.add_subtask("incoming", child.model_copy(deep=True), MessageHeader(), MessageMeta()) is not None


def test_populated_owner_cannot_be_rebound_without_recovery(manager):
    manager.add_task(task(), MessageHeader(), MessageMeta(), owner_person_id=None)
    with pytest.raises(ValueError, match="snapshot recovery"):
        manager.set_owner_identity("unrelated")
    assert manager.identity.owner_agent_id == "worker"


def test_failed_recovery_commit_restores_successor_memory_and_both_sources(
    manager, monkeypatch
):
    manager.add_task(task(), MessageHeader(), MessageMeta(), owner_person_id=None)
    successor = TaskManager(
        db_path=manager.storage_settings.sqlite_path,
        storage_settings=manager.storage_settings,
        logger=logging.getLogger(__name__),
    )
    successor.set_owner_identity(
        agent_id="successor",
        agent_name="Successor",
        team_id="team",
        named_agent_type="Worker",
    )
    existing = Task(
        task_id="existing",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Already owned",
    )
    successor.add_task(existing, MessageHeader(), MessageMeta(), owner_person_id=None)
    original_sources = manager.runtime_state_store.peek_task_runtime_by_role(
        "team", "Worker"
    )
    original_tasks = successor.task_state.tasks

    def conflict(**kwargs):
        raise RuntimeRecoveryConflict("another owner committed first")

    monkeypatch.setattr(
        successor.runtime_state_store, "commit_task_runtime_recovery", conflict
    )
    with pytest.raises(RuntimeRecoveryConflict):
        successor.restore_runtime_state_for_owner_ids(["worker"])
    assert successor.task_state.tasks is original_tasks
    assert list(successor.task_state.tasks) == ["existing"]
    assert list(successor.task_state.task_queue) == ["existing"]
    assert (
        manager.runtime_state_store.peek_task_runtime_by_role("team", "Worker")
        == original_sources
    )
