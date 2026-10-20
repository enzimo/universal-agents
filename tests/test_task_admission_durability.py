import logging
import sqlite3
from datetime import datetime, timezone

import pytest

import storage.change_ledger as ledger
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from storage.config import resolve_storage_settings
from storage.runtime_custody import RuntimeRecoveryConflict
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts


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


@pytest.mark.parametrize("terminal", [ProgressState.COMPLETED, ProgressState.CANCELLED])
@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
def test_failed_terminal_request_save_keeps_memory_and_durable_state_retryable(
    manager, monkeypatch, terminal, running, caplog
):
    use_signed_contexts(monkeypatch)
    signed = context_signer().issue(team_id="team", person_id="alice", person_roles=("member",))
    manager.add_task(
        task(), MessageHeader(), MessageMeta(gateway_context=GatewayContext(task_context=signed)),
        owner_person_id="alice",
    )
    if running:
        manager.update_main_task_state("incoming", ProgressState.IN_PROGRESS, datetime.now(timezone.utc).isoformat())
    info = manager.get_task_info("incoming")
    previous_state = info.state
    previous_states = list(info.task.task_state)
    previous_history = list(info.task.execution_history)
    previous_queue = list(manager.task_state.task_queue)
    previous_current = manager.task_state.current_in_progress_task_id

    def finish():
        now = datetime.now(timezone.utc).isoformat()
        if terminal is ProgressState.COMPLETED:
            manager.complete_task("incoming", now)
        else:
            assert manager.try_transition_task_state(
                "incoming", allowed_from={previous_state}, to_state=terminal,
                timestamp=now, reason="The person stopped the request.",
            )

    def request_state():
        with sqlite3.connect(manager.storage_settings.sqlite_path) as conn:
            return conn.execute(
                "SELECT state FROM change_requests WHERE request_id=?", (signed.context.context_id,),
            ).fetchone()[0]

    with monkeypatch.context() as failure:
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("request closure unavailable")

        failure.setattr(ledger, "close_request", unavailable)
        with caplog.at_level(logging.WARNING), pytest.raises(sqlite3.OperationalError, match="request closure"):
            finish()

    assert manager.get_task_info("incoming") is info
    assert info.state is previous_state
    assert info.task.task_state == previous_states
    assert info.task.execution_history == previous_history
    assert list(manager.task_state.task_queue) == previous_queue
    assert manager.task_state.current_in_progress_task_id == previous_current
    assert request_state() == "open"
    saved = manager.runtime_state_store.load_task_runtime_by_owner("worker")
    assert RuntimeSnapshotV3.model_validate_json(saved[0].runtime_state_json).tasks[0].state is previous_state
    assert "event=task_state_change_rolled_back" in caplog.text

    finish()

    assert request_state() == ("ended" if terminal is ProgressState.COMPLETED else "stopped")
    if terminal is ProgressState.COMPLETED:
        assert manager.get_task_info("incoming") is None
        assert manager.get_task_from_db("incoming")["status"] == ProgressState.COMPLETED.value
    else:
        assert info.state is ProgressState.CANCELLED
        saved = manager.runtime_state_store.load_task_runtime_by_owner("worker")
        assert RuntimeSnapshotV3.model_validate_json(saved[0].runtime_state_json).tasks[0].state is terminal


def test_later_snapshot_failure_does_not_restore_a_committed_terminal_state(manager, monkeypatch):
    use_signed_contexts(monkeypatch)
    signed = context_signer().issue(team_id="team", person_id="alice", person_roles=("member",))
    manager.add_task(
        task(), MessageHeader(), MessageMeta(gateway_context=GatewayContext(task_context=signed)),
        owner_person_id="alice",
    )

    def unavailable(**kwargs):
        raise sqlite3.OperationalError("later snapshot write unavailable")

    # The first, partial history save already commits the terminal task and
    # its request. A failure in the later whole-snapshot write must not undo
    # that successful transaction in memory.
    with monkeypatch.context() as failure:
        failure.setattr(manager.runtime_state_store, "save_task_runtime", unavailable)
        with pytest.raises(sqlite3.OperationalError, match="later snapshot"):
            manager.update_main_task_state("incoming", ProgressState.COMPLETED, datetime.now(timezone.utc).isoformat())

    assert manager.get_task_info("incoming").state is ProgressState.COMPLETED
    saved = manager.runtime_state_store.load_task_runtime_by_owner("worker")
    assert RuntimeSnapshotV3.model_validate_json(saved[0].runtime_state_json).tasks[0].state is ProgressState.COMPLETED
    with sqlite3.connect(manager.storage_settings.sqlite_path) as conn:
        assert conn.execute(
            "SELECT state FROM change_requests WHERE request_id=?", (signed.context.context_id,),
        ).fetchone()[0] == "ended"


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
