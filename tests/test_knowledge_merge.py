"""Merging knowledge pages, merged names leading to their target, and freezes, on SQLite and PostgreSQL."""

from __future__ import annotations

from datetime import timedelta

import pytest

from knowledge_base.records import (
    CODE_CONFLICT,
    CODE_FROZEN,
    CODE_INVALID,
    CODE_MERGED,
    CODE_REVIEW_REQUIRED,
    CODE_SCOPE,
    CODE_SENSITIVE,
    KnowledgeError,
)
from knowledge_base.service import DraftInput, KnowledgeService
from knowledge_base.vocabulary import ChangeKind, ContributionStatus, NoteStatus, PageStatus
from runtime_settings.knowledge import KnowledgeSettings, KnowledgeWriteLimits
from storage.knowledge_repository import create_knowledge_repository
from tests.knowledge_support import BACKENDS, NOW, agent, knowledge_storage, person


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    with knowledge_storage(request.param, tmp_path) as settings:
        yield create_knowledge_repository(settings)


def service(store, clock=None, **limits) -> KnowledgeService:
    settings = KnowledgeSettings(limits=KnowledgeWriteLimits(**limits)) if limits else KnowledgeSettings()
    return KnowledgeService(store, settings=settings, clock=clock or Clock())


def draft(body: str, title: str = "") -> DraftInput:
    return DraftInput(title=title, body=body)


SYSTEM = agent("sys", agent_type="KnowledgeCuratorAgent")


def _two_pages(svc: KnowledgeService):
    kept = svc.write(SYSTEM, "team/release-process", draft("Tag, then deploy.", "Release process")).page
    extra = svc.write(SYSTEM, "team/release-steps", draft("Run CI before tagging.", "Release steps")).page
    return kept, extra


def _merge(svc, actor, kept, extra, body="Run CI, tag, then deploy.", **fields):
    return svc.merge(
        actor, extra.reference(), kept.reference(), DraftInput(body=body, **fields),
        base_revision=kept.revision, source_revision=extra.revision,
    )


def test_a_merge_keeps_one_page_and_the_old_name_leads_to_it(store):
    svc = service(store)
    kept, extra = _two_pages(svc)
    linking = svc.write(SYSTEM, "team/handbook", draft("See [[release-steps]].", "Handbook")).page
    svc.add_note(SYSTEM, extra.reference(), "Tag from main only.")
    proposal = svc.write(agent("w", person="alice"), extra.reference(), draft("Run CI twice."), base_revision=extra.revision)

    result = _merge(svc, SYSTEM, kept, extra, title="Releasing")

    assert (result.target.revision, result.target.title, result.target.body) == (2, "Releasing", "Run CI, tag, then deploy.")
    assert result.source.status is PageStatus.ARCHIVED and result.source.revision == 2
    [target_entry] = store.list_revisions(kept.page_id, limit=1)
    [source_entry] = store.list_revisions(extra.page_id, limit=1)
    # Fixed history text from trusted code, naming the other page.
    assert (target_entry.change_kind, target_entry.change_summary) == (
        ChangeKind.MERGE, "Merged Release steps (team/release-steps) into this page"
    )
    assert (source_entry.change_kind, source_entry.change_summary) == (
        ChangeKind.MERGE, "Merged into Releasing (team/release-process)"
    )
    assert source_entry.body == "Run CI before tagging.", "the source keeps its text in its archived revision"

    view = svc.read(SYSTEM, "team/release-steps")
    assert view.page.page_id == kept.page_id and view.redirected_from == "team/release-steps"
    assert [n.body for n in view.open_notes] == ["Tag from main only."], "open notes moved to the target"
    assert [p.page_id for p in view.backlinks] == [linking.page_id], "links to the old name count as links here"
    assert svc.history(SYSTEM, "team/release-steps")[0].page_id == kept.page_id
    assert svc.problems(SYSTEM).broken_links == []
    assert [hit.page.reference() for hit in svc.search(SYSTEM, query="deploy")] == ["team/release-process"]

    # The pending proposal for the old page now targets the kept page, proposed
    # against its text before the merge, so publishing it unchanged conflicts.
    moved = store.get_contribution(proposal.contribution.contribution_id)
    assert (moved.page_id, moved.slug, moved.base_revision, moved.status) == (
        kept.page_id, "release-process", kept.revision, ContributionStatus.PENDING
    )
    curator_for_alice = agent("curator", person="alice", agent_type="KnowledgeCuratorAgent")
    with pytest.raises(KnowledgeError) as conflict:
        svc.publish_contribution(curator_for_alice, moved.contribution_id)
    assert conflict.value.code == CODE_CONFLICT
    published = svc.publish_contribution(
        curator_for_alice, moved.contribution_id, draft=draft("Run CI twice, tag, then deploy."), base_revision=2
    )
    assert published.page.page_id == kept.page_id and published.page.revision == 3


def test_the_edit_log_records_the_merge_for_both_pages(store):
    svc = service(store)
    kept, extra = _two_pages(svc)
    _merge(svc, SYSTEM, kept, extra)

    report = svc.activity(SYSTEM, hours=1)
    by_type = {item.agent_type: item for item in report.by_agent_type}
    # Two creates and the merge of both pages.
    assert by_type["KnowledgeCuratorAgent"].applied == 4
    assert dict(report.busiest_pages) == {"team/release-process": 2, "team/release-steps": 2}


@pytest.mark.parametrize("space", ["team", "personal"])
def test_a_note_inserted_after_a_merge_follows_the_target(store, monkeypatch, space):
    svc = service(store)
    actor = SYSTEM if space == "team" else agent("w", person="alice")
    kept = svc.write(actor, f"{space}/kept", draft("First.", "Kept")).page
    extra = svc.write(actor, f"{space}/extra", draft("Second.", "Extra")).page
    insert_note = store.add_note

    def merge_before_inserting(page, **kwargs):
        # The caller already resolved extra; commit the merge before the
        # insertion acquires its write lock, using the real database for both.
        _merge(svc, actor, kept, extra)
        return insert_note(page, **kwargs)

    monkeypatch.setattr(store, "add_note", merge_before_inserting)
    result = svc.add_note(actor, extra.reference(), "A note prepared before the merge.")
    assert result.page.page_id == kept.page_id
    assert (result.note.page_id, result.note.team_id, result.note.space, result.note.owner_person_id) == (
        kept.page_id, kept.team_id, kept.space, kept.owner_person_id,
    )
    assert result.note.work_person_id == actor.work_person_id
    assert store.list_notes(extra.page_id, status=NoteStatus.OPEN, limit=10) == []
    assert [note.note_id for note in svc.read(actor, extra.reference()).open_notes] == [result.note.note_id]


@pytest.mark.parametrize("change", ["edit", "archive", "note"])
def test_a_proposal_inserted_after_a_merge_targets_the_kept_page(store, monkeypatch, change):
    svc = service(store)
    kept, extra = _two_pages(svc)
    actor = agent("w", person="alice")
    insert_contribution = store.add_contribution

    def merge_before_inserting(contribution, **kwargs):
        _merge(svc, SYSTEM, kept, extra)
        return insert_contribution(contribution, **kwargs)

    monkeypatch.setattr(store, "add_contribution", merge_before_inserting)
    if change == "edit":
        result = svc.write(actor, extra.reference(), draft("Run CI twice."), base_revision=extra.revision)
    elif change == "archive":
        result = svc.archive(actor, extra.reference(), base_revision=extra.revision, reason="Outdated instructions.")
    else:
        result = svc.add_note(actor, extra.reference(), "Tag from main only.")
    proposal = result.contribution
    assert (proposal.page_id, proposal.slug, proposal.base_revision, proposal.owner_person_id) == (
        kept.page_id, kept.slug, kept.revision, "alice",
    )
    assert store.get_contribution(proposal.contribution_id) == proposal
    assert svc.read(actor, extra.reference()).pending_contributions == [proposal]
    if change == "note":
        published = svc.publish_contribution(actor, proposal.contribution_id)
        assert published.note.page_id == kept.page_id
    else:
        with pytest.raises(KnowledgeError) as conflict:
            svc.publish_contribution(actor, proposal.contribution_id)
        assert conflict.value.code == CODE_CONFLICT
        current = svc.read(actor, kept.reference()).page
        published = svc.publish_contribution(
            actor, proposal.contribution_id,
            draft=draft("Run CI twice, tag, then deploy.") if change == "edit" else None,
            base_revision=current.revision,
        )
        assert published.page.page_id == kept.page_id
        if change == "archive":
            assert published.page.status is PageStatus.ARCHIVED
            assert (published.page.title, published.page.body, published.page.summary,
                    published.page.page_type, published.page.tags) == (
                current.title, current.body, current.summary, current.page_type, current.tags,
            ), "archiving preserves the kept page's merged content"


def test_note_publication_follows_a_merge_after_the_reviewer_reads_the_source(store, monkeypatch):
    svc = service(store)
    kept, extra = _two_pages(svc)
    actor = agent("curator", person="alice", agent_type="KnowledgeCuratorAgent")
    proposal = svc.add_note(actor, extra.reference(), "Tag from main only.").contribution
    insert_note = store.add_note

    def merge_before_inserting(page, **kwargs):
        _merge(svc, SYSTEM, kept, extra)
        return insert_note(page, **kwargs)

    monkeypatch.setattr(store, "add_note", merge_before_inserting)
    result = svc.publish_contribution(actor, proposal.contribution_id)
    assert (result.page.page_id, result.page.revision) == (kept.page_id, 2)
    assert (result.note.page_id, result.note.space, result.note.owner_person_id, result.note.work_person_id) == (
        kept.page_id, kept.space, "", "alice",
    )
    published = store.get_contribution(proposal.contribution_id)
    assert (published.page_id, published.slug, published.published_revision, published.status) == (
        kept.page_id, kept.slug, 2, ContributionStatus.PUBLISHED,
    )
    assert store.list_notes(extra.page_id, status=NoteStatus.OPEN, limit=10) == []


def test_a_new_page_proposal_cannot_take_a_name_merged_before_its_insert(store, monkeypatch):
    svc = service(store)
    kept = svc.write(SYSTEM, "team/kept", draft("First.", "Kept")).page
    actor = agent("w", person="alice")
    insert_contribution = store.add_contribution

    def merge_before_inserting(contribution, **kwargs):
        extra = svc.write(SYSTEM, "team/new-topic", draft("Second.", "Other topic")).page
        _merge(svc, SYSTEM, kept, extra)
        return insert_contribution(contribution, **kwargs)

    monkeypatch.setattr(store, "add_contribution", merge_before_inserting)
    with pytest.raises(KnowledgeError) as merged:
        svc.write(actor, "team/new-topic", draft("A different topic.", "New topic"))
    assert merged.value.code == CODE_MERGED
    assert merged.value.details["redirects_to"] == kept.reference()
    assert svc.list_contributions(actor) == []


def test_backlinks_count_each_linking_page_once_before_the_result_limit(store):
    svc = service(store)
    kept, extra = _two_pages(svc)
    handbook = svc.write(
        SYSTEM, "team/handbook", draft("See [[release-process]] and [[release-steps]].", "A handbook"),
    ).page
    guide = svc.write(SYSTEM, "team/guide", draft("See [[release-process]].", "Z guide")).page
    _merge(svc, SYSTEM, kept, extra)
    backlinks = store.backlinks(kept, reader_person_id="", limit=2)
    assert [page.page_id for page in backlinks] == [handbook.page_id, guide.page_id]
    assert [page.page_id for page in svc.read(SYSTEM, extra.reference()).backlinks] == [handbook.page_id, guide.page_id]


def test_stale_revisions_change_nothing(store):
    svc = service(store)
    kept, extra = _two_pages(svc)
    svc.write(SYSTEM, extra.reference(), draft("Run CI, then tag."), base_revision=extra.revision)
    with pytest.raises(KnowledgeError) as stale_source:
        _merge(svc, SYSTEM, kept, extra)
    assert stale_source.value.code == CODE_CONFLICT and stale_source.value.details["current_revision"] == 2
    with pytest.raises(KnowledgeError) as stale_target:
        svc.merge(SYSTEM, extra.reference(), kept.reference(), draft("x"), base_revision=7, source_revision=2)
    assert stale_target.value.code == CODE_CONFLICT
    assert store.get_page_by_id(kept.page_id).revision == 1
    assert store.get_page_by_id(extra.page_id).status is PageStatus.ACTIVE
    assert store.get_redirect_target(extra.key) is None


def test_merged_text_is_checked_like_any_write(store):
    svc = service(store)
    kept, extra = _two_pages(svc)
    with pytest.raises(KnowledgeError) as secret:
        _merge(svc, SYSTEM, kept, extra, body="Deploy with password: hunter22hunter")
    assert secret.value.code == CODE_SENSITIVE
    with pytest.raises(KnowledgeError) as flagged:
        _merge(svc, SYSTEM, kept, extra, body="Ask ana@example.com before tagging.")
    assert flagged.value.code == CODE_REVIEW_REQUIRED, "work for nobody cannot put contact details on team pages"
    with pytest.raises(KnowledgeError) as empty:
        _merge(svc, SYSTEM, kept, extra, body="  ")
    assert empty.value.code == CODE_INVALID
    assert store.get_page_by_id(extra.page_id).status is PageStatus.ACTIVE


def test_a_persons_run_merges_only_their_own_pages(store):
    svc = service(store)
    kept, extra = _two_pages(svc)
    curator_for_alice = agent("curator", person="alice", agent_type="KnowledgeCuratorAgent")
    with pytest.raises(KnowledgeError) as team:
        _merge(svc, curator_for_alice, kept, extra)
    assert team.value.code == CODE_SCOPE

    tea = svc.write(curator_for_alice, "personal/tea", draft("Green tea.", "Tea")).page
    more = svc.write(curator_for_alice, "personal/tea-notes", draft("Sencha first.", "Tea notes")).page
    merged = _merge(svc, curator_for_alice, tea, more, body="Green tea, sencha first.")
    assert merged.target.reference() == "personal/tea"
    assert svc.read(curator_for_alice, "personal/tea-notes").page.page_id == tea.page_id
    # Another person's work cannot reach Alice's merged name: personal/ is always their own space.
    with pytest.raises(KnowledgeError):
        svc.read(agent("w", person="bob"), "personal/tea-notes")

    with pytest.raises(KnowledgeError) as across:
        svc.merge(curator_for_alice, "personal/tea", "team/release-process", draft("x"), base_revision=1, source_revision=2)
    assert across.value.code == CODE_INVALID and "same space" in across.value.message
    with pytest.raises(KnowledgeError) as itself:
        svc.merge(SYSTEM, "team/release-process", "team/release-process", draft("x"), base_revision=1, source_revision=1)
    assert itself.value.code == CODE_INVALID


def test_a_merged_name_is_never_reused_for_a_new_page_but_edits_reach_the_target(store):
    svc = service(store)
    kept, extra = _two_pages(svc)
    _merge(svc, SYSTEM, kept, extra)
    with pytest.raises(KnowledgeError) as reused:
        svc.write(SYSTEM, "team/release-steps", draft("A new topic.", "Release steps"))
    assert reused.value.code == CODE_MERGED and reused.value.details["redirects_to"] == "team/release-process"
    # A proposed new page whose title makes the merged name is refused when published.
    proposal = svc.write(agent("w", person="alice"), "team/other", draft("New steps.", "Release steps")).contribution
    with pytest.raises(KnowledgeError) as published:
        svc.publish_contribution(agent("c", person="alice"), proposal.contribution_id)
    assert published.value.code == CODE_MERGED
    edited = svc.write(SYSTEM, "team/release-steps", draft("Edited by the old name."), base_revision=2).page
    assert edited.page_id == kept.page_id and edited.revision == 3
    noted = svc.add_note(SYSTEM, "team/release-steps", "A note by the old name.")
    assert noted.note.page_id == kept.page_id


def test_merges_follow_on_so_an_old_name_leads_one_step(store):
    svc = service(store)
    kept, extra = _two_pages(svc)
    _merge(svc, SYSTEM, kept, extra)
    final = svc.write(SYSTEM, "team/shipping", draft("Ship it.", "Shipping")).page
    kept = store.get_page_by_id(kept.page_id)
    svc.merge(SYSTEM, kept.reference(), final.reference(), draft("Run CI, tag, deploy, ship."),
              base_revision=final.revision, source_revision=kept.revision)
    for name in ("team/release-steps", "team/release-process"):
        assert svc.read(SYSTEM, name).page.page_id == final.page_id


def test_archived_and_frozen_pages_are_not_merged_by_agents(store):
    clock = Clock()
    svc = service(store, clock)
    kept, extra = _two_pages(svc)
    gone = svc.write(SYSTEM, "team/gone", draft("Old.", "Gone")).page
    svc.archive(SYSTEM, "team/gone", base_revision=gone.revision, reason="Outdated")
    with pytest.raises(KnowledgeError) as archived:
        svc.merge(SYSTEM, extra.reference(), "team/gone", draft("x"), base_revision=2, source_revision=1)
    assert archived.value.code == CODE_INVALID and "archived" in archived.value.message

    svc.freeze(SYSTEM, kept.reference(), minutes=30)
    with pytest.raises(KnowledgeError) as frozen:
        _merge(svc, SYSTEM, kept, extra)
    assert frozen.value.code == CODE_FROZEN
    assert store.get_redirect_target(extra.key) is None
    # A person's merge is not stopped by a freeze, which applies to agents.
    assert _merge(svc, person("ana"), kept, extra).target.revision == 2


def test_after_unfreezing_the_curator_merges(store):
    svc = service(store)
    kept, extra = _two_pages(svc)
    svc.freeze(SYSTEM, extra.reference(), minutes=30)
    svc.unfreeze(SYSTEM, extra.reference())
    assert _merge(svc, SYSTEM, kept, extra).source.status is PageStatus.ARCHIVED


def test_freezes_are_recorded_and_reported_but_are_not_writes(store):
    clock = Clock()
    svc = service(store, clock, edit_war_edits=2, window_minutes=30)
    kept, extra = _two_pages(svc)
    curator = agent("curator", agent_type="KnowledgeCuratorAgent")
    with pytest.raises(KnowledgeError) as zero:
        svc.freeze(curator, kept.reference(), minutes=0)
    assert zero.value.code == CODE_INVALID
    svc.freeze(curator, kept.reference(), minutes=60)
    # Two agents keep changing the other page until the store freezes it.
    a, b = agent("a", agent_type="Coder"), agent("b", agent_type="Researcher")
    page = extra
    for actor, text in ((a, "One."), (b, "Two."), (a, "Three.")):
        try:
            page = svc.write(actor, extra.reference(), draft(text), base_revision=page.revision).page
        except KnowledgeError as error:
            assert error.code == CODE_FROZEN
    alice = agent("w", person="alice")
    svc.write(alice, "personal/tea", draft("Green tea.", "Tea"))
    svc.freeze(alice, "personal/tea", minutes=60)

    report = svc.activity(curator, hours=24)
    frozen = {item.reference: item for item in report.recently_frozen}
    assert set(frozen) == {"team/release-process", "team/release-steps"}, "personal pages stay private"
    assert frozen["team/release-steps"].after_edit_war and not frozen["team/release-process"].after_edit_war
    assert frozen["team/release-process"].title == "Release process"
    assert "KnowledgeCuratorAgent" in {item.agent_type for item in report.by_agent_type}
    curator_writes = {item.agent_type: item for item in report.by_agent_type}["KnowledgeCuratorAgent"]
    assert curator_writes.applied == 2, "the curator's freeze is not counted as a write"

    svc.unfreeze(curator, kept.reference())
    after_unfreeze = {item.reference: item for item in svc.activity(curator, hours=24).recently_frozen}
    assert after_unfreeze["team/release-process"].frozen_until_utc is None
    clock.now = NOW + timedelta(hours=2)
    after_expiry = svc.activity(curator, hours=24)
    assert {item.reference for item in after_expiry.recently_frozen} == {"team/release-process", "team/release-steps"}
    assert next(item for item in after_expiry.recently_frozen if item.reference == "team/release-steps").frozen_until_utc
    assert after_expiry.frozen_pages == []
    clock.now = NOW + timedelta(days=2)
    assert svc.activity(curator, hours=24).recently_frozen == []
