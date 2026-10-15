"""The ``knowledge`` transfer section carries team pages and each imported person's own knowledge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from knowledge_base.service import DraftInput, KnowledgeService
from knowledge_base.vocabulary import ContributionKind, PageStatus
from runtime_settings.knowledge import KnowledgeSettings
from storage.knowledge_repository import create_knowledge_repository
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.knowledge import KnowledgeSection
from team_transfer.people import PeopleSection
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.knowledge_support import agent
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), PeopleSection(), KnowledgeSection())
TEAM = "team_a"


def _actor(actor_id: str, person: str = ""):
    base = agent(actor_id, person=person)
    from dataclasses import replace

    return replace(base, team_id=TEAM)


def _old_instance(tmp_path: Path) -> Path:
    from storage.gateway_state_repository import create_gateway_state_repository

    path, config = new_instance(tmp_path, "alpha", TEAM)
    people = create_gateway_state_repository(config.state_storage_settings)
    for person, role, status in (("usr_ana", "owner", "active"), ("usr_gone", "member", "removed")):
        people.create_gateway_user(canonical_user_id=person, display_name=person, timezone="UTC")
        people.create_team_membership(canonical_user_id=person, team_id=TEAM, role=role, status=status)
    svc = KnowledgeService(create_knowledge_repository(config.manager_storage_settings), settings=KnowledgeSettings())
    system = _actor("sys")
    page = svc.write(system, "team/ci", DraftInput(title="CI", body="CI runs on push. See [[release]].")).page
    svc.write(system, "team/ci", DraftInput(body="CI runs on push and nightly."), base_revision=page.revision)
    svc.add_note(system, "team/ci", "Nightly is slow.")
    deploy = svc.write(system, "team/deploy", DraftInput(title="Deploy", body="Deploy on Fridays.")).page
    extra = svc.write(system, "team/deploying", DraftInput(title="Deploying", body="Deploy after CI.")).page
    svc.merge(system, "team/deploying", "team/deploy", DraftInput(body="Deploy after CI, on Fridays."),
              base_revision=deploy.revision, source_revision=extra.revision)
    svc.write(_actor("w1", "usr_ana"), "personal/notes", DraftInput(title="Notes", body="Ana's own notes."))
    svc.write(_actor("w1", "usr_ana"), "team/idea", DraftInput(title="Idea", body="A pending team idea."))
    svc.write(_actor("w2", "usr_gone"), "personal/old", DraftInput(title="Old", body="Held until deletion."))
    svc.write(_actor("w3", "usr_stranger"), "personal/x", DraftInput(title="X", body="Not in the team."))
    return path


def _transfer(tmp_path: Path, config_path: Path) -> tuple[Any, Path]:
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=False, sections=SECTIONS)
    return plan, target


def test_knowledge_arrives_with_ids_and_people_kept(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    plan, target = _transfer(tmp_path, _old_instance(tmp_path))
    records = [r for r in plan.records if r.section == "knowledge"]
    refused = {r.key: r.reason for r in records if r.outcome is RecordOutcome.REFUSED}
    assert all("usr_stranger, who is not imported" in reason for reason in refused.values()) and refused
    assert not any("usr_gone" in key for key in refused), "a removed person's knowledge stays behind"

    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    copied = KnowledgeService(create_knowledge_repository(config.manager_storage_settings), settings=KnowledgeSettings())
    system = _actor("sys")
    ci = copied.read(system, "team/ci")
    assert ci.page.revision == 2 and [n.body for n in ci.open_notes] == ["Nightly is slow."]
    assert [h.page.reference() for h in copied.search(system, query="nightly")] == ["team/ci"]
    merged = copied.read(system, "team/deploying")
    assert (merged.page.reference(), merged.redirected_from) == ("team/deploy", "team/deploying")
    ana = _actor("w9", "usr_ana")
    assert copied.read(ana, "personal/notes").page.body == "Ana's own notes."
    assert [c.slug for c in copied.list_contributions(ana)] == ["idea"]
    assert copied.search(_actor("w9", "usr_gone"), query="held", space="personal") == []


def _reviewed_import(tmp_path: Path, *, keep_knowledge: bool) -> tuple[Any, Path]:
    """Export, open the review as an operator would, choose, then import with those choices."""

    from team_transfer.review import prepare_review

    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(_old_instance(tmp_path)), out, PASSPHRASE, owner_person_id="usr_ana", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        files = prepare_review(reader, target, tmp_path / "review")
        assert any(record.section == "knowledge" for record in files.review.records)
        if not keep_knowledge:
            files.save(files.review.model_copy(update={"records": [
                record.model_copy(update={"selected": record.section != "knowledge"})
                for record in files.review.records
            ]}))
        choices, overrides = files.choices(require_confirmation=True)
        plan = import_team(
            reader, target, dry_run=False, sections=SECTIONS, choices=choices, overrides=overrides,
        )
    return plan, target


def _team_pages(target: Path) -> list[str]:
    from assistant_gateway.config import GatewayInstanceConfig

    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    copied = KnowledgeService(create_knowledge_repository(config.manager_storage_settings), settings=KnowledgeSettings())
    return sorted(hit.page.reference() for hit in copied.search(_actor("sys"), query="deploy OR CI OR runs"))


def test_a_reviewed_import_carries_the_selected_knowledge(tmp_path: Path) -> None:
    """The review page lists knowledge rows and the import honors that selection.

    The review keyed every section's records but had no key for knowledge, so
    any backup with knowledge pages failed to open for review.
    """

    plan, target = _reviewed_import(tmp_path, keep_knowledge=True)
    assert any(r.section == "knowledge" and r.outcome is RecordOutcome.IMPORT for r in plan.records)
    assert "team/ci" in _team_pages(target)


def test_knowledge_left_unselected_in_review_stays_behind(tmp_path: Path) -> None:
    plan, target = _reviewed_import(tmp_path, keep_knowledge=False)
    assert not [r for r in plan.records if r.section == "knowledge"]
    assert any(r.section == "people" and r.outcome is RecordOutcome.IMPORT for r in plan.records)
    assert _team_pages(target) == []


def _knowledge_service(config_path: Path) -> KnowledgeService:
    from assistant_gateway.config import GatewayInstanceConfig

    config = GatewayInstanceConfig.load(config_path)
    return KnowledgeService(create_knowledge_repository(config.manager_storage_settings), settings=KnowledgeSettings())


def _export_review_source(tmp_path: Path, config_path: Path) -> Path:
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    return out


def _select_knowledge(files, keys: set[str]) -> dict[str, Any]:
    files.save(files.review.model_copy(update={"records": [
        record.model_copy(update={"selected": record.section != "knowledge" or record.key in keys})
        for record in files.review.records
    ]}))
    choices, overrides = files.choices(require_confirmation=True)
    return {"choices": choices, "overrides": overrides}


@pytest.mark.parametrize("kind", [ContributionKind.NOTE, ContributionKind.EDIT, ContributionKind.ARCHIVE])
def test_review_requires_a_contributions_page_and_can_import_it_later(tmp_path: Path, kind: ContributionKind) -> None:
    from team_transfer.errors import TransferError
    from team_transfer.review import prepare_review

    config_path = _old_instance(tmp_path)
    source = _knowledge_service(config_path)
    page = source.read(_actor("sys"), "team/ci").page
    writer = _actor("writer", "usr_ana")
    if kind is ContributionKind.NOTE:
        proposed = source.add_note(writer, "team/ci", "Please check the CI settings.")
    elif kind is ContributionKind.EDIT:
        proposed = source.write(writer, "team/ci", DraftInput(body="CI runs after operator review."), base_revision=page.revision)
    else:
        proposed = source.archive(writer, "team/ci", base_revision=page.revision, reason="CI has been replaced.")
    contribution = proposed.contribution
    assert contribution is not None
    key = f"contribution/{contribution.contribution_id}"
    target = tmp_path / "new"
    with TransferReader.open(_export_review_source(tmp_path, config_path), PASSPHRASE) as reader:
        files = prepare_review(reader, target, tmp_path / "review")
        arguments = _select_knowledge(files, {key})
        plan = import_team(reader, target, dry_run=True, sections=SECTIONS, **arguments)
        refused = plan.by_outcome(RecordOutcome.REFUSED)
        assert [(record.key, record.reason) for record in refused] == [(key, f"its page {page.page_id} is not imported")]
        with pytest.raises(TransferError, match="Selected records were refused"):
            import_team(reader, target, dry_run=False, sections=SECTIONS, require_valid=True, **arguments)
        assert not (target / "instance_config.yaml").exists()

        # A page imported in an earlier batch also satisfies the dependency.
        arguments = _select_knowledge(files, {f"page/{page.page_id}"})
        import_team(reader, target, dry_run=False, sections=SECTIONS, require_valid=True, **arguments)
        files = prepare_review(reader, target, tmp_path / "review")
        arguments = _select_knowledge(files, {key})
        import_team(reader, target, dry_run=False, sections=SECTIONS, allow_existing=True, require_valid=True, **arguments)

    copied = _knowledge_service(target / "instance_config.yaml")
    result = copied.publish_contribution(_actor("curator", "usr_ana"), contribution.contribution_id)
    if kind is ContributionKind.NOTE:
        assert result.note is not None and result.note.body == contribution.body
    elif kind is ContributionKind.EDIT:
        assert result.page.body == contribution.body
    else:
        assert result.page.status is PageStatus.ARCHIVED


def test_review_imports_a_new_page_contribution_without_a_page(tmp_path: Path) -> None:
    from team_transfer.review import prepare_review

    out = _export_review_source(tmp_path, _old_instance(tmp_path))
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        files = prepare_review(reader, target, tmp_path / "review")
        keys = {key for key, raw in files.archive["knowledge"].items()
                if raw["table"] == "contribution" and raw["row"]["slug"] == "idea"}
        assert len(keys) == 1
        arguments = _select_knowledge(files, keys)
        import_team(reader, target, dry_run=False, sections=SECTIONS, require_valid=True, **arguments)
    copied = _knowledge_service(target / "instance_config.yaml")
    [contribution] = copied.list_contributions(_actor("curator", "usr_ana"))
    result = copied.publish_contribution(_actor("curator", "usr_ana"), contribution.contribution_id)
    assert result.page.body == "A pending team idea."


@pytest.mark.parametrize("reselect_pages", [False, True])
@pytest.mark.parametrize("remove_link", [True, False])
def test_later_link_selection_checks_the_destination_pages_current_text(
    tmp_path: Path, remove_link: bool, reselect_pages: bool
) -> None:
    """Selecting the pages again does not make the backup's text decide the links:
    the destination keeps its own page rows, so its text is what the links must match."""
    from team_transfer.errors import TransferError
    from team_transfer.review import prepare_review

    config_path = _old_instance(tmp_path)
    source = _knowledge_service(config_path)
    ci = source.read(_actor("sys"), "team/ci").page
    page = source.write(_actor("sys"), "team/linked", DraftInput(title="Linked", body="Read [[team/ci|CI settings]].")).page
    out = _export_review_source(tmp_path, config_path)
    target = tmp_path / "new"
    key = f"link/{page.page_id}/team/ci"
    with TransferReader.open(out, PASSPHRASE) as reader:
        files = prepare_review(reader, target, tmp_path / "review")
        pages = {f"page/{page.page_id}", f"page/{ci.page_id}"}
        arguments = _select_knowledge(files, pages | {key})
        initial = import_team(reader, target, dry_run=True, sections=SECTIONS, **arguments)
        assert not initial.by_outcome(RecordOutcome.REFUSED)
        arguments = _select_knowledge(files, pages)
        import_team(reader, target, dry_run=False, sections=SECTIONS, require_valid=True, **arguments)
        copied = _knowledge_service(target / "instance_config.yaml")
        assert copied.read(_actor("sys"), "team/ci").backlinks == []
        body = "This page has no links now." if remove_link else page.body
        if remove_link:
            copied.write(_actor("sys"), "team/linked", DraftInput(body=body), base_revision=page.revision)
        files = prepare_review(reader, target, tmp_path / "review")
        arguments = _select_knowledge(files, {key} | (pages if reselect_pages else set()))
        plan = import_team(reader, target, dry_run=True, sections=SECTIONS, allow_existing=True, **arguments)
        [record] = [record for record in plan.records if record.section == "knowledge" and record.key == key]
        if remove_link:
            assert record.outcome is RecordOutcome.REFUSED
            assert "current text" in record.reason
            with pytest.raises(TransferError, match="Selected records were refused"):
                import_team(reader, target, dry_run=False, sections=SECTIONS, allow_existing=True, require_valid=True, **arguments)
        else:
            assert record.outcome is RecordOutcome.IMPORT
            import_team(reader, target, dry_run=False, sections=SECTIONS, allow_existing=True, require_valid=True, **arguments)
        assert copied.read(_actor("sys"), "team/linked").page.body == body
        backlinks = [page.reference() for page in copied.read(_actor("sys"), "team/ci").backlinks]
        assert backlinks == ([] if remove_link else ["team/linked"])


def test_rows_this_version_refuses_are_listed(tmp_path: Path) -> None:
    from team_transfer.knowledge import KnowledgeTable, _row_problem

    assert "columns" in _row_problem(KnowledgeTable.LINK, {"page_id": "p"})
    bad_space = {
        "page_id": "p", "team_id": "t", "owner_person_id": "", "target_space": "elsewhere", "target_slug": "x",
    }
    assert "refuses" in _row_problem(KnowledgeTable.LINK, bad_space)
    redirect = {
        "team_id": "t", "space": "elsewhere", "owner_person_id": "", "from_slug": "a", "from_page_id": "p1",
        "to_page_id": "p2", "created_at_utc": "2026-10-11T00:00:00.000000Z",
    }
    assert "refuses" in _row_problem(KnowledgeTable.REDIRECT, redirect)
    assert _row_problem(KnowledgeTable.REDIRECT, {**redirect, "space": "team"}) == ""


def test_a_redirect_is_imported_only_with_both_of_its_pages() -> None:
    from team_transfer.knowledge import KnowledgeTable, _import_problem

    redirect = {
        "team_id": "t", "space": "team", "owner_person_id": "", "from_slug": "a", "from_page_id": "p1",
        "to_page_id": "p2", "created_at_utc": "2026-10-11T00:00:00.000000Z",
    }
    problem = _import_problem(KnowledgeTable.REDIRECT, redirect, set(), set(), set(), {"p1"})
    assert problem == "its page p2 is not imported"
    assert _import_problem(KnowledgeTable.REDIRECT, redirect, set(), set(), set(), {"p1", "p2"}) == ""


@pytest.mark.parametrize("remove_link", [True, False])
def test_planning_checks_links_against_the_destinations_own_page_even_when_the_page_is_given_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remove_link: bool
) -> None:
    """``plan`` itself keeps the destination's row for a page it already has.

    Import paths drop present pages before planning, so this drives ``plan``
    directly with the backup's page row and its link.
    """

    from types import SimpleNamespace

    config_path = _old_instance(tmp_path)
    source = _knowledge_service(config_path)
    page = source.write(_actor("sys"), "team/linked", DraftInput(title="Linked", body="Read [[team/ci|CI settings]].")).page
    from assistant_gateway.config import GatewayInstanceConfig

    repository = create_knowledge_repository(GatewayInstanceConfig.load(config_path).manager_storage_settings)
    [backup_page] = [row for row in repository.export_rows("knowledge_pages") if row["page_id"] == page.page_id]
    [link] = [row for row in repository.export_rows("knowledge_links") if row["page_id"] == page.page_id]
    destination_page = {**backup_page, "body": "This page has no links now." if remove_link else backup_page["body"]}
    monkeypatch.setattr(KnowledgeSection, "_existing_pages", lambda self, context: {page.page_id: destination_page})
    records = [{"table": "page", "row": backup_page}, {"table": "link", "row": link}]

    plan = KnowledgeSection().plan(records, SimpleNamespace(accepted=lambda section: set()))

    outcomes = {record.key: (record.outcome, record.reason) for record in plan}
    link_key = f"link/{page.page_id}/team/ci"
    if remove_link:
        assert outcomes[link_key] == (RecordOutcome.REFUSED, "its link is absent from the page's current text")
    else:
        assert outcomes[link_key][0] is RecordOutcome.IMPORT
