"""Conversation turns keep the files they received (storage.turn_attachments).

Runs against SQLite and, with ``LIVE_TEST_POSTGRES_DSN``, a real PostgreSQL
server (one schema per test): the column is added in place on both, merges
into one turn from many threads lose nothing, and lookups read newest turns
first in batches.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from storage.config import resolve_storage_settings
from storage.conversation_repository import (
    TURN_ATTACHMENT_SCAN_BATCH_ROWS,
    ConversationRepository,
    create_conversation_repository,
)
from storage.postgres_connection import connect_postgres
from storage.sqlite_connection import connect_sqlite
from storage.turn_attachments import TURN_ATTACHMENT_SUMMARY_FIELDS

CONVERSATION = "conv_files"
OWNER = "usr_ann"
LOGGER = logging.getLogger("test.turn_attachments")


@dataclass
class _Backend:
    """Opens the conversation store (again, as after a restart) and runs raw SQL on its database."""

    open: Callable[[], ConversationRepository]
    connect: Callable[[], Any]
    bind: str

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql.replace("?", self.bind), params)
            conn.commit()


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres]),
    ]
)
def backend(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[_Backend]:
    artifacts = tmp_path / "conversation_artifacts"
    if request.param == "sqlite":
        db_path = str(tmp_path / "tasks.db")
        settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": db_path}})
        yield _Backend(
            open=lambda: create_conversation_repository(settings, logger=LOGGER, artifacts_dir=artifacts),
            connect=lambda: connect_sqlite(db_path),
            bind="?",
        )
        return
    from psycopg.conninfo import make_conninfo

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set LIVE_TEST_POSTGRES_DSN to a disposable database to run the PostgreSQL conversation tests.")
    schema = "turn_files_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    scoped = make_conninfo(dsn, options=f"-csearch_path={schema}")
    settings = resolve_storage_settings({"backend": "postgres", "postgres": {"dsn": scoped}})
    try:
        yield _Backend(
            open=lambda: create_conversation_repository(settings, logger=LOGGER, artifacts_dir=artifacts),
            connect=lambda: connect_postgres(scoped),
            bind="%s",
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def _file(attachment_id: str, *, filename: str | None = None, caption: str | None = None) -> UserTaskAttachment:
    return UserTaskAttachment(
        attachment_id=attachment_id,
        filename=filename or f"{attachment_id}.png",
        content_type="image/png",
        kind="image",
        size_bytes=12,
        sha256="0" * 64,
        created_at="2026-11-12T00:00:00+00:00",
        storage_refs=[AttachmentStorageRef(storage_type="local", path=f"/instance/state/uploads/c/{attachment_id}")],
        caption=caption,
    )


def _thread(repo: ConversationRepository, conversation_id: str = CONVERSATION) -> None:
    repo.ensure_thread(
        conversation_id=conversation_id,
        team_id="team",
        owner_agent_id="lead",
        owner_agent_name="Lead",
        requester_name="Ann",
        requester_id=OWNER,
        root_task_id="task_1",
        owner_canonical_user_id=OWNER,
    )


def _turn(
    repo: ConversationRepository,
    task_id: str,
    files: tuple[UserTaskAttachment, ...] = (),
    *,
    conversation_id: str = CONVERSATION,
) -> dict[str, Any]:
    return repo.append_turn(
        conversation_id=conversation_id,
        task_id=task_id,
        followup_to_task_id=None,
        owner_agent_id="lead",
        requester_name="Ann",
        requester_id=OWNER,
        correlation_id=f"corr_{task_id}",
        problem_text=f"Request {task_id}",
        owner_canonical_user_id=OWNER,
        attachments=files,
    )


def _listed(repo: ConversationRepository, **kwargs: Any) -> list[tuple[int, str, str]]:
    return [
        (item.turn_index, item.task_id, item.attachment.attachment_id)
        for item in repo.list_turn_attachments(CONVERSATION, **kwargs)
    ]


def test_a_turn_keeps_its_files_after_the_store_is_opened_again(backend: _Backend) -> None:
    repo = backend.open()
    _thread(repo)
    photo, page = _file("att_photo", caption="my fig"), _file("att_page", filename="notes.pdf")
    # The same file twice in one request is kept once.
    _turn(repo, "task_1", (photo, page, photo))
    _turn(repo, "task_2")

    reopened = backend.open()
    first = reopened.get_turn_by_task_id("task_1")
    second = reopened.get_turn_by_task_id("task_2")

    # Generic turn reads carry what a model may see: no storage references or checksums.
    assert [item["attachment_id"] for item in first["attachments"]] == ["att_photo", "att_page"]
    assert all(set(item) == set(TURN_ATTACHMENT_SUMMARY_FIELDS) for item in first["attachments"])
    assert first["attachments"][0]["caption"] == "my fig"
    assert "/instance/state/uploads" not in repr(first)
    assert second["attachments"] == []
    # The full records, storage references included, come only from the trusted lookup.
    listed = reopened.list_turn_attachments(CONVERSATION)
    assert [item.attachment for item in listed] == [page, photo]
    assert {(item.turn_index, item.task_id) for item in listed} == {(1, "task_1")}


def test_merging_adds_only_new_files_and_reports_how_many(backend: _Backend, caplog: pytest.LogCaptureFixture) -> None:
    repo = backend.open()
    _thread(repo)
    photo = _file("att_photo")
    _turn(repo, "task_1", (photo,))

    assert repo.merge_turn_attachments(conversation_id=CONVERSATION, task_id="task_1", attachments=[_file("att_2")]) == 1
    # A retried merge adds nothing twice.
    assert repo.merge_turn_attachments(conversation_id=CONVERSATION, task_id="task_1", attachments=[_file("att_2")]) == 0
    # A different record under a known id is not saved, and is logged.
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        changed = _file("att_photo", filename="other.png")
        assert repo.merge_turn_attachments(conversation_id=CONVERSATION, task_id="task_1", attachments=[changed]) == 0
    assert "event=conversation_turn_attachment_conflict" in caplog.text
    assert "attachment_id=att_photo" in caplog.text
    # No turn row: nothing is written and the caller is told.
    assert repo.merge_turn_attachments(conversation_id=CONVERSATION, task_id="task_missing", attachments=[photo]) is None
    assert repo.merge_turn_attachments(conversation_id="conv_other", task_id="task_1", attachments=[photo]) is None

    turn = backend.open().get_turn_by_task_id("task_1")
    assert [(item["attachment_id"], item["filename"]) for item in turn["attachments"]] == [
        ("att_photo", "att_photo.png"),
        ("att_2", "att_2.png"),
    ]


def test_concurrent_merges_into_one_turn_lose_no_file(backend: _Backend) -> None:
    _thread(backend.open())
    _turn(backend.open(), "task_1", (_file("att_initial"),))
    writers, files_each = 6, 5
    start = threading.Barrier(writers)
    errors: list[BaseException] = []

    def merge(writer: int) -> None:
        # Each thread has its own store and connections, like separate processes.
        repo = backend.open()
        start.wait()
        try:
            for number in range(files_each):
                # Every file is also sent by the next writer, so retries race with first writes.
                for owner in (writer, (writer + 1) % writers):
                    repo.merge_turn_attachments(
                        conversation_id=CONVERSATION,
                        task_id="task_1",
                        attachments=[_file(f"att_{owner}_{number}")],
                    )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=merge, args=(writer,)) for writer in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    stored = [item["attachment_id"] for item in backend.open().get_turn_by_task_id("task_1")["attachments"]]
    expected = {"att_initial"} | {f"att_{writer}_{number}" for writer in range(writers) for number in range(files_each)}
    assert len(stored) == len(set(stored)) == len(expected)
    assert set(stored) == expected
    assert stored[0] == "att_initial"


def test_files_are_listed_newest_first_once_each_and_only_from_their_conversation(backend: _Backend) -> None:
    repo = backend.open()
    _thread(repo)
    _thread(repo, "conv_other")
    _turn(repo, "task_1", (_file("att_a"), _file("att_b")))
    _turn(repo, "task_2")
    _turn(repo, "task_3", (_file("att_c"),))
    repo.merge_turn_attachments(conversation_id=CONVERSATION, task_id="task_3", attachments=[_file("att_d")])
    # The same upload named again in a later turn is listed once, with that turn.
    _turn(repo, "task_4", (_file("att_a"),))
    _turn(repo, "task_other", (_file("att_elsewhere"),), conversation_id="conv_other")

    assert _listed(repo) == [
        (4, "task_4", "att_a"),
        (3, "task_3", "att_d"),
        (3, "task_3", "att_c"),
        (1, "task_1", "att_b"),
    ]
    assert _listed(repo, limit=2) == [(4, "task_4", "att_a"), (3, "task_3", "att_d")]
    assert _listed(repo, attachment_ids={"att_b", "att_c", "att_elsewhere", "att_unknown"}) == [
        (3, "task_3", "att_c"),
        (1, "task_1", "att_b"),
    ]
    assert _listed(repo, attachment_ids=[]) == []
    assert _listed(repo, limit=0) == []
    assert repo.list_turn_attachments("conv_none") == []


def test_lookups_read_past_the_first_batch_of_turns(backend: _Backend) -> None:
    repo = backend.open()
    _thread(repo)
    turns = TURN_ATTACHMENT_SCAN_BATCH_ROWS * 2 + 30
    for number in range(1, turns + 1):
        # Every third turn has no files, so batches skip rows without them.
        files = () if number % 3 == 0 else (_file(f"att_{number}"),)
        _turn(repo, f"task_{number:04d}", files)

    listed = _listed(repo)
    expected = [number for number in range(turns, 0, -1) if number % 3]
    assert [turn_index for turn_index, _, _ in listed] == expected
    assert [attachment_id for _, _, attachment_id in listed] == [f"att_{number}" for number in expected]
    # The oldest file is found by id after every newer batch.
    assert _listed(repo, attachment_ids={"att_1", f"att_{turns - 1}"}) == [
        (turns - 1, f"task_{turns - 1:04d}", f"att_{turns - 1}"),
        (1, "task_0001", "att_1"),
    ]


def test_turns_saved_before_the_upgrade_say_their_files_were_not_recorded(backend: _Backend) -> None:
    repo = backend.open()
    _thread(repo)
    _turn(repo, "task_old")
    _turn(repo, "task_old_running")
    # The table as it was before turns kept their files.
    backend.execute("DROP INDEX idx_conversation_task_links_unrecorded_files")
    backend.execute("ALTER TABLE conversation_task_links DROP COLUMN attachments_json")

    upgraded = backend.open()

    old = upgraded.get_turn_by_task_id("task_old")
    # No files listed, and marked as unknown rather than none.
    assert (old["attachments"], old["attachments_recorded"]) == ([], False)
    assert upgraded.list_turn_attachments(CONVERSATION) == []
    assert upgraded.last_turn_without_file_records(CONVERSATION) == 2
    assert upgraded.last_turn_without_file_records(CONVERSATION, exclude_task_id="task_old_running") == 1
    assert upgraded.last_turn_without_file_records("conv_none") is None
    # A file sent while an old turn still runs is recorded on it.
    assert upgraded.merge_turn_attachments(
        conversation_id=CONVERSATION, task_id="task_old_running", attachments=[_file("att_late")]
    ) == 1
    assert upgraded.get_turn_by_task_id("task_old_running")["attachments_recorded"] is True
    assert upgraded.last_turn_without_file_records(CONVERSATION) == 1
    # New turns record their files, none included.
    _turn(upgraded, "task_new", (_file("att_new"),))
    _turn(upgraded, "task_new_empty")
    empty = upgraded.get_turn_by_task_id("task_new_empty")
    assert (empty["attachments"], empty["attachments_recorded"]) == ([], True)
    assert upgraded.last_turn_without_file_records(CONVERSATION) == 1
    assert _listed(upgraded) == [(3, "task_new", "att_new"), (2, "task_old_running", "att_late")]


def test_a_conversation_started_after_the_upgrade_has_no_unrecorded_turns(backend: _Backend) -> None:
    repo = backend.open()
    _thread(repo)
    _turn(repo, "task_1", (_file("att_photo"),))
    _turn(repo, "task_2")

    assert repo.last_turn_without_file_records(CONVERSATION) is None
    assert all(turn["attachments_recorded"] for turn in repo.get_conversation_turns(conversation_id=CONVERSATION))


def test_unreadable_stored_files_are_logged_and_skipped(backend: _Backend, caplog: pytest.LogCaptureFixture) -> None:
    repo = backend.open()
    _thread(repo)
    _turn(repo, "task_bad")
    _turn(repo, "task_mixed", (_file("att_good"),))
    backend.execute(
        "UPDATE conversation_task_links SET attachments_json = ? WHERE task_id = ?", ("{not json", "task_bad")
    )
    backend.execute(
        "UPDATE conversation_task_links SET attachments_json = ? WHERE task_id = ?",
        ('[{"attachment_id": "att_broken"}, ' + _file("att_good").model_dump_json() + "]", "task_mixed"),
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        assert repo.get_turn_by_task_id("task_bad")["attachments"] == []
        assert [item["attachment_id"] for item in repo.get_turn_by_task_id("task_mixed")["attachments"]] == ["att_good"]
        assert _listed(repo) == [(2, "task_mixed", "att_good")]
    assert "event=conversation_turn_attachments_unreadable conversation_id=conv_files task_id=task_bad" in caplog.text
    assert "event=conversation_turn_attachment_unreadable conversation_id=conv_files task_id=task_mixed" in caplog.text
    assert "attachment_id=att_broken" in caplog.text
    # A later merge writes a readable list again.
    assert repo.merge_turn_attachments(conversation_id=CONVERSATION, task_id="task_bad", attachments=[_file("att_new")]) == 1
    assert _listed(repo, attachment_ids={"att_new"}) == [(1, "task_bad", "att_new")]


def test_a_persons_storage_notice_counts_their_turns_file_records(backend: _Backend) -> None:
    repo = backend.open()
    _thread(repo)
    _turn(repo, "task_1")
    without_files = repo.stored_text_bytes_by_owner()[OWNER]

    repo.merge_turn_attachments(conversation_id=CONVERSATION, task_id="task_1", attachments=[_file("att_photo")])

    record_bytes = len(f"[{_file('att_photo').model_dump_json()}]") - len("[]")
    assert repo.stored_text_bytes_by_owner()[OWNER] - without_files >= record_bytes
