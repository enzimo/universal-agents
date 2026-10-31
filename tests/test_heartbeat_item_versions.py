"""Two changes to one heartbeat item never lose one of them (2026-10-20).

Real heartbeat stores on SQLite and PostgreSQL (``LIVE_TEST_POSTGRES_DSN``; the
PostgreSQL store runs in its own schema) and the real heartbeat tools. Before
this change an update read the item on one connection and then wrote title,
instruction, target agent type and Objective from that read on another, so an
update made in between was lost. Now each change runs in one write transaction
on the stored row, and an agent's change names the version it read and is
refused with ``changed_since_read`` when the item changed since.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from uuid import uuid4

import pytest

from custom_agents.heartbeat_store import HeartbeatStore
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.enums import ChangeRefusal
from custom_tools.providers.heartbeat import (
    disable_heartbeat_item,
    enable_heartbeat_item,
    list_heartbeat_items,
    remove_heartbeat_item,
    update_heartbeat_item,
)
from tests.task_work_support import set_agent_ref
from storage.change_ledger import ChangeRefused
from storage.config import resolve_storage_settings
from storage.heartbeat_repository import (
    HEARTBEAT_ITEM_BOOKKEEPING_FIELDS,
    HEARTBEAT_ITEM_VERSION_FIELDS,
    HeartbeatItemRecord,
    heartbeat_item_version,
)
from tests.test_heartbeat_tools import _FakeAgent

SOURCE = "hb_source_test"


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres]),
    ]
)
def settings(tmp_path, request):
    if request.param == "sqlite":
        yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "task_history.db")}})
        return
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set LIVE_TEST_POSTGRES_DSN to a disposable database to run the PostgreSQL heartbeat tests.")
    schema = "heartbeat_versions_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def store(settings) -> HeartbeatStore:
    # In PostgreSQL this also checks that the store starts in a schema other
    # than public (it used to look for its columns only in public).
    heartbeat = HeartbeatStore(storage_settings=settings)
    heartbeat.ensure_source(source_id=SOURCE)
    return heartbeat


def _item(store: HeartbeatStore) -> HeartbeatItemRecord:
    return store.add_item(source_id=SOURCE, title="Old title", instruction="Old instruction", conversation_id="c1")


# -- the store -------------------------------------------------------------------


def test_every_heartbeat_item_field_is_either_versioned_or_bookkeeping() -> None:
    versioned, bookkeeping = set(HEARTBEAT_ITEM_VERSION_FIELDS), set(HEARTBEAT_ITEM_BOOKKEEPING_FIELDS)
    assert versioned.isdisjoint(bookkeeping)
    assert versioned | bookkeeping == {field.name for field in fields(HeartbeatItemRecord)}


def test_two_updates_at_the_same_time_keep_both_fields(store, monkeypatch) -> None:
    """The reported case: two updates of different fields at once.

    The first update stops inside its write transaction, after reading the
    item; the second starts meanwhile. It waits for the first to commit, then
    reads the row the first wrote, so neither field is lost. Before, each
    update read the item outside its write and wrote all four fields from
    that read, so the second update's instruction went back to the old one.
    """

    item = _item(store)
    repository = store._repository
    real_locked_item = repository._locked_item
    first_has_read, second_starts = threading.Event(), threading.Event()
    reads: list[str] = []

    def read_then_hold(conn, item_id):
        stored = real_locked_item(conn, item_id)
        reads.append(item_id)
        if len(reads) == 1:
            first_has_read.set()
            assert second_starts.wait(5)
            time.sleep(0.3)  # the second update now waits for this transaction
        return stored

    monkeypatch.setattr(repository, "_locked_item", read_then_hold)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.update_item, item.heartbeat_item_id, title="New title")
        assert first_has_read.wait(5)
        second_starts.set()
        second = pool.submit(store.update_item, item.heartbeat_item_id, instruction="New instruction")
        first.result(timeout=10)
        second.result(timeout=10)

    stored = store.get_item(item.heartbeat_item_id)
    assert (stored.title, stored.instruction) == ("New title", "New instruction")


def test_an_update_from_an_older_read_is_refused_and_changes_nothing(store) -> None:
    item = _item(store)
    read_by_both = heartbeat_item_version(item)
    store.update_item(item.heartbeat_item_id, title="New title", expected_version=read_by_both)

    for change in (
        lambda: store.update_item(item.heartbeat_item_id, instruction="Stale", expected_version=read_by_both),
        lambda: store.set_item_enabled(item.heartbeat_item_id, False, expected_version=read_by_both),
        lambda: store.remove_item(item.heartbeat_item_id, expected_version=read_by_both),
    ):
        with pytest.raises(ChangeRefused) as refused:
            change()
        assert refused.value.reason is ChangeRefusal.CHANGED_SINCE_READ
        assert refused.value.details == {"heartbeat_item_id": item.heartbeat_item_id}

    stored = store.get_item(item.heartbeat_item_id)
    assert (stored.title, stored.instruction, stored.enabled) == ("New title", "Old instruction", True)
    # Read again, the change goes through and keeps the other one.
    redone = store.update_item(
        item.heartbeat_item_id, instruction="New instruction", expected_version=heartbeat_item_version(stored)
    )
    assert (redone.title, redone.instruction) == ("New title", "New instruction")


def test_runs_conversation_resets_and_switching_leave_the_version_valid(store) -> None:
    """These write the same row, so a version covering them would refuse every edit while items run."""

    item = _item(store)
    read = heartbeat_item_version(item)
    store.mark_item_run_started(item.heartbeat_item_id)
    store.mark_item_run_finished(item.heartbeat_item_id, success=True)
    assert store.start_new_item_conversation(item.heartbeat_item_id)
    # An Objective's pause switches its linked items without a version (trusted).
    assert store.set_item_enabled(item.heartbeat_item_id, False) is not None

    after = store.get_item(item.heartbeat_item_id)
    assert after.conversation_id != item.conversation_id and after.last_success_at
    assert heartbeat_item_version(after) == read
    updated = store.update_item(item.heartbeat_item_id, title="New title", expected_version=read)
    assert (updated.title, updated.enabled, updated.conversation_id) == ("New title", False, after.conversation_id)


def test_a_removed_item_stays_removed(store) -> None:
    item = _item(store)
    read = heartbeat_item_version(item)
    assert store.remove_item(item.heartbeat_item_id, expected_version=read)

    assert store.update_item(item.heartbeat_item_id, title="Late", expected_version=read) is None
    assert store.set_item_enabled(item.heartbeat_item_id, True, expected_version=read) is None
    assert store.remove_item(item.heartbeat_item_id, expected_version=read) is False
    assert store.get_item(item.heartbeat_item_id) is None


# -- the tools -----------------------------------------------------------------------


@pytest.fixture
def agent(settings, store, tmp_path):
    meta = MessageMeta(gateway_context=GatewayContext(heartbeat_source_id=SOURCE, session_key="heartbeat"))
    set_agent_ref(_FakeAgent(str(tmp_path / "unused.db"), meta, storage_settings=settings))


def _listed(store: HeartbeatStore) -> dict:
    [item] = json.loads(list_heartbeat_items())["items"]
    return item


def test_the_tools_refuse_a_change_from_an_older_read(store, agent) -> None:
    """Two of a person's requests list the item; one changes the instruction, the other is stale."""

    item = _item(store)
    read_by_both = _listed(store)["version"]
    changed = json.loads(update_heartbeat_item(item.heartbeat_item_id, read_by_both, instruction="Check the stove"))
    assert changed["version"] != read_by_both

    for refused in (
        update_heartbeat_item(item.heartbeat_item_id, read_by_both, title="Oven"),
        disable_heartbeat_item(item.heartbeat_item_id, read_by_both),
        enable_heartbeat_item(item.heartbeat_item_id, read_by_both),
        remove_heartbeat_item(item.heartbeat_item_id, read_by_both),
    ):
        assert refused["status"] == "error"
        payload = json.loads(refused["content"][0]["text"])
        assert payload["error_code"] == "changed_since_read"
        assert payload["heartbeat_item_id"] == item.heartbeat_item_id
        assert "changed since you read it" in payload["error"]

    stored = store.get_item(item.heartbeat_item_id)
    assert (stored.title, stored.instruction, stored.enabled) == ("Old title", "Check the stove", True)
    disabled = json.loads(disable_heartbeat_item(item.heartbeat_item_id, _listed(store)["version"]))
    assert disabled["enabled"] is False


def test_the_tools_need_the_version(store, agent) -> None:
    item = _item(store)
    for missing in (
        update_heartbeat_item(item.heartbeat_item_id, "", title="x"),
        disable_heartbeat_item(item.heartbeat_item_id, " "),
        enable_heartbeat_item(item.heartbeat_item_id, ""),
        remove_heartbeat_item(item.heartbeat_item_id, ""),
    ):
        payload = json.loads(missing["content"][0]["text"])
        assert payload["error_code"] == "invalid_input"
        assert "expected_version is required" in payload["error"]
    assert store.get_item(item.heartbeat_item_id).title == "Old title"


def test_a_run_between_listing_and_changing_does_not_refuse_the_change(store, agent) -> None:
    item = _item(store)
    read = _listed(store)["version"]
    store.mark_item_run_started(item.heartbeat_item_id)
    store.start_new_item_conversation(item.heartbeat_item_id)

    updated = json.loads(update_heartbeat_item(item.heartbeat_item_id, read, title="New title"))

    assert (updated["title"], updated["version"]) == ("New title", _listed(store)["version"])
