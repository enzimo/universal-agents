"""People's memory commands at the team services (team_services.memory, storage.operation_outcomes).

Real SQLite stores; frames are stand-ins with the contract's retry keys
(tests/team_services_support.py), and every body and reply is checked against the
contract's models. tests/test_live_team_memory.py runs the signed path over NATS.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from access_control.people_directory import PeopleDirectory
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from storage.config import resolve_storage_settings
from storage.operation_outcomes import OUTCOMES_TABLE
from storage.pinned_memory_repository import create_pinned_memory_repository
from team_services.memory import MemoryOperations
from tests.people_directory_support import access_store_at
from tests.team_services_support import checked_reply, stand_in_frame
from tests.test_pinned_memory_stale_edits import BACKENDS
from tests.test_pinned_memory_stale_edits import _storage as memory_storage

TEAM = "team_one"
NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
ADA, BO, CY = "p-AdaAdaAdaAdaAdaAdaAdaA", "p-BoBoBoBoBoBoBoBoBoBoBo", "p-CyCyCyCyCyCyCyCyCyCyCy"


@pytest.fixture(params=BACKENDS)
def memory(request, tmp_path: Path) -> Iterator[MemoryOperations]:
    """On SQLite, and on PostgreSQL with ``LIVE_TEST_POSTGRES_DSN`` (its own schema per test)."""

    access = access_store_at(tmp_path)
    for person_id, reference, roles, standing in (
        ("Ada Admin", ADA, ("admin",), PersonStanding.ACTIVE),
        ("Bo Member", BO, ("member",), PersonStanding.ACTIVE),
        ("Cy Gone", CY, (), PersonStanding.DISABLED),
    ):
        access.record_person(TeamPersonV1(team_id=TEAM, person_id=person_id, person_ref=reference, standing=standing,
                                          roles=roles, changed_at_utc=NOW))
    with memory_storage(request.param, tmp_path) as storage:
        yield MemoryOperations(create_pinned_memory_repository(storage), PeopleDirectory(access, TEAM), {},
                               lambda: NOW, logging.getLogger("test"))


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
