"""The curator's tools and the knowledge recall step."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_agents.memory_plugins.base import MemoryPluginContext
from custom_agents.memory_plugins.hydration import KnowledgeRecallPlugin
from custom_messages.contracts.routing_v1 import MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.providers.knowledge import knowledge_add_note, knowledge_history, knowledge_write
from custom_tools.providers.knowledge import knowledge_read
from custom_tools.runtime_context import set_agent_ref
from custom_tools.providers.knowledge_curation import (
    freeze_knowledge_page,
    knowledge_activity_report,
    knowledge_problems,
    list_pending_contributions,
    merge_knowledge_pages,
    publish_contribution,
    read_contribution,
    revert_knowledge_page,
    unfreeze_knowledge_page,
)
from knowledge_base.records import CODE_CONFLICT, CODE_FROZEN, CODE_INVALID, CODE_NOT_FOUND, CODE_SCOPE, KnowledgeError
from knowledge_base.service import DraftInput, KnowledgeService
from knowledge_base.vocabulary import ContributionStatus, PageStatus
from runtime_settings.knowledge import KnowledgeSettings
from storage.knowledge_repository import create_knowledge_repository
from tests.knowledge_support import BACKENDS, NOW, TEAM, agent, knowledge_storage, person
from tests.test_knowledge_tools import _Agent, _call
from tests.test_knowledge_tools import _signed_task_contexts  # noqa: F401 - autouse fixture
from tests.tool_result_support import sdk_tool_result, tool_json


def _sdk_result(agent, tool, **inputs):
    set_agent_ref(agent)
    try:
        return asyncio.run(sdk_tool_result(tool, inputs))
    finally:
        set_agent_ref(None)


def test_curator_publication_refusal_fails_the_call_and_preserves_the_current_page(tmp_path):
    db = tmp_path / "knowledge.db"
    team_run = _Agent(db, signed_for=None)
    curator = _Agent(db, signed_for="alice")
    curator.named_agent_type = "KnowledgeCuratorAgent"
    _call(team_run, knowledge_write, page="team/guide", title="Guide", body="Initial guide.")
    pending = _call(curator, knowledge_write, page="team/guide", body="Proposed guide.", base_revision=1)
    _call(team_run, knowledge_write, page="team/guide", body="Newer guide.", base_revision=1)

    refused = _sdk_result(curator, publish_contribution, contribution_id=pending["contribution_id"])

    assert refused["status"] == "error", refused
    assert tool_json(refused)["error_code"] == CODE_CONFLICT
    page = _call(team_run, knowledge_read, page="team/guide")
    assert (page["body"], page["revision"]) == ("Newer guide.", 2)
    applied = _sdk_result(curator, publish_contribution, contribution_id=pending["contribution_id"],
                          body="Merged guide.", base_revision=2)
    assert applied["status"] == "success", applied
    assert tool_json(applied)["status"] == "published"
    page = _call(team_run, knowledge_read, page="team/guide")
    assert (page["body"], page["revision"]) == ("Merged guide.", 3)


def test_curator_merge_failure_leaves_both_pages_and_success_archives_the_source(tmp_path):
    curator = _Agent(tmp_path / "knowledge.db", signed_for="alice")
    curator.named_agent_type = "KnowledgeCuratorAgent"
    _call(curator, knowledge_write, page="personal/source", title="Source", body="Source text.")
    _call(curator, knowledge_write, page="personal/target", title="Target", body="Target text.")
    inputs = {"source": "personal/source", "target": "personal/target", "body": "Merged text.",
              "base_revision": 2, "source_revision": 1}

    refused = _sdk_result(curator, merge_knowledge_pages, **inputs)

    assert refused["status"] == "error", refused
    assert tool_json(refused)["error_code"] == CODE_CONFLICT
    assert _call(curator, knowledge_read, page="personal/source")["status"] == "active"
    assert _call(curator, knowledge_read, page="personal/target")["revision"] == 1
    applied = _sdk_result(curator, merge_knowledge_pages, **{**inputs, "base_revision": 1})
    assert applied["status"] == "success", applied
    page = _call(curator, knowledge_read, page="personal/target")
    assert (page["body"], page["revision"]) == ("Merged text.", 2)
    assert tool_json(applied)["archived"] == "personal/source"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    with knowledge_storage(request.param, tmp_path) as settings:
        yield create_knowledge_repository(settings)


def service(store, clock=None) -> KnowledgeService:
    return KnowledgeService(store, settings=KnowledgeSettings(), clock=clock or Clock())


def draft(body: str, title: str = "Page") -> DraftInput:
    return DraftInput(title=title, body=body)


def test_publishing_an_archive_keeps_every_field_of_the_current_page(store):
    svc = service(store)
    svc.write(person("bob"), "team/old-link", draft("Old reference."))
    svc.write(person("bob"), "team/current-link", draft("Current reference."))
    page = svc.write(
        person("bob"), "team/guide",
        DraftInput(title="Old guide", body="See [[old-link]].", summary="Old overview", page_type="how-to", tags=["old"]),
    ).page
    pending = svc.archive(agent("writer", person="alice"), "team/guide", base_revision=page.revision, reason="Replaced").contribution
    current = svc.write(
        person("bob"), "team/guide",
        DraftInput(title="Current guide", body="See [[current-link]].", summary="Current overview", page_type="decision", tags=["current"]),
        base_revision=page.revision,
    ).page

    with pytest.raises(KnowledgeError) as stale:
        svc.publish_contribution(person("alice"), pending.contribution_id)
    assert stale.value.code == CODE_CONFLICT
    with pytest.raises(KnowledgeError) as edited:
        svc.publish_contribution(person("alice"), pending.contribution_id, draft=DraftInput(body="An archive edit."), base_revision=current.revision)
    assert edited.value.code == CODE_INVALID

    archived = svc.publish_contribution(person("alice"), pending.contribution_id, base_revision=current.revision).page

    assert archived.status is PageStatus.ARCHIVED and archived.revision == current.revision + 1
    assert (archived.title, archived.body, archived.summary, archived.page_type, archived.tags) == (
        current.title, current.body, current.summary, current.page_type, current.tags
    )
    assert [link.slug for link in svc.page_links(archived, archived.body)] == ["current-link"]
    assert svc.get_contribution(person("alice"), pending.contribution_id).status is ContributionStatus.PUBLISHED


def test_archive_publication_refuses_a_writer_that_commits_after_its_read(store, monkeypatch):
    svc = service(store)
    page = svc.write(person("bob"), "team/guide", draft("First text.")).page
    pending = svc.archive(agent("writer", person="alice"), "team/guide", base_revision=page.revision, reason="Replaced").contribution
    original_apply = store.apply_page_change

    def apply_after_another_writer(change, **kwargs):
        monkeypatch.setattr(store, "apply_page_change", original_apply)
        svc.write(person("bob"), "team/guide", DraftInput(body="Another writer's text."), base_revision=page.revision)
        return original_apply(change, **kwargs)

    monkeypatch.setattr(store, "apply_page_change", apply_after_another_writer)
    with pytest.raises(KnowledgeError) as conflict:
        svc.publish_contribution(person("alice"), pending.contribution_id, base_revision=page.revision)

    assert conflict.value.code == CODE_CONFLICT
    current = svc.read(person("bob"), "team/guide").page
    assert current.status is PageStatus.ACTIVE and current.body == "Another writer's text." and current.revision == 2
    assert svc.get_contribution(person("alice"), pending.contribution_id).status is ContributionStatus.PENDING


def test_archive_publication_cannot_use_a_future_revision_to_replace_a_concurrent_edit(store, monkeypatch):
    svc = service(store)
    page = svc.write(person("bob"), "team/guide", draft("First text.")).page
    pending = svc.archive(agent("writer", person="alice"), "team/guide", base_revision=page.revision, reason="Replaced").contribution
    original_get = store.get_page

    def read_before_another_writer(key):
        snapshot = original_get(key)
        monkeypatch.setattr(store, "get_page", original_get)
        svc.write(person("bob"), "team/guide", DraftInput(body="Another writer's text."), base_revision=page.revision)
        return snapshot

    monkeypatch.setattr(store, "get_page", read_before_another_writer)
    with pytest.raises(KnowledgeError) as conflict:
        svc.publish_contribution(person("alice"), pending.contribution_id, base_revision=page.revision + 1)

    assert conflict.value.code == CODE_CONFLICT
    current = svc.read(person("bob"), "team/guide").page
    assert current.status is PageStatus.ACTIVE and current.body == "Another writer's text." and current.revision == 2
    assert svc.get_contribution(person("alice"), pending.contribution_id).status is ContributionStatus.PENDING


def test_publish_tool_distinguishes_omitted_summaries_from_empty_redactions(tmp_path):
    alice = _Agent(tmp_path / "kb.db", signed_for="alice")
    alice.named_agent_type = "KnowledgeCuratorAgent"
    for name, override, expected in (("keep", {}, "Private alice@example.com"), ("clear", {"summary": ""}, "")):
        pending = _call(
            alice, knowledge_write, page=f"team/{name}", title=name, body="Read the guide.", summary="Private alice@example.com"
        )
        published = _call(alice, publish_contribution, contribution_id=pending["contribution_id"], **override)
        assert published["ok"]
        assert _call(alice, knowledge_read, page=published["page"])["summary"] == expected


def test_publish_tool_refuses_a_blank_edited_note_and_publishes_only_a_redaction(tmp_path):
    db = tmp_path / "kb.db"
    system, alice = _Agent(db, signed_for=None), _Agent(db, signed_for="alice")
    alice.named_agent_type = "KnowledgeCuratorAgent"
    _call(system, knowledge_write, page="team/guide", title="Guide", body="Read the guide.")
    pending = _call(alice, knowledge_add_note, page="team/guide", note="Private alice@example.com")
    empty = _call(alice, publish_contribution, contribution_id=pending["contribution_id"], note="")
    assert empty["error_code"] == CODE_INVALID
    assert _call(system, knowledge_read, page="team/guide")["open_notes"] == []
    published = _call(alice, publish_contribution, contribution_id=pending["contribution_id"], note="The guide changed.")
    assert published["ok"]
    assert [note["text"] for note in _call(system, knowledge_read, page="team/guide")["open_notes"]] == ["The guide changed."]


@pytest.mark.parametrize("fields,expected", [({}, "Private overview"), ({"summary": ""}, "")])
def test_merge_tool_preserves_an_omitted_summary_and_clears_an_empty_summary(tmp_path, fields, expected):
    alice = _Agent(tmp_path / "kb.db", signed_for="alice")
    alice.named_agent_type = "KnowledgeCuratorAgent"
    _call(alice, knowledge_write, page="personal/guide", title="Guide", body="Guide text.", summary="Private overview")
    _call(alice, knowledge_write, page="personal/extra", title="Extra", body="Additional text.")
    merged = _call(
        alice, merge_knowledge_pages, source="personal/extra", target="personal/guide", body="Guide and additional text.",
        base_revision=1, source_revision=1, **fields,
    )
    assert merged["ok"]
    assert _call(alice, knowledge_read, page="personal/guide")["summary"] == expected


def test_activity_report_counts_writes_refusals_and_busy_pages(store):
    clock = Clock()
    svc = service(store, clock)
    a, b = agent("a", agent_type="Researcher"), agent("b", agent_type="Coder")
    page = svc.write(a, "team/ci", draft("v1")).page
    page = svc.write(b, "team/ci", draft("v2"), base_revision=page.revision).page
    with pytest.raises(KnowledgeError):
        svc.write(a, "team/secrets", draft("password: hunter22"))
    svc.freeze(agent("curator"), "team/ci", minutes=30)
    report = svc.activity(agent("curator"), hours=1)
    by_type = {item.agent_type: item for item in report.by_agent_type}
    assert by_type["Researcher"].applied == 1 and by_type["Researcher"].refused == {"sensitive_content": 1}
    assert by_type["Coder"].applied == 1
    assert report.busiest_pages[0] == ("team/ci", 2)
    assert report.frozen_pages[0][0] == "team/ci"


def test_problems_find_broken_links_stale_pages_and_open_notes(store):
    clock = Clock()
    svc = service(store, clock)
    system = agent("sys")
    svc.write(system, "team/old", draft("Old process."))
    clock.now = NOW + timedelta(days=200)
    svc.write(system, "team/release", draft("See [[missing-page]] and [[old]]."))
    svc.add_note(system, "team/release", "Step 3 changed.")
    problems = svc.problems(agent("curator"), stale_days=180)
    assert problems.broken_links == [("team/release", "team/missing-page")]
    assert [page for page, _ in problems.stale_pages] == ["team/old"]
    assert problems.pages_with_open_notes == [("team/release", 1)]


def test_notes_are_resolved_or_dismissed_only_where_the_curator_may_see_them(store):
    svc = service(store)
    system = agent("sys")
    svc.write(system, "team/release", draft("Steps."))
    svc.add_note(system, "team/release", "Step 3 changed.")
    alice = agent("w", person="alice")
    svc.write(alice, "personal/notes", draft("Mine."))
    svc.add_note(alice, "personal/notes", "Private observation.")
    team_note = svc.open_notes(agent("curator"), space="team")[0]
    private_note = svc.open_notes(alice, space="personal")[0]
    with pytest.raises(KnowledgeError) as other_person:
        svc.resolve_note(agent("curator", person="bob"), private_note.note_id, merged=True)
    assert other_person.value.code == CODE_NOT_FOUND
    svc.resolve_note(agent("curator"), team_note.note_id, merged=True)
    assert svc.open_notes(agent("curator"), space="team") == []
    svc.resolve_note(agent("curator", person="alice"), private_note.note_id, merged=False)


def test_freeze_stops_agents_and_revert_restores_an_earlier_revision(store):
    svc = service(store)
    system = agent("sys")
    page = svc.write(system, "team/policy", draft("Squash merges.")).page
    page = svc.write(agent("other"), "team/policy", draft("Merge commits, always, forever."), base_revision=page.revision).page
    curator = agent("curator")
    svc.freeze(curator, "team/policy", minutes=60)
    with pytest.raises(KnowledgeError) as frozen:
        svc.write(system, "team/policy", draft("Rebase."), base_revision=page.revision)
    assert frozen.value.code == CODE_FROZEN
    reverted = svc.revert(curator, "team/policy", to_revision=1, base_revision=page.revision, reason="Undo the looping edit").page
    assert reverted.revision == 3 and reverted.body == "Squash merges."
    with pytest.raises(KnowledgeError) as invalid:
        svc.revert(curator, "team/policy", to_revision=3, base_revision=3, reason="x")
    assert invalid.value.code == CODE_INVALID
    # One way to lift a freeze: unfreeze. A freeze of zero minutes is refused, not a lift.
    with pytest.raises(KnowledgeError) as zero:
        svc.freeze(curator, "team/policy", minutes=0)
    assert zero.value.code == CODE_INVALID and store.get_page_by_id(page.page_id).frozen_until_utc
    assert svc.unfreeze(curator, "team/policy").frozen_until_utc is None
    with pytest.raises(KnowledgeError) as not_frozen:
        svc.unfreeze(curator, "team/policy")
    assert not_frozen.value.code == CODE_INVALID
    assert svc.write(system, "team/policy", draft("Rebase."), base_revision=3).page.revision == 4


def test_curator_tools_show_only_the_runs_own_person(tmp_path):
    db = tmp_path / "kb.db"
    system = _Agent(db, signed_for=None)
    alice = _Agent(db, signed_for="alice")
    bob = _Agent(db, signed_for="bob")
    _call(system, knowledge_write, page="team/ci", title="CI", body="CI runs on push.")
    proposed = _call(alice, knowledge_write, page="team/ci", body="CI runs on push. Alice's laptop is slow.", base_revision=1)
    alice_curator = _Agent(db, signed_for="alice")
    alice_curator.named_agent_type = "KnowledgeCuratorAgent"
    listed = _call(alice_curator, list_pending_contributions)
    assert [c["contribution_id"] for c in listed["contributions"]] == [proposed["contribution_id"]]
    assert _call(bob, list_pending_contributions)["contributions"] == []
    detail = _call(alice_curator, read_contribution, contribution_id=proposed["contribution_id"])
    assert detail["current_page"]["body"] == "CI runs on push." and "laptop" in detail["text"]
    published = _call(alice_curator, publish_contribution, contribution_id=proposed["contribution_id"], body="CI runs on push.  Faster runners help.")
    assert published["status"] == "published" and published["revision"] == 2
    report = _call(alice_curator, knowledge_activity_report, hours=1)
    assert "alice" not in json.dumps(report)
    _call(system, knowledge_write, page="team/ci-copy", title="CI", body="CI runs on push. Faster runners help.")
    problems = _call(alice_curator, knowledge_problems)
    assert problems["duplicate_pages"] == [{"page": "team/ci", "duplicate": "team/ci-copy"}]


def test_a_persons_curator_cannot_revert_a_team_page_or_publish_a_private_reason(tmp_path):
    db = tmp_path / "kb.db"
    system = _Agent(db, signed_for=None)
    alice = _Agent(db, signed_for="alice")
    alice.named_agent_type = "KnowledgeCuratorAgent"
    bob = _Agent(db, signed_for="bob")
    _call(system, knowledge_write, page="team/policy", title="Policy", body="Squash merges.")
    _call(system, knowledge_write, page="team/policy", body="Merge commits.", base_revision=1)
    private_reason = "Alice suffers depression; contact alice@example.org for details."

    refused = _call(
        alice, revert_knowledge_page, page="team/policy", to_revision=1, base_revision=2, reason=private_reason
    )
    assert refused["error_code"] == CODE_SCOPE
    history = _call(bob, knowledge_history, page="team/policy")
    assert history["current_revision"] == 2
    assert private_reason not in json.dumps([refused, history])


def test_team_revert_history_uses_trusted_text_while_personal_reasons_stay_personal(store):
    svc = service(store)
    system = agent("sys")
    svc.write(system, "team/policy", draft("Squash merges."))
    svc.write(system, "team/policy", draft("Merge commits."), base_revision=1)
    private_reason = "Alice suffers depression; contact alice@example.org for details."
    svc.revert(person("alice"), "team/policy", to_revision=1, base_revision=2, reason=private_reason)
    _, revisions = svc.history(agent("bob-worker", person="bob"), "team/policy")
    assert revisions[0].change_summary == "Reverted to revision 1"
    assert private_reason not in revisions[0].change_summary

    alice = agent("alice-curator", person="alice")
    svc.write(alice, "personal/policy", draft("Squash merges."))
    svc.write(alice, "personal/policy", draft("Merge commits."), base_revision=1)
    svc.revert(alice, "personal/policy", to_revision=1, base_revision=2, reason=private_reason)
    assert private_reason in svc.history(alice, "personal/policy")[1][0].change_summary
    with pytest.raises(KnowledgeError):
        svc.history(agent("bob-worker", person="bob"), "personal/policy")


def test_frozen_notes_stay_open_until_a_later_run_can_merge_them(store):
    clock = Clock()
    svc = service(store, clock)
    curator = agent("curator", agent_type="KnowledgeCuratorAgent")
    svc.write(curator, "team/policy", draft("Squash merges."))
    note = svc.add_note(curator, "team/policy", "Run the checks first.").note
    svc.freeze(curator, "team/policy", minutes=60)
    with pytest.raises(KnowledgeError) as frozen:
        svc.write(curator, "team/policy", draft("Run the checks, then squash merge."), base_revision=1)
    assert frozen.value.code == CODE_FROZEN
    assert [n.note_id for n in svc.open_notes(curator)] == [note.note_id]

    clock.now += timedelta(minutes=61)
    svc.write(curator, "team/policy", draft("Run the checks, then squash merge."), base_revision=1)
    svc.resolve_note(curator, note.note_id, merged=True)
    assert svc.open_notes(curator) == []
    assert svc.read(curator, "team/policy").page.body == "Run the checks, then squash merge."


def test_exact_duplicate_candidates_are_scoped_and_disappear_after_cleanup(store):
    svc = service(store)
    system = agent("sys")
    for reference in ("team/release-a", "team/release-b", "team/release-c"):
        svc.write(system, reference, draft("Run the checks before publishing.", title="Release process"))
    svc.write(agent("alice-worker", person="alice"), "personal/release-d", draft("Run the checks before publishing.", title="Release process"))
    svc.write(replace(system, team_id="another-team"), "team/release-d", draft("Run the checks before publishing.", title="Release process"))
    problems = svc.problems(system)
    assert problems.duplicate_pages == [("team/release-a", "team/release-b"), ("team/release-a", "team/release-c")]
    svc.archive(system, "team/release-b", base_revision=1, reason="See release-a")
    svc.archive(system, "team/release-c", base_revision=1, reason="See release-a")
    assert svc.problems(system).duplicate_pages == []


def _context(store, person: str | None, text: str) -> MemoryPluginContext:
    return MemoryPluginContext(
        agent_id="lead",
        team_id=TEAM,
        named_agent_type="TeamArchitect",
        task=Task(task_type=MessageMetaType.REQUEST, problem_statement=text),
        msg_meta=MessageMeta(),
        canonical_user_id=person,
        request_timezone=None,
        current_utc=NOW.isoformat(),
        conversation_id=None,
        gateway_context=None,
        knowledge_repository=store,
        config={},
    )


def test_recall_lists_matching_pages_the_verified_person_may_see(store):
    svc = service(store)
    svc.write(agent("sys"), "team/deploy", DraftInput(title="Deploying", body="Deploy with docker compose.", summary="How we deploy."))
    svc.write(agent("w", person="alice"), "personal/deploy-notes", draft("My deploy checklist.", title="My deploy notes"))
    plugin = KnowledgeRecallPlugin()
    for_alice = plugin.hydrate(_context(store, "alice", "How do I deploy the gateway?"))[0].text
    assert "team/deploy: Deploying (How we deploy.)" in for_alice and "personal/deploy-notes" in for_alice
    for_bob = plugin.hydrate(_context(store, "bob", "How do I deploy the gateway?"))[0].text
    assert "personal/deploy-notes" not in for_bob
    for_nobody = plugin.hydrate(_context(store, None, "deploy"))[0].text
    assert "personal/" not in for_nobody
    assert plugin.hydrate(_context(store, "alice", "unrelated words entirely")) == []
    assert not plugin.enabled({}, _context(None, "alice", "deploy"))


def test_merge_and_unfreeze_tools_for_the_team_run_and_a_persons_run(tmp_path):
    db = tmp_path / "kb.db"
    team_run = _Agent(db, signed_for=None)
    team_run.named_agent_type = "KnowledgeCuratorAgent"
    alice_run = _Agent(db, signed_for="alice")
    alice_run.named_agent_type = "KnowledgeCuratorAgent"
    _call(team_run, knowledge_write, page="team/ci", title="CI", body="CI runs on push.")
    _call(team_run, knowledge_write, page="team/ci-runs", title="CI runs", body="CI runs nightly.")

    refused = _call(
        alice_run, merge_knowledge_pages, source="team/ci-runs", target="team/ci", body="x",
        base_revision=1, source_revision=1,
    )
    assert refused["error_code"] == CODE_SCOPE
    frozen = _call(team_run, freeze_knowledge_page, page="team/ci", minutes=30)
    assert frozen["frozen_until"]
    blocked = _call(
        team_run, merge_knowledge_pages, source="team/ci-runs", target="team/ci",
        body="CI runs on push and nightly.", base_revision=1, source_revision=1,
    )
    assert blocked["error_code"] == CODE_FROZEN
    assert _call(team_run, unfreeze_knowledge_page, page="team/ci") == {"ok": True, "page": "team/ci", "frozen_until": None}
    assert _call(team_run, unfreeze_knowledge_page, page="team/ci")["error_code"] == CODE_INVALID
    merged = _call(
        team_run, merge_knowledge_pages, source="team/ci-runs", target="team/ci",
        body="CI runs on push and nightly.", base_revision=1, source_revision=1,
    )
    assert merged == {
        "ok": True, "status": "merged", "page": "team/ci", "revision": 2, "archived": "team/ci-runs",
        "redirects_to": "team/ci",
    }
    read = _call(alice_run, knowledge_read, page="team/ci-runs")
    assert (read["page"], read["redirected_from"], read["body"]) == ("team/ci", "team/ci-runs", "CI runs on push and nightly.")
    history = _call(alice_run, knowledge_history, page="team/ci")
    assert [(item["change"], item["summary"]) for item in history["revisions"]][0] == (
        "merge", "Merged CI runs (team/ci-runs) into this page"
    )
