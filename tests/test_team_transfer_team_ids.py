"""A first import may give a 0.9 team whose id does not fit protocol 1 a new team id (owner's decision 103).

``team_transfer.team_ids`` holds the mapping and its one rename rule; ``sections.read_section_records``
applies each section's registered rename; the service checks the id the import writes, records it
in the receipt and names both ids in the report. These tests cover the mapping, the suggestion and
its explanation, the checks on a requested id, where later batches take the id from, and an import
of settings, people and team memory under a new id.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from assistant_gateway.onboarding_engine import shortened_team_id, team_id_answer
from custom_messages.wire.ids import TEAM_ID
from runtime_settings.instance_layout import InstanceLayout
from storage.pinned_memory_repository import COREMEM_BANK, create_pinned_memory_repository
from team_transfer.container import TransferFileError, TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.errors import TransferError
from team_transfer.people import PeopleSection
from team_transfer.pinned_memory import PinnedMemorySection
from team_transfer.sections import TEAM_ID_RENAMES, RecordOutcome, renamed_records
from team_transfer.service import export_source, export_team, import_team, plan_import
from team_transfer.settings import SettingsSection
from team_transfer.team_ids import (
    TeamIds,
    new_team_ids,
    recorded_team_id,
    suggested_team_id,
    target_team_ids,
    team_id_explanation,
)
from tests.memory_support import SYSTEM_MEMORY_WRITER
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
OLD = "team_research_assistant_pro"  # 27 characters, as 0.9's onboarding built it
NEW = "team_research"
OTHER = "team_other"
IDS = TeamIds(OLD, NEW)
SECTIONS = (SettingsSection(), PeopleSection(), PinnedMemorySection())


# -- the one rename rule -----------------------------------------------------------------


def test_rename_changes_only_the_exports_id_and_its_forms_with_a_named_separator() -> None:
    assert IDS.rename(OLD) == NEW
    assert IDS.rename(f"{OLD}_TeamArchitect", "_") == f"{NEW}_TeamArchitect"
    assert IDS.rename(f"{OLD}/sched_daily", "/") == f"{NEW}/sched_daily"
    assert IDS.rename(f"{OLD}.inbox", ".", "_") == f"{NEW}.inbox"
    # Anything else comes out as it went in.
    assert IDS.rename(OTHER, "_") == OTHER, "a third team's id"
    assert IDS.rename(f"{OTHER}_TeamArchitect", "_") == f"{OTHER}_TeamArchitect"
    assert IDS.rename(f"{OLD}x", "_") == f"{OLD}x", "a longer id that merely starts with the export's id"
    assert IDS.rename(f"{OLD}/sched", "_") == f"{OLD}/sched", "a separator the caller did not name"
    assert IDS.rename(f"x_{OLD}", "_") == f"x_{OLD}", "the id inside a value, not at its start"
    assert IDS.rename(None) is None and IDS.rename(7) == 7
    assert TeamIds.unchanged(OLD).rename(OLD) == OLD


def test_rename_derived_follows_only_the_value_derived_from_the_exports_id() -> None:
    def derive(team_id: str) -> str:
        return f"mcp_host_{len(team_id)}"

    assert IDS.rename_derived(derive(OLD), derive) == derive(NEW)
    assert IDS.rename_derived(derive(OTHER), derive) == derive(OTHER)
    assert TeamIds.unchanged(OLD).rename_derived(derive(OLD), derive) == derive(OLD)


def test_renamed_fields_copies_the_record_and_leaves_absent_fields_absent() -> None:
    record = {"team_id": OLD, "owner": f"{OLD}/lead", "note": OLD}

    renamed = IDS.renamed_fields(record, ("team_id", "owner", "missing"), "/")

    assert renamed == {"team_id": NEW, "owner": f"{NEW}/lead", "note": OLD}
    assert record["team_id"] == OLD, "the record read from the file is not changed"
    assert IDS.describe() == f"team {OLD}, imported as {NEW}"
    assert TeamIds.unchanged(OLD).describe() == f"team {OLD}"


def test_a_section_without_a_registered_rename_is_refused_under_a_new_id() -> None:
    records = [{"team_id": OLD}]

    with pytest.raises(TransferError, match="cannot be imported under a new team id"):
        renamed_records("not_a_section", records, IDS)
    assert renamed_records("not_a_section", records, TeamIds.unchanged(OLD)) is records
    import team_transfer.registry  # noqa: F401 - sections register their renames as they load

    assert {"settings", "people", "pinned_memory", "skills", "team_tools", "operator_secrets"} <= set(TEAM_ID_RENAMES)


def test_settings_built_from_the_team_id_follow_it_and_a_third_teams_value_stays() -> None:
    rename = TEAM_ID_RENAMES["settings"]
    records = [
        {"path": ["transport", "subject_prefix"], "value": OLD},
        {"path": ["gateway", "response_queue_name"], "value": f"{OLD}_alpha_custom"},
        {"path": ["transport", "subject_prefix"], "value": OTHER},
        {"path": ["user", "initial_context"], "value": f"We are {OLD}."},
    ]

    renamed = rename(records, IDS)

    assert [record["value"] for record in renamed] == [NEW, f"{NEW}_alpha_custom", OTHER, f"We are {OLD}."]
    assert records[0]["value"] == OLD


# -- the suggestion and the explanation ----------------------------------------------------


@pytest.mark.parametrize(("exported", "suggested"), [
    (OLD, "team_research_assistant"),
    ("Team_Research_Assistant_Pro", "Team_Research_Assistant"),  # upper case kept
    (".research-team", "research-team"),  # leading punctuation dropped
    ("--__x", "x"),
    ("...", "team"),  # only punctuation
    ("", "team"),
    ("a" * 23 + ".b", "a" * 23),  # exactly 24 after replacement, ending in '_'
    ("équipe_de_recherche_numéro_un", "quipe_de_recherche_num_r"),  # non-ASCII letters
    ("team research", "team_research"),  # a space
    ("team.research", "team_research"),  # a dot
])
def test_the_suggestion_always_fits(exported: str, suggested: str) -> None:
    assert suggested_team_id(exported) == suggested
    assert TEAM_ID.fits(suggested_team_id(exported))


def test_onboardings_derived_ids_are_cut_as_before() -> None:
    def before(name: str) -> str:
        import re

        slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_") or "assistant"
        return f"team_{slug}"[: TEAM_ID.cap].rstrip("_")

    for name in ("Lisa", "My Research Assistant Pro Max", "a" * 30, "Zoë", "", "x  y"):
        assert team_id_answer("", name) == before(name)
    assert shortened_team_id("team_ok") == "team_ok"


def test_the_explanation_says_what_is_wrong_with_the_id() -> None:
    long = team_id_explanation(OLD)
    dotted = team_id_explanation("team.research")
    spaced = team_id_explanation("team research")

    assert long is not None and f"{OLD!r} is 27 characters long; protocol 1 allows at most 24" in long
    assert dotted is not None and "has characters protocol 1 does not allow" in dotted
    assert spaced is not None and "has characters protocol 1 does not allow" in spaced
    assert "characters long" not in dotted
    assert long.startswith("This version carries the team id in every message address")
    assert team_id_explanation(NEW) is None


# -- a requested new id, and where later steps take the id from ------------------------------


def test_a_requested_new_id_is_checked_like_a_typed_onboarding_id() -> None:
    assert new_team_ids(OLD, None) == TeamIds.unchanged(OLD)
    assert new_team_ids(OLD, f"  {NEW} ") == IDS
    with pytest.raises(TransferError, match="The new team id is empty"):
        new_team_ids(OLD, "  ")
    with pytest.raises(TransferError, match="is 25 characters long"):
        new_team_ids(OLD, "t" * 25)
    with pytest.raises(TransferError, match="has characters protocol 1 does not allow"):
        new_team_ids(OLD, "team research")
    with pytest.raises(TransferError, match=f"The export's team id {NEW!r} fits this version, so the team keeps it"):
        new_team_ids(NEW, OTHER)


def _receipt(layout: InstanceLayout, body: dict[str, Any] | str) -> None:
    layout.instance_config_path.parent.mkdir(parents=True, exist_ok=True)
    layout.instance_config_path.write_text("schema_version: 1\n", encoding="utf-8")
    layout.team_import_receipt_path.parent.mkdir(parents=True, exist_ok=True)
    layout.team_import_receipt_path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def test_later_steps_take_the_id_the_first_import_recorded(tmp_path: Path) -> None:
    fresh = InstanceLayout.at(tmp_path / "fresh")
    assert target_team_ids(OLD, fresh, NEW) == IDS
    assert target_team_ids(NEW, fresh, None) == TeamIds.unchanged(NEW)

    renamed = InstanceLayout.at(tmp_path / "renamed")
    _receipt(renamed, {"source_mac": "m", "team_id": NEW, "records": {}})
    assert recorded_team_id(renamed, OLD) == NEW
    assert target_team_ids(OLD, renamed, None) == IDS
    assert target_team_ids(OLD, renamed, NEW) == IDS
    with pytest.raises(TransferError) as refused:
        target_team_ids(OLD, renamed, OTHER)
    assert f"gave the team the id {NEW!r}" in str(refused.value) and f"{OTHER!r} was asked for" in str(refused.value)

    older = InstanceLayout.at(tmp_path / "older")
    _receipt(older, {"source_mac": "m", "records": {}})  # before receipts recorded the id: it was kept
    assert target_team_ids(NEW, older, None) == TeamIds.unchanged(NEW)

    broken = InstanceLayout.at(tmp_path / "broken")
    _receipt(broken, "{")
    with pytest.raises(TransferError, match="cannot be read"):
        recorded_team_id(broken, OLD)
    _receipt(broken, {"team_id": ""})
    with pytest.raises(TransferError, match="names no valid team id"):
        recorded_team_id(broken, OLD)


# -- an import under a new id ------------------------------------------------------------------


def _old_instance(tmp_path: Path) -> Path:
    from storage.gateway_state_repository import create_gateway_state_repository

    path, config = new_instance(tmp_path, "alpha", OLD)
    config.transport.subject_prefix = OLD
    config.gateway.response_queue_name = f"{OLD}_alpha_custom"
    config.user.initial_context = f"We are {OLD}."
    config.save(path)
    people = create_gateway_state_repository(config.state_storage_settings)
    for person, role in (("usr_ana", "owner"), ("usr_ben", "member")):
        people.create_gateway_user(canonical_user_id=person, display_name=person, timezone="UTC")
        people.create_team_membership(canonical_user_id=person, team_id=OLD, role=role)
    memory = create_pinned_memory_repository(config.manager_storage_settings)
    memory.create_coremem(writer=SYSTEM_MEMORY_WRITER, team_id=OLD, fact_text="The team ships on Fridays.", capacity=10)
    memory.create_usermem(writer=SYSTEM_MEMORY_WRITER, canonical_user_id="usr_ana", fact_text="Short answers.",
                          capacity=10)
    return path


def _export(tmp_path: Path) -> Path:
    out = tmp_path / "team.uatransfer"
    export_team(export_source(_old_instance(tmp_path)), out, PASSPHRASE, owner_person_id="usr_ana",
                sections=SECTIONS, key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    return out


def test_settings_people_and_team_memory_are_imported_under_the_new_id(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig
    from instance_upgrades.protocol1_ids import check_ids_before_upgrade, load_saved_for_check
    from storage.gateway_state_repository import create_gateway_state_repository

    export, target = _export(tmp_path), tmp_path / "new"
    with TransferReader.open(export, PASSPHRASE) as opened:
        reader = opened.with_team_ids(new_team_ids(OLD, NEW))
        plan = import_team(reader, target, dry_run=False, sections=SECTIONS)

    assert plan.team_ids == IDS
    outcomes = {(r.section, r.key): r for r in plan.records}
    assert outcomes[("settings", "transport.subject_prefix")].outcome is RecordOutcome.IMPORT
    assert outcomes[("settings", "gateway.response_queue_name")].outcome is RecordOutcome.IMPORT
    noted = outcomes[("settings", "user.initial_context")]
    assert noted.outcome is RecordOutcome.ATTENTION
    assert f"it names the export's team id {OLD!r}, and this instance's team id is {NEW!r}" in noted.reason

    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert config.gateway.team_id == NEW
    assert config.transport.subject_prefix == NEW
    assert config.gateway.response_queue_name == f"{NEW}_alpha_custom"
    assert config.user.initial_context == f"We are {OLD}.", "a value the operator wrote is kept as it is"

    people = create_gateway_state_repository(config.state_storage_settings)
    assert sorted(person.canonical_user_id for person in people.list_team_people(NEW)) == ["usr_ana", "usr_ben"]
    assert people.list_team_people(OLD) == []

    facts = create_pinned_memory_repository(config.manager_storage_settings).list_unexpired_facts()
    assert [fact.team_id for fact in facts if fact.bank == COREMEM_BANK] == [NEW]

    layout = InstanceLayout.at(target)
    receipt = json.loads(layout.team_import_receipt_path.read_text(encoding="utf-8"))
    assert receipt["team_id"] == NEW
    [report_path] = layout.team_import_dir.glob("report-*.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert (report["team_id"], report["source"]["team_id"]) == (NEW, OLD)

    # The launcher's id check passes: the instance starts on this version.
    checked = load_saved_for_check(target / "instance_config.yaml")
    assert check_ids_before_upgrade(checked, lambda: checked.manager_storage_settings) is None


def test_a_later_batch_reads_under_the_recorded_id_and_a_reader_under_the_old_one_is_refused(tmp_path: Path) -> None:
    export, target = _export(tmp_path), tmp_path / "new"
    with TransferReader.open(export, PASSPHRASE) as opened:
        import_team(opened.with_team_ids(IDS), target, dry_run=False, sections=SECTIONS)

        later = target_team_ids(OLD, InstanceLayout.at(target), None)
        plan, _context = plan_import(opened.with_team_ids(later), target, sections=SECTIONS, allow_existing=True)
        assert later == IDS and plan.records == []
        assert {record.reason for record in plan.skipped_records} == {"already imported"}

        with pytest.raises(TransferError) as refused:
            plan_import(opened, target, sections=SECTIONS, allow_existing=True)
    assert str(refused.value) == f"The target instance is team {NEW}, and this import is for team {OLD}: they are different teams."


def test_the_reader_takes_ids_only_for_its_own_team(tmp_path: Path) -> None:
    with TransferReader.open(_export(tmp_path), PASSPHRASE) as opened:
        assert opened.team_ids == TeamIds.unchanged(OLD)
        assert opened.with_team_ids(IDS).team_ids == IDS
        assert opened.team_ids == TeamIds.unchanged(OLD), "the open file itself keeps the export's ids"
        with pytest.raises(TransferFileError, match=f"this file is team {OLD}"):
            opened.with_team_ids(TeamIds(OTHER, NEW))
