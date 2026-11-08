"""People's memory commands at the team services (team_services.memory, storage.operation_outcomes).

Real SQLite stores; frames are stand-ins with the contract's retry keys
(tests/team_services_support.py), and every body and reply is checked against the
contract's models. tests/test_live_team_memory.py runs the signed path over NATS.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from access_control.people_directory import PeopleDirectory
from custom_agents.pinned_memory_runtime import (
    AGENT_TOOL_COMMAND_ORIGIN,
    PinnedMemoryActor,
    PinnedMemoryCommand,
    PinnedMemoryRequestContext,
    PinnedMemoryService,
)
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.sizes import INLINE_TEXT_MAX_CHARS, REPLY_BODY_MAX_BYTES, encoded_size
from storage.access_repository import AccessRepository
from storage.change_ledger import ChangeWriter, ChangeWriterKind
from storage.config import StorageSettings, resolve_storage_settings
from storage.operation_outcomes import OUTCOME_PERSON_COLUMN, OUTCOMES_TABLE
from storage.pinned_memory_repository import PinnedMemoryRepository, create_pinned_memory_repository
from storage.postgres_connection import connect_postgres
from storage.sql_dialect import POSTGRES, SQLITE
from team_services import memory as memory_module
from team_services.memory import MemoryOperations
from tests.people_directory_support import access_store_at
from tests.team_services_support import checked_reply, stand_in_frame
from tests.test_pinned_memory_stale_edits import BACKENDS
from tests.test_pinned_memory_stale_edits import _storage as memory_storage

TEAM = "team_one"
NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
ADA, BO, CY = "p-AdaAdaAdaAdaAdaAdaAdaA", "p-BoBoBoBoBoBoBoBoBoBoBo", "p-CyCyCyCyCyCyCyCyCyCyCy"
DI = "p-DiDiDiDiDiDiDiDiDiDiDi"
PEOPLE = {ADA: "Ada Admin", BO: "Bo Member", CY: "Cy Gone", DI: "Di Removed"}


class Clock:
    """The team services' clock, which a test moves."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Team:
    """One team's memory commands and what a test reaches beside them: its database, store, people and clock."""

    storage: StorageSettings
    repository: PinnedMemoryRepository
    access: AccessRepository
    clock: Clock
    memory: MemoryOperations


def record(access: AccessRepository, reference: str, roles: tuple[str, ...], standing: PersonStanding, *,
           team: str = TEAM, person_id: str | None = None, at: datetime = NOW) -> None:
    """Write one person's standing and roles into the gateway's copy of the team's people."""

    access.record_person(TeamPersonV1(team_id=team, person_id=person_id or PEOPLE[reference], person_ref=reference,
                                      standing=standing, roles=roles, changed_at_utc=at))


@pytest.fixture(params=BACKENDS)
def team(request, tmp_path: Path) -> Iterator[Team]:
    """On SQLite, and on PostgreSQL with ``LIVE_TEST_POSTGRES_DSN`` (its own schema per test)."""

    access = access_store_at(tmp_path)
    record(access, ADA, ("admin",), PersonStanding.ACTIVE)
    record(access, BO, ("member",), PersonStanding.ACTIVE)
    record(access, CY, (), PersonStanding.DISABLED)
    record(access, DI, (), PersonStanding.REMOVED)
    clock = Clock()
    with memory_storage(request.param, tmp_path) as storage:
        repository = create_pinned_memory_repository(storage)
        yield Team(storage, repository, access, clock,
                   MemoryOperations(repository, PeopleDirectory(access, TEAM), {}, clock, logging.getLogger("test")))


@pytest.fixture
def memory(team: Team) -> MemoryOperations:
    return team.memory


def ask(memory: MemoryOperations, kind: str, body: dict, *, person: str = ADA, request_id: str = "cmd-1",
        **frame) -> dict:
    verified = stand_in_frame(TEAM, kind, {"request_id": request_id, **body}, person_ref=person,
                              request_id=request_id, **frame)
    return checked_reply(kind, memory.answer(verified))


def create(memory: MemoryOperations, text: str, *, bank: str = "usermem", person: str = ADA,
           request_id: str = "cmd-create", **fields) -> dict:
    return ask(memory, "memory.create", {"bank": bank, "fact_text": {"type": "inline", "text": text}, **fields},
               person=person, request_id=request_id)


def listed(memory: MemoryOperations, *, bank: str = "usermem", person: str = ADA, offset: int = 0,
           limit: int = 10) -> dict:
    return ask(memory, "memory.list", {"bank": bank, "sort": "memory_id", "offset": offset, "limit": limit},
               person=person, request_id="cmd-list")


def test_a_persons_own_memory_is_theirs_alone_and_shown_without_metadata(memory: MemoryOperations) -> None:
    saved = create(memory, "Prefers short answers.", metadata_json='{"source":"chat"}', tags=["style"])
    assert saved["outcome"] == "ok" and saved["fact"]["fact_text"]["text"] == "Prefers short answers."
    assert len(saved["fact"]["seen_version"]) == 64 and "metadata_json" not in saved["fact"]
    assert saved["fact"]["tags"] == ["style"] and saved["event_id"]
    assert [fact["memory_id"] for fact in listed(memory)["facts"]] == [1]
    assert listed(memory, person=BO) == {"outcome": "ok", "facts": [], "total": 0}


def test_everyone_reads_team_memory_but_only_administrators_change_it(memory: MemoryOperations) -> None:
    refused = create(memory, "Deploy after smoke tests.", bank="coremem", person=BO, request_id="cmd-bo")
    assert refused == {"outcome": "not_allowed", "message": "Only administrators change team memory."}
    assert create(memory, "Deploy after smoke tests.", bank="coremem")["outcome"] == "ok"
    assert [f["fact_text"]["text"] for f in listed(memory, bank="coremem", person=BO)["facts"]] == [
        "Deploy after smoke tests."]
    deleted = ask(memory, "memory.delete", {"bank": "coremem", "memory_id": 1}, person=BO, request_id="cmd-del")
    assert deleted["outcome"] == "not_allowed"


def test_only_an_active_person_from_the_gateway_is_answered(memory: MemoryOperations, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert listed(memory, person=CY)["outcome"] == "not_allowed"
        assert listed(memory, person="p-NobodyNobodyNobodyNob")["outcome"] == "not_allowed"
        from_agent = ask(memory, "memory.list", {"bank": "usermem", "sort": "memory_id", "offset": 0, "limit": 5},
                         sender_roles=frozenset({"lead_host", "worker_host"}), binding="root_task")
    assert from_agent["outcome"] == "not_allowed"
    assert caplog.text.count("event=team_command_person_refused") == 3


def test_a_retried_command_is_carried_out_once_and_gets_the_same_reply(memory: MemoryOperations, caplog) -> None:
    first = create(memory, "Likes tea.", request_id="cmd-tea")
    again = create(memory, "Likes tea.", request_id="cmd-tea")
    assert again == first and listed(memory)["total"] == 1
    # The same id with another body is the sender's defect: the stored reply answers it, and both digests are logged.
    with caplog.at_level(logging.WARNING):
        other = create(memory, "Likes coffee.", request_id="cmd-tea")
    assert other == first and listed(memory)["total"] == 1
    assert "event=operation_outcome_body_mismatch" in caplog.text


def test_a_refused_change_is_stored_so_a_retry_is_refused_the_same_way(memory: MemoryOperations) -> None:
    body = {"bank": "usermem", "memory_id": 1, "fact_text": {"type": "inline", "text": "Changed."}}
    missing = ask(memory, "memory.replace", body, request_id="cmd-replace")
    assert missing["outcome"] == "not_found"
    create(memory, "Original.")
    assert ask(memory, "memory.replace", body, request_id="cmd-replace") == missing
    assert listed(memory)["facts"][0]["fact_text"]["text"] == "Original."
    assert ask(memory, "memory.replace", body, request_id="cmd-replace-2")["outcome"] == "ok"


def test_a_change_based_on_an_older_read_names_the_current_version(memory: MemoryOperations) -> None:
    created = create(memory, "Original.")
    old = created["fact"]["seen_version"]
    changed = ask(memory, "memory.replace", {"bank": "usermem", "memory_id": 1, "seen_version": old,
                                             "fact_text": {"type": "inline", "text": "Newer."}}, request_id="cmd-a")
    stale = ask(memory, "memory.delete", {"bank": "usermem", "memory_id": 1, "seen_version": old},
                request_id="cmd-b")
    assert stale["outcome"] == "changed_since_read"
    assert stale["current_version"] == changed["fact"]["seen_version"] != old
    assert listed(memory)["total"] == 1


def test_times_paging_undo_and_clearing_an_expiry(memory: MemoryOperations) -> None:
    created = create(memory, "Travels in May next year.", valid_from="2026-01-01T00:00:00.000Z",
                     expires="2027-06-01T00:00:00.000Z")
    assert (created["fact"]["valid_from"], created["fact"]["expires"]) == ("2026-01-01T00:00:00.000Z",
                                                                           "2027-06-01T00:00:00.000Z")
    cleared = ask(memory, "memory.replace", {"bank": "usermem", "memory_id": 1, "clear_expiry": True},
                  request_id="cmd-clear")
    assert "expires" not in cleared["fact"]
    for number in range(2):
        create(memory, f"Fact {number}.", request_id=f"cmd-{number}")
    page = listed(memory, limit=2)
    assert (len(page["facts"]), page["total"], page["next_offset"]) == (2, 3, 2)
    assert "next_offset" not in listed(memory, offset=2, limit=2)
    undone = ask(memory, "memory.undo", {"bank": "usermem", "event_id": created["event_id"]}, request_id="cmd-undo")
    assert undone["outcome"] == "changed_since_read"  # the fact changed after that event
    latest = ask(memory, "memory.undo", {"bank": "usermem", "event_id": cleared["event_id"]}, request_id="cmd-u2")
    assert latest["outcome"] == "ok" and latest["undone_event_type"] == "replace"
    assert latest["fact"]["expires"] == "2027-06-01T00:00:00.000Z"


def test_the_outcome_table_is_created_on_an_older_database(tmp_path: Path) -> None:
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    create_pinned_memory_repository(settings)
    with sqlite3.connect(tmp_path / "manager.db") as conn:
        conn.execute(f"DROP TABLE {OUTCOMES_TABLE}")
    create_pinned_memory_repository(settings)
    with sqlite3.connect(tmp_path / "manager.db") as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {OUTCOMES_TABLE}").fetchone() == (0,)


# -- helpers for the review fixes' tests --------------------------------------------------------------

@contextmanager
def database(storage: StorageSettings) -> Iterator[tuple[Any, str]]:
    """A connection to the test's own database, and its parameter marker; the caller commits a write."""

    if storage.postgres_dsn:
        with connect_postgres(storage.postgres_dsn) as conn:
            yield conn, POSTGRES.bind
        return
    conn = sqlite3.connect(storage.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn, SQLITE.bind
    finally:
        conn.close()


def stored_rows(team: Team) -> list[dict[str, str]]:
    """The stored replies: whose command each answered, of what kind, for which team, and when."""

    with database(team.storage) as (conn, _):
        rows = conn.execute(f"SELECT team_id, {OUTCOME_PERSON_COLUMN} AS person_id, kind, stored_at_utc "
                            f"FROM {OUTCOMES_TABLE} ORDER BY stored_at_utc, person_id").fetchall()
    return [{key: str(row[key]) for key in ("team_id", "person_id", "kind", "stored_at_utc")} for row in rows]


def fact_count(team: Team) -> int:
    with database(team.storage) as (conn, _):
        return int(conn.execute("SELECT COUNT(*) AS n FROM pinned_memory_facts_active").fetchone()["n"])


def agent_service(team: Team, person: str = "Ada Admin") -> tuple[PinnedMemoryService, PinnedMemoryRequestContext]:
    """The memory service as an agent working for ``person`` uses it, which may write facts people cannot send."""

    service = PinnedMemoryService(create_pinned_memory_repository(team.storage), command_origin=AGENT_TOOL_COMMAND_ORIGIN)
    context = PinnedMemoryRequestContext(team_id=TEAM, writer=ChangeWriter(ChangeWriterKind.SYSTEM),
                                         canonical_user_id=person, actor=PinnedMemoryActor(kind="agent", id="lead"))
    return service, context


def agent_writes(team: Team, *texts: str) -> None:
    service, context = agent_service(team)
    for text in texts:
        service.execute(PinnedMemoryCommand(bank="usermem", action="create", fact_text=text), context=context)


def agent_reads(team: Team) -> dict[int, dict[str, Any]]:
    service, context = agent_service(team)
    listed_facts = service.execute(PinnedMemoryCommand(bank="usermem", action="list"), context=context)["facts"]
    return {fact["memory_id"]: fact for fact in listed_facts}


def texts(page: dict) -> list[str]:
    return [fact["fact_text"]["text"] for fact in page["facts"]]


def inline(text: str) -> dict[str, str]:
    return {"type": "inline", "text": text}


# -- replies fit the contract's limits -----------------------------------------------------------------

def test_a_list_of_long_facts_returns_what_fits_in_one_frame_and_where_to_continue(team: Team) -> None:
    # Five facts of 30,001 characters, each taking 90 KB in a frame: an agent or an import can write them.
    long_facts = [f"{number}" + "€" * 30_000 for number in range(5)]
    agent_writes(team, *long_facts)

    first = listed(team.memory, limit=10)
    assert (len(first["facts"]), first["total"], first["next_offset"]) == (2, 5, 2)
    seen, page = texts(first), first
    while "next_offset" in page:
        page = listed(team.memory, offset=page["next_offset"], limit=10)
        assert page["total"] == 5 and encoded_size(page) <= REPLY_BODY_MAX_BYTES
        seen += texts(page)
    assert encoded_size(first) <= REPLY_BODY_MAX_BYTES
    assert seen == long_facts


def test_a_fact_longer_than_inline_text_is_explained_and_never_cut_or_skipped(team: Team) -> None:
    memory = team.memory
    too_long = "x" * (INLINE_TEXT_MAX_CHARS + 1)
    agent_writes(team, "First.", too_long, "Third.")

    shown = ask(memory, "memory.show", {"bank": "usermem", "memory_id": 2}, request_id="cmd-show")
    assert shown["outcome"] == "runtime_error" and f"{INLINE_TEXT_MAX_CHARS + 1:,} characters" in shown["message"]
    # A page ends just before it; a page starting at it explains it and where to continue.
    page = listed(memory)
    assert (texts(page), page["total"], page["next_offset"]) == (["First."], 3, 1)
    at_it = listed(memory, offset=1)
    assert at_it["outcome"] == "runtime_error"
    assert "Memory #2" in at_it["message"] and "list from position 3" in at_it["message"]
    assert texts(listed(memory, offset=2)) == ["Third."]

    # Its reply would have to carry it, so changing only its tags is refused, and the refusal is stored.
    retag = {"bank": "usermem", "memory_id": 2, "tags": ["long"]}
    refused = ask(memory, "memory.replace", retag, request_id="cmd-retag")
    assert refused["outcome"] == "invalid_request" and "Nothing was changed." in refused["message"]
    assert ask(memory, "memory.replace", retag, request_id="cmd-retag") == refused
    assert [(row["person_id"], row["kind"]) for row in stored_rows(team)] == [("Ada Admin", "memory.replace")]
    kept = agent_reads(team)[2]
    assert (kept["fact_text"], kept["tags"]) == (too_long, [])

    deleted = ask(memory, "memory.delete", {"bank": "usermem", "memory_id": 2}, request_id="cmd-delete")
    assert (deleted["outcome"], deleted["memory_id"]) == ("ok", 2)
    assert texts(listed(memory)) == ["First.", "Third."]


def test_a_reply_frame_with_the_longest_ids_leaves_the_body_the_room_its_allowance_promises() -> None:
    """REPLY_FRAME_ALLOWANCE is the room around a reply's body: measured here with every id at its longest."""

    from custom_messages.wire.frame import reply_audience, sign_frame
    from custom_messages.wire.kinds import MAX_FRAME_BYTES
    from custom_messages.wire.models import Frame
    from custom_messages.wire.sizes import REPLY_FRAME_ALLOWANCE
    from custom_transport.protocol1.live import build_frame, encode, request_subject
    from tests import message_contract_support as fixtures

    installation, team_id, runtime = "i" * 62, "t" * 24, "r" * 64
    epoch = 9007199254740991
    issuer = {"installation": installation, "team": team_id, "runtime": runtime, "agent": "a" * 32,
              "session": "s" * 64, "membership_epoch": epoch, "revocation_epoch": epoch, "key_id": "k" * 128}
    audience = {"installation": installation, "team": team_id, "target": "team_services", "id": team_id}
    key = fixtures.private_key("test-services-key-1")
    request = sign_frame(build_frame("memory.create", {"request_id": "c" * 64, "bank": "usermem",
                                                      "fact_text": inline("Likes tea.")}, audience, issuer=issuer,
                                     issued=NOW, subject=request_subject("memory.create", audience, runtime)), key)
    inbox = f"_INBOX.{runtime}."
    body = {"outcome": "ok", "facts": [], "total": 0}
    reply = sign_frame(build_frame("memory.list.reply", body,
                                   reply_audience(Frame.model_validate(request)), issuer=issuer, issued=NOW,
                                   subject=inbox + "x" * (512 - len(inbox)),
                                   answering=Frame.model_validate(request)), key)
    Frame.model_validate(reply)  # the contract allows every one of these ids
    around_body = len(encode(reply)[0]) - encoded_size(body)
    assert around_body < 2048 and 4 * around_body <= REPLY_FRAME_ALLOWANCE
    assert REPLY_BODY_MAX_BYTES + around_body <= MAX_FRAME_BYTES


# -- stored outcomes --------------------------------------------------------------------------------------

def test_a_refused_change_stays_refused_after_the_person_becomes_an_administrator(team: Team) -> None:
    memory = team.memory
    refused = create(memory, "Deploy on Fridays.", bank="coremem", person=BO, request_id="cmd-bo")
    assert refused == {"outcome": "not_allowed", "message": "Only administrators change team memory."}
    record(team.access, BO, ("admin",), PersonStanding.ACTIVE)
    assert create(memory, "Deploy on Fridays.", bank="coremem", person=BO, request_id="cmd-bo") == refused
    assert listed(memory, bank="coremem")["total"] == 0
    # A new command is decided as Bo stands now.
    assert create(memory, "Deploy on Fridays.", bank="coremem", person=BO, request_id="cmd-bo-2")["outcome"] == "ok"


def test_a_disabled_persons_refused_change_stays_refused_after_they_are_enabled_again(team: Team) -> None:
    memory = team.memory
    refused = create(memory, "Likes tea.", person=CY, request_id="cmd-cy")
    assert refused["outcome"] == "not_allowed" and "disabled" in refused["message"]
    record(team.access, CY, ("member",), PersonStanding.ACTIVE)
    assert create(memory, "Likes tea.", person=CY, request_id="cmd-cy") == refused
    assert listed(memory, person=CY)["total"] == 0
    assert create(memory, "Likes tea.", person=CY, request_id="cmd-cy-2")["outcome"] == "ok"


def test_a_person_disabled_after_a_change_gets_its_stored_reply_when_the_command_is_repeated(team: Team) -> None:
    memory = team.memory
    saved = create(memory, "Likes tea.", person=BO, request_id="cmd-bo")
    assert saved["outcome"] == "ok"
    record(team.access, BO, (), PersonStanding.DISABLED)
    assert create(memory, "Likes tea.", person=BO, request_id="cmd-bo") == saved
    assert create(memory, "Likes coffee.", person=BO, request_id="cmd-bo-2")["outcome"] == "not_allowed"


def test_a_refusal_that_cannot_be_stored_is_answered_runtime_error_and_decided_again_later(
        team: Team, monkeypatch, caplog) -> None:
    def broken(_outcome, _reply):
        raise RuntimeError("the database refused the write")

    monkeypatch.setattr(team.repository, "save_command_refusal", broken)
    with caplog.at_level(logging.ERROR):
        answered = create(team.memory, "Deploy on Fridays.", bank="coremem", person=BO, request_id="cmd-bo")
    assert answered["outcome"] == "runtime_error"
    assert "event=team_memory_refusal_not_stored" in caplog.text
    monkeypatch.undo()
    assert stored_rows(team) == []
    # Nothing was stored, so the repeat is decided again: Bo is an administrator by then.
    record(team.access, BO, ("admin",), PersonStanding.ACTIVE)
    assert create(team.memory, "Deploy on Fridays.", bank="coremem", person=BO, request_id="cmd-bo")["outcome"] == "ok"


def test_a_repeat_gets_its_stored_reply_before_its_body_is_checked_again(team: Team, monkeypatch, caplog) -> None:
    first = create(team.memory, "Likes tea.", request_id="cmd-tea")

    def decided_again(*_args):
        raise AssertionError("a repeat answered from its stored reply is not decided again")

    monkeypatch.setattr(team.repository, "save_command_refusal", decided_again)
    with caplog.at_level(logging.WARNING):
        again = create(team.memory, "Likes tea.", request_id="cmd-tea", metadata_json="[]")
    assert again == first
    assert "event=operation_outcome_body_mismatch" in caplog.text
    monkeypatch.undo()
    # The same body as a new command is refused.
    assert create(team.memory, "Likes tea.", request_id="cmd-new", metadata_json="[]")["outcome"] == "invalid_request"
    assert listed(team.memory)["total"] == 1


@pytest.mark.parametrize("found", ["first look", "inside the write", "a refusal"])
def test_another_persons_command_under_a_used_request_id_is_refused_and_shown_nothing(
        team: Team, monkeypatch, found: str) -> None:
    memory = team.memory
    create(memory, "Keeps the spare key under the mat.", request_id="cmd-shared")
    if found != "first look":
        # As when two copies race: the first look finds nothing, and the store's own check (or the refusal's) does.
        monkeypatch.setattr(team.repository, "stored_command_outcome", lambda _outcome: None)
    bank = "coremem" if found == "a refusal" else "usermem"
    refused = create(memory, "Likes tea.", bank=bank, person=BO, request_id="cmd-shared")
    assert refused["outcome"] == "invalid_request" and "request id" in refused["message"]
    assert "spare key" not in str(refused) and "Ada" not in str(refused)
    assert fact_count(team) == 1 and texts(listed(memory)) == ["Keeps the spare key under the mat."]
    assert [row["person_id"] for row in stored_rows(team)] == ["Ada Admin"]


def test_an_unexpected_failure_is_answered_runtime_error_logged_and_never_stored(team: Team, monkeypatch,
                                                                                 caplog) -> None:
    real = team.repository.create_usermem

    def unreadable_once(**fields):
        monkeypatch.setattr(team.repository, "create_usermem", real)
        raise ValueError("Invalid isoformat string: 'yesterday'")  # a stored row the code cannot read

    monkeypatch.setattr(team.repository, "create_usermem", unreadable_once)
    with caplog.at_level(logging.ERROR):
        failed = create(team.memory, "Likes tea.", request_id="cmd-tea")
    assert failed["outcome"] == "runtime_error" and "yesterday" not in failed["message"]
    assert "event=team_memory_command_failed" in caplog.text
    assert stored_rows(team) == []
    assert create(team.memory, "Likes tea.", request_id="cmd-tea")["outcome"] == "ok"
    assert listed(team.memory)["total"] == 1


def test_a_change_and_its_stored_reply_are_one_transaction(team: Team, monkeypatch) -> None:
    def no_reply(_action, _result):
        raise RuntimeError("the reply could not be built")

    monkeypatch.setattr(memory_module, "_change_reply", no_reply)
    assert create(team.memory, "Likes tea.", request_id="cmd-tea")["outcome"] == "runtime_error"
    monkeypatch.undo()
    assert fact_count(team) == 0 and stored_rows(team) == []
    assert create(team.memory, "Likes tea.", request_id="cmd-tea")["outcome"] == "ok"
    assert fact_count(team) == 1


def test_clearing_an_expiry_and_naming_one_together_is_refused(memory: MemoryOperations) -> None:
    create(memory, "Travels in May.", expires="2027-06-01T00:00:00.000Z")
    both = {"bank": "usermem", "memory_id": 1, "clear_expiry": True, "expires": "2027-07-01T00:00:00.000Z"}
    refused = ask(memory, "memory.replace", both, request_id="cmd-both")
    assert refused["outcome"] == "invalid_request" and "not both" in refused["message"]
    assert listed(memory)["facts"][0]["expires"] == "2027-06-01T00:00:00.000Z"


def test_a_stored_time_without_a_time_zone_is_read_as_utc(team: Team) -> None:
    create(team.memory, "Office move.", valid_from="2026-01-01T09:00:00.000Z", expires="2027-01-01T00:00:00.000Z")
    # As imports before 0.10 wrote them.
    with database(team.storage) as (conn, p):
        conn.execute(f"UPDATE pinned_memory_facts_active SET valid_from_utc = {p}, invalid_after_utc = {p} "
                     f"WHERE memory_id = 1", ("2026-01-01T09:00:00", "2027-01-01T00:00:00"))
        conn.commit()
    (from_list,) = listed(team.memory)["facts"]
    from_show = ask(team.memory, "memory.show", {"bank": "usermem", "memory_id": 1}, request_id="cmd-show")["fact"]
    for fact in (from_list, from_show):
        assert (fact["valid_from"], fact["expires"]) == ("2026-01-01T09:00:00.000Z", "2027-01-01T00:00:00.000Z")


# -- who a command acts for -----------------------------------------------------------------------------------

def test_a_command_token_presented_by_another_host_than_its_signer_refuses_the_request(team: Team, caplog) -> None:
    body = {"request_id": "cmd-1", "bank": "usermem", "fact_text": inline("Likes tea.")}
    verified = stand_in_frame(TEAM, "memory.create", body, person_ref=ADA, request_id="cmd-1",
                              token_signer="other-host")
    with caplog.at_level(logging.WARNING), pytest.raises(FrameRefused) as refused:
        team.memory.answer(verified)
    assert (refused.value.code, refused.value.known_at) == ("bad_authority", "/authority")
    assert "event=team_command_token_wrong_sender" in caplog.text
    assert fact_count(team) == 0 and stored_rows(team) == []


@pytest.mark.parametrize("person, frame", [
    pytest.param(ADA, {"sender_roles": frozenset({"lead_host", "worker_host"})}, id="sender-not-the-gateway"),
    pytest.param(ADA, {"binding": "root_task"}, id="token-not-bound-to-a-command"),
    pytest.param("p-NobodyNobodyNobodyNob", {}, id="unknown-person"),
    pytest.param(DI, {}, id="removed-person"),
])
def test_a_change_without_an_active_person_from_the_gateway_is_refused(team: Team, caplog, person: str,
                                                                       frame: dict) -> None:
    with caplog.at_level(logging.WARNING):
        refused = ask(team.memory, "memory.create", {"bank": "usermem", "fact_text": inline("Likes tea.")},
                      person=person, request_id="cmd-1", **frame)
    assert refused["outcome"] == "not_allowed"
    assert "event=team_command_person_refused" in caplog.text
    assert fact_count(team) == 0


# -- teams, threads and upkeep ----------------------------------------------------------------------------------

def test_two_teams_keep_their_stored_replies_apart(team: Team) -> None:
    eve = "p-EveEveEveEveEveEveEveE"
    record(team.access, eve, ("admin",), PersonStanding.ACTIVE, team="team_two", person_id="Eve Admin")
    other = MemoryOperations(create_pinned_memory_repository(team.storage), PeopleDirectory(team.access, "team_two"),
                             {}, team.clock, logging.getLogger("test"))
    first = create(team.memory, "Likes tea.", request_id="cmd-same")
    body = {"request_id": "cmd-same", "bank": "usermem", "fact_text": inline("Likes coffee.")}
    second = checked_reply("memory.create", other.answer(stand_in_frame("team_two", "memory.create", body,
                                                                        person_ref=eve, request_id="cmd-same")))
    assert (first["outcome"], second["outcome"]) == ("ok", "ok")
    assert second["fact"]["fact_text"]["text"] == "Likes coffee." and fact_count(team) == 2
    assert sorted((row["team_id"], row["person_id"]) for row in stored_rows(team)) == [
        ("team_one", "Ada Admin"), ("team_two", "Eve Admin")]


@pytest.mark.asyncio
async def test_a_command_is_answered_in_a_worker_thread_not_on_the_event_loop(memory: MemoryOperations,
                                                                            monkeypatch) -> None:
    threads: list[int] = []

    def answer(_verified):
        threads.append(threading.get_ident())
        return {"outcome": "ok", "facts": [], "total": 0}

    monkeypatch.setattr(memory, "answer", answer)
    body = {"request_id": "cmd-list", "bank": "usermem", "sort": "memory_id", "offset": 0, "limit": 5}
    await memory.handle(stand_in_frame(TEAM, "memory.list", body, person_ref=ADA, request_id="cmd-list"))
    assert len(threads) == 1 and threads[0] != threading.get_ident()


def test_upkeep_deletes_replies_older_than_seven_days_when_nothing_new_is_stored(team: Team, caplog) -> None:
    team.clock.now = NOW - timedelta(days=8)
    create(team.memory, "Old.", request_id="cmd-old")
    team.clock.now = NOW - timedelta(days=6)
    create(team.memory, "Newer.", request_id="cmd-newer")
    assert len(stored_rows(team)) == 2
    team.clock.now = NOW
    with caplog.at_level(logging.INFO):
        asyncio.run(team.memory.purge_outcomes())
    assert [row["stored_at_utc"] for row in stored_rows(team)] == [(NOW - timedelta(days=6)).isoformat()]
    assert "event=team_memory_outcomes_purged count=1" in caplog.text


def test_removing_a_person_deletes_the_stored_replies_of_their_commands_only(team: Team) -> None:
    from storage.person_departure_repository import PersonDepartureRepository

    create(team.memory, "Likes tea.", request_id="cmd-ada")
    create(team.memory, "Likes coffee.", person=BO, request_id="cmd-bo")
    create(team.memory, "Deploy on Fridays.", bank="coremem", person=BO, request_id="cmd-bo-refused")
    assert sorted(row["person_id"] for row in stored_rows(team)) == ["Ada Admin", "Bo Member", "Bo Member"]

    deleted = PersonDepartureRepository(team.storage).delete_person_records("Bo Member")

    assert deleted[OUTCOMES_TABLE] == 2
    assert [row["person_id"] for row in stored_rows(team)] == ["Ada Admin"]


# -- the team lead's memory settings ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_commands_use_the_team_leads_own_memory_settings(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig
    from runtime_settings.agent_types import AgentTypeRuntimeSettings
    from team_services.process import team_operations

    config = GatewayInstanceConfig.create(instance_name="lead_limits", instance_root=str(tmp_path / "instance"),
                                          team_id=TEAM, timezone="UTC")
    # The team-wide default allows 200 team facts; the team lead's agent type allows one.
    config.agent_types[config.gateway.named_agent_type] = AgentTypeRuntimeSettings.model_validate(
        {"agent": {"memory": {"coremem": {"max_facts_per_team": 1}}}})
    config.save()
    record(AccessRepository(config.manager_storage_settings), ADA, ("admin",), PersonStanding.ACTIVE)
    family = next(family for family in team_operations(config, lambda: NOW) if "memory.create" in family.handlers)

    async def create_team_fact(text: str, request_id: str) -> dict:
        body = {"request_id": request_id, "bank": "coremem", "fact_text": inline(text)}
        verified = stand_in_frame(TEAM, "memory.create", body, person_ref=ADA, request_id=request_id)
        return checked_reply("memory.create", await family.handlers["memory.create"](verified))

    assert (await create_team_fact("Deploy on Fridays.", "cmd-1"))["outcome"] == "ok"
    assert (await create_team_fact("Smoke tests first.", "cmd-2"))["outcome"] == "capacity_reached"
