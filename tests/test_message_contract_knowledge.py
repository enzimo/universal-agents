"""Protocol requests carry what real knowledge workflows need, using a real SQLite store.

These are contract conformance checks, not the not-yet-live protocol dispatcher.
The explicit field mapping makes lost inputs and outcomes visible before rollout.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from jsonschema import ValidationError as SchemaValidationError
from pydantic import ValidationError

from custom_messages.wire.frame import body_model, format_timestamp
from knowledge_base.records import CODE_CONFLICT, KnowledgeError
from knowledge_base.service import DraftInput, KnowledgeService
from knowledge_base.vocabulary import ContributionKind, ContributionStatus, NoteStatus, PageStatus
from runtime_settings.knowledge import KnowledgeSettings
from storage.knowledge_repository import create_knowledge_repository
from tests.knowledge_support import NOW, agent, knowledge_storage, person
from tests.test_message_contract import body_schema_file, validator

SYSTEM = agent("curator", agent_type="KnowledgeCuratorAgent")
PERSON = person("person_1")


@pytest.fixture
def service(tmp_path):
    with knowledge_storage("sqlite", tmp_path) as settings:
        yield KnowledgeService(create_knowledge_repository(settings), settings=KnowledgeSettings(), clock=lambda: NOW)


def checked(kind: str, body: dict):
    """Both the language-neutral schema and generated model must preserve the workflow fields."""

    validator(body_schema_file(kind)).validate(body)
    adapter, _ = body_model(kind)
    result = adapter.validate_python(body)
    assert adapter.dump_python(result, mode="json", exclude_none=True, by_alias=True) == body
    return result


def request(kind: str, **fields):
    return checked(kind, {"request_id": "command_1", "space": "team", "page": "guide", **fields})


def note_payload(note):
    return {
        "note_id": note.note_id,
        "content": {"type": "inline", "text": note.body},
        "created_at": format_timestamp(NOW),
        "created_by": {"kind": "person", "person": note.actor_id},
    }


def page_payload(page, *, notes=None):
    payload = {
        "space": page.space.value,
        "page": page.slug,
        "title": page.title,
        "body": page.body,
        "tags": list(page.tags),
        "status": page.status.value,
        "seen_version": str(page.revision),
        "updated_at": format_timestamp(NOW),
        "updated_by": {"kind": "system"},
    }
    if notes is not None:
        payload["open_notes"] = [note_payload(note) for note in notes]
    return payload


@pytest.mark.parametrize("changed_page", [None, "source", "target"])
def test_merge_carries_the_draft_and_checks_both_read_versions(service, changed_page):
    source = service.write(SYSTEM, "team/source", DraftInput(title="Source", body="Check the build.")).page
    target = service.write(SYSTEM, "team/target", DraftInput(title="Target", body="Deploy after review.")).page
    command = request(
        "knowledge.merge", page="source", seen_version=str(source.revision), into="target",
        target_seen_version=str(target.revision), content={"type": "inline", "text": "Check the build, then deploy after review."},
        confirm_review=False,
    )
    if changed_page:
        service.write(SYSTEM, f"team/{changed_page}", DraftInput(body="Newer instructions."), base_revision=1)

    def merge():
        return service.merge(
            SYSTEM, f"{command.space}/{command.page}", f"{command.space}/{command.into}",
            DraftInput(body=command.content.text), source_revision=int(command.seen_version),
            base_revision=int(command.target_seen_version), confirm_review=command.confirm_review,
        )

    if changed_page:
        with pytest.raises(KnowledgeError) as refused:
            merge()
        assert refused.value.code == CODE_CONFLICT
        assert service.read(SYSTEM, "team/source").page.status is PageStatus.ACTIVE
        assert service.read(SYSTEM, f"team/{changed_page}").page.body == "Newer instructions."
    else:
        result = merge()
        assert result.source.status is PageStatus.ARCHIVED
        assert result.target.body == command.content.text
        checked("knowledge.merge.reply", {"outcome": "ok", "page": page_payload(result.target)})


def test_merge_can_reference_text_larger_than_the_inline_limit():
    reference = {"sha256": "A" * 43, "size": 40000, "media_type": "text/plain"}
    command = request(
        "knowledge.merge", seen_version="1", into="target", target_seen_version="2",
        content={"type": "blob", "ref": reference}, confirm_review=False,
    )
    assert command.content.ref.size == 40000


@pytest.mark.parametrize("content", [
    {"type": "inline"},
    {"type": "inline", "text": ""},
    {"type": "blob"},
    {"type": "inline", "text": "Merged instructions.", "ref": {"sha256": "A" * 43, "size": 20, "media_type": "text/plain"}},
])
def test_merge_cannot_omit_text_or_mix_inline_text_with_a_reference(content):
    body = {
        "request_id": "command_1", "space": "team", "page": "source", "into": "target",
        "seen_version": "1", "target_seen_version": "2", "content": content, "confirm_review": False,
    }
    with pytest.raises(SchemaValidationError):
        validator(body_schema_file("knowledge.merge")).validate(body)
    adapter, _ = body_model("knowledge.merge")
    with pytest.raises(ValidationError):
        adapter.validate_python(body)


@pytest.mark.parametrize("kind", ["knowledge.add_note", "knowledge.archive"])
def test_agent_team_changes_report_the_saved_proposal_instead_of_publication(service, kind):
    page = service.write(PERSON, "team/guide", DraftInput(title="Guide", body="Current instructions.")).page
    worker = agent("worker", person="person_1")
    if kind == "knowledge.add_note":
        command = request(kind, note="Add a review step.", confirm_review=False)
        result = service.add_note(worker, "team/guide", command.note, confirm_review=command.confirm_review)
        expected_kind = ContributionKind.NOTE
    else:
        command = request(kind, seen_version=str(page.revision), reason="The guide has been replaced.")
        result = service.archive(worker, "team/guide", base_revision=int(command.seen_version), reason=command.reason)
        expected_kind = ContributionKind.ARCHIVE
    reply = checked(kind + ".reply", {"outcome": "pending_review", "contribution_id": result.contribution.contribution_id})
    saved = service.get_contribution(worker, reply.contribution_id)
    assert (saved.kind, saved.status) == (expected_kind, ContributionStatus.PENDING)
    view = service.read(PERSON, "team/guide")
    assert (view.page.status, view.page.body, view.open_notes) == (PageStatus.ACTIVE, "Current instructions.", [])


@pytest.mark.parametrize("merged", [True, False])
def test_a_later_read_exposes_the_note_id_needed_to_resolve_or_dismiss_it(service, merged):
    service.write(PERSON, "team/guide", DraftInput(title="Guide", body="Current instructions."))
    command = request("knowledge.add_note", note="Check the deployment steps.", confirm_review=False)
    result = service.add_note(PERSON, "team/guide", command.note, confirm_review=command.confirm_review)
    checked("knowledge.add_note.reply", {
        "outcome": "ok", "page": page_payload(result.page), "note": note_payload(result.note), "review_flags": [],
    })
    # A later request discovers server-assigned note ids through the protocol read.
    view = service.read(SYSTEM, "team/guide")
    reply = checked("knowledge.read.reply", {"outcome": "ok", "page": page_payload(view.page, notes=view.open_notes)})
    [note] = reply.page.open_notes
    assert note.content.text == command.note
    resolution = request("knowledge.resolve_note", seen_version=reply.page.seen_version, note_id=note.note_id, merged=merged)
    service.resolve_note(SYSTEM, resolution.note_id, merged=resolution.merged)
    assert service.read(SYSTEM, "team/guide").open_notes == []
    state = NoteStatus.RESOLVED if merged else NoteStatus.DISMISSED
    assert service._repository.list_notes(view.page.page_id, status=state, limit=5)[0].note_id == note.note_id


def test_archive_and_revert_carry_the_callers_reason(service):
    original = service.write(PERSON, "personal/guide", DraftInput(title="Guide", body="Original instructions.")).page
    archive = request("knowledge.archive", space="personal", seen_version=str(original.revision), reason="Replaced by the new guide.")
    archived = service.archive(PERSON, "personal/guide", base_revision=int(archive.seen_version), reason=archive.reason).page
    assert service.history(PERSON, "personal/guide")[1][0].change_summary == archive.reason
    restore = request(
        "knowledge.revert", space="personal", seen_version=str(archived.revision), to_revision=original.revision,
        reason="The replacement guide is incomplete.",
    )
    restored = service.revert(
        PERSON, "personal/guide", to_revision=restore.to_revision, base_revision=int(restore.seen_version), reason=restore.reason,
    ).page
    assert (restored.status, restored.body) == (PageStatus.ACTIVE, original.body)
    assert service.history(PERSON, "personal/guide")[1][0].change_summary.endswith(restore.reason)


def test_freeze_carries_the_requested_duration(service):
    page = service.write(SYSTEM, "team/guide", DraftInput(title="Guide", body="Current instructions.")).page
    command = request("knowledge.freeze", seen_version=str(page.revision), minutes=90)
    frozen = service.freeze(SYSTEM, "team/guide", minutes=command.minutes)
    assert datetime.fromisoformat(frozen.frozen_until_utc) == NOW + timedelta(minutes=90)


@pytest.mark.parametrize(("kind", "body", "required"), [
    ("knowledge.merge", {"into": "target", "seen_version": "1", "target_seen_version": "2", "content": {"type": "inline", "text": "Combined instructions."}, "confirm_review": False}, "target_seen_version"),
    ("knowledge.merge", {"into": "target", "seen_version": "1", "target_seen_version": "2", "content": {"type": "inline", "text": "Combined instructions."}, "confirm_review": False}, "content"),
    ("knowledge.archive", {"seen_version": "1", "reason": "Replaced instructions."}, "reason"),
    ("knowledge.revert", {"seen_version": "2", "to_revision": 1, "reason": "Restore reviewed instructions."}, "reason"),
    ("knowledge.resolve_note", {"seen_version": "1", "note_id": "note_1", "merged": False}, "merged"),
    ("knowledge.freeze", {"seen_version": "1", "minutes": 60}, "minutes"),
])
def test_incomplete_knowledge_commands_are_rejected(kind, body, required):
    complete = {"request_id": "command_1", "space": "team", "page": "guide", **body}
    checked(kind, complete)
    del complete[required]
    with pytest.raises(SchemaValidationError):
        validator(body_schema_file(kind)).validate(complete)
    adapter, _ = body_model(kind)
    with pytest.raises(ValidationError):
        adapter.validate_python(complete)
