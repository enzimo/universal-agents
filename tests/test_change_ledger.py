"""Separate requests and personal knowledge pages: the change ledger and rule (b).

Real stores (SQLite, and PostgreSQL when ``LIVE_TEST_POSTGRES_DSN`` is set),
the real knowledge service and repository, and the request rows the team lead
writes (``storage.change_ledger``). Each test asserts what was saved.
"""

from __future__ import annotations

import logging
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Barrier, Event
from uuid import uuid4

import pytest

from custom_messages.enums import ChangeRecordKind, ChangeRefusal
from knowledge_base.records import KnowledgeActor, KnowledgeError
from knowledge_base.service import DraftInput, KnowledgeService
from knowledge_base.vocabulary import ActorKind
from runtime_settings.knowledge import KnowledgeSettings
from storage.change_ledger import (
    LEDGER_RETENTION,
    ChangeRequestStore,
    ChangeWriterKind,
    RequestState,
    close_request,
    prune,
)
from storage.config import StorageSettings, resolve_storage_settings
from storage.knowledge_repository import create_knowledge_repository
from storage.postgres_connection import connect_postgres
from storage.sqlite_connection import connect_sqlite
from tests.knowledge_support import BACKENDS, NOW, TEAM, agent, knowledge_storage, person

ALICE = "alice"
PAGE = "personal/trip"


def request_actor(request_id: str, actor_id: str = "agent_1", who: str = ALICE) -> KnowledgeActor:
    """An agent working on one of the person's requests (a gateway-signed context)."""

    return replace(agent(actor_id, person=who, work_id=request_id), request_id=request_id)


def scheduled_actor(who: str = ALICE) -> KnowledgeActor:
    """An agent doing scheduled work for the person (scheduler-signed: no request id)."""

    return agent("agent_sched", person=who, work_id="schedule_run_1")


def service(settings: StorageSettings, *, now: datetime = NOW) -> KnowledgeService:
    limits = {"page_edits_per_agent": 100, "edit_war_edits": 100, "recent_revisions_compared": 1}
    return KnowledgeService(
        create_knowledge_repository(settings), settings=KnowledgeSettings(limits=limits), clock=lambda: now
    )


def edit(svc: KnowledgeService, actor: KnowledgeActor, body: str, *, base: int, reference: str = PAGE) -> int:
    page = svc.write(actor, reference, DraftInput(body=body), base_revision=base).page
    assert page is not None
    return page.revision


def refusal(svc: KnowledgeService, actor: KnowledgeActor, body: str, *, base: int) -> str:
    with pytest.raises(KnowledgeError) as refused:
        svc.write(actor, PAGE, DraftInput(body=body), base_revision=base)
    return refused.value.code


def _connect(settings: StorageSettings):
    if settings.backend == "sqlite":
        return connect_sqlite(str(settings.sqlite_path))
    return connect_postgres(str(settings.postgres_dsn))


def ledger_rows(settings: StorageSettings) -> list[tuple[str, str, str, str]]:
    with _connect(settings) as conn:
        rows = conn.execute(
            "SELECT change_type, writer_kind, request_id, version FROM change_ledger ORDER BY position"
        ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rows]


def request_ids(settings: StorageSettings) -> list[str]:
    with _connect(settings) as conn:
        rows = conn.execute("SELECT request_id FROM change_requests ORDER BY request_id").fetchall()
    return [str(r[0]) for r in rows]


@pytest.fixture(params=BACKENDS)
def storage(request, tmp_path):
    with knowledge_storage(request.param, tmp_path) as settings:
        yield settings


@pytest.fixture
def setup(storage):
    """A personal page Alice made herself (revision 1), and the store that opens requests."""

    svc = service(storage)
    svc.write(person(ALICE), PAGE, DraftInput(title="Trip", body="Leave Monday."))
    return svc, ChangeRequestStore(storage)


def test_a_request_cannot_change_a_page_another_request_changed_while_it_ran(storage, setup):
    svc, requests = setup
    requests.open(team_id=TEAM, request_id="req_b", person_id=ALICE, now=NOW)
    requests.open(team_id=TEAM, request_id="req_a", person_id=ALICE, now=NOW)

    assert edit(svc, request_actor("req_a"), "Leave Tuesday.", base=1) == 2
    # B read the newest version and still may not change it: separate requests stop and check.
    assert refusal(svc, request_actor("req_b", "agent_2"), "Leave Wednesday.", base=2) == ChangeRefusal.CHANGE_CONFLICT

    assert svc.read(person(ALICE), PAGE).page.body == "Leave Tuesday."
    # A request that starts after that change is not stopped, and A keeps changing its own page.
    requests.open(team_id=TEAM, request_id="req_c", person_id=ALICE, now=NOW)
    assert edit(svc, request_actor("req_c", "agent_3"), "Leave Thursday.", base=2) == 3
    assert refusal(svc, request_actor("req_a"), "Leave Friday.", base=3) == ChangeRefusal.CHANGE_CONFLICT
    assert ledger_rows(storage) == [
        ("create", ChangeWriterKind.PERSON, "", "1"),
        ("change", ChangeWriterKind.REQUEST, "req_a", "2"),
        ("change", ChangeWriterKind.REQUEST, "req_c", "3"),
    ]


def test_a_stale_read_is_refused_as_changed_since_read(setup):
    svc, requests = setup
    requests.open(team_id=TEAM, request_id="req_a", person_id=ALICE, now=NOW)
    svc.write(person(ALICE), PAGE, DraftInput(body="Leave Sunday."), base_revision=1)

    assert refusal(svc, request_actor("req_a"), "Leave Tuesday.", base=1) == ChangeRefusal.CHANGED_SINCE_READ


def test_scheduled_work_and_peoples_edits_are_recorded_but_never_stop_a_request(storage, setup):
    svc, requests = setup
    requests.open(team_id=TEAM, request_id="req_b", person_id=ALICE, now=NOW)

    assert edit(svc, scheduled_actor(), "Scheduled update.", base=1) == 2
    assert edit(svc, person(ALICE), "Alice's own edit.", base=2) == 3
    assert edit(svc, request_actor("req_b"), "B's change.", base=3) == 4
    # Scheduled work is never stopped by rule (b), also after a request changed the page.
    assert edit(svc, scheduled_actor(), "Scheduled again.", base=4) == 5
    kinds = [row[1] for row in ledger_rows(storage)]
    assert kinds == ["person", "scheduled", "person", "request", "scheduled"]


def test_notes_add_without_overwriting_so_they_never_stop_or_count_against_a_request(setup):
    svc, requests = setup
    requests.open(team_id=TEAM, request_id="req_b", person_id=ALICE, now=NOW)
    requests.open(team_id=TEAM, request_id="req_a", person_id=ALICE, now=NOW)

    svc.add_note(request_actor("req_a"), PAGE, "Check the weather.")
    assert edit(svc, request_actor("req_b", "agent_2"), "Leave Tuesday.", base=1) == 2
    svc.add_note(request_actor("req_a"), PAGE, "Book the train.")

    assert {note.body for note in svc.read(person(ALICE), PAGE).open_notes} == {"Check the weather.", "Book the train."}


def test_a_stopped_or_ended_request_changes_nothing(setup):
    svc, requests = setup
    requests.open(team_id=TEAM, request_id="req_a", person_id=ALICE, now=NOW)
    requests.close(team_id=TEAM, request_id="req_a", person_id=ALICE, state=RequestState.STOPPED, now=NOW)

    assert refusal(svc, request_actor("req_a"), "Leave Tuesday.", base=1) == ChangeRefusal.REQUEST_CLOSED
    with pytest.raises(KnowledgeError) as created:
        svc.write(request_actor("req_a"), "personal/new", DraftInput(title="New", body="Text."))
    assert created.value.code == ChangeRefusal.REQUEST_CLOSED
    with pytest.raises(KnowledgeError) as noted:
        svc.add_note(request_actor("req_a"), PAGE, "Late note.")
    assert noted.value.code == ChangeRefusal.REQUEST_CLOSED
    # A request that ended before its row existed is closed too (a worker still running is refused).
    requests.close(team_id=TEAM, request_id="req_old", person_id=ALICE, state=RequestState.ENDED, now=NOW)
    assert refusal(svc, request_actor("req_old"), "Leave Tuesday.", base=1) == ChangeRefusal.REQUEST_CLOSED
    assert svc.read(person(ALICE), PAGE).page.revision == 1


def test_creating_a_page_is_recorded_and_never_stopped(storage, setup):
    svc, requests = setup
    requests.open(team_id=TEAM, request_id="req_b", person_id=ALICE, now=NOW)
    requests.open(team_id=TEAM, request_id="req_a", person_id=ALICE, now=NOW)

    svc.write(request_actor("req_a"), "personal/a", DraftInput(title="A", body="From A."))
    svc.write(request_actor("req_b"), "personal/b", DraftInput(title="B", body="From B."))

    assert [row[:3] for row in ledger_rows(storage)][-2:] == [
        ("create", "request", "req_a"),
        ("create", "request", "req_b"),
    ]


def test_a_request_without_durable_admission_is_refused_without_resetting_its_start(storage, setup, caplog):
    svc, _requests = setup
    with caplog.at_level(logging.WARNING, logger="storage.change_ledger"):
        assert refusal(svc, request_actor("req_old"), "Leave Tuesday.", base=1) == ChangeRefusal.REQUEST_CLOSED
    assert "event=change_ledger_request_not_admitted" in caplog.text
    assert "request_id=req_old" in caplog.text
    assert request_ids(storage) == []
    ChangeRequestStore(storage).open(team_id=TEAM, request_id="req_new", person_id=ALICE, now=NOW)
    assert edit(svc, request_actor("req_new"), "Leave Wednesday.", base=1) == 2
    # Reading the newest revision cannot turn an unadmitted request into a new one.
    assert refusal(svc, request_actor("req_old"), "Leave Thursday.", base=2) == ChangeRefusal.REQUEST_CLOSED
    assert request_ids(storage) == ["req_new"]


def test_team_pages_are_not_in_the_ledger(storage, setup):
    svc, _requests = setup
    system = KnowledgeActor(kind=ActorKind.SYSTEM, actor_id="system", team_id=TEAM)

    svc.write(system, "team/ci", DraftInput(title="CI", body="Runs on push."))

    assert [row[0] for row in ledger_rows(storage)] == ["create"]  # only Alice's personal page


def test_closing_a_request_prunes_old_ledger_entries_and_closed_requests(storage, setup):
    svc, requests = setup
    requests.open(team_id=TEAM, request_id="req_a", person_id=ALICE, now=NOW)
    edit(svc, request_actor("req_a"), "Leave Tuesday.", base=1)
    requests.close(team_id=TEAM, request_id="req_a", person_id=ALICE, state=RequestState.ENDED, now=NOW)
    later = NOW + LEDGER_RETENTION + timedelta(days=1)
    requests.open(team_id=TEAM, request_id="req_b", person_id=ALICE, now=later)
    edit(service(storage, now=later), request_actor("req_b"), "Leave Wednesday.", base=2)

    requests.close(team_id=TEAM, request_id="req_b", person_id=ALICE, state=RequestState.ENDED, now=later)

    # Entries and closed requests older than the retention are gone; req_b's own are kept.
    assert ledger_rows(storage) == [("change", "request", "req_b", "3")]
    assert request_ids(storage) == ["req_b"]


def _race(settings: StorageSettings, request_id: str, barrier: multiprocessing.synchronize.Barrier, out) -> None:
    svc = service(settings)
    barrier.wait()
    try:
        out.put((request_id, edit(svc, request_actor(request_id, f"agent_{request_id}"), f"From {request_id}.", base=1)))
    except KnowledgeError as error:
        out.put((request_id, error.code))


def test_two_requests_writing_at_once_in_two_processes_save_exactly_one(storage, setup):
    _svc, requests = setup
    requests.open(team_id=TEAM, request_id="req_a", person_id=ALICE, now=NOW)
    requests.open(team_id=TEAM, request_id="req_b", person_id=ALICE, now=NOW)
    context = multiprocessing.get_context("spawn")
    barrier, out = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_race, args=(storage, rid, barrier, out)) for rid in ("req_a", "req_b")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
    outcomes = dict(out.get(timeout=5) for _ in workers)

    saved = [rid for rid, outcome in outcomes.items() if outcome == 2]
    refused = [outcome for outcome in outcomes.values() if outcome != 2]
    assert len(saved) == 1 and refused in ([ChangeRefusal.CHANGE_CONFLICT], [ChangeRefusal.CHANGED_SINCE_READ])
    assert [row[2] for row in ledger_rows(storage) if row[0] == "change"] == saved


def test_the_ledger_names_the_record_kind(storage, setup):
    svc, requests = setup
    requests.open(team_id=TEAM, request_id="req_a", person_id=ALICE, now=NOW)
    edit(svc, request_actor("req_a"), "Leave Tuesday.", base=1)
    with _connect(storage) as conn:
        kinds = {str(row[0]) for row in conn.execute("SELECT record_kind FROM change_ledger").fetchall()}
    assert kinds == {ChangeRecordKind.KNOWLEDGE_PAGE.value}


def test_removing_a_person_deletes_their_ledger_and_request_rows(storage, setup):
    from storage.person_departure_repository import PersonDepartureRepository

    svc, requests = setup
    requests.open(team_id=TEAM, request_id="req_a", person_id=ALICE, now=NOW)
    edit(svc, request_actor("req_a"), "Leave Tuesday.", base=1)
    svc.write(person("bob"), "personal/bob", DraftInput(title="Bob", body="Bob's page."))

    deleted = PersonDepartureRepository(storage).delete_person_records(ALICE)

    assert deleted["change_requests"] == 1 and deleted["change_ledger"] == 2
    assert request_ids(storage) == []
    assert [row[1] for row in ledger_rows(storage)] == [ChangeWriterKind.PERSON]  # Bob's page stays


def test_with_checks_turned_off_requests_are_recorded_but_never_refused(storage, setup):
    svc, requests = setup
    requests.open(team_id=TEAM, request_id="req_b", person_id=ALICE, now=NOW)
    requests.open(team_id=TEAM, request_id="req_a", person_id=ALICE, now=NOW)
    unchecked = lambda request_id: replace(request_actor(request_id), conflict_checks=False)  # noqa: E731

    assert edit(svc, unchecked("req_a"), "Leave Tuesday.", base=1) == 2
    assert edit(svc, unchecked("req_b"), "Leave Wednesday.", base=2) == 3
    requests.close(team_id=TEAM, request_id="req_b", person_id=ALICE, state=RequestState.ENDED, now=NOW)
    assert edit(svc, unchecked("req_b"), "Leave Thursday.", base=3) == 4
    # "Changed since you read it" still applies with the checks off.
    assert refusal(svc, unchecked("req_a"), "Stale.", base=3) == ChangeRefusal.CHANGED_SINCE_READ

    # Turned on again, only later changes count: req_a's own earlier changes and
    # req_b's unchecked ones do not stop req_a.
    assert edit(svc, request_actor("req_a"), "Leave Friday.", base=4) == 5
    with _connect(storage) as conn:
        checked = [int(r[0]) for r in conn.execute("SELECT checked FROM change_ledger ORDER BY position").fetchall()]
    assert checked == [1, 0, 0, 0, 1]


@pytest.mark.live
@pytest.mark.postgres
@pytest.mark.integration
def test_request_closure_waits_for_a_checked_write_then_refuses_later_writes(tmp_path, monkeypatch):
    """Closure cannot commit while a page change still holds an OPEN check."""

    from psycopg.conninfo import make_conninfo

    with knowledge_storage("postgres", tmp_path) as settings:
        svc = service(settings)
        svc.write(person(ALICE), PAGE, DraftInput(title="Trip", body="Original."))
        requests = ChangeRequestStore(settings)
        requests.open(team_id=TEAM, request_id="request_a", person_id=ALICE, now=NOW)
        closing_name = "ledger_close_" + uuid4().hex
        closing_settings = resolve_storage_settings({
            "backend": "postgres", "postgres": {"dsn": make_conninfo(settings.postgres_dsn, application_name=closing_name)},
        })
        closing_store = ChangeRequestStore(closing_settings)
        checked, release = Event(), Event()
        repo = svc._repository
        original = repo._check_ledger

        def hold_checked_write(*args, **kwargs):
            original(*args, **kwargs)
            checked.set()
            assert release.wait(10), "The test did not release its checked writer."

        monkeypatch.setattr(repo, "_check_ledger", hold_checked_write)
        with ThreadPoolExecutor(max_workers=2) as pool:
            write = pool.submit(edit, svc, request_actor("request_a"), "Already checked.", base=1)
            try:
                assert checked.wait(10)
                close = pool.submit(
                    closing_store.close, team_id=TEAM, request_id="request_a", person_id=ALICE,
                    state=RequestState.STOPPED, now=NOW,
                )
                # Observe PostgreSQL itself waiting for the row lock. A test
                # thread that merely has not been scheduled cannot satisfy this.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not close.done():
                    with connect_postgres(settings.postgres_dsn) as conn:
                        waiting = conn.execute(
                            "SELECT wait_event_type FROM pg_stat_activity WHERE application_name=%s", (closing_name,),
                        ).fetchone()
                    if waiting is not None and waiting[0] == "Lock":
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("Request closure did not wait for the checked write's row lock.")
            finally:
                release.set()
            assert write.result(timeout=10) == 2
            close.result(timeout=10)
        monkeypatch.setattr(repo, "_check_ledger", original)
        assert svc.read(person(ALICE), PAGE).page.body == "Already checked."
        assert refusal(svc, request_actor("request_a"), "Too late.", base=2) == ChangeRefusal.REQUEST_CLOSED


@pytest.mark.live
@pytest.mark.postgres
@pytest.mark.integration
def test_retention_does_not_deadlock_concurrent_terminal_saves(tmp_path):
    """Each save holds an old closed row before both try to prune the other."""

    with knowledge_storage("postgres", tmp_path) as settings:
        requests = ChangeRequestStore(settings)
        old = NOW - LEDGER_RETENTION - timedelta(days=1)
        for request_id in ("request_a", "request_b"):
            requests.open(team_id=TEAM, request_id=request_id, person_id=ALICE, now=old)
            requests.close(team_id=TEAM, request_id=request_id, person_id=ALICE, state=RequestState.ENDED, now=old)
        both_locked = Barrier(2)

        def terminal_save(request_id):
            with connect_postgres(settings.postgres_dsn) as conn:
                conn.begin_write()
                close_request(
                    conn, conn.dialect, team_id=TEAM, request_id=request_id, person_id=ALICE,
                    state=RequestState.ENDED, now=NOW,
                )
                both_locked.wait(timeout=5)
                prune(conn, conn.dialect, team_id=TEAM, before=NOW - LEDGER_RETENTION)
                conn.commit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(terminal_save, "request_a")
            second = pool.submit(terminal_save, "request_b")
            first.result(timeout=10)
            second.result(timeout=10)
        assert request_ids(settings) == []
