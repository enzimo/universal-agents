"""What 0.9.12 and 0.9.13 stored about a conversation's files opens unchanged here.

Those releases shipped the turn's files (``conversation_task_links.attachments_json``,
``attachments_recorded`` and its partial index) and the task-history entries
``task_files_accepted`` and ``request_files_not_kept``. An instance upgraded in
place keeps them (AGENTS.md: in-place upgrades from every 0.9 release, no
fallback readers), so the current readers must take them as written.

The fixtures were written by v0.9.13's own code, not by hand: its
``ConversationRepository`` (``append_turn``, ``merge_turn_attachments``) for
``tests/fixtures/upgrade/0.9.13-conversation-files.sql`` (its two conversation
tables with their indexes and rows), and its ``admit_request_files`` and
``task_files_details`` for the held request in
``tests/fixtures/upgrade/0.9.13-task-with-files.json`` (``Task.model_dump_json``).
The broader upgrade suite (``scripts/run_tests.sh upgrade``) starts from
earlier 0.9 tables that predate these columns, so it covers only the path
that adds them.

Runs on SQLite and, with ``LIVE_TEST_POSTGRES_DSN``, on PostgreSQL (one schema
per test): 0.9.13 used the same table definitions for both.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from custom_agents.conversation_attachments import (
    REQUEST_FILES_NOT_KEPT_NOTE,
    UNRECORDED_TURNS_NOTE,
    TaskAttachments,
    answer_files,
    inventory_text,
)
from custom_agents.conversation_store import ConversationStore
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.task_v1 import Task
from storage.config import StorageSettings, resolve_storage_settings
from storage.postgres_connection import connect_postgres
from storage.sqlite_connection import connect_sqlite

FIXTURES = Path(__file__).parent / "fixtures" / "upgrade"
CONVERSATION = "conv_0913"
PERSON = "usr_ann"
LOGGER = logging.getLogger("test.shipped_file_records")


@dataclass
class _Instance:
    """A conversation database holding 0.9.13's tables and rows, and how to open today's store on it."""

    settings: StorageSettings
    db_path: str
    artifacts: Path

    def store(self) -> ConversationStore:
        return ConversationStore(self.db_path, artifacts_dir=self.artifacts, logger=LOGGER, storage_settings=self.settings)


def _statements() -> list[str]:
    text = (FIXTURES / "0.9.13-conversation-files.sql").read_text()
    return [line for line in text.splitlines() if line.strip() and not line.startswith("--")]


def _load(connect: Callable[[], Any]) -> None:
    with connect() as conn:
        for statement in _statements():
            conn.execute(statement)
        conn.commit()


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres]),
    ]
)
def instance(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[_Instance]:
    artifacts = tmp_path / "conversation_artifacts"
    if request.param == "sqlite":
        db_path = str(tmp_path / "tasks.db")
        _load(lambda: connect_sqlite(db_path))
        settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": db_path}})
        yield _Instance(settings, db_path, artifacts)
        return
    from psycopg.conninfo import make_conninfo

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set LIVE_TEST_POSTGRES_DSN to a disposable database to run the PostgreSQL upgrade tests.")
    schema = "shipped_files_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    scoped = make_conninfo(dsn, options=f"-csearch_path={schema}")
    try:
        _load(lambda: connect_postgres(scoped))
        settings = resolve_storage_settings({"backend": "postgres", "postgres": {"dsn": scoped}})
        yield _Instance(settings, str(tmp_path / "unused.db"), artifacts)
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def _held_request() -> Task:
    return Task.model_validate_json((FIXTURES / "0.9.13-task-with-files.json").read_text())


def _ids(attachments: Any) -> list[str]:
    return [attachment.attachment_id for attachment in attachments]


def test_turns_0_9_13_saved_list_their_files_and_say_which_request_files_are_unknown(instance: _Instance) -> None:
    store = instance.store()

    listed = store.list_turn_attachments(CONVERSATION)

    # Newest first: turn 2, then turn 1's guidance file (merged in later) and its request's two files.
    assert [(entry.turn_index, entry.attachment.attachment_id) for entry in listed] == [
        (2, "att_menu"), (1, "att_guidance"), (1, "att_invoice"), (1, "att_receipt"),
    ]
    menu = listed[0].attachment
    assert (menu.filename, menu.caption, menu.origin) == ("menu.png", "caption of menu.png", "web_chat")
    assert menu.local_path() == "/instance/state/uploads/web_session/att_menu.png"
    assert _ids(entry.attachment for entry in store.list_turn_attachments(CONVERSATION, attachment_ids=["att_invoice"])) == [
        "att_invoice"
    ]
    # Turn 2 was saved with attachments_recorded 0: some of its request's files were not kept.
    assert store.last_turn_without_file_records(CONVERSATION) == 2
    assert store.last_turn_without_file_records(CONVERSATION, exclude_task_id="task_2") is None


def test_a_turn_0_9_13_saved_takes_new_files_after_the_upgrade(instance: _Instance) -> None:
    store = instance.store()
    later = UserTaskAttachment(
        attachment_id="att_later",
        filename="later.png",
        content_type="image/png",
        kind="image",
        size_bytes=3,
        sha256="2" * 64,
        created_at="2026-11-16T09:00:00+00:00",
        storage_refs=[AttachmentStorageRef(storage_type="local", path="/instance/state/uploads/web_session/att_later.png")],
    )

    added = store.merge_turn_attachments(conversation_id=CONVERSATION, task_id="task_3", attachments=[later])

    assert added == 1
    assert _ids(entry.attachment for entry in instance.store().list_turn_attachments(CONVERSATION))[0] == "att_later"


def test_a_request_held_across_the_upgrade_keeps_its_files_and_its_count_of_files_not_kept(instance: _Instance) -> None:
    task = _held_request()
    held = SimpleNamespace(task=task, meta=SimpleNamespace(conversation_id=CONVERSATION), owner_person_id=PERSON)

    scope = TaskAttachments.load(held, instance.store())

    # Its own files: the request's, then those kept from guidance, an answer and an escalation result.
    assert _ids(scope.own) == ["att_receipt", "att_guidance", "att_answer", "att_result"]
    assert scope.request_files_not_kept == 1
    assert _ids(answer_files(task)) == ["att_answer"]
    assert scope.conversation_id == CONVERSATION
    found, unknown = scope.find(["att_answer", "att_menu", "att_dropped"])
    assert (_ids(found), unknown) == (["att_answer", "att_menu"], ["att_dropped"])
    text = inventory_text(scope, exclude_ids=_ids(scope.own))
    assert "att_menu" in text and "att_invoice" in text and "/instance/state/uploads" not in text
    assert REQUEST_FILES_NOT_KEPT_NOTE.format(count=1) in text
    assert UNRECORDED_TURNS_NOTE.format(turn=2) in text
