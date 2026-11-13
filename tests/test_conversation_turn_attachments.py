"""Each conversation turn keeps the files the person sent with it (``storage.conversation_repository``).

The files of a message are saved with its turn, and files sent while the
request runs (guidance, answers to questions) are merged into the same turn,
each once. A follow-up lists the newest files and opens one by its id. These
tests use SQLite; tests/test_conversation_turn_attachments_postgres.py runs
the same scenarios on a real PostgreSQL server.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from storage.config import resolve_storage_settings
from storage.conversation_repository import (
    CONVERSATION_ATTACHMENT_LIST_LIMIT,
    TURN_ATTACHMENT_FIELDS,
    create_conversation_repository,
)
from tests.conversation_attachment_records import (
    OWNER,
    add_turn,
    attachment_record,
    ids,
    merge_from_threads,
    previous_release_turns_table,
)

LOGGER = logging.getLogger("test.turn_attachments")


def _open(db_path: Path):
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db_path)}})
    return create_conversation_repository(settings, logger=LOGGER, artifacts_dir=db_path.parent / "artifacts")


def _shown(record: dict) -> dict:
    return {key: record[key] for key in TURN_ATTACHMENT_FIELDS}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "task_history.db"


def test_a_turns_files_are_saved_with_it_and_read_back_after_reopening(db_path: Path) -> None:
    repo = _open(db_path)
    photo, notes = attachment_record("att_photo"), attachment_record("att_notes", kind="document")
    turn = add_turn(repo, "conv_1", "task_1", [photo, notes])
    before = repo.stored_text_bytes_by_owner()[OWNER]

    reopened = _open(db_path)
    assert ids(turn["attachments"]) == ["att_photo", "att_notes"]
    # Turn records show each file's name, type and size; the whole record
    # (with where the bytes are stored) comes only from the trusted lookups.
    shown = [_shown(photo), _shown(notes)]
    assert reopened.get_turn_by_task_id("task_1")["attachments"] == shown
    assert reopened.get_conversation_turns(conversation_id="conv_1")[0]["attachments"] == shown
    assert reopened.get_recent_turns("conv_1")[0]["attachments"] == shown
    assert {key for item in shown for key in item} == set(TURN_ATTACHMENT_FIELDS)
    assert reopened.find_conversation_attachment("conv_1", "att_photo")["storage_refs"] == photo["storage_refs"]
    assert reopened.list_conversation_attachments("conv_1")[0]["sha256"] == notes["sha256"]
    # A turn without files reads as an empty list.
    assert add_turn(reopened, "conv_1", "task_2")["attachments"] == []
    # The saved list counts toward the person's stored conversation text.
    add_turn(reopened, "conv_1", "task_3", [attachment_record("att_more")])
    assert reopened.stored_text_bytes_by_owner()[OWNER] > before


def test_merging_adds_only_new_files_in_order_and_a_resent_file_changes_nothing(db_path: Path) -> None:
    repo = _open(db_path)
    add_turn(repo, "conv_1", "task_1", [attachment_record("att_a")])

    merged = repo.merge_turn_attachments(
        conversation_id="conv_1", task_id="task_1", attachments=[attachment_record("att_b"), attachment_record("att_a")]
    )
    updated_at = repo.get_turn_by_task_id("task_1")["updated_at"]
    resent = repo.merge_turn_attachments(
        conversation_id="conv_1", task_id="task_1", attachments=[attachment_record("att_b", "renamed.png")]
    )

    assert ids(merged) == ["att_a", "att_b"]
    # The first copy of a file wins; nothing was written for the resend.
    assert resent == merged
    assert repo.get_turn_by_task_id("task_1")["attachments"][1]["filename"] == "att_b.png"
    assert repo.get_turn_by_task_id("task_1")["updated_at"] == updated_at
    # No such turn (or the turn is in another conversation): nothing is saved.
    assert repo.merge_turn_attachments(conversation_id="conv_1", task_id="task_x", attachments=[attachment_record("att_c")]) is None
    assert repo.merge_turn_attachments(conversation_id="conv_2", task_id="task_1", attachments=[attachment_record("att_c")]) is None
    with pytest.raises(ValueError, match="attachment_id"):
        repo.merge_turn_attachments(conversation_id="conv_1", task_id="task_1", attachments=[{"filename": "x.png"}])


def test_a_task_admitted_again_keeps_its_turn_and_merges_its_files(db_path: Path) -> None:
    repo = _open(db_path)
    first = add_turn(repo, "conv_1", "task_1", [attachment_record("att_a")])
    again = add_turn(repo, "conv_1", "task_1", [attachment_record("att_a"), attachment_record("att_b")])

    assert again["turn_index"] == first["turn_index"] == 1
    assert ids(again["attachments"]) == ids(repo.get_turn_by_task_id("task_1")["attachments"]) == ["att_a", "att_b"]
    assert len(repo.get_conversation_turns(conversation_id="conv_1")) == 1


def test_the_list_gives_the_newest_unique_files_with_their_turns_and_only_this_conversations(db_path: Path) -> None:
    repo = _open(db_path)
    # 25 files over five turns; turn 5 sends the turn-1 photo again.
    for turn in range(1, 6):
        files = [attachment_record(f"att_t{turn}_{index}") for index in range(5)]
        if turn == 5:
            files[0] = attachment_record("att_t1_0")
        add_turn(repo, "conv_1", f"task_{turn}", files)
    add_turn(repo, "conv_1", "task_6")  # no files
    add_turn(repo, "conv_other", "task_other", [attachment_record("att_elsewhere")])

    listed = repo.list_conversation_attachments("conv_1")

    assert len(listed) == CONVERSATION_ATTACHMENT_LIST_LIMIT == 20
    assert len(set(ids(listed))) == 20
    # Newest first: within a turn, later files arrived later.
    assert ids(listed)[:5] == ["att_t5_4", "att_t5_3", "att_t5_2", "att_t5_1", "att_t1_0"]
    assert (listed[4]["turn_index"], listed[4]["task_id"]) == (5, "task_5")
    assert "att_elsewhere" not in ids(listed)
    assert ids(repo.list_conversation_attachments("conv_1", limit=3)) == ["att_t5_4", "att_t5_3", "att_t5_2"]
    assert repo.list_conversation_attachments("conv_none") == []


def test_the_current_requests_files_do_not_take_the_places_of_earlier_files(db_path: Path) -> None:
    """A prompt lists its request's files with the request; the bound counts only the other turns' files."""

    repo = _open(db_path)
    for turn in range(1, 4):
        add_turn(repo, "conv_1", f"task_{turn}", [attachment_record(f"att_t{turn}_{index}") for index in range(10)])
    # The current request's turn: 20 files with its message and one a plan step received.
    add_turn(repo, "conv_1", "task_4", [attachment_record(f"att_t4_{index}") for index in range(21)])
    # Named with the request: its message's files and an earlier file the person sent again.
    named = {f"att_t4_{index}" for index in range(20)} | {"att_t2_9"}

    listed = repo.list_conversation_attachments("conv_1", current_task_id="task_4", skip_ids=named)

    others = [item for item in listed if item["task_id"] != "task_4"]
    assert ids(listed)[0] == "att_t4_20" and len(others) == CONVERSATION_ATTACHMENT_LIST_LIMIT
    assert ids(others) == [f"att_t3_{index}" for index in range(9, -1, -1)] + [
        f"att_t2_{index}" for index in range(8, -1, -1)
    ] + ["att_t1_9"]
    # The request's turn is listed whole even when newer turns fill the bound
    # (another request of the conversation started later).
    older = repo.list_conversation_attachments("conv_1", limit=3, current_task_id="task_1")
    assert ids(older) == ["att_t4_20", "att_t4_19", "att_t4_18"] + [f"att_t1_{index}" for index in range(9, -1, -1)]


def test_a_file_is_found_by_its_exact_id_in_this_conversation_only(db_path: Path) -> None:
    repo = _open(db_path)
    add_turn(repo, "conv_1", "task_1", [attachment_record("attX1"), attachment_record("att%2")])
    add_turn(repo, "conv_1", "task_2", [attachment_record("att_1")])
    add_turn(repo, "conv_2", "task_3", [attachment_record("att_other")])

    found = repo.find_conversation_attachment("conv_1", "att_1")

    assert (found["attachment_id"], found["turn_index"], found["task_id"]) == ("att_1", 2, "task_2")
    # LIKE wildcards in an id are matched literally.
    assert repo.find_conversation_attachment("conv_1", "att%2")["attachment_id"] == "att%2"
    assert repo.find_conversation_attachment("conv_1", "att%") is None
    assert repo.find_conversation_attachment("conv_1", "att_") is None
    assert repo.find_conversation_attachment("conv_1", "att") is None
    # Another conversation's file is not found here.
    assert repo.find_conversation_attachment("conv_1", "att_other") is None
    assert repo.find_conversation_attachment("conv_2", "att_other")["task_id"] == "task_3"
    assert repo.find_conversation_attachment("conv_1", "") is None


def test_an_unreadable_saved_list_reads_as_no_files_and_is_reported(db_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    repo = _open(db_path)
    add_turn(repo, "conv_1", "task_1", [attachment_record("att_a")])
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE conversation_task_links SET attachments_json = '{\"not\": \"a list\"}'")

    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        assert repo.get_turn_by_task_id("task_1")["attachments"] == []
        assert repo.list_conversation_attachments("conv_1") == []

    assert "event=conversation_turn_attachments_unreadable task_id=task_1" in caplog.text


def test_an_upgraded_store_adds_the_column_and_older_turns_have_no_files(db_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(previous_release_turns_table())
        conn.execute(
            "INSERT INTO conversation_task_links (conversation_id, task_id, turn_index, owner_canonical_user_id, "
            "problem_text) VALUES ('conv_old', 'task_old', 1, ?, 'Before the upgrade')",
            (OWNER,),
        )

    with caplog.at_level(logging.INFO, logger=LOGGER.name):
        repo = _open(db_path)

    assert "event=storage_column_added backend=SQLite table=conversation_task_links column=attachments_json" in caplog.text
    assert repo.get_turn_by_task_id("task_old")["attachments"] == []
    assert repo.list_conversation_attachments("conv_old") == []
    assert ids(
        repo.merge_turn_attachments(conversation_id="conv_old", task_id="task_old", attachments=[attachment_record("att_new")])
    ) == ["att_new"]


def test_files_added_to_one_turn_at_once_from_several_stores_are_all_kept_once(db_path: Path) -> None:
    add_turn(_open(db_path), "conv_1", "task_1", [attachment_record("att_first")])

    merge_from_threads(lambda: _open(db_path), "conv_1", "task_1", writers=6, files_each=5)

    saved = ids(_open(db_path).get_turn_by_task_id("task_1")["attachments"])
    expected = {"att_first", "att_shared"} | {f"att_w{writer}_{index}" for writer in range(6) for index in range(5)}
    assert len(saved) == len(set(saved)) == len(expected)
    assert set(saved) == expected
    assert saved[0] == "att_first"


class _MergingStore:
    """A store that only counts merges; ``wait`` holds every merge until it is set."""

    def __init__(self, wait: threading.Event | None = None) -> None:
        self.wait = wait

    def merge_turn_attachments(self, **_: Any) -> list[dict[str, Any]]:
        if self.wait is not None:
            self.wait.wait(10)
        return []


def test_the_concurrent_merge_helper_fails_at_once_when_a_store_does_not_open() -> None:
    opened: list[int] = []
    lock = threading.Lock()

    def open_repository() -> _MergingStore:
        with lock:
            opened.append(len(opened))
            first = len(opened) == 1
        if first:
            raise RuntimeError("database is locked while opening the store")
        return _MergingStore()

    started = time.monotonic()
    with pytest.raises(AssertionError, match="database is locked"):
        merge_from_threads(open_repository, "conv_1", "task_1", writers=3, files_each=1)
    # The other writers stopped instead of waiting for the one that failed.
    assert time.monotonic() - started < 10


def test_the_concurrent_merge_helper_fails_when_a_writer_never_finishes() -> None:
    release = threading.Event()
    try:
        with pytest.raises(AssertionError, match="still running"):
            merge_from_threads(lambda: _MergingStore(release), "conv_1", "task_1", writers=2, files_each=1, join_timeout=0.5)
    finally:
        release.set()
