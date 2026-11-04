"""The Web Chat Knowledge page at the team services (team_services.knowledge), through the gateway.

Each request runs the whole way except NATS: the gateway converts the browser's request
(assistant_gateway.application.knowledge_wire), the team services answer it on a real
SQLite store with a stand-in frame (tests/team_services_support.py, every body and reply
checked against the contract), and the gateway turns the reply into the browser's view
and names editors. tests/test_live_team_memory.py covers signing and verifying over NATS.

The person comes only from the command token's reference; ``personal`` is always that
person's own space; a person's team edit publishes directly; the never-stored rules and
review confirmation apply; a person publishes or withdraws only their own proposals; a
retried change is carried out once.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import pytest

from access_control.people_directory import PeopleDirectory
from assistant_gateway.application.knowledge_pages import GatewayKnowledgePages, GatewayKnowledgeRefused
from assistant_gateway.application.knowledge_wire import ARCHIVE_REASON, knowledge_request
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.team_commands import GatewayTeamCommandService
from assistant_gateway.team_services_client import AccessStorePersonReferences
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.control_v1 import (
    KnowledgeCommandAction,
    KnowledgeCommandPayload,
    KnowledgeCommandResultPayload,
    KnowledgeReviewFlagPayload,
)
from knowledge_base.service import DraftInput, knowledge_service_for
from knowledge_base.vocabulary import ContributionStatus
from runtime_settings.knowledge import knowledge_settings
from storage.config import resolve_storage_settings
from storage.knowledge_repository import create_knowledge_repository
from storage.pinned_memory_repository import create_pinned_memory_repository
from team_services.knowledge import KnowledgeOperations
from tests.knowledge_support import TEAM
from tests.knowledge_support import agent as agent_actor
from tests.people_directory_support import access_store_at
from tests.team_services_support import checked_reply, stand_in_frame

SECRET = "hunter2-Correct-Horse-9"
PEOPLE = ("alice", "bob", "carol")
A = KnowledgeCommandAction


def reference_of(person: str) -> str:
    return f"p-{person:_<22}"[:24]


@dataclass
class Answer:
    """What the browser gets: the result, or the refusal's code, words and flags."""

    success: bool
    result: Optional[KnowledgeCommandResultPayload] = None
    error_code: str = ""
    error_message: str = ""
    review_flags: tuple[KnowledgeReviewFlagPayload, ...] = field(default=())
    current_revision: int = 0
    merged_into: str = ""

    def as_json(self) -> str:
        return json.dumps({"result": self.result.model_dump() if self.result else None, "message": self.error_message,
                           "flags": [flag.model_dump() for flag in self.review_flags]})


class _Ids:
    def new_correlation_id(self) -> str:
        return f"cmd-{uuid4().hex}"


class _InProcess:
    """The team services' port, answering with the knowledge handlers in process."""

    def __init__(self, operations: KnowledgeOperations) -> None:
        self.operations = operations

    async def command(self, kind: str, body: dict, *, person_ref: str, request_id: str) -> dict:
        return checked_reply(kind, self.operations.answer(
            stand_in_frame(TEAM, kind, body, person_ref=person_ref, request_id=request_id)))


class Team:
    def __init__(self, tmp_path: Path, *, enabled: bool = True) -> None:
        storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
        access = access_store_at(tmp_path)
        now = datetime.now(timezone.utc)
        for person in PEOPLE:
            access.record_person(TeamPersonV1(team_id=TEAM, person_id=person, person_ref=reference_of(person),
                                              standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=now))
        self.repository = create_knowledge_repository(storage)
        memory = create_pinned_memory_repository(storage)
        settings = knowledge_settings({"enabled": enabled})
        self.operations = KnowledgeOperations(self.repository if enabled else None, PeopleDirectory(access, TEAM),
                                              settings, lambda: datetime.now(timezone.utc),
                                              logging.getLogger("test.team_knowledge"), personal_memory=memory)
        self.service = knowledge_service_for(self.repository, settings=knowledge_settings({}), personal_memory=memory)
        references = AccessStorePersonReferences(access.settings, TEAM)
        self.pages = GatewayKnowledgePages(
            team_commands=GatewayTeamCommandService(team_services=_InProcess(self.operations), references=references,
                                                    id_generator=_Ids()),
            references=references, display_name=lambda person_id: f"Name of {person_id}", enabled=enabled,
        )

    async def send(self, person: str, **fields: Any) -> Answer:
        context = GatewayRouteContextV1(session_key=f"website:{person}", channel_name="website", user_id=person,
                                        chat_id="web-chat", canonical_user_id=person)
        try:
            result = await self.pages.run(context, KnowledgeCommandPayload(**fields))
        except GatewayKnowledgeRefused as refused:
            return Answer(False, error_code=refused.code, error_message=refused.message,
                          review_flags=refused.review_flags, current_revision=refused.current_revision,
                          merged_into=refused.merged_into)
        return Answer(True, result)

    def write(self, person: str, space: str = "team", **fields: Any):
        return self.send(person, action=A.WRITE, space=space, **fields)

    def worker(self, name: str, *, person: str = "", agent_type: str = "Researcher"):
        return replace(agent_actor(name, person=person, agent_type=agent_type), team_id=TEAM)


@pytest.fixture
def team(tmp_path: Path) -> Team:
    return Team(tmp_path)


@pytest.mark.asyncio
async def test_a_person_the_team_does_not_know_changes_nothing(team: Team) -> None:
    kind, body = knowledge_request(KnowledgeCommandPayload(action=A.WRITE, title="Deploys", body="After smoke tests."))
    reply = checked_reply(kind, team.operations.answer(stand_in_frame(
        TEAM, kind, {"request_id": "cmd-1", **body}, person_ref="p-NobodyNobodyNobodyNob", request_id="cmd-1")))

    assert reply["outcome"] == "not_allowed"
    assert (await team.send("alice", action=A.LIST)).result.total == 0


@pytest.mark.asyncio
async def test_personal_pages_belong_to_the_person_the_token_names(team: Team) -> None:
    saved = await team.write("alice", "personal", title="Travel", body="Window seats.")
    assert saved.success is True and saved.result.page.space == "personal"

    alice = await team.send("alice", action=A.READ, space="personal", page="travel")
    bob = await team.send("bob", action=A.READ, space="personal", page="travel")
    bob_list = await team.send("bob", action=A.LIST, space="personal")

    assert alice.result.page.body == "Window seats."
    assert (bob.success, bob.error_code) == (False, "not_found")
    assert bob_list.result.total == 0 and bob_list.result.pages == []


@pytest.mark.asyncio
async def test_a_persons_team_edit_publishes_directly_and_editors_are_named(team: Team) -> None:
    created = await team.write("alice", title="Release process", body="Tag, then publish.")
    edited = await team.write("bob", page="release-process", base_revision=created.result.page.revision,
                              title="Release process", body="Tag, test, then publish.")
    history = await team.send("alice", action=A.HISTORY, page="release-process")

    assert edited.success is True and edited.result.page.revision == 2
    assert edited.result.page.body == "Tag, test, then publish."
    assert edited.result.page.last_editor.kind == "person" and edited.result.page.last_editor.label == "You"
    assert [revision.editor.label for revision in history.result.revisions] == ["Name of bob", "You"]
    assert reference_of("bob") not in history.as_json() and "bob" not in edited.as_json()
    pending = team.repository.list_contributions(team_id=TEAM, owner_person_id="bob",
                                                 status=ContributionStatus.PENDING, limit=10)
    assert pending == []


@pytest.mark.asyncio
async def test_a_stale_base_revision_is_a_conflict_and_saves_nothing(team: Team) -> None:
    created = await team.write("alice", title="Runbook", body="Restart the worker.")
    await team.write("bob", page="runbook", base_revision=1, title="Runbook", body="Restart both workers.")

    stale = await team.write("alice", page="runbook", base_revision=created.result.page.revision, body="Mine.")
    taken = await team.write("carol", title="Runbook", body="Another runbook.")

    assert (stale.success, stale.error_code, stale.current_revision) == (False, "conflict", 2)
    assert (taken.success, taken.error_code) == (False, "page_exists")
    current = await team.send("alice", action=A.READ, page="runbook")
    assert current.result.page.body == "Restart both workers."


@pytest.mark.asyncio
async def test_never_stored_content_is_refused_by_field_and_place_without_its_value(team: Team) -> None:
    refused = await team.write("alice", title="Database", body=f"Host db1.\npassword: {SECRET}")
    assert (refused.success, refused.error_code) == (False, "sensitive_content")
    assert [(flag.field, flag.line) for flag in refused.review_flags] == [("body", 2)]
    assert SECRET not in refused.as_json()


@pytest.mark.asyncio
async def test_content_held_for_review_needs_the_persons_confirmation(team: Team) -> None:
    held = await team.write("alice", title="Contacts", body="Ask the vendor.\nWrite to ops@example.com")
    confirmed = await team.write("alice", title="Contacts", body="Ask the vendor.\nWrite to ops@example.com",
                                 confirm_review=True)

    assert (held.success, held.error_code) == (False, "review_required")
    assert [(flag.kind, flag.field, flag.line, flag.column) for flag in held.review_flags] == [
        ("email_address", "body", 2, 10)
    ]
    assert "ops@example.com" not in held.as_json()
    assert "Everyone on the team will see it" in held.error_message
    assert confirmed.success is True
    assert [flag.kind for flag in confirmed.result.review_flags] == ["email_address"]


@pytest.mark.asyncio
async def test_notes_archive_and_revert_keep_every_revision(team: Team) -> None:
    await team.write("alice", title="On call", body="Page the lead.")
    await team.write("alice", page="on-call", base_revision=1, body="Page the lead, then the backup.")
    noted = await team.send("bob", action=A.ADD_NOTE, page="on-call", note="Add the phone tree.")
    archived = await team.send("bob", action=A.ARCHIVE, page="on-call", base_revision=2)
    reverted = await team.send("alice", action=A.REVERT, page="on-call", revision=1, base_revision=3)
    history = await team.send("alice", action=A.HISTORY, page="on-call")

    assert [note.body for note in noted.result.page.open_notes] == ["Add the phone tree."]
    assert noted.result.page.open_notes[0].editor.label == "You"
    assert archived.result.page.status == "archived"
    assert reverted.result.page.status == "active" and reverted.result.page.body == "Page the lead."
    assert [revision.change_kind for revision in history.result.revisions] == ["revert", "archive", "edit", "create"]
    assert history.result.revisions[1].change_summary == ARCHIVE_REASON
    assert history.result.revisions[0].change_summary == "Reverted to revision 1"


@pytest.mark.asyncio
async def test_a_person_publishes_and_withdraws_only_their_own_proposals(team: Team) -> None:
    await team.write("alice", title="Deploys", body="Deploy on Tuesdays.")
    # An agent working for alice: its team edits wait for her as proposals.
    worker = team.worker("worker_1", person="alice")
    edit = team.service.write(worker, "team/deploys", DraftInput(title="Deploys", body="Deploy on Tuesdays and Fridays."),
                              base_revision=1).contribution
    note = team.service.add_note(worker, "team/deploys", "Fridays need two reviewers.").contribution

    listed = await team.send("alice", action=A.LIST_CONTRIBUTIONS)
    shown = await team.send("alice", action=A.READ_CONTRIBUTION, contribution_id=edit.contribution_id)
    bob_read = await team.send("bob", action=A.READ_CONTRIBUTION, contribution_id=edit.contribution_id)
    bob_withdraw = await team.send("bob", action=A.WITHDRAW_CONTRIBUTION, contribution_id=edit.contribution_id)
    published = await team.send(
        "alice", action=A.PUBLISH_CONTRIBUTION, contribution_id=edit.contribution_id, edited=True, title="Deploys",
        body="Deploy on Tuesdays and Fridays (with review).", base_revision=shown.result.contribution.current.revision,
    )
    withdrawn = await team.send("alice", action=A.WITHDRAW_CONTRIBUTION, contribution_id=note.contribution_id)

    assert listed.result.total == 2 and all(item.body == "" for item in listed.result.contributions)
    assert shown.result.contribution.current.body == "Deploy on Tuesdays."
    assert shown.result.contribution.body == "Deploy on Tuesdays and Fridays."
    assert shown.result.contribution.agent_type == "Researcher"
    assert (bob_read.error_code, bob_withdraw.error_code) == ("not_found", "not_found")
    assert published.success is True
    assert published.result.page.body == "Deploy on Tuesdays and Fridays (with review)."
    assert withdrawn.success is True and withdrawn.result.contribution.contribution_id == note.contribution_id
    assert (await team.send("alice", action=A.LIST_CONTRIBUTIONS)).result.total == 0


@pytest.mark.asyncio
async def test_the_page_list_pages_twenty_at_a_time_with_a_total(team: Team) -> None:
    for number in range(23):
        await team.write("alice", title=f"Guide {number:02d}", body=f"Guide number {number}.")

    first = await team.send("bob", action=A.LIST)
    last = await team.send("bob", action=A.LIST, offset=20)
    await team.write("alice", title="Kubernetes upgrades", body="Drain nodes first.")
    found = await team.send("bob", action=A.LIST, query="number")
    one = await team.send("bob", action=A.LIST, query="drain nodes")

    assert (len(first.result.pages), first.result.total) == (20, 23)
    assert (len(last.result.pages), last.result.total) == (3, 23)
    assert (len(found.result.pages), found.result.total) == (20, 23)
    assert [page.page for page in one.result.pages] == ["kubernetes-upgrades"] and one.result.total == 1
    assert "[" in one.result.pages[0].snippet


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [A.LIST, A.COUNT_CONTRIBUTIONS])
async def test_with_the_knowledge_base_off_requests_are_refused_plainly(tmp_path: Path, action) -> None:
    off = await Team(tmp_path, enabled=False).send("alice", action=action)

    assert (off.success, off.error_code) == (False, "knowledge_off")
    assert "turned off" in off.error_message


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["note", "create"])
async def test_edited_proposals_publish_only_the_persons_redacted_text(team: Team, kind) -> None:
    worker = team.worker("worker_1", person="alice")
    if kind == "note":
        await team.write("alice", title="Runbook", body="Check the workers.")
        proposed = team.service.add_note(worker, "team/runbook", "Email alice@example.com.")
    else:
        proposed = team.service.write(
            worker, "team/runbook", DraftInput(title="Runbook", body="Check the workers.", summary="Email alice@example.com.")
        )
    bob = await team.send("bob", action=A.PUBLISH_CONTRIBUTION, contribution_id=proposed.contribution.contribution_id,
                          edited=True, body="Contact the on-call lead.", summary="")
    assert (bob.success, bob.error_code) == (False, "not_found")
    published = await team.send(
        "alice", action=A.PUBLISH_CONTRIBUTION, contribution_id=proposed.contribution.contribution_id, edited=True,
        title="Runbook", body="Contact the on-call lead.", summary="", base_revision=1 if kind == "note" else 0,
    )
    assert published.success is True
    viewed = await team.send("bob", action=A.READ, page="runbook")
    assert "alice@example.com" not in viewed.as_json()
    if kind == "note":
        assert [note.body for note in viewed.result.page.open_notes] == ["Contact the on-call lead."]
    else:
        assert viewed.result.page.body == "Contact the on-call lead." and viewed.result.page.summary == ""


@pytest.mark.asyncio
async def test_a_summary_is_preserved_when_omitted_and_cleared_when_empty(team: Team) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.", summary="Worker maintenance.")
    kept = await team.write("alice", page="runbook", base_revision=1, body="Check all workers.")
    cleared = await team.write("alice", page="runbook", base_revision=2, body="Check all workers.", summary="")
    assert kept.result.page.summary == "Worker maintenance."
    assert cleared.success is True and cleared.result.page.summary == ""


@pytest.mark.asyncio
async def test_an_empty_edited_note_does_not_publish_the_original(team: Team) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.")
    proposed = team.service.add_note(team.worker("worker_1", person="alice"), "team/runbook",
                                     "Email alice@example.com.").contribution
    refused = await team.send("alice", action=A.PUBLISH_CONTRIBUTION, contribution_id=proposed.contribution_id,
                              edited=True, body="", base_revision=1)
    assert (refused.success, refused.error_code) == (False, "invalid_request")
    viewed = await team.send("alice", action=A.READ, page="runbook")
    assert viewed.result.page.open_notes == []
    assert (await team.send("alice", action=A.LIST_CONTRIBUTIONS)).result.total == 1


@pytest.mark.asyncio
async def test_an_archive_proposal_archives_the_current_page_without_replacing_its_text(team: Team) -> None:
    await team.write("alice", title="Runbook", body="Old instructions.")
    proposed = team.service.archive(team.worker("worker_1", person="alice"), "team/runbook", base_revision=1,
                                    reason="Retired runbook.").contribution
    await team.write("bob", page="runbook", base_revision=1, title="Updated runbook", body="New instructions.",
                     summary="Current maintenance.", tags=["current"])
    published = await team.send("alice", action=A.PUBLISH_CONTRIBUTION, contribution_id=proposed.contribution_id,
                                base_revision=2)
    assert published.success is True
    page = published.result.page
    assert (page.revision, page.status, page.title, page.body, page.summary, page.tags) == (
        3, "archived", "Updated runbook", "New instructions.", "Current maintenance.", ["current"],
    )


@pytest.mark.asyncio
async def test_archived_pages_can_be_found_and_restored_from_a_new_list_request(team: Team) -> None:
    await team.write("alice", "personal", title="Runbook", body="Check the workers.")
    await team.send("alice", action=A.ARCHIVE, space="personal", page="runbook", base_revision=1)
    current = await team.send("alice", action=A.LIST, space="personal")
    archived = await team.send("alice", action=A.LIST, space="personal", status="archived", query="workers")
    bob = await team.send("bob", action=A.LIST, space="personal", status="archived")
    assert current.result.total == bob.result.total == 0
    assert archived.result.total == 1
    [page] = archived.result.pages
    restored = await team.write("alice", "personal", page=page.page, base_revision=page.revision,
                                body="Check the workers.")
    assert restored.success is True and restored.result.page.status == "active"


@pytest.mark.asyncio
async def test_all_pending_proposals_can_be_paged_without_crossing_owner_boundaries(team: Team) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.")
    proposed_ids = set()
    for number in range(101):
        worker = team.worker(f"worker_{number}", person="alice")
        proposed_ids.add(team.service.add_note(worker, "team/runbook", f"Check number {number}.").contribution.contribution_id)
    team.service.add_note(team.worker("bob_worker", person="bob"), "team/runbook", "Bob's own proposal.")
    seen = []
    for offset in range(0, 101, 20):
        result = await team.send("alice", action=A.LIST_CONTRIBUTIONS, offset=offset)
        assert result.success is True and result.result.total == 101
        assert all(item.body == "" for item in result.result.contributions)
        seen.extend(item.contribution_id for item in result.result.contributions)
    assert len(seen) == 101 and set(seen) == proposed_ids


@pytest.mark.asyncio
async def test_links_merged_names_and_merge_history_reach_the_page(team: Team) -> None:
    await team.write("alice", title="CI pipeline", body="Pushes run the fast suite.")
    await team.write("alice", title="Old CI notes", body="Nightly runs too.")
    await team.write("alice", title="Release process",
                     body="See [[ci-pipeline]], [[Old CI notes|the old notes]], [[team/missing-page]] and `[[in-code]]`.")
    # The curator's team run merges the old notes into the pipeline page.
    team.service.merge(
        team.worker("curator_1", agent_type="KnowledgeCuratorAgent"), "team/old-ci-notes", "team/ci-pipeline",
        DraftInput(title="CI pipeline", body="Pushes run the fast suite. Nightly runs too."),
        base_revision=1, source_revision=1,
    )

    page = await team.send("bob", action=A.READ, page="release-process")
    via_old_name = await team.send("bob", action=A.READ, page="old-ci-notes")
    taken = await team.write("bob", title="Old CI notes", body="A new page under the merged name.")
    history = await team.send("bob", action=A.HISTORY, page="ci-pipeline")
    listed = await team.send("bob", action=A.LIST)

    assert [(link.written, link.space, link.page, link.exists) for link in page.result.page.links] == [
        ("ci-pipeline", "team", "ci-pipeline", True),
        ("Old CI notes", "team", "old-ci-notes", True),
        ("team/missing-page", "team", "missing-page", False),
        ("in-code", "team", "in-code", False),
    ]
    assert via_old_name.result.page.page == "ci-pipeline"
    assert via_old_name.result.page.redirected_from == "team/old-ci-notes"
    assert (taken.success, taken.error_code, taken.merged_into) == (False, "merged", "team/ci-pipeline")
    assert history.result.revisions[0].change_kind == "merge"
    assert history.result.revisions[0].editor.label == "KnowledgeCuratorAgent"
    # The merged source is archived: lists and totals leave it out.
    assert sorted(item.page for item in listed.result.pages) == ["ci-pipeline", "release-process"]
    assert listed.result.total == 2


@pytest.mark.asyncio
async def test_the_proposal_count_is_the_persons_waiting_proposals_only(team: Team) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.")
    alice_worker = team.worker("worker_a", person="alice")
    first = team.service.add_note(alice_worker, "team/runbook", "Check the queue first.").contribution
    team.service.add_note(alice_worker, "team/runbook", "Then check the workers.")
    team.service.add_note(team.worker("worker_b", person="bob"), "team/runbook", "Bob's own proposal.")

    alice = await team.send("alice", action=A.COUNT_CONTRIBUTIONS)
    carol = await team.send("carol", action=A.COUNT_CONTRIBUTIONS)
    await team.send("alice", action=A.WITHDRAW_CONTRIBUTION, contribution_id=first.contribution_id)
    after = await team.send("alice", action=A.COUNT_CONTRIBUTIONS)

    assert (alice.success, alice.result.total, alice.result.contributions) == (True, 2, [])
    assert carol.result.total == 0
    assert after.result.total == 1


def _answer(team: Team, payload: KnowledgeCommandPayload, *, person: str = "alice", request_id: str) -> dict:
    kind, body = knowledge_request(payload)
    return checked_reply(kind, team.operations.answer(stand_in_frame(
        TEAM, kind, {"request_id": request_id, **body}, person_ref=reference_of(person), request_id=request_id)))


@pytest.mark.asyncio
async def test_a_retried_change_is_carried_out_once_and_answered_alike(team: Team) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.")
    edit = KnowledgeCommandPayload(action=A.WRITE, page="runbook", base_revision=1, body="Check all workers.")
    first = _answer(team, edit, request_id="cmd-edit")
    # The page is now at revision 2: without the stored reply, the retry would be stale.
    again = _answer(team, edit, request_id="cmd-edit")
    note = KnowledgeCommandPayload(action=A.ADD_NOTE, page="runbook", note="Check the queue too.")
    noted = _answer(team, note, request_id="cmd-note")
    noted_again = _answer(team, note, request_id="cmd-note")

    assert first["outcome"] == "ok" and again == first
    assert noted_again == noted and [n["note_id"] for n in noted["page"]["open_notes"]] == [noted["note"]["note_id"]]
    read = await team.send("alice", action=A.READ, page="runbook")
    assert read.result.page.revision == 2 and len(read.result.page.open_notes) == 1


@pytest.mark.asyncio
async def test_a_change_whose_reply_was_not_stored_yet_is_answered_from_its_receipt(team: Team, monkeypatch) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.")
    edit = KnowledgeCommandPayload(action=A.WRITE, page="runbook", base_revision=1, body="Check all workers.")
    # The process stops after the change commits and before its full reply is stored: only the receipt is kept.
    monkeypatch.setattr(team.repository, "complete_command_outcome", lambda *_args: None)
    first = _answer(team, edit, request_id="cmd-edit")
    monkeypatch.undo()
    retried = _answer(team, edit, request_id="cmd-edit")

    assert first["outcome"] == "ok"
    assert retried["page"]["content"]["text"] == "Check all workers." and retried["page"]["seen_version"] == "2"
    assert (await team.send("alice", action=A.READ, page="runbook")).result.page.revision == 2
    # The rebuilt reply is now stored.
    assert _answer(team, edit, request_id="cmd-edit") == retried
