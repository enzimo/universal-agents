"""Tests for folder target creation/apply enforcement, C01/C03/C04/C05/C07/C12.

Covers:
- S1: /access grant, CLI access grant, request_access, and propose_access_change
  grant items refuse existing file paths naming the folder (or /share for a
  person's file); folder grants and file-path blocks succeed; apply-time
  refusal rolls back cleanly when a target became a file before approval.
- S3 (creation): misspelled existing paths are refused with the disk's spelling
  across grants, blocks, shares, /send, requests, restrictions, and proposals;
  blocks on future paths succeed.
- C01: authority checks run before disk checks (zero existence/spelling oracle
  for users/<other>/ or unshared data folders).
- C03: startup reporting warns and notifies administrators once for stored
  file-path grants, misspelled grants, and misspelled blocks without deleting
  or rewriting them.
- C04: proposal submission does not probe inside newly proposed data folders;
  ProposalApplier enforces disk checks at apply time after approval.
- C05: production AccessService sites carry folder_roots, _service_with_catalog
  preserves it, and an unconfigured AccessService logs a structured warning.
- C07: approve_pending reports the folder/spelling refusal to an authorized
  approver while keeping the authority message for an unauthorized caller.
- C12: check_spelling_below consolidates workspace/data-folder spelling checks
  without leaking host folder basenames.
"""

from __future__ import annotations

import asyncio
import json
import logging
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import assistant_cli
from access_catalog.catalog import load_catalog
from access_control import folders
from access_control.account_sharing import TeamAccountSharing
from access_control.cedar import CedarAccessClient
from access_control.data_folders import DataFolderRegistry, protected_locations
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import (
    AGENT_ROLE_TEAM_LEAD,
    AGENT_ROLE_WORKER,
    AgentPrincipal,
    PersonPrincipal,
    agent_role_resolver,
    agent_type_id,
)
from access_control.folders import (
    FolderRoots,
    PathSpellingError,
    clear_spelling_cache,
)
from access_control.inbox import StoredAccessInbox
from access_control.operator_commands import open_access_service
from access_control.proposals import (
    ACCESS_PROPOSAL_KIND,
    ProposalApplier,
    ProposalCompiler,
    ProposalRefused,
    _service_with_catalog,
)
from access_control.requests import AccessRequests
from assistant_gateway.access_commands import GatewayAccessCommands
from assistant_gateway.access_inbox import GatewayAccessInboxSettings, GatewayAccessRequestsSource
from assistant_gateway.application.commands.access import AccessBlockInput, AccessGrantInput
from assistant_gateway.file_sharing import FileSharingRefused, GatewayFileSharing
from custom_agents import access_runtime
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeKind,
    AccessProposalV1,
    AccessRequestStatus,
    AccessReviewChoice,
    ChangeProposalV1,
    DelegationMode,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    ProposedBlockV1,
    ProposedDataFolderV1,
    ProposedGrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_tools.providers.access import request_access
from id_generation import new_id
from runtime_settings.instance_layout import InstanceLayout
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer, context_verify_keys
from tests.data_folder_support import store_data_folder
from tests.folder_spelling_support import SimulatedFS, install_simulated_fs
from tests.task_work_support import set_agent_ref
from access_control.identity import ENV_VERIFY_KEYS

TEAM = "team-creation-test"
ADMIN = PersonPrincipal("admin-1", TEAM, ("admin",))
ALICE = PersonPrincipal("alice", TEAM, ("member",))
BOB = PersonPrincipal("bob", TEAM, ("member",))
BINARY = Path(__file__).resolve().parents[1] / "target" / "debug" / "ua-cedar-authorizer"
pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    clear_spelling_cache()
    access_runtime._runtime_access.cache_clear()
    yield
    clear_spelling_cache()
    access_runtime._runtime_access.cache_clear()
    set_agent_ref(None)


def _setup_instance(tmp_path: Path) -> tuple[InstanceLayout, Any, Path]:
    layout = InstanceLayout.at(tmp_path / "inst")
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.workspaces_dir.mkdir(parents=True, exist_ok=True)
    (layout.workspaces_dir / "team" / "finance").mkdir(parents=True, exist_ok=True)
    (layout.workspaces_dir / "team" / "finance" / "payroll.csv").write_text("alice,100\n", encoding="utf-8")
    (layout.workspaces_dir / "users" / "alice" / "notes").mkdir(parents=True, exist_ok=True)
    (layout.workspaces_dir / "users" / "alice" / "notes" / "report.txt").write_text("mine\n", encoding="utf-8")
    (layout.workspaces_dir / "users" / "bob").mkdir(parents=True, exist_ok=True)
    (layout.workspaces_dir / "users" / "bob" / "secret.txt").write_text("bobs\n", encoding="utf-8")

    reports_dir = tmp_path / "external_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "q1.csv").write_text("rev,10\n", encoding="utf-8")

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(layout.root / "manager.db")}})
    store_data_folder(storage, "reports", reports_dir, writable=True, team_id=TEAM)
    return layout, storage, reports_dir


def _gateway_source(layout: InstanceLayout, storage: Any) -> GatewayAccessRequestsSource:
    settings = GatewayAccessInboxSettings(
        team_id=TEAM,
        instance_name="test-inst",
        response_route="resp",
        lead_agent_type="TeamArchitect",
        inbox_url="/webchat",
        instance_root=layout.root,
        storage_settings=storage,
        cedar_binary=str(BINARY),
        authorization_enabled=True,
        protected_locations=protected_locations(layout.root),
    )
    return GatewayAccessRequestsSource(settings)


def _people_lookup(person_id: str) -> PersonPrincipal | None:
    return {"admin-1": ADMIN, "alice": ALICE, "bob": BOB}.get(person_id)


def _make_agent(
    layout: InstanceLayout,
    storage: Any,
    *,
    person: PersonPrincipal = ADMIN,
    agent_type: str = "WebResearchAgent",
    is_team_lead: bool = False,
    task_id: str = "task-1",
) -> SimpleNamespace:
    context = context_signer().issue(team_id=TEAM, person_id=person.person_id, person_roles=person.roles)
    meta = SimpleNamespace(
        conversation_id="conv-1",
        gateway_context=SimpleNamespace(canonical_user_id=person.person_id, task_context=context),
    )
    task_manager = SimpleNamespace(
        current_task_id=task_id,
        get_task_info=lambda _tid: SimpleNamespace(meta=meta),
        storage_settings=storage,
    )
    return SimpleNamespace(
        id=f"agent-{agent_type}-{task_id}",
        name=agent_type,
        named_agent_type=agent_type,
        team_id=TEAM,
        is_team_lead=is_team_lead,
        access_role="team_lead" if is_team_lead else "worker",
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        authorization_config={"enabled": True, "cedar_binary_path": str(BINARY)},
        task_manager=task_manager,
        test_task_id=task_id,
    )


def test_gateway_access_grant_refuses_existing_file_and_allows_folder_and_block(tmp_path: Path) -> None:
    """S1: /access grant refuses file paths (even with a legacy grant), allows folders and file blocks."""

    layout, storage, _reports = _setup_instance(tmp_path)
    source = _gateway_source(layout, storage)
    try:
        commands = GatewayAccessCommands(
            requests=source.requests,
            person=_people_lookup,
            review_url=lambda _rid=None: "/webchat?view=inbox",
            authorization_enabled=True,
        )

        # Grant on existing file in team/ must be refused naming the containing folder.
        reply = asyncio.run(
            commands.grant(
                ADMIN.person_id,
                AccessGrantInput(to="agent-type:TeamArchitect", target="team/finance/payroll.csv", level="read"),
            )
        )
        assert reply.startswith("Not done:")
        assert "team/finance" in reply
        assert not source.requests().service.store.list_team(TEAM)

        # Seed a legacy file-path grant directly; /access grant must still refuse rather than say Already given.
        source.requests().service.store.create(
            GrantV1(
                grant_id=new_id("grant"),
                team_id=TEAM,
                kind=GrantKind.ACCESS,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "TeamArchitect")),
                target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/payroll.csv"),
                level=AccessLevel.READ,
                lifetime=GrantLifetime.UNTIL_REVOKED,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=ADMIN.person_id),
                for_person_id=ADMIN.person_id,
                created_at_utc=datetime.now(timezone.utc),
            )
        )
        legacy_reply = asyncio.run(
            commands.grant(
                ADMIN.person_id,
                AccessGrantInput(to="agent-type:TeamArchitect", target="team/finance/payroll.csv", level="read"),
            )
        )
        assert legacy_reply.startswith("Not done:")
        assert "Already given" not in legacy_reply
        assert "team/finance" in legacy_reply

        # Owner trying to create a FOLDER grant on their own file gets the /share message.
        owner_file_reply = asyncio.run(
            commands.grant(
                ALICE.person_id,
                AccessGrantInput(to="user:bob", target="users/alice/notes/report.txt", level="read"),
            )
        )
        assert owner_file_reply.startswith("Not done:")
        assert "/share" in owner_file_reply

        # Grant on the containing folder itself succeeds.
        folder_reply = asyncio.run(
            commands.grant(
                ADMIN.person_id,
                AccessGrantInput(to="agent-type:TeamArchitect", target="team/finance", level="read"),
            )
        )
        assert folder_reply.startswith("Done.")

        # Block on the file path stays allowed.
        block_reply = asyncio.run(
            commands.block(
                ADMIN.person_id,
                AccessBlockInput(
                    subject="agent-type:TeamArchitect",
                    target="team/finance/payroll.csv",
                    reason="Payroll stays restricted",
                ),
            )
        )
        assert block_reply.startswith("Blocked.")
    finally:
        source.close()


def test_operator_cli_access_grant_refuses_existing_file_and_allows_folder_and_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S1: operator CLI access grant refuses existing file paths, allows folders and file blocks."""

    layout, storage, _reports = _setup_instance(tmp_path)
    service = open_access_service(
        instance_root=layout.root,
        team_id=TEAM,
        storage_settings=storage,
        cedar_binary=str(BINARY),
        lead_agent_type="TeamArchitect",
    )
    try:
        monkeypatch.setattr(assistant_cli, "_acting_person", lambda _args, _cfg: ADMIN)
        file_args = Namespace(
            to="agent-type:TeamArchitect",
            tool=None,
            tool_set=None,
            folder="data/reports/q1.csv",
            level="read",
            duration=None,
            delegate_depth=0,
            delegate_mode="automatic",
            reason="read report",
            everyone=False,
            by=ADMIN.person_id,
        )
        with pytest.raises(HandDownRefused) as exc_info:
            assistant_cli._access_grant(file_args, SimpleNamespace(), service)
        assert "data/reports" in str(exc_info.value)
        assert not service.store.list_team(TEAM)

        folder_args = Namespace(**{**vars(file_args), "folder": "data/reports"})
        assistant_cli._access_grant(folder_args, SimpleNamespace(), service)
        assert "Created " in capsys.readouterr().out

        requests = AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type="TeamArchitect")
        block = requests.block(
            ADMIN,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "TeamArchitect")),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports/q1.csv"),
            reason="sensitive file",
        )
        assert block.kind is GrantKind.BLOCK
        assert block.target.id == "data/reports/q1.csv"
    finally:
        service.cedar.close()


def test_request_access_refuses_existing_file_and_apply_time_rollback(tmp_path: Path) -> None:
    """S1: request_access refuses existing files immediately and fails closed on apply if a path becomes a file."""

    layout, storage, reports_dir = _setup_instance(tmp_path)
    agent = _make_agent(layout, storage, person=ADMIN, agent_type="WebResearchAgent", task_id="task-req-1")
    set_agent_ref(agent)
    service = access_runtime.runtime_access(agent).service
    assert service is not None
    try:
        tool_ctx = SimpleNamespace(tool_use={"toolUseId": "tu-file-1"}, interrupt=lambda *_a, **_kw: None)
        res = json.loads(
            request_access(
                level="read",
                reason="need payroll",
                tool_context=tool_ctx,
                folder="team/finance/payroll.csv",
            )
        )
        assert res["status"] == "refused"
        assert "team/finance" in res["reason"]
        assert not service.store.list_requests(TEAM, status=AccessRequestStatus.WAITING)

        # Request on a path that does not exist yet enters WAITING.
        requests = access_runtime.runtime_requests(agent)
        worker = AgentPrincipal(agent.id, "WebResearchAgent", AGENT_ROLE_WORKER, TEAM, work_id="work-future-1")
        outcome = requests.submit(
            requester=worker,
            person=ADMIN,
            target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports/future_item"),
            level=AccessLevel.READ,
            reason="need future folder",
            task_id="task-req-1",
        )
        assert outcome.waiting and outcome.request is not None
        req_id = outcome.request.request_id

        # Before approval, the path becomes a file on disk.
        (reports_dir / "future_item").write_text("became a file\n", encoding="utf-8")
        with pytest.raises(HandDownRefused) as exc_info:
            requests.decide(req_id, ADMIN, AccessReviewChoice.APPROVE)
        assert "data/reports" in str(exc_info.value)
        # Nothing stored; request remains WAITING.
        assert not service.store.list_team(TEAM)
        stored_req = service.store.get_request(req_id)
        assert stored_req is not None and stored_req.status is AccessRequestStatus.WAITING
    finally:
        service.cedar.close()
        access_runtime._runtime_access.cache_clear()


def test_propose_access_change_refuses_existing_file_c04_no_oracle_and_apply_rollback(tmp_path: Path) -> None:
    """S1 & C04: ProposalCompiler refuses existing file grants, skips newly proposed data folders at submit, and ProposalApplier refuses/rolls back at apply."""

    layout, storage, reports_dir = _setup_instance(tmp_path)
    source = _gateway_source(layout, storage)
    try:
        requests = source.requests()
        service = requests.service
        applier = ProposalApplier(
            requests=source.requests,
            instance_root=layout.root,
            catalogs=source.catalogs,
            data_folders=source.data_folders,
            protected=protected_locations(layout.root),
            person=_people_lookup,
        )
        source.appliers[ACCESS_PROPOSAL_KIND] = applier
        compiler = ProposalCompiler(
            catalog=service.catalog,
            team_id=TEAM,
            holds=lambda person, target, level: service.holds(person, target, level, task_id=None),
            account_sharing=TeamAccountSharing(service.store, TEAM),
            data_folders=source.data_folders(),
            protected=protected_locations(layout.root),
            grant_exists=service.store.get,
            instance_root=layout.root,
        )

        # Grant on existing file is refused at submission.
        file_proposal = AccessProposalV1(
            title="Read payroll",
            reason="Need payroll file",
            grants=(
                ProposedGrantV1(
                    agent_type="TeamArchitect",
                    target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/payroll.csv"),
                    level=AccessLevel.READ,
                    reason="Read payroll",
                ),
            ),
        )
        with pytest.raises(ProposalRefused) as exc_info:
            compiler.compile(file_proposal, person=ADMIN, task_id="work-p1")
        assert "team/finance" in str(exc_info.value)

        # Block on existing file is allowed in a proposal.
        block_proposal = AccessProposalV1(
            title="Block payroll",
            reason="Keep payroll private",
            blocks=(
                ProposedBlockV1(
                    agent_type="WebResearchAgent",
                    target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/payroll.csv"),
                    reason="No payroll",
                ),
            ),
        )
        compiled_block = compiler.compile(block_proposal, person=ADMIN, task_id="work-p2")
        assert len(compiled_block.rows) == 1

        # C04: newly proposed data folder is NOT probed at submission (identical for existing file vs missing path).
        proposed_ext = tmp_path / "proposed_ext"
        proposed_ext.mkdir()
        (proposed_ext / "secret.csv").write_text("top-secret\n", encoding="utf-8")

        def _make_new_folder_proposal(target_path: str) -> AccessProposalV1:
            return AccessProposalV1(
                title="Register ext and grant",
                reason="Use external data",
                data_folders=(ProposedDataFolderV1(name="newext", path=str(proposed_ext), writable=False),),
                grants=(
                    ProposedGrantV1(
                        agent_type="TeamArchitect",
                        target=TargetRefV1(kind=TargetKind.FOLDER, id=target_path),
                        level=AccessLevel.READ,
                        reason="Read data",
                    ),
                ),
            )

        compiled_existing = compiler.compile(_make_new_folder_proposal("data/newext/secret.csv"), person=ADMIN, task_id="work-c04")
        compiled_missing = compiler.compile(_make_new_folder_proposal("data/newext/missing.csv"), person=ADMIN, task_id="work-c04")
        assert len(compiled_existing.rows) == len(compiled_missing.rows)

        # At apply time (after approval), ProposalApplier refuses the file grant inside the newly proposed data folder and stores nothing.
        change_id = new_id("proposal")
        service.store.save_plan(
            TEAM,
            change_id,
            ACCESS_PROPOSAL_KIND,
            compiled_existing.digest,
            compiled_existing.proposal.model_dump(mode="json"),
            at=service.clock(),
        )
        lead = AgentPrincipal("lead-1", "TeamArchitect", AGENT_ROLE_TEAM_LEAD, TEAM, work_id="work-c04")
        req = source.requests().submit_change(
            requester=lead,
            person=ADMIN,
            proposal=ChangeProposalV1(
                kind=ACCESS_PROPOSAL_KIND,
                change_id=change_id,
                plan_digest=compiled_existing.digest,
                title="Register ext and grant",
                summary="Access proposal: register 1 folder, give 1 access.",
                effect=tuple(r.text for r in compiled_existing.rows),
                items=compiled_existing.rows,
            ),
            reason="Use external data",
            task_id="task-c04",
        ).request
        assert req is not None
        with pytest.raises(HandDownRefused) as apply_exc:
            source.requests().decide(req.request_id, ADMIN, AccessReviewChoice.APPROVE_ALL)
        assert "data/newext" in str(apply_exc.value)
        # Neither the data folder nor the grant was stored.
        assert all(rec.name != "newext" for rec in source.data_folders().records())
        assert not service.store.list_team(TEAM)

        # Future path in existing registered data folder becomes a file before apply -> refused & nothing stored.
        future_proposal = AccessProposalV1(
            title="Future folder",
            reason="Prepare access",
            grants=(
                ProposedGrantV1(
                    agent_type="TeamArchitect",
                    target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports/soon_file"),
                    level=AccessLevel.READ,
                    reason="Read future folder",
                ),
            ),
        )
        compiled_future = compiler.compile(future_proposal, person=ADMIN, task_id="work-fut")
        fut_change_id = new_id("proposal")
        service.store.save_plan(
            TEAM,
            fut_change_id,
            ACCESS_PROPOSAL_KIND,
            compiled_future.digest,
            compiled_future.proposal.model_dump(mode="json"),
            at=service.clock(),
        )
        fut_req = source.requests().submit_change(
            requester=lead,
            person=ADMIN,
            proposal=ChangeProposalV1(
                kind=ACCESS_PROPOSAL_KIND,
                change_id=fut_change_id,
                plan_digest=compiled_future.digest,
                title="Future folder",
                summary="Access proposal: give 1 access.",
                effect=tuple(r.text for r in compiled_future.rows),
                items=compiled_future.rows,
            ),
            reason="Prepare access",
            task_id="task-fut",
        ).request
        assert fut_req is not None
        (reports_dir / "soon_file").write_text("file appeared\n", encoding="utf-8")
        with pytest.raises(HandDownRefused) as fut_exc:
            source.requests().decide(fut_req.request_id, ADMIN, AccessReviewChoice.APPROVE_ALL)
        assert "data/reports" in str(fut_exc.value)
        assert not service.store.list_team(TEAM)
    finally:
        source.close()


def test_misspelled_grant_block_and_share_are_refused_with_disk_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3: misspelled existing targets are refused with the disk spelling across grant, block, share, send, request, restrict, and proposal."""

    layout, storage, _reports = _setup_instance(tmp_path)
    sim = SimulatedFS(case_insensitive=True, unicode_normalizing=True)
    install_simulated_fs(monkeypatch, sim, patch_safe_open=True)

    source = _gateway_source(layout, storage)
    try:
        commands = GatewayAccessCommands(
            requests=source.requests,
            person=_people_lookup,
            review_url=lambda _rid=None: "/webchat?view=inbox",
            authorization_enabled=True,
        )

        # Misspelled grant on team/Finance -> refused naming team/finance.
        sim.stat_calls.clear()
        grant_reply = asyncio.run(
            commands.grant(
                ADMIN.person_id,
                AccessGrantInput(to="agent-type:TeamArchitect", target="team/Finance", level="read"),
            )
        )
        assert grant_reply.startswith("Not done:")
        assert "team/finance" in grant_reply
        assert sim.stat_calls

        # Misspelled block on team/finance/PAYROLL.csv -> refused naming team/finance/payroll.csv.
        sim.stat_calls.clear()
        block_reply = asyncio.run(
            commands.block(
                ADMIN.person_id,
                AccessBlockInput(
                    subject="agent-type:TeamArchitect",
                    target="team/finance/PAYROLL.csv",
                    reason="Restrict payroll",
                ),
            )
        )
        assert block_reply.startswith("Not done:")
        assert "team/finance/payroll.csv" in block_reply
        assert sim.stat_calls

        # Block on future (non-existent) path inside correctly spelled folder succeeds.
        future_block = asyncio.run(
            commands.block(
                ADMIN.person_id,
                AccessBlockInput(
                    subject="agent-type:TeamArchitect",
                    target="team/finance/Future.csv",
                    reason="Pre-block future file",
                ),
            )
        )
        assert future_block.startswith("Blocked.")

        # Misspelled request_access -> refused naming team/finance.
        requests = source.requests()
        worker = AgentPrincipal("w-1", "WebResearchAgent", AGENT_ROLE_WORKER, TEAM, work_id="work-spell-1")
        with pytest.raises(HandDownRefused) as req_exc:
            requests.submit(
                requester=worker,
                person=ADMIN,
                target=TargetRefV1(kind=TargetKind.FOLDER, id="team/Finance"),
                level=AccessLevel.READ,
                reason="Need finance",
                task_id="t-spell",
            )
        assert "team/finance" in str(req_exc.value)

        # Misspelled restrict -> refused naming team/finance/payroll.csv.
        lead = AgentPrincipal("lead-1", "TeamArchitect", AGENT_ROLE_TEAM_LEAD, TEAM, work_id="work-spell-1")
        requests.service.grant_by_person(
            ADMIN,
            GrantV1(
                grant_id="new",
                team_id=TEAM,
                subject=lead.type_subject(),
                target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance"),
                level=AccessLevel.READ,
                lifetime=GrantLifetime.UNTIL_REVOKED,
                delegation_depth=1,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=ADMIN.person_id),
                for_person_id=ADMIN.person_id,
                created_at_utc=requests.service.clock(),
            ),
        )
        with pytest.raises(HandDownRefused) as restrict_exc:
            requests.restrict(
                lead,
                recipient_agent_type="WebResearchAgent",
                target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/PAYROLL.csv"),
                reason="Keep out",
                task_id="work-spell-1",
                person_id=ADMIN.person_id,
            )
        assert "team/finance/payroll.csv" in str(restrict_exc.value)

        # Misspelled share -> refused naming users/alice/notes/report.txt; exact FILE share succeeds.
        sharing = GatewayFileSharing(
            requests=source.requests,
            person=_people_lookup,
            workspaces_dir=layout.workspaces_dir,
            team_people=lambda: [("admin-1", "Admin"), ("alice", "Alice"), ("bob", "Bob")],
            display_name=lambda pid: pid,
            authorization_enabled=True,
        )
        with pytest.raises(FileSharingRefused) as share_exc:
            sharing.share("alice", "Notes/Report.txt", "bob", "read")
        assert "users/alice/notes/report.txt" in str(share_exc.value)

        share_grant = sharing.share("alice", "notes/report.txt", "bob", "read")
        assert share_grant.target.kind is TargetKind.FILE
        assert share_grant.target.id == "users/alice/notes/report.txt"

        # Misspelled /send to team/Finance -> refused naming team/finance.
        with pytest.raises(FileSharingRefused) as send_exc:
            sharing.send("alice", "notes/report.txt", "team/Finance")
        assert "team/finance" in str(send_exc.value)

        # Misspelled proposal grant and misspelled proposal block -> refused with disk spelling.
        compiler = ProposalCompiler(
            catalog=requests.service.catalog,
            team_id=TEAM,
            holds=lambda person, target, level: requests.service.holds(person, target, level, task_id=None),
            account_sharing=TeamAccountSharing(requests.service.store, TEAM),
            data_folders=source.data_folders(),
            protected=protected_locations(layout.root),
            grant_exists=requests.service.store.get,
            instance_root=layout.root,
        )
        with pytest.raises(ProposalRefused) as prop_grant_exc:
            compiler.compile(
                AccessProposalV1(
                    title="Misspelled grant",
                    reason="Read finance",
                    grants=(
                        ProposedGrantV1(
                            agent_type="TeamArchitect",
                            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/Finance"),
                            level=AccessLevel.READ,
                            reason="Read finance",
                        ),
                    ),
                ),
                person=ADMIN,
                task_id="work-spell-prop",
            )
        assert "team/finance" in str(prop_grant_exc.value)
        with pytest.raises(ProposalRefused) as prop_block_exc:
            compiler.compile(
                AccessProposalV1(
                    title="Misspelled block",
                    reason="Block payroll",
                    blocks=(
                        ProposedBlockV1(
                            agent_type="WebResearchAgent",
                            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/PAYROLL.csv"),
                            reason="Block payroll",
                        ),
                    ),
                ),
                person=ADMIN,
                task_id="work-spell-prop",
            )
        assert "team/finance/payroll.csv" in str(prop_block_exc.value)
    finally:
        source.close()


def test_unauthorized_callers_get_identical_refusal_regardless_of_disk_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C01: authority checks run before disk checks so unauthorized callers get no existence or spelling oracle."""

    layout, storage, _reports = _setup_instance(tmp_path)
    sim = SimulatedFS(case_insensitive=True, unicode_normalizing=True)
    install_simulated_fs(monkeypatch, sim, patch_safe_open=True)

    source = _gateway_source(layout, storage)
    try:
        commands = GatewayAccessCommands(
            requests=source.requests,
            person=_people_lookup,
            review_url=lambda _rid=None: "/webchat?view=inbox",
            authorization_enabled=True,
        )

        # Non-owner Alice probing Bob's private folder via /access grant gets identical authority refusal, 0 disk stats.
        sim.stat_calls.clear()
        r_exist = asyncio.run(
            commands.grant(ALICE.person_id, AccessGrantInput(to="user:alice", target="users/bob/secret.txt", level="read"))
        )
        r_miss = asyncio.run(
            commands.grant(ALICE.person_id, AccessGrantInput(to="user:alice", target="users/bob/missing.txt", level="read"))
        )
        r_case = asyncio.run(
            commands.grant(ALICE.person_id, AccessGrantInput(to="user:alice", target="users/bob/SECRET.txt", level="read"))
        )
        assert "beyond your own authority" in r_exist
        assert "beyond your own authority" in r_miss
        assert "beyond your own authority" in r_case
        assert sim.stat_calls == []

        # Non-admin Alice probing unshared data/reports via /access grant and /access block: 0 disk stats.
        sim.stat_calls.clear()
        d_exist = asyncio.run(
            commands.grant(ALICE.person_id, AccessGrantInput(to="agent-type:TeamArchitect", target="data/reports/q1.csv", level="read"))
        )
        d_miss = asyncio.run(
            commands.grant(ALICE.person_id, AccessGrantInput(to="agent-type:TeamArchitect", target="data/reports/missing.csv", level="read"))
        )
        b_exist = asyncio.run(
            commands.block(ALICE.person_id, AccessBlockInput(subject="agent-type:TeamArchitect", target="data/reports/Q1.csv", reason="probe"))
        )
        assert "beyond your own authority" in d_exist
        assert "beyond your own authority" in d_miss
        assert "cannot give access" in b_exist
        assert sim.stat_calls == []

        # Agent calling request_access (submit) on users/bob/... gets identical person-folder refusal, 0 disk stats.
        requests = source.requests()
        worker = AgentPrincipal("w-1", "WebResearchAgent", AGENT_ROLE_WORKER, TEAM, work_id="work-oracle-1")
        sim.stat_calls.clear()
        messages = []
        for probe in ("users/bob/secret.txt", "users/bob/missing.txt", "users/bob/SECRET.txt"):
            with pytest.raises(HandDownRefused) as exc_info:
                requests.submit(
                    requester=worker,
                    person=ALICE,
                    target=TargetRefV1(kind=TargetKind.FOLDER, id=probe),
                    level=AccessLevel.READ,
                    reason="probe",
                    task_id="t-oracle",
                )
            messages.append(str(exc_info.value))
        assert len(set(messages)) == 1
        assert "share it with a person instead" in messages[0]
        assert sim.stat_calls == []
    finally:
        source.close()


def test_approve_pending_reports_file_refusal_to_approver_and_authority_error_to_outsider(tmp_path: Path) -> None:
    """C07: approve_pending keeps the authority message for outsiders and surfaces the file/spelling refusal to approvers."""

    layout, storage, reports_dir = _setup_instance(tmp_path)
    source = _gateway_source(layout, storage)
    try:
        service = source.requests().service
        lead = AgentPrincipal("lead-1", "TeamArchitect", AGENT_ROLE_TEAM_LEAD, TEAM, work_id="work-pend-1")
        service.grant_by_person(
            ADMIN,
            GrantV1(
                grant_id="new",
                team_id=TEAM,
                subject=lead.type_subject(),
                target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports"),
                level=AccessLevel.READ,
                lifetime=GrantLifetime.UNTIL_REVOKED,
                delegation_depth=1,
                delegation_mode=DelegationMode.ASK_OWNER,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=ADMIN.person_id),
                for_person_id=ADMIN.person_id,
                created_at_utc=service.clock(),
            ),
        )
        pending = service.hand_down(
            lead,
            GrantV1(
                grant_id="new",
                team_id=TEAM,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "WebResearchAgent")),
                target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports/pending_target"),
                level=AccessLevel.READ,
                lifetime=GrantLifetime.TASK,
                task_id="work-pend-1",
                granted_by=lead.type_subject(),
                created_at_utc=service.clock(),
            ),
            task_id="work-pend-1",
            person_id=ADMIN.person_id,
        )
        assert pending.status is GrantStatus.PENDING

        # Path becomes a file before approval.
        (reports_dir / "pending_target").write_text("file now\n", encoding="utf-8")

        # Outsider still gets the authority message.
        with pytest.raises(HandDownRefused) as outsider_exc:
            service.approve_pending(BOB, pending.grant_id)
        assert "Only the target's approver can approve this hand-down." in str(outsider_exc.value)

        # Authorized approver gets the actual file-path refusal naming data/reports.
        with pytest.raises(HandDownRefused) as approver_exc:
            service.approve_pending(ADMIN, pending.grant_id)
        assert "data/reports" in str(approver_exc.value)
        assert "Only the target's approver" not in str(approver_exc.value)
    finally:
        source.close()


def test_startup_report_warns_and_notifies_admins_for_legacy_file_grants_and_misspelled_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C03: startup reporting warns and notifies administrators once for file grants, misspelled grants, and misspelled blocks without altering stored rows."""

    layout, storage, reports_dir = _setup_instance(tmp_path)
    sim = SimulatedFS(case_insensitive=True, unicode_normalizing=True)
    install_simulated_fs(monkeypatch, sim, patch_safe_open=True)

    store = AccessRepository(storage)
    now = datetime.now(timezone.utc)
    lead_sub = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "TeamArchitect"))
    by_admin = SubjectRefV1(kind=SubjectKind.USER, id=ADMIN.person_id)

    g_file = store.create(
        GrantV1(
            grant_id="grant-legacy-file",
            team_id=TEAM,
            kind=GrantKind.ACCESS,
            subject=lead_sub,
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/payroll.csv"),
            level=AccessLevel.READ,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=by_admin,
            created_at_utc=now,
        )
    )
    g_misspelled = store.create(
        GrantV1(
            grant_id="grant-legacy-case",
            team_id=TEAM,
            kind=GrantKind.ACCESS,
            subject=lead_sub,
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/Finance"),
            level=AccessLevel.READ,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=by_admin,
            created_at_utc=now,
        )
    )
    b_misspelled = store.create(
        GrantV1(
            grant_id="block-legacy-case",
            team_id=TEAM,
            kind=GrantKind.BLOCK,
            subject=lead_sub,
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/PAYROLL.csv"),
            level=AccessLevel.FULL,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=by_admin,
            reason="block payroll",
            created_at_utc=now,
        )
    )
    # Valid grant and valid file block must not be reported.
    store.create(
        GrantV1(
            grant_id="grant-valid-folder",
            team_id=TEAM,
            kind=GrantKind.ACCESS,
            subject=lead_sub,
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance"),
            level=AccessLevel.READ,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=by_admin,
            created_at_utc=now,
        )
    )
    store.create(
        GrantV1(
            grant_id="block-valid-file",
            team_id=TEAM,
            kind=GrantKind.BLOCK,
            subject=lead_sub,
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/payroll.csv"),
            level=AccessLevel.FULL,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=by_admin,
            reason="valid file block",
            created_at_utc=now,
        )
    )
    # Valid single-file FILE-kind share must not be reported.
    store.create(
        GrantV1(
            grant_id="share-valid-file",
            team_id=TEAM,
            kind=GrantKind.ACCESS,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=BOB.person_id),
            target=TargetRefV1(kind=TargetKind.FILE, id="users/alice/notes/report.txt"),
            level=AccessLevel.READ,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=ALICE.person_id),
            created_at_utc=now,
        )
    )

    roots = FolderRoots.for_layout(layout, DataFolderRegistry(store, TEAM).usable(protected_locations(layout.root)))
    with caplog.at_level(logging.WARNING):
        reported = folders.report_folder_target_problems(roots, store, TEAM, now=now)

    assert set(reported) == {g_file.grant_id, g_misspelled.grant_id, b_misspelled.grant_id}
    assert "event=ineffective_folder_grant_found" in caplog.text
    assert "event=misspelled_folder_block_found" in caplog.text

    # Notices created for admins with deterministic ids; second run creates 0 new notices.
    page = store.notice_page(TEAM, person_id=ADMIN.person_id, roles=("admin",), unread_only=False, offset=0, limit=20)
    problem_notices = [n for n, _read in page.notices if n.kind is AccessNoticeKind.FOLDER_TARGET_PROBLEM]
    assert len(problem_notices) == 3
    assert all(n.recipient_roles == ("admin",) for n in problem_notices)
    assert folders.report_folder_target_problems(roots, store, TEAM, now=now) == []

    # Stored grants and blocks were NOT deleted or rewritten.
    assert store.get(g_file.grant_id) is not None and store.get(g_file.grant_id).target.id == "team/finance/payroll.csv"
    assert store.get(g_misspelled.grant_id) is not None and store.get(g_misspelled.grant_id).target.id == "team/Finance"
    assert store.get(b_misspelled.grant_id) is not None and store.get(b_misspelled.grant_id).target.id == "team/finance/PAYROLL.csv"


def test_production_services_wire_folder_roots_and_unconfigured_service_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """C05: CLI, gateway, agent runtime, and _service_with_catalog wire folder_roots; None logs a warning."""

    layout, storage, _reports = _setup_instance(tmp_path)
    cli_service = open_access_service(
        instance_root=layout.root,
        team_id=TEAM,
        storage_settings=storage,
        cedar_binary=str(BINARY),
        lead_agent_type="TeamArchitect",
    )
    gw_source = _gateway_source(layout, storage)
    agent = _make_agent(layout, storage)
    rt_service = access_runtime.runtime_access(agent).service
    bare = AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(layout.root),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver("TeamArchitect"),
        lead_agent_type="TeamArchitect",
    )
    try:
        assert cli_service.current_folder_roots() is not None
        assert gw_source.requests().service.current_folder_roots() is not None
        assert rt_service is not None and rt_service.current_folder_roots() is not None
        copied = _service_with_catalog(cli_service, cli_service.catalog)
        assert copied.current_folder_roots() is not None

        with caplog.at_level(logging.WARNING):
            assert bare.folder_target_refusal(TargetRefV1(kind=TargetKind.FOLDER, id="team/finance")) is None
        assert "event=folder_target_check_skipped" in caplog.text
    finally:
        cli_service.cedar.close()
        gw_source.close()
        if rt_service is not None:
            rt_service.cedar.close()
        access_runtime._runtime_access.cache_clear()
        bare.cedar.close()


def test_check_spelling_below_anchors_under_workspaces_and_uses_explicit_prefix_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C12: check_spelling_below anchors at workspaces/ when under it and never leaks host root names outside."""

    layout, _storage, reports_dir = _setup_instance(tmp_path)
    sim = SimulatedFS(case_insensitive=True, unicode_normalizing=True)
    install_simulated_fs(monkeypatch, sim)

    with pytest.raises(PathSpellingError) as ws_exc:
        folders.check_spelling_below(layout.workspaces_dir, layout.workspaces_dir / "team", ("Finance", "payroll.csv"))
    assert ws_exc.value.disk == "team/finance/payroll.csv"

    with pytest.raises(PathSpellingError) as data_exc:
        folders.check_spelling_below(layout.workspaces_dir, reports_dir, ("Q1.csv",), display_prefix="data/reports")
    assert data_exc.value.disk == "data/reports/q1.csv"
    assert "external_reports" not in str(data_exc.value)

    with pytest.raises(PathSpellingError) as bare_exc:
        folders.check_spelling_below(layout.workspaces_dir, reports_dir, ("Q1.csv",))
    assert bare_exc.value.disk == "q1.csv"
    assert "external_reports" not in str(bare_exc.value)


def test_gateway_startup_reports_folder_target_problems_and_logs_failures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """C03: assistant_gateway.gateway._report_folder_target_problems runs at startup and catches errors."""

    from assistant_gateway import gateway as gateway_module

    layout, storage, _reports = _setup_instance(tmp_path)
    store = AccessRepository(storage)
    legacy = store.create(
        GrantV1(
            grant_id="grant-gw-startup-file",
            team_id=TEAM,
            kind=GrantKind.ACCESS,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "TeamArchitect")),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/payroll.csv"),
            level=AccessLevel.READ,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=ADMIN.person_id),
            created_at_utc=datetime.now(timezone.utc),
        )
    )
    roots = FolderRoots.for_layout(layout, DataFolderRegistry(store, TEAM).usable(protected_locations(layout.root)))

    class _Inbox:
        def __init__(self, repo: Any) -> None:
            self._repo = repo

        def folder_roots(self) -> FolderRoots:
            return roots

        def access_store(self) -> Any:
            return self._repo

    asyncio.run(gateway_module._report_folder_target_problems(_Inbox(store), TEAM))  # type: ignore[arg-type]
    page = store.notice_page(TEAM, person_id=ADMIN.person_id, roles=("admin",), unread_only=False, offset=0, limit=10)
    assert any(n.grant_id == legacy.grant_id and n.kind is AccessNoticeKind.FOLDER_TARGET_PROBLEM for n, _r in page.notices)

    with caplog.at_level(logging.ERROR, logger="assistant_gateway.gateway"):
        asyncio.run(gateway_module._report_folder_target_problems(_Inbox(None), TEAM))  # type: ignore[arg-type]
    assert any("event=folder_target_problem_check_failed" in rec.getMessage() for rec in caplog.records)
