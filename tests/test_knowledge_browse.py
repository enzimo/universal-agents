"""People's page lists (``KnowledgeService.browse``): one space, a page of results and the total.

The total must agree with what ``search`` returns, including the widening
from all words to any word, on both databases.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from knowledge_base.records import CODE_SCOPE, KnowledgeError
from knowledge_base.service import DraftInput, KnowledgeService
from knowledge_base.vocabulary import ContributionStatus, KnowledgeSpace, PageStatus
from runtime_settings.knowledge import KnowledgeSettings
from storage.knowledge_repository import create_knowledge_repository
from tests.knowledge_support import BACKENDS, agent, knowledge_storage, person


@pytest.fixture(params=BACKENDS)
def svc(request, tmp_path):
    with knowledge_storage(request.param, tmp_path) as settings:
        yield KnowledgeService(create_knowledge_repository(settings), settings=KnowledgeSettings())


def _all_hits(svc: KnowledgeService, actor, query: str, space: str) -> list[str]:
    """Every page search finds, read in pages of 20."""

    found: list[str] = []
    while True:
        hits, _total = svc.browse(actor, query=query, space=space, limit=20, offset=len(found))
        found.extend(hit.page.reference() for hit in hits)
        if len(hits) < 20:
            return found


def test_browse_pages_one_space_with_an_exact_total(svc):
    # People's writes carry no write limits, so one person can create them all.
    carol = person("carol")
    for number in range(25):
        svc.write(carol, f"team/guide-{number}", DraftInput(title=f"Guide {number}", body=f"Steps for guide {number}."))
    svc.write(person("alice"), "personal/travel", DraftInput(title="Travel", body="Window seats."))
    svc.write(person("bob"), "personal/travel", DraftInput(title="Travel", body="Aisle seats."))

    first, total = svc.browse(person("alice"), query="", space="team", limit=20)
    rest, _ = svc.browse(person("alice"), query="", space="team", limit=20, offset=20)
    personal, personal_total = svc.browse(person("alice"), query="", space="personal", limit=20)

    assert (len(first), len(rest), total) == (20, 5, 25)
    assert {hit.page.reference() for hit in first} | {hit.page.reference() for hit in rest} == {
        f"team/guide-{number}" for number in range(25)
    }
    assert personal_total == 1 and [hit.page.body for hit in personal] == ["Window seats."]
    with pytest.raises(KnowledgeError) as nobody:
        svc.browse(agent("sys"), query="", space="personal", limit=20)
    assert nobody.value.code == CODE_SCOPE


def test_the_total_follows_search_widening_from_all_words_to_any_word(svc):
    system = agent("sys")
    svc.write(system, "team/backups", DraftInput(title="Backups", body="Nightly backups to the vault."))
    svc.write(system, "team/restores", DraftInput(title="Restores", body="Restore from the vault weekly."))
    svc.write(system, "team/vault", DraftInput(title="Vault", body="The vault keeps backups for a year."))
    svc.write(system, "team/lunch", DraftInput(title="Lunch", body="Tacos on Friday."))
    reader = person("alice")

    for query in ("vault", "backups vault", "nightly restore", "tacos backups", "nothing matches"):
        found = _all_hits(svc, reader, query, "team")
        _hits, total = svc.browse(reader, query=query, space="team", limit=20)
        assert total == len(found), query
    assert svc.browse(reader, query="nightly restore", space="team", limit=20)[1] == 2
    assert svc.browse(reader, query="", space=KnowledgeSpace.TEAM.value, limit=20)[1] == 4


def test_merged_pages_leave_lists_searches_and_totals(svc):
    carol = person("carol")
    svc.write(carol, "team/backups", DraftInput(title="Backups", body="Nightly backups to the vault."))
    svc.write(carol, "team/vault-backups", DraftInput(title="Vault backups", body="The vault keeps backups."))
    svc.merge(
        agent("curator"), "team/vault-backups", "team/backups",
        DraftInput(title="Backups", body="Nightly backups to the vault, kept for a year."),
        base_revision=1, source_revision=1,
    )

    listed, total = svc.browse(carol, query="", space="team", limit=20)
    found, found_total = svc.browse(carol, query="vault backups", space="team", limit=20)

    assert ([hit.page.reference() for hit in listed], total) == (["team/backups"], 1)
    assert ([hit.page.reference() for hit in found], found_total) == (["team/backups"], 1)


def test_archived_pages_can_be_found_and_restored_without_listing_merge_sources(svc):
    alice, bob = person("alice"), person("bob")
    for actor, reference in (
        (alice, "team/old-backups"),
        (alice, "team/old-restores"),
        (alice, "team/current"),
        (alice, "team/merged"),
        (alice, "personal/current"),
        (alice, "personal/merged"),
        (alice, "personal/old-backups"),
        (bob, "personal/merged"),
        (bob, "personal/old-backups"),
    ):
        body = "Private vault reminders and equipment inventory." if reference.startswith("personal/") else "Vault backup instructions."
        svc.write(actor, reference, DraftInput(title="Vault guide", body=body))
    for actor, reference in (
        (alice, "team/old-backups"), (alice, "team/old-restores"),
        (alice, "personal/merged"), (bob, "personal/merged"), (bob, "personal/old-backups"),
    ):
        svc.archive(actor, reference, base_revision=1, reason="Outdated")
    svc.merge(
        alice, "team/merged", "team/current",
        DraftInput(title="Vault guide", body="Combined vault backup instructions."),
        base_revision=1, source_revision=1,
    )
    svc.merge(
        alice, "personal/old-backups", "personal/current",
        DraftInput(title="Vault guide", body="Private vault reminders collected in one page."),
        base_revision=1, source_revision=1,
    )
    other_team = replace(alice, team_id="other-team")
    svc.write(other_team, "team/merged", DraftInput(title="Vault guide", body="Another team's vault backup instructions."))
    svc.archive(other_team, "team/merged", base_revision=1, reason="Outdated")

    for query in ("", "vault", "vault missing"):
        first, total = svc.browse(alice, query=query, space="team", status=PageStatus.ARCHIVED, limit=1)
        second, second_total = svc.browse(
            alice, query=query, space="team", status=PageStatus.ARCHIVED, limit=1, offset=1,
        )
        assert total == second_total == 2
        assert {hit.page.reference() for hit in first + second} == {"team/old-backups", "team/old-restores"}
        personal, personal_total = svc.browse(
            alice, query=query, space="personal", status=PageStatus.ARCHIVED, limit=20,
        )
        assert personal_total == 1 and [hit.page.owner_person_id for hit in personal] == ["alice"]
        bob_pages, bob_total = svc.browse(bob, query=query, space="personal", status=PageStatus.ARCHIVED, limit=20)
        assert bob_total == 2 and {hit.page.reference() for hit in bob_pages} == {
            "personal/merged", "personal/old-backups",
        }
        # Redirects in another team or space, or for another owner, must not
        # hide ordinary archived pages with the same name.
        other_pages, other_total = svc.browse(
            other_team, query=query, space="team", status=PageStatus.ARCHIVED, limit=20,
        )
        assert ([hit.page.reference() for hit in other_pages], other_total) == (["team/merged"], 1)
    active, active_total = svc.browse(alice, query="", space="team", limit=20)
    assert ([hit.page.reference() for hit in active], active_total) == (["team/current"], 1)
    # Agent searches keep their active-only default.
    assert [hit.page.reference() for hit in svc.search(agent("sys"), query="vault")] == ["team/current"]

    restored = svc.write(
        alice, "team/old-backups", DraftInput(body="Vault backup instructions are current."), base_revision=2,
    ).page
    assert restored.status is PageStatus.ACTIVE
    archived, archived_total = svc.browse(
        alice, query="vault", space="team", status=PageStatus.ARCHIVED, limit=20,
    )
    assert ([hit.page.reference() for hit in archived], archived_total) == (["team/old-restores"], 1)


def test_browse_contributions_returns_every_proposal_and_an_exact_total(svc):
    alice = person("alice")
    svc.write(alice, "team/backups", DraftInput(title="Backups", body="Keep backups."))
    # Independent workers avoid write-limit interference; all proposals are
    # reviewed through the same person's browser list.
    expected = {
        svc.add_note(agent(f"worker-{number}", person="alice"), "team/backups", f"Observation {number}.")
        .contribution.contribution_id
        for number in range(101)
    }
    svc.add_note(agent("bob-worker", person="bob"), "team/backups", "Bob's observation.")
    rejected = svc.add_note(agent("rejected-worker", person="alice"), "team/backups", "Rejected observation.")
    svc.reject_contribution(alice, rejected.contribution.contribution_id, reason="Not useful")

    found = []
    for offset in (0, 50, 100):
        contributions, total = svc.browse_contributions(alice, limit=50, offset=offset)
        assert total == 101
        found.extend(c.contribution_id for c in contributions)
        assert all(c.owner_person_id == "alice" and c.status is ContributionStatus.PENDING for c in contributions)
    assert len(found) == len(set(found)) == 101 and set(found) == expected
    assert svc.browse_contributions(alice, limit=50, offset=101) == ([], 101)
