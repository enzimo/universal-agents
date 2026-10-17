"""The access store and the person departure store on a real PostgreSQL server, under concurrent writers.

PostgreSQL's default READ COMMITTED isolation lets two transactions read the
same rows before either writes, so the access store's read-then-write steps
(claiming an approved change, a person's settings, the data folder name check,
claims, the event sequence, the previous account link, the one-entry plan)
need its transaction-level write lock (``ACCESS_STORE_WRITE_LOCK_KEY``). Each
test pauses the first writer right after its read, so without the lock the
second writer would read the same state (the defect found in the 2026-10-17
audit). Holding a removed person's rows must never delete a row it did not
hold. Needs ``LIVE_TEST_POSTGRES_DSN``; each test gets its own schema.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator
from uuid import uuid4

import pytest

from access_control.grant_entries import plan_grants
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestStatus,
    AccessRequestV1,
    AccountLinkStatus,
    AccountLinkV1,
    DataFolderRecordV1,
    GrantLifetime,
    GrantV1,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.person_settings_v1 import PersonModelProfileV1, PersonSettingsV1
from model_providers.endpoints import EndpointKind
from storage.access_repository import AccessRepository, AccessStoreConflict, DecisionInProgress
from storage.config import StorageSettings, resolve_storage_settings
from storage.person_departure_repository import PersonDepartureRepository

pytestmark = [pytest.mark.live, pytest.mark.postgres, pytest.mark.integration]

TEAM = "team_pg"
PERSON = "person_pg"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
# How long the first writer waits after its read; the second writer starts during it.
PAUSE_SECONDS = 0.5
OWNER = SubjectRefV1(kind=SubjectKind.USER, id="owner_pg")


@contextmanager
def postgres_storage() -> Iterator[StorageSettings]:
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL access store tests")
    schema = "access_test_" + uuid4().hex
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
def settings() -> Iterator[StorageSettings]:
    with postgres_storage() as created:
        yield created


class _PausedAfterRead:
    """A connection that, after a statement matching ``marker``, signals ``read`` and waits."""

    def __init__(self, conn: Any, marker: str, read: threading.Event) -> None:
        self._conn = conn
        self._marker = marker
        self._read = read

    def __enter__(self) -> "_PausedAfterRead":
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: object) -> object:
        return self._conn.__exit__(*exc)

    def execute(self, sql: str, params: object = None) -> Any:
        cursor = self._conn.execute(sql, params)
        if self._marker in sql and not self._read.is_set():
            self._read.set()
            time.sleep(PAUSE_SECONDS)
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _race(first: AccessRepository, marker: str, first_call: Callable[[], Any], second_call: Callable[[], Any]) -> list[Any]:
    """Run ``first_call`` paused after its ``marker`` read, and ``second_call`` during the pause.

    Returns both outcomes (a result or the exception raised).
    """

    read = threading.Event()
    connect = first._connect
    first._connect = lambda: _PausedAfterRead(connect(), marker, read)  # type: ignore[method-assign]
    outcomes: list[Any] = [None, None]

    def run(index: int, call: Callable[[], Any]) -> None:
        try:
            outcomes[index] = call()
        except Exception as exc:  # noqa: BLE001 - returned to the test
            outcomes[index] = exc

    thread = threading.Thread(target=run, args=(0, first_call))
    thread.start()
    assert read.wait(10), "the first writer never reached its read"
    run(1, second_call)
    thread.join()
    return outcomes


def _request(request_id: str = "access_req_pg", status: AccessRequestStatus = AccessRequestStatus.WAITING) -> AccessRequestV1:
    return AccessRequestV1(
        request_id=request_id,
        team_id=TEAM,
        requester_agent_id="agent-pg",
        requester_agent_type="WebResearchAgent",
        person_id=PERSON,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
        target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"),
        level=AccessLevel.WRITE,
        reason="Track the report",
        status=status,
        created_at_utc=NOW,
    )


def test_claiming_an_approved_change_twice_at_once_applies_it_once(settings) -> None:
    first, second = AccessRepository(settings), AccessRepository(settings)
    first.create_request(_request())
    first.finish_request(_request().model_copy(update={"status": AccessRequestStatus.GRANTED}))

    outcomes = _race(
        first,
        "applied=0",
        lambda: first.claim_change("access_req_pg", at=NOW),
        lambda: second.claim_change("access_req_pg", at=NOW),
    )

    claimed = [outcome for outcome in outcomes if isinstance(outcome, AccessRequestV1)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, AccessStoreConflict)]
    assert len(claimed) == 1 and len(refused) == 1, outcomes


def test_two_changes_to_one_persons_settings_at_once_keep_both(settings) -> None:
    first, second = AccessRepository(settings), AccessRepository(settings)
    own = PersonModelProfileV1(
        kind=EndpointKind.OPENAI_COMPATIBLE, base_url="https://models.example.test/v1", model_id="own-model", tested_at_utc=NOW
    )

    def team_keys_off(current: PersonSettingsV1 | None) -> PersonSettingsV1:
        base = current or PersonSettingsV1(team_id=TEAM, person_id=PERSON, changed_at_utc=NOW)
        return base.model_copy(update={"uses_team_keys": False})

    def own_model(current: PersonSettingsV1 | None) -> PersonSettingsV1:
        base = current or PersonSettingsV1(team_id=TEAM, person_id=PERSON, changed_at_utc=NOW)
        return base.model_copy(update={"own_model": own})

    # The gateway turns team keys off while the broker saves the person's own model.
    _race(
        first,
        "FROM access_person_settings",
        lambda: first.change_person_settings(TEAM, PERSON, team_keys_off),
        lambda: second.change_person_settings(TEAM, PERSON, own_model),
    )

    stored = first.person_settings(TEAM, PERSON)
    assert stored is not None and stored.uses_team_keys is False and stored.own_model == own


def test_two_data_folders_with_one_name_at_once_register_one(settings, tmp_path) -> None:
    first, second = AccessRepository(settings), AccessRepository(settings)

    def folder(record_id: str) -> DataFolderRecordV1:
        return DataFolderRecordV1(
            record_id=record_id,
            team_id=TEAM,
            name="research",
            path=str(tmp_path),
            registered_by=OWNER,
            reason="Shared research data",
            created_at_utc=NOW,
        )

    outcomes = _race(
        first,
        "SELECT 1 FROM access_data_folders",
        lambda: first.register_data_folder(folder("data_folder_a")),
        lambda: second.register_data_folder(folder("data_folder_b")),
    )

    assert sum(isinstance(outcome, AccessStoreConflict) for outcome in outcomes) == 1, outcomes
    assert [record.name for record in first.data_folders(TEAM)] == ["research"]


def test_two_decisions_claiming_one_request_at_once_let_one_proceed(settings) -> None:
    first, second = AccessRepository(settings), AccessRepository(settings)
    first.create_request(_request())
    until = NOW + timedelta(minutes=10)

    outcomes = _race(
        first,
        "FROM access_request_claims",
        lambda: first.claim_decision("access_req_pg", claim_id="decision_a", at=NOW, until=until),
        lambda: second.claim_decision("access_req_pg", claim_id="decision_b", at=NOW, until=until),
    )

    assert sum(isinstance(outcome, AccessRequestV1) for outcome in outcomes) == 1, outcomes
    assert sum(isinstance(outcome, DecisionInProgress) for outcome in outcomes) == 1, outcomes


def test_events_written_at_once_get_distinct_sequence_numbers(settings) -> None:
    stores = [AccessRepository(settings) for _ in range(6)]
    barrier = threading.Barrier(len(stores))

    def record(store: AccessRepository, index: int) -> None:
        barrier.wait(10)
        store.record_decision(TEAM, {"call": index}, at=NOW)

    threads = [threading.Thread(target=record, args=(store, index)) for index, store in enumerate(stores)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with stores[0]._connect() as conn:
        sequences = [row[0] for row in conn.execute("SELECT sequence FROM access_events").fetchall()]
    assert sorted(sequences) == list(range(1, len(stores) + 1))


def test_two_links_stored_at_once_leave_one_active_link(settings) -> None:
    first, second = AccessRepository(settings), AccessRepository(settings)

    def link(link_id: str) -> AccountLinkV1:
        return AccountLinkV1(
            link_id=link_id,
            team_id=TEAM,
            tool_id="cli:x",
            owner_kind=LinkOwnerKind.PERSON,
            owner_id=PERSON,
            credential_key=f"x_{link_id}",
            created_by=SubjectRefV1(kind=SubjectKind.USER, id=PERSON),
            created_at_utc=NOW,
        )

    _race(
        first,
        "FROM access_account_links WHERE team_id",
        lambda: first.create_link(link("link_a")),
        lambda: second.create_link(link("link_b")),
    )

    links = first.list_links(TEAM, include_revoked=True)
    assert sorted(item.status for item in links) == [AccountLinkStatus.ACTIVE, AccountLinkStatus.REVOKED]


def test_two_grants_for_one_entry_stored_at_once_store_one(settings) -> None:
    first, second = AccessRepository(settings), AccessRepository(settings)
    subject = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect")

    def grant(grant_id: str) -> GrantV1:
        return GrantV1(
            grant_id=grant_id,
            team_id=TEAM,
            subject=subject,
            target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"),
            level=AccessLevel.READ,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=OWNER,
            created_at_utc=NOW,
            for_person_id=PERSON,
        )

    def store(repository: AccessRepository, new: GrantV1) -> GrantV1:
        def plan():
            return plan_grants([new], repository.for_subjects(TEAM, [subject]), lambda _set: frozenset())

        return repository.store_planned(new, plan)[0]

    # The plan's read of the holder's grants runs inside the write transaction.
    stored = _race(
        first,
        "FROM access_grants WHERE team_id",
        lambda: store(first, grant("grant_a")),
        lambda: store(second, grant("grant_b")),
    )

    assert [held.grant_id for held in first.for_subjects(TEAM, [subject])] == ["grant_a"]
    assert [item.grant_id for item in stored] == ["grant_a", "grant_a"], "the second call got the first one's grant"


def test_holding_a_person_never_deletes_a_row_it_did_not_hold(settings) -> None:
    departures = PersonDepartureRepository(settings)
    from storage.postgres_connection import connect_postgres

    with connect_postgres(settings.postgres_dsn) as conn:
        conn.execute(
            "CREATE TABLE pinned_memory_facts_active (fact_id TEXT PRIMARY KEY, canonical_user_id TEXT NOT NULL, "
            "bank TEXT NOT NULL, text TEXT NOT NULL)"
        )
        for fact_id in ("fact_1", "fact_2"):
            conn.execute(
                "INSERT INTO pinned_memory_facts_active VALUES (%s, %s, 'usermem', 'remembered')", (fact_id, PERSON)
            )
    connect = departures._connect
    written: list[str] = []

    class MemoryWrittenMeanwhile:
        """The hold's connection: just before it removes the memory rows, an agent saves one more."""

        def __init__(self) -> None:
            self._conn = connect()

        def __enter__(self) -> "MemoryWrittenMeanwhile":
            self._conn.__enter__()
            return self

        def __exit__(self, *exc: object) -> object:
            return self._conn.__exit__(*exc)

        def execute(self, sql: str, params: object = None) -> Any:
            if sql.startswith("DELETE FROM pinned_memory_facts_active") and not written:
                with connect_postgres(settings.postgres_dsn) as other:
                    other.execute(
                        "INSERT INTO pinned_memory_facts_active VALUES ('fact_3', %s, 'usermem', 'saved meanwhile')",
                        (PERSON,),
                    )
                written.append("fact_3")
            return self._conn.execute(sql, params)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._conn, name)

    departures._connect = MemoryWrittenMeanwhile  # type: ignore[method-assign]

    moved = departures.hold_person_records(TEAM, PERSON, at=NOW)

    assert written == ["fact_3"]
    with connect_postgres(settings.postgres_dsn) as conn:
        left = conn.execute("SELECT count(*) FROM pinned_memory_facts_active").fetchone()[0]
    assert left == 0 and moved == 3, "every row that left the table is held"
    assert departures.held_row_count(TEAM, PERSON) == 3
    departures._connect = connect  # type: ignore[method-assign]
    assert departures.restore_person_records(TEAM, PERSON) == 3


def test_an_older_store_with_two_active_links_keeps_the_newest_on_postgres(settings, caplog) -> None:
    import logging

    from storage.access_repository import ONE_ACTIVE_LINK_INDEX
    from storage.postgres_connection import connect_postgres

    store = AccessRepository(settings)

    def link(link_id: str, at: datetime, kind: LinkOwnerKind = LinkOwnerKind.PERSON, owner: str = PERSON) -> AccountLinkV1:
        return AccountLinkV1(
            link_id=link_id, team_id=TEAM, tool_id="cli:x", owner_kind=kind, owner_id=owner,
            credential_key=f"k_{link_id}", created_by=SubjectRefV1(kind=SubjectKind.USER, id=PERSON), created_at_utc=at,
        )

    store.create_link(link("link_new", NOW))
    store.create_link(link("link_team", NOW, LinkOwnerKind.TEAM, TEAM))
    older = link("link_old", NOW - timedelta(minutes=5))
    with connect_postgres(settings.postgres_dsn) as conn:  # a store from before the index
        conn.execute(f"DROP INDEX {ONE_ACTIVE_LINK_INDEX}")
        conn.execute(
            "INSERT INTO access_account_links (link_id, team_id, tool_id, owner_kind, owner_id, status, payload_json, "
            "created_at) VALUES (%s,%s,%s,%s,%s,'active',%s,%s)",
            ("link_old", TEAM, "cli:x", "person", PERSON, older.model_dump_json(), older.created_at_utc.isoformat()),
        )

    with caplog.at_level(logging.WARNING, logger="storage.access_repository"):
        reopened = AccessRepository(settings)

    assert sorted(item.link_id for item in reopened.list_links(TEAM)) == ["link_new", "link_team"]
    revoked = [(e.grant_id, e.detail.get("reason"), e.detail.get("replaced_by")) for e in reopened.events(TEAM)
               if e.kind == "account_link_revoked"]
    assert revoked == [("link_old", "duplicate_active_link", "link_new")]
    assert "revoked_link_id=link_old" in caplog.text
    caplog.clear()
    AccessRepository(settings)
    assert "account_link_duplicate_revoked" not in caplog.text


def test_an_older_store_gets_the_approval_id_column_on_postgres(settings) -> None:
    from storage.postgres_connection import connect_postgres

    store = AccessRepository(settings)
    store.create(
        GrantV1(
            grant_id="grant_old", team_id=TEAM, subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"), level=AccessLevel.READ, granted_by=OWNER,
            approval_id="access_req_old", created_at_utc=NOW, for_person_id=PERSON,
        )
    )
    with connect_postgres(settings.postgres_dsn) as conn:  # a store from before 2026-10-17
        conn.execute("ALTER TABLE access_grants DROP COLUMN approval_id")

    AccessRepository(settings)

    with connect_postgres(settings.postgres_dsn) as conn:
        assert [row[0] for row in conn.execute("SELECT approval_id FROM access_grants").fetchall()] == ["access_req_old"]
        assert conn.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = 'access_grants_approval'"
        ).fetchone()


def test_a_grant_plan_reads_through_the_transactions_own_connection_on_postgres(settings, monkeypatch) -> None:
    import storage.access_repository as access_repository

    store = AccessRepository(settings)
    subject = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect")
    grant = GrantV1(
        grant_id="grant_new", team_id=TEAM, subject=subject, target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"),
        level=AccessLevel.READ, granted_by=OWNER, created_at_utc=NOW, for_person_id=PERSON,
    )
    connect = access_repository.connect_postgres
    opened: list[str] = []

    def counting(*args: Any, **kwargs: Any) -> Any:
        opened.append("connection")
        return connect(*args, **kwargs)

    def plan():
        held = store.for_subjects(TEAM, [subject])  # reads inside the write transaction
        store.get("grant_missing")
        return plan_grants([grant], held, lambda _set: frozenset())

    monkeypatch.setattr(access_repository, "connect_postgres", counting)
    stored, _plan = store.store_planned(grant, plan)

    assert stored.grant_id == "grant_new"
    assert opened == ["connection"], "one connection: the plan borrowed the write transaction's"


def test_two_decisions_on_postgres_take_turns_and_the_second_finds_the_request_decided(settings) -> None:
    first, second = AccessRepository(settings), AccessRepository(settings)
    first.create_request(_request())
    inside = threading.Event()
    outcomes: dict[str, Any] = {}

    def decide_first() -> None:
        with first.decision_transaction("access_req_pg", at=NOW) as request:
            inside.set()
            time.sleep(PAUSE_SECONDS)  # the second decision waits for the write lock meanwhile
            first.finish_request(request.model_copy(update={"status": AccessRequestStatus.GRANTED}))
        outcomes["first"] = "granted"

    thread = threading.Thread(target=decide_first)
    thread.start()
    assert inside.wait(10)
    try:
        with second.decision_transaction("access_req_pg", at=NOW):
            outcomes["second"] = "decided too"
    except AccessStoreConflict as exc:
        outcomes["second"] = str(exc)
    thread.join()

    assert outcomes["first"] == "granted"
    assert "already granted" in outcomes["second"]


def test_a_decisions_grant_writes_join_its_transaction_on_postgres(settings) -> None:
    # Without joining, a grant write inside the decision would open a second
    # connection and wait forever for the advisory lock the decision holds.
    store = AccessRepository(settings)
    store.create_request(_request())
    subject = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect")
    grant = GrantV1(
        grant_id="grant_decided", team_id=TEAM, subject=subject, target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"),
        level=AccessLevel.READ, granted_by=OWNER, approval_id="access_req_pg", created_at_utc=NOW, for_person_id=PERSON,
    )
    done = threading.Event()

    def decide() -> None:
        with store.decision_transaction("access_req_pg", at=NOW) as request:
            store.store_planned(grant, lambda: plan_grants([grant], store.for_subjects(TEAM, [subject]), lambda _s: frozenset()))
            store.finish_request(
                request.model_copy(update={"status": AccessRequestStatus.GRANTED, "grant_ids": ("grant_decided",)})
            )
        done.set()

    thread = threading.Thread(target=decide, daemon=True)
    thread.start()
    assert done.wait(10), "the decision's writes waited for its own lock"
    assert store.get("grant_decided").status.value == "active"
    assert store.get_request("access_req_pg").status is AccessRequestStatus.GRANTED
    assert store.grants_without_granted_outcome(TEAM) == []
