"""Memory edits act only on the fact version they were based on (real SQLite and PostgreSQL).

Several requests can change the same team or personal memory, and a request's
change can arrive long after it read the fact (a model step, a slow tool call, a
restarted request). These tests check, against the real repository and service:

- an agent's replace or delete needs the record digest of the fact version it
  read, and a digest from an older version is refused (``changed_since_read``);
- the digest names the fact itself, so a delayed change never reaches another
  fact that was later given the same number;
- undo reverses an event only while the fact is still as that event left it;
- simultaneous creates each get their own number.

PostgreSQL runs need ``LIVE_TEST_POSTGRES_DSN``; each test gets its own schema.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest

from custom_agents.pinned_memory_runtime import (
    PinnedMemoryActor,
    PinnedMemoryCommand,
    PinnedMemoryRequestContext,
    PinnedMemoryService,
)
from storage.config import StorageSettings, resolve_storage_settings
from storage.pinned_memory_repository import (
    COREMEM_BANK,
    PinnedMemoryConflictError,
    create_pinned_memory_repository,
    pinned_memory_record_digest_values,
)

BACKENDS = [
    "sqlite",
    pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres, pytest.mark.integration]),
]
BANKS = ["coremem", "usermem"]
TEAM = "team_memory"
PERSON = "person_a"
AGENT_TOOL = "agent_tool"


@contextmanager
def _storage(backend: str, tmp_path: Path) -> Iterator[StorageSettings]:
    if backend == "sqlite":
        yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "memory.db")}})
        return
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set LIVE_TEST_POSTGRES_DSN to a disposable database to run the PostgreSQL memory tests.")
    schema = "pinned_memory_test_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(params=BACKENDS)
def storage(request, tmp_path) -> Iterator[StorageSettings]:
    with _storage(request.param, tmp_path) as settings:
        yield settings


def _service(storage: StorageSettings, *, origin: str = AGENT_TOOL) -> PinnedMemoryService:
    """A fresh service and repository, as a new process (or a restarted request) opens them."""

    return PinnedMemoryService(create_pinned_memory_repository(storage), command_origin=origin)


def _context() -> PinnedMemoryRequestContext:
    return PinnedMemoryRequestContext(
        team_id=TEAM, canonical_user_id=PERSON, actor=PinnedMemoryActor(kind="agent", id="agent_lead")
    )


def _run(service: PinnedMemoryService, bank: str, action: str, **fields: Any) -> dict[str, Any]:
    return service.execute(PinnedMemoryCommand(bank=bank, action=action, **fields), context=_context())


def _texts(service: PinnedMemoryService, bank: str) -> dict[int, str]:
    return {fact["memory_id"]: fact["fact_text"] for fact in _run(service, bank, "list")["facts"]}


def _change(service: PinnedMemoryService, bank: str, action: str, fact: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Replace or delete ``fact`` with the digest of the version given (what the request read)."""

    if action == "replace":
        fields.setdefault("fact_text", "A delayed replacement.")
    return _run(
        service, bank, action, memory_id=fact["memory_id"], expected_record_digest=fact["record_digest"], **fields
    )


# -- a replace or delete needs the version the agent read ------------------------------


@pytest.mark.parametrize("bank", BANKS)
@pytest.mark.parametrize("action", ["replace", "delete"])
def test_an_agents_change_without_the_version_it_read_is_refused(storage, bank, action) -> None:
    service = _service(storage)
    _run(service, bank, "create", fact_text="Prefers trains.")

    fields = {"fact_text": "Prefers planes."} if action == "replace" else {}
    with pytest.raises(ValueError, match="record_digest"):
        _run(service, bank, action, memory_id=1, **fields)

    assert _texts(service, bank) == {1: "Prefers trains."}


@pytest.mark.parametrize("bank", BANKS)
@pytest.mark.parametrize("action", ["replace", "delete"])
def test_a_change_based_on_an_older_version_is_refused_after_a_restart(storage, bank, action) -> None:
    first_request = _service(storage)
    read = _run(first_request, bank, "create", fact_text="Prefers trains.")["fact"]
    other_request = _service(storage)
    _change(other_request, bank, "replace", read, fact_text="Prefers night trains.")

    restarted = _service(storage)
    with pytest.raises(PinnedMemoryConflictError, match="changed after it was loaded"):
        _change(restarted, bank, action, read)

    assert _texts(restarted, bank) == {1: "Prefers night trains."}


# -- a reused number never redirects a delayed change -----------------------------------


@pytest.mark.parametrize("bank", BANKS)
@pytest.mark.parametrize("action", ["replace", "delete", "undo_create"])
def test_a_delayed_change_never_reaches_the_fact_that_took_over_its_number(storage, bank, action) -> None:
    first_request = _service(storage)
    created = _run(first_request, bank, "create", fact_text="Prefers trains.")
    read = created["fact"]
    # Another request deletes the fact and saves an unrelated one, which gets number 1 again.
    other_request = _service(storage)
    _change(other_request, bank, "delete", read)
    unrelated = _run(other_request, bank, "create", fact_text="Lives in Lisbon.")["fact"]
    assert unrelated["memory_id"] == read["memory_id"] == 1

    restarted = _service(storage)
    with pytest.raises(PinnedMemoryConflictError):
        if action == "undo_create":
            _run(restarted, bank, "undo", event_id=created["event_id"])
        else:
            _change(restarted, bank, action, read)

    assert _texts(restarted, bank) == {1: "Lives in Lisbon."}


@pytest.mark.parametrize("bank", BANKS)
def test_undoing_a_delete_is_refused_while_its_number_holds_another_fact(storage, bank) -> None:
    service = _service(storage)
    read = _run(service, bank, "create", fact_text="Prefers trains.")["fact"]
    deleted = _change(service, bank, "delete", read)
    _run(service, bank, "create", fact_text="Lives in Lisbon.")

    with pytest.raises(PinnedMemoryConflictError, match="already in use"):
        _run(_service(storage), bank, "undo", event_id=deleted["event_id"])

    assert _texts(service, bank) == {1: "Lives in Lisbon."}


# -- undo reverses only the current state ---------------------------------------------------


@pytest.mark.parametrize("bank", BANKS)
def test_undoing_an_older_replace_is_refused_after_a_later_replace(storage, bank) -> None:
    service = _service(storage)
    fact_a = _run(service, bank, "create", fact_text="A")["fact"]
    a_to_b = _change(service, bank, "replace", fact_a, fact_text="B")
    b_to_c = _change(service, bank, "replace", a_to_b["fact"], fact_text="C")

    with pytest.raises(PinnedMemoryConflictError, match="changed after"):
        _run(_service(storage), bank, "undo", event_id=a_to_b["event_id"])
    assert _texts(service, bank) == {1: "C"}

    # The latest change can still be undone.
    _run(service, bank, "undo", event_id=b_to_c["event_id"])
    assert _texts(service, bank) == {1: "B"}


@pytest.mark.parametrize("bank", BANKS)
def test_changes_can_be_undone_one_after_another_latest_first(storage, bank) -> None:
    service = _service(storage)
    created = _run(service, bank, "create", fact_text="A")
    a_to_b = _change(service, bank, "replace", created["fact"], fact_text="B")
    b_to_c = _change(service, bank, "replace", a_to_b["fact"], fact_text="C")
    deleted = _change(service, bank, "delete", b_to_c["fact"])

    for event, after in ((deleted, {1: "C"}), (b_to_c, {1: "B"}), (a_to_b, {1: "A"}), (created, {})):
        _run(service, bank, "undo", event_id=event["event_id"])
        assert _texts(service, bank) == after


@pytest.mark.parametrize("bank", BANKS)
def test_undoing_a_create_is_refused_after_a_later_edit(storage, bank) -> None:
    service = _service(storage)
    created = _run(service, bank, "create", fact_text="A")
    _change(service, bank, "replace", created["fact"], fact_text="B")

    with pytest.raises(PinnedMemoryConflictError, match="changed after"):
        _run(_service(storage), bank, "undo", event_id=created["event_id"])

    assert _texts(service, bank) == {1: "B"}


@pytest.mark.parametrize("bank", BANKS)
def test_undoing_a_delete_is_refused_after_the_fact_came_back_and_changed(storage, bank) -> None:
    service = _service(storage)
    fact_b = _change(service, bank, "replace", _run(service, bank, "create", fact_text="A")["fact"], fact_text="B")
    first_delete = _change(service, bank, "delete", fact_b["fact"])
    restored = _run(service, bank, "undo", event_id=first_delete["event_id"])
    fact_c = _change(service, bank, "replace", restored["fact"], fact_text="C")
    second_delete = _change(service, bank, "delete", fact_c["fact"])

    # Undoing the first delete would bring back B and lose C.
    with pytest.raises(PinnedMemoryConflictError, match="changed after"):
        _run(_service(storage), bank, "undo", event_id=first_delete["event_id"])
    assert _texts(service, bank) == {}

    _run(service, bank, "undo", event_id=second_delete["event_id"])
    assert _texts(service, bank) == {1: "C"}


@pytest.mark.parametrize("bank", BANKS)
def test_an_event_is_undone_only_once(storage, bank) -> None:
    service = _service(storage)
    created = _run(service, bank, "create", fact_text="A")
    replaced = _change(service, bank, "replace", created["fact"], fact_text="B")
    _run(service, bank, "undo", event_id=replaced["event_id"])

    with pytest.raises(PinnedMemoryConflictError):
        _run(service, bank, "undo", event_id=replaced["event_id"])
    assert _texts(service, bank) == {1: "A"}


# -- a change reports its own event ---------------------------------------------------------------


def _right_after_the_write_commits(monkeypatch, repository: Any, action: str, then: Any) -> None:
    """Run ``then`` (another request's change) as soon as the transaction that recorded ``action`` commits."""

    insert_event = repository._insert_event
    state = {"done": False}

    def insert_event_and_wait_for_its_commit(conn: Any, **fields: Any) -> str:
        event_id = insert_event(conn, **fields)
        if fields["event_type"] == action and not state["done"]:
            commit = conn.commit

            def commit_then_other_request() -> None:
                commit()
                if not state["done"]:
                    state["done"] = True
                    then()

            conn.commit = commit_then_other_request
        return event_id

    monkeypatch.setattr(repository, "_insert_event", insert_event_and_wait_for_its_commit)


@pytest.mark.parametrize("bank", BANKS)
@pytest.mark.parametrize("action", ["create", "replace", "delete"])
def test_a_change_returns_its_own_event_and_fact_when_another_change_follows_at_once(
    storage, monkeypatch, bank, action
) -> None:
    writer = _service(storage)
    # Another request changes the same number the moment the writer's change is saved.
    other = _service(storage, origin="gateway")
    read = None if action == "create" else _run(writer, bank, "create", fact_text="A")["fact"]

    def other_request() -> None:
        if action == "create":
            _run(other, bank, "delete", memory_id=1)
            _run(other, bank, "create", fact_text="Unrelated")
        elif action == "replace":
            _run(other, bank, "replace", memory_id=1, fact_text="C")
        else:
            _run(other, bank, "create", fact_text="Unrelated")
            _run(other, bank, "delete", memory_id=1)

    _right_after_the_write_commits(monkeypatch, writer.repository, action, other_request)
    if action == "create":
        result = _run(writer, bank, "create", fact_text="B")
    else:
        result = _change(writer, bank, action, read, **({"fact_text": "B"} if action == "replace" else {}))

    event = create_pinned_memory_repository(storage).get_event(result["event_id"])
    assert event is not None and event.event_type == action
    if action == "delete":
        assert json.loads(event.before_json)["fact_text"] == "A"
    else:
        # The event and the fact (with its record digest) are the writer's own.
        assert json.loads(event.after_json)["fact_text"] == "B"
        assert result["fact"]["fact_text"] == "B"
        assert result["fact"]["record_digest"] == pinned_memory_record_digest_values(json.loads(event.after_json))

    # So an undo of the reported event reverses the writer's change, never the other request's.
    restarted = _service(storage)
    if action == "delete":
        _run(restarted, bank, "undo", event_id=result["event_id"])
        assert _texts(restarted, bank) == {1: "A"}
    else:
        with pytest.raises(PinnedMemoryConflictError):
            _run(restarted, bank, "undo", event_id=result["event_id"])
        assert _texts(restarted, bank) == {1: "Unrelated" if action == "create" else "C"}


# -- simultaneous creates ---------------------------------------------------------------------


def test_two_creates_at_the_same_moment_both_succeed_with_distinct_numbers(storage, monkeypatch) -> None:
    # Each create pauses just before it saves its row, for up to a second, so
    # the other create can reach the same point. Without a write lock both
    # have read the free numbers by then and pick the same one; with it, the
    # second create waits for the first and its pause ends at once.
    meeting = threading.Barrier(2, timeout=1.0)
    repositories = [create_pinned_memory_repository(storage) for _ in range(2)]
    for repository in repositories:
        insert = repository._insert_active

        def insert_after_meeting(conn: Any, row: dict[str, Any], _insert: Any = insert) -> None:
            try:
                meeting.wait()
            except threading.BrokenBarrierError:
                pass
            _insert(conn, row)

        monkeypatch.setattr(repository, "_insert_active", insert_after_meeting)

    def create(index: int) -> int:
        row = repositories[index].create_coremem(team_id=TEAM, fact_text=f"Fact {index}", capacity=10)
        return row.fact.memory_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        numbers = list(pool.map(create, range(2)))

    assert sorted(numbers) == [1, 2]
    assert [row.memory_id for row in create_pinned_memory_repository(storage).list_coremem(team_id=TEAM)] == [1, 2]


def test_a_person_s_own_command_still_changes_the_fact_by_number(storage) -> None:
    # People's slash commands name the fact by its number as they see it now;
    # only agent changes must carry the version they read.
    person = _service(storage, origin="gateway")
    _run(person, COREMEM_BANK, "create", fact_text="Ships on Fridays.")

    _run(person, COREMEM_BANK, "replace", memory_id=1, fact_text="Ships on Thursdays.")
    assert _texts(person, COREMEM_BANK) == {1: "Ships on Thursdays."}
    _run(person, COREMEM_BANK, "delete", memory_id=1)
    assert _texts(person, COREMEM_BANK) == {}
