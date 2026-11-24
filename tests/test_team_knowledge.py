"""The Web Chat Knowledge page at the team services (team_services.knowledge), through the gateway.

Each request runs the whole way except NATS: the gateway converts the browser's request
(assistant_gateway.application.knowledge_wire), the team services answer it on a real
SQLite store with a stand-in frame (tests/team_services_support.py, every body and reply
checked against the contract), and the gateway turns the reply into the browser's view
and names editors. tests/test_live_team_memory.py covers signing and verifying over NATS.

The person comes only from the command token's reference; ``personal`` is always that
person's own space; a person's team edit publishes directly; the never-stored rules and
review confirmation apply; a person publishes or withdraws only their own proposals; a
retried change is carried out once; knowledge longer than Web Chat carries is refused
plainly on every path and nothing stored changes (on SQLite and PostgreSQL).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
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
from custom_transport.protocol1.knowledge_blobs import ClientBlobStore
from knowledge_base.service import DraftInput, knowledge_service_for
from knowledge_base.vocabulary import ContributionStatus
from runtime_settings.knowledge import knowledge_settings
from storage.blob_index_repository import BlobIndexRepository
from storage.config import StorageSettings, resolve_storage_settings
from storage.knowledge_repository import create_knowledge_repository
from storage.pinned_memory_repository import create_pinned_memory_repository
from storage.operation_outcomes import is_receipt
from team_services.blobs import BlobOperations, BlobService
from team_services.outcomes import AnswerUnknown
from team_services.knowledge import (
    KNOWLEDGE_KINDS,
    KnowledgeOperations,
)
from tests.knowledge_support import BACKENDS, TEAM, knowledge_storage
from tests.knowledge_support import agent as agent_actor
from tests.people_directory_support import access_store_at
from tests.team_services_support import checked_reply, stand_in_frame
from tests.test_protocol1_blobs import FakeJetStream

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


class _InProcess:
    """The team services' port, answering with the knowledge handlers in process."""

    def __init__(self, operations: KnowledgeOperations) -> None:
        self.operations = operations

    async def command(self, kind: str, body: dict, *, person_ref: str, request_id: str) -> dict:
        reply = await self.operations.handle(
            stand_in_frame(TEAM, kind, body, person_ref=person_ref, request_id=request_id)
        )
        return checked_reply(kind, reply)


class Team:
    """The team services and the gateway on one store; ``knowledge`` is the shared ``agent.knowledge`` setting."""

    def __init__(self, tmp_path: Path, *, enabled: bool = True, storage: Optional[StorageSettings] = None,
                 knowledge: Optional[dict] = None) -> None:
        storage = storage or resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
        access = self.access = access_store_at(tmp_path)
        now = datetime.now(timezone.utc)
        for person in PEOPLE:
            access.record_person(TeamPersonV1(team_id=TEAM, person_id=person, person_ref=reference_of(person),
                                              standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=now))
        self.repository = create_knowledge_repository(storage)
        memory = create_pinned_memory_repository(storage)
        settings = knowledge_settings({**(knowledge or {}), "enabled": enabled})
        people_dir = PeopleDirectory(access, TEAM)
        self.js = FakeJetStream()
        self.blob_repository = BlobIndexRepository(storage)
        self.blob_service = BlobService(
            repository=self.blob_repository,
            people=people_dir,
            js=self.js,
            team_id=TEAM,
            clock=lambda: datetime.now(timezone.utc),
            logger=logging.getLogger("test.team_blobs"),
        )
        self.blob_ops = BlobOperations(self.blob_service, people_dir, logging.getLogger("test.team_blobs"))
        self.operations = KnowledgeOperations(
            self.repository if enabled else None,
            people_dir,
            settings,
            lambda: datetime.now(timezone.utc),
            logging.getLogger("test.team_knowledge"),
            personal_memory=memory,
            blobs=self.blob_service,
        )
        # Agents' side: the same settings (both read agent.knowledge).
        self.service = knowledge_service_for(self.repository, settings=knowledge_settings(knowledge or {}),
                                             personal_memory=memory)
        references = AccessStorePersonReferences(access.settings, TEAM)

        async def _get_js() -> FakeJetStream:
            return self.js

        async def _register_blob(reg_body: dict) -> dict:
            frame = stand_in_frame(TEAM, "blobs.register", reg_body, person_ref=None, request_id=reg_body["request_id"])
            return checked_reply("blobs.register", await self.blob_ops.handle_register(frame))

        self.client_blobs = ClientBlobStore(
            get_js=_get_js,
            team=TEAM,
            register=_register_blob,
            request_ids=lambda: f"blob-{uuid4().hex}",
        )
        self.pages = GatewayKnowledgePages(
            team_commands=GatewayTeamCommandService(team_services=_InProcess(self.operations), references=references,
                                                    request_ids=lambda: f"cmd-{uuid4().hex}"),
            references=references, display_name=lambda person_id: f"Name of {person_id}", enabled=enabled,
            blobs=self.client_blobs,
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
async def test_an_agent_type_longer_than_the_contract_carries_is_shown_as_agent(team: Team, caplog) -> None:
    """The runtime allows agent types of up to 64 characters until phase 3 (plan decision 56); the contract 24."""

    long_type = "ResearchAndDevelopmentSpecialist"
    worker = team.worker("worker_1", agent_type=long_type)
    team.service.write(worker, "team/findings", DraftInput(title="Findings", body="Checked the queue."))
    team.service.add_note(worker, "team/findings", "Check again tomorrow.")
    proposed = team.service.add_note(team.worker("worker_2", person="alice", agent_type=long_type), "team/findings",
                                     "Add the dashboards.").contribution
    with caplog.at_level(logging.WARNING, logger="test.team_knowledge"):
        read = await team.send("bob", action=A.READ, page="findings")
        history = await team.send("bob", action=A.HISTORY, page="findings")
        noted = await team.send("bob", action=A.ADD_NOTE, page="findings", note="Thanks.")
        proposals = await team.send("alice", action=A.LIST_CONTRIBUTIONS)
        proposal = await team.send("alice", action=A.READ_CONTRIBUTION, contribution_id=proposed.contribution_id)

    assert read.result.page.last_editor.label == "Agent"
    assert [note.editor.label for note in read.result.page.open_notes] == ["Agent"]
    assert [revision.editor.label for revision in history.result.revisions] == ["Agent"]
    assert noted.success is True and noted.result.page.revision == 1
    assert [item.agent_type for item in proposals.result.contributions] == ["Agent"]
    assert proposal.result.contribution.agent_type == "Agent"
    # Logged once per process, with the type, not on every read.
    assert caplog.text.count("event=team_knowledge_agent_type_not_shown") == 1 and long_type in caplog.text


@pytest.mark.asyncio
async def test_a_person_who_left_the_team_is_shown_as_a_team_member(team: Team) -> None:
    await team.write("carol", title="Runbook", body="Check the workers.")
    await team.send("carol", action=A.ADD_NOTE, page="runbook", note="Check the queue.")
    now = datetime.now(timezone.utc)
    # The gateway's people copy drops a person removed from the team.
    team.access.replace_people(TEAM, [
        TeamPersonV1(team_id=TEAM, person_id=person, person_ref=reference_of(person), standing=PersonStanding.ACTIVE,
                     roles=("member",), changed_at_utc=now) for person in ("alice", "bob")
    ], at=now)
    read = await team.send("bob", action=A.READ, page="runbook")
    history = await team.send("bob", action=A.HISTORY, page="runbook")

    assert read.result.page.last_editor.label == "A team member"
    assert [note.editor.label for note in read.result.page.open_notes] == ["A team member"]
    assert [revision.editor.label for revision in history.result.revisions] == ["A team member"]


@pytest.mark.asyncio
async def test_more_flagged_places_than_a_reply_carries_still_ask_for_confirmation(team: Team) -> None:
    body = "\n".join(f"Write to vendor{number}@example.com" for number in range(120))
    held = await team.write("alice", title="Vendors", body=body)
    saved = await team.write("alice", title="Vendors", body=body, confirm_review=True)

    assert (held.success, held.error_code, len(held.review_flags)) == (False, "review_required", 100)
    assert held.error_message.startswith("Only the first 100 of 120 flagged places are listed.")
    assert [(flag.field, flag.line) for flag in held.review_flags[:2]] == [("body", 1), ("body", 2)]
    assert saved.success is True and [flag.kind for flag in saved.result.review_flags] == ["email_address"]


@pytest.mark.asyncio
async def test_a_list_of_all_spaces_with_type_and_tag_filters(team: Team) -> None:
    await team.write("alice", title="Deploys", body="Deploy on Tuesdays.", tags=["ops"])
    team.service.write(team.worker("sys"), "team/rollback", DraftInput(title="Rollback", body="Undo the deploy.",
                                                                        page_type="how-to", tags=["ops"]))
    await team.write("alice", "personal", title="My checklist", body="Mine.", tags=["ops"])
    await team.write("bob", "personal", title="Bob's checklist", body="His.", tags=["ops"])

    def listed(**filters: Any) -> dict:
        body = {"request_id": f"cmd-{uuid4().hex}", "space": "all", "status": "active", "offset": 0, "limit": 20,
                **filters}
        return checked_reply("knowledge.list", team.operations.answer(stand_in_frame(
            TEAM, "knowledge.list", body, person_ref=reference_of("alice"), request_id=body["request_id"])))

    every, how_to, tagged = listed(), listed(page_type="how-to"), listed(tag="Ops")

    assert sorted(page["page"] for page in every["pages"]) == ["deploys", "my-checklist", "rollback"]
    assert every["total"] == 3
    assert ([page["page"] for page in how_to["pages"]], how_to["total"]) == (["rollback"], 1)
    assert tagged["total"] == 3 and {page["space"] for page in tagged["pages"]} == {"team", "personal"}


@pytest.mark.asyncio
async def test_a_history_counts_the_newest_fifty_revisions(team: Team) -> None:
    await team.write("alice", title="Runbook", body="Revision 1.")
    for revision in range(2, 56):
        await team.write("alice", page="runbook", base_revision=revision - 1, body=f"Revision {revision}.")

    def history(offset: int, limit: int) -> dict:
        request_id = f"cmd-{uuid4().hex}"
        body = {"request_id": request_id, "space": "team", "page": "runbook", "offset": offset, "limit": limit}
        return checked_reply("knowledge.history", team.operations.answer(stand_in_frame(
            TEAM, "knowledge.history", body, person_ref=reference_of("alice"), request_id=request_id)))

    first, last, past = history(0, 20), history(40, 20), history(50, 10)

    assert (len(first["revisions"]), first["total"], first["next_offset"]) == (20, 50, 20)
    assert (len(last["revisions"]), last["total"], "next_offset" in last) == (10, 50, False)
    assert last["revisions"][-1]["revision"] == 6
    assert (past["revisions"], past["total"], "next_offset" in past) == ([], 50, False)


@pytest.mark.asyncio
async def test_tags_are_sent_as_the_service_stores_them_and_an_empty_note_is_not_sent(team: Team) -> None:
    saved = await team.write("alice", title="Runbook", body="Check the workers.",
                             tags=["A" * 90, "Ops Team", "ops team", "🙂"])
    empty = await team.send("alice", action=A.ADD_NOTE, page="runbook", note="   ")

    assert saved.success is True and saved.result.page.tags == ["a" * 80, "ops-team"]
    assert (empty.error_code, empty.error_message) == ("invalid_request", "A note needs text. Write the note, then add it.")
    assert (await team.send("alice", action=A.READ, page="runbook")).result.page.open_notes == []


@pytest.mark.asyncio
async def test_a_request_the_contract_refuses_is_not_sent_and_answered_plainly(team: Team, monkeypatch, caplog) -> None:
    from assistant_gateway.application import knowledge_pages

    real = knowledge_pages.knowledge_request
    monkeypatch.setattr(knowledge_pages, "knowledge_request",
                        lambda payload: (real(payload)[0], {**real(payload)[1], "tags": ["x" * 81]}))
    with caplog.at_level(logging.ERROR, logger="assistant_gateway.application.knowledge_pages"):
        refused = await team.write("alice", title="Runbook", body="Check the workers.")

    assert (refused.error_code, refused.error_message) == ("invalid_request", knowledge_pages.NOT_SENDABLE_TEXT)
    assert "event=web_knowledge_request_not_sendable" in caplog.text and "tags" in caplog.text


def _stored_outcomes(team: Team) -> list[dict[str, Any]]:
    """Every stored outcome, as the table keeps it (storage.operation_outcomes)."""

    with team.repository._connect() as conn:
        rows = conn.execute("SELECT kind, reply_json, person_id FROM protocol1_operation_outcomes").fetchall()
    return [{"kind": row["kind"], "reply": json.loads(row["reply_json"]), "person": row["person_id"]} for row in rows]


def _frame(payload: KnowledgeCommandPayload, *, person: str = "alice", request_id: str) -> Any:
    kind, body = knowledge_request(payload)
    return stand_in_frame(TEAM, kind, {"request_id": request_id, **body}, person_ref=reference_of(person),
                          request_id=request_id)


@pytest.mark.asyncio
async def test_another_persons_command_under_a_reused_key_is_refused_and_shown_nothing(team: Team) -> None:
    await team.write("alice", "personal", title="Diary", body="Private thoughts.")
    await team.write("bob", title="Runbook", body="Check the workers.")
    first = _answer(team, KnowledgeCommandPayload(action=A.ADD_NOTE, space="personal", page="diary",
                                                  note="A private note."), request_id="cmd-shared")
    # bob's own note, sent (by a defect) under the same request id: the same kind and retry key.
    taken = _answer(team, KnowledgeCommandPayload(action=A.ADD_NOTE, page="runbook", note="Bob's note."),
                    person="bob", request_id="cmd-shared")

    assert first["outcome"] == "ok"
    assert taken["outcome"] == "invalid_request" and "already used" in taken["message"]
    assert "private" not in json.dumps(taken).lower()
    assert (await team.send("bob", action=A.READ, page="runbook")).result.page.open_notes == []


@pytest.mark.asyncio
async def test_a_refusal_of_a_person_who_may_not_act_is_stored(team: Team) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.")
    now = datetime.now(timezone.utc)

    def standing(state: PersonStanding) -> None:
        roles = ("member",) if state is PersonStanding.ACTIVE else ()
        team.access.record_person(TeamPersonV1(team_id=TEAM, person_id="carol", person_ref=reference_of("carol"),
                                               standing=state, roles=roles, changed_at_utc=now))

    note = KnowledgeCommandPayload(action=A.ADD_NOTE, page="runbook", note="Carol's note.")
    standing(PersonStanding.DISABLED)
    refused = _answer(team, note, person="carol", request_id="cmd-carol")
    standing(PersonStanding.ACTIVE)
    # The repeat (the gateway's second try under the same id) gets the same answer, not a carried-out note.
    again = _answer(team, note, person="carol", request_id="cmd-carol")

    assert refused["outcome"] == "not_allowed" and again == refused
    assert (await team.send("alice", action=A.READ, page="runbook")).result.page.open_notes == []


@pytest.mark.asyncio
async def test_a_refusal_that_cannot_be_stored_asks_for_a_retry(team: Team, monkeypatch) -> None:
    def broken(*_args: Any) -> None:
        raise ConnectionError("the database went away")

    monkeypatch.setattr(team.repository, "save_command_refusal", broken)
    edit = KnowledgeCommandPayload(action=A.WRITE, page="runbook", base_revision=1, body="Check all workers.")

    reply = _answer(team, edit, request_id="cmd-edit")

    assert reply["outcome"] == "runtime_error" and "Nothing was changed" in reply["message"]


@pytest.mark.asyncio
async def test_old_stored_outcomes_are_purged_by_the_knowledge_upkeep(team: Team) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.")
    family = team.operations.family()
    with team.repository._connect() as conn:
        conn.execute("UPDATE protocol1_operation_outcomes SET stored_at_utc = ?", ("2026-01-01T00:00:00+00:00",))
        conn.commit()

    await family.upkeep()

    assert _stored_outcomes(team) == [] and set(family.handlers) == set(KNOWLEDGE_KINDS)


@pytest.mark.asyncio
async def test_a_retried_change_is_carried_out_once_and_answered_alike(team: Team, caplog) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.")
    edit = KnowledgeCommandPayload(action=A.WRITE, page="runbook", base_revision=1, body="Check all workers.")
    first = _answer(team, edit, request_id="cmd-edit")
    # The page is now at revision 2: without the stored outcome, the retry would be stale.
    with caplog.at_level(logging.INFO, logger="test.team_knowledge"):
        again = _answer(team, edit, request_id="cmd-edit")
    note = KnowledgeCommandPayload(action=A.ADD_NOTE, page="runbook", note="Check the queue too.")
    noted = _answer(team, note, request_id="cmd-note")
    noted_again = _answer(team, note, request_id="cmd-note")

    assert first["outcome"] == "ok" and again == first
    # The stored outcome answers before the command runs again: nothing is refused or failed on the way.
    assert "event=operation_outcome_from_receipt" in caplog.text
    assert "event=team_knowledge_refused" not in caplog.text
    assert "event=operation_outcome_found_after_failure" not in caplog.text
    assert noted_again == noted and [n["note_id"] for n in noted["page"]["open_notes"]] == [noted["note"]["note_id"]]
    read = await team.send("alice", action=A.READ, page="runbook")
    assert read.result.page.revision == 2 and len(read.result.page.open_notes) == 1


@pytest.mark.asyncio
async def test_a_change_stores_only_its_receipt_never_the_text(team: Team) -> None:
    """No copy of page text stays in the outcomes table, which a person's departure does not reach."""

    await team.write("alice", "personal", title="Diary", body="Private thoughts.")
    await team.write("alice", "personal", page="diary", base_revision=1, body="More private thoughts.")
    await team.send("alice", action=A.ADD_NOTE, space="personal", page="diary", note="A private note.")
    await team.write("alice", title="Runbook", body="Check the workers.\nWrite to ops@example.com",
                     confirm_review=True)
    await team.send("alice", action=A.ARCHIVE, page="runbook", base_revision=1)
    await team.send("alice", action=A.REVERT, page="runbook", revision=1, base_revision=2)
    worker = team.worker("worker_1", person="alice")
    proposal = team.service.add_note(worker, "team/runbook", "A proposed note.").contribution
    other = team.service.add_note(worker, "team/runbook", "Another proposed note.").contribution
    await team.send("alice", action=A.PUBLISH_CONTRIBUTION, contribution_id=proposal.contribution_id)
    await team.send("alice", action=A.WITHDRAW_CONTRIBUTION, contribution_id=other.contribution_id)

    outcomes = _stored_outcomes(team)

    kinds = sorted(outcome["kind"] for outcome in outcomes)
    assert kinds == sorted(["knowledge.write"] * 3 + ["knowledge.add_note", "knowledge.archive", "knowledge.revert",
                                                       "knowledge.publish_contribution",
                                                       "knowledge.withdraw_contribution"])
    assert all(is_receipt(outcome["reply"]) and outcome["person"] == "alice" for outcome in outcomes)
    stored = json.dumps(outcomes)
    for text in ("Private thoughts", "A private note", "Check the workers", "ops@example.com", "proposed note"):
        assert text not in stored
    # The confirmed review kinds are kept, so a reply built later names them too.
    assert {"review_kinds": ["email_address"]}.items() <= next(
        outcome["reply"]["receipt"] for outcome in outcomes if outcome["reply"]["receipt"].get("review_kinds")
    ).items()


@pytest.mark.asyncio
async def test_a_repeated_change_is_answered_from_its_receipt_with_the_page_as_it_is_now(team: Team) -> None:
    flagged = "Check the workers.\nWrite to ops@example.com"
    await team.write("alice", title="Runbook", body=flagged, confirm_review=True)
    edit = KnowledgeCommandPayload(action=A.WRITE, page="runbook", base_revision=1, body="Check all workers.\n" + flagged,
                                   confirm_review=True)
    first = _answer(team, edit, request_id="cmd-edit")
    await team.write("bob", page="runbook", base_revision=2, body="Check every worker.", confirm_review=True)
    again = _answer(team, edit, request_id="cmd-edit")

    assert (first["outcome"], first["page"]["seen_version"]) == ("ok", "2")
    # Carried out once: the repeat shows bob's later revision, with the flags alice confirmed.
    assert (again["outcome"], again["page"]["seen_version"]) == ("ok", "3")
    assert again["page"]["content"]["text"] == "Check every worker."
    assert again["review_flags"] == first["review_flags"] and [flag["kind"] for flag in first["review_flags"]] == [
        "email_address"]
    history = await team.send("alice", action=A.HISTORY, page="runbook")
    assert [revision.revision for revision in history.result.revisions] == [3, 2, 1]


@pytest.mark.asyncio
async def test_a_repeated_note_names_its_own_note_after_many_newer_notes(team: Team) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.")
    note = KnowledgeCommandPayload(action=A.ADD_NOTE, page="runbook", note="The first note.")
    first = _answer(team, note, request_id="cmd-note")
    for number in range(55):
        await team.send("bob", action=A.ADD_NOTE, page="runbook", note=f"Later note {number}.")
    again = _answer(team, note, request_id="cmd-note")

    assert again["note"] == first["note"] and again["note"]["content"]["text"] == "The first note."
    assert again["page"]["open_notes"][0]["content"]["text"] == "Later note 54."


@pytest.mark.asyncio
async def test_a_change_found_stored_after_its_commit_failed_is_answered_from_its_receipt(
    team: Team, monkeypatch, caplog
) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.")
    original = team.repository.apply_page_change

    def acknowledgement_lost(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise ConnectionError("the commit's acknowledgement was lost")

    monkeypatch.setattr(team.repository, "apply_page_change", acknowledgement_lost)
    edit = KnowledgeCommandPayload(action=A.WRITE, page="runbook", base_revision=1, body="Check all workers.")
    with caplog.at_level(logging.WARNING, logger="test.team_knowledge"):
        reply = _answer(team, edit, request_id="cmd-edit")

    assert (reply["outcome"], reply["page"]["seen_version"]) == ("ok", "2")
    assert "event=operation_outcome_found_after_failure" in caplog.text


@pytest.mark.asyncio
async def test_a_refused_change_stays_refused_when_repeated(team: Team) -> None:
    edit = KnowledgeCommandPayload(action=A.WRITE, page="runbook", base_revision=1, body="Check all workers.")
    refused = _answer(team, edit, request_id="cmd-edit")
    await team.write("alice", title="Runbook", body="Check the workers.")
    again = _answer(team, edit, request_id="cmd-edit")

    assert refused["outcome"] == "not_found" and again == refused
    assert (await team.send("alice", action=A.READ, page="runbook")).result.page.body == "Check the workers."


@pytest.mark.asyncio
async def test_a_change_whose_reply_cannot_be_built_is_answered_as_unknown(tmp_path: Path, caplog) -> None:
    """The change is made, so the request is never answered as refused (team_services.outcomes)."""

    team = Team(tmp_path, knowledge=LONG_PAGES)
    await team.write("alice", title="Runbook", body="Check the workers.")
    team.repository.get_note = lambda _note_id: None  # type: ignore[method-assign]
    frame = _frame(KnowledgeCommandPayload(action=A.ADD_NOTE, page="runbook", note="Check the queue."),
                   request_id="cmd-note")
    with caplog.at_level(logging.ERROR, logger="test.team_knowledge"), pytest.raises(AnswerUnknown):
        await team.operations.handle(frame)
    with pytest.raises(AnswerUnknown):
        await team.operations.handle(frame)

    assert "event=team_knowledge_reply_after_change_failed" in caplog.text
    # Never treated as a failed (unchanged) request.
    assert "event=team_knowledge_command_failed" not in caplog.text
    page = team.repository.get_page_by_id(_stored_outcomes(team)[-1]["reply"]["receipt"]["page_id"])
    assert [note.body for note in team.repository.list_notes(page.page_id, status=None, limit=50)] == [
        "Check the queue."]


@pytest.mark.asyncio
async def test_a_change_that_stores_no_receipt_is_answered_as_unknown(team: Team, monkeypatch) -> None:
    await team.write("alice", title="Runbook", body="Check the workers.")
    original = team.repository.add_note
    monkeypatch.setattr(team.repository, "add_note",
                        lambda *args, **kwargs: original(*args, **{**kwargs, "command_outcome": None}))

    with pytest.raises(AnswerUnknown):
        await team.operations.handle(_frame(KnowledgeCommandPayload(action=A.ADD_NOTE, page="runbook", note="Noted."),
                                            request_id="cmd-note"))


# -- text over 32,768 characters and oversized pages via UA1_BLOBS (decision 77) ---------

LONG_PAGES = {"limits": {"max_body_chars": 500_000, "max_note_chars": 50_000, "writes_per_agent_per_hour": 1000,
                         "page_edits_per_agent": 100, "new_pages_per_work": 100}}
LONG = 40_000


def long_text(name: str, length: int = LONG, phrase: str = "drain the node, check «queues» — then restart ✓ 🚀") -> str:
    """``length`` characters of distinct multi-line text, with characters of one to four bytes."""

    line = f"{name}: {phrase}\n"
    return (line * (length // len(line) + 1))[: length - 1] + "."


@dataclass
class LongWorld:
    """Knowledge agents stored over 32,768 chars, and proposals among it."""

    team: Team
    texts: dict[str, str]
    proposals: dict[str, str]


def long_world(team: Team) -> LongWorld:
    texts = {name: long_text(name) for name in ("handbook", "runbook", "grown", "journal", "note", "later",
                                                "proposal", "edit")}
    svc = team.service

    def system(number: int):
        return team.worker(f"sys_{number}")

    svc.write(system(1), "team/handbook", DraftInput(title="Handbook", body=texts["handbook"]))
    svc.write(system(2), "team/runbook", DraftInput(title="Runbook", body=texts["runbook"]))
    svc.write(system(2), "team/runbook", DraftInput(body="Restart the worker."), base_revision=1)
    svc.write(system(3), "team/grown", DraftInput(title="Grown", body="Short at first."))
    svc.write(system(3), "team/grown", DraftInput(body=texts["grown"]), base_revision=1)
    svc.write(system(4), "team/noted", DraftInput(title="Noted", body="A short page with a long note."))
    svc.add_note(system(4), "team/noted", texts["note"])
    alice = team.worker("alice_1", person="alice")
    svc.write(alice, "personal/journal", DraftInput(title="Journal", body=texts["journal"]))
    later = svc.write(alice, "team/later", DraftInput(title="Later", body="Proposed while the name was free."))
    svc.write(system(5), "team/later", DraftInput(title="Later", body=texts["later"]))
    proposals = {
        "new_long": svc.write(alice, "team/proposal", DraftInput(title="Proposal", body=texts["proposal"])),
        "short_on_long": svc.write(alice, "team/handbook", DraftInput(body="A shorter handbook."), base_revision=1),
        "long_on_short": svc.write(alice, "team/runbook", DraftInput(body=texts["edit"]), base_revision=2),
        "note_on_long": svc.add_note(alice, "team/handbook", "Mention the backup."),
        "archive_long": svc.archive(alice, "team/handbook", base_revision=1, reason="Superseded."),
        "new_named_like_long": later,
    }
    return LongWorld(team, texts, {name: result.contribution.contribution_id for name, result in proposals.items()})


@pytest.fixture(params=BACKENDS)
def world(request, tmp_path: Path) -> Iterator[LongWorld]:
    with knowledge_storage(request.param, tmp_path) as storage:
        yield long_world(Team(tmp_path, storage=storage, knowledge=LONG_PAGES))


@pytest.mark.asyncio
async def test_web_chat_reads_edits_reverts_notes_and_publishes_long_knowledge_via_blobs(world: LongWorld) -> None:
    """Every Web Chat Knowledge path handles text > 32,768 chars cleanly via UA1_BLOBS."""

    team, texts, props = world.team, world.texts, world.proposals

    # Reads: long team page, long personal page, long earlier revision, long open note, long history, long proposals.
    handbook = await team.send("alice", action=A.READ, page="handbook")
    journal = await team.send("alice", action=A.READ, space="personal", page="journal")
    earlier = await team.send("alice", action=A.READ, page="runbook", revision=1)
    noted = await team.send("alice", action=A.READ, page="noted")
    history = await team.send("alice", action=A.HISTORY, page="handbook")
    prop_new = await team.send("alice", action=A.READ_CONTRIBUTION, contribution_id=props["new_long"])
    prop_short_on_long = await team.send("alice", action=A.READ_CONTRIBUTION, contribution_id=props["short_on_long"])

    assert handbook.success and handbook.result.page.body == texts["handbook"]
    assert journal.success and journal.result.page.body == texts["journal"]
    assert earlier.success and earlier.result.page.body == texts["runbook"] and earlier.result.page.shown_revision == 1
    assert noted.success and [n.body for n in noted.result.page.open_notes] == [texts["note"]]
    assert history.success and len(history.result.revisions) == 1
    assert prop_new.success and prop_new.result.contribution.body == texts["proposal"]
    assert prop_short_on_long.success and prop_short_on_long.result.contribution.current.body == texts["handbook"]

    # Writes and notes: 500,000-char page body and 50,000-char note round-trip cleanly.
    huge_body = long_text("huge", 500_000, phrase="scale the workers safely and verify digests")
    huge_note = long_text("hugenote", 50_000, phrase="check the rollout log")
    created_huge = await team.write("alice", title="Huge page", body=huge_body)
    assert created_huge.success and created_huge.result.page.body == huge_body
    read_huge = await team.send("alice", action=A.READ, page="huge-page")
    assert read_huge.success and read_huge.result.page.body == huge_body
    added_note = await team.send("alice", action=A.ADD_NOTE, page="huge-page", note=huge_note)
    assert added_note.success and [n.body for n in added_note.result.page.open_notes] == [huge_note]

    # Revert to a long revision and revert a long page to a short revision.
    reverted_to_long = await team.send("alice", action=A.REVERT, page="runbook", revision=1, base_revision=2)
    assert reverted_to_long.success and reverted_to_long.result.page.body == texts["runbook"]
    reverted_to_short = await team.send("alice", action=A.REVERT, page="grown", revision=1, base_revision=2)
    assert reverted_to_short.success and reverted_to_short.result.page.body == "Short at first."

    # Archive a long page, publish a long proposal (edited > 32,768 chars), and withdraw a proposal for a long page.
    edited_prop_body = long_text("edited_prop", 60_000, phrase="reviewed and expanded guidance")
    published = await team.send(
        "alice", action=A.PUBLISH_CONTRIBUTION, contribution_id=props["new_long"],
        edited=True, title="Proposal", body=edited_prop_body,
    )
    assert published.success and published.result.page.body == edited_prop_body
    withdrawn = await team.send("alice", action=A.WITHDRAW_CONTRIBUTION, contribution_id=props["short_on_long"])
    assert withdrawn.success
    archived = await team.send("alice", action=A.ARCHIVE, page="handbook", base_revision=1)
    assert archived.success and archived.result.page.status == "archived" and archived.result.page.body == texts["handbook"]


# -- replies larger than one frame carries (team_services.knowledge) ---------------------

WIDE_LIMITS = {"limits": {**LONG_PAGES["limits"], "max_title_chars": 1000, "max_summary_chars": 4000,
                          "max_tags": 100, "page_edits_per_agent": 100}}
# Three bytes per character in UTF-8.
WIDE = "界"


@pytest.fixture(params=BACKENDS)
def wide(request, tmp_path: Path) -> Iterator[Team]:
    with knowledge_storage(request.param, tmp_path) as storage:
        yield Team(tmp_path, storage=storage, knowledge=WIDE_LIMITS)


async def _direct(team: Team, kind: str, body: dict, *, person: str = "alice") -> dict:
    request_id = f"cmd-{uuid4().hex}"
    return checked_reply(kind, await team.operations.handle(stand_in_frame(
        TEAM, kind, {"request_id": request_id, **body}, person_ref=reference_of(person), request_id=request_id)))


@pytest.mark.asyncio
async def test_a_page_list_too_large_for_one_reply_comes_in_parts_and_the_browser_gets_whole_pages(wide: Team) -> None:
    for number in range(20):
        saved = await wide.write("alice", title=f"{number:02d} " + WIDE * 990, body="Short.", summary=WIDE * 3990,
                                 tags=[f"tag-{number}-{tag}-" + "x" * 60 for tag in range(100)])
        assert saved.success is True

    part = await _direct(wide, "knowledge.list", {"space": "team", "status": "active", "offset": 0, "limit": 20})
    rest = await _direct(wide, "knowledge.list", {"space": "team", "status": "active", "offset": part["next_offset"],
                                                  "limit": 20 - part["next_offset"]})
    whole = await wide.send("bob", action=A.LIST)

    assert 0 < len(part["pages"]) < 20 and part["next_offset"] == len(part["pages"]) and part["total"] == 20
    assert (len(whole.result.pages), whole.result.total) == (20, 20)
    assert [page.page for page in whole.result.pages] == [page["page"] for page in part["pages"] + rest["pages"]]


@pytest.mark.asyncio
async def test_a_history_too_large_for_one_reply_comes_in_parts(wide: Team) -> None:
    worker = wide.worker("sys")
    page = wide.service.write(worker, "team/runbook", DraftInput(title="Runbook", body="Revision 1.")).page
    for revision in range(2, 31):
        wide.service.write(worker, "team/runbook", DraftInput(title=f"Runbook {revision} " + WIDE * 980,
                                                              body=f"Revision {revision}."),
                           base_revision=revision - 1, change_summary=WIDE * 3990)

    part = await _direct(wide, "knowledge.history", {"space": "team", "page": page.slug, "offset": 0, "limit": 50})
    whole = await wide.send("alice", action=A.HISTORY, page="runbook")

    assert len(part["revisions"]) < 30 and part["next_offset"] == len(part["revisions"]) and part["total"] == 30
    assert [revision.revision for revision in whole.result.revisions] == list(range(30, 0, -1))


@pytest.mark.asyncio
async def test_an_oversized_page_with_many_wide_notes_spills_to_blobs_instead_of_refusing(wide: Team) -> None:
    """When body + open notes + metadata exceed REPLY_BODY_MAX_BYTES, blobs carry them transparently."""

    worker = wide.worker("sys")
    wide.service.write(worker, "team/handbook", DraftInput(title="Handbook", body=WIDE * 32_000))
    for number in range(4):
        wide.service.add_note(worker, "team/handbook", f"{number} " + WIDE * 32_000)

    opened = await wide.send("alice", action=A.READ, page="handbook")
    assert opened.success is True
    assert opened.result.page.body == WIDE * 32_000
    assert len(opened.result.page.open_notes) == 4

    grown = await wide.write("alice", page="handbook", base_revision=1, body=WIDE * 40_000)
    assert grown.success is True and grown.result.page.body == WIDE * 40_000

    noted = await wide.send("alice", action=A.ADD_NOTE, page="handbook", note=WIDE * 30_000)
    assert noted.success is True and len(noted.result.page.open_notes) == 5

    proposal = wide.service.add_note(wide.worker("worker_a", person="alice"), "team/handbook", "Short note.")
    proposed = await wide.send("alice", action=A.READ_CONTRIBUTION, contribution_id=proposal.contribution.contribution_id)
    assert proposed.success is True and proposed.result.contribution.current.body == WIDE * 40_000

    archived = await wide.send("alice", action=A.ARCHIVE, page="handbook", base_revision=2)
    assert archived.success is True and archived.result.page.status == "archived"
