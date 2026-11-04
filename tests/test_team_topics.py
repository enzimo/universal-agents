"""People's topic commands at the team services (team_services.topics).

Real SQLite stores; frames are stand-ins with the contract's retry keys
(tests/team_services_support.py), and every body and reply is checked against the
contract's models.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from access_control.people_directory import PeopleDirectory
from custom_agents.conversation_store import ConversationStore
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from storage.config import resolve_storage_settings
from storage.operation_outcomes import OperationOutcomeStore
from team_services.topics import TopicOperations
from tests.people_directory_support import access_store_at
from tests.team_services_support import checked_reply, stand_in_frame

TEAM = "team_topics"
ADA, BO = "p-AdaAdaAdaAdaAdaAdaAdaA", "p-BoBoBoBoBoBoBoBoBoBoBo"


class Team:
    def __init__(self, tmp_path: Path) -> None:
        storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
        access = access_store_at(tmp_path)
        now = datetime.now(timezone.utc)
        for person_id, reference in (("ada", ADA), ("bo", BO)):
            access.record_person(TeamPersonV1(team_id=TEAM, person_id=person_id, person_ref=reference,
                                              standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=now))
        self.store = ConversationStore(db_path=str(tmp_path / "manager.db"), storage_settings=storage,
                                       artifacts_dir=tmp_path / "artifacts")
        self.operations = TopicOperations(self.store, OperationOutcomeStore(storage), PeopleDirectory(access, TEAM),
                                          lambda: datetime.now(timezone.utc), logging.getLogger("test.topics"))

    def thread(self, conversation_id: str, owner: str, preview: str) -> None:
        self.store.ensure_thread(conversation_id=conversation_id, team_id=TEAM, owner_agent_id="lead_1",
                                 owner_agent_name="TeamArchitect", requester_name=owner, requester_id=owner,
                                 root_task_id=f"task_{conversation_id}", subject_preview=preview,
                                 owner_canonical_user_id=owner)

    def ask(self, kind: str, body: dict, *, person: str = ADA, request_id: str = "cmd-1") -> dict:
        frame = stand_in_frame(TEAM, kind, {"request_id": request_id, **body}, person_ref=person, request_id=request_id)
        return checked_reply(kind, self.operations.answer(frame))


@pytest.fixture
def team(tmp_path: Path) -> Team:
    return Team(tmp_path)


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
