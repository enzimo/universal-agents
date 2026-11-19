"""Each section's records follow a new team id, and another team's records keep theirs (owner's decision 103).

``sections.read_section_records`` applies the rename each section registers in
``TEAM_ID_RENAMES`` right after the version step (``team_transfer.team_ids``). These tests run
the renames on the previous release's records (every real file for this feature is a 0.9
export), on records naming every place a section holds the team id, and through imports under
a new id. The team here is ``team_a``, which fits, so ``TeamIds`` is built directly: a new id is
offered only for an id that does not fit, which ``test_team_transfer_team_ids`` covers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from team_transfer.container import TransferReader
from team_transfer.errors import NewTeamIdRefused
from team_transfer.format import SectionDocumentV1
from team_transfer.registry import TRANSFER_SECTIONS
from team_transfer.sections import ImportContext, RecordOutcome, renamed_records, upgrade_records
from team_transfer.team_ids import TeamIds

OLD, NEW, OTHER = "team_a", "team_new", "team_other"
IDS = TeamIds(OLD, NEW)
FIXTURES = Path(__file__).parent / "fixtures" / "team_transfer" / "previous-sections.json"


def _naming(value: Any, team_id: str, path: str = "") -> list[str]:
    """Where ``team_id`` appears in any string of ``value``, keys included."""

    if isinstance(value, str):
        return [path] if team_id in value else []
    if isinstance(value, dict):
        found = []
        for key, item in value.items():
            found += [f"{path}/{key} (key)"] if team_id in str(key) else []
            found += _naming(item, team_id, f"{path}/{key}")
        return found
    if isinstance(value, list):
        return [hit for index, item in enumerate(value) for hit in _naming(item, team_id, f"{path}/{index}")]
    return []


# -- the previous release's records ---------------------------------------------------------


def _documents_valid_for_this_version(name: str, records: list[dict[str, Any]]) -> None:
    """Each record validates with this version's models, the documents inside it included."""

    from custom_messages.contracts.access_v2 import (
        AccountLinkV1,
        AccountSharingV1,
        DataFolderRecordV1,
        GrantV1,
    )
    from custom_messages.contracts.person_settings_v1 import PersonSettingsV1
    from team_transfer.knowledge import KnowledgeTable, _row_problem

    section = {section.name: section for section in TRANSFER_SECTIONS}[name]
    access_models = {
        "grant": GrantV1, "data_folder": DataFolderRecordV1, "account_link": AccountLinkV1,
        "person_settings": PersonSettingsV1, "account_sharing": AccountSharingV1,
    }
    for record in records:
        section.record_model.model_validate(record)
        if name == "access" and record["kind"] in access_models:
            access_models[record["kind"]].model_validate(record["document"])
        if name == "knowledge":
            assert _row_problem(KnowledgeTable(record["table"]), record["row"]) == ""


def test_the_previous_releases_records_follow_a_new_team_id() -> None:
    fixtures = json.loads(FIXTURES.read_text())
    sections = {section.name: section for section in TRANSFER_SECTIONS}
    covered = {"settings", "control", "operator_secrets", "access", "knowledge"}
    # Schedules register their rename with checkpoint 3; the broker renames its credentials itself (checkpoint 4).
    assert set(fixtures) - covered == {"schedules", "broker_credentials"}

    for name in sorted(covered):
        converted = upgrade_records(SectionDocumentV1.model_validate(fixtures[name]), sections[name].schema_version)
        renamed = renamed_records(name, converted, IDS)

        _documents_valid_for_this_version(name, renamed)
        assert _naming(renamed, OLD) == [], name
        assert renamed_records(name, converted, TeamIds.unchanged(OLD)) is converted
    access = renamed_records(
        "access",
        upgrade_records(SectionDocumentV1.model_validate(fixtures["access"]), sections["access"].schema_version),
        IDS,
    )
    assert [record["document"]["owner_id"] for record in access if record["kind"] == "account_link"] == [NEW]


# -- each section's rename, on every place it holds the team id ------------------------------


def _subject(kind: str, subject_id: str) -> dict[str, str]:
    return {"kind": kind, "id": subject_id}


def test_access_records_follow_the_new_id_in_every_field_and_subject_that_names_the_team() -> None:
    grant = {
        "kind": "grant", "record_id": "g1",
        "document": {
            "team_id": OLD,
            "subject": _subject("agent_type", f"{OLD}/TeamArchitect"),
            "granted_by": _subject("team", OLD),
            "revoked_by": _subject("role", f"{OLD}/admin"),
            "delegate_to": [
                _subject("schedule", f"{OLD}/sched_daily"), _subject("agent", "agent_lead"),
                _subject("user", "usr_ana"), _subject("agent_type", f"{OTHER}/Writer"),
            ],
        },
    }
    team_link = {"kind": "account_link", "record_id": "l1", "document": {
        "team_id": OLD, "owner_kind": "team", "owner_id": OLD, "created_by": _subject("user", "usr_ana"),
        "revoked_by": _subject("team", OLD),
    }}
    person_link = {"kind": "account_link", "record_id": "l2", "document": {
        "team_id": OLD, "owner_kind": "person", "owner_id": "usr_ana", "created_by": _subject("role", f"{OLD}/owner"),
    }}
    folder = {"kind": "data_folder", "record_id": "datasets", "document": {
        "team_id": OLD, "registered_by": _subject("team", OLD), "removed_by": _subject("role", f"{OLD}/admin"),
    }}
    others = [
        {"kind": kind, "record_id": "usr_ana", "document": {"team_id": OLD, "person_id": "usr_ana"}}
        for kind in ("person_settings", "account_sharing", "profile_choice")
    ]
    third_team = {"kind": "grant", "record_id": "g3", "document": {
        "team_id": OTHER, "subject": _subject("agent_type", f"{OTHER}/Writer"), "granted_by": _subject("team", OTHER),
    }}
    records = [grant, team_link, person_link, folder, *others, third_team]

    renamed = renamed_records("access", records, IDS)

    document = renamed[0]["document"]
    assert document["team_id"] == NEW
    assert document["subject"]["id"] == f"{NEW}/TeamArchitect"
    assert document["granted_by"]["id"] == NEW and document["revoked_by"]["id"] == f"{NEW}/admin"
    assert [subject["id"] for subject in document["delegate_to"]] == [
        f"{NEW}/sched_daily", "agent_lead", "usr_ana", f"{OTHER}/Writer"
    ]
    assert renamed[1]["document"] == {**team_link["document"], "team_id": NEW, "owner_id": NEW,
                                      "revoked_by": _subject("team", NEW)}
    assert renamed[2]["document"]["owner_id"] == "usr_ana", "a person's link names the person"
    assert renamed[2]["document"]["created_by"]["id"] == f"{NEW}/owner"
    assert renamed[3]["document"]["registered_by"]["id"] == NEW
    assert renamed[3]["document"]["removed_by"]["id"] == f"{NEW}/admin"
    assert [record["document"]["team_id"] for record in renamed[4:7]] == [NEW] * 3
    assert renamed[7] == third_team, "another team's record keeps its id"
    assert [record["record_id"] for record in renamed] == [record["record_id"] for record in records]
    assert records[0]["document"]["team_id"] == OLD, "the records read from the file are not changed"


def test_knowledge_rows_of_the_team_follow_the_new_id_and_another_teams_rows_keep_theirs() -> None:
    from team_transfer.knowledge import KnowledgeTable, record_key

    rows = [{"table": table.value, "row": {"team_id": OLD, "page_id": "p1"}} for table in KnowledgeTable]
    redirect = {"team_id": OLD, "space": "team", "owner_person_id": "", "from_slug": "old", "from_page_id": "p1",
                "to_page_id": "p2", "created_at_utc": "2026-10-11T00:00:00.000000Z"}
    third_team = {"table": "page", "row": {"team_id": OTHER, "page_id": "p9"}}

    renamed = renamed_records("knowledge", [*rows, {"table": "redirect", "row": redirect}, third_team], IDS)

    assert [record["row"]["team_id"] for record in renamed] == [NEW] * (len(rows) + 1) + [OTHER]
    assert record_key(KnowledgeTable.REDIRECT, renamed[-2]["row"]) == f"redirect/{NEW}/team//old"
    assert renamed[-1] == third_team


def test_a_new_id_another_teams_knowledge_already_has_is_refused() -> None:
    records = [{"table": "page", "row": {"team_id": OLD}}, {"table": "page", "row": {"team_id": NEW}}]

    with pytest.raises(NewTeamIdRefused, match=f"another team whose id is {NEW!r}, the new team id asked for"):
        renamed_records("knowledge", records, IDS)
    assert renamed_records("knowledge", records, TeamIds(OLD, "team_free"))[1]["row"]["team_id"] == NEW


def test_objectives_follow_the_new_id_and_another_teams_objective_is_still_refused(tmp_path: Path) -> None:
    from runtime_settings.instance_layout import InstanceLayout
    from team_transfer.objectives import ObjectivesSection
    from tests.test_team_transfer_objectives import _objective, _policy, _profile

    def scope(team_id: str) -> str:
        return f"manager_scope:{team_id}:TeamArchitect"

    other = _objective("obj_other", scope(OTHER), None).model_copy(update={"team_id": OTHER})
    records = [
        {"kind": "objective", "record_id": "obj_team", "document": _objective("obj_team", scope(OLD), None).model_dump(mode="json")},
        {"kind": "objective", "record_id": "obj_other", "document": other.model_dump(mode="json")},
        {"kind": "work_profile", "record_id": "prof", "document": _profile("prof", scope(OLD), "usr_ana").model_dump(mode="json")},
        {"kind": "work_policy", "record_id": "obj_team",
         "document": _policy("obj_team", scope(OLD), "usr_ana").model_dump(mode="json")},
    ]

    renamed = renamed_records("objectives", records, IDS)

    assert (renamed[0]["document"]["team_id"], renamed[0]["document"]["owner_agent_id"]) == (NEW, scope(NEW))
    assert renamed[1] == records[1], "another team's objective keeps its id"
    assert [record["document"]["owner_agent_id"] for record in renamed[2:]] == [scope(NEW)] * 2
    assert IDS.rename_after("manager_scope:", f"manager_scope:{OLD}x:Lead", ":") == f"manager_scope:{OLD}x:Lead"

    layout = InstanceLayout.at(tmp_path / "new")
    context = ImportContext(None, layout.root, layout, {}, team_ids=IDS)  # type: ignore[arg-type]
    context.baseline_keys["people"] = {"usr_ana"}
    outcomes = {record.key: record for record in ObjectivesSection().plan(renamed, context)}
    assert outcomes["objective/obj_team"].outcome is RecordOutcome.IMPORT
    assert outcomes["objective/obj_other"].outcome is RecordOutcome.REFUSED
    assert outcomes["objective/obj_other"].reason == f"it belongs to the team {OTHER}"


# -- imports under a new id ------------------------------------------------------------------


def _import(tmp_path: Path, config_path: Path, sections: tuple, *, team_ids: TeamIds = IDS) -> tuple[Any, Path]:
    from team_transfer.crypto import new_key_derivation
    from team_transfer.service import export_source, export_team, import_team

    out = tmp_path / "team.uatransfer"
    export_team(export_source(config_path), out, "correct horse battery staple", owner_person_id="usr_ana",
                sections=sections, key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    target = tmp_path / "new"
    with TransferReader.open(out, "correct horse battery staple") as reader:
        plan = import_team(reader.with_team_ids(team_ids), target, dry_run=False, sections=sections)
    return plan, target


def test_access_imported_under_a_new_id_belongs_to_the_new_team(tmp_path: Path) -> None:
    from access_control.entities import agent_type_id
    from tests.test_team_transfer_access import OWNER, SECTIONS, _old_instance, _target_store

    config_path, datasets = _old_instance(tmp_path)
    plan, target = _import(tmp_path, config_path, SECTIONS)

    imported = {r.key for r in plan.records if r.section == "access" and r.outcome is RecordOutcome.IMPORT}
    assert imported == {
        "data_folder/datasets", "grant/g_schedules", "grant/g_share", "grant/g_data",
        "account_link/link_github_ana", "person_settings/usr_ana", "profile_choice/usr_ben",
    }, "the same records as an import that keeps the id"
    store = _target_store(target)
    grants = {grant.grant_id: grant for grant in store.list_team(NEW)}
    assert set(grants) == {"g_schedules", "g_share", "g_data"} and store.list_team(OLD) == []
    assert grants["g_data"].subject.id == agent_type_id(NEW, "TeamArchitect")
    assert grants["g_data"].granted_by == OWNER
    assert [folder.path for folder in store.data_folders(NEW)] == [str(datasets)]
    assert [link.link_id for link in store.list_links(NEW)] == ["link_github_ana"]
    assert store.person_settings(NEW, "usr_ana").uses_team_keys is False
    assert store.profile_choice(NEW, "usr_ben") == ("standard", "person")


def _knowledge_shared_with(tmp_path: Path, other_team: str) -> Path:
    """The knowledge test's old instance, whose store also holds one team page of ``other_team``."""

    from dataclasses import replace

    from knowledge_base.service import DraftInput
    from tests.test_team_transfer_knowledge import _actor, _knowledge_service, _old_instance

    config_path = _old_instance(tmp_path)
    _knowledge_service(config_path).write(
        replace(_actor("sys"), team_id=other_team), "team/theirs", DraftInput(title="Theirs", body="Their own page.")
    )
    return config_path


def test_knowledge_imported_under_a_new_id_is_the_new_teams(tmp_path: Path) -> None:
    from dataclasses import replace

    from assistant_gateway.config import GatewayInstanceConfig
    from storage.knowledge_repository import KNOWLEDGE_TRANSFER_TABLES, create_knowledge_repository
    from tests.test_team_transfer_knowledge import SECTIONS, _actor, _knowledge_service

    plan, target = _import(tmp_path, _knowledge_shared_with(tmp_path, OTHER), SECTIONS)

    refused = [r.reason for r in plan.records if r.section == "knowledge" and r.outcome is RecordOutcome.REFUSED]
    assert refused and all("usr_stranger, who is not imported" in reason for reason in refused), "as without a new id"
    repository = create_knowledge_repository(
        GatewayInstanceConfig.load(target / "instance_config.yaml").manager_storage_settings
    )
    for table in KNOWLEDGE_TRANSFER_TABLES:
        assert {row["team_id"] for row in repository.export_rows(table)} <= {NEW, OTHER}, table
    pages = repository.export_rows("knowledge_pages")
    assert [row["slug"] for row in pages if row["team_id"] == OTHER] == ["theirs"], "another team's page keeps its id"
    assert OLD not in {row["team_id"] for row in pages}
    copied = _knowledge_service(target / "instance_config.yaml")
    merged = copied.read(replace(_actor("sys"), team_id=NEW), "team/deploying")
    assert (merged.page.reference(), merged.redirected_from) == ("team/deploy", "team/deploying")
    assert copied.read(replace(_actor("sys"), team_id=OTHER), "team/theirs").page.title == "Theirs"


def test_an_import_under_the_id_of_another_team_in_the_knowledge_is_refused_before_writing(tmp_path: Path) -> None:
    from tests.test_team_transfer_knowledge import SECTIONS

    with pytest.raises(NewTeamIdRefused) as refused:
        _import(tmp_path, _knowledge_shared_with(tmp_path, NEW), SECTIONS)

    assert "Choose another new team id" in str(refused.value)
    assert not (tmp_path / "new").exists(), "nothing is written"


def test_objectives_imported_under_a_new_id_are_the_new_team_leads(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig
    from storage.objective_repository import create_objective_repository
    from team_transfer.objectives import owner_scope
    from tests.test_team_transfer_objectives import SECTIONS, _old_instance

    plan, target = _import(tmp_path, _old_instance(tmp_path), SECTIONS)

    imported = {r.key for r in plan.records if r.section == "objectives" and r.outcome is RecordOutcome.IMPORT}
    assert imported == {"objective/obj_ana", "objective/obj_team", "work_profile/prof_ana", "work_policy/obj_ana"}
    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    scope = owner_scope(config)
    assert scope.startswith(f"manager_scope:{NEW}:")
    objectives = create_objective_repository(config.manager_storage_settings)
    arrived = objectives.list_team_objectives(team_id=NEW)
    assert {o.objective_id for o in arrived} == {"obj_ana", "obj_team"}
    assert {o.owner_agent_id for o in arrived} == {scope}
    assert objectives.list_team_objectives(team_id=OLD) == []


def test_a_specialist_imported_under_a_new_id_names_the_new_team_and_passes_the_runtimes_check(tmp_path: Path) -> None:
    from custom_agents.generated_agent_runtime import (
        GeneratedAgentProvisioningRecordV1,
        apply_generated_agent_provisioning,
    )
    from tests.test_team_transfer_control import SECTIONS, _old_instance, _specialist

    config_path, config = _old_instance(tmp_path)
    _specialist(config.layout.control_dir, "Foreign", team_id=OTHER)
    exported = (config.layout.control_dir / "agent_bundles" / "team_lead_generated" / "Approved" / "provisioning.json")
    before = GeneratedAgentProvisioningRecordV1.model_validate_json(exported.read_text(encoding="utf-8"))

    plan, target = _import(tmp_path, config_path, SECTIONS)

    outcomes = {r.key: r for r in plan.records if r.section == "control"}
    assert outcomes["agent_bundles/team_lead_generated/Approved"].outcome is RecordOutcome.IMPORT
    foreign = outcomes["agent_bundles/team_lead_generated/Foreign"]
    assert foreign.outcome is RecordOutcome.REFUSED, "another team's specialist is refused, as without a new id"
    assert f"belongs to team {OTHER}" in foreign.reason
    tampered = outcomes["agent_bundles/team_lead_generated/Tampered"]
    assert "changed after the specialist was approved" in tampered.reason
    bundle = target / "control" / "agent_bundles" / "team_lead_generated" / "Approved"
    record = GeneratedAgentProvisioningRecordV1.model_validate_json((bundle / "provisioning.json").read_text("utf-8"))
    assert record == before.model_copy(update={"team_id": NEW}), "only the team id changes; the plan digest stays"
    checked = apply_generated_agent_provisioning({
        "provisioning_record_path": str(bundle / "provisioning.json"),
        "named_agent_type": "Approved",
        "team_id": NEW,
        "agent_definition_source_path": str(bundle / "agent.yaml"),
        "default_tools": ["fn:schedules"],
    })
    assert checked["team_id"] == NEW
