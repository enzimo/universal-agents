"""Each section's records follow a new team id, and another team's records keep theirs (owner's decision 103).

``sections.read_section_records`` applies the rename each section registers in
``TEAM_ID_RENAMES`` right after the version step (``team_transfer.team_ids``). These tests run
the renames on the previous release's records (every real file for this feature is a 0.9
export), on records naming every place a section holds the team id, and through imports under
a new id. The team here is ``team_a``, which fits, so ``TeamIds`` is built directly: a new id is
offered only for an id that does not fit, which ``test_team_transfer_team_ids`` covers.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
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
    if name == "broker_credentials":  # its model is the decrypted document's (BrokerCredentialsDocumentV1)
        section.record_model.model_validate({"records": records})
        return
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
        if name == "schedules" and record["kind"] == "schedule":
            from team_transfer.schedule_markdown import parse_schedule

            parse_schedule(record["schedule"]["markdown"])


def test_the_previous_releases_records_follow_a_new_team_id() -> None:
    fixtures = json.loads(FIXTURES.read_text())
    sections = {section.name: section for section in TRANSFER_SECTIONS}
    covered = {"settings", "control", "operator_secrets", "access", "knowledge", "schedules", "broker_credentials"}
    assert set(fixtures) == covered

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
    from custom_agents.internal_improvement_runtime import internal_improvement_schedule_id

    maintenance = {"kind": "grant", "record_id": "g4", "document": {
        "team_id": OLD, "subject": _subject("schedule", f"{OLD}/{internal_improvement_schedule_id(OLD)}"),
        "delegate_to": [
            _subject("schedule", f"{OTHER}/{internal_improvement_schedule_id(OLD)}"),
            # Malformed: a schedule subject holding only the team id. The team follows; nothing is added.
            _subject("schedule", OLD),
        ],
    }}
    records = [grant, team_link, person_link, folder, *others, third_team, maintenance]

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
    # The team's maintenance schedule's id is built from the team id, so a grant to it follows that id too;
    # another team's subject that happens to name the same schedule id keeps both parts.
    assert renamed[8]["document"]["subject"]["id"] == f"{NEW}/{internal_improvement_schedule_id(NEW)}"
    assert renamed[8]["document"]["delegate_to"] == [maintenance["document"]["delegate_to"][0], _subject("schedule", NEW)]
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


def _maintenance_agent(team_id: str, *, team_lead: bool, interval_seconds: int = 86_400) -> Any:
    """The agent the runtime builds the team's maintenance schedule with (``internal_improvement_runtime``)."""

    import logging
    from types import SimpleNamespace

    from custom_agents.internal_improvement_runtime import INTERNAL_IMPROVEMENT_AGENT_TYPE, TEAM_ARCHITECT_AGENT_TYPE

    return SimpleNamespace(
        id="agent_lead" if team_lead else "agent_maintainer", name="lead", team_id=team_id, is_team_lead=team_lead,
        named_agent_type=TEAM_ARCHITECT_AGENT_TYPE if team_lead else INTERNAL_IMPROVEMENT_AGENT_TYPE,
        schedule_maintenance={"internal_improvement_review_interval_seconds": interval_seconds},
        bootstrap_config={"instance_name": "alpha", "canonical_user_id": "usr_ana", "timezone": "UTC"},
        default_request_timezone="UTC", _logger=logging.getLogger("test.maintenance"),
    )


def _maintenance_schedule(team_id: str, interval_seconds: int = 86_400) -> Any:
    """The team's maintenance schedule as the manager stores it."""

    from custom_agents.internal_improvement_runtime import (
        build_internal_improvement_schedule,
        prepare_internal_improvement_schedule_for_manager,
    )

    built = build_internal_improvement_schedule(_maintenance_agent(team_id, team_lead=False, interval_seconds=interval_seconds))
    prepared = prepare_internal_improvement_schedule_for_manager(_maintenance_agent(team_id, team_lead=True), built)
    assert prepared is not None
    return prepared


def _schedule_record(schedule: Any) -> dict[str, Any]:
    from team_transfer.schedule_markdown import render_schedule
    from team_transfer.schedule_records import CarriedScheduleV2, ScheduleRecordKind, ScheduleTransferRecordV2

    return ScheduleTransferRecordV2(
        kind=ScheduleRecordKind.SCHEDULE, record_id=schedule.schedule_id,
        schedule=CarriedScheduleV2(owner_vouched=True, markdown=render_schedule(schedule.model_dump(mode="json"))),
    ).model_dump(mode="json")


def _scheduled(raw: dict[str, Any]) -> Any:
    from custom_messages.contracts.schedule_v1 import ScheduledMessage
    from team_transfer.schedule_markdown import parse_schedule

    return ScheduledMessage.model_validate(parse_schedule(raw["schedule"]["markdown"]))


def test_schedules_follow_the_new_id_with_the_maintenance_schedule_and_another_teams_keep_theirs() -> None:
    from access_control.identity import ISSUER_GATEWAY, ContextSigner, generate_signing_key
    from custom_agents.internal_improvement_runtime import (
        internal_improvement_schedule_id,
        internal_improvement_schedule_name,
        internal_improvement_task_id,
    )
    from custom_agents.local_schedule_policy import local_schedule_service_id
    from custom_agents.schedule_execution_identity import execution_digest
    from custom_messages.contracts.routing_v1 import MessageHeader
    from tests.test_team_transfer_schedules import _schedule

    signer = ContextSigner.from_value(ISSUER_GATEWAY, generate_signing_key(ISSUER_GATEWAY)[0])
    route = f"{OLD}_alpha_user_gateway"
    daily = _schedule("sched_daily", "usr_ana", signer=signer, claim_team_id=OLD, payload_header=MessageHeader(
        from_agent_id=route, to_routing_path=f"{OLD}_TeamArchitect", response_routing_key=route))
    changed = _schedule("sched_changed", "usr_ana", claim_team_id=OLD)
    third = _schedule("sched_third", "usr_ana", claim_team_id=OTHER, payload_header=MessageHeader(
        from_agent_id=f"{OTHER}_alpha_user_gateway", to_routing_path=f"{OLD}x", response_routing_key="agent_x"))
    maintenance = _maintenance_schedule(OLD)
    old_id, new_id = internal_improvement_schedule_id(OLD), internal_improvement_schedule_id(NEW)

    def binding(schedule: Any, digest: str, actor: str) -> dict[str, Any]:
        return {"kind": "worker_binding", "record_id": schedule.schedule_id, "document": {
            "schedule_id": schedule.schedule_id, "team_id": OLD, "definition_digest": "d" * 64, "execution_digest": digest,
            "actor_principal_id": actor, "authority_mode": "runtime_default"}}

    third_record = _schedule_record(third)
    daily_target = {"kind": "scheduled_target", "record_id": "sched_daily",
                    "target": {"schedule_id": "sched_daily", "session_key": "sess_ana"}}
    other_pool = {"kind": "pool_limit", "record_id": "Writer", "document": {"team_id": OTHER, "scope": "Writer"}}
    persons_service = {"kind": "managed_service", "record_id": "svc1", "execution": {
        "record_id": "svc1", "name": "Site", "owner_kind": "user", "owner_id": "usr_ana"}}
    records = [
        # Bindings come before their schedules here: the rename reads every schedule first.
        # The first is set up by the runtime itself (local_schedule_policy.install_schedule_default).
        binding(daily, execution_digest(daily, "d" * 64), local_schedule_service_id(OLD)),
        binding(changed, "0" * 64, local_schedule_service_id(OTHER)),
        _schedule_record(daily), _schedule_record(changed), third_record, _schedule_record(maintenance),
        {"kind": "scheduled_target", "record_id": old_id,
         "target": {"schedule_id": old_id, "session_key": "sess_ana", "owner_person_id": "usr_ana"}},
        daily_target,
        {"kind": "pool_limit", "record_id": "*", "document": {"team_id": OLD, "scope": "*"}},
        other_pool,
        {"kind": "execution_job", "record_id": "job1", "execution": {
            "record_id": "job1", "name": "Export", "owner_kind": "team", "owner_id": OLD}},
        persons_service,
    ]
    exported = json.loads(json.dumps(records))

    renamed = {(record["kind"], record["record_id"]): record for record in renamed_records("schedules", records, IDS)}

    assert records == exported, "the records read from the file are not changed"
    new_daily = _scheduled(renamed[("schedule", "sched_daily")])
    assert new_daily.claim_team_id == NEW
    header = new_daily.payload_header
    assert (header.from_agent_id, header.to_routing_path, header.response_routing_key) == (
        f"{NEW}_alpha_user_gateway", f"{NEW}_TeamArchitect", f"{NEW}_alpha_user_gateway")
    context = new_daily.payload_meta.gateway_context
    assert context.task_context is None, "signed for the old team; the writer signs it again"
    assert (context.canonical_user_id, context.session_key) == ("usr_ana", "sess_ana"), "the rest of the context stays"
    assert renamed[("schedule", "sched_third")] == third_record, "another team's schedule keeps its values"

    new_maintenance = _scheduled(renamed[("schedule", new_id)])
    assert (new_maintenance.schedule_id, new_maintenance.schedule_name, new_maintenance.payload.task_id) == (
        new_id, internal_improvement_schedule_name(NEW), internal_improvement_task_id(NEW))
    assert new_maintenance.claim_team_id == NEW
    assert new_maintenance.payload_meta.gateway_context.scheduled_job_id == new_id
    assert new_maintenance.payload_header == maintenance.payload_header, "agent ids name no team"
    assert renamed[("scheduled_target", new_id)]["target"]["schedule_id"] == new_id
    assert renamed[("scheduled_target", "sched_daily")] == daily_target

    matched = renamed[("worker_binding", "sched_daily")]["document"]
    assert (matched["team_id"], matched["actor_principal_id"]) == (NEW, local_schedule_service_id(NEW))
    assert matched["execution_digest"] == execution_digest(new_daily, "d" * 64) != execution_digest(daily, "d" * 64)
    unmatched = renamed[("worker_binding", "sched_changed")]["document"]
    assert (unmatched["team_id"], unmatched["execution_digest"]) == (NEW, "0" * 64), "it still waits for review"
    assert unmatched["actor_principal_id"] == local_schedule_service_id(OTHER), "another team's runtime keeps its id"

    assert renamed[("pool_limit", "*")]["document"]["team_id"] == NEW
    assert renamed[("pool_limit", "Writer")] == other_pool
    assert renamed[("execution_job", "job1")]["execution"]["owner_id"] == NEW
    assert renamed[("managed_service", "svc1")] == persons_service

    ours = [record for key, record in renamed.items() if key not in {("schedule", "sched_third"), ("pool_limit", "Writer")}]
    assert _naming(ours, OLD) == [] and _naming(list(renamed.values()), old_id) == []


# A schedule this version cannot read, in the two ways it fails: its YAML names a setting the model does not
# have (parse_schedule refuses it, so the whole import stops), or the model refuses a value (the planner
# refuses the record). Both are made from a valid person's schedule's Markdown.
_UNREADABLE = {
    "unknown setting": ("schedule_name: {id}", "schedule_name: {id}\nnot_a_setting: 1"),
    "model refuses": ("management_class: user", "management_class: retired_class"),
}


def _unreadable(markdown: str, schedule_id: str, kind: str) -> str:
    before, after = (part.format(id=schedule_id) for part in _UNREADABLE[kind])
    assert before in markdown, kind
    return markdown.replace(before, after)


@pytest.mark.parametrize("kind", sorted(_UNREADABLE))
def test_a_schedule_the_rename_cannot_read_keeps_the_exported_id_and_is_logged(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.test_team_transfer_schedules import _schedule

    record = _schedule_record(_schedule("sched_bad", "usr_ana", claim_team_id=OLD))
    record["schedule"]["markdown"] = _unreadable(record["schedule"]["markdown"], "sched_bad", kind)
    binding = {"kind": "worker_binding", "record_id": "sched_bad", "document": {
        "schedule_id": "sched_bad", "team_id": OLD, "definition_digest": "d" * 64, "execution_digest": "0" * 64}}
    target = {"kind": "scheduled_target", "record_id": "sched_bad", "target": {"schedule_id": "sched_bad"}}
    pool = {"kind": "pool_limit", "record_id": "*", "document": {"team_id": OLD, "scope": "*"}}

    with caplog.at_level("WARNING", logger="team_transfer.schedule_team_ids"):
        renamed = renamed_records("schedules", [record, binding, target, pool], IDS)

    assert renamed[0] == record, "left as exported: the planner refuses it or the import stops"
    warnings = [entry.getMessage() for entry in caplog.records]
    assert len(warnings) == 1 and "event=team_transfer_schedule_rename_skipped record_id=sched_bad" in warnings[0]
    assert renamed[1]["document"]["team_id"] == NEW, "its binding still follows the new id"
    assert renamed[1]["document"]["execution_digest"] == "0" * 64, "no schedule to compute the digest with"
    assert renamed[2] == target and renamed[3]["document"]["team_id"] == NEW


def _hosted(team_id: str, server_name: str = "echo", lifecycle: str = "stopped") -> Any:
    from mcp_control.hosting import MCPHostedServerDefinitionV1

    return MCPHostedServerDefinitionV1.build(
        server_name=server_name, purpose="Return an attested copy of reviewed arguments.",
        owner_canonical_user_id="usr_ana", team_id=team_id, target_agent_types=("TeamArchitect",),
        tools=({"name": "echo", "description": "Echo.", "input_schema": {"type": "object"},
                "behavior": "echo_arguments"},),
        bind_host="127.0.0.1", port=41000, lifecycle=lifecycle,
        created_at_utc=datetime(2026, 9, 1, tzinfo=timezone.utc), installed_by_principal_id="usr_admin",
    )


def _connection(team_id: str, name: str, *, source: Any = None, locator: str | None = None) -> Any:
    from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeApprovalRecordV1
    from mcp_control.registry import (
        MCPConnectionLifecycle,
        MCPConnectionRecordV1,
        MCPConnectionSource,
        MCPOperationDefinitionV1,
        _new_record,
        descriptor_from_server_config,
    )

    descriptor = descriptor_from_server_config(
        {"name": name, "transport": "streamable_http", "server_url": f"https://{name}.example.test/mcp"},
        source=source or MCPConnectionSource.STATIC_GLOBAL, source_locator=locator or f"agent.external_mcp_servers.{name}",
        team_id=team_id, owner_canonical_user_id="usr_ana",
    )
    approval = ChangeApprovalRecordV1(mode=ChangeApprovalMode.PERSON_APPROVAL, approved_by="usr_admin",
                                      approved_at_utc=datetime(2026, 9, 2, tzinfo=timezone.utc))
    record = _new_record(descriptor, lifecycle=MCPConnectionLifecycle.ACTIVE, target_agent_types=("TeamArchitect",),
                         approval=approval)
    operations = (MCPOperationDefinitionV1.build(operation_name="search", input_schema={"type": "object"}),)
    values = record.model_dump(mode="python", exclude={"record_digest", "operation_catalog_sha256"})
    return MCPConnectionRecordV1.build(**{**values, "operations": operations})


def _mcp_record(kind: str, record_id: str, model: Any, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, "record_id": record_id, "document": model.model_dump(mode="json", exclude_none=True), **fields}


def test_mcp_servers_follow_the_new_id_with_their_digests_and_the_hosted_servers_id(caplog: pytest.LogCaptureFixture) -> None:
    from mcp_control.hosting import MCPHostedServerDefinitionV1, hosted_source_locator
    from mcp_control.registry import MCPConnectionRecordV1, MCPConnectionSource

    hosted, other_hosted = _hosted(OLD), _hosted(OTHER, "other_echo")
    served = _connection(OLD, "echo", source=MCPConnectionSource.HOSTED, locator=hosted_source_locator(hosted.hosted_server_id))
    static, other = _connection(OLD, "docs"), _connection(OTHER, "theirs")
    levels = {"connection_id": static.connection_id, "operations": {}}
    tampered = _mcp_record("connection", "mcp_conn_bad", _connection(OLD, "bad"))
    tampered["document"]["record_digest"] = "0" * 64
    records = [
        # The connection comes before its hosted server here: hosted servers are renamed first.
        _mcp_record("connection", served.connection_id, served),
        _mcp_record("connection", static.connection_id, static, levels=levels),
        _mcp_record("connection", other.connection_id, other),
        tampered,
        _mcp_record("hosted", hosted.hosted_server_id, hosted),
        _mcp_record("hosted", other_hosted.hosted_server_id, other_hosted),
        {"kind": "static_baseline", "record_id": "static-baseline", "document": {"connection_ids": [static.connection_id]}},
    ]
    exported = json.loads(json.dumps(records))

    with caplog.at_level("WARNING", logger="team_transfer.integrations"):
        renamed = renamed_records("mcp", records, IDS)

    assert records == exported, "the records read from the file are not changed"
    new_hosted = MCPHostedServerDefinitionV1.model_validate(renamed[4]["document"])
    assert new_hosted.team_id == NEW and new_hosted.hosted_server_id == _hosted(NEW).hosted_server_id
    assert renamed[4]["record_id"] == new_hosted.hosted_server_id != hosted.hosted_server_id
    assert (new_hosted.created_at_utc, new_hosted.installed_by_principal_id) == (
        hosted.created_at_utc, hosted.installed_by_principal_id), "only the team, its digest and id change"

    new_served = MCPConnectionRecordV1.model_validate(renamed[0]["document"])
    assert new_served.descriptor.team_id == NEW
    assert new_served.descriptor.source_locator == hosted_source_locator(new_hosted.hosted_server_id)
    assert (renamed[0]["record_id"], new_served.connection_id) == (served.connection_id, served.connection_id)
    assert new_served.descriptor.source_config_sha256 == served.descriptor.source_config_sha256
    assert (new_served.approval, new_served.operations) == (served.approval, served.operations)
    assert new_served.record_digest == new_served.calculate_digest() != served.record_digest

    new_static = MCPConnectionRecordV1.model_validate(renamed[1]["document"])
    assert (new_static.descriptor.team_id, new_static.descriptor.source_locator) == (NEW, static.descriptor.source_locator)
    assert renamed[1]["levels"] == levels
    assert renamed[2] == records[2] and renamed[5] == records[5], "another team's server keeps its values"
    assert renamed[6] == records[6], "the static baseline names no team"
    assert renamed[3] == tampered, "left as exported: the planner refuses it with the model's reason"
    assert "event=team_transfer_integration_rename_skipped section=mcp record_id=mcp_conn_bad" in caplog.text

    ours = [renamed[index] for index in (0, 1, 4, 6)]
    assert _naming(ours, OLD) == [] and _naming(ours, hosted.hosted_server_id) == []


def test_procedures_follow_the_new_id_and_keep_their_definition_id(tmp_path: Path) -> None:
    from custom_messages.contracts.installed_permissions_v1 import InstalledPermissionDefinitionV1
    from tests.test_installed_permissions import HANDLER_ID
    from tests.test_team_transfer_integrations import _registry

    handler = _registry(tmp_path, "registry").require(HANDLER_ID)
    descriptor = handler.descriptor

    def procedure(team_id: str, name: str) -> dict[str, Any]:
        definition_id = f"installed_permission.{HANDLER_ID}.{hashlib.sha256(name.encode()).hexdigest()[:16]}"
        return {"definition_id": definition_id, "document": InstalledPermissionDefinitionV1.build({
            "definition_id": definition_id, "proposal_id": f"installed_permission_proposal_{name}",
            "installed_at_utc": datetime(2026, 9, 1, tzinfo=timezone.utc), "installed_by_principal_id": "usr_admin",
            "team_id": team_id, "owner_canonical_user_id": "usr_ana", "handler_id": HANDLER_ID,
            "handler_descriptor_digest": handler.descriptor_digest, "input_schema_digest": handler.input_schema_digest,
            "display_name": descriptor.display_name, "description": descriptor.description,
            "impact_description": descriptor.impact_description, "risk": descriptor.risk,
            "manager_delegable": descriptor.manager_delegable,
        }).model_dump(mode="json")}

    ours, theirs, tampered = procedure(OLD, "ours"), procedure(OTHER, "theirs"), procedure(OLD, "bad")
    tampered["document"]["definition_digest"] = "0" * 64

    renamed = renamed_records("procedures", [ours, theirs, tampered], IDS)

    definition = InstalledPermissionDefinitionV1.model_validate(renamed[0]["document"])
    assert (definition.team_id, definition.definition_id) == (NEW, ours["definition_id"])
    assert definition.definition_digest == definition.calculate_digest() != ours["document"]["definition_digest"]
    assert renamed[1] == theirs and renamed[2] == tampered
    assert _naming(renamed[0], OLD) == []


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


def _names(text: str, needle: str) -> bool:
    """Whether ``text`` holds ``needle`` as a whole id: not inside a longer word (``team_a`` in ``team_alpha``).

    A separator after it still counts, so routes (``team_a_TeamArchitect``) and subjects (``team_a/x``) are found.
    """

    return re.search(rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])", text) is not None


def _files_naming(root: Path, needles: tuple[str, ...], *, leaving_out: Path) -> list[str]:
    """Each file under ``root`` (SQLite stores and ``leaving_out`` aside) that names one of ``needles``."""

    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_relative_to(leaving_out) or path.read_bytes()[:16] == b"SQLite format 3\x00":
            continue
        text = path.read_bytes().decode("utf-8", "replace")
        found += [f"{path.relative_to(root)}: {needle}" for needle in needles if _names(text, needle)]
    return found


def test_mcp_servers_and_procedures_imported_under_a_new_id_are_the_new_teams(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents.installed_permissions import InstalledPermissionService, resolve_installed_permission_paths
    from custom_agents.providers.mcp_connections import MCPConnectionAdministration
    from custom_agents.providers.mcp_hosting import MCPHostingAdministration
    from mcp_control.hosting import MCPHostedServerStore, hosted_source_locator, resolve_mcp_hosting_paths
    from mcp_control.registry import (
        MCPConnectionLifecycle,
        MCPConnectionStore,
        reconcile_instance_mcp_connections,
        resolve_mcp_connection_paths,
    )
    from runtime_settings.instance_layout import InstanceLayout
    from team_transfer.integrations import McpSection, ProceduresSection
    from team_transfer.settings import SettingsSection
    from tests.pinned_memory_support import new_instance
    from tests.test_team_transfer_integrations import DOCS, _install_procedure, _registry

    # The old instance: a configured server approved at its first start, a hosted server a person installed
    # (which activated its connection) and an installed procedure.
    path, config = new_instance(tmp_path / "old", "alpha", OLD)
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.agent.external_mcp_servers = [DOCS]
    config.mcp.hosting.enabled = True
    config.save(path)
    [docs] = reconcile_instance_mcp_connections(config)
    hosted = MCPHostedServerStore(resolve_mcp_hosting_paths(config)).save(_hosted(OLD, lifecycle="pending"))
    installed = MCPHostingAdministration(config).install(
        hosted.hosted_server_id, approval=MCPConnectionAdministration.person_approval("usr_admin"))
    registry = _registry(tmp_path, "registry")
    definition_id = _install_procedure(config.instance_root_path, registry, OLD)
    new_hosted_id = _hosted(NEW).hosted_server_id

    plan, target = _import(tmp_path, path, (SettingsSection(), McpSection(), ProceduresSection(registry)))

    assert {(r.section, r.key): r.outcome for r in plan.records if r.section in {"mcp", "procedures"}} == {
        ("mcp", f"connection/{docs.connection_id}"): RecordOutcome.IMPORT,
        ("mcp", f"connection/{installed['connection_id']}"): RecordOutcome.IMPORT,
        ("mcp", f"hosted/{new_hosted_id}"): RecordOutcome.IMPORT,
        ("mcp", "static_baseline/static-baseline"): RecordOutcome.IMPORT,
        ("procedures", definition_id): RecordOutcome.IMPORT,
    }
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    connections = MCPConnectionStore(resolve_mcp_connection_paths(target))
    before = {record.connection_id: record.record_digest for record in connections.list_records()}
    assert set(before) == {docs.connection_id, installed["connection_id"]}, "connection ids are kept"
    assert {record.descriptor.team_id for record in connections.list_records()} == {NEW}
    assert connections.require(installed["connection_id"]).descriptor.source_locator == hosted_source_locator(new_hosted_id)
    [definition] = MCPHostedServerStore(resolve_mcp_hosting_paths(imported)).list_definitions()
    assert (definition.hosted_server_id, definition.team_id) == (new_hosted_id, NEW)

    # The new instance's startup reconciliation recognises the carried records and adds none.
    reconcile_instance_mcp_connections(imported)
    assert {record.connection_id: record.record_digest for record in connections.list_records()} == before

    # The procedure is the new team's, and its handler still matches.
    procedures = InstalledPermissionService(paths=resolve_installed_permission_paths(target), registry=registry)
    [listed] = procedures.list_installed(team_id=NEW)
    assert (listed["definition_id"], listed["handler_registration_valid"]) == (definition_id, True)
    assert procedures.list_installed(team_id=OLD) == []

    assert _files_naming(target, (OLD, hosted.hosted_server_id), leaving_out=InstanceLayout.at(target).team_import_dir) == []

    # Removing the hosted server by its new id finds its imported connection: its tool leaves the catalog.
    MCPHostingAdministration(imported).remove(new_hosted_id, by="usr_admin")
    assert connections.require(installed["connection_id"]).lifecycle is MCPConnectionLifecycle.DISABLED


def _stored_naming(root: Path, needles: tuple[str, ...]) -> list[str]:
    """Each stored value in the SQLite files under ``root`` that names one of ``needles``."""

    import sqlite3
    from contextlib import closing

    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.read_bytes()[:16] != b"SQLite format 3\x00":
            continue
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
            tables = [name for (name,) in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            for table in tables:
                for row in database.execute(f'SELECT * FROM "{table}"'):
                    for value in row:
                        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
                        found += [f"{path.name} {table}: {needle}" for needle in needles if _names(text, needle)]
    return found


def test_schedules_imported_under_a_new_id_are_the_new_teams_and_its_runtime_finds_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from access_control.identity import ISSUER_GATEWAY, ContextSigner, public_entry, verify_keys_value
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents import task_context_runtime
    from custom_agents.internal_improvement_runtime import (
        internal_improvement_schedule_id,
        internal_improvement_schedule_name,
    )
    from custom_agents.local_schedule_policy import local_schedule_service_id
    from custom_agents.schedule_context_refresh import refresh_schedule_gateway_context
    from custom_agents.schedule_execution_identity import execution_digest
    from custom_agents.schedule_store import ScheduleStore
    from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
    from custom_messages.contracts.schedule_v1 import ScheduledMessage
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
    from internal_credentials import parse_env_file
    from storage.gateway_state_repository import create_gateway_state_repository
    from storage.worker_pool_repository import WorkerPoolRepository
    from team_transfer.crypto import new_key_derivation
    from team_transfer.service import export_source, export_team, import_team
    from tests.test_team_transfer_schedules import PASSPHRASE, SECTIONS, WORKER, _old_instance

    old_id, new_id = internal_improvement_schedule_id(OLD), internal_improvement_schedule_id(NEW)
    carried_interval = 7 * 3600

    # The old instance, with the team's maintenance schedule as its manager stored it and its gateway signed it.
    config_path, old_store = _old_instance(tmp_path / "old")
    old_config = GatewayInstanceConfig.load(config_path)
    old_key = parse_env_file(old_config.layout.team_env_path)["ACCESS_GATEWAY_SIGNING_KEY"]
    maintenance = _maintenance_schedule(OLD, interval_seconds=carried_interval)
    maintenance.payload_meta.gateway_context.session_key = "sess_ana"
    maintenance.payload_meta.gateway_context.task_context = ContextSigner.from_value(ISSUER_GATEWAY, old_key).issue(
        team_id=OLD, person_id="usr_ana", person_roles=("owner",), schedule_id=old_id
    )
    old_store.add(maintenance)
    create_gateway_state_repository(old_config.state_storage_settings).upsert_scheduled_target(
        scheduled_job_id=old_id, session_key="sess_ana", conversation_id="conv_maintenance",
        owner_canonical_user_id="usr_ana",
    )

    out = tmp_path / "team.uatransfer"
    export_team(export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana", sections=SECTIONS,
                key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    with TransferReader.open(out, PASSPHRASE) as reader:
        kept = import_team(reader, tmp_path / "kept", dry_run=False, sections=SECTIONS)
        plan = import_team(reader.with_team_ids(IDS), tmp_path / "new", dry_run=False, sections=SECTIONS)

    def outcomes(imported: Any, maintenance_id: str) -> dict[str, tuple[RecordOutcome, str]]:
        return {
            r.key.replace(maintenance_id, "<maintenance>"): (r.outcome, r.reason.replace(maintenance_id, "<maintenance>"))
            for r in imported.records if r.section == "schedules"
        }

    assert outcomes(kept, old_id)["schedule/<maintenance>"] == (RecordOutcome.IMPORT, "")
    assert outcomes(plan, new_id) == outcomes(kept, old_id), "the same outcome as an import that keeps the id"

    config = GatewayInstanceConfig.load(tmp_path / "new" / "instance_config.yaml")
    store = ScheduleStore(storage_settings=config.manager_storage_settings, default_cron_timezone="UTC")
    new_key = parse_env_file(config.layout.team_env_path)["ACCESS_GATEWAY_SIGNING_KEY"]
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", verify_keys_value([public_entry(ISSUER_GATEWAY, new_key)]))
    task_context_runtime._verifier.cache_clear()
    try:
        # A person's schedule: the new team's, signed again by the new gateway for the new team.
        daily = store.get_scheduled_message("sched_daily")
        assert daily.claim_team_id == NEW
        assert task_context_runtime.gateway_vouches(daily.payload_meta.gateway_context, "usr_ana")
        signed = daily.payload_meta.gateway_context.task_context.context
        assert (signed.team_id, signed.person_roles) == (NEW, ("admin", "owner")), "roles from the new team's people"

        # Its worker setup and the pool sizes are the new team's; the binding still matches its schedule.
        binding = store.worker_records.get_binding("sched_worker", team_id=NEW)
        assert binding is not None and store.worker_records.get_binding("sched_worker", team_id=OLD) is None
        assert binding.actor_principal_id == local_schedule_service_id(NEW), "set up by the new team's runtime"
        assert execution_digest(store.get_scheduled_message("sched_worker"), binding.definition_digest) == binding.execution_digest
        pools = WorkerPoolRepository(config.manager_storage_settings)
        assert {limit.scope for limit in pools.list_limits(NEW)} == {"*", WORKER} and not pools.list_limits(OLD)

        # The maintenance schedule arrives under the id the new team's runtime builds, with its carried cadence.
        assert store.get_scheduled_message(old_id) is None
        imported = store.get_scheduled_message(new_id)
        assert (imported.schedule_name, imported.claim_team_id, imported.interval_ms) == (
            internal_improvement_schedule_name(NEW), NEW, carried_interval * 1000)
        assert imported.payload_meta.gateway_context.task_context is None, "the gateway signs it at its first start"
        gateway = create_gateway_state_repository(config.state_storage_settings)
        assert gateway.get_scheduled_target(new_id).conversation_id == "conv_maintenance"
        assert gateway.get_scheduled_target(old_id) is None

        # The manager's ensure-only install at its first start finds it and adds no second one.
        assert store.add_if_absent(_maintenance_schedule(NEW)) is False
        maintenance_schedules = [
            (found.schedule_id, found.interval_ms)
            for found in (store.get_scheduled_message(row["schedule_id"]) for row in store.all_rows())
            if found is not None and found.effective_system_schedule_kind is SystemScheduleKind.INTERNAL_IMPROVEMENT
        ]
        assert maintenance_schedules == [(new_id, carried_interval * 1000)]

        # The gateway's refresh at its first start (ids from the new team, as assistant_gateway.gateway builds
        # them) applies to the imported schedule and leaves it signed for the new team.
        incoming = ScheduledMessage(
            schedule_id=new_id, schedule_name=internal_improvement_schedule_name(NEW),
            management_class=ScheduleManagementClass.SYSTEM, system_schedule_kind=SystemScheduleKind.INTERNAL_IMPROVEMENT,
            install_mode="refresh", payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Refresh."),
            payload_meta=MessageMeta(gateway_context=GatewayContext(
                canonical_user_id="usr_ana", session_key="sess_ana",
                task_context=ContextSigner.from_value(ISSUER_GATEWAY, new_key).issue(
                    team_id=NEW, person_id="usr_ana", person_roles=("owner",), schedule_id=new_id),
            )),
            interval_ms=86_400_000, repeat_count=None,
        )
        assert refresh_schedule_gateway_context(SimpleNamespace(_schedule_store=store, team_id=NEW, id="agent_lead"), incoming)
        refreshed = store.get_scheduled_message(new_id)
        assert task_context_runtime.gateway_vouches(refreshed.payload_meta.gateway_context, "usr_ana")
        assert refreshed.payload_meta.gateway_context.task_context.context.team_id == NEW
        assert refreshed.interval_ms == carried_interval * 1000, "the refresh keeps the carried cadence"
    finally:
        task_context_runtime._verifier.cache_clear()

    assert _stored_naming(tmp_path / "new", (OLD, old_id)) == []
    assert _stored_naming(tmp_path / "kept", (OLD, old_id)), "the search finds the ids where they are kept"


@pytest.mark.parametrize("kind", sorted(_UNREADABLE))
def test_a_schedule_this_version_cannot_read_gets_the_same_outcome_under_a_new_id(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename leaves it under the export's id, so only the planner's refusal keeps the old id out."""

    import team_transfer.schedule_markdown as schedule_markdown
    from team_transfer.crypto import new_key_derivation
    from team_transfer.errors import TransferError
    from team_transfer.service import export_source, export_team, import_team
    from tests.test_team_transfer_schedules import PASSPHRASE, SECTIONS, _old_instance

    config_path, _store = _old_instance(tmp_path / "old")
    render = schedule_markdown.render_schedule

    def exported_unreadable(document: dict[str, Any]) -> str:
        markdown = render(document)
        return _unreadable(markdown, "sched_worker", kind) if document["schedule_id"] == "sched_worker" else markdown

    out = tmp_path / "team.uatransfer"
    # Only the export writes the broken schedule; the import runs this version's code unchanged.
    with monkeypatch.context() as patched:
        patched.setattr(schedule_markdown, "render_schedule", exported_unreadable)
        export_team(export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana", sections=SECTIONS,
                    key_derivation=new_key_derivation(opslimit=1, memlimit=8192))

    def imported(team_ids: TeamIds, target: Path) -> Any:
        with TransferReader.open(out, PASSPHRASE) as reader:
            try:
                plan = import_team(reader.with_team_ids(team_ids), target, dry_run=False, sections=SECTIONS)
            except TransferError as exc:
                return str(exc)
        return {r.key: (r.outcome, r.reason) for r in plan.records if r.section == "schedules"}

    kept, renamed = imported(TeamIds.unchanged(OLD), tmp_path / "kept"), imported(IDS, tmp_path / "new")

    assert renamed == kept
    if kind == "unknown setting":
        assert "Unknown schedule settings: not_a_setting" in renamed
        assert not (tmp_path / "new").exists(), "the import stops before writing anything"
        return
    assert renamed["schedule/sched_worker"][0] is RecordOutcome.REFUSED
    assert renamed["schedule/sched_worker"][1].startswith("this version refuses it")
    assert renamed["worker_binding/sched_worker"] == (RecordOutcome.REFUSED, "its schedule sched_worker is not imported")
    assert _stored_naming(tmp_path / "new", (OLD,)) == []


async def _serving_broker(team_id: str, root_token: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real broker app of team ``team_id`` on a loopback port; the transfer code reaches it over HTTP."""

    from aiohttp.test_utils import TestServer

    from secure_tool_broker.service import SecureToolBrokerService
    from tests.broker_access_support import open_gate

    server = TestServer(SecureToolBrokerService(token=root_token, access_gate=open_gate(team_id=team_id)).build_app(),
                        host="127.0.0.1")
    await server.start_server()
    monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", str(server.make_url("")).rstrip("/"))
    return server


@pytest.mark.asyncio
async def test_credentials_stored_after_an_import_under_a_new_id_are_the_new_teams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio
    import logging

    from assistant_gateway.config import GatewayInstanceConfig
    from internal_credentials import parse_env_file
    from model_providers.endpoints import EndpointKind
    from runtime_settings.instance_layout import InstanceLayout
    from secure_tool_broker.credential_state import credential_owner_context
    from secure_tool_broker.credential_state_store import read_credential_state, write_credential_state
    from secure_tool_broker.model_keys import TEAM_SCOPE, ModelKeyStore, StoredModelKey
    from storage.access_repository import AccessRepository
    from storage.gateway_state_repository import create_gateway_state_repository
    from team_transfer.broker_credentials import BrokerCredentialsSection
    from team_transfer.credential_handover import import_handed_over_credentials, write_credential_handover
    from team_transfer.crypto import new_key_derivation
    from team_transfer.people import PeopleSection
    from team_transfer.review import archive_records
    from team_transfer.service import export_source, export_team, import_team
    from team_transfer.settings import SettingsSection
    from tests.pinned_memory_support import new_instance
    from tests.test_team_transfer_broker_credentials import MODEL_KEY, X_APP, _use_store

    sections = (SettingsSection(), PeopleSection(), BrokerCredentialsSection())
    passphrase = "correct horse battery staple"
    # The old instance's broker holds the team's app settings and model key and two of Ana's sign-ins.
    path, config = new_instance(tmp_path / "old", "alpha", OLD)
    (path.parent / "team.env").write_text("SECURE_TOOL_BROKER_TOKEN=old-broker-root\n")
    people = create_gateway_state_repository(config.state_storage_settings)
    people.create_gateway_user(canonical_user_id="usr_ana", display_name="Ana", timezone="UTC")
    people.create_team_membership(canonical_user_id="usr_ana", team_id=OLD, role="owner")
    _use_store(tmp_path / "old_store", monkeypatch)
    with credential_owner_context(OLD, scope_kind="team"):
        assert write_credential_state("x.oauth_client_json", X_APP).status == "success"
    ModelKeyStore(OLD, scope=TEAM_SCOPE).save(StoredModelKey(
        address="https://api.openai.com/v1", kind=EndpointKind.OPENAI, key=MODEL_KEY,
        saved_at=datetime(2026, 9, 1, tzinfo=timezone.utc), saved_by_person_id="usr_ana"))
    assert write_credential_state("github.token", "old-github-sign-in", owner_id="usr_ana").status == "success"
    assert write_credential_state("x.sign_in_json", '{"token": "ana-x"}', owner_id="usr_ana").status == "success"
    out = tmp_path / "team.uatransfer"
    broker = await _serving_broker(OLD, "old-broker-root", monkeypatch)
    try:
        await asyncio.to_thread(export_team, export_source(path), out, passphrase, owner_person_id="usr_ana",
                                sections=sections, key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    finally:
        await broker.close()

    # The first import gives the team its new id; the credentials wait for the new instance's broker.
    caplog.set_level(logging.INFO, logger="team_transfer.credential_handover")
    target = tmp_path / "new"
    with TransferReader.open(out, passphrase) as reader:
        renamed_reader = reader.with_team_ids(IDS)
        import_team(renamed_reader, target, dry_run=False, sections=sections)
        index = archive_records(renamed_reader)["broker_credentials"]
        kept_index = archive_records(reader)["broker_credentials"]
        write_credential_handover(renamed_reader, InstanceLayout.at(target), sorted(index))
    # The review shows the team's credentials as the new team's, under the same keys.
    assert set(index) == set(kept_index)
    assert {entry["owner"]["owner_id"] for entry in index.values() if entry["owner"]["kind"] == "team"} == {NEW}
    assert {entry["owner"]["owner_id"] for entry in kept_index.values() if entry["owner"]["kind"] == "team"} == {OLD}

    # The first start: the new broker (team NEW) already holds a sign-in Ana made since, which is kept.
    _use_store(tmp_path / "new_store", monkeypatch)
    assert write_credential_state("github.token", "new-github-sign-in", owner_id="usr_ana").status == "success"
    broker = await _serving_broker(NEW, parse_env_file(target / "team.env")["SECURE_TOOL_BROKER_TOKEN"], monkeypatch)
    try:
        report = await asyncio.to_thread(
            import_handed_over_credentials, str(target / "instance_config.yaml"), retry_delays=(0.0,))
    finally:
        await broker.close()

    assert report is not None
    # The instance's log names its own team, and where the credentials came from.
    logged = {
        event: message
        for message in (entry.getMessage() for entry in caplog.records)
        for event in ("team_import_credentials_handed_over", "team_import_credentials_refused",
                      "team_import_credentials_imported", "credentials_transfer_imported")
        if f"event={event} " in message
    }
    assert len(logged) == 4, logged
    for event in ("team_import_credentials_handed_over", "team_import_credentials_refused",
                  "team_import_credentials_imported"):
        assert f"team_id={NEW} exported_team_id={OLD} " in logged[event], logged[event]
    assert "refused=person:usr_ana/stored_value/github.sign_in" in logged["team_import_credentials_refused"]
    assert f"team_id={NEW} source_team_id={OLD} " in logged["credentials_transfer_imported"]
    outcomes = {outcome["key"]: outcome for outcome in report.outcomes}
    assert {key for key, outcome in outcomes.items() if not outcome["imported"]} == {
        "person:usr_ana/stored_value/github.sign_in"}
    assert {outcome["owner"]["owner_id"] for outcome in outcomes.values() if outcome["owner"]["kind"] == "team"} == {NEW}
    with credential_owner_context(NEW, scope_kind="team"):
        assert read_credential_state("x.oauth_client_json") == X_APP
    assert [entry.key for entry in ModelKeyStore(NEW, scope=TEAM_SCOPE).entries()] == [MODEL_KEY]
    assert read_credential_state("github.token", owner_id="usr_ana") == "new-github-sign-in"

    # Ana's notices (what moved, and, as the exporting owner, what did not) are the new team's.
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    notices = AccessRepository(imported.manager_storage_settings)
    assert {notice.title for notice, _read in notices.notices_for(NEW, person_id="usr_ana", roles=())} == {
        "Your sign-ins and keys moved to this instance", "Some sign-ins and keys did not move"}
    assert notices.notices_for(OLD, person_id="usr_ana", roles=()) == []
    [credentials_report] = InstanceLayout.at(target).team_import_dir.glob("credentials-*.json")
    written = json.loads(credentials_report.read_text())
    assert (written["team_id"], written["source"]["team_id"]) == (NEW, OLD), "stored for the new team, from the export's"

    assert _stored_naming(target, (OLD,)) == [] and _stored_naming(tmp_path / "new_store", (OLD,)) == []
    import_files = InstanceLayout.at(target).team_import_dir
    assert _files_naming(target, (OLD,), leaving_out=import_files) == []
    assert _files_naming(tmp_path / "new_store", (OLD,), leaving_out=import_files) == []
