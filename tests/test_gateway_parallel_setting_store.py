"""The store saves a thread's Run concurrently setting only while no request is open.

``set_session_parallel`` is one conditional update, and the Web Chat thread
list reads the setting and the open-request lock in the same query; both run
here on SQLite and, with ``LIVE_TEST_POSTGRES_DSN``, on real PostgreSQL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from assistant_gateway.domain.request_state import RUN_STATE_RUNNING
from assistant_gateway.domain.sessions import ParallelSettingOutcome
from storage.config import StorageSettings, resolve_storage_settings
from storage.gateway_state_repository import create_gateway_state_repository
from tests.test_gateway_question_store import _postgres_settings

SESSION = "session_alpha"
CONVERSATION = "conv_alpha"


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


def _repository(settings: StorageSettings):
    repo = create_gateway_state_repository(settings, logger=logging.getLogger("test"))
    repo.create_session(
        session_key=SESSION,
        conversation_id=CONVERSATION,
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="alice",
        chat_id="web-chat",
        owner_canonical_user_id="alice",
    )
    return repo


def _open_request(repo, correlation_id: str, conversation_id: str) -> None:
    repo.record_request(
        correlation_id=correlation_id,
        session_key=SESSION,
        request_kind="interactive",
        request_text="Prepare the report",
        conversation_id=conversation_id,
        followup_to_task_id=None,
        active_task_id=f"task_{correlation_id}",
        run_state=RUN_STATE_RUNNING,
    )


def _listed(repo):
    (thread,) = repo.list_web_chat_threads(
        owner_canonical_user_id="alice", channel_name="website", chat_id="web-chat", limit=10
    )
    return thread


def _set(repo, value: bool, *, conversation_id: str = CONVERSATION) -> ParallelSettingOutcome:
    return repo.set_session_parallel(SESSION, conversation_id=conversation_id, parallel_thread=value)


def test_the_setting_changes_only_while_no_request_is_open_in_the_conversation(settings) -> None:
    repo = _repository(settings)
    before = repo.get_session(SESSION).updated_at
    assert _set(repo, True) is ParallelSettingOutcome.SAVED
    listed = _listed(repo)
    assert (listed.parallel_thread, listed.has_open_request) == (True, False)
    assert repo.get_session(SESSION).updated_at == before, "a setting change is not activity"

    _open_request(repo, "corr_older", "conv_older")
    assert _set(repo, False) is ParallelSettingOutcome.SAVED, "another conversation's request does not lock it"

    _open_request(repo, "corr_open", CONVERSATION)
    assert _set(repo, True) is ParallelSettingOutcome.REQUEST_OPEN
    listed = _listed(repo)
    assert (listed.parallel_thread, listed.has_open_request) == (False, True)

    repo.finish_request("corr_open")
    assert _set(repo, True) is ParallelSettingOutcome.SAVED
    assert repo.get_session(SESSION).parallel_thread is True


def test_stale_missing_and_archived_threads_are_refused_and_new_ones_start_off(settings) -> None:
    repo = _repository(settings)
    assert _set(repo, True, conversation_id="conv_shown_earlier") is ParallelSettingOutcome.CONVERSATION_CHANGED
    assert repo.set_session_parallel(
        "no_such_session", conversation_id=CONVERSATION, parallel_thread=True
    ) is ParallelSettingOutcome.NOT_FOUND
    assert _set(repo, True) is ParallelSettingOutcome.SAVED

    repo.create_session(
        session_key=SESSION, conversation_id="conv_again", manager_agent_id="manager_1",
        channel_name="website", user_id="alice", chat_id="web-chat", owner_canonical_user_id="alice",
    )
    assert repo.get_session(SESSION).parallel_thread is False, "a re-created session starts with it off"

    repo.set_session_status(SESSION, "archived")
    assert _set(repo, True, conversation_id="conv_again") is ParallelSettingOutcome.NOT_FOUND
