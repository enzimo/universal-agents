"""Separate requests and memory facts: the change ledger's review rule (coordination plan 7.10, phase R1b).

Real memory stores (SQLite, and PostgreSQL when ``LIVE_TEST_POSTGRES_DSN`` is
set) with the real change ledger and request rows. A fact's fields are
fact_text, tags, metadata, valid_from_utc and invalid_after_utc
(``storage.pinned_memory_changes``). Each test asserts what was saved.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from custom_messages.enums import ChangeRefusal
from storage.change_ledger import ChangeRefused, ChangeRequestStore, ChangeWriter, ChangeWriterKind, RequestState
from storage.config import StorageSettings
from storage.pinned_memory_changes import MemoryWriter
from storage.pinned_memory_repository import (
    PinnedMemoryConflictError,
    PinnedMemoryRepository,
    create_pinned_memory_repository,
    pinned_memory_record_digest,
)
from storage.postgres_connection import connect_postgres
from storage.sqlite_connection import connect_sqlite
from tests.knowledge_support import BACKENDS, TEAM, knowledge_storage

ALICE, BOB = "alice", "bob"
NOW = datetime.now(timezone.utc)


def request(request_id: str, person: str = ALICE, *, checked: bool = True) -> MemoryWriter:
    return MemoryWriter(TEAM, ChangeWriter(ChangeWriterKind.REQUEST, request_id=request_id, person_id=person, checked=checked))


def by(kind: ChangeWriterKind, person: str = ALICE) -> MemoryWriter:
    return MemoryWriter(TEAM, ChangeWriter(kind, person_id=person))


@pytest.fixture(params=BACKENDS)
def storage(request, tmp_path):
    with knowledge_storage(request.param, tmp_path) as settings:
        yield settings


@pytest.fixture
def store(storage: StorageSettings) -> PinnedMemoryRepository:
    return create_pinned_memory_repository(storage)


@pytest.fixture
def requests(storage: StorageSettings) -> ChangeRequestStore:
    return ChangeRequestStore(storage)


def open_requests(requests: ChangeRequestStore, *request_ids: str, person: str = ALICE) -> None:
    for request_id in request_ids:
        requests.open(team_id=TEAM, request_id=request_id, person_id=person, now=NOW)


def usermem(store: PinnedMemoryRepository, writer: MemoryWriter, **fields) -> int:
    return store.create_usermem(
        canonical_user_id=ALICE, fact_text="Trip on Tuesday.", capacity=10, writer=writer, **fields
    ).fact.memory_id


def fact(store: PinnedMemoryRepository, memory_id: int = 1, *, bank: str = "usermem"):
    rows = store.list_usermem(canonical_user_id=ALICE) if bank == "usermem" else store.list_coremem(team_id=TEAM)
    return next(row for row in rows if row.memory_id == memory_id)


def replace(store: PinnedMemoryRepository, writer: MemoryWriter, memory_id: int = 1, *, bank: str = "usermem", **fields):
    digest = pinned_memory_record_digest(fact(store, memory_id, bank=bank))
    if bank == "usermem":
        return store.replace_usermem(
            canonical_user_id=ALICE, memory_id=memory_id, writer=writer, expected_record_digest=digest, **fields
        )
    return store.replace_coremem(team_id=TEAM, memory_id=memory_id, writer=writer, expected_record_digest=digest, **fields)


def refused(action, *args, **kwargs) -> ChangeRefused:
    with pytest.raises(ChangeRefused) as error:
        action(*args, **kwargs)
    return error.value


def _connect(settings: StorageSettings):
    if settings.backend == "sqlite":
        return connect_sqlite(str(settings.sqlite_path))
    return connect_postgres(str(settings.postgres_dsn))


def ledger(settings: StorageSettings) -> list[tuple[str, str, str, list[str]]]:
    with _connect(settings) as conn:
        rows = conn.execute(
            "SELECT record_key, change_type, writer_kind, changed_fields FROM change_ledger "
            "WHERE record_kind = 'memory_fact' ORDER BY position"
        ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2]), json.loads(str(r[3]))) for r in rows]


def count(settings: StorageSettings, table: str) -> int:
    with _connect(settings) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_a_request_is_shown_another_requests_change_once_then_saves_only_other_fields(storage, store, requests):
    usermem(store, by(ChangeWriterKind.PERSON))
    open_requests(requests, "req_a", "req_b")
    replace(store, request("req_b"), fact_text="Trip on Wednesday.")

    conflict = refused(replace, store, request("req_a"), tags=["travel"])
    assert conflict.reason is ChangeRefusal.CHANGE_CONFLICT
    brief = conflict.details["brief"]
    assert brief["record"] == "usermem #1" and brief["case_id"] == conflict.details["case_id"]
    assert [(change["by"], change["fields"]) for change in brief["changes"]] == [("another of your requests", ["fact_text"])]
    assert brief["now"] == {"fact_text": "Trip on Wednesday."}
    assert brief["you_planned"] == {"tags": '["travel"]'}
    # Refused: nothing changed, and what the request was shown is saved.
    assert fact(store).tags == [] and count(storage, "change_reviews") == 1

    # Seen: a field the other change did not set goes through; the one it set needs the person.
    replace(store, request("req_a"), tags=["travel"])
    assert fact(store).tags == ["travel"]
    overlap = refused(replace, store, request("req_a"), fact_text="Trip on Tuesday after all.")
    assert overlap.reason is ChangeRefusal.NEEDS_PERSON_DECISION
    assert fact(store).fact_text == "Trip on Wednesday."


def test_each_field_counts_on_its_own_and_unchanged_values_set_nothing(storage, store, requests):
    usermem(store, by(ChangeWriterKind.PERSON), metadata={"source": "chat"})
    open_requests(requests, "req_a", "req_b")
    replace(store, request("req_b"), invalid_after_utc="2030-01-01T00:00:00+00:00", metadata={"source": "chat"})

    conflict = refused(replace, store, request("req_a"), fact_text="Trip on Tuesday.", valid_from_utc="2026-01-01T00:00:00+00:00")
    # Re-sending the metadata it already had set nothing; only the expiry changed.
    assert conflict.details["brief"]["changes"][0]["fields"] == ["invalid_after_utc"]
    replace(store, request("req_a"), valid_from_utc="2026-01-01T00:00:00+00:00")
    # Clearing the expiry sets the field the other request set.
    assert refused(replace, store, request("req_a"), clear_invalid_after_utc=True).reason is ChangeRefusal.NEEDS_PERSON_DECISION


def test_a_delete_after_another_writers_change_needs_the_person(storage, store, requests):
    usermem(store, by(ChangeWriterKind.PERSON))
    open_requests(requests, "req_a", "req_b")
    replace(store, request("req_b"), tags=["travel"])

    def delete():
        digest = pinned_memory_record_digest(fact(store))
        store.delete_usermem(canonical_user_id=ALICE, memory_id=1, writer=request("req_a"), expected_record_digest=digest)

    assert refused(delete).reason is ChangeRefusal.CHANGE_CONFLICT
    removal = refused(delete)
    assert removal.reason is ChangeRefusal.NEEDS_PERSON_DECISION and "It set tags" in removal.message
    assert fact(store).fact_text == "Trip on Tuesday."


def test_people_and_scheduled_jobs_count_and_system_upkeep_does_not(storage, store, requests):
    usermem(store, by(ChangeWriterKind.PERSON))
    open_requests(requests, "req_a")
    replace(store, MemoryWriter.system(TEAM), tags=["system"])
    replace(store, request("req_a"), tags=["mine"])  # System upkeep never counts.

    replace(store, by(ChangeWriterKind.PERSON), fact_text="Trip on Thursday.")
    replace(store, by(ChangeWriterKind.SCHEDULED), metadata={"audited": True})
    brief = refused(replace, store, request("req_a"), tags=["mine", "trip"]).details["brief"]
    assert [(change["by"], change["fields"]) for change in brief["changes"]] == [
        ("you (in the app)", ["fact_text"]),
        ("one of your scheduled jobs", ["metadata"]),
    ]
    replace(store, request("req_a"), tags=["mine", "trip"])


def test_team_memory_names_another_team_members_request(storage, store, requests):
    store.create_coremem(team_id=TEAM, fact_text="Office closes at six.", capacity=10, writer=by(ChangeWriterKind.PERSON))
    open_requests(requests, "req_a")
    open_requests(requests, "req_bob", person=BOB)
    replace(store, request("req_bob", BOB), bank="coremem", fact_text="Office closes at five.")

    brief = refused(replace, store, request("req_a"), bank="coremem", fact_text="Office closes at seven.").details["brief"]
    assert brief["record"] == "coremem #1"
    assert brief["changes"][0]["by"] == "another team member's request"


def test_creating_is_never_refused_and_a_closed_request_changes_nothing(storage, store, requests):
    open_requests(requests, "req_a", "req_b")
    usermem(store, request("req_b"))
    usermem(store, request("req_a"))  # Another request's new fact does not stop a create.

    requests.close(team_id=TEAM, request_id="req_a", person_id=ALICE, state=RequestState.ENDED, now=NOW)
    closed = refused(usermem, store, request("req_a"))
    assert closed.reason is ChangeRefusal.REQUEST_CLOSED
    assert len(store.list_usermem(canonical_user_id=ALICE)) == 2


def test_a_fact_another_writer_created_while_the_request_ran_needs_the_person_to_change(storage, store, requests):
    open_requests(requests, "req_a", "req_b")
    usermem(store, request("req_b"))

    assert refused(replace, store, request("req_a"), tags=["travel"]).reason is ChangeRefusal.CHANGE_CONFLICT
    # Creating set every field, so any change to it is the person's to decide.
    assert refused(replace, store, request("req_a"), tags=["travel"]).reason is ChangeRefusal.NEEDS_PERSON_DECISION


def test_undo_is_checked_as_the_change_it_makes(storage, store, requests):
    usermem(store, by(ChangeWriterKind.PERSON))
    open_requests(requests, "req_a", "req_b")
    edited = replace(store, request("req_b"), fact_text="Trip on Wednesday.")

    # Undoing another request's edit restores fact_text: shown once, then the person's to decide.
    assert refused(store.undo_memory_event, edited.event_id, writer=request("req_a")).reason is ChangeRefusal.CHANGE_CONFLICT
    assert refused(store.undo_memory_event, edited.event_id, writer=request("req_a")).reason is (
        ChangeRefusal.NEEDS_PERSON_DECISION
    )
    assert fact(store).fact_text == "Trip on Wednesday."

    # Bringing back a deleted fact creates it again, which is never refused.
    digest = pinned_memory_record_digest(fact(store))
    deleted = store.delete_usermem(canonical_user_id=ALICE, memory_id=1, writer=request("req_b"), expected_record_digest=digest)
    store.undo_memory_event(deleted.event_id, writer=request("req_a"))
    assert fact(store).fact_text == "Trip on Wednesday."


def test_the_version_check_comes_first(storage, store, requests):
    usermem(store, by(ChangeWriterKind.PERSON))
    open_requests(requests, "req_a", "req_b")
    stale = pinned_memory_record_digest(fact(store))
    replace(store, request("req_b"), fact_text="Trip on Wednesday.")

    with pytest.raises(PinnedMemoryConflictError):
        store.replace_usermem(
            canonical_user_id=ALICE, memory_id=1, writer=request("req_a"), expected_record_digest=stale, tags=["x"]
        )
    assert count(storage, "change_reviews") == 0  # A stale read is not a review.
    assert refused(replace, store, request("req_a"), tags=["x"]).reason is ChangeRefusal.CHANGE_CONFLICT


def test_the_ledger_keeps_field_names_by_the_facts_internal_id(storage, store, requests):
    first = store.create_usermem(canonical_user_id=ALICE, fact_text="Trip on Tuesday.", capacity=10, writer=by(ChangeWriterKind.PERSON))
    replace(store, by(ChangeWriterKind.PERSON), tags=["travel"], fact_text="Trip on Monday.")
    digest = pinned_memory_record_digest(fact(store))
    store.delete_usermem(canonical_user_id=ALICE, memory_id=1, writer=by(ChangeWriterKind.PERSON), expected_record_digest=digest)
    second = store.create_usermem(canonical_user_id=ALICE, fact_text="Hotel booked.", capacity=10, writer=by(ChangeWriterKind.PERSON))

    assert second.fact.memory_id == first.fact.memory_id == 1  # The number is reused; the record is not.
    first_key, second_key = first.fact.fact_uuid, second.fact.fact_uuid
    assert ledger(storage) == [
        (first_key, "create", "person", ["*"]),
        (first_key, "change", "person", ["fact_text", "tags"]),
        (first_key, "change", "person", ["*"]),
        (second_key, "create", "person", ["*"]),
    ]


def test_with_checks_turned_off_requests_are_recorded_but_never_refused(storage, store, requests):
    usermem(store, by(ChangeWriterKind.PERSON))
    open_requests(requests, "req_a", "req_b")
    replace(store, request("req_b"), fact_text="Trip on Wednesday.")

    replace(store, request("req_a", checked=False), fact_text="Trip on Friday.")
    assert fact(store).fact_text == "Trip on Friday."
    with _connect(storage) as conn:
        checked = conn.execute("SELECT checked FROM change_ledger ORDER BY position DESC LIMIT 1").fetchone()
    assert int(checked[0]) == 0
