"""The gateway's store of agent questions, on real SQLite and PostgreSQL.

Every question is kept under its own id until one outcome ends it, so a second
question in a chat never erases the first. PostgreSQL runs need
``LIVE_TEST_POSTGRES_DSN``; each test gets its own schema.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from threading import Barrier

import pytest

from assistant_gateway.domain.request_state import (
    RUN_STATE_RUNNING,
    HeldGuidanceRecord,
    PendingQueryStatus,
    ThreadRequestRunning,
)
from storage.config import StorageSettings, resolve_storage_settings
from storage.gateway_state_repository import create_gateway_state_repository

SESSION = "session_alpha"


@contextmanager
def _postgres_settings() -> Iterator[StorageSettings]:
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL question tests")
    schema = "gateway_questions_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield resolve_storage_settings(
            {
                "backend": "postgres",
                "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")},
            }
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(
    params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])]
)
def settings(request, tmp_path: Path) -> Iterator[StorageSettings]:
    if request.param == "postgres":
        with _postgres_settings() as postgres:
            yield postgres
        return
    yield resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "gateway_state.db")}}
    )


def _open(settings: StorageSettings):
    repo = create_gateway_state_repository(settings, logger=logging.getLogger("test"))
    if repo.get_session(SESSION) is None:
        repo.create_session(
            session_key=SESSION,
            conversation_id="conv_alpha",
            manager_agent_id="manager_1",
            owner_canonical_user_id="alice",
        )
    return repo


def _ask(repo, question_id: str, *, correlation_id: str, task_id: str):
    return repo.add_pending_query(
        session_key=SESSION,
        correlation_id=correlation_id,
        task_query_id=question_id,
        parent_task_id=task_id,
        conversation_id="conv_alpha",
        query_text=f"Question {question_id}?",
        query_payload_json='{"message_class":"TaskQuery"}',
    )


def _open_ids(repo) -> list[str]:
    return [question.task_query_id for question in repo.list_open_pending_queries(SESSION)]


def test_two_questions_in_one_chat_are_both_kept_and_end_in_order(settings) -> None:
    repo = _open(settings)
    first = _ask(repo, "q_report", correlation_id="corr_report", task_id="task_report")
    second = _ask(repo, "q_trip", correlation_id="corr_trip", task_id="task_trip")

    assert first.created and second.created
    assert _open_ids(repo) == ["q_report", "q_trip"]
    stored = repo.get_pending_query("q_report")
    assert stored.owner_canonical_user_id == "alice"
    assert stored.query_payload_json == '{"message_class":"TaskQuery"}'

    assert repo.close_pending_query("q_report", status=PendingQueryStatus.ANSWERED) is True
    assert _open_ids(repo) == ["q_trip"]
    assert repo.close_pending_query("q_trip", status=PendingQueryStatus.ANSWERED) is True
    assert _open_ids(repo) == []
    assert repo.get_pending_query("q_report").status == PendingQueryStatus.ANSWERED
    assert repo.get_pending_query("q_report").closed_at is not None


def test_one_outcome_takes_each_question(settings) -> None:
    repo = _open(settings)
    _ask(repo, "q_report", correlation_id="corr_report", task_id="task_report")

    assert repo.close_pending_query("q_report", status=PendingQueryStatus.ANSWERED) is True
    # A duplicate answer, and an expiry after the answer, both find it taken.
    assert repo.close_pending_query("q_report", status=PendingQueryStatus.ANSWERED) is False
    assert repo.close_pending_query("q_report", status=PendingQueryStatus.EXPIRED) is False
    # An answer that could not be sent opens it again, once.
    assert repo.reopen_answered_pending_query("q_report") is True
    assert repo.reopen_answered_pending_query("q_report") is False
    assert _open_ids(repo) == ["q_report"]


def test_a_question_arriving_again_keeps_its_stored_state(settings) -> None:
    repo = _open(settings)
    _ask(repo, "q_report", correlation_id="corr_report", task_id="task_report")
    repo.close_pending_query("q_report", status=PendingQueryStatus.ANSWERED)

    again = _ask(repo, "q_report", correlation_id="corr_report", task_id="task_report")

    assert again.created is False
    assert again.stored.status == PendingQueryStatus.ANSWERED
    assert _open_ids(repo) == []


def test_a_newer_question_of_the_same_task_takes_the_earlier_ones_place(settings) -> None:
    repo = _open(settings)
    _ask(repo, "q_report", correlation_id="corr_report", task_id="task_report")
    _ask(repo, "q_trip", correlation_id="corr_trip", task_id="task_trip")

    newer = _ask(repo, "q_report_2", correlation_id="corr_report", task_id="task_report")

    assert [question.task_query_id for question in newer.replaced] == ["q_report"]
    assert _open_ids(repo) == ["q_report_2", "q_trip"]
    assert repo.get_pending_query("q_report").status == PendingQueryStatus.REPLACED


def test_a_finished_task_ends_only_its_own_questions(settings) -> None:
    repo = _open(settings)
    # Every run of a recurring schedule shares the schedule's request id.
    _ask(repo, "q_run_1", correlation_id="corr_schedule", task_id="task_run_1")
    _ask(repo, "q_run_2", correlation_id="corr_schedule", task_id="task_run_2")

    closed = repo.close_open_pending_queries_for_task(
        correlation_id="corr_schedule",
        parent_task_id="task_run_1",
        status=PendingQueryStatus.REQUEST_ENDED,
    )

    assert [question.task_query_id for question in closed] == ["q_run_1"]
    assert _open_ids(repo) == ["q_run_2"]


def test_showing_is_recorded_once_and_only_while_open(settings) -> None:
    repo = _open(settings)
    _ask(repo, "q_report", correlation_id="corr_report", task_id="task_report")
    _ask(repo, "q_trip", correlation_id="corr_trip", task_id="task_trip")

    assert repo.mark_pending_query_shown("q_report") is True
    assert repo.mark_pending_query_shown("q_report") is False
    repo.clear_pending_query_shown("q_report")
    assert repo.get_pending_query("q_report").was_shown is False
    assert repo.mark_pending_query_shown("q_report") is True
    repo.close_pending_query("q_report", status=PendingQueryStatus.ANSWERED)
    repo.clear_pending_query_shown("q_report")
    assert repo.get_pending_query("q_report").was_shown is True
    repo.close_pending_query("q_trip", status=PendingQueryStatus.EXPIRED)
    assert repo.mark_pending_query_shown("q_trip") is False


def test_waiting_questions_survive_a_restart(settings) -> None:
    repo = _open(settings)
    _ask(repo, "q_report", correlation_id="corr_report", task_id="task_report")
    _ask(repo, "q_trip", correlation_id="corr_trip", task_id="task_trip")
    repo.mark_pending_query_shown("q_report")

    restarted = _open(settings)

    questions = restarted.list_open_pending_queries(SESSION)
    assert [question.task_query_id for question in questions] == ["q_report", "q_trip"]
    assert [question.was_shown for question in questions] == [True, False]


def test_expiry_ends_open_questions_asked_before_the_cutoff(settings) -> None:
    repo = _open(settings)
    _ask(repo, "q_report", correlation_id="corr_report", task_id="task_report")
    _ask(repo, "q_trip", correlation_id="corr_trip", task_id="task_trip")
    repo.close_pending_query("q_trip", status=PendingQueryStatus.ANSWERED)
    now = datetime.now(timezone.utc)

    assert repo.expire_pending_queries(cutoff_iso=(now - timedelta(minutes=5)).isoformat()) == 0
    assert repo.expire_pending_queries(cutoff_iso=(now + timedelta(minutes=5)).isoformat()) == 1
    assert repo.get_pending_query("q_report").status == PendingQueryStatus.EXPIRED
    assert repo.get_pending_query("q_trip").status == PendingQueryStatus.ANSWERED


def test_the_one_question_per_chat_table_is_removed(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway_state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE gateway_pending_queries (session_key TEXT PRIMARY KEY, status TEXT)"
        )
    _open(resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db_path)}}))

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "gateway_pending_queries" not in tables
    assert "gateway_questions" in tables


def _running_request(repo, correlation_id: str, conversation_id: str = "conv_alpha"):
    repo.record_request(
        correlation_id=correlation_id,
        session_key=SESSION,
        request_kind="interactive",
        request_text="Prepare the report",
        conversation_id=conversation_id,
        followup_to_task_id="task_previous",
        active_task_id=f"task_{correlation_id}",
        run_state=RUN_STATE_RUNNING,
    )


def test_only_one_request_runs_in_a_thread_until_its_final_reply(settings) -> None:
    repo = _open(settings)
    _running_request(repo, "corr_first")
    with pytest.raises(ThreadRequestRunning):
        _running_request(repo, "corr_second")
    assert repo.get_request("corr_second") is None
    # A new conversation is a separate thread even in the same session.
    _running_request(repo, "corr_other", "conv_other")

    assert repo.finish_request("corr_first") is True
    assert repo.finish_request("corr_first") is False
    _running_request(repo, "corr_second")
    assert repo.get_running_request(
        session_key=SESSION, conversation_id="conv_alpha"
    ).correlation_id == "corr_second"
    assert repo.get_running_request(
        session_key=SESSION, conversation_id="conv_other"
    ).correlation_id == "corr_other"


def test_a_timed_out_reply_wait_keeps_the_request_identity_after_restart(settings) -> None:
    repo = _open(settings)
    _running_request(repo, "corr_report")
    repo.update_request_status("corr_report", "timed_out")

    restarted = _open(settings)
    running = restarted.get_running_request(
        session_key=SESSION, conversation_id="conv_alpha"
    )
    assert running.correlation_id == "corr_report"
    assert running.active_task_id == "task_corr_report"
    assert running.active_task_id != running.followup_to_task_id


def test_held_guidance_and_its_request_commit_together_and_settle_once(settings) -> None:
    repo = _open(settings)
    _running_request(repo, "corr_report")
    held = HeldGuidanceRecord(
        correlation_id="corr_guidance",
        target_correlation_id="corr_report",
        session_key=SESSION,
        conversation_id="conv_alpha",
        steering_json='{"guidance_text":"Use the June report"}',
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    def save(unit):
        unit.request.record_request(
            correlation_id="corr_guidance",
            session_key=SESSION,
            request_kind="task_steering",
            request_text="Use the June report",
            conversation_id="conv_alpha",
            followup_to_task_id="task_corr_report",
            status="held",
        )
        unit.request.hold_guidance(held)

    with repo.request_unit_of_work() as unit:
        save(unit)
    assert repo.get_request("corr_guidance") is None
    assert repo.get_held_guidance("corr_guidance") is None

    with repo.request_unit_of_work() as unit:
        save(unit)
        unit.commit()
    restarted = _open(settings)
    assert restarted.list_held_guidance() == [held]
    assert restarted.release_held_guidance("corr_guidance", status="accepted") is True
    assert restarted.release_held_guidance("corr_guidance", status="rejected") is False
    assert restarted.get_request("corr_guidance").status == "accepted"
    assert restarted.list_held_guidance() == []


def test_simultaneous_messages_admit_only_one_request_in_the_thread(settings) -> None:
    repo = _open(settings)
    ready = Barrier(2)

    def start(correlation_id):
        ready.wait(timeout=10)
        try:
            _running_request(repo, correlation_id)
        except ThreadRequestRunning:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, ["corr_first", "corr_second"]))
    assert sorted(results) == [False, True]
    assert len(repo.list_requests_for_session(SESSION)) == 1


def test_simultaneous_questions_of_different_tasks_are_both_kept(settings) -> None:
    repo = _open(settings)
    ready = Barrier(2)

    def ask(suffix):
        ready.wait(timeout=10)
        return _ask(repo, f"q_{suffix}", correlation_id=f"corr_{suffix}", task_id=f"task_{suffix}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ask, ["report", "trip"]))
    assert all(result.created for result in results)
    assert set(_open_ids(repo)) == {"q_report", "q_trip"}
