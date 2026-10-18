"""The ``access`` transfer section carries lasting grants, data folders, account links and person settings."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from access_control.entities import agent_type_id
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccountLinkV1,
    DataFolderRecordV1,
    GrantKind,
    GrantLifetime,
    GrantV1,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.person_settings_v1 import PersonSettingsV1
from runtime_settings.instance_layout import person_folder_name
from storage.access_repository import AccessRepository
from team_transfer.access import AccessSection
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.people import PeopleSection
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), PeopleSection(), AccessSection())
TEAM = "team_a"
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
OWNER = SubjectRefV1(kind=SubjectKind.USER, id="usr_ana")
_CEDAR_BINARY = Path(__file__).resolve().parents[1] / "target/debug/ua-cedar-authorizer"


def _grant(grant_id: str, subject: SubjectRefV1, target: TargetRefV1, **fields: Any) -> GrantV1:
    return GrantV1(
        grant_id=grant_id,
        team_id=TEAM,
        subject=subject,
        target=target,
        level=AccessLevel.WRITE,
        granted_by=OWNER,
        created_at_utc=NOW,
        **fields,
    )


def _old_instance(tmp_path: Path) -> tuple[Path, Path]:
    from storage.gateway_state_repository import create_gateway_state_repository

    path, config = new_instance(tmp_path, "alpha", TEAM)
    people = create_gateway_state_repository(config.state_storage_settings)
    for person, role in (("usr_ana", "owner"), ("usr_ben", "member")):
        people.create_gateway_user(canonical_user_id=person, display_name=person, timezone="UTC")
        people.create_team_membership(canonical_user_id=person, team_id=TEAM, role=role)
    people.create_gateway_user(canonical_user_id="usr_gone", display_name="Gone", timezone="UTC")
    people.create_team_membership(canonical_user_id="usr_gone", team_id=TEAM, role="member", status="removed")

    store = AccessRepository(config.manager_storage_settings)
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    for name, folder in (("datasets", datasets), ("vanished", tmp_path / "vanished-folder")):
        store.register_data_folder(
            DataFolderRecordV1(
                record_id=f"data_folder_{name}", team_id=TEAM, name=name, path=str(folder),
                registered_by=OWNER, created_at_utc=NOW,
            )
        )
    ana_folder = person_folder_name("usr_ana")
    for grant in (
        _grant("g_schedules", SubjectRefV1(kind=SubjectKind.USER, id="usr_ben"), TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules")),
        _grant("g_share", SubjectRefV1(kind=SubjectKind.USER, id="usr_ben"), TargetRefV1(kind=TargetKind.FOLDER, id=f"users/{ana_folder}/reports")),
        _grant("g_data", SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "TeamArchitect")), TargetRefV1(kind=TargetKind.FOLDER, id="data/datasets"), for_person_id="usr_ana"),
        _grant("g_vanished", SubjectRefV1(kind=SubjectKind.USER, id="usr_ana"), TargetRefV1(kind=TargetKind.FOLDER, id="data/vanished")),
        _grant("g_gone", SubjectRefV1(kind=SubjectKind.USER, id="usr_gone"), TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules")),
        _grant("g_team_tool", SubjectRefV1(kind=SubjectKind.USER, id="usr_ana"), TargetRefV1(kind=TargetKind.TOOL, id="cli:tea")),
        _grant("g_task", SubjectRefV1(kind=SubjectKind.USER, id="usr_ana"), TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"),
               lifetime=GrantLifetime.TASK, task_id="task_1"),
    ):
        store.create(grant)
    store.create_link(
        AccountLinkV1(
            link_id="link_github_ana", team_id=TEAM, tool_id="cli:github", owner_kind=LinkOwnerKind.PERSON,
            owner_id="usr_ana", credential_key="github_ana", created_by=OWNER, created_at_utc=NOW,
        )
    )
    store.change_person_settings(
        TEAM, "usr_ana", lambda _old: PersonSettingsV1(team_id=TEAM, person_id="usr_ana", uses_team_keys=False, changed_at_utc=NOW)
    )
    store.record_profile_choice(TEAM, "usr_ben", "standard", source="person", at=NOW)
    return path, datasets


def test_export_waits_for_unconfirmed_external_effects_to_be_reconciled(tmp_path: Path) -> None:
    from datetime import timedelta

    from assistant_gateway.config import GatewayInstanceConfig
    from custom_messages.contracts.access_v2 import AccessRequestStatus, AccessRequestV1
    from storage.access_repository import DecisionClaim, DecisionRecovery
    from team_transfer.errors import TransferError

    config_path, _datasets = _old_instance(tmp_path)
    store = AccessRepository(GatewayInstanceConfig.load(config_path).manager_storage_settings)
    request = store.create_request(AccessRequestV1(
        request_id="access_req_uncertain", team_id=TEAM, requester_agent_id="agent-lead",
        requester_agent_type="TeamArchitect",
        subject=SubjectRefV1(kind=SubjectKind.USER, id="usr_ana"),
        target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"),
        level=AccessLevel.WRITE, reason="Approve the change", created_at_utc=NOW,
    ))
    claim = DecisionClaim(request.request_id, "decision_uncertain")
    store.claim_decision(request.request_id, claim_id=claim.claim_id, at=NOW, until=NOW + timedelta(minutes=10))
    finished = request.model_copy(update={"status": AccessRequestStatus.GRANTED})
    store.save_decision_recovery(DecisionRecovery(claim, finished, {}, NOW))
    out = tmp_path / "team.uatransfer"
    with pytest.raises(TransferError, match="awaiting confirmation"):
        export_team(
            export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana", sections=SECTIONS,
            key_derivation=new_key_derivation(**FAST_KEYS),
        )
    assert not out.exists()


def test_lasting_access_arrives_and_what_would_dangle_is_refused(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    config_path, datasets = _old_instance(tmp_path)
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=False, sections=SECTIONS)

    outcomes = {r.key: r for r in plan.records if r.section == "access"}
    imported = {key for key, r in outcomes.items() if r.outcome is RecordOutcome.IMPORT}
    assert imported == {
        "data_folder/datasets",
        "grant/g_schedules",
        "grant/g_share",
        "grant/g_data",
        "account_link/link_github_ana",
        "person_settings/usr_ana",
        "profile_choice/usr_ben",
    }
    assert "grant/g_task" not in outcomes, "task grants end with their task and stay behind"
    assert "not an existing folder on this host" in outcomes["data_folder/vanished"].reason
    assert "data folder vanished is not imported" in outcomes["grant/g_vanished"].reason
    assert "usr_gone, who is not imported" in outcomes["grant/g_gone"].reason
    assert "cli:tea does not exist" in outcomes["grant/g_team_tool"].reason

    store = AccessRepository(GatewayInstanceConfig.load(target / "instance_config.yaml").manager_storage_settings)
    grants = {grant.grant_id: grant for grant in store.list_team(TEAM)}
    assert set(grants) == {"g_schedules", "g_share", "g_data"}
    assert grants["g_data"].for_person_id == "usr_ana" and grants["g_data"].granted_by == OWNER
    assert [folder.path for folder in store.data_folders(TEAM)] == [str(datasets)]
    assert [link.link_id for link in store.list_links(TEAM)] == ["link_github_ana"]
    assert store.person_settings(TEAM, "usr_ana").uses_team_keys is False
    assert store.profile_choice(TEAM, "usr_ben") == ("standard", "person")


@pytest.mark.parametrize("kind", [GrantKind.ACCESS, GrantKind.BLOCK])
def test_grants_for_agent_types_follow_the_imported_definitions(tmp_path: Path, kind: GrantKind) -> None:
    from team_transfer.control import ControlSection
    from tests.test_team_transfer_control import GOOD_BUNDLE

    config_path, _datasets = _old_instance(tmp_path)
    from assistant_gateway.config import GatewayInstanceConfig

    config = GatewayInstanceConfig.load(config_path)
    bundle = config.layout.control_dir / "agent_bundles" / "travel"
    bundle.mkdir(parents=True)
    (bundle / "agent.yaml").write_text(GOOD_BUNDLE)
    (bundle / "prompt.md").write_text("You plan trips.\n")
    store = AccessRepository(config.manager_storage_settings)
    tool = TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules")
    store.create(_grant("g_travel", SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "TravelPlanner")), tool, kind=kind))
    store.create(_grant("g_nobody", SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "NobodyAgent")), tool, kind=kind))
    # Agent types are named <team>/<type>, as every agent's subjects are; a bare or another team's id names nobody.
    store.create(_grant("g_bare", SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id="TravelPlanner"), tool, kind=kind))
    store.create(_grant("g_other_team", SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id="team_b/TravelPlanner"), tool, kind=kind))
    sections = (SettingsSection(), ControlSection(), PeopleSection(), AccessSection())
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana", sections=sections,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )

    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, tmp_path / "new", dry_run=True, sections=sections)

    outcomes = {r.key: r for r in plan.records if r.section == "access"}
    assert outcomes["grant/g_travel"].outcome is RecordOutcome.IMPORT
    assert outcomes["grant/g_nobody"].outcome is RecordOutcome.REFUSED
    assert "agent type NobodyAgent does not exist" in outcomes["grant/g_nobody"].reason
    assert outcomes["grant/g_bare"].outcome is RecordOutcome.REFUSED
    assert "TravelPlanner is not an agent type of team team_a" in outcomes["grant/g_bare"].reason
    assert outcomes["grant/g_other_team"].outcome is RecordOutcome.REFUSED
    assert "team_b/TravelPlanner is not an agent type of team team_a" in outcomes["grant/g_other_team"].reason


@pytest.mark.integration
@pytest.mark.skipif(not _CEDAR_BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_imported_agent_access_and_blocks_still_control_real_decisions(tmp_path: Path) -> None:
    from access_catalog.catalog import load_catalog
    from access_control.cedar import CedarAccessClient
    from access_control.decisions import AccessService
    from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver
    from assistant_gateway.config import GatewayInstanceConfig

    config_path, _datasets = _old_instance(tmp_path)
    store = AccessRepository(GatewayInstanceConfig.load(config_path).manager_storage_settings)
    lead_subject = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "TeamArchitect"))
    worker_subject = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "WebResearchAgent"))
    # Google has no standard agent access, so an allowed decision proves the imported grant counts.
    google = TargetRefV1(kind=TargetKind.TOOL, id="cli:google")
    resolution = load_catalog(None).resolve_broker_action("google", "gog.exec", {"argv": ["gmail", "search", "transfer test"]})
    original = (
        _grant("g_lead_google", lead_subject, google, for_person_id="usr_ana"),
        _grant("g_worker_default", worker_subject, google),
        _grant("g_ben_google", SubjectRefV1(kind=SubjectKind.USER, id="usr_ben"), google),
        _grant(
            "b_worker_search", worker_subject,
            TargetRefV1(kind=TargetKind.OPERATION, id=f"{resolution.tool_id}#{resolution.operation}"),
            kind=GrantKind.BLOCK, reason="Keep the worker from searching mail",
        ),
    )
    for grant in original:
        store.create(grant)

    plan, target = _export_and_import(tmp_path, config_path)

    outcomes = {record.key: record for record in plan.records if record.section == "access"}
    imported = _target_store(target)
    for grant in original:
        assert outcomes[f"grant/{grant.grant_id}"].outcome is RecordOutcome.IMPORT
        assert imported.get(grant.grant_id) == grant
    cedar = CedarAccessClient(binary=str(_CEDAR_BINARY))
    try:
        service = AccessService(
            store=imported, catalog=load_catalog(target), cedar=cedar, team_id=TEAM,
            agent_role_of=agent_role_resolver("TeamArchitect"),
        )
        ana = PersonPrincipal("usr_ana", TEAM, ("owner",))
        ben = PersonPrincipal("usr_ben", TEAM, ("member",))
        lead = AgentPrincipal("agent_lead", "TeamArchitect", "team_lead", TEAM)
        worker = AgentPrincipal("agent_worker", "WebResearchAgent", "worker", TEAM)

        def decide(agent: AgentPrincipal, person: PersonPrincipal):
            return service.decide(agent=agent, person=person, resolution=resolution, task_id="task_after_import")

        assert decide(lead, ana).allowed
        assert not decide(lead, ben).allowed, "Ana's agent grant must not apply to Ben's work"
        blocked = decide(worker, ana)
        assert not blocked.allowed
        assert "block:b_worker_search" in blocked.blocking_policy_ids
        imported.revoke("b_worker_search", actor=OWNER, at=datetime.now(timezone.utc))
        assert decide(worker, ana).allowed, "the worker's imported team default applies once its block ends"
    finally:
        cedar.close()


# -- the team's sharing choices (access_control.account_sharing) -----------------------


def _export_and_import(tmp_path: Path, config_path: Path):
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=False, sections=SECTIONS)
    return plan, target


def _target_store(target: Path) -> AccessRepository:
    from assistant_gateway.config import GatewayInstanceConfig

    return AccessRepository(GatewayInstanceConfig.load(target / "instance_config.yaml").manager_storage_settings)


def test_sharing_choices_travel_with_who_made_them_and_a_fixed_service_is_refused(tmp_path: Path) -> None:
    from access_catalog.catalog import CatalogSource
    from access_control.account_sharing import TeamAccountSharing
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_messages.contracts.access_v2 import AccountSharing, AccountSharingV1

    config_path, _datasets = _old_instance(tmp_path)
    store = AccessRepository(GatewayInstanceConfig.load(config_path).manager_storage_settings)
    sharing = TeamAccountSharing(store, TEAM, now=lambda: NOW)
    sharing.choose_tool(CatalogSource(None).current().tools["http:exa"], AccountSharing.PRIVATE, by="usr_ana")
    sharing.choose_model_keys(AccountSharing.PRIVATE, by="usr_ana")
    # A choice this version does not allow (the GitHub API is always private).
    store.set_account_sharing(
        AccountSharingV1(team_id=TEAM, subject="http:github", sharing=AccountSharing.SHARED, changed_at_utc=NOW)
    )

    plan, target = _export_and_import(tmp_path, config_path)

    outcomes = {r.key: r for r in plan.records if r.section == "access"}
    assert outcomes["account_sharing/http:exa"].outcome is RecordOutcome.IMPORT
    assert outcomes["account_sharing/model_keys"].outcome is RecordOutcome.IMPORT
    assert outcomes["account_sharing/http:github"].outcome is RecordOutcome.REFUSED
    assert "keeps GitHub API always private" in outcomes["account_sharing/http:github"].reason
    imported = {record.subject: record for record in _target_store(target).team_account_sharing(TEAM)}
    assert set(imported) == {"http:exa", "model_keys"}
    assert imported["http:exa"].sharing is AccountSharing.PRIVATE and imported["http:exa"].changed_by_person_id == "usr_ana"
    assert imported["model_keys"].changed_at_utc == NOW



def _block(grant_id: str, operation: str) -> GrantV1:
    return _grant(
        grant_id,
        SubjectRefV1(kind=SubjectKind.USER, id="usr_ben"),
        TargetRefV1(kind=TargetKind.OPERATION, id=operation),
        kind=GrantKind.BLOCK,
        reason="Not for Ben",
    )


def test_blocks_on_renamed_operations_keep_their_scope(tmp_path: Path) -> None:
    from access_catalog.catalog import load_catalog
    from access_control.stale_blocks import decided_grant
    from assistant_gateway.config import GatewayInstanceConfig
    from team_transfer.access import RENAMED_OPERATIONS

    config_path, _datasets = _old_instance(tmp_path)
    store = AccessRepository(GatewayInstanceConfig.load(config_path).manager_storage_settings)
    store.create(_block("b_find_tools", "fn:access#find_tools"))
    store.create(_block("b_delete_facts", "fn:memory#delete_usermem"))
    store.create(_block("b_start_server", "fn:capabilities#manage_hosted_mcp_server.start"))
    store.create(_block("b_objective_status", "fn:objectives#update_objective_status"))
    store.create(_block("b_split", "fn:capabilities#plan_hosted_mcp_server_change"))
    store.create(_block("b_removed", "fn:web_research#old_exa_search"))
    store.create(_block("b_current", "fn:web_research#exa_search"))

    # Releases exported schema 3 before the action tools were merged, so
    # version 3 files may contain these old operation names.
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana",
        sections=(SettingsSection(), PeopleSection(), AccessSection()),
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        original = reader.section_document("access")
        assert original.schema_version == AccessSection.schema_version
        preview = import_team(reader, target, dry_run=True, sections=SECTIONS)
        assert not target.exists()
        plan = import_team(reader, target, dry_run=False, sections=SECTIONS)
        assert reader.section_document("access") == original, "conversion must not change the signed source"
    assert preview.records == plan.records

    outcomes = {r.key: r for r in plan.records if r.section == "access"}
    imported = {grant.grant_id: grant for grant in _target_store(target).list_team(TEAM)}
    assert RENAMED_OPERATIONS["fn:access#find_tools"] == "fn:tool_loading#find_tools"
    assert outcomes["grant/b_find_tools"].outcome is RecordOutcome.IMPORT
    assert imported["b_find_tools"].target.id == "fn:tool_loading#find_tools"
    assert outcomes["grant/b_delete_facts"].outcome is RecordOutcome.IMPORT
    assert imported["b_delete_facts"].target.id == "fn:memory#memory.delete"
    assert imported["b_start_server"].target.id == "fn:capabilities#hosted_mcp_servers.start"
    assert outcomes["grant/b_objective_status"].outcome is RecordOutcome.IMPORT
    assert imported["b_objective_status"].target.id == "fn:objectives#objectives.update_status"
    catalog = load_catalog(target)
    for block_id in ("b_delete_facts", "b_start_server", "b_objective_status"):
        assert decided_grant(catalog, imported[block_id]).target == imported[block_id].target
    # One function became two actions: no one new name, so it blocks the whole tool, for attention.
    assert outcomes["grant/b_split"].outcome is RecordOutcome.ATTENTION
    assert "from using all of fn:capabilities" in outcomes["grant/b_split"].reason
    assert outcomes["grant/b_current"].outcome is RecordOutcome.IMPORT
    assert imported["b_current"].target.id == "fn:web_research#exa_search"
    # No new name: it travels for attention, and blocks the whole tool here (access_control.stale_blocks).
    removed = outcomes["grant/b_removed"]
    assert removed.outcome is RecordOutcome.ATTENTION
    assert "will stop usr_ben from using all of fn:web_research" in removed.reason
    assert imported["b_removed"].target.id == "fn:web_research#old_exa_search"


def test_every_renamed_operation_names_an_operation_this_version_has() -> None:
    from access_catalog.catalog import load_catalog
    from team_transfer.access import RENAMED_OPERATIONS

    catalog = load_catalog(None)
    for old, new in RENAMED_OPERATIONS.items():
        tool_id, _, operation = new.partition("#")
        assert catalog.tools[tool_id].operation(operation) is not None, new
        old_tool, _, old_operation = old.partition("#")
        assert catalog.tools[old_tool].operation(old_operation) is None, f"{old} still exists"


def test_transfer_reviews_blocks_against_the_accepted_cli_http_and_mcp_definitions(tmp_path: Path) -> None:
    from access_catalog.catalog import load_catalog
    from access_catalog.instance_tools import write_instance_tool
    from access_control.stale_blocks import decided_grant
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_messages.contracts.access_v2 import ProposedHttpToolV1
    from mcp_control.registry import (
        MCPOperationDefinitionV1, MCPConnectionStore, reconcile_instance_mcp_connections, resolve_mcp_connection_paths,
    )
    from team_transfer.integrations import McpSection
    from team_transfer.team_tools import TeamToolsSection
    from tests.test_team_transfer_integrations import DOCS
    from tests.test_team_transfer_tools import PROGRAM, _cli

    config_path, _datasets = _old_instance(tmp_path)
    config = GatewayInstanceConfig.load(config_path)
    write_instance_tool(config.instance_root_path, _cli("tea", PROGRAM))
    program = config.layout.workspaces_dir / "team/tools/tea/tea"
    program.parent.mkdir(parents=True)
    program.write_bytes(PROGRAM)
    program.chmod(0o755)
    write_instance_tool(config.instance_root_path, ProposedHttpToolV1(
        tool_id="http:gitea", display_name="Gitea", hosts=("gitea.example.com",),
    ))
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.agent.external_mcp_servers = [DOCS]
    config.save(config_path)
    [connection] = reconcile_instance_mcp_connections(config)
    MCPConnectionStore(resolve_mcp_connection_paths(config.instance_root_path)).set_operations(
        connection.connection_id, [MCPOperationDefinitionV1.build(operation_name="search", input_schema={"type": "object"})],
    )
    store = AccessRepository(config.manager_storage_settings)
    current = {"cli:tea": "command:repo.list", "http:gitea": "get", "mcp:docs": "search"}
    for index, (tool_id, operation) in enumerate(current.items()):
        store.create(_block(f"b_current_{index}", f"{tool_id}#{operation}"))
        store.create(_block(f"b_stale_{index}", f"{tool_id}#removed_operation"))
    sections = (SettingsSection(), TeamToolsSection(), McpSection(), PeopleSection(), AccessSection())
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana",
        sections=sections, key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        preview = import_team(reader, target, dry_run=True, sections=sections)
        assert not target.exists(), "review must inspect archive definitions without writing the destination"
        plan = import_team(reader, target, dry_run=False, sections=sections)
        unselected = import_team(reader, tmp_path / "without-tools", dry_run=True, sections=SECTIONS)
    assert preview.records == plan.records
    outcomes = {r.key: r for r in plan.records if r.section == "access"}
    refused = {r.key: r for r in unselected.records if r.section == "access"}
    imported = {block.grant_id: block for block in _target_store(target).list_team(TEAM)}
    catalog = load_catalog(target)
    for index, tool_id in enumerate(current):
        assert outcomes[f"grant/b_current_{index}"].outcome is RecordOutcome.IMPORT
        attention = outcomes[f"grant/b_stale_{index}"]
        assert attention.outcome is RecordOutcome.ATTENTION
        assert f"using all of {tool_id}" in attention.reason
        effective = decided_grant(catalog, imported[f"b_stale_{index}"])
        assert effective.target == TargetRefV1(kind=TargetKind.TOOL, id=tool_id)
        assert refused[f"grant/b_stale_{index}"].outcome is RecordOutcome.REFUSED


def test_team_links_this_version_would_not_use_are_refused_or_shown_for_attention(tmp_path: Path) -> None:
    # An earlier release let every team account serve everyone, and took a
    # team link for the GitHub API. This one never uses that link, and keeps
    # X's team account unused until an administrator shares X.
    from assistant_gateway.config import GatewayInstanceConfig

    config_path, _datasets = _old_instance(tmp_path)
    store = AccessRepository(GatewayInstanceConfig.load(config_path).manager_storage_settings)
    for tool_id in ("http:github", "cli:x", "http:exa"):
        store.create_link(
            AccountLinkV1(
                link_id=f"link_{tool_id.replace(':', '_')}_team", team_id=TEAM, tool_id=tool_id,
                owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, credential_key=f"{tool_id.replace(':', '-')}-team",
                created_by=OWNER, created_at_utc=NOW,
            )
        )

    plan, target = _export_and_import(tmp_path, config_path)

    outcomes = {r.key: r for r in plan.records if r.section == "access"}
    github = outcomes["account_link/link_http_github_team"]
    x = outcomes["account_link/link_cli_x_team"]
    assert github.outcome is RecordOutcome.REFUSED
    assert "uses only each person's own account for GitHub API, so the link would never be used" in github.reason
    assert x.outcome is RecordOutcome.ATTENTION
    assert "not used while X (xurl) is private (where this version starts it)" in x.reason
    assert "/accounts share cli:x after the import uses it" in x.reason
    assert outcomes["account_link/link_http_exa_team"].outcome is RecordOutcome.IMPORT
    imported = {link.tool_id for link in _target_store(target).list_links(TEAM)}
    assert {"cli:x", "http:exa"} <= imported and "http:github" not in imported


def test_a_file_with_two_links_for_one_tool_and_owner_refuses_the_older(tmp_path: Path, monkeypatch) -> None:
    # A store from before the one-active-link index could export two active
    # links for one tool and owner; only the newer one was in use.
    from datetime import timedelta

    config_path, _datasets = _old_instance(tmp_path)
    export = AccessSection.export

    def exported_by_an_older_store(self, source, files):  # noqa: ANN001 - patched method
        result = export(self, source, files)
        older = AccountLinkV1(
            link_id="link_github_ana_older", team_id=TEAM, tool_id="cli:github", owner_kind=LinkOwnerKind.PERSON,
            owner_id="usr_ana", credential_key="github_ana_older", created_by=OWNER, created_at_utc=NOW - timedelta(days=1),
        )
        result.records.append({"kind": "account_link", "record_id": older.link_id, "document": older.model_dump(mode="json")})
        return result

    monkeypatch.setattr(AccessSection, "export", exported_by_an_older_store)
    plan, target = _export_and_import(tmp_path, config_path)

    outcomes = {r.key: r for r in plan.records if r.section == "access"}
    older = outcomes["account_link/link_github_ana_older"]
    assert older.outcome is RecordOutcome.REFUSED
    assert "the newer account link link_github_ana for the same tool and owner replaces it" in older.reason
    assert outcomes["account_link/link_github_ana"].outcome is RecordOutcome.IMPORT
    assert [link.link_id for link in _target_store(target).list_links(TEAM)] == ["link_github_ana"]


@pytest.mark.skipif(not _CEDAR_BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_a_failed_decision_leaves_nothing_to_export_and_older_leftovers_stay_behind(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    import logging
    import sqlite3

    from access_catalog.catalog import load_catalog
    from access_control.cedar import CedarAccessClient
    from access_control.decisions import AccessService, HandDownRefused
    from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver
    from access_control.inbox import StoredAccessInbox
    from access_control.requests import AccessRequests
    from access_control.starter_profiles import apply_starter_profile
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_messages.contracts.access_v2 import AccessReviewChoice

    config_path, _datasets = _old_instance(tmp_path)
    store = AccessRepository(GatewayInstanceConfig.load(config_path).manager_storage_settings)
    cedar = CedarAccessClient(binary=str(_CEDAR_BINARY))
    try:
        service = AccessService(
            store=store, catalog=load_catalog(None), cedar=cedar, team_id=TEAM,
            agent_role_of=agent_role_resolver("TeamArchitect"),
        )
        requests = AccessRequests(service=service, inbox=StoredAccessInbox(store), lead_agent_type="TeamArchitect")
        ana = PersonPrincipal("usr_ana", TEAM, ("owner",))
        apply_starter_profile(service, ana, "careful", lead_agent_type="TeamArchitect")
        worker = AgentPrincipal("agent-web", "WebResearchAgent", "worker", TEAM, work_id="task-1")

        def ask() -> str:
            return requests.submit(
                requester=worker, person=ana, target=TargetRefV1(kind=TargetKind.TOOL, id="cli:x"),
                level=AccessLevel.WRITE, reason="Post the update", task_id="task-1",
            ).request.request_id

        failed_id = ask()

        def busy(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        # A decision that fails while storing its outcome stores none of its grants.
        monkeypatch.setattr(store, "finish_request", busy)
        with pytest.raises(HandDownRefused, match="couldn't be saved because the access store was busy"):
            requests.decide(failed_id, ana, AccessReviewChoice.APPROVE)
        monkeypatch.undo()
        assert [g for g in store.list_team(TEAM) if g.approval_id == failed_id] == []

        # An older version stored a lasting grant before its outcome and stopped in between.
        older_id = ask()
        store.create(_grant(
            "g_left", SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "TeamArchitect")),
            TargetRefV1(kind=TargetKind.TOOL, id="cli:x"), approval_id=older_id, for_person_id="usr_ana",
        ))
    finally:
        cedar.close()

    with caplog.at_level(logging.WARNING, logger="team_transfer.access"):
        plan, target = _export_and_import(tmp_path, config_path)

    keys = {r.key for r in plan.records if r.section == "access"}
    assert "grant/g_left" not in keys, "a grant whose request was not granted stays behind"
    assert not any(grant.approval_id in {failed_id, older_id} for grant in _target_store(target).list_team(TEAM))
    assert f"event=team_transfer_grant_without_outcome grant_id=g_left request_id={older_id}" in caplog.text


def test_a_later_batch_does_not_guess_what_the_destination_chose() -> None:
    # Only a fresh import knows the starting sharing applies; a later batch
    # without the team's choice for the tool says nothing rather than guess.
    from types import SimpleNamespace

    from access_catalog.catalog import CatalogSource
    from team_transfer.access import AccessKind, AccessRecordV1, _unused_team_link

    link = AccountLinkV1(
        link_id="link_x_team", team_id=TEAM, tool_id="cli:x", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM,
        credential_key="cli-x-team", created_by=OWNER, created_at_utc=NOW,
    )
    record = AccessRecordV1(kind=AccessKind.ACCOUNT_LINK, record_id=link.link_id, document=link.model_dump(mode="json"))
    known = SimpleNamespace(release_tools=CatalogSource(None).current().tools)

    assert "where this version starts it" in _unused_team_link(record, known, {}, fresh=True)
    assert _unused_team_link(record, known, {}, fresh=False) == ""
    assert "as the team chose" in _unused_team_link(record, known, {"cli:x": "private"}, fresh=False)
    assert _unused_team_link(record, known, {"cli:x": "shared"}, fresh=True) == ""
