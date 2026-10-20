"""Request admission and closure commit with real task snapshots, on both databases."""

from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, get_ident
import time
from uuid import uuid4

import pytest

import storage.change_ledger as ledger
from access_control.identity import ENV_VERIFY_KEYS, ISSUER_SCHEDULER, ContextSigner, IdentityRejected
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3, TaskSnapshotRecord
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from storage.runtime_state_repository import create_runtime_state_repository
from storage.task_custody_repository import TaskCustodyConflict
from tests.broker_access_support import _scheduler_key, context_signer, forget_task_context_keys, use_scheduler_key
from tests.knowledge_support import BACKENDS, NOW, TEAM, knowledge_storage
from tests.test_change_ledger import _connect


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    use_scheduler_key(monkeypatch)
    yield
    forget_task_context_keys()


@pytest.fixture(params=BACKENDS)
def storage(request, tmp_path):
    with knowledge_storage(request.param, tmp_path) as settings:
        yield settings


def record(signed, *, task_id="task_root", parent=None, state=ProgressState.ACCEPTED, owner="alice"):
    return TaskSnapshotRecord(
        task_id=task_id, state=state,
        task=Task(task_id=task_id, parent_task_id=parent, task_type=MessageMetaType.REQUEST, problem_statement="Save notes."),
        header=MessageHeader(message_id=f"message_{task_id}", timestamp=NOW.isoformat()),
        meta=MessageMeta(gateway_context=GatewayContext(source="test", task_context=signed)),
        owner_person_id=owner,
    )


def save(repo, task, *, agent_id="agent_lead", existing=False):
    snapshot = RuntimeSnapshotV3(schema_version=3, tasks=[task], task_queue=[task.task_id])
    repo.save_task_runtime(
        owner_agent_id=agent_id, owner_agent_name="Lead", team_id=TEAM, owner_named_agent_type="TeamArchitect",
        runtime_state_json=snapshot.model_dump_json(), expect_existing=existing,
    )


def rows(storage):
    with _connect(storage) as conn:
        return {str(row[0]): str(row[1]) for row in conn.execute("SELECT request_id, state FROM change_requests").fetchall()}


def signed_request():
    return context_signer().issue(team_id=TEAM, person_id="alice", person_roles=("member",))


@pytest.mark.parametrize("terminal", [ProgressState.COMPLETED, ProgressState.FAILED, ProgressState.ABANDONED])
def test_root_admission_and_terminal_state_save_the_request_with_the_task(storage, terminal):
    repo = create_runtime_state_repository(storage)
    signed = signed_request()
    root = record(signed)
    save(repo, root)
    assert rows(storage) == {signed.context.context_id: "open"}

    # A worker carries the same context but never owns the request's lifecycle.
    child = record(signed, task_id="child", parent=root.task_id, state=ProgressState.COMPLETED)
    save(repo, child, agent_id="worker")
    assert rows(storage) == {signed.context.context_id: "open"}
    save(repo, root.model_copy(update={"state": terminal}), existing=True)
    assert rows(storage) == {signed.context.context_id: "ended"}


def test_cancelled_task_and_request_are_stopped_together(storage):
    repo = create_runtime_state_repository(storage)
    signed = signed_request()
    root = record(signed)
    save(repo, root)
    save(repo, root.model_copy(update={"state": ProgressState.CANCELLED}), existing=True)
    assert rows(storage) == {signed.context.context_id: "stopped"}


def test_scheduled_and_unsigned_work_have_no_request_row(storage):
    repo = create_runtime_state_repository(storage)
    scheduled = ContextSigner.from_value(ISSUER_SCHEDULER, _scheduler_key()[0]).issue(
        team_id=TEAM, person_id="alice", person_roles=("member",), schedule_id="schedule_1",
    )
    save(repo, record(scheduled), agent_id="scheduled")
    save(repo, record(None, task_id="unsigned_root", owner=None), agent_id="unsigned")
    assert rows(storage) == {}


@pytest.mark.parametrize("invalid", ["signature", "team", "owner"])
def test_unverified_or_mismatched_identity_cannot_close_a_request(storage, invalid):
    repo = create_runtime_state_repository(storage)
    signed = signed_request()
    save(repo, record(signed))
    owner = "alice"
    if invalid == "signature":
        signed = signed.model_copy(update={"signature": "AA"})
    elif invalid == "team":
        signed = context_signer().sign(signed.context.model_copy(update={"team_id": "other_team"}))
    else:
        owner = "bob"
    # Even a supplied owner cannot substitute for the signature and its team.
    with pytest.raises(IdentityRejected):
        save(repo, record(signed, task_id="other_root", state=ProgressState.COMPLETED, owner=owner), agent_id="other")
    assert set(rows(storage).values()) == {"open"}
    assert repo.load_task_runtime_by_owner("other") == []


def test_expired_original_context_still_closes_its_request(storage):
    repo = create_runtime_state_repository(storage)
    signed = context_signer().issue(
        team_id=TEAM, person_id="alice", person_roles=("member",), now=NOW - timedelta(days=10),
    )
    root = record(signed)
    save(repo, root)
    save(repo, root.model_copy(update={"state": ProgressState.COMPLETED}), existing=True)
    assert rows(storage) == {signed.context.context_id: "ended"}


@pytest.mark.parametrize("operation", ["open_request", "close_request"])
def test_lifecycle_failure_rolls_back_task_save_and_recovers_on_retry(storage, monkeypatch, operation):
    repo = create_runtime_state_repository(storage)
    signed = signed_request()
    root = record(signed)
    closing = operation == "close_request"
    if closing:
        save(repo, root)
        root = root.model_copy(update={"state": ProgressState.CANCELLED})

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("one-shot database outage")

    with monkeypatch.context() as failure:
        failure.setattr(ledger, operation, unavailable)
        with pytest.raises(RuntimeError, match="one-shot database outage"):
            save(repo, root, existing=closing)

    # Reopening models restart: both records retain their last committed state.
    recovered = create_runtime_state_repository(storage)
    assert rows(storage) == ({signed.context.context_id: "open"} if closing else {})
    snapshots = recovered.load_task_runtime_by_owner("agent_lead")
    if closing:
        assert RuntimeSnapshotV3.model_validate_json(snapshots[0].runtime_state_json).tasks[0].state is ProgressState.ACCEPTED
    else:
        assert snapshots == []
    save(recovered, root, existing=closing)
    assert rows(storage) == {signed.context.context_id: "stopped" if closing else "open"}


def test_failure_after_both_writes_rolls_back_admission(storage, monkeypatch):
    repo = create_runtime_state_repository(storage)
    root = record(signed_request())

    def crash(*_args, **_kwargs):
        raise RuntimeError("crash before commit")

    with monkeypatch.context() as failure:
        failure.setattr(repo, "_sync_task_custody", crash)
        with pytest.raises(RuntimeError, match="crash before commit"):
            save(repo, root)
    assert rows(storage) == {}
    assert repo.load_task_runtime_by_owner("agent_lead") == []


def test_recovery_keeps_the_original_request_start(storage):
    repo = create_runtime_state_repository(storage)
    signed = signed_request()
    save(repo, record(signed))
    with _connect(storage) as conn:
        before = conn.execute("SELECT start_position FROM change_requests").fetchone()[0]
    sources = repo.load_task_runtime_by_owner("agent_lead")
    repo.commit_task_runtime_recovery(sources=sources, replacement=replace(sources[0], owner_agent_id="replacement"))
    with _connect(storage) as conn:
        assert conn.execute("SELECT start_position FROM change_requests").fetchone()[0] == before
    assert rows(storage) == {signed.context.context_id: "open"}


def cancel(repo, task_ids, *, root_id="task_root"):
    repo.record_task_cancellations(
        team_id=TEAM, root_task_id=root_id, task_ids=task_ids, canonical_user_id="alice",
        command_id="stop_1", reason="Stop this request.", cancelled_at=NOW.isoformat(),
    )


def test_stop_marker_closes_request_before_crash_and_recovery_discards_the_task(storage):
    repo = create_runtime_state_repository(storage)
    signed = signed_request()
    save(repo, record(signed))
    cancel(repo, ["task_root"])
    assert rows(storage) == {signed.context.context_id: "stopped"}
    # Crash before the terminal snapshot: recovery intentionally discards the
    # cancelled task, so there will never be a later terminal-state callback.
    sources = repo.load_task_runtime_by_owner("agent_lead")
    assert RuntimeSnapshotV3.model_validate_json(sources[0].runtime_state_json).tasks[0].state is ProgressState.ACCEPTED
    empty = RuntimeSnapshotV3(schema_version=3).model_dump_json()
    repo.commit_task_runtime_recovery(
        sources=sources, replacement=replace(sources[0], owner_agent_id="replacement", runtime_state_json=empty),
    )
    assert rows(storage) == {signed.context.context_id: "stopped"}
    restored = repo.load_task_runtime_by_owner("replacement")
    assert RuntimeSnapshotV3.model_validate_json(restored[0].runtime_state_json).tasks == []


def test_cancelling_a_delegated_task_does_not_close_the_request(storage):
    repo = create_runtime_state_repository(storage)
    signed = signed_request()
    save(repo, record(signed))
    save(repo, record(signed, task_id="child", parent="task_root"), agent_id="worker")
    cancel(repo, ["child"], root_id="child")
    assert rows(storage) == {signed.context.context_id: "open"}


def test_failed_request_closure_also_rolls_back_its_stop_marker(storage, monkeypatch):
    repo = create_runtime_state_repository(storage)
    signed = signed_request()
    save(repo, record(signed))

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as failure:
        failure.setattr(ledger, "close_request", unavailable)
        with pytest.raises(RuntimeError, match="database unavailable"):
            cancel(repo, ["task_root"])
    assert rows(storage) == {signed.context.context_id: "open"}
    assert repo.get_task_cancellation(team_id=TEAM, task_id="task_root") is None
    cancel(repo, ["task_root"])
    assert rows(storage) == {signed.context.context_id: "stopped"}


def test_missing_historical_key_aborts_terminal_save_instead_of_losing_closure(storage, monkeypatch):
    repo = create_runtime_state_repository(storage)
    signed = signed_request()
    root = record(signed)
    save(repo, root)
    terminal = root.model_copy(update={"state": ProgressState.CANCELLED})
    with monkeypatch.context() as failure:
        failure.setenv(ENV_VERIFY_KEYS, "")
        with pytest.raises(IdentityRejected):
            save(repo, terminal, existing=True)
    saved = repo.load_task_runtime_by_owner("agent_lead")
    assert RuntimeSnapshotV3.model_validate_json(saved[0].runtime_state_json).tasks[0].state is ProgressState.ACCEPTED
    assert rows(storage) == {signed.context.context_id: "open"}
    save(repo, terminal, existing=True)
    assert rows(storage) == {signed.context.context_id: "stopped"}


def test_partial_task_record_save_closes_the_request_in_the_same_transaction(storage):
    repo = create_runtime_state_repository(storage)
    signed = signed_request()
    root = record(signed)
    save(repo, root)
    repo.save_task_runtime_task(
        owner_agent_id="agent_lead", task=root.model_copy(update={"state": ProgressState.COMPLETED}),
        task_plan_links={}, task_objective_links={}, plan_objective_links={},
    )
    assert rows(storage) == {signed.context.context_id: "ended"}


def test_task_manager_undoes_in_memory_admission_when_ledger_save_fails(tmp_path, monkeypatch):
    from custom_agents.task_manager import TaskManager
    from storage.config import resolve_storage_settings

    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    manager = TaskManager(storage_settings=settings)
    manager.set_owner_identity(agent_id="lead", agent_name="Lead", team_id=TEAM, named_agent_type="TeamArchitect")
    root = record(signed_request())

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("request admission unavailable")

    with monkeypatch.context() as failure:
        failure.setattr(ledger, "open_request", unavailable)
        with pytest.raises(RuntimeError, match="request admission unavailable"):
            manager.add_task(root.task, root.header, root.meta, owner_person_id="alice")
    assert manager.get_task_info(root.task_id) is None
    assert list(manager.task_state.task_queue) == []
    assert rows(settings) == {}
    assert manager.add_task(root.task, root.header, root.meta, owner_person_id="alice")
    assert rows(settings) == {root.meta.gateway_context.task_context.context.context_id: "open"}


@pytest.mark.parametrize("changed", ["missing", "owner", "context", "parent"])
def test_an_admitted_request_cannot_lose_or_replace_its_identity(storage, changed):
    repo = create_runtime_state_repository(storage)
    root = record(signed_request())
    save(repo, root)
    terminal = root.model_copy(deep=True, update={"state": ProgressState.COMPLETED})
    if changed == "missing":
        terminal.meta.gateway_context.task_context = None
    elif changed == "owner":
        terminal.owner_person_id = None
    elif changed == "context":
        terminal.meta.gateway_context.task_context = signed_request()
    else:
        terminal.task.parent_task_id = "other_root"
    with pytest.raises(IdentityRejected, match="original signed identity"):
        save(repo, terminal, existing=True)
    saved = repo.load_task_runtime_by_owner("agent_lead")
    assert RuntimeSnapshotV3.model_validate_json(saved[0].runtime_state_json).tasks[0].state is ProgressState.ACCEPTED
    assert set(rows(storage).values()) == {"open"}


@pytest.mark.live
@pytest.mark.postgres
@pytest.mark.integration
def test_competing_admissions_lock_requests_in_the_same_order_before_custody_decides(tmp_path, monkeypatch):
    """Opposite task orders must produce one custody refusal, never a deadlock."""

    from psycopg.conninfo import make_conninfo
    from storage.config import resolve_storage_settings
    from storage.postgres_connection import connect_postgres
    import storage.runtime_state_repository as runtime

    with knowledge_storage("postgres", tmp_path) as settings:
        first_repo = create_runtime_state_repository(settings)
        second_name = "request_contender_" + uuid4().hex
        other_settings = resolve_storage_settings({
            "backend": "postgres", "postgres": {"dsn": make_conninfo(settings.postgres_dsn, application_name=second_name)},
        })
        second_repo = create_runtime_state_repository(other_settings)
        roots = [record(signed_request(), task_id=task_id) for task_id in ("root_a", "root_b")]
        locked, release = Event(), Event()
        first_thread = None
        original = runtime.record_task_request_state

        def hold_first(conn, dialect, **kwargs):
            original(conn, dialect, **kwargs)
            if get_ident() == first_thread and kwargs["task"].task_id == "root_a":
                locked.set()
                assert release.wait(10)

        monkeypatch.setattr(runtime, "record_task_request_state", hold_first)

        def admit(repo, owner, tasks):
            nonlocal first_thread
            if owner == "first":
                first_thread = get_ident()
            snapshot = RuntimeSnapshotV3(schema_version=3, tasks=tasks, task_queue=[task.task_id for task in tasks])
            repo.save_task_runtime(
                owner_agent_id=owner, owner_agent_name=owner, team_id=TEAM, owner_named_agent_type="TeamArchitect",
                runtime_state_json=snapshot.model_dump_json(), expect_existing=False,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(admit, first_repo, "first", roots)
            try:
                assert locked.wait(10)
                second = pool.submit(admit, second_repo, "second", list(reversed(roots)))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not second.done():
                    with connect_postgres(settings.postgres_dsn) as conn:
                        waiting = conn.execute(
                            "SELECT wait_event_type FROM pg_stat_activity WHERE application_name=%s", (second_name,),
                        ).fetchone()
                    if waiting is not None and waiting[0] == "Lock":
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("The competing admission did not reach its row-lock wait.")
            finally:
                release.set()
            first.result(timeout=10)
            with pytest.raises(TaskCustodyConflict):
                second.result(timeout=10)
        assert len(first_repo.load_task_runtime_by_owner("first")) == 1
        assert second_repo.load_task_runtime_by_owner("second") == []
