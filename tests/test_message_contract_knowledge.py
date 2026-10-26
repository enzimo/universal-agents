"""Protocol requests carry what real knowledge workflows need, using a real SQLite store.

These are contract conformance checks, not the not-yet-live protocol dispatcher.
The explicit field mapping makes lost inputs and outcomes visible before rollout.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import pytest
import rfc8785
from jsonschema import ValidationError as SchemaValidationError
from pydantic import ValidationError

from custom_messages.wire.encoding import b64url
from custom_messages.wire.frame import MAX_FRAME_BYTES, FrameRefused, body_model, format_timestamp, sign_frame
from knowledge_base.records import CODE_CONFLICT, KnowledgeError
from knowledge_base.service import DraftInput, KnowledgeService
from knowledge_base.vocabulary import ContributionKind, ContributionStatus, NoteStatus, PageStatus
from runtime_settings.knowledge import KnowledgeSettings, KnowledgeWriteLimits
from storage.knowledge_repository import create_knowledge_repository
from tests.knowledge_support import NOW, agent, knowledge_storage, person
from tests import message_contract_support as fixtures
from tests.test_message_contract import body_schema_file, checked, validator

SYSTEM = agent("curator", agent_type="KnowledgeCuratorAgent")
PERSON = person("person_1")


@pytest.fixture
def service(tmp_path):
    with knowledge_storage("sqlite", tmp_path) as settings:
        yield KnowledgeService(create_knowledge_repository(settings), settings=KnowledgeSettings(), clock=lambda: NOW)


def request(kind: str, **fields):
    return checked(kind, {"request_id": "command_1", "space": "team", "page": "guide", **fields})


def note_payload(note):
    return {
        "note_id": note.note_id,
        "content": {"type": "inline", "text": note.body},
        "created_at": format_timestamp(datetime.fromisoformat(note.created_at_utc)),
        "created_by": editor_payload(note.actor_kind, note.actor_id, note.actor_agent_type),
    }


def editor_payload(kind, actor_id, agent_type):
    if kind == "person":
        return {"kind": "person", "person": actor_id}
    if kind == "agent":
        return {"kind": "agent", "agent_type": agent_type}
    return {"kind": "system"}


def page_summary(page):
    return {
        "space": page.space.value,
        "page": page.slug,
        "title": page.title,
        "page_type": page.page_type,
        "summary": page.summary,
        "tags": list(page.tags),
        "status": page.status.value,
        "seen_version": str(page.revision),
        "updated_at": format_timestamp(datetime.fromisoformat(page.updated_at_utc)),
    }


def page_payload(service, actor, reference, *, revision=0):
    """Build the wire read from real service views, including current and shown revisions."""

    view = service.read(actor, reference, revision=revision)
    current = view.page
    [latest] = service.history(actor, reference, limit=1)[1]
    shown = view.revision or latest
    payload = page_summary(current)
    payload.update(
        type="inline",
        title=shown.title, page_type=shown.page_type, summary=shown.summary, tags=list(shown.tags),
        content={"type": "inline", "text": shown.body},
        created_at=format_timestamp(datetime.fromisoformat(current.created_at_utc)),
        updated_by=editor_payload(latest.actor_kind, latest.actor_id, latest.actor_agent_type),
        shown_by=editor_payload(shown.actor_kind, shown.actor_id, shown.actor_agent_type),
        backlinks=[page_summary(page) for page in view.backlinks],
        open_notes=[note_payload(note) for note in view.open_notes],
        pending_contributions=[{
            "contribution_id": item.contribution_id, "kind": item.kind.value,
            "created_at": format_timestamp(datetime.fromisoformat(item.created_at_utc)),
        } for item in view.pending_contributions],
        links=[{"written": link.written, "space": link.space, "page": link.slug, "exists": link.exists}
               for link in service.page_links(current, shown.body)],
    )
    if current.frozen_until_utc:
        payload["frozen_until"] = format_timestamp(datetime.fromisoformat(current.frozen_until_utc))
    if view.revision:
        payload["shown_revision"] = view.revision.revision
    if view.redirected_from:
        payload["redirected_from"] = view.redirected_from
    return payload


def test_complete_page_reference_preserves_metadata_that_exceeds_the_frame_limit(tmp_path):
    settings = KnowledgeSettings(limits=KnowledgeWriteLimits(
        max_title_chars=1000, max_summary_chars=4000, max_tags=100,
    ))
    with knowledge_storage("sqlite", tmp_path) as storage:
        service = KnowledgeService(create_knowledge_repository(storage), settings=settings, clock=lambda: NOW)
        service.write(PERSON, "team/target", DraftInput(title="Target", body="Target instructions."))
        tags = [f"tag-{number:03}-" + "a" * 72 for number in range(100)]
        for number in range(20):
            service.write(PERSON, f"team/backlink-{number}", DraftInput(
                title="T" * 1000, summary="S" * 4000, tags=tags, body="Read [[target]].",
            ))
        page = page_payload(service, PERSON, "team/target")

    assert len(page["backlinks"]) == 20
    checked("knowledge.read.reply", {"outcome": "ok", "page": page})
    frame = fixtures.example_frame("knowledge.read.reply", "ok")
    frame["body"] = {"outcome": "ok", "page": page}
    key = fixtures.private_key(frame["issuer"]["key_id"])
    signed = sign_frame(frame, key)
    data = rfc8785.dumps(signed)
    assert len(data) > MAX_FRAME_BYTES
    with pytest.raises(FrameRefused, match="too_large"):
        fixtures.verify(data, signed)

    # Model the file boundary without claiming a protocol dispatcher or blob client.
    page_file = tmp_path / "complete-page.json"
    page_file.write_bytes(rfc8785.dumps(page))
    content = page_file.read_bytes()
    reference = {"sha256": b64url(hashlib.sha256(content).digest()), "size": len(content),
                 "media_type": "application/json"}
    frame["body"] = {"outcome": "ok", "page": {"type": "blob", "ref": reference}}
    checked("knowledge.read.reply", frame["body"])
    signed = sign_frame(frame, key)
    data = rfc8785.dumps(signed)
    assert len(data) < MAX_FRAME_BYTES
    fixtures.verify(data, signed)

    restored = json.loads(content)
    reply = checked("knowledge.read.reply", {"outcome": "ok", "page": restored})
    assert reply.page.model_dump(mode="json", exclude_unset=True) == page
    assert len(reply.page.backlinks) == 20


@pytest.mark.parametrize("limit, counts", [(3, [3] * 6), (50, [8, 8, 2])])
def test_agent_search_continues_beyond_the_configured_per_page_limit(service, limit, counts):
    for number in range(18):
        service.write(PERSON, f"team/release-{number}", DraftInput(
            title=f"Release guide {number}", body="Review the release.", page_type="how-to", tags=["releases"],
        ))
    actor = agent("worker", person="person_1")
    seen = []
    returned = []
    offset = 0
    while True:
        command = checked("knowledge.list", {
            "request_id": f"search_{offset}", "space": "team", "status": "active", "query": "release",
            "page_type": "how-to", "tag": "releases", "offset": offset, "limit": limit,
        })
        filters = {"query": command.query, "space": command.space, "page_type": command.page_type, "tag": command.tag}
        hits = service.search(actor, **filters, limit=command.limit, offset=command.offset)
        next_offset = command.offset + len(hits)
        body = {"outcome": "ok", "pages": [page_summary(hit.page) for hit in hits]}
        if service.search(actor, **filters, limit=1, offset=next_offset):
            body["next_offset"] = next_offset
        reply = checked("knowledge.list.reply", body)
        assert "total" not in reply.model_dump(exclude_unset=True)
        seen.extend(row.page for row in reply.pages)
        returned.append(len(reply.pages))
        if "next_offset" not in body:
            break
        assert body["next_offset"] > offset
        offset = body["next_offset"]

    assert returned == counts
    assert len(seen) == len(set(seen)) == 18
    assert set(seen) == {f"release-{number}" for number in range(18)}


@pytest.mark.parametrize("changed_page", [None, "source", "target"])
def test_merge_carries_the_draft_and_checks_both_read_versions(service, changed_page):
    source = service.write(SYSTEM, "team/source", DraftInput(title="Source", body="Check the build.")).page
    target = service.write(SYSTEM, "team/target", DraftInput(title="Target", body="Deploy after review.")).page
    target = service.write(
        SYSTEM, "team/target", DraftInput(body="Deploy the reviewed build."), base_revision=target.revision,
    ).page
    assert source.revision != target.revision
    command = request(
        "knowledge.merge", page="source", seen_version=str(source.revision), into="target",
        target_seen_version=str(target.revision), content={"type": "inline", "text": "Check the build, then deploy after review."},
        confirm_review=False,
    )
    if changed_page:
        read_page = source if changed_page == "source" else target
        service.write(
            SYSTEM, f"team/{changed_page}", DraftInput(body="Newer instructions."), base_revision=read_page.revision,
        )

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
        checked("knowledge.merge.reply", {"outcome": "ok", "page": page_payload(service, SYSTEM, "team/target")})


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
        command = request(kind, note={"type": "inline", "text": "Add a review step."}, confirm_review=False)
        result = service.add_note(worker, "team/guide", command.note.text, confirm_review=command.confirm_review)
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
    command = request("knowledge.add_note", note={"type": "inline", "text": "Check the deployment steps."}, confirm_review=False)
    result = service.add_note(PERSON, "team/guide", command.note.text, confirm_review=command.confirm_review)
    checked("knowledge.add_note.reply", {
        "outcome": "ok", "page": page_payload(service, PERSON, "team/guide"), "note": note_payload(result.note), "review_flags": [],
    })
    # A later request discovers server-assigned note ids through the protocol read.
    view = service.read(SYSTEM, "team/guide")
    reply = checked("knowledge.read.reply", {"outcome": "ok", "page": page_payload(service, SYSTEM, "team/guide")})
    [note] = reply.page.open_notes
    assert note.content.text == command.note.text
    resolution = request("knowledge.resolve_note", note_id=note.note_id, merged=merged)
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
    service.write(SYSTEM, "team/guide", DraftInput(title="Guide", body="Current instructions."))
    command = request("knowledge.freeze", minutes=90)
    frozen = service.freeze(SYSTEM, "team/guide", minutes=command.minutes)
    assert datetime.fromisoformat(frozen.frozen_until_utc) == NOW + timedelta(minutes=90)
    reply = checked("knowledge.freeze.reply", {"outcome": "ok", "page": page_payload(service, SYSTEM, "team/guide")})
    assert datetime.fromisoformat(reply.page.frozen_until) == NOW + timedelta(minutes=90)


def test_historical_reads_keep_the_current_version_and_the_shown_revisions_editor(service):
    original = service.write(PERSON, "team/guide", DraftInput(
        title="Release guide", body="Review the release.", page_type="how-to", tags=["r" * 80],
    )).page
    current = service.write(
        SYSTEM, "team/guide", DraftInput(title="Current guide", body="Review and deploy."), base_revision=original.revision,
    ).page
    reply = checked("knowledge.read.reply", {
        "outcome": "ok", "page": page_payload(service, SYSTEM, "team/guide", revision=original.revision),
    })
    assert reply.page.seen_version == str(current.revision)
    assert reply.page.shown_revision == original.revision
    assert (reply.page.title, reply.page.content.text, reply.page.page_type, reply.page.tags) == (
        original.title, original.body, "how-to", ["r" * 80],
    )
    assert reply.page.updated_by.agent_type == SYSTEM.agent_type
    assert reply.page.shown_by.person == PERSON.actor_id


def test_read_exposes_real_backlinks_notes_pending_contributions_and_freeze_time(service):
    page = service.write(PERSON, "team/guide", DraftInput(title="Guide", body="Current instructions.")).page
    service.write(PERSON, "team/checklist", DraftInput(title="Checklist", body="Read [[guide]] first."))
    note = service.add_note(PERSON, "team/guide", "Check the release steps.").note
    pending = service.archive(
        agent("worker", person="person_1"), "team/guide", base_revision=page.revision, reason="Replaced by the checklist.",
    ).contribution
    service.freeze(SYSTEM, "team/guide", minutes=60)
    reply = checked("knowledge.read.reply", {"outcome": "ok", "page": page_payload(service, PERSON, "team/guide")})
    assert reply.page.backlinks[0].page == "checklist"
    assert reply.page.open_notes[0].note_id == note.note_id
    assert reply.page.pending_contributions[0].contribution_id == pending.contribution_id
    assert reply.page.pending_contributions[0].kind == "archive"
    assert datetime.fromisoformat(reply.page.frozen_until) == NOW + timedelta(minutes=60)


@pytest.mark.parametrize(("kind", "body", "required"), [
    ("knowledge.merge", {"into": "target", "seen_version": "1", "target_seen_version": "2", "content": {"type": "inline", "text": "Combined instructions."}, "confirm_review": False}, "target_seen_version"),
    ("knowledge.merge", {"into": "target", "seen_version": "1", "target_seen_version": "2", "content": {"type": "inline", "text": "Combined instructions."}, "confirm_review": False}, "content"),
    ("knowledge.archive", {"seen_version": "1", "reason": "Replaced instructions."}, "reason"),
    ("knowledge.revert", {"seen_version": "2", "to_revision": 1, "reason": "Restore reviewed instructions."}, "reason"),
    ("knowledge.resolve_note", {"note_id": "note_1", "merged": False}, "merged"),
    ("knowledge.freeze", {"minutes": 60}, "minutes"),
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
