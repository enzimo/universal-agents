"""The manager's side of the Web Chat Knowledge page (``ManagerKnowledgeCommand``).

The person comes only from the gateway-signed context; ``personal`` is always
that person's own space; a person's team edit publishes directly; the
never-stored rules and review confirmation apply; a person publishes or
withdraws only their own proposals.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from custom_agents.knowledge_command_runtime import ARCHIVE_REASON, handle_manager_knowledge_command
from custom_messages.contracts.control_v1 import (
    KnowledgeCommandAction,
    KnowledgeCommandPayload,
    ManagerKnowledgeCommand,
)
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from knowledge_base.service import DraftInput
from knowledge_base.vocabulary import ContributionStatus
from storage.knowledge_repository import create_knowledge_repository
from tests.knowledge_support import agent as agent_actor
from tests.test_manager_control_runtime import (  # noqa: F401
    _bootstrap_agent,
    _CaptureComms,
    _config,
    _gateway_signing_keys,
    _signed,
)

SECRET = "hunter2-Correct-Horse-9"


@pytest.fixture
def manager(tmp_path: Path):
    config = _config(tmp_path)
    agent = _bootstrap_agent(config)
    agent.team_comms = _CaptureComms()
    agent._logger = logging.getLogger("test.knowledge_command")
    agent.knowledge_config = {}
    agent._knowledge_repository = create_knowledge_repository(config.manager_storage_settings)
    return agent


def _context(agent, person: str, *, claimed: str = "", roles: tuple[str, ...] = ("member",)) -> GatewayContext:
    signed = _signed(GatewayContext(canonical_user_id=person), team_id=agent.team_id, roles=list(roles))
    return signed.model_copy(update={"canonical_user_id": claimed}) if claimed else signed


async def _send(agent, context: GatewayContext, **fields) -> ManagerKnowledgeCommand:
    await handle_manager_knowledge_command(
        agent,
        command=ManagerKnowledgeCommand(command=KnowledgeCommandPayload(**fields)),
        msg_header=MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
        msg_meta=MessageMeta(gateway_context=context),
    )
    return agent.team_comms.messages[-1].msg_content


def _write(agent, person: str, space: str = "team", **fields):
    return _send(agent, _context(agent, person), action=KnowledgeCommandAction.WRITE, space=space, **fields)


@pytest.mark.asyncio
async def test_an_unsigned_command_changes_nothing(manager) -> None:
    response = await _send(
        manager, GatewayContext(canonical_user_id="alice"),
        action=KnowledgeCommandAction.WRITE, title="Deploys", body="Deploy after smoke tests.",
    )

    assert response.success is False and response.error_code == "authorization_denied"
    assert response.error_message.startswith("unsigned_sender")
    listed = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.LIST)
    assert listed.result.total == 0


@pytest.mark.asyncio
async def test_personal_pages_belong_to_the_signed_person_whatever_the_message_claims(manager) -> None:
    # Signed for alice; the unsigned copy claims bob.
    saved = await _send(
        manager, _context(manager, "alice", claimed="bob"),
        action=KnowledgeCommandAction.WRITE, space="personal", title="Travel", body="Window seats.",
    )
    assert saved.success is True and saved.result.page.space == "personal"

    alice = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.READ, space="personal", page="travel")
    bob = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.READ, space="personal", page="travel")
    bob_list = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.LIST, space="personal")

    assert alice.result.page.body == "Window seats."
    assert (bob.success, bob.error_code) == (False, "not_found")
    assert bob_list.result.total == 0 and bob_list.result.pages == []


@pytest.mark.asyncio
async def test_a_persons_team_edit_publishes_directly_with_the_person_as_editor(manager) -> None:
    created = await _write(manager, "alice", title="Release process", body="Tag, then publish.")
    edited = await _write(
        manager, "bob", page="release-process", base_revision=created.result.page.revision,
        title="Release process", body="Tag, test, then publish.",
    )
    history = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.HISTORY, page="release-process")

    assert edited.success is True and edited.result.page.revision == 2
    assert edited.result.page.body == "Tag, test, then publish."
    assert edited.result.page.last_editor.kind == "person" and edited.result.page.last_editor.person_id == "bob"
    assert [revision.editor.person_id for revision in history.result.revisions] == ["bob", "alice"]
    pending = manager._knowledge_repository.list_contributions(
        team_id=manager.team_id, owner_person_id="bob", status=ContributionStatus.PENDING, limit=10
    )
    assert pending == []


@pytest.mark.asyncio
async def test_a_stale_base_revision_is_a_conflict_and_saves_nothing(manager) -> None:
    created = await _write(manager, "alice", title="Runbook", body="Restart the worker.")
    await _write(manager, "bob", page="runbook", base_revision=1, title="Runbook", body="Restart both workers.")

    stale = await _write(manager, "alice", page="runbook", base_revision=created.result.page.revision, body="Mine.")
    taken = await _write(manager, "carol", title="Runbook", body="Another runbook.")

    assert (stale.success, stale.error_code, stale.current_revision) == (False, "conflict", 2)
    assert (taken.success, taken.error_code) == (False, "page_exists")
    current = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.READ, page="runbook")
    assert current.result.page.body == "Restart both workers."


@pytest.mark.asyncio
async def test_never_stored_content_is_refused_by_field_and_place_without_its_value(manager) -> None:
    refused = await _write(manager, "alice", title="Database", body=f"Host db1.\npassword: {SECRET}")

    assert (refused.success, refused.error_code) == (False, "sensitive_content")
    assert [(flag.field, flag.line) for flag in refused.review_flags] == [("body", 2)]
    serialized = refused.model_dump_json()
    assert SECRET not in serialized
    assert refused.command.body == ""


@pytest.mark.asyncio
async def test_content_held_for_review_needs_the_persons_confirmation(manager) -> None:
    held = await _write(manager, "alice", title="Contacts", body="Ask the vendor.\nWrite to ops@example.com")
    confirmed = await _write(
        manager, "alice", title="Contacts", body="Ask the vendor.\nWrite to ops@example.com", confirm_review=True
    )

    assert (held.success, held.error_code) == (False, "review_required")
    assert [(flag.kind, flag.field, flag.line, flag.column) for flag in held.review_flags] == [
        ("email_address", "body", 2, 10)
    ]
    assert "ops@example.com" not in held.model_dump_json(exclude={"command"})
    assert "Everyone on the team will see it" in held.error_message
    assert confirmed.success is True
    assert [flag.kind for flag in confirmed.result.review_flags] == ["email_address"]


@pytest.mark.asyncio
async def test_notes_archive_and_revert_keep_every_revision(manager) -> None:
    await _write(manager, "alice", title="On call", body="Page the lead.")
    await _write(manager, "alice", page="on-call", base_revision=1, body="Page the lead, then the backup.")
    noted = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.ADD_NOTE, page="on-call", note="Add the phone tree.")
    archived = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.ARCHIVE, page="on-call", base_revision=2)
    reverted = await _send(
        manager, _context(manager, "alice"), action=KnowledgeCommandAction.REVERT, page="on-call", revision=1, base_revision=3
    )
    history = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.HISTORY, page="on-call")

    assert [note.body for note in noted.result.page.open_notes] == ["Add the phone tree."]
    assert archived.result.page.status == "archived"
    assert reverted.result.page.status == "active" and reverted.result.page.body == "Page the lead."
    assert [revision.change_kind for revision in history.result.revisions] == ["revert", "archive", "edit", "create"]
    assert history.result.revisions[1].change_summary == ARCHIVE_REASON
    assert history.result.revisions[0].change_summary == "Reverted to revision 1"


@pytest.mark.asyncio
async def test_a_person_publishes_and_withdraws_only_their_own_proposals(manager, tmp_path) -> None:
    await _write(manager, "alice", title="Deploys", body="Deploy on Tuesdays.")
    service = _service(manager)
    # An agent working for alice: its team edits wait for her as proposals.
    worker = agent_actor("worker_1", person="alice")
    edit = service.write(
        _with_team(worker, manager), "team/deploys", DraftInput(title="Deploys", body="Deploy on Tuesdays and Fridays."),
        base_revision=1,
    ).contribution
    note = service.add_note(_with_team(worker, manager), "team/deploys", "Fridays need two reviewers.").contribution

    listed = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.LIST_CONTRIBUTIONS)
    shown = await _send(
        manager, _context(manager, "alice"), action=KnowledgeCommandAction.READ_CONTRIBUTION, contribution_id=edit.contribution_id
    )
    bob_read = await _send(
        manager, _context(manager, "bob"), action=KnowledgeCommandAction.READ_CONTRIBUTION, contribution_id=edit.contribution_id
    )
    bob_withdraw = await _send(
        manager, _context(manager, "bob"), action=KnowledgeCommandAction.WITHDRAW_CONTRIBUTION, contribution_id=edit.contribution_id
    )
    published = await _send(
        manager, _context(manager, "alice"), action=KnowledgeCommandAction.PUBLISH_CONTRIBUTION,
        contribution_id=edit.contribution_id, edited=True, title="Deploys", body="Deploy on Tuesdays and Fridays (with review).",
        base_revision=shown.result.contribution.current.revision,
    )
    withdrawn = await _send(
        manager, _context(manager, "alice"), action=KnowledgeCommandAction.WITHDRAW_CONTRIBUTION, contribution_id=note.contribution_id
    )

    assert listed.result.total == 2 and all(item.body == "" for item in listed.result.contributions)
    assert shown.result.contribution.current.body == "Deploy on Tuesdays."
    assert shown.result.contribution.body == "Deploy on Tuesdays and Fridays."
    assert (bob_read.error_code, bob_withdraw.error_code) == ("not_found", "not_found")
    assert published.success is True
    assert published.result.page.body == "Deploy on Tuesdays and Fridays (with review)."
    assert withdrawn.success is True
    remaining = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.LIST_CONTRIBUTIONS)
    assert remaining.result.total == 0


@pytest.mark.asyncio
async def test_the_page_list_pages_twenty_at_a_time_with_a_total(manager) -> None:
    for number in range(23):
        await _write(manager, "alice", title=f"Guide {number:02d}", body=f"Guide number {number}.")

    first = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.LIST)
    last = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.LIST, offset=20)
    await _write(manager, "alice", title="Kubernetes upgrades", body="Drain nodes first.")
    found = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.LIST, query="number")
    one = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.LIST, query="drain nodes")

    assert (len(first.result.pages), first.result.total) == (20, 23)
    assert (len(last.result.pages), last.result.total) == (3, 23)
    assert (len(found.result.pages), found.result.total) == (20, 23)
    assert [page.page for page in one.result.pages] == ["kubernetes-upgrades"] and one.result.total == 1
    assert "[" in one.result.pages[0].snippet


@pytest.mark.asyncio
async def test_knowledge_off_and_a_non_manager_answer_plainly(manager) -> None:
    manager._knowledge_repository = None
    off = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.LIST)
    manager.is_team_lead = False
    not_manager = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.LIST)

    assert (off.success, off.error_code) == (False, "knowledge_off")
    assert "turned off" in off.error_message
    assert (not_manager.success, not_manager.error_code, not_manager.error_message) == (
        False, "authorization_denied", "manager_required",
    )


def _service(agent):
    from custom_tools.providers.knowledge import knowledge_service

    return knowledge_service(agent)


def _with_team(actor, agent):
    from dataclasses import replace

    return replace(actor, team_id=agent.team_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["note", "create"])
async def test_edited_proposals_publish_only_the_persons_redacted_text(manager, kind) -> None:
    service = _service(manager)
    worker = _with_team(agent_actor("worker_1", person="alice"), manager)
    if kind == "note":
        await _write(manager, "alice", title="Runbook", body="Check the workers.")
        proposed = service.add_note(worker, "team/runbook", "Email alice@example.com.")
    else:
        proposed = service.write(
            worker, "team/runbook", DraftInput(title="Runbook", body="Check the workers.", summary="Email alice@example.com.")
        )
    bob = await _send(
        manager, _context(manager, "bob"), action=KnowledgeCommandAction.PUBLISH_CONTRIBUTION,
        contribution_id=proposed.contribution.contribution_id, edited=True, body="Contact the on-call lead.", summary="",
    )
    assert (bob.success, bob.error_code) == (False, "not_found")
    published = await _send(
        manager, _context(manager, "alice"), action=KnowledgeCommandAction.PUBLISH_CONTRIBUTION,
        contribution_id=proposed.contribution.contribution_id, edited=True, title="Runbook",
        body="Contact the on-call lead.", summary="", base_revision=1 if kind == "note" else 0,
    )
    assert published.success is True
    viewed = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.READ, page="runbook")
    assert "alice@example.com" not in viewed.result.model_dump_json()
    if kind == "note":
        assert [note.body for note in viewed.result.page.open_notes] == ["Contact the on-call lead."]
    else:
        assert viewed.result.page.body == "Contact the on-call lead." and viewed.result.page.summary == ""


@pytest.mark.asyncio
async def test_a_summary_is_preserved_when_omitted_and_cleared_when_empty(manager) -> None:
    await _write(manager, "alice", title="Runbook", body="Check the workers.", summary="Worker maintenance.")
    kept = await _write(manager, "alice", page="runbook", base_revision=1, body="Check all workers.")
    cleared = await _write(manager, "alice", page="runbook", base_revision=2, body="Check all workers.", summary="")
    assert kept.result.page.summary == "Worker maintenance."
    assert cleared.success is True and cleared.result.page.summary == ""


@pytest.mark.asyncio
async def test_an_empty_edited_note_does_not_publish_the_original(manager) -> None:
    await _write(manager, "alice", title="Runbook", body="Check the workers.")
    worker = _with_team(agent_actor("worker_1", person="alice"), manager)
    proposed = _service(manager).add_note(worker, "team/runbook", "Email alice@example.com.").contribution
    refused = await _send(
        manager, _context(manager, "alice"), action=KnowledgeCommandAction.PUBLISH_CONTRIBUTION,
        contribution_id=proposed.contribution_id, edited=True, body="", base_revision=1,
    )
    assert (refused.success, refused.error_code) == (False, "invalid_request")
    viewed = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.READ, page="runbook")
    assert viewed.result.page.open_notes == []
    listed = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.LIST_CONTRIBUTIONS)
    assert listed.result.total == 1


@pytest.mark.asyncio
async def test_an_archive_proposal_archives_the_current_page_without_replacing_its_text(manager) -> None:
    await _write(manager, "alice", title="Runbook", body="Old instructions.")
    service = _service(manager)
    worker = _with_team(agent_actor("worker_1", person="alice"), manager)
    proposed = service.archive(worker, "team/runbook", base_revision=1, reason="Retired runbook.").contribution
    await _write(
        manager, "bob", page="runbook", base_revision=1, title="Updated runbook", body="New instructions.",
        summary="Current maintenance.", tags=["current"],
    )
    published = await _send(
        manager, _context(manager, "alice"), action=KnowledgeCommandAction.PUBLISH_CONTRIBUTION,
        contribution_id=proposed.contribution_id, base_revision=2,
    )
    assert published.success is True
    page = published.result.page
    assert (page.revision, page.status, page.title, page.body, page.summary, page.tags) == (
        3, "archived", "Updated runbook", "New instructions.", "Current maintenance.", ["current"],
    )


@pytest.mark.asyncio
async def test_archived_pages_can_be_found_and_restored_from_a_new_list_request(manager) -> None:
    await _write(manager, "alice", "personal", title="Runbook", body="Check the workers.")
    await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.ARCHIVE, space="personal", page="runbook", base_revision=1)
    current = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.LIST, space="personal")
    archived = await _send(
        manager, _context(manager, "alice"), action=KnowledgeCommandAction.LIST,
        space="personal", status="archived", query="workers",
    )
    bob = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.LIST, space="personal", status="archived")
    assert current.result.total == bob.result.total == 0
    assert archived.result.total == 1
    [page] = archived.result.pages
    restored = await _write(manager, "alice", "personal", page=page.page, base_revision=page.revision, body="Check the workers.")
    assert restored.success is True and restored.result.page.status == "active"


@pytest.mark.asyncio
async def test_all_pending_proposals_can_be_paged_without_crossing_owner_boundaries(manager) -> None:
    await _write(manager, "alice", title="Runbook", body="Check the workers.")
    service = _service(manager)
    proposed_ids = set()
    for number in range(101):
        worker = _with_team(agent_actor(f"worker_{number}", person="alice"), manager)
        proposed_ids.add(service.add_note(worker, "team/runbook", f"Check number {number}.").contribution.contribution_id)
    bob = _with_team(agent_actor("bob_worker", person="bob"), manager)
    service.add_note(bob, "team/runbook", "Bob's own proposal.")
    seen = []
    for offset in range(0, 101, 20):
        result = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.LIST_CONTRIBUTIONS, offset=offset)
        assert result.success is True and result.result.total == 101
        assert all(item.body == "" for item in result.result.contributions)
        seen.extend(item.contribution_id for item in result.result.contributions)
    assert len(seen) == 101 and set(seen) == proposed_ids


@pytest.mark.asyncio
async def test_links_merged_names_and_merge_history_reach_the_page(manager) -> None:
    await _write(manager, "alice", title="CI pipeline", body="Pushes run the fast suite.")
    await _write(manager, "alice", title="Old CI notes", body="Nightly runs too.")
    await _write(
        manager, "alice", title="Release process",
        body="See [[ci-pipeline]], [[Old CI notes|the old notes]], [[team/missing-page]] and `[[in-code]]`.",
    )
    # The curator's team run merges the old notes into the pipeline page.
    service = _service(manager)
    curator = _with_team(agent_actor("curator_1", agent_type="KnowledgeCuratorAgent"), manager)
    service.merge(
        curator, "team/old-ci-notes", "team/ci-pipeline",
        DraftInput(title="CI pipeline", body="Pushes run the fast suite. Nightly runs too."),
        base_revision=1, source_revision=1,
    )

    page = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.READ, page="release-process")
    via_old_name = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.READ, page="old-ci-notes")
    taken = await _write(manager, "bob", title="Old CI notes", body="A new page under the merged name.")
    history = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.HISTORY, page="ci-pipeline")
    listed = await _send(manager, _context(manager, "bob"), action=KnowledgeCommandAction.LIST)

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
    # The merged source is archived: lists and totals leave it out.
    assert sorted(item.page for item in listed.result.pages) == ["ci-pipeline", "release-process"]
    assert listed.result.total == 2


@pytest.mark.asyncio
async def test_the_proposal_count_is_the_signed_persons_waiting_proposals_only(manager) -> None:
    await _write(manager, "alice", title="Runbook", body="Check the workers.")
    service = _service(manager)
    alice_worker = _with_team(agent_actor("worker_a", person="alice"), manager)
    first = service.add_note(alice_worker, "team/runbook", "Check the queue first.").contribution
    service.add_note(alice_worker, "team/runbook", "Then check the workers.")
    service.add_note(_with_team(agent_actor("worker_b", person="bob"), manager), "team/runbook", "Bob's own proposal.")

    count = KnowledgeCommandAction.COUNT_CONTRIBUTIONS
    alice = await _send(manager, _context(manager, "alice"), action=count)
    # A message that names bob is still counted for the person the gateway signed.
    claimed = await _send(manager, _context(manager, "alice", claimed="bob"), action=count)
    carol = await _send(manager, _context(manager, "carol"), action=count)
    await _send(
        manager, _context(manager, "alice"), action=KnowledgeCommandAction.WITHDRAW_CONTRIBUTION,
        contribution_id=first.contribution_id,
    )
    after = await _send(manager, _context(manager, "alice"), action=count)

    assert (alice.success, alice.result.total, alice.result.contributions) == (True, 2, [])
    assert claimed.result.total == 2
    assert carol.result.total == 0
    assert after.result.total == 1


@pytest.mark.asyncio
async def test_the_proposal_count_with_the_knowledge_base_off_is_refused_plainly(manager) -> None:
    manager._knowledge_repository = None
    off = await _send(manager, _context(manager, "alice"), action=KnowledgeCommandAction.COUNT_CONTRIBUTIONS)

    assert (off.success, off.error_code) == (False, "knowledge_off")
