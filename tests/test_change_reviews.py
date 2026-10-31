"""The change ledger's review rule, at the ledger itself (coordination plan 7.10).

The knowledge store tests (``tests/test_change_ledger.py``) cover a real
store; these cover what no single store reaches: every writer kind and person
as a request sees them, system upkeep, hidden and shortened values in a
brief, and removals and creations against field changes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_messages.enums import ChangeRecordKind, ChangeRefusal
from storage.change_ledger import (
    ALL_FIELDS,
    BRIEF_VALUE_LIMIT,
    ChangeRefused,
    ChangeType,
    ChangeWriter,
    ChangeWriterKind,
    PendingReview,
    PlannedChange,
    RecordRef,
    check_change,
    ensure_ledger_tables,
    open_request,
    record_change,
    save_review,
)
from storage.sql_dialect import SQLITE
from storage.sqlite_connection import connect_sqlite

TEAM = "team_reviews"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
ALICE, BOB = "alice", "bob"
FACT = RecordRef(ChangeRecordKind.MEMORY_FACT, "fact_7")
ALICE_REQUEST = ChangeWriter(ChangeWriterKind.REQUEST, request_id="req_alice", person_id=ALICE)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "ledger.db")
    with connect_sqlite(path) as conn:
        conn.begin_write()
        ensure_ledger_tables(conn, SQLITE)
        open_request(conn, SQLITE, team_id=TEAM, request_id=ALICE_REQUEST.request_id, person_id=ALICE, now=NOW)
        conn.commit()
    return path


def write(db: str, writer: ChangeWriter, fields: set[str], change_type: ChangeType = ChangeType.CHANGE) -> None:
    with connect_sqlite(db) as conn:
        conn.begin_write()
        change = PlannedChange(FACT, change_type, fields=frozenset(fields))
        record_change(conn, SQLITE, team_id=TEAM, writer=writer, change=change, version="v", now=NOW)
        conn.commit()


def check(db: str, fields: set[str], *, current: dict[str, str] | None = None) -> None:
    change = PlannedChange(
        FACT, ChangeType.CHANGE, label="memory fact 7", fields=frozenset(fields), current=current or {}
    )
    with connect_sqlite(db) as conn:
        conn.begin_write()
        try:
            check_change(conn, SQLITE, team_id=TEAM, writer=ALICE_REQUEST, change=change, now=NOW)
        finally:
            conn.rollback()


def refused(db: str, fields: set[str], **kwargs) -> ChangeRefused:
    with pytest.raises(ChangeRefused) as error:
        check(db, fields, **kwargs)
    review = error.value.review
    if review is not None:
        with connect_sqlite(db) as conn:
            conn.begin_write()
            save_review(conn, SQLITE, review)
            conn.commit()
    return error.value


def test_each_writer_is_named_as_the_request_sees_it_and_system_upkeep_never_counts(db):
    write(db, ChangeWriter(ChangeWriterKind.REQUEST, request_id="req_bob", person_id=BOB), {"text"})
    write(db, ChangeWriter(ChangeWriterKind.SCHEDULED, person_id=ALICE), {"text"})
    write(db, ChangeWriter(ChangeWriterKind.SCHEDULED, person_id=BOB), {"text"})
    write(db, ChangeWriter(ChangeWriterKind.PERSON, person_id=ALICE), {"text"})
    write(db, ChangeWriter(ChangeWriterKind.SYSTEM), {"text"})
    write(db, ChangeWriter(ChangeWriterKind.PERSON, person_id=BOB), {"text"})
    # The request's own changes never count against it.
    write(db, ALICE_REQUEST, {"text"})

    brief = refused(db, {"text"}).details["brief"]

    assert [change["by"] for change in brief["changes"]] == [
        "another team member's request",
        "one of your scheduled jobs",
        "another team member's scheduled job",
        "you (in the app)",
        "another team member (in the app)",
    ]
    assert brief["earlier_changes_not_listed"] == 0


def test_only_system_upkeep_never_stops_a_request(db):
    write(db, ChangeWriter(ChangeWriterKind.SYSTEM), {"text"})

    check(db, {"text"})


def test_a_brief_hides_credentials_and_shortens_long_values(db):
    write(db, ChangeWriter(ChangeWriterKind.REQUEST, request_id="req_other", person_id=ALICE), {"text", "notes"})

    brief = refused(
        db, {"text"}, current={"text": "password=hunter2hunter2", "notes": "n" * (BRIEF_VALUE_LIMIT + 500)}
    ).details["brief"]

    assert brief["now"]["text"].startswith("(hidden:")
    assert brief["now"]["notes"].startswith("n" * BRIEF_VALUE_LIMIT)
    assert "shortened; read the record for the rest" in brief["now"]["notes"]


def test_a_removal_overlaps_any_change_and_a_creation_overlaps_any_field(db):
    other = ChangeWriter(ChangeWriterKind.REQUEST, request_id="req_other", person_id=ALICE)
    write(db, other, {"title"})
    assert refused(db, {ALL_FIELDS}).reason is ChangeRefusal.CHANGE_CONFLICT
    removal = refused(db, {ALL_FIELDS})
    assert removal.reason is ChangeRefusal.NEEDS_PERSON_DECISION
    assert removal.review is None

    write(db, other, set(), ChangeType.CREATE)
    assert refused(db, {"notes"}).reason is ChangeRefusal.CHANGE_CONFLICT
    assert refused(db, {"notes"}).reason is ChangeRefusal.NEEDS_PERSON_DECISION


def test_notes_and_unchecked_changes_never_count(db):
    write(db, ChangeWriter(ChangeWriterKind.REQUEST, request_id="req_other", person_id=ALICE), set(), ChangeType.APPEND)
    write(db, ChangeWriter(ChangeWriterKind.REQUEST, request_id="req_off", person_id=ALICE, checked=False), {"text"})

    check(db, {"text"})


def test_a_review_only_moves_forward(db):
    def review(position: int) -> None:
        with connect_sqlite(db) as conn:
            conn.begin_write()
            save_review(conn, SQLITE, PendingReview(TEAM, ALICE_REQUEST.request_id, ALICE, FACT, position, f"case_{position}", NOW))
            conn.commit()

    review(5)
    review(3)
    review(5)  # Retrying a saved brief never creates another case.

    with connect_sqlite(db) as conn:
        rows = conn.execute("SELECT reviewed_position FROM change_reviews").fetchall()
        cases = conn.execute("SELECT case_id, state FROM change_cases").fetchall()
    assert [int(row[0]) for row in rows] == [5]
    assert {tuple(row) for row in cases} == {("case_5", "open"), ("case_3", "open")}


def test_two_unsaved_retries_share_one_case(db):
    write(db, ChangeWriter(ChangeWriterKind.PERSON, person_id=ALICE), {"text"})
    reviews = []
    for _ in range(2):
        with pytest.raises(ChangeRefused) as error:
            check(db, {"text"})
        reviews.append(error.value.review)
    assert reviews[0].case_id == reviews[1].case_id
    with connect_sqlite(db) as conn:
        conn.begin_write()
        for review in reviews:
            save_review(conn, SQLITE, review)
        assert conn.execute("SELECT COUNT(*) FROM change_cases").fetchone()[0] == 1
        conn.commit()
