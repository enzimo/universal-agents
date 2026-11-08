"""People's topic commands at the team services (team_services.topics).

Real SQLite stores; frames are stand-ins with the contract's retry keys
(tests/team_services_support.py), and every body and reply is checked against the
contract's models.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from access_control.people_directory import PeopleDirectory
from custom_agents.conversation_store import ConversationStore
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from storage.config import resolve_storage_settings
from team_services.topics import TopicOperations
from tests.people_directory_support import access_store_at
from tests.team_services_support import checked_reply, stand_in_frame
from tests.test_pinned_memory_stale_edits import BACKENDS, _storage
from storage.operation_outcomes import CommandOutcome
from topic_tags import TopicTagInputError

TEAM = "team_topics"
ADA, BO = "p-AdaAdaAdaAdaAdaAdaAdaA", "p-BoBoBoBoBoBoBoBoBoBoBo"


class Team:
    def __init__(self, tmp_path: Path, storage=None) -> None:
        storage = storage or resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
        access = access_store_at(tmp_path)
        now = datetime.now(timezone.utc)
        for person_id, reference in (("ada", ADA), ("bo", BO)):
            access.record_person(TeamPersonV1(team_id=TEAM, person_id=person_id, person_ref=reference,
                                              standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=now))
        self.store = ConversationStore(db_path=str(tmp_path / "manager.db"), storage_settings=storage,
                                       artifacts_dir=tmp_path / "artifacts")
        self.operations = TopicOperations(self.store, PeopleDirectory(access, TEAM),
                                          lambda: datetime.now(timezone.utc), logging.getLogger("test.topics"))

    def thread(self, conversation_id: str, owner: str, preview: str) -> None:
        self.store.ensure_thread(conversation_id=conversation_id, team_id=TEAM, owner_agent_id="lead_1",
                                 owner_agent_name="TeamArchitect", requester_name=owner, requester_id=owner,
                                 root_task_id=f"task_{conversation_id}", subject_preview=preview,
                                 owner_canonical_user_id=owner)

    def ask(self, kind: str, body: dict, *, person: str = ADA, request_id: str = "cmd-1") -> dict:
        frame = stand_in_frame(TEAM, kind, {"request_id": request_id, **body}, person_ref=person, request_id=request_id)
        return checked_reply(kind, self.operations.answer(frame))


@pytest.fixture(params=BACKENDS)
def team(tmp_path: Path, request):
    with _storage(request.param, tmp_path) as storage:
        yield Team(tmp_path, storage)


def test_a_person_tags_their_own_threads_and_finds_them_by_tag(team: Team) -> None:
    team.thread("conv_ada", "ada", "Car repair\n  quotes")
    team.thread("conv_bo", "bo", "Bo's car repair")

    tagged = team.ask("topics.tag_thread", {"conversation_id": "conv_ada", "tags": ["car-repair"], "private": False})
    not_hers = team.ask("topics.tag_thread", {"conversation_id": "conv_bo", "tags": ["car-repair"], "private": False},
                        request_id="cmd-2")
    found = team.ask("topics.find_threads", {"tags": ["car-repair"]}, request_id="cmd-3")
    listed = team.ask("topics.list_tags", {}, request_id="cmd-4")
    bo_found = team.ask("topics.find_threads", {"tags": ["car-repair"]}, person=BO, request_id="cmd-5")

    assert tagged == {"outcome": "ok", "applied_tags": ["car-repair"]}
    assert not_hers["outcome"] == "not_found"
    [match] = found["matches"]
    assert (match["conversation_id"], match["subject_preview"], match["matched_tags"]) == (
        "conv_ada", "Car repair quotes", ["car-repair"])
    assert [(tag["tag"], tag["thread_count"]) for tag in listed["tags"]] == [("car-repair", 1)]
    assert bo_found == {"outcome": "ok", "matches": []}


def test_a_retried_tag_command_gets_its_stored_reply(team: Team, caplog) -> None:
    team.thread("conv_ada", "ada", "Taxes")
    body = {"conversation_id": "conv_ada", "tags": ["taxes"], "private": True}
    first = team.ask("topics.tag_thread", body, request_id="cmd-tag")
    with caplog.at_level(logging.WARNING):
        again = team.ask("topics.tag_thread", {**body, "tags": ["other"]}, request_id="cmd-tag")

    assert again == first == {"outcome": "ok", "applied_tags": ["taxes"]}
    assert "event=operation_outcome_body_mismatch" in caplog.text
    assert [tag["tag"] for tag in team.ask("topics.list_tags", {}, request_id="cmd-list")["tags"]] == ["taxes"]


def test_a_person_the_team_does_not_know_gets_nothing(team: Team) -> None:
    team.thread("conv_ada", "ada", "Taxes")
    team.ask("topics.tag_thread", {"conversation_id": "conv_ada", "tags": ["taxes"], "private": False})

    stranger = team.ask("topics.list_tags", {}, person="p-NobodyNobodyNobodyNob", request_id="cmd-2")

    assert stranger["outcome"] == "runtime_error" and "does not know" in stranger["message"]


@pytest.mark.parametrize("failure", ["second_tag", "saved_reply"])
def test_all_tags_and_their_reply_roll_back_together(team: Team, monkeypatch, failure: str) -> None:
    team.thread("conv_ada", "ada", "Taxes")
    repository = team.store._repository
    if failure == "second_tag":
        original = repository._tag_thread_row
        def fail(conn, conversation_id, tag, *args):
            if tag == "second":
                raise OSError("database write failed")
            return original(conn, conversation_id, tag, *args)
        monkeypatch.setattr(repository, "_tag_thread_row", fail)
    else:
        original = CommandOutcome.save
        def fail(self, *args):
            original(self, *args)
            raise OSError("database commit failed")
        monkeypatch.setattr(CommandOutcome, "save", fail)
    body = {"conversation_id": "conv_ada", "tags": ["first", "second"], "private": True}
    reply = team.ask("topics.tag_thread", body)
    assert reply["outcome"] == "runtime_error"
    assert team.store.list_thread_tags("conv_ada") == []
    with repository._connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM protocol1_operation_outcomes").fetchone()["count"] == 0
    monkeypatch.undo()
    assert team.ask("topics.tag_thread", body)["outcome"] == "ok"
    assert [row["tag"] for row in team.store.list_thread_tags("conv_ada")] == ["first", "second"]


def test_retry_does_not_restore_older_tags_or_privacy(team: Team) -> None:
    team.thread("conv_ada", "ada", "Taxes")
    body = {"conversation_id": "conv_ada", "tags": ["taxes", "old"], "private": False}
    first = team.ask("topics.tag_thread", body)
    team.store.untag_thread(conversation_id="conv_ada", tag="old")
    team.ask("topics.tag_thread", {**body, "tags": ["taxes"], "private": True}, request_id="newer")
    assert team.ask("topics.tag_thread", body) == first
    [tag] = team.store.list_thread_tags("conv_ada")
    assert (tag["tag"], tag["canonical_user_id"]) == ("taxes", "ada")
    assert team.ask("topics.tag_thread", body, person=BO)["outcome"] == "invalid_request"


def test_a_final_refusal_is_repeated_even_when_the_thread_now_exists(team: Team) -> None:
    body = {"conversation_id": "conv_ada", "tags": ["taxes"], "private": False}
    refused = team.ask("topics.tag_thread", body)
    team.thread("conv_ada", "ada", "Taxes")
    assert team.ask("topics.tag_thread", body) == refused
    assert refused["outcome"] == "not_found"
    assert team.store.list_thread_tags("conv_ada") == []


def test_stored_long_tags_are_omitted_with_a_warning(team: Team, caplog) -> None:
    team.thread("conv_ada", "ada", "Taxes")
    team.store.tag_thread(conversation_id="conv_ada", tag="valid", team_id=TEAM)
    repository = team.store._repository
    # Seed the exact old persisted shape without passing through the new validator.
    with repository._write() as conn:
        repository._tag_thread_row(conn, "conv_ada", "x" * 65, TEAM, "", "manual", None)
    with caplog.at_level(logging.WARNING):
        listed = team.ask("topics.list_tags", {})
        assert [row["tag"] for row in team.store.list_thread_tags("conv_ada")] == ["valid"]
    assert [row["tag"] for row in listed["tags"]] == ["valid"]
    assert "event=topic_tags_omitted" in caplog.text and "limit=64" in caplog.text
    assert "x" * 65 not in caplog.text


def test_new_long_tags_and_oversized_lists_are_refused_before_any_write(team: Team) -> None:
    team.thread("conv_ada", "ada", "Taxes")
    for tags in (["first", "x" * 65], [f"tag-{i}" for i in range(21)], []):
        with pytest.raises(TopicTagInputError):
            team.store.tag_thread_with_tags(conversation_id="conv_ada", tags=tags)
        assert team.store.list_thread_tags("conv_ada") == []
    team.store.tag_thread(conversation_id="conv_ada", tag="x" * 64)
    assert len(team.store.list_thread_tags("conv_ada")) == 1


def test_a_commit_whose_acknowledgement_is_lost_is_answered_from_its_outcome(team: Team, monkeypatch, caplog) -> None:
    team.thread("conv_ada", "ada", "Taxes")
    original = team.store.tag_thread_with_tags
    def lost_ack(**kwargs):
        original(**kwargs)
        raise OSError("lost acknowledgement")
    monkeypatch.setattr(team.store, "tag_thread_with_tags", lost_ack)
    with caplog.at_level(logging.WARNING):
        reply = team.ask("topics.tag_thread", {"conversation_id": "conv_ada", "tags": ["taxes"], "private": True})
    assert reply == {"outcome": "ok", "applied_tags": ["taxes"]}
    assert "event=operation_outcome_found_after_failure" in caplog.text


def test_simultaneous_copies_store_one_reply(team: Team, monkeypatch) -> None:
    team.thread("conv_ada", "ada", "Taxes")
    barrier = Barrier(2)
    original = team.store.stored_command_outcome
    def read(outcome):
        result = original(outcome)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(team.store, "stored_command_outcome", read)
    body = {"conversation_id": "conv_ada", "tags": ["taxes"], "private": True}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: team.ask("topics.tag_thread", body), range(2)))
    assert results == [{"outcome": "ok", "applied_tags": ["taxes"]}] * 2
    with team.store._repository._connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM protocol1_operation_outcomes").fetchone()["count"] == 1
