"""Who may write where, and what stays private: the knowledge service's rules."""

from __future__ import annotations

import pytest

from knowledge_base.records import (
    CODE_DISABLED,
    CODE_INVALID,
    CODE_NOT_FOUND,
    CODE_REVIEW_REQUIRED,
    CODE_SCOPE,
    CODE_SENSITIVE,
    KnowledgeError,
)
from knowledge_base.service import PERSONAL_OVERLAP_KIND, DraftInput, KnowledgeService
from knowledge_base.vocabulary import ContributionKind, ContributionStatus, KnowledgeSpace, PageStatus
from runtime_settings.knowledge import KnowledgeSettings
from storage.knowledge_repository import create_knowledge_repository
from tests.knowledge_support import agent, knowledge_storage, person

ALICE_MEMORY = "Alice is recovering from knee surgery and prefers no stairs"


@pytest.fixture
def svc(tmp_path):
    with knowledge_storage("sqlite", tmp_path) as settings:
        repository = create_knowledge_repository(settings)
        yield KnowledgeService(
            repository,
            settings=KnowledgeSettings(),
            personal_texts=lambda person_id: [ALICE_MEMORY] if person_id == "alice" else [],
        )


def draft(body: str, title: str = "Page") -> DraftInput:
    return DraftInput(title=title, body=body)


def test_personal_always_means_the_actors_own_person(svc):
    alice, bob = agent("w1", person="alice"), agent("w2", person="bob")
    svc.write(alice, "personal/travel", draft("Window seats."))
    svc.write(bob, "personal/travel", draft("Aisle seats."))
    assert svc.read(alice, "personal/travel").page.body == "Window seats."
    assert svc.read(bob, "personal/travel").page.body == "Aisle seats."
    with pytest.raises(KnowledgeError) as nobody:
        svc.write(agent("sys"), "personal/travel", draft("x"))
    assert nobody.value.code == CODE_SCOPE
    with pytest.raises(KnowledgeError) as no_space:
        svc.read(agent("sys"), "travel")
    assert no_space.value.code == CODE_INVALID


def test_team_changes_from_a_persons_work_wait_for_review_and_stay_private(svc):
    system = agent("sys")
    page = svc.write(system, "team/ci", draft("CI runs on push.")).page
    alice = agent("w1", person="alice")
    result = svc.write(alice, "team/ci", draft("CI runs on push and nightly."), base_revision=page.revision)
    assert result.pending and result.contribution.kind is ContributionKind.EDIT
    assert svc.read(system, "team/ci").page.body == "CI runs on push."
    new = svc.write(alice, "team/new-page", draft("Brand new."))
    assert new.pending and new.contribution.kind is ContributionKind.CREATE
    with pytest.raises(KnowledgeError):
        svc.read(system, "team/new-page")
    # Visible in Alice's work only.
    assert svc.read(alice, "team/ci").pending_contributions[0].contribution_id == result.contribution.contribution_id
    assert svc.read(agent("w2", person="bob"), "team/ci").pending_contributions == []
    assert svc.list_contributions(agent("w2", person="bob")) == []


def test_another_persons_work_cannot_see_or_decide_a_contribution(svc):
    system = agent("sys")
    page = svc.write(system, "team/ci", draft("CI.")).page
    pending = svc.write(agent("w1", person="alice"), "team/ci", draft("Changed."), base_revision=page.revision).contribution
    bob_curator = agent("curator", person="bob", agent_type="KnowledgeCuratorAgent")
    for attempt in (
        lambda: svc.get_contribution(bob_curator, pending.contribution_id),
        lambda: svc.publish_contribution(bob_curator, pending.contribution_id),
        lambda: svc.reject_contribution(bob_curator, pending.contribution_id, reason="no"),
        lambda: svc.publish_contribution(system, pending.contribution_id),
    ):
        with pytest.raises(KnowledgeError) as refused:
            attempt()
        assert refused.value.code == CODE_NOT_FOUND
    with pytest.raises(KnowledgeError) as missing:
        svc.get_contribution(agent("w1", person="alice"), "kb_contrib_missing")
    assert missing.value.message.startswith("There is no contribution") and refused.value.code == missing.value.code


def test_overlap_with_personal_memories_is_flagged_for_review(svc):
    alice = agent("w1", person="alice")
    svc.write(alice, "personal/health", draft("Doctor visit every Monday morning at the clinic."))
    page = svc.write(agent("sys"), "team/offsite", draft("Offsite plan.")).page
    copied_memory = svc.write(
        alice, "team/offsite", draft("Offsite plan. Alice is recovering from knee surgery."), base_revision=page.revision
    )
    assert PERSONAL_OVERLAP_KIND in copied_memory.contribution.review_kinds
    copied_page = svc.write(alice, "team/schedule", draft("Standup moved: doctor visit every Monday morning."))
    assert PERSONAL_OVERLAP_KIND in copied_page.contribution.review_kinds
    unrelated = svc.write(alice, "team/tools", draft("We use ruff for linting."))
    assert unrelated.contribution.review_kinds == ()


def test_system_work_cannot_publish_contact_details_and_people_must_confirm(svc):
    with pytest.raises(KnowledgeError) as system:
        svc.write(agent("sys"), "team/vendor", draft("Support: help@vendor.example.com"))
    assert system.value.code == CODE_REVIEW_REQUIRED and system.value.details["review_kinds"] == ["email_address"]
    alice = person("alice")
    with pytest.raises(KnowledgeError) as unconfirmed:
        svc.write(alice, "team/vendor", draft("Support: help@vendor.example.com"))
    assert unconfirmed.value.code == CODE_REVIEW_REQUIRED
    saved = svc.write(alice, "team/vendor", draft("Support: help@vendor.example.com"), confirm_review=True)
    assert saved.page.revision == 1 and saved.review_kinds == ("email_address",)
    # Contact details in a person's own space need no review.
    assert svc.write(agent("w1", person="alice"), "personal/contacts", draft("Dentist: +1 415-555-0132")).page


def test_blocked_content_is_refused_in_every_space_and_again_when_publishing(svc):
    alice = agent("w1", person="alice")
    for reference in ("personal/creds", "team/creds"):
        with pytest.raises(KnowledgeError) as refused:
            svc.write(alice, reference, draft("db password: hunter22"))
        assert refused.value.code == CODE_SENSITIVE
        assert "hunter22" not in refused.value.message
    pending = svc.write(alice, "team/howto", draft("Ask the team lead.")).contribution
    curator = agent("curator", person="alice", agent_type="KnowledgeCuratorAgent")
    with pytest.raises(KnowledgeError) as on_publish:
        svc.publish_contribution(curator, pending.contribution_id, draft=DraftInput(body="Token: ghp_abcdefghijklmnopqrstuvwxyz0123"))
    assert on_publish.value.code == CODE_SENSITIVE
    assert svc.get_contribution(curator, pending.contribution_id).status is ContributionStatus.PENDING


def test_notes_and_archives_from_a_persons_work_are_published_by_review(svc):
    system = agent("sys")
    page = svc.write(system, "team/ci", draft("CI.")).page
    alice = agent("w1", person="alice")
    curator = agent("curator", person="alice", agent_type="KnowledgeCuratorAgent")
    note = svc.add_note(alice, "team/ci", "Nightly runs fail on Sundays.").contribution
    assert note.kind is ContributionKind.NOTE and svc.read(system, "team/ci").open_notes == []
    svc.publish_contribution(curator, note.contribution_id)
    assert [n.body for n in svc.read(system, "team/ci").open_notes] == ["Nightly runs fail on Sundays."]
    archive = svc.archive(alice, "team/ci", base_revision=page.revision, reason="Replaced").contribution
    archived = svc.publish_contribution(curator, archive.contribution_id).page
    assert archived.status is PageStatus.ARCHIVED
    rejected = svc.write(alice, "team/idea", draft("An idea.")).contribution
    assert svc.reject_contribution(curator, rejected.contribution_id, reason="Too vague").status is ContributionStatus.REJECTED
    withdrawn = svc.write(alice, "team/idea-2", draft("Another.")).contribution
    assert svc.withdraw_contribution(person("alice"), withdrawn.contribution_id).status is ContributionStatus.WITHDRAWN


def test_team_pages_never_link_into_a_personal_space(svc):
    system = agent("sys")
    svc.write(agent("w1", person="alice"), "personal/secret-plan", draft("Mine."))
    svc.write(system, "team/plan", draft("See [[personal/secret-plan]] and [[team/roadmap]] and [[roadmap]]."))
    view = svc.read(agent("w1", person="alice"), "personal/secret-plan")
    assert view.backlinks == []


def test_sizes_and_types_are_checked(svc):
    system = agent("sys")
    with pytest.raises(KnowledgeError) as untitled:
        svc.write(system, "team/x", DraftInput(body="text"))
    assert untitled.value.code == CODE_INVALID
    with pytest.raises(KnowledgeError) as long_body:
        svc.write(system, "team/x", draft("x" * 24001))
    assert "Split the page" in long_body.value.message
    with pytest.raises(KnowledgeError) as bad_type:
        svc.write(system, "team/x", DraftInput(title="X", body="y", page_type="memo"))
    assert bad_type.value.code == CODE_INVALID


def test_a_disabled_knowledge_base_refuses_everything(tmp_path):
    with knowledge_storage("sqlite", tmp_path) as settings:
        svc = KnowledgeService(create_knowledge_repository(settings), settings=KnowledgeSettings(enabled=False))
        with pytest.raises(KnowledgeError) as disabled:
            svc.search(agent("sys"), query="x")
        assert disabled.value.code == CODE_DISABLED


def test_the_team_space_is_shared_across_people(svc):
    svc.write(agent("sys"), "team/glossary", draft("SLA means service level agreement."))
    for reader in (agent("w1", person="alice"), agent("w2", person="bob"), agent("sys")):
        assert svc.read(reader, "team/glossary").page.space is KnowledgeSpace.TEAM


def test_a_proposed_page_whose_name_was_taken_is_published_as_an_edit(svc):
    alice = agent("w1", person="alice")
    curator = agent("curator", person="alice", agent_type="KnowledgeCuratorAgent")
    proposed = svc.write(alice, "team/runbook", draft("Restart the worker first.", title="Runbook")).contribution
    taken = svc.write(agent("sys"), "team/runbook", draft("Check the logs.", title="Runbook")).page
    with pytest.raises(KnowledgeError) as exists:
        svc.publish_contribution(curator, proposed.contribution_id)
    assert exists.value.code == "page_exists"
    merged = svc.publish_contribution(
        curator,
        proposed.contribution_id,
        draft=DraftInput(body="Check the logs, then restart the worker."),
        base_revision=taken.revision,
    ).page
    assert merged.revision == 2 and merged.body == "Check the logs, then restart the worker."


def test_change_summaries_and_archive_reasons_are_checked_like_page_text(svc):
    system = agent("sys")
    page = svc.write(system, "team/db", draft("How to reach the database.")).page
    with pytest.raises(KnowledgeError) as summary:
        svc.write(system, "team/db", draft("Updated."), base_revision=page.revision, change_summary="new password: hunter22")
    assert summary.value.code == CODE_SENSITIVE
    with pytest.raises(KnowledgeError) as reason:
        svc.archive(system, "team/db", base_revision=page.revision, reason="token ghp_abcdefghijklmnopqrstuvwxyz0123 rotated")
    assert reason.value.code == CODE_SENSITIVE
    with pytest.raises(KnowledgeError) as contact:
        svc.archive(system, "team/db", base_revision=page.revision, reason="Ask alice@example.com")
    assert contact.value.code == CODE_REVIEW_REQUIRED


def test_publishing_writes_a_fixed_history_entry_and_names_new_pages_from_the_reviewed_title(svc):
    alice = agent("w1", person="alice")
    curator = agent("curator", person="alice", agent_type="KnowledgeCuratorAgent")
    page = svc.write(agent("sys"), "team/ci", draft("CI runs on push.")).page
    edit = svc.write(alice, "team/ci", draft("CI runs on push and nightly."), base_revision=page.revision,
                     change_summary="Alice asked for this after her surgery").contribution
    svc.publish_contribution(curator, edit.contribution_id)
    history = svc.history(agent("sys"), "team/ci")[1]
    assert history[0].change_summary == "Edit published after review"
    assert all("surgery" not in revision.change_summary for revision in history)

    # A proposed page name is never published unless the reviewer repeats it.
    named = svc.write(alice, "team/alices-surgery-release-notes", draft("Tag before deploying.", title="Alice's notes")).contribution
    published = svc.publish_contribution(curator, named.contribution_id, draft=DraftInput(title="Release tagging")).page
    assert published.reference() == "team/release-tagging"
    assert svc.history(agent("sys"), "team/release-tagging")[1][0].change_summary == "New page published after review"
    with pytest.raises(KnowledgeError):
        svc.read(agent("sys"), "team/alices-surgery-release-notes")
    chosen = svc.write(alice, "team/x", draft("Rotate on-call weekly.", title="On-call")).contribution
    assert svc.publish_contribution(curator, chosen.contribution_id, page="on-call-rota").page.reference() == "team/on-call-rota"
    note = svc.add_note(alice, "team/ci", "Weekly run is slow.").contribution
    with pytest.raises(KnowledgeError) as only_new:
        svc.publish_contribution(curator, note.contribution_id, page="other")
    assert only_new.value.code == CODE_INVALID


@pytest.mark.parametrize("space,for_person", [("team", ""), ("team", "alice"), ("personal", "alice")])
@pytest.mark.parametrize(
    "page_name,tags",
    [
        ("123-45-6789", []),
        ("safe", ["123-45-6789"]),
        ("password: hunter22", []),
        ("safe", ["password: hunter22"]),
        ("123///45///6789", []),
        ("safe", ["123///45///6789"]),
    ],
)
def test_prohibited_metadata_is_refused_before_any_page_or_proposal_is_saved(svc, space, for_person, page_name, tags):
    actor = agent("writer", person=for_person)
    with pytest.raises(KnowledgeError) as refused:
        svc.write(actor, f"{space}/{page_name}", DraftInput(title="Safe title", body="Safe text.", tags=tags))
    assert refused.value.code == CODE_SENSITIVE
    assert "hunter22" not in str(refused.value.payload()) and "123-45-6789" not in str(refused.value.payload())
    assert svc.search(actor, query="") == []
    assert svc.list_contributions(actor) == []


@pytest.mark.parametrize("page,tags", [("team/123-45-6789", []), ("safe", ["password: hunter22"]), ("safe", ["123///45///6789"])])
def test_publishing_rechecks_reviewed_metadata_and_keeps_a_refused_contribution_pending(svc, page, tags):
    alice = agent("writer", person="alice")
    pending = svc.write(alice, "team/proposal", DraftInput(title="Safe title", body="Safe text.")).contribution
    curator = agent("curator", person="alice", agent_type="KnowledgeCuratorAgent")
    with pytest.raises(KnowledgeError) as refused:
        svc.publish_contribution(curator, pending.contribution_id, page=page, draft=DraftInput(tags=tags))
    assert refused.value.code == CODE_SENSITIVE
    assert "hunter22" not in str(refused.value.payload())
    assert svc.get_contribution(alice, pending.contribution_id).status is ContributionStatus.PENDING
    assert svc.search(agent("sys"), query="") == []


def test_metadata_cannot_hide_a_configured_blocked_pattern_by_normalizing_it(tmp_path):
    with knowledge_storage("sqlite", tmp_path) as settings:
        svc = KnowledgeService(
            create_knowledge_repository(settings),
            settings=KnowledgeSettings(sensitive_content={"blocked_patterns": [{"name": "project", "pattern": "bluebird"}]}),
        )
        with pytest.raises(KnowledgeError) as refused:
            svc.write(agent("sys"), "team/BLUEBIRD", DraftInput(title="Safe", body="Safe."))
        assert refused.value.code == CODE_SENSITIVE


def test_team_metadata_retains_contact_and_personal_memory_review_flags(svc):
    alice = agent("writer", person="alice")
    proposal = svc.write(
        alice,
        "team/alice@example.com",
        DraftInput(title="Safe title", body="Safe text.", tags=[ALICE_MEMORY]),
    ).contribution
    assert set(proposal.review_kinds) == {"email_address", PERSONAL_OVERLAP_KIND}
    with pytest.raises(KnowledgeError) as refused:
        svc.write(agent("sys"), "team/alice@example.com", DraftInput(title="Safe", body="Safe."))
    assert refused.value.code == CODE_REVIEW_REQUIRED


def test_rejection_reasons_obey_the_never_stored_rules(svc):
    alice = agent("writer", person="alice")
    pending = svc.write(alice, "team/proposal", DraftInput(title="Safe", body="Safe.")).contribution
    with pytest.raises(KnowledgeError) as refused:
        svc.reject_contribution(person("alice"), pending.contribution_id, reason="password: hunter22")
    assert refused.value.code == CODE_SENSITIVE and "hunter22" not in str(refused.value.payload())
    unchanged = svc.get_contribution(alice, pending.contribution_id)
    assert unchanged.status is ContributionStatus.PENDING and unchanged.decision_reason == ""


@pytest.mark.parametrize("title,reference", [("CI/CD pipeline", "team/ci-cd-pipeline"), ("personal/work notes", "team/personal-work-notes")])
def test_reviewed_titles_are_names_even_when_they_contain_a_slash(svc, title, reference):
    alice = agent("writer", person="alice")
    pending = svc.write(alice, "team/proposal", DraftInput(title="Proposed title", body="Safe text.")).contribution
    result = svc.publish_contribution(person("alice"), pending.contribution_id, draft=DraftInput(title=title))
    assert result.page.reference() == reference
    assert svc.read(agent("sys"), reference).page.title == title


def test_a_reviewed_page_name_cannot_choose_a_personal_space(svc):
    alice = agent("writer", person="alice")
    pending = svc.write(alice, "team/proposal", DraftInput(title="Safe", body="Safe.")).contribution
    with pytest.raises(KnowledgeError) as refused:
        svc.publish_contribution(person("alice"), pending.contribution_id, page="personal/proposal")
    assert refused.value.code == CODE_INVALID
    assert svc.get_contribution(alice, pending.contribution_id).status is ContributionStatus.PENDING


@pytest.mark.parametrize("summary,expected", [(None, "Private alice@example.com"), ("", ""), ("   ", "")])
def test_edits_distinguish_omitted_summaries_from_explicit_redactions(svc, summary, expected):
    alice = person("alice")
    current = svc.write(
        alice, "personal/guide", DraftInput(title="Guide", body="Read the guide.", summary="Private alice@example.com")
    ).page
    edited = svc.write(alice, "personal/guide", DraftInput(summary=summary), base_revision=current.revision).page
    assert edited.summary == expected
    assert edited.title == current.title and edited.body == current.body


@pytest.mark.parametrize("summary", ["", "   "])
def test_publishing_a_redacted_summary_never_copies_it_into_team_history(svc, summary):
    alice = agent("writer", person="alice")
    pending = svc.write(
        alice, "team/guide", DraftInput(title="Guide", body="Read the guide.", summary="Private alice@example.com")
    ).contribution
    published = svc.publish_contribution(person("alice"), pending.contribution_id, draft=DraftInput(summary=summary)).page
    assert published.summary == ""
    assert all("alice@example.com" not in revision.summary for revision in svc.history(person("bob"), "team/guide")[1])


def test_publishing_with_an_omitted_summary_keeps_the_reviewed_proposal_summary(svc):
    pending = svc.write(
        agent("writer", person="alice"), "team/guide", DraftInput(title="Guide", body="Read the guide.", summary="Guide overview")
    ).contribution
    published = svc.publish_contribution(person("alice"), pending.contribution_id, draft=DraftInput(body="Read the updated guide.")).page
    assert published.summary == "Guide overview"


@pytest.mark.parametrize("note", ["", "   "])
def test_an_explicitly_empty_edited_note_is_refused_without_publishing_the_original(svc, note):
    svc.write(person("bob"), "team/guide", draft("Read the guide."))
    pending = svc.add_note(agent("writer", person="alice"), "team/guide", "Private alice@example.com").contribution
    with pytest.raises(KnowledgeError) as empty:
        svc.publish_contribution(person("alice"), pending.contribution_id, note=note)
    assert empty.value.code == CODE_INVALID
    assert svc.read(person("bob"), "team/guide").open_notes == []
    assert svc.get_contribution(person("alice"), pending.contribution_id).status is ContributionStatus.PENDING


def test_publishing_an_edited_note_stores_only_its_reviewed_text(svc):
    svc.write(person("bob"), "team/guide", draft("Read the guide."))
    pending = svc.add_note(agent("writer", person="alice"), "team/guide", "Private alice@example.com").contribution
    published = svc.publish_contribution(person("alice"), pending.contribution_id, note="The guide changed.")
    assert published.note.body == "The guide changed."
    assert [note.body for note in svc.read(person("bob"), "team/guide").open_notes] == ["The guide changed."]


def test_publishing_an_edited_note_enforces_the_same_size_limit_as_adding_one(svc):
    svc.write(person("bob"), "team/guide", draft("Read the guide."))
    pending = svc.add_note(agent("writer", person="alice"), "team/guide", "The guide changed.").contribution
    with pytest.raises(KnowledgeError) as oversized:
        svc.publish_contribution(person("alice"), pending.contribution_id, note="x" * (KnowledgeSettings().limits.max_note_chars + 1))
    assert oversized.value.code == CODE_INVALID
    assert svc.read(person("bob"), "team/guide").open_notes == []
    assert svc.get_contribution(person("alice"), pending.contribution_id).status is ContributionStatus.PENDING
