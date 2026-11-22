"""Access proposals end to end (Phase 6): from "no Portainer tool" to BOA restarting a stack.

TeamArchitect proposes a Portainer web API tool, access for itself (which it
may hand on) and for BackgroundOperationsAgent, and a team account link, with
the real ``propose_access_change`` tool; its task pauses. An administrator
approves once, keeping BOA's access for later tasks; trusted code applies the
whole proposal in one go. The broker's access gate (real Cedar, SQLite and
catalog reloading) then lets BOA restart a stack once the credential is
linked, and still refuses deleting (full access) and other hosts. A failure
while storing leaves no tool file and no grants.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from strands.interrupt import InterruptException

from access_catalog.catalog import CatalogSource
from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller
from access_control.data_folders import DataFolderRegistry
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import agent_role_resolver
from access_control.identity import ContextVerifier
from access_control.proposals import ACCESS_PROPOSAL_KIND, ProposalApplier
from access_control.requests import AccessRequests
from custom_agents import access_request_runtime, access_runtime
from custom_messages.contracts.access_v2 import (
    AccessRequestStatus,
    AccessReviewChoice,
    AccountLinkV1,
    CredentialInjectionV1,
    GrantLifetime,
    InjectionPlacement,
    LinkOwnerKind,
    LinkPurpose,
    REASON_ACCOUNT_LINK_OUTDATED,
    REASON_GATE_LINK_MISSING,
    SubjectKind,
    SubjectRefV1,
)
from custom_tools.providers import access as access_tools
from custom_tools.providers.access_proposals import get_proposal_status, propose_access_change
from tests.task_work_support import set_agent_ref
from secure_tool_broker.access_gate import REASON_ACCOUNT_LINK_MISSING, REASON_UNLISTED_HOST, BrokerAccessGate
from storage.access_repository import AccessRepository
from tests.access_decision_support import uncertain_store
from tests.broker_access_support import agent_context, context_verify_keys
from tests.test_access_request_tools import BINARY, OWNER, TEAM, Context, make_agent

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

PORTAINER = {
    "tool_id": "http:portainer",
    "display_name": "Portainer",
    "description": "Manages the Control Center stack.",
    "hosts": ["portainer.example.test"],
    "routes": [{"name": "stacks.restart", "method": "POST", "path": "/api/stacks/{id}/restart", "level": "write"}],
    "credential_injections": [{"placement": "header", "name": "X-API-Key", "field": "api_token"}],
}
RESTART = "https://portainer.example.test/api/stacks/1/restart"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    instance = tmp_path / "instance"
    (instance / "control").mkdir(parents=True)
    lead = make_agent(None, tmp_path, agent_type="TeamArchitect", lead=True)
    lead.instance_root = str(instance)
    storage = lead.task_manager.storage_settings
    catalogs = CatalogSource(instance)
    from access_control.cedar import CedarAccessClient

    service = AccessService(
        store=AccessRepository(storage),
        catalog=catalogs.current(),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver("TeamArchitect"),
    )
    people = {OWNER.person_id: OWNER}
    holder: dict = {}

    def requests() -> AccessRequests:
        service.catalog = catalogs.current()
        return AccessRequests(
            service=service,
            inbox=access_runtime.StoredAccessInbox(service.store),
            lead_agent_type="TeamArchitect",
            appliers={ACCESS_PROPOSAL_KIND: holder["applier"]},
        )

    holder["applier"] = ProposalApplier(
        requests=requests,
        instance_root=instance,
        catalogs=catalogs,
        data_folders=lambda: DataFolderRegistry(service.store, TEAM),
        protected=(instance,),
        person=people.get,
    )
    gate = BrokerAccessGate(
        catalogs=catalogs,
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEAM,
        access=service,
        links=service.store,
    )
    yield lead, requests, gate, instance, service
    service.cedar.close()
    access_runtime._runtime_access.cache_clear()
    set_agent_ref(None)


@pytest.mark.parametrize("committed", [False, True])
def test_an_unconfirmed_proposal_is_reconciled_after_reopening_the_store(world, monkeypatch, committed):
    from datetime import timedelta

    from access_control.requests import DecisionOutcomeUnknown

    lead, requests, _gate, instance, service = world
    set_agent_ref(lead)
    context = Context("tooluse-uncertain-proposal")
    with pytest.raises(InterruptException):
        propose(context)
    request_id = context.raised[0]["access_request_id"]
    with uncertain_store(monkeypatch, service.store, "apply_together", committed=committed):
        with pytest.raises(DecisionOutcomeUnknown, match="could not yet confirm"):
            requests().decide(request_id, OWNER, AccessReviewChoice.APPROVE)

    tool = instance / "control" / "access" / "tools" / "proposed-http-portainer.yaml"
    assert tool.exists()
    service.store = AccessRepository(service.store.settings)
    later = service.clock() + timedelta(minutes=11)
    assert len(service.store.decision_recoveries(TEAM, now=later)) == 1
    monkeypatch.setattr(service, "clock", lambda: later)
    requests().recover_decisions()
    assert service.store.decision_recoveries(TEAM, now=later) == []
    assert tool.exists() is committed
    stored = service.store.get_request(request_id)
    if committed:
        assert stored.status is AccessRequestStatus.GRANTED
        assert all(service.store.get(key).status.value == "active" for key in stored.grant_ids)
    else:
        assert stored.status is AccessRequestStatus.WAITING
        assert requests().decide(request_id, OWNER, AccessReviewChoice.APPROVE).status is AccessRequestStatus.GRANTED


def test_failed_proposal_cleanup_keeps_its_record_until_a_retry_succeeds(world, monkeypatch):
    from datetime import timedelta

    lead, requests, _gate, instance, service = world
    set_agent_ref(lead)
    context = Context("tooluse-proposal-cleanup-retry")
    with pytest.raises(InterruptException):
        propose(context)
    request_id = context.raised[0]["access_request_id"]
    with uncertain_store(monkeypatch, service.store, "apply_together"):
        with pytest.raises(HandDownRefused):
            requests().decide(request_id, OWNER, AccessReviewChoice.APPROVE)
    later = service.clock() + timedelta(minutes=11)
    monkeypatch.setattr(service, "clock", lambda: later)

    def cannot_remove(_path):
        raise PermissionError("read-only filesystem")

    tool = instance / "control" / "access" / "tools" / "proposed-http-portainer.yaml"
    with monkeypatch.context() as failure:
        failure.setattr("access_control.proposals.remove_instance_tool", cannot_remove)
        requests().recover_decisions()
    assert tool.exists() and len(service.store.decision_recoveries(TEAM)) == 1
    requests().recover_decisions()
    assert not tool.exists() and service.store.decision_recoveries(TEAM) == []


def boa(gate: BrokerAccessGate, method: str, url: str):
    return gate.check_broker_action(
        BrokerCaller(CALLER_MANAGER),
        "secure_requests",
        method,
        {"url": url},
        agent_context(
            OWNER.person_id,
            agent_id="agent-boa",
            named_agent_type="BackgroundOperationsAgent",
            roles=("owner",),
            task_id="task-boa",
        ),
    )


def propose(context: Context, **extra) -> dict:
    return json.loads(
        asyncio.run(
            propose_access_change(
                title="Restart the Control Center stack from Portainer",
                reason="BOA must restart the Control Center stack when its health check fails.",
                tool_context=context,
                tools=[PORTAINER],
                # Workers keep no lasting access: TeamArchitect keeps it and hands it
                # on to BackgroundOperationsAgent's tasks ("access follows the work").
                grants=[
                    {"agent_type": "TeamArchitect", "target": {"kind": "tool", "id": "http:portainer"},
                     "level": "write", "delegation_depth": 1, "reason": "BOA restarts stacks"},
                ],
                account_links=[{"tool_id": "http:portainer", "owner": "team", "purpose": "Restart the stack"}],
                **extra,
            )
        )
    )


def test_teamlead_goes_from_no_portainer_tool_to_boa_restarting_a_stack(world) -> None:
    lead, requests, gate, instance, service = world
    assert boa(gate, "post", RESTART).reason_code == REASON_UNLISTED_HOST
    set_agent_ref(lead)
    context = Context("tooluse-propose")

    with pytest.raises(InterruptException):
        propose(context)
    [paused] = context.raised
    request = service.store.get_request(paused["access_request_id"])
    rows = {row.item_id: row for row in request.change.items}
    assert request.status is AccessRequestStatus.WAITING and request.change.kind == ACCESS_PROPOSAL_KIND
    assert "new_host" in rows["tool-0"].flags and "can_hand_on" in rows["grant-0"].flags
    assert rows["grant-0"].keepable and "credential Portainer uses" in rows["account-0"].text
    assert not (instance / "control" / "access" / "tools").exists(), "nothing is applied before the review"

    # One review: the administrator keeps TeamArchitect's access for later tasks.
    decided = requests().decide(
        request.request_id, OWNER, AccessReviewChoice.APPROVE, kept_item_ids=("grant-0",)
    )
    assert decided.applied_at_utc is not None and len(decided.grant_ids) == 1
    [grant] = [service.store.get(grant_id) for grant_id in decided.grant_ids]
    assert grant.subject.id.split("/")[-1] == "TeamArchitect" and grant.lifetime is GrantLifetime.UNTIL_REVOKED
    assert grant.delegation_depth == 1 and grant.for_person_id == OWNER.person_id
    assert holder_links(world) == ["http:portainer"], "the gateway opens this form for the approver"

    # The resumed tool call reports what was applied.
    context.responses[access_tools.ACCESS_REQUEST_INTERRUPT_NAME] = access_request_runtime.decision_response(
        service.store.get_request(request.request_id)
    )
    result = propose(context)
    assert result["status"] == "applied" and set(result["grant_ids"]) == set(decided.grant_ids)
    # The gateway sends the decision after the approver's form ended; it says what was linked.
    assert result["account_links"] == [{"tool_id": "http:portainer", "owner": "team", "linked": False}]
    assert "set_up_tools" in result["message"]

    # Until the credential is linked, BOA's restart is refused for that reason alone.
    assert boa(gate, "post", RESTART).reason_code == REASON_ACCOUNT_LINK_MISSING
    service.store.create_link(
        AccountLinkV1(
            link_id="account_link_portainer",
            team_id=TEAM,
            tool_id="http:portainer",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM,
            credential_key="team_portainer",
            injections=(CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-API-Key", field="api_token"),),
            created_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            created_at_utc=service.clock(),
        )
    )
    # BOA's task gets it from TeamArchitect on its first call (no request, no turn).
    allowed = boa(gate, "post", RESTART)
    assert allowed.allowed, allowed
    status = json.loads(get_proposal_status(result["proposal_id"]))
    assert status["account_links"] == [{"tool_id": "http:portainer", "owner": "team", "linked": True}]
    assert not boa(gate, "delete", "https://portainer.example.test/api/stacks/1").allowed, "deleting needs full"
    assert boa(gate, "post", "https://other.example.test/api").reason_code == REASON_UNLISTED_HOST


def holder_links(world) -> list[str]:
    _lead, requests, _gate, _instance, _service = world
    applier = requests().appliers[ACCESS_PROPOSAL_KIND]
    return [item.tool_id for items in applier.account_links.values() for item in items]


def test_a_failure_while_storing_leaves_no_tool_and_no_grants(world, monkeypatch: pytest.MonkeyPatch) -> None:
    lead, requests, gate, instance, service = world
    set_agent_ref(lead)
    context = Context("tooluse-propose-2")
    with pytest.raises(InterruptException):
        propose(context)
    [paused] = context.raised

    def broken(*_args, **_kwargs):
        raise ValueError("disk full")

    monkeypatch.setattr(service.store, "apply_together", broken)
    with pytest.raises(HandDownRefused, match="nothing changed"):
        requests().decide(paused["access_request_id"], OWNER, AccessReviewChoice.APPROVE)

    tools_dir = instance / "control" / "access" / "tools"
    assert not any(tools_dir.glob("*.yaml")) if tools_dir.exists() else True
    assert service.store.list_team(TEAM) == []
    assert service.store.get_request(paused["access_request_id"]).status is AccessRequestStatus.WAITING
    assert boa(gate, "post", RESTART).reason_code == REASON_UNLISTED_HOST


def test_proposals_are_decided_by_administrators_and_checked_before_review(world) -> None:
    lead, requests, _gate, _instance, service = world
    set_agent_ref(lead)
    member = type(OWNER)("member-1", TEAM, ("member",))
    context = Context("tooluse-propose-3")
    with pytest.raises(InterruptException):
        propose(context)
    request = service.store.get_request(context.raised[0]["access_request_id"])
    assert not requests().may_decide(member, request)

    refused = json.loads(
        asyncio.run(
            propose_access_change(
                title="Use a clashing host",
                reason="x",
                tool_context=Context("tooluse-propose-4"),
                tools=[{**PORTAINER, "tool_id": "http:github2", "hosts": ["api.github.com"]}],
            )
        )
    )
    assert refused["status"] == "refused" and "api.github.com" in refused["reason"]
    unknown = json.loads(
        asyncio.run(
            propose_access_change(
                title="Access to nothing",
                reason="x",
                tool_context=Context("tooluse-propose-5"),
                grants=[{"agent_type": "X", "target": {"kind": "tool", "id": "http:nowhere"}, "level": "read"}],
            )
        )
    )
    assert unknown["status"] == "refused" and "not in the tool catalog" in unknown["reason"]
    # Workers keep no lasting access, so a row giving one lasting access is refused before anyone reviews it.
    worker_row = json.loads(
        asyncio.run(
            propose_access_change(
                title="Let BOA keep Schedules",
                reason="x",
                tool_context=Context("tooluse-propose-6"),
                grants=[{"agent_type": "BackgroundOperationsAgent", "target": {"kind": "tool", "id": "fn:schedules"},
                         "level": "read"}],
            )
        )
    )
    assert worker_row["status"] == "refused" and "Workers keep no lasting access" in worker_row["reason"]


def _propose_tool_change(context: Context, changed_tool: dict, **extra) -> dict:
    return json.loads(
        asyncio.run(
            propose_access_change(
                title="Update Portainer hosts and add gate credential",
                reason="Portainer moved behind a Cloudflare gate and gained a mirror host.",
                tool_context=context,
                tool_changes=[changed_tool],
                **extra,
            )
        )
    )


def test_tool_change_proposal_end_to_end_replaces_file_enforces_gate_and_outdated_link(world) -> None:
    """A2 + A3 + A4 end-to-end: propose tool_changes -> review rows -> approve -> atomic replace -> gate & outdated link."""

    lead, requests, gate, instance, service = world
    set_agent_ref(lead)

    # 1. Create the initial Portainer tool via proposal and link its team account.
    c1 = Context("tooluse-initial-portainer")
    with pytest.raises(InterruptException):
        propose(c1)
    r1 = requests().decide(
        c1.raised[0]["access_request_id"], OWNER, AccessReviewChoice.APPROVE, kept_item_ids=("grant-0",)
    )
    assert r1.applied_at_utc is not None
    service.store.create_link(
        AccountLinkV1(
            link_id="link_portainer_v1",
            team_id=TEAM,
            tool_id="http:portainer",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM,
            credential_key="team_portainer",
            injections=(CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-API-Key", field="api_token"),),
            created_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            created_at_utc=service.clock(),
        )
    )
    assert boa(gate, "post", RESTART).allowed

    # 2. Propose a tool change adding a new host, changing account injections, and adding a gate credential.
    updated_portainer = {
        **PORTAINER,
        "hosts": ["portainer.example.test", "portainer-mirror.example.test"],
        "credential_injections": [
            {"placement": "header", "name": "Authorization", "field": "api_token", "template": "Bearer {value}"}
        ],
        "gate_injections": [
            {"placement": "header", "name": "X-CF-Gate", "field": "cf_secret"}
        ],
    }
    c2 = Context("tooluse-change-portainer")
    with pytest.raises(InterruptException):
        _propose_tool_change(
            c2,
            updated_portainer,
            account_links=[
                {
                    "tool_id": "http:portainer",
                    "owner": "team",
                    "purpose": "Pass the Cloudflare gate.",
                    "link_purpose": "gate",
                }
            ],
        )
    req2 = service.store.get_request(c2.raised[0]["access_request_id"])
    rows = {row.item_id: row for row in req2.change.items}
    assert "change-0" in rows and "change-0-hosts-added" in rows
    assert "new_host" in rows["change-0-hosts-added"].flags
    assert "change-0-account-credentials" in rows and "change-0-gate-credentials" in rows
    assert "/link http:portainer team" in rows["change-0-relink"].text
    assert "gate credential" in rows["account-0"].text

    # Only an administrator may approve tool_changes (installs=True).
    member = type(OWNER)("member-1", TEAM, ("member",))
    assert not requests().may_decide(member, req2)

    # 3. Approve: file is replaced atomically and catalog reloads.
    decided = requests().decide(req2.request_id, OWNER, AccessReviewChoice.APPROVE)
    assert decided.applied_at_utc is not None
    c2.responses[access_tools.ACCESS_REQUEST_INTERRUPT_NAME] = access_request_runtime.decision_response(
        service.store.get_request(req2.request_id)
    )
    resumed = _propose_tool_change(
        c2,
        updated_portainer,
        account_links=[
            {
                "tool_id": "http:portainer",
                "owner": "team",
                "purpose": "Pass the Cloudflare gate.",
                "link_purpose": "gate",
            }
        ],
    )
    assert resumed["status"] == "applied"
    assert resumed["account_links"] == [
        {"tool_id": "http:portainer", "owner": "team", "linked": False, "link_purpose": "gate"}
    ]
    assert "/link <tool_id> gate" in resumed["message"]

    # 4. Broker sees the new host immediately and refuses for missing gate link first, then outdated account link.
    mirror_url = "https://portainer-mirror.example.test/api/stacks/1/restart"
    assert boa(gate, "post", mirror_url).reason_code == REASON_GATE_LINK_MISSING
    service.store.create_link(
        AccountLinkV1(
            link_id="link_portainer_gate",
            team_id=TEAM,
            tool_id="http:portainer",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM,
            purpose=LinkPurpose.GATE,
            credential_key="team_portainer_gate",
            injections=(CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-CF-Gate", field="cf_secret"),),
            created_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            created_at_utc=service.clock(),
        )
    )
    # Gate link now matches, so broker reaches the old account link and refuses it as outdated!
    assert boa(gate, "post", mirror_url).reason_code == REASON_ACCOUNT_LINK_OUTDATED


def test_tool_change_rollback_on_same_open_repository_and_conflict_after_review(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2 + C18: failed decision restores previous file on the same open repository (no reopen), and edited files are refused."""

    lead, requests, _gate, instance, service = world
    set_agent_ref(lead)

    c1 = Context("tooluse-seed-portainer")
    with pytest.raises(InterruptException):
        propose(c1)
    requests().decide(c1.raised[0]["access_request_id"], OWNER, AccessReviewChoice.APPROVE, kept_item_ids=("grant-0",))

    tool_file = instance / "control" / "access" / "tools" / "proposed-http-portainer.yaml"
    original_bytes = tool_file.read_bytes()

    updated = {**PORTAINER, "hosts": ["portainer.example.test", "portainer-v2.example.test"]}
    c2 = Context("tooluse-change-rollback")
    with pytest.raises(InterruptException):
        _propose_tool_change(c2, updated)
    req_id = c2.raised[0]["access_request_id"]

    # Simulate apply_together failure on the SAME open AccessRepository (C18: no reopen before reconcile).
    def broken(*_args, **_kwargs):
        raise ValueError("disk full during store commit")

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "apply_together", broken)
        with pytest.raises(HandDownRefused, match="nothing changed"):
            requests().decide(req_id, OWNER, AccessReviewChoice.APPROVE)

    assert tool_file.read_bytes() == original_bytes
    assert service.store.decision_recoveries(TEAM) == []
    assert service.store.get_request(req_id).status is AccessRequestStatus.WAITING
    assert "portainer-v2.example.test" not in service.catalog.http_hosts

    # Now edit the file on disk after review -> approval is refused and external edit stays intact.
    external = original_bytes + b"# operator comment\n"
    tool_file.write_bytes(external)
    with pytest.raises(HandDownRefused, match="changed after the proposal was prepared"):
        requests().decide(req_id, OWNER, AccessReviewChoice.APPROVE)
    assert tool_file.read_bytes() == external


@pytest.mark.parametrize("committed", [False, True])
def test_unconfirmed_tool_change_is_reconciled_after_reopening_the_store(world, monkeypatch, committed: bool) -> None:
    from datetime import timedelta

    from access_control.requests import DecisionOutcomeUnknown

    lead, requests, _gate, instance, service = world
    set_agent_ref(lead)
    c1 = Context("tooluse-seed-reopen")
    with pytest.raises(InterruptException):
        propose(c1)
    requests().decide(c1.raised[0]["access_request_id"], OWNER, AccessReviewChoice.APPROVE, kept_item_ids=("grant-0",))
    tool_file = instance / "control" / "access" / "tools" / "proposed-http-portainer.yaml"
    original_bytes = tool_file.read_bytes()

    updated = {**PORTAINER, "hosts": ["portainer.example.test", "portainer-reopen.example.test"]}
    c2 = Context("tooluse-change-reopen")
    with pytest.raises(InterruptException):
        _propose_tool_change(c2, updated)
    req_id = c2.raised[0]["access_request_id"]

    with uncertain_store(monkeypatch, service.store, "apply_together", committed=committed):
        with pytest.raises(DecisionOutcomeUnknown, match="could not yet confirm"):
            requests().decide(req_id, OWNER, AccessReviewChoice.APPROVE)

    assert tool_file.read_bytes() != original_bytes
    service.store = AccessRepository(service.store.settings)
    later = service.clock() + timedelta(minutes=11)
    monkeypatch.setattr(service, "clock", lambda: later)
    requests().recover_decisions()
    assert service.store.decision_recoveries(TEAM, now=later) == []
    if committed:
        assert b"portainer-reopen.example.test" in tool_file.read_bytes()
        assert service.store.get_request(req_id).status is AccessRequestStatus.GRANTED
    else:
        assert tool_file.read_bytes() == original_bytes
        assert service.store.get_request(req_id).status is AccessRequestStatus.WAITING


def test_tool_change_with_authorization_off_applies_with_confirmed_and_rolls_back_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A7: with authorization.enabled false, tool_changes shows diffs before confirmation, applies on confirmed=true, and rolls back on failure."""

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    instance = tmp_path / "instance"
    (instance / "control").mkdir(parents=True)
    lead = make_agent(None, tmp_path, agent_type="TeamArchitect", lead=True)
    lead.instance_root = str(instance)
    lead.authorization_config = {"enabled": False}
    set_agent_ref(lead)

    try:
        # 1. Create tool with authorization off.
        created = json.loads(
            asyncio.run(
                propose_access_change(
                    title="Add Portainer",
                    reason="Manage stacks",
                    tool_context=Context("tooluse-auth-off-1"),
                    tools=[PORTAINER],
                    confirmed=True,
                )
            )
        )
        assert created["status"] == "applied"
        tool_file = instance / "control" / "access" / "tools" / "proposed-http-portainer.yaml"
        original_bytes = tool_file.read_bytes()

        # 2. Propose a tool change with confirmed=False -> agreement_needed with trusted diff rows, file untouched.
        updated = {
            **PORTAINER,
            "hosts": ["portainer.example.test", "portainer-extra.example.test"],
            "gate_injections": [{"placement": "header", "name": "X-Gate", "field": "gate"}],
        }
        preview = _propose_tool_change(Context("tooluse-auth-off-2"), updated, confirmed=False)
        assert preview["status"] == "agreement_needed"
        assert any("portainer-extra.example.test" in line for line in preview["changes"])
        assert any("Team gate credential added" in line for line in preview["changes"])
        assert tool_file.read_bytes() == original_bytes

        # 3. Built-in tool is still refused with authorization off.
        refused = _propose_tool_change(
            Context("tooluse-auth-off-builtin"),
            {**PORTAINER, "tool_id": "http:github", "hosts": ["api.github.com"]},
            confirmed=True,
        )
        assert refused["status"] == "refused" and "built-in tool" in refused["reason"]

        # 4. A failure after replacing the file (e.g. protected data folder) restores the original file!
        failed = _propose_tool_change(
            Context("tooluse-auth-off-rollback"),
            updated,
            data_folders=[{"name": "bad_folder", "path": str(instance), "writable": False}],
            confirmed=True,
        )
        assert failed["status"] == "refused"
        assert tool_file.read_bytes() == original_bytes

        # 5. confirmed=True applies the replacement cleanly.
        applied = _propose_tool_change(Context("tooluse-auth-off-3"), updated, confirmed=True)
        assert applied["status"] == "applied"
        assert b"portainer-extra.example.test" in tool_file.read_bytes()
        assert b"X-Gate" in tool_file.read_bytes()
    finally:
        access_runtime._runtime_access.cache_clear()
        set_agent_ref(None)
