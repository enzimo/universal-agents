"""The knowledge store on real SQLite and PostgreSQL: one contract, both databases."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from knowledge_base.records import (
    CODE_CONFLICT,
    CODE_EXISTS,
    CODE_FROZEN,
    CODE_LIMIT,
    CODE_NOT_PENDING,
    CODE_REPEAT,
    KnowledgeError,
)
from knowledge_base.service import DraftInput, KnowledgeService
from knowledge_base.vocabulary import ContributionStatus, KnowledgeSpace, PageStatus
from runtime_settings.knowledge import KnowledgeSettings, KnowledgeWriteLimits
from storage.knowledge_repository import (
    KNOWLEDGE_TABLE_COLUMNS,
    create_knowledge_repository,
)
from tests.knowledge_support import BACKENDS, NOW, agent, knowledge_storage, person


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, **delta) -> None:
        self.now = self.now + timedelta(**delta)


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    with knowledge_storage(request.param, tmp_path) as settings:
        yield create_knowledge_repository(settings)


def service(repository, clock=None, **limits) -> KnowledgeService:
    settings = KnowledgeSettings(limits=KnowledgeWriteLimits(**limits)) if limits else KnowledgeSettings()
    return KnowledgeService(repository, settings=settings, clock=clock or Clock())


def draft(body: str, title: str = "Release process", **fields) -> DraftInput:
    return DraftInput(title=title, body=body, **fields)


def test_columns_match_the_created_tables(store):
    for table, columns in KNOWLEDGE_TABLE_COLUMNS.items():
        rows = store.export_rows(table)
        assert rows == []
    # Insert one of everything and compare the stored columns with the declared ones.
    svc = service(store)
    system = agent("sys")
    page = svc.write(system, "team/release", draft("Run [[ci]] first.")).page
    svc.add_note(system, "team/release", "Also tag it.")
    svc.write(agent("w", person="alice"), "team/release", draft("Changed."), base_revision=page.revision)
    old = svc.write(system, "team/release-steps", draft("Tag.", title="Release steps")).page
    svc.merge(system, "team/release-steps", "team/release", draft("Run [[ci]] first, then tag."),
              base_revision=page.revision, source_revision=old.revision)
    for table, columns in KNOWLEDGE_TABLE_COLUMNS.items():
        rows = store.export_rows(table)
        assert rows, table
        assert tuple(rows[0]) == columns, table


def test_create_edit_and_stale_edit(store):
    svc = service(store)
    system = agent("sys")
    created = svc.write(system, "team/Release Process", draft("Tag, then deploy.", tags=["Release", "release", "CI"])).page
    assert (created.reference(), created.revision, created.tags) == ("team/release-process", 1, ("release", "ci"))
    edited = svc.write(system, "team/release-process", draft("Tag, test, then deploy."), base_revision=1).page
    assert edited.revision == 2 and edited.title == "Release process"
    with pytest.raises(KnowledgeError) as stale:
        svc.write(agent("other"), "team/release-process", draft("Mine."), base_revision=1)
    assert stale.value.code == CODE_CONFLICT and stale.value.details["current_revision"] == 2
    with pytest.raises(KnowledgeError) as exists:
        svc.write(system, "team/release-process", draft("Again."))
    assert exists.value.code == CODE_EXISTS
    revisions = store.list_revisions(edited.page_id, limit=10)
    assert [r.revision for r in revisions] == [2, 1]
    assert store.get_revision(edited.page_id, 1).body == "Tag, then deploy."


def test_search_ranks_all_words_then_widens_and_respects_spaces(store):
    svc = service(store)
    system = agent("sys")
    alice = agent("w", person="alice")
    bob = agent("w2", person="bob")
    svc.write(system, "team/deploy", draft("How to deploy the gateway with docker compose.", title="Deploying"))
    svc.write(system, "team/backup", draft("Nightly backups of the database.", title="Backups"))
    svc.write(alice, "personal/notes", draft("Alice deploys on Fridays.", title="My deploy notes"))

    both = [hit.page.reference() for hit in svc.search(alice, query="deploy")]
    assert set(both) == {"team/deploy", "personal/notes"}
    assert [hit.page.reference() for hit in svc.search(bob, query="deploy")] == ["team/deploy"]
    assert [hit.page.reference() for hit in svc.search(system, query="deploy")] == ["team/deploy"]
    # Stemming: "deploying" finds "deploy".
    assert "team/deploy" in [hit.page.reference() for hit in svc.search(system, query="deploying gateway")]
    # No page has both words, so the search widens to either word.
    widened = {hit.page.reference() for hit in svc.search(system, query="docker backups")}
    assert widened == {"team/deploy", "team/backup"}
    hit = svc.search(system, query="docker")[0]
    assert "[docker]" in hit.snippet.lower()
    # An empty query lists recent pages; query syntax is never passed through.
    assert len(svc.search(system, query="")) == 2
    assert svc.search(system, query='deploy" OR "x*') is not None


def test_archived_pages_leave_search_and_an_edit_restores_them(store):
    svc = service(store)
    system = agent("sys")
    svc.write(system, "team/old", draft("Old deploy steps."))
    archived = svc.archive(system, "team/old", base_revision=1, reason="Replaced by team/deploy").page
    assert archived.status is PageStatus.ARCHIVED
    assert svc.search(system, query="deploy") == []
    restored = svc.write(system, "team/old", draft("Old deploy steps, still valid."), base_revision=2).page
    assert restored.status is PageStatus.ACTIVE and restored.revision == 3


def test_an_agent_can_change_only_a_personal_pages_type(store):
    svc = service(store)
    worker = agent("worker", person="alice")
    created = svc.write(worker, "personal/release", draft("Run tests before tagging.", page_type="reference")).page

    edited = svc.write(worker, "personal/release", DraftInput(page_type="how-to"), base_revision=created.revision).page

    assert edited.page_type == "how-to" and edited.revision == 2
    assert (edited.title, edited.body, edited.summary, edited.tags) == (
        created.title, created.body, created.summary, created.tags
    )
    assert [hit.page.page_id for hit in svc.search(worker, query="tagging", page_type="how-to")] == [edited.page_id]
    assert svc.search(worker, query="tagging", page_type="reference") == []
    for page_type, repeated_revision in (("how-to", 2), ("reference", 1)):
        with pytest.raises(KnowledgeError) as repeat:
            svc.write(worker, "personal/release", DraftInput(page_type=page_type), base_revision=edited.revision)
        assert repeat.value.code == CODE_REPEAT
        assert repeat.value.details["repeated_revision"] == repeated_revision


def test_backlinks_show_only_pages_the_reader_may_see(store):
    svc = service(store)
    system = agent("sys")
    alice = agent("w", person="alice")
    svc.write(system, "team/ci", draft("CI.", title="CI"))
    svc.write(system, "team/release", draft("See [[ci]].", title="Release"))
    svc.write(alice, "personal/mine", draft("I use [[team/ci]] daily.", title="Mine"))
    team_page = store.get_page(svc.page_key(system, "team/ci"))
    by_alice = {p.reference() for p in store.backlinks(team_page, reader_person_id="alice", limit=10)}
    by_bob = {p.reference() for p in store.backlinks(team_page, reader_person_id="bob", limit=10)}
    assert by_alice == {"team/release", "personal/mine"}
    assert by_bob == {"team/release"}
    by_nobody = {p.reference() for p in store.backlinks(team_page, reader_person_id="", limit=10)}
    assert by_nobody == {"team/release"}
    assert [p.reference() for p in svc.read(system, "team/ci").backlinks] == ["team/release"]


def test_hourly_write_limit_counts_every_write_of_one_agent(store):
    clock = Clock()
    svc = service(store, clock, writes_per_agent_per_hour=3)
    worker = agent("sys")
    svc.write(worker, "team/a", draft("A."))
    svc.write(worker, "team/b", draft("B."))
    svc.add_note(worker, "team/a", "note")
    with pytest.raises(KnowledgeError) as limited:
        svc.write(worker, "team/c", draft("C."))
    assert limited.value.code == CODE_LIMIT and limited.value.details["limit"] == "writes_per_agent_per_hour"
    # Refusals do not count, other agents are unaffected, and the hour passes.
    svc.write(agent("other"), "team/c", draft("C."))
    clock.advance(minutes=61)
    svc.write(worker, "team/d", draft("D."))


def test_page_edit_limit_and_new_pages_per_request(store):
    clock = Clock()
    svc = service(store, clock, page_edits_per_agent=2, new_pages_per_work=2)
    worker = agent("sys", work_id="request_1")
    page = svc.write(worker, "team/a", draft("v1")).page
    page = svc.write(worker, "team/a", draft("v2"), base_revision=page.revision).page
    page = svc.write(worker, "team/a", draft("v3"), base_revision=page.revision).page
    with pytest.raises(KnowledgeError) as edits:
        svc.write(worker, "team/a", draft("v4"), base_revision=page.revision)
    assert edits.value.details["limit"] == "page_edits_per_agent"
    svc.write(agent("helper", work_id="request_1"), "team/b", draft("B."))
    with pytest.raises(KnowledgeError) as created:
        svc.write(agent("third", work_id="request_1"), "team/c", draft("C."))
    assert created.value.details["limit"] == "new_pages_per_work"
    svc.write(agent("third", work_id="request_2"), "team/c", draft("C."))


def test_archive_limit_counts_pending_and_applied_writes_and_resets_after_an_hour(store):
    clock = Clock()
    svc = service(store, clock, archives_per_agent_per_hour=2)
    worker = agent("worker", person="alice")
    svc.write(worker, "personal/old", draft("Old instructions."))
    for slug in ("a", "b"):
        svc.write(person("admin"), f"team/{slug}", draft("Old instructions."))

    applied = svc.archive(worker, "personal/old", base_revision=1, reason="Outdated").page
    pending = svc.archive(worker, "team/a", base_revision=1, reason="Outdated").contribution
    assert applied.status is PageStatus.ARCHIVED and pending.status is ContributionStatus.PENDING

    for advance in (0, 59):
        clock.advance(minutes=advance)
        with pytest.raises(KnowledgeError) as limited:
            svc.archive(worker, "team/b", base_revision=1, reason="Outdated")
        assert limited.value.code == CODE_LIMIT
        assert limited.value.details["limit"] == "archives_per_agent_per_hour"
    assert [c.contribution_id for c in svc.list_contributions(worker)] == [pending.contribution_id]
    assert svc.read(worker, "team/b").page.status is PageStatus.ACTIVE

    clock.advance(minutes=2)
    after_hour = svc.archive(worker, "team/b", base_revision=1, reason="Outdated").contribution
    assert after_hour.status is ContributionStatus.PENDING
    assert len(svc.list_contributions(worker)) == 2


def test_repeating_a_recent_revision_is_refused(store):
    svc = service(store)
    a, b = agent("a"), agent("b")
    page = svc.write(a, "team/policy", draft("Use squash merges.")).page
    page = svc.write(b, "team/policy", draft("Use merge commits."), base_revision=page.revision).page
    with pytest.raises(KnowledgeError) as repeat:
        svc.write(a, "team/policy", draft("Use  squash merges.  "), base_revision=page.revision)
    assert repeat.value.code == CODE_REPEAT and repeat.value.details["repeated_revision"] == 1
    with pytest.raises(KnowledgeError) as unchanged:
        svc.write(a, "team/policy", draft("use merge commits."), base_revision=page.revision)
    assert unchanged.value.code == CODE_REPEAT


def test_edit_war_freezes_the_page_for_agents_but_not_people(store):
    clock = Clock()
    svc = service(store, clock, edit_war_edits=4, page_edits_per_agent=10, edit_war_freeze_minutes=60)
    agents = [agent("a"), agent("b")]
    page = svc.write(agents[0], "team/hot", draft("v0")).page
    for index in range(4):
        page = svc.write(agents[index % 2], "team/hot", draft(f"v{index + 1}"), base_revision=page.revision).page
    with pytest.raises(KnowledgeError) as frozen:
        svc.write(agents[0], "team/hot", draft("v5"), base_revision=page.revision)
    assert frozen.value.code == CODE_FROZEN
    stored = store.get_page_by_id(page.page_id)
    assert stored.frozen_until_utc and stored.revision == page.revision
    with pytest.raises(KnowledgeError) as still:
        svc.write(agent("c"), "team/hot", draft("v5"), base_revision=page.revision)
    assert still.value.code == CODE_FROZEN
    # Notes still work, and a person may edit a frozen page.
    svc.add_note(agent("c"), "team/hot", "I think v2 was right.")
    svc.write(person("alice"), "team/hot", draft("Settled."), base_revision=page.revision)
    clock.advance(minutes=61)
    current = store.get_page_by_id(page.page_id)
    svc.write(agents[0], "team/hot", draft("After the freeze."), base_revision=current.revision)


def test_refusals_are_recorded_for_the_activity_report(store):
    svc = service(store, writes_per_agent_per_hour=1)
    worker = agent("sys")
    svc.write(worker, "team/a", draft("A."))
    with pytest.raises(KnowledgeError):
        svc.write(worker, "team/b", draft("B."))
    with pytest.raises(KnowledgeError):
        svc.write(worker, "team/c", draft("password: hunter22"))
    with store._connect() as conn:  # the edit log is not part of the store's interface
        rows = conn.execute("SELECT action, outcome, reason FROM knowledge_write_events ORDER BY created_at_utc").fetchall()
    outcomes = [(row["outcome"], row["reason"]) for row in rows]
    assert ("applied", "") in outcomes
    assert ("refused", CODE_LIMIT) in outcomes
    assert ("refused", "sensitive_content") in outcomes


def test_contributions_are_private_and_publish_once(store):
    svc = service(store)
    system = agent("sys")
    page = svc.write(system, "team/ci", draft("CI runs on push.", title="CI")).page
    alice_work = agent("w", person="alice")
    proposed = svc.write(alice_work, "team/ci", draft("CI runs on push and nightly."), base_revision=page.revision)
    assert proposed.pending and store.get_page_by_id(page.page_id).revision == 1
    contribution = proposed.contribution
    assert [c.contribution_id for c in svc.list_contributions(alice_work)] == [contribution.contribution_id]
    assert svc.list_contributions(agent("w2", person="bob")) == []
    curator = agent("curator", person="alice", agent_type="KnowledgeCuratorAgent")
    published = svc.publish_contribution(curator, contribution.contribution_id)
    assert published.page.revision == 2 and "nightly" in published.page.body
    assert store.get_contribution(contribution.contribution_id).status is ContributionStatus.PUBLISHED
    with pytest.raises(KnowledgeError) as again:
        svc.publish_contribution(curator, contribution.contribution_id)
    assert again.value.code == CODE_NOT_PENDING
    revision = store.get_revision(page.page_id, 2)
    assert revision.contribution_id == contribution.contribution_id and revision.work_person_id == "alice"


def test_contribution_pagination_and_count_share_owner_team_status_and_page_filters(store):
    svc = service(store)
    alice = person("alice")
    primary = svc.write(alice, "team/primary", draft("Primary page.")).page
    secondary = svc.write(alice, "team/secondary", draft("Secondary page.")).page
    expected = []
    for number in range(101):
        contribution = svc.add_note(
            agent(f"worker-{number}", person="alice"), "team/primary", f"Observation {number}.",
        ).contribution
        expected.append(contribution.contribution_id)
    # The fixed clock ties all timestamps. The id order keeps offsets stable.
    assert len({c["created_at_utc"] for c in store.export_rows("knowledge_contributions")}) == 1
    bob = svc.add_note(agent("bob-worker", person="bob"), "team/primary", "Bob's observation.").contribution
    rejected = svc.add_note(agent("rejected", person="alice"), "team/primary", "Rejected.").contribution
    svc.reject_contribution(alice, rejected.contribution_id, reason="Outdated")
    other_page = svc.add_note(agent("other-page", person="alice"), "team/secondary", "Another page.").contribution
    svc.reject_contribution(alice, other_page.contribution_id, reason="Outdated")
    other_team = replace(alice, team_id="other-team")
    svc.write(other_team, "team/primary", draft("Another team's primary page."))
    svc.add_note(replace(agent("other-team-worker", person="alice"), team_id="other-team"),
                 "team/primary", "Another team's observation.")

    filters = dict(team_id=alice.team_id, owner_person_id="alice", status=ContributionStatus.PENDING)
    chunks = [store.list_contributions(**filters, limit=50, offset=offset) for offset in (0, 50, 100)]
    found = [c.contribution_id for chunk in chunks for c in chunk]
    assert [len(chunk) for chunk in chunks] == [50, 50, 1]
    assert found == sorted(expected) and len(set(found)) == 101
    assert store.count_contributions(**filters) == 101
    assert store.list_contributions(**filters, limit=50, offset=101) == []
    for status, page_id, total in (
        (None, "", 103),
        (None, primary.page_id, 102),
        (None, secondary.page_id, 1),
        (ContributionStatus.REJECTED, "", 2),
        (ContributionStatus.PENDING, secondary.page_id, 0),
    ):
        scoped = {**filters, "status": status, "page_id": page_id}
        assert store.count_contributions(**scoped) == total
        assert len(store.list_contributions(**scoped, limit=200)) == total
    assert store.count_contributions(team_id=alice.team_id, owner_person_id="bob", status=None) == 1
    assert [c.contribution_id for c in store.list_contributions(
        team_id=alice.team_id, owner_person_id="bob", status=None, limit=50,
    )] == [bob.contribution_id]
    assert store.count_contributions(team_id="other-team", owner_person_id="alice", status=None) == 1


def test_transfer_rows_round_trip_with_ids(store, tmp_path):
    svc = service(store)
    svc.write(agent("sys"), "team/ci", draft("CI. See [[release]].", title="CI"))
    svc.write(agent("w", person="alice"), "personal/mine", draft("Mine.", title="Mine"))
    exported = {table: store.export_rows(table) for table in KNOWLEDGE_TABLE_COLUMNS}
    target_settings = None
    from storage.config import resolve_storage_settings

    target_settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "target.db")}})
    target = create_knowledge_repository(target_settings)
    for table, rows in exported.items():
        assert target.import_rows(table, rows) == len(rows)
        assert target.import_rows(table, rows) == 0  # already there: kept as is
    copied = KnowledgeService(target, settings=KnowledgeSettings())
    assert [hit.page.reference() for hit in copied.search(agent("x"), query="ci")] == ["team/ci"]
    assert copied.read(agent("w", person="alice"), "personal/mine").page.space is KnowledgeSpace.PERSONAL
