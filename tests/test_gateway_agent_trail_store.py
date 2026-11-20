"""The gateway store's agent trail (storage.gateway_agent_trails, A2), on SQLite and real PostgreSQL.

Each request keeps the agent types that worked on it, first recorded first;
an agent names a request only by its task, and only a running interactive
request of this gateway changes. The thread's stored copy follows the requests,
survives reopening the store, and history cleanup rebuilds it. On PostgreSQL a
cleanup that overlaps a recording writes a copy that includes it (C03).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from assistant_gateway.domain.agent_trail import TrailAgent, decode_thread_trail
from assistant_gateway.domain.history import GatewayHistoryScope
from assistant_gateway.domain.request_state import (
    REQUEST_KIND_INTERACTIVE,
    REQUEST_KIND_TASK_STEERING,
    RUN_STATE_RUNNING,
)
from storage.config import StorageSettings, resolve_storage_settings
from storage.gateway_state_repository import create_gateway_state_repository
from tests.test_gateway_question_store import _postgres_settings

ALICE = "alice"
THREAD = "website:web-chat:alpha"
LEAD = "TeamArchitect"
WEB = "WebResearchAgent"
LOGGER = logging.getLogger("test.agent_trail_store")


@pytest.fixture(
    params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])]
)
def settings(request, tmp_path: Path) -> Iterator[StorageSettings]:
    if request.param == "postgres":
        with _postgres_settings() as postgres:
            yield postgres
        return
    yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "gateway_state.db")}})


def _repo(settings: StorageSettings):
    return create_gateway_state_repository(settings, logger=LOGGER)


def _thread(repo, session_key: str = THREAD) -> None:
    repo.create_session(
        session_key=session_key, conversation_id=f"conv_{session_key}", manager_agent_id="manager_1",
        channel_name="website", user_id=ALICE, chat_id="web-chat", owner_canonical_user_id=ALICE,
    )


def _request(repo, number: int, *, session_key: str = THREAD, running: bool = True,
             kind: str = REQUEST_KIND_INTERACTIVE) -> str:
    repo.record_request(
        correlation_id=f"corr_{number}", session_key=session_key, request_kind=kind, request_text=f"ask {number}",
        conversation_id=f"conv_{session_key}", followup_to_task_id=None, owner_canonical_user_id=ALICE,
        active_task_id=f"task_{number}", run_state=RUN_STATE_RUNNING if running else None,
    )
    return f"task_{number}"


def _listed(repo, session_key: str = THREAD):
    [record] = [item for item in repo.list_web_chat_threads(
        owner_canonical_user_id=ALICE, channel_name="website", chat_id="web-chat", limit=100,
    ) if item.session_key == session_key]
    return record


def _shape(repo, session_key: str = THREAD) -> list[str]:
    trail = decode_thread_trail(_listed(repo, session_key).agent_trail_json)
    return ["P" if entry.is_person else entry.agent.agent_type for entry in trail.entries]


def test_agents_are_recorded_once_per_request_in_first_recorded_order(settings) -> None:
    repo = _repo(settings)
    _thread(repo)
    task = _request(repo, 1)
    assert repo.record_request_agent(active_task_id=task, agent_type=LEAD, lead=True) == THREAD
    assert repo.record_request_agent(active_task_id=task, agent_type=WEB, lead=False) == THREAD
    assert repo.record_request_agent(active_task_id=task, agent_type=LEAD, lead=True) is None
    assert repo.record_request_agent(active_task_id=task, agent_type=WEB, lead=False) is None
    assert _shape(repo) == ["P", LEAD, WEB]
    trail = decode_thread_trail(_listed(repo).agent_trail_json)
    assert trail.agents == (TrailAgent(LEAD, True), TrailAgent(WEB)) and not trail.has_older


def test_ids_the_gateway_does_not_hold_as_running_interactive_requests_change_nothing(settings) -> None:
    repo = _repo(settings)
    _thread(repo)
    finished = _request(repo, 1)
    repo.finish_request("corr_1")
    guidance = _request(repo, 2, running=False, kind=REQUEST_KIND_TASK_STEERING)
    for task in (finished, guidance, "task_unknown", ""):
        assert repo.record_request_agent(active_task_id=task, agent_type=WEB, lead=False) is None
    assert _listed(repo).agent_trail_json is None


def test_the_list_names_the_running_requests_task_and_the_trail_survives_reopening(settings) -> None:
    repo = _repo(settings)
    _thread(repo)
    task = _request(repo, 1)
    repo.record_request_agent(active_task_id=task, agent_type=LEAD, lead=True)
    assert _listed(repo).running_task_id == task
    repo.finish_request("corr_1")
    reopened = _repo(settings)
    assert _listed(reopened).running_task_id is None
    assert _shape(reopened) == ["P", LEAD]


def test_the_copy_keeps_the_last_six_entries_and_says_when_older_ones_exist(settings) -> None:
    repo = _repo(settings)
    _thread(repo)
    for number in range(1, 5):
        task = _request(repo, number)
        repo.record_request_agent(active_task_id=task, agent_type=LEAD, lead=True)
        if number == 4:
            repo.record_request_agent(active_task_id=task, agent_type=WEB, lead=False)
        repo.finish_request(f"corr_{number}")
        time.sleep(0.002)  # requests are ordered by their creation time
    trail = decode_thread_trail(_listed(repo).agent_trail_json)
    assert _shape(repo) == [LEAD, "P", LEAD, "P", LEAD, WEB] and trail.has_older


def test_history_cleanup_rebuilds_the_trail_from_what_is_left(settings) -> None:
    repo = _repo(settings)
    _thread(repo)
    for number in (1, 2):
        task = _request(repo, number)
        repo.record_request_agent(active_task_id=task, agent_type=LEAD if number == 1 else WEB, lead=number == 1)
        repo.finish_request(f"corr_{number}")
        time.sleep(0.002)
    assert _shape(repo) == ["P", LEAD, "P", WEB]
    # Delete everything of Alice's from before a moment after the first request.
    cut = _created_at(repo, "corr_1")
    repo.delete_history_records(
        GatewayHistoryScope(person_id=ALICE, selected_at=_now(), older_than=cut), removable_thread=lambda _: False,
    )
    assert repo.get_request("corr_1") is None and repo.get_request("corr_2") is not None
    trail = decode_thread_trail(_listed(repo).agent_trail_json)
    assert _shape(repo) == ["P", WEB] and trail.agents == (TrailAgent(WEB),)


def test_an_unreadable_stored_agent_list_is_logged_and_left_out(settings, caplog) -> None:
    repo = _repo(settings)
    _thread(repo)
    first = _request(repo, 1)
    repo.record_request_agent(active_task_id=first, agent_type=LEAD, lead=True)
    repo.finish_request("corr_1")
    _execute(settings, "UPDATE gateway_requests SET agent_types_json = 'broken' WHERE correlation_id = 'corr_1'")
    time.sleep(0.002)
    second = _request(repo, 2)
    with caplog.at_level(logging.WARNING):
        repo.record_request_agent(active_task_id=second, agent_type=LEAD, lead=True)
    assert "event=gateway_request_agents_unreadable correlation_id=corr_1" in caplog.text
    assert _shape(repo) == ["P", "P", LEAD]


@pytest.mark.live
@pytest.mark.postgres
def test_a_cleanup_that_overlaps_a_recording_writes_a_copy_that_includes_it() -> None:
    """C03: the recording holds its transaction open; the cleanup of the same thread waits for it and then
    rebuilds from the recording's committed requests, so the copy equals a rebuild made afterwards."""

    from storage.gateway_agent_trails import rebuild_thread_trail, record_request_agent
    from storage.postgres_connection import connect_postgres
    from storage.sql_dialect import POSTGRES

    with _postgres_settings() as settings:
        repo = _repo(settings)
        _thread(repo)
        old = _request(repo, 1)
        repo.record_request_agent(active_task_id=old, agent_type=LEAD, lead=True)
        repo.finish_request("corr_1")
        time.sleep(0.002)
        running = _request(repo, 2)
        repo.record_request_agent(active_task_id=running, agent_type=LEAD, lead=True)
        cut = _created_at(repo, "corr_1")

        recording = connect_postgres(settings.postgres_dsn or "")
        record_request_agent(recording, POSTGRES, active_task_id=running, agent=TrailAgent(WEB), logger=LOGGER)
        cleaned = threading.Event()

        def cleanup() -> None:
            repo.delete_history_records(
                GatewayHistoryScope(person_id=ALICE, selected_at=_now(), older_than=cut),
                removable_thread=lambda _: False,
            )
            cleaned.set()

        worker = threading.Thread(target=cleanup)
        worker.start()
        assert not cleaned.wait(0.5)  # it waits for the recording's locks
        recording.commit()
        recording.close()
        worker.join(10)
        assert cleaned.is_set()
        stored = _listed(repo).agent_trail_json
        assert _shape(repo) == ["P", LEAD, WEB]
        with connect_postgres(settings.postgres_dsn or "") as conn:
            rebuild_thread_trail(conn, POSTGRES, THREAD, logger=LOGGER)
            conn.commit()
        assert _listed(repo).agent_trail_json == stored


def _now() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()


def _created_at(repo, correlation_id: str) -> str:
    created = datetime.fromisoformat(repo.get_request(correlation_id).created_at)
    return (created + timedelta(microseconds=500)).isoformat()


def _execute(settings: StorageSettings, query: str) -> None:
    if settings.backend == "sqlite":
        import sqlite3

        with sqlite3.connect(settings.sqlite_path or "") as conn:
            conn.execute(query)
        return
    from storage.postgres_connection import connect_postgres

    with connect_postgres(settings.postgres_dsn or "") as conn:
        conn.execute(query)
        conn.commit()
