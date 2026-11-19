"""The setup page records keep each page's result until it is sent, on real SQLite and PostgreSQL.

A page that ends while no team lead can be reached keeps its result in its
record (``ToolSetupRecord.result_json``, ``result_state``) so the gateway sends
it later (``assistant_gateway.application.tool_setup``). Covered: the claim
that ends a page stores exactly one result as unsent; an unsent result moves
to sent or unsendable once; only unsent results are listed for sending; a
table from before this change gains the columns when the store opens; and a
removed person's setup pages go with their chat sessions. PostgreSQL runs need
``LIVE_TEST_POSTGRES_DSN``; each test gets its own schema.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from assistant_gateway.domain.request_state import (
    TOOL_SETUP_RESULT_SENT,
    TOOL_SETUP_RESULT_UNSENDABLE,
    TOOL_SETUP_RESULT_UNSENT,
    ToolSetupRecord,
)
from storage.config import StorageSettings, resolve_storage_settings
from storage.gateway_state_repository import create_gateway_state_repository
from tests.test_gateway_question_store import _postgres_settings

SESSION = "session_alpha"
# The table as versions before kept results created it.
_TABLE_BEFORE_KEPT_RESULTS = """
CREATE TABLE gateway_tool_setups (
    setup_request_id TEXT PRIMARY KEY,
    parent_task_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    owner_kind TEXT NOT NULL,
    session_key TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    conversation_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
)
"""


@pytest.fixture(
    params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])]
)
def settings(request, tmp_path: Path) -> Iterator[StorageSettings]:
    if request.param == "postgres":
        with _postgres_settings() as postgres:
            yield postgres
        return
    yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "gateway_state.db")}})


def _open(settings: StorageSettings):
    return create_gateway_state_repository(settings, logger=logging.getLogger("test"))


def _execute(settings: StorageSettings, statement: str, params: tuple = ()) -> list[tuple]:
    """Run SQL beside the repository, as an earlier version or an operator would."""

    if settings.backend == "postgres":
        from storage.postgres_connection import connect_postgres

        with connect_postgres(str(settings.postgres_dsn)) as conn:
            cursor = conn.execute(statement.replace("?", "%s"), params)
            rows = cursor.fetchall() if statement.lstrip().upper().startswith("SELECT") else []
            return [tuple(row.values()) for row in rows]
    with sqlite3.connect(str(settings.sqlite_path)) as conn:
        return [tuple(row) for row in conn.execute(statement, params).fetchall()]


def _page(setup_request_id: str, *, session_key: str = SESSION, person_id: str = "alice") -> ToolSetupRecord:
    return ToolSetupRecord(
        setup_request_id=setup_request_id,
        parent_task_id="task_alpha",
        person_id=person_id,
        tool_id="cli:github",
        kind="sign_in",
        owner_kind="person",
        session_key=session_key,
        correlation_id="corr_alpha",
        conversation_id="conv_alpha",
        status="waiting",
        created_at=f"2026-11-19T10:00:0{setup_request_id[-1]}+00:00",
    )


def _result(setup_request_id: str) -> str:
    return '{"setup_request_id": "%s", "status": "completed"}' % setup_request_id


def test_ending_a_page_keeps_its_one_result_unsent(settings) -> None:
    repo = _open(settings)
    repo.record_tool_setup(_page("page_1"))
    assert repo.get_tool_setup("page_1").result_state is None, "a waiting page has no result yet"
    assert repo.list_unsent_tool_setup_results() == []

    assert repo.finish_tool_setup("page_1", status="completed", result_json=_result("page_1"))
    assert not repo.finish_tool_setup("page_1", status="expired", result_json=_result("other")), "one result per page"

    record = repo.get_tool_setup("page_1")
    assert (record.status, record.result_json, record.result_state) == (
        "completed",
        _result("page_1"),
        TOOL_SETUP_RESULT_UNSENT,
    )
    assert record.result_unsent and record.finished_at
    assert [item.setup_request_id for item in repo.list_unsent_tool_setup_results()] == ["page_1"]
    [listed] = repo.list_tool_setups(parent_task_id="task_alpha")
    assert listed == record


def test_an_unsent_result_is_marked_sent_or_unsendable_once_and_its_copy_dropped(settings) -> None:
    repo = _open(settings)
    for page_id in ("page_1", "page_2", "page_3"):
        repo.record_tool_setup(_page(page_id))
        assert repo.finish_tool_setup(page_id, status="completed", result_json=_result(page_id))
    assert [item.setup_request_id for item in repo.list_unsent_tool_setup_results()] == ["page_1", "page_2", "page_3"]

    assert repo.mark_tool_setup_result("page_1", state=TOOL_SETUP_RESULT_SENT)
    assert not repo.mark_tool_setup_result("page_1", state=TOOL_SETUP_RESULT_SENT), "only the first caller"
    assert not repo.mark_tool_setup_result("page_1", state=TOOL_SETUP_RESULT_UNSENDABLE), "sent stays sent"
    assert repo.mark_tool_setup_result("page_2", state=TOOL_SETUP_RESULT_UNSENDABLE)
    repo.record_tool_setup(_page("page_4"))
    assert not repo.mark_tool_setup_result("page_4", state=TOOL_SETUP_RESULT_SENT), "a waiting page has nothing to send"

    assert [item.setup_request_id for item in repo.list_unsent_tool_setup_results()] == ["page_3"]
    sent, unsendable, unsent = (repo.get_tool_setup(page_id) for page_id in ("page_1", "page_2", "page_3"))
    assert (sent.result_state, sent.result_json) == (TOOL_SETUP_RESULT_SENT, None), "the copy goes once it is sent"
    assert (unsendable.result_state, unsendable.result_json) == (TOOL_SETUP_RESULT_UNSENDABLE, None)
    assert unsent.result_json == _result("page_3"), "an unsent result keeps its copy"


def test_a_table_from_before_kept_results_gains_their_columns_when_the_store_opens(settings, caplog) -> None:
    _execute(settings, _TABLE_BEFORE_KEPT_RESULTS)
    columns = "setup_request_id, parent_task_id, person_id, tool_id, kind, owner_kind, session_key, " \
        "correlation_id, conversation_id, status, created_at, finished_at"
    for page_id, status, finished_at in (
        ("page_old", "completed", "2026-11-18T10:00:00+00:00"),
        ("page_open", "waiting", None),
    ):
        _execute(
            settings,
            f"INSERT INTO gateway_tool_setups ({columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (page_id, "task_alpha", "alice", "cli:github", "sign_in", "person", SESSION, "corr_alpha",
             "conv_alpha", status, "2026-11-18T09:00:00+00:00", finished_at),
        )

    with caplog.at_level(logging.INFO):
        repo = _open(settings)

    # The shared upgrade step (storage.table_columns.ensure_column) adds and logs each column once.
    added = [record.getMessage() for record in caplog.records if "event=storage_column_added" in record.getMessage()]
    assert [line.rsplit(" table=", 1)[1] for line in added] == [
        "gateway_tool_setups column=result_json",
        "gateway_tool_setups column=result_state",
    ]
    old = repo.get_tool_setup("page_old")
    assert (old.status, old.result_json, old.result_state) == ("completed", None, None)
    assert repo.list_unsent_tool_setup_results() == [], "a result finished before this version is unknown, never resent"
    assert repo.finish_tool_setup("page_open", status="expired", result_json=_result("page_open"))
    assert [item.setup_request_id for item in repo.list_unsent_tool_setup_results()] == ["page_open"]
    if settings.backend == "postgres":
        indexes = _execute(settings, "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = 'gateway_tool_setups'")
    else:
        indexes = _execute(settings, "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'gateway_tool_setups'")
    assert ("idx_gateway_tool_setups_unsent",) in indexes
    # Opening the store again changes nothing.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert _open(settings).get_tool_setup("page_open").result_unsent
    assert "event=storage_column_added" not in caplog.text


def test_a_removed_persons_setup_pages_go_with_their_chat_sessions(settings) -> None:
    repo = _open(settings)
    for session_key, person_id in ((SESSION, "alice"), ("session_bob", "bob")):
        repo.create_session(
            session_key=session_key, conversation_id=f"conv_{person_id}", manager_agent_id="manager_1",
            owner_canonical_user_id=person_id,
        )
    repo.record_tool_setup(_page("page_1"))
    assert repo.finish_tool_setup("page_1", status="completed", result_json=_result("page_1"))
    repo.record_tool_setup(_page("page_2", session_key="session_bob", person_id="bob"))

    repo.forget_person_records("alice")

    assert repo.get_tool_setup("page_1") is None
    assert repo.list_unsent_tool_setup_results() == []
    assert repo.get_tool_setup("page_2") is not None
