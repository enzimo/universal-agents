"""A 0.9 team whose id does not fit moves to this version through the command line under a new id (decision 103).

The new id is chosen once, for the first import into a fresh root: ``team review --new-team-id``
(or ``team import --dry-run --new-team-id``). After that the tool already has it: the review
folder records it for ``team import --review``, and the first import's receipt for later batches
and ``team import-credentials`` (``team_transfer.team_ids``, ``team_transfer.review``). These
tests run the real command line on a team with a 27-character id and every section, then search
every file and stored value of the new instance for the old id and the ids the old instance
derived from it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.errors import NewTeamIdRefused, TransferError
from team_transfer.review import ReviewFiles, prepare_review, text_digest
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, plan_import
from team_transfer.team_ids import TeamIds
from tests.test_team_transfer_team_id_renames import _names, _stored_naming

PASSPHRASE = "correct horse battery staple"
OLD = "team_research_assistant_pro"  # 27 characters, as 0.9's onboarding built it
NEW = "team_research_assistant"  # the suggestion: the new id is a prefix of the old one
OTHER = "team_other"
IDS = TeamIds(OLD, NEW)
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _run_cli(*argv: str) -> None:
    import assistant_cli
    from team_transfer.cli import run_team_command

    run_team_command(assistant_cli.build_parser().parse_args(["team", *argv]))


def _choose(files: ReviewFiles, selected) -> None:
    """Select the records ``selected(record)`` names and confirm every selected schedule as it stands."""

    files.save(files.review.model_copy(update={"records": [
        record.model_copy(update={
            "selected": selected(record),
            "confirmed_digest": text_digest(files.text(record))
            if record.section == "schedules" and record.file else None,
        })
        for record in files.review.records
    ]}))


def _select_importable(files: ReviewFiles, left_out: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Select every record but ``left_out`` and those this version refuses (the review page's job)."""

    excluded = set(left_out)
    for _round in range(5):
        _choose(files, lambda record: (record.section, record.key) not in excluded)
        choices, overrides = files.choices(require_confirmation=True)
        plan, _context = plan_import(files.reader, files.review.target_root, choices=choices, overrides=overrides,
                                     allow_existing=True)
        refused = {(record.section, record.key) for record in plan.by_outcome(RecordOutcome.REFUSED)}
        if not refused:
            return excluded - left_out
        excluded |= refused
    raise AssertionError(f"the selection does not settle: {sorted(excluded)}")


# -- the review folder records the id ---------------------------------------------------------


def test_the_review_folder_records_the_new_id_and_refuses_another(tmp_path: Path) -> None:
    from tests.test_team_transfer_team_ids import _export

    export, target, directory = _export(tmp_path), tmp_path / "new", tmp_path / "review"
    with TransferReader.open(export, PASSPHRASE) as opened:
        # Without a new id, a team whose id does not fit gets no review: nothing is written.
        with pytest.raises(TransferError, match="--new-team-id"):
            prepare_review(opened, target, directory)
        assert not directory.exists() and not target.exists()
        with pytest.raises(NewTeamIdRefused):
            prepare_review(opened, target, directory, new_team_id="team research")
        assert not directory.exists()

        files = prepare_review(opened, target, directory, new_team_id=NEW)
        assert (files.review.team_id, files.reader.team_ids) == (NEW, IDS)
        assert prepare_review(opened, target, directory).reader.team_ids == IDS, "reopened without the option"
        assert ReviewFiles.open(opened, directory, team_id=NEW).reader.team_ids == IDS
        for asked in (OTHER, OLD):
            with pytest.raises(TransferError) as refused:
                ReviewFiles.open(opened, directory, team_id=asked)
            assert f"prepared to import the team as {NEW!r}, and {asked!r} was asked for" in str(refused.value)
            with pytest.raises(TransferError, match="prepared to import the team as"):
                prepare_review(opened, target, directory, new_team_id=asked)

        # A review saved before the field was prepared under the export's own id; an edited id must pass the rule.
        saved = json.loads((directory / "review.json").read_text())
        for value, expected in ((None, TeamIds.unchanged(OLD)), (OTHER, TeamIds(OLD, OTHER))):
            (directory / "review.json").write_text(json.dumps({**saved, "team_id": value}))
            assert ReviewFiles.open(opened, directory).reader.team_ids == expected
        (directory / "review.json").write_text(json.dumps({**saved, "team_id": "team research"}))
        with pytest.raises(NewTeamIdRefused):
            ReviewFiles.open(opened, directory)


def test_a_new_team_id_is_refused_for_a_team_whose_id_fits(tmp_path: Path) -> None:
    from team_transfer.settings import SettingsSection
    from tests.pinned_memory_support import new_instance

    path, _config = new_instance(tmp_path / "old", "alpha", "team_a")
    out = tmp_path / "team.uatransfer"
    export_team(export_source(path), out, PASSPHRASE, owner_person_id="usr_ana", sections=(SettingsSection(),),
                key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    secret = tmp_path / "passphrase.txt"
    secret.write_text(PASSPHRASE)

    for command in (("review", "--review-dir", str(tmp_path / "review")), ("import", "--dry-run")):
        with pytest.raises(SystemExit, match="fits this version, so the team keeps it"):
            _run_cli(*command, "--in", str(out), "--instance-root", str(tmp_path / "new"),
                     "--new-team-id", "team_b", "--passphrase-file", str(secret))
    assert not (tmp_path / "review").exists() and not (tmp_path / "new").exists()


# -- a whole team through the command line ---------------------------------------------------


def _old_team(tmp_path: Path, registry: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    """An old instance of team ``OLD`` with records in every section; returns its config and the ids it derived."""

    import tests.test_team_transfer_schedules as schedule_tests
    from access_control.entities import agent_type_id
    from access_control.identity import ISSUER_GATEWAY, ContextSigner
    from assistant_gateway.bootstrap import bootstrap_instance
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents.internal_improvement_runtime import (
        internal_improvement_schedule_id,
        internal_improvement_schedule_name,
        internal_improvement_task_id,
    )
    from custom_agents.providers.mcp_connections import MCPConnectionAdministration
    from custom_agents.providers.mcp_hosting import MCPHostingAdministration
    from custom_messages.contracts.access_v2 import (
        AccessLevel,
        AccountLinkV1,
        DataFolderRecordV1,
        GrantV1,
        LinkOwnerKind,
        SubjectKind,
        SubjectRefV1,
        TargetKind,
        TargetRefV1,
    )
    from internal_credentials import parse_env_file
    from knowledge_base.service import DraftInput, KnowledgeService, KnowledgeSettings
    from mcp_control.hosting import MCPHostedServerStore, resolve_mcp_hosting_paths
    from mcp_control.registry import reconcile_instance_mcp_connections
    from storage.access_repository import AccessRepository
    from storage.gateway_state_repository import create_gateway_state_repository
    from storage.knowledge_repository import create_knowledge_repository
    from storage.objective_repository import create_objective_repository
    from storage.pinned_memory_repository import create_pinned_memory_repository
    from team_transfer.objectives import owner_scope
    from tests.memory_support import SYSTEM_MEMORY_WRITER
    from tests.test_team_transfer_control import _specialist
    from tests.test_team_transfer_integrations import DOCS, _install_procedure
    from tests.test_team_transfer_knowledge import _actor
    from tests.test_team_transfer_objectives import _objective
    from tests.test_team_transfer_team_id_renames import _hosted, _maintenance_schedule

    # People, sessions, person and system schedules, worker setups, pool sizes, the heartbeat list and a
    # team-owned job: the schedules tests' old instance, built for this team.
    with monkeypatch.context() as patched:
        patched.setattr(schedule_tests, "TEAM", OLD)
        path, store = schedule_tests._old_instance(tmp_path / "old")
    config = GatewayInstanceConfig.load(path)
    old_key = parse_env_file(config.layout.team_env_path)["ACCESS_GATEWAY_SIGNING_KEY"]

    # Settings built from the team id, and the configured and hosted MCP servers.
    config.transport.subject_prefix = OLD
    config.gateway.response_queue_name = f"{OLD}_alpha_custom"
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.agent.external_mcp_servers = [DOCS]
    config.mcp.hosting.enabled = True
    config.save(path)
    reconcile_instance_mcp_connections(config)
    hosted = MCPHostedServerStore(resolve_mcp_hosting_paths(config)).save(_hosted(OLD, lifecycle="pending"))
    MCPHostingAdministration(config).install(
        hosted.hosted_server_id, approval=MCPConnectionAdministration.person_approval("usr_admin"))
    _install_procedure(config.instance_root_path, registry, OLD)

    # Control files with a specialist the team lead made.
    bootstrap_instance(config)
    _specialist(config.layout.control_dir, "Approved", team_id=OLD)

    # The team's maintenance schedule, as its manager stored it and its gateway signed it.
    maintenance = _maintenance_schedule(OLD, interval_seconds=7 * 3600)
    maintenance.payload_meta.gateway_context.session_key = "sess_ana"
    maintenance.payload_meta.gateway_context.task_context = ContextSigner.from_value(ISSUER_GATEWAY, old_key).issue(
        team_id=OLD, person_id="usr_ana", person_roles=("owner",), schedule_id=maintenance.schedule_id)
    store.add(maintenance)
    create_gateway_state_repository(config.state_storage_settings).upsert_scheduled_target(
        scheduled_job_id=maintenance.schedule_id, session_key="sess_ana", conversation_id="conv_maintenance",
        owner_canonical_user_id="usr_ana")

    # Access: grants to a person, the lead's type, the team and a role; a data folder; the team's account link.
    owner = SubjectRefV1(kind=SubjectKind.USER, id="usr_ana")
    access = AccessRepository(config.manager_storage_settings)
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    access.register_data_folder(DataFolderRecordV1(
        record_id="data_folder_datasets", team_id=OLD, name="datasets", path=str(datasets), registered_by=owner,
        created_at_utc=NOW))
    for grant_id, subject, target in (
        ("g_person", SubjectRefV1(kind=SubjectKind.USER, id="usr_ben"), "fn:schedules"),
        ("g_lead", SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(OLD, "TeamArchitect")), "fn:schedules"),
        ("g_team", SubjectRefV1(kind=SubjectKind.TEAM, id=OLD), "fn:schedules"),
        ("g_role", SubjectRefV1(kind=SubjectKind.ROLE, id=f"{OLD}/admin"), "fn:schedules"),
    ):
        access.create(GrantV1(grant_id=grant_id, team_id=OLD, subject=subject,
                              target=TargetRefV1(kind=TargetKind.TOOL, id=target), level=AccessLevel.WRITE,
                              granted_by=owner, created_at_utc=NOW))
    access.create_link(AccountLinkV1(
        link_id="link_x_team", team_id=OLD, tool_id="cli:x", owner_kind=LinkOwnerKind.TEAM, owner_id=OLD,
        credential_key="x_team", created_by=owner, created_at_utc=NOW))

    # An objective of the team lead, team memory (two facts) and knowledge with a merged page.
    create_objective_repository(config.manager_storage_settings).insert_objective(
        _objective("obj_ana", owner_scope(config), "usr_ana").model_copy(update={"team_id": OLD}))
    memory = create_pinned_memory_repository(config.manager_storage_settings)
    for fact in ("The team ships on Fridays.", "Reports go out on Mondays."):
        memory.create_coremem(writer=SYSTEM_MEMORY_WRITER, team_id=OLD, fact_text=fact, capacity=10)
    knowledge = KnowledgeService(create_knowledge_repository(config.manager_storage_settings),
                                 settings=KnowledgeSettings())
    system = replace(_actor("sys"), team_id=OLD)
    deploy = knowledge.write(system, "team/deploy", DraftInput(title="Deploy", body="Deploy on Fridays.")).page
    extra = knowledge.write(system, "team/deploying", DraftInput(title="Deploying", body="Deploy after CI.")).page
    knowledge.merge(system, "team/deploying", "team/deploy", DraftInput(body="Deploy after CI, on Fridays."),
                    base_revision=deploy.revision, source_revision=extra.revision)
    derived = {
        "maintenance schedule": internal_improvement_schedule_id(OLD),
        "maintenance name": internal_improvement_schedule_name(OLD),
        "maintenance task": internal_improvement_task_id(OLD),
        "hosted server": hosted.hosted_server_id,
    }
    return path, derived


@pytest.fixture
def every_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, dict[str, str], Any]:
    """The old team exported with every section but credentials; the sections use the test's procedure registry."""

    import team_transfer.registry as registry_module
    from team_transfer.integrations import ProceduresSection
    from tests.test_team_transfer_integrations import _registry

    registry = _registry(tmp_path, "registry")
    sections = tuple(ProceduresSection(registry) if isinstance(section, ProceduresSection) else section
                     for section in registry_module.TRANSFER_SECTIONS)
    monkeypatch.setattr(registry_module, "TRANSFER_SECTIONS", sections)
    path, derived = _old_team(tmp_path, registry, monkeypatch)
    out = tmp_path / "team.uatransfer"
    export_team(export_source(path), out, PASSPHRASE, owner_person_id="usr_ana",
                sections=tuple(section for section in sections if not section.after_start),
                key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    secret = tmp_path / "passphrase.txt"
    secret.write_text(PASSPHRASE)
    return out, secret, derived, registry


def _files_naming(root: Path, needles: tuple[str, ...]) -> list[str]:
    """Each file under ``root`` but the import reports (SQLite stores aside) that names one of ``needles``."""

    from runtime_settings.instance_layout import InstanceLayout

    reports = set(InstanceLayout.at(root).team_import_dir.glob("report-*.json"))
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path in reports or path.read_bytes()[:16] == b"SQLite format 3\x00":
            continue
        text = path.read_bytes().decode("utf-8", "replace")
        found += [f"{path.relative_to(root)}: {needle}" for needle in needles if _names(text, needle)]
    return found


def test_a_long_team_id_moves_through_the_command_line_under_a_new_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], every_section: Any
) -> None:
    import team_transfer.review_web as review_web
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents.generated_agent_runtime import GeneratedAgentProvisioningRecordV1
    from custom_agents.installed_permissions import InstalledPermissionService, resolve_installed_permission_paths
    from custom_agents.internal_improvement_runtime import internal_improvement_schedule_id
    from custom_agents.schedule_store import ScheduleStore
    from instance_upgrades.protocol1_ids import check_ids_before_upgrade, load_saved_for_check
    from mcp_control.registry import MCPConnectionStore, reconcile_instance_mcp_connections, resolve_mcp_connection_paths
    from runtime_settings.instance_layout import InstanceLayout
    from storage.access_repository import AccessRepository
    from storage.gateway_state_repository import create_gateway_state_repository
    from storage.knowledge_repository import KNOWLEDGE_TRANSFER_TABLES, create_knowledge_repository
    from storage.objective_repository import create_objective_repository
    from storage.pinned_memory_repository import COREMEM_BANK, create_pinned_memory_repository
    from storage.worker_pool_repository import WorkerPoolRepository
    from tests.test_team_transfer_team_id_renames import _maintenance_schedule

    out, secret, derived, registry = every_section
    target, review, later = tmp_path / "new", tmp_path / "review", tmp_path / "later-review"
    common = ("--in", str(out), "--instance-root", str(target), "--passphrase-file", str(secret))
    # What the person does on the review page, done while the command holds the export open.
    on_the_page: list[Any] = []
    served: list[tuple[str | None, TeamIds, Any]] = []

    async def no_page(files: ReviewFiles) -> None:
        served.append((files.review.team_id, files.reader.team_ids, on_the_page.pop(0)(files)))

    monkeypatch.setattr(review_web, "serve_review", no_page)

    # Without the option the review is refused, naming it and onboarding's Import; nothing is written.
    with pytest.raises(SystemExit) as refused:
        _run_cli("review", *common, "--review-dir", str(review))
    assert "--new-team-id" in str(refused.value) and "onboarding" in str(refused.value)
    assert not review.exists() and not target.exists() and served == []

    with pytest.raises(SystemExit) as dry_run:
        _run_cli("import", *common, "--dry-run", "--new-team-id", NEW)
    assert dry_run.value.code == 1, "it lists records this version refuses, as for any team"
    assert f"Team {OLD} would be imported as {NEW}" in capsys.readouterr().out
    assert not target.exists()

    def select_all_but_one_fact(files: ReviewFiles) -> tuple[str, set[tuple[str, str]]]:
        [left_out] = [record.key for record in files.review.records if record.section == "pinned_memory"
                      and files.archive["pinned_memory"][record.key]["document"]["fact_text"].startswith("Reports")]
        return left_out, _select_importable(files, {("pinned_memory", left_out)})

    on_the_page.append(select_all_but_one_fact)
    _run_cli("review", *common, "--review-dir", str(review), "--new-team-id", NEW)
    [(recorded, ids, (left_out, refused_records))] = served
    assert (recorded, ids) == (NEW, IDS)
    # Only the old instance's own edge cases are refused (a removed person's, records that do not move), as
    # when a team keeps its id; none for the new id.
    assert refused_records == {
        ("schedules", key) for key in (
            "session/sess_gone", "schedule/sched_gone", "schedule/heartbeat_launcher", "schedule/sched_aar",
            "heartbeat_item/hb_goal", "worker_binding/sched_sys_worker", "pool_limit/GhostAgent",
            "pool_limit/PersonalAdminAgent", "execution_job/exec_job_1",
        )
    }

    # The review folder supplies the id: another one is refused naming both, and none is needed.
    with pytest.raises(SystemExit) as refused:
        _run_cli("import", *common, "--review", str(review), "--new-team-id", OTHER)
    assert f"prepared to import the team as {NEW!r}, and {OTHER!r} was asked for" in str(refused.value)
    assert not target.exists()
    _run_cli("import", *common, "--review", str(review))
    printed = capsys.readouterr().out
    assert f"Team {OLD} was imported as {NEW}: every imported record names the new team id." in printed

    # Nothing in the new instance names the old id or an id the old instance derived from it; only the
    # import report, the history of where the team came from, does. The same search finds each of them in
    # the old instance.
    needles = (OLD, *derived.values())
    old_root = tmp_path / "old" / "alpha"
    found = {hit.rsplit(": ", 1)[1] for hit in _files_naming(old_root, needles) + _stored_naming(old_root, needles)}
    assert found == set(needles)
    assert _files_naming(target, needles) == []
    assert _stored_naming(target, needles) == []
    layout = InstanceLayout.at(target)
    [report] = layout.team_import_dir.glob("report-*.json")
    assert json.loads(report.read_text())["source"]["team_id"] == OLD
    assert json.loads(layout.team_import_receipt_path.read_text())["team_id"] == NEW

    # Each section's records are there, the new team's.
    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert (config.gateway.team_id, config.transport.subject_prefix, config.gateway.response_queue_name) == (
        NEW, NEW, f"{NEW}_alpha_custom")
    people = create_gateway_state_repository(config.state_storage_settings)
    assert sorted(person.canonical_user_id for person in people.list_team_people(NEW)) == ["usr_ana", "usr_ben"]
    access = AccessRepository(config.manager_storage_settings)
    assert {grant.grant_id for grant in access.list_team(NEW)} == {"g_person", "g_lead", "g_team", "g_role"}
    assert [link.owner_id for link in access.list_links(NEW)] == [NEW]
    assert [folder.name for folder in access.data_folders(NEW)] == ["datasets"]
    specialist = layout.control_dir / "agent_bundles" / "team_lead_generated" / "Approved" / "provisioning.json"
    assert GeneratedAgentProvisioningRecordV1.model_validate_json(specialist.read_text()).team_id == NEW
    assert [o.objective_id for o in create_objective_repository(
        config.manager_storage_settings).list_team_objectives(team_id=NEW)] == ["obj_ana"]
    store = ScheduleStore(storage_settings=config.manager_storage_settings, default_cron_timezone="UTC")
    maintenance_id = internal_improvement_schedule_id(NEW)
    assert store.get_scheduled_message("sched_daily").claim_team_id == NEW
    assert store.get_scheduled_message(maintenance_id).interval_ms == 7 * 3600 * 1000
    assert store.worker_records.get_binding("sched_worker", team_id=NEW) is not None
    assert WorkerPoolRepository(config.manager_storage_settings).list_limits(NEW)
    facts = create_pinned_memory_repository(config.manager_storage_settings).list_unexpired_facts()
    assert [(f.team_id, f.fact_text) for f in facts if f.bank == COREMEM_BANK] == [(NEW, "The team ships on Fridays.")]
    knowledge = create_knowledge_repository(config.manager_storage_settings)
    assert {row["team_id"] for table in KNOWLEDGE_TRANSFER_TABLES for row in knowledge.export_rows(table)} == {NEW}
    connections = MCPConnectionStore(resolve_mcp_connection_paths(target))
    assert {record.descriptor.team_id for record in connections.list_records()} == {NEW}
    procedures = InstalledPermissionService(paths=resolve_installed_permission_paths(target), registry=registry)
    assert len(procedures.list_installed(team_id=NEW)) == 1

    # The first start: the launcher's id check passes, the manager's maintenance schedule is the imported one,
    # and the startup reconciliation keeps the MCP records as imported.
    checked = load_saved_for_check(target / "instance_config.yaml")
    assert check_ids_before_upgrade(checked, lambda: checked.manager_storage_settings) is None
    assert store.add_if_absent(_maintenance_schedule(NEW)) is False
    before = {record.connection_id: record.record_digest for record in connections.list_records()}
    reconcile_instance_mcp_connections(config)
    assert {record.connection_id: record.record_digest for record in connections.list_records()} == before

    # A later batch from the same backup: a new review folder takes the id from the receipt, and a different
    # one is refused naming both.
    with pytest.raises(SystemExit) as refused:
        _run_cli("review", *common, "--review-dir", str(tmp_path / "other-review"), "--new-team-id", OTHER)
    assert f"gave the team the id {NEW!r}" in str(refused.value) and f"{OTHER!r} was asked for" in str(refused.value)
    on_the_page.append(lambda files: _choose(files, lambda record: (record.section, record.key) == (
        "pinned_memory", left_out)))
    _run_cli("review", *common, "--review-dir", str(later))
    assert served[-1][:2] == (NEW, IDS)
    _run_cli("import", *common, "--review", str(later))
    assert "Imported 1 of 1 record(s)" in capsys.readouterr().out
    facts = create_pinned_memory_repository(config.manager_storage_settings).list_unexpired_facts()
    assert sorted((f.team_id, f.fact_text) for f in facts if f.bank == COREMEM_BANK) == [
        (NEW, "Reports go out on Mondays."), (NEW, "The team ships on Fridays.")]
    assert _files_naming(target, needles) == [] and _stored_naming(target, needles) == []


# -- credentials after the first start ---------------------------------------------------------


@pytest.mark.asyncio
async def test_import_credentials_stores_the_teams_credentials_under_the_receipts_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    import team_transfer.review_web as review_web
    from internal_credentials import parse_env_file
    from secure_tool_broker.credential_state import credential_owner_context
    from secure_tool_broker.credential_state_store import read_credential_state, write_credential_state
    from storage.gateway_state_repository import create_gateway_state_repository
    from team_transfer.broker_credentials import BrokerCredentialsSection
    from team_transfer.people import PeopleSection
    from team_transfer.settings import SettingsSection
    from tests.pinned_memory_support import new_instance
    from tests.test_team_transfer_broker_credentials import X_APP, _use_store
    from tests.test_team_transfer_team_id_renames import _serving_broker

    sections = (SettingsSection(), PeopleSection(), BrokerCredentialsSection())
    path, config = new_instance(tmp_path / "old", "alpha", OLD)
    (path.parent / "team.env").write_text("SECURE_TOOL_BROKER_TOKEN=old-broker-root\n")
    people = create_gateway_state_repository(config.state_storage_settings)
    people.create_gateway_user(canonical_user_id="usr_ana", display_name="Ana", timezone="UTC")
    people.create_team_membership(canonical_user_id="usr_ana", team_id=OLD, role="owner")
    _use_store(tmp_path / "old_store", monkeypatch)
    with credential_owner_context(OLD, scope_kind="team"):
        assert write_credential_state("x.oauth_client_json", X_APP).status == "success"
    out = tmp_path / "team.uatransfer"
    broker = await _serving_broker(OLD, "old-broker-root", monkeypatch)
    try:
        await asyncio.to_thread(export_team, export_source(path), out, PASSPHRASE, owner_person_id="usr_ana",
                                sections=sections, key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    finally:
        await broker.close()
    secret = tmp_path / "passphrase.txt"
    secret.write_text(PASSPHRASE)
    target, review = tmp_path / "new", tmp_path / "review"
    team_owners: list[set[str]] = []

    async def no_page(files: ReviewFiles) -> None:
        index = files.archive["broker_credentials"].values()
        team_owners.append({entry["owner"]["owner_id"] for entry in index if entry["owner"]["kind"] == "team"})

    monkeypatch.setattr(review_web, "serve_review", no_page)
    common = ("--in", str(out), "--passphrase-file", str(secret))
    await asyncio.to_thread(_run_cli, "review", *common, "--instance-root", str(target), "--review-dir", str(review),
                            "--new-team-id", NEW)
    assert team_owners == [{NEW}], "the review shows the team's credentials as the new team's"
    await asyncio.to_thread(_run_cli, "import", *common, "--instance-root", str(target), "--review", str(review))
    capsys.readouterr()

    # The command takes the id from the receipt: a review prepared for another id is refused before the broker.
    saved = (review / "review.json").read_text()
    (review / "review.json").write_text(json.dumps({**json.loads(saved), "team_id": OTHER}))
    config_path = str(target / "instance_config.yaml")
    with pytest.raises(SystemExit) as refused:
        await asyncio.to_thread(_run_cli, "import-credentials", *common, "--config", config_path, "--review", str(review))
    assert f"prepared to import the team as {OTHER!r}, and this instance's first import gave it the id {NEW!r}" in str(
        refused.value)
    (review / "review.json").write_text(saved)

    # The new instance's first start: its broker is team NEW.
    _use_store(tmp_path / "new_store", monkeypatch)
    broker = await _serving_broker(NEW, parse_env_file(target / "team.env")["SECURE_TOOL_BROKER_TOKEN"], monkeypatch)
    try:
        await asyncio.to_thread(_run_cli, "import-credentials", *common, "--config", config_path, "--review", str(review))
    finally:
        await broker.close()

    assert "Stored 1 of 1 credential record(s) through the broker." in capsys.readouterr().out
    with credential_owner_context(NEW, scope_kind="team"):
        assert read_credential_state("x.oauth_client_json") == X_APP
    assert _stored_naming(target, (OLD,)) == [] and _stored_naming(tmp_path / "new_store", (OLD,)) == []
