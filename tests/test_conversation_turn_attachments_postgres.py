"""Each turn's saved files on a real PostgreSQL server (``storage.conversation_repository``).

The same scenarios as tests/test_conversation_turn_attachments.py through the
Postgres dialect: saving and reading back, merging without repeats, the
bounded newest-first list, the exact id lookup (``LIKE ... ESCAPE``), the
column an upgraded store adds, and files added to one turn from several
stores at once, which take turns on the store's advisory write lock. Needs
``LIVE_TEST_POSTGRES_DSN``; each test gets its own schema.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator
from uuid import uuid4

import pytest

from storage.config import StorageSettings, resolve_storage_settings
from storage.conversation_repository import create_conversation_repository
from tests.conversation_attachment_records import (
    OWNER,
    add_turn,
    attachment_record,
    ids,
    merge_from_threads,
    previous_release_turns_table,
)

pytestmark = [pytest.mark.live, pytest.mark.postgres, pytest.mark.integration]

LOGGER = logging.getLogger("test.turn_attachments_postgres")


@contextmanager
def _postgres_storage() -> Iterator[StorageSettings]:
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL turn attachment tests")
    schema = "turn_attachments_test_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.commit()
    try:
        yield resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()


@pytest.fixture
def storage() -> Iterator[StorageSettings]:
    with _postgres_storage() as created:
        yield created


def _open(storage: StorageSettings, tmp_path):
    return create_conversation_repository(storage, logger=LOGGER, artifacts_dir=tmp_path / "artifacts")


def test_files_are_saved_merged_once_and_read_back(storage: StorageSettings, tmp_path) -> None:
    repo = _open(storage, tmp_path)
    add_turn(repo, "conv_1", "task_1", [attachment_record("att_a")])

    merged = repo.merge_turn_attachments(
        conversation_id="conv_1", task_id="task_1", attachments=[attachment_record("att_b"), attachment_record("att_a")]
    )
    resent = repo.merge_turn_attachments(conversation_id="conv_1", task_id="task_1", attachments=[attachment_record("att_b")])
    again = add_turn(repo, "conv_1", "task_1", [attachment_record("att_c")])

    assert ids(merged) == ids(resent) == ["att_a", "att_b"]
    assert again["turn_index"] == 1
    assert ids(_open(storage, tmp_path).get_turn_by_task_id("task_1")["attachments"]) == ["att_a", "att_b", "att_c"]
    assert repo.merge_turn_attachments(conversation_id="conv_1", task_id="task_x", attachments=[attachment_record("att_d")]) is None
    assert repo.stored_text_bytes_by_owner()[OWNER] > 0


def test_the_list_is_newest_first_and_bounded_and_ids_match_exactly(storage: StorageSettings, tmp_path) -> None:
    repo = _open(storage, tmp_path)
    for turn in range(1, 6):
        add_turn(repo, "conv_1", f"task_{turn}", [attachment_record(f"att_t{turn}_{index}") for index in range(5)])
    add_turn(repo, "conv_1", "task_6", [attachment_record("attX1"), attachment_record("att%2")])
    add_turn(repo, "conv_2", "task_7", [attachment_record("att_1")])

    listed = repo.list_conversation_attachments("conv_1")

    assert len(listed) == len(set(ids(listed))) == 20
    assert ids(listed)[:3] == ["att%2", "attX1", "att_t5_4"]
    # The current request's turn is listed whole and its named files are left out, outside the bound.
    assert ids(repo.list_conversation_attachments("conv_1", limit=2, current_task_id="task_1", skip_ids={"att%2"})) == [
        "attX1",
        "att_t5_4",
        *[f"att_t1_{index}" for index in range(4, -1, -1)],
    ]
    assert repo.find_conversation_attachment("conv_1", "att%2")["task_id"] == "task_6"
    assert repo.find_conversation_attachment("conv_1", "att_1") is None  # "attX1" is not a match; conv_2's is elsewhere
    assert repo.find_conversation_attachment("conv_1", "att%") is None
    assert repo.find_conversation_attachment("conv_2", "att_1")["turn_index"] == 1


def test_an_upgraded_store_adds_the_column_and_older_turns_have_no_files(storage: StorageSettings, tmp_path) -> None:
    from storage.postgres_connection import connect_postgres

    with connect_postgres(storage.postgres_dsn or "") as conn:
        conn.execute(previous_release_turns_table())
        conn.execute(
            "INSERT INTO conversation_task_links (conversation_id, task_id, turn_index, owner_canonical_user_id, "
            "problem_text) VALUES ('conv_old', 'task_old', 1, %s, 'Before the upgrade')",
            (OWNER,),
        )
        conn.commit()

    repo = _open(storage, tmp_path)

    assert repo.get_turn_by_task_id("task_old")["attachments"] == []
    assert ids(
        repo.merge_turn_attachments(conversation_id="conv_old", task_id="task_old", attachments=[attachment_record("att_new")])
    ) == ["att_new"]


def test_files_added_to_one_turn_at_once_from_several_stores_are_all_kept_once(storage: StorageSettings, tmp_path) -> None:
    add_turn(_open(storage, tmp_path), "conv_1", "task_1", [attachment_record("att_first")])

    merge_from_threads(lambda: _open(storage, tmp_path), "conv_1", "task_1", writers=6, files_each=5)

    saved = ids(_open(storage, tmp_path).get_turn_by_task_id("task_1")["attachments"])
    expected = {"att_first", "att_shared"} | {f"att_w{writer}_{index}" for writer in range(6) for index in range(5)}
    assert len(saved) == len(set(saved)) == len(expected)
    assert set(saved) == expected
