"""The web chat's Access page (the access map) with real Cedar and SQLite.

Covers: agent types with their grants, where each came from and their
standard access (which cannot be removed); what each person gave (members see
only their own); tools with their operations' levels and account links
without secrets; blocks and unblocks as the signed-in person; removing access
and what was handed on from it; the authorization-off state; and the HTTP
routes with the same sign-in and CSRF checks as the access Inbox.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver, agent_type_id
from access_control.inbox import StoredAccessInbox
from access_control.requests import AccessRequests
from access_control.starter_profiles import apply_starter_profile
from assistant_gateway.access_inbox import GatewayAccessInbox, GatewayAccessInboxSettings
from assistant_gateway.access_map import ACCESS_OFF_TEXT, NOT_A_MEMBER_TEXT, GatewayAccessMap
from assistant_gateway.access_setup import MembershipRoles
from assistant_gateway.application.access_map_ports import AccessBlockInput
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeKind,
    AccessNoticeV1,
    AccountLinkV1,
    ChangeProposalV1,
    DelegationMode,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
LEAD = "TeamArchitect"
OWNER = PersonPrincipal("owner-1", TEAM, ("owner",))
# work_id: the request its task works on (task-only access is bound to it).
WORKER = AgentPrincipal("agent-web", "WebResearchAgent", "worker", TEAM, work_id="task-1")
# A standard-set tool: every person holds it, agents need a grant for it.
SCHEDULES = TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules")
# A tool members do not hold (in no standard set).
X_POSTS = TargetRefV1(kind=TargetKind.TOOL, id="cli:x")
NAMES = {"owner-1": "Olivia Owner", "admin-1": "Ada Admin", "member-1": "Mia Member", "member-2": "Max Member"}
ROLES = {"owner-1": "owner", "admin-1": "admin", "member-1": "member", "member-2": "member"}

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


class Identity:
    """Active users, their names and team roles, as the gateway identity store keeps them."""

    def __init__(self, roles: dict[str, str]) -> None:
        self.roles = roles

    def get_gateway_user(self, person_id: str) -> Any:
        if person_id not in self.roles:
            return None
        return SimpleNamespace(status="active", display_name=NAMES.get(person_id, ""))

    def list_team_memberships(self, person_id: str, status: str = "active") -> list[Any]:
        role = self.roles.get(person_id)
        return [SimpleNamespace(team_id=TEAM, role=role)] if role else []


@dataclass
class Harness:
    inbox: GatewayAccessInbox
    agents: AccessRequests

    @property
    def map(self) -> GatewayAccessMap:
        return self.inbox.access_map

    def view(self, person_id: str, view: str = "agents", **kwargs: Any) -> dict[str, Any]:
        return asyncio.run(self.map.view(person_id, view=view, **kwargs))


def build_inbox(tmp_path: Path, storage: Any, roles: dict[str, str], *, enabled: bool = True) -> GatewayAccessInbox:
    async def emit(_event: Any) -> None:
        return None

    return GatewayAccessInbox(
        settings=GatewayAccessInboxSettings(
            team_id=TEAM,
            instance_name="test",
            response_route="gateway-route",
            lead_agent_type=LEAD,
            inbox_url="https://ua.example/webchat",
            instance_root=None,
            storage_settings=storage,
            cedar_binary=str(BINARY),
            authorization_enabled=enabled,
        ),
        memberships=MembershipRoles(Identity(roles)),
        manager_client=SimpleNamespace(connected=False),
        session_repository=SimpleNamespace(get_session_by_conversation_id=lambda _id: None),
        proactive_session=lambda: None,
        emit_delivery_event=emit,
    )


@pytest.fixture
def harness(tmp_path: Path):
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    inbox = build_inbox(tmp_path, storage, ROLES)
    # The agents' side: its own service on the same store, as in an agent process.
    service = AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver(LEAD),
    )
    agents = AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type=LEAD)
    # The owner's starter profile gives TeamArchitect the standard tool set,
    # which it hands on to a worker without asking.
    apply_starter_profile(service, OWNER, "trusted", lead_agent_type=LEAD)
    handed = agents.submit(requester=WORKER, person=OWNER, target=SCHEDULES, level=AccessLevel.READ, reason="Research")
    assert handed.grant is not None and handed.grant.status is GrantStatus.ACTIVE
    yield Harness(inbox, agents)
    inbox.close()
    service.cedar.close()


def holder(page: dict[str, Any], key: str) -> dict[str, Any]:
    return next(item for item in page["items"] if item["key"] == key)


def person_grant(harness: Harness, *, by: PersonPrincipal, to: SubjectRefV1, target: TargetRefV1, **extra: Any) -> GrantV1:
    service = harness.agents.service
    spec = GrantV1(
        grant_id="new",
        team_id=TEAM,
        subject=to,
        target=target,
        level=extra.pop("level", AccessLevel.READ),
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=by.person_id),
        reason=extra.pop("reason", "Given by hand"),
        created_at_utc=service.clock(),
        **extra,
    )
    return service.grant_by_person(by, spec)


def test_agent_types_show_their_grants_where_they_came_from_and_standard_access_apart(harness: Harness) -> None:
    page = harness.view("owner-1")

    assert page["enabled"] is True and page["notice"] == ""
    assert [item["key"] for item in page["items"]] == ["agent_type:TeamArchitect", "agent_type:WebResearchAgent"]
    lead = holder(page, "agent_type:TeamArchitect")
    assert lead["role_words"] == "Team lead" and lead["can_block"] is True
    # Internet access is standard for every role (decision D14).
    assert {row["target_name"] for row in lead["standard"]} == {
        "Agent runtime tools",
        "Team lead tools",
        "Internet access",
        "the team/ folder",
        "the agents/ folder",
    }
    assert all("grant_id" not in row and "can_revoke" not in row for row in lead["standard"]), "standard access has no remove"
    # The Trusted profile: the standard set, shell and Python in the sandbox, and
    # model choice (the owner is an administrator).
    assert sorted(row["target_name"] for row in lead["grants"]) == [
        "Model choice", "Sandboxed code", "Saved credentials", "Sealbox", "Standard team tools"
    ]
    [starter] = [row for row in lead["grants"] if row["target_name"] == "Standard team tools"]
    assert starter["chain"] == ["You", "TeamArchitect"]
    assert starter["target_name"] == "Standard team tools" and starter["target_kind_words"] == "tool set"
    assert starter["hand_on_words"].startswith("Can hand it on to other agents without asking")
    assert starter["lifetime_words"] == "until someone removes it" and starter["can_revoke"] is True

    worker = holder(page, "agent_type:WebResearchAgent")
    assert worker["role_words"] == "Worker"
    assert [row["target_name"] for row in worker["standard"]] == [
        "Agent runtime tools",
        "Internet access",
        "the team/ folder",
        "the agents/ folder",
    ]
    [handed] = worker["grants"]
    assert handed["chain"] == ["You", "TeamArchitect", "WebResearchAgent"]
    assert handed["given_by"] == "TeamArchitect" and handed["level_words"] == "read"
    assert handed["hand_on_words"] == "Cannot hand it on."

    # Another administrator sees the owner's name at the root of the chain.
    admin_view = holder(harness.view("admin-1"), "agent_type:WebResearchAgent")
    assert admin_view["grants"][0]["chain"] == ["Olivia Owner", "TeamArchitect", "WebResearchAgent"]

    # Standard grants are not stored, so they cannot be removed by id either.
    with pytest.raises(HandDownRefused, match="no such grant"):
        asyncio.run(harness.inbox.revoke_grant("owner-1", "std-worker-agent_runtime"))


def test_members_see_only_what_serves_their_own_work_and_cannot_remove_others(harness: Harness) -> None:
    """Decision D13: what the owner authorized for their own work is not shown to a member."""

    page = harness.view("member-1")

    assert [item["key"] for item in page["items"]] == ["agent_type:TeamArchitect"]
    assert page["items"][0]["grants"] == [], "the owner's starter profile serves the owner's work"
    assert page["block_tools"] == []
    assert all(item["can_block"] is False for item in page["items"])
    owners = holder(harness.view("admin-1"), "agent_type:WebResearchAgent")
    [grant] = owners["grants"]
    assert grant["for_whose_work"] == "Olivia Owner's work"
    with pytest.raises(HandDownRefused, match="Only the person who gave this access"):
        asyncio.run(harness.inbox.revoke_grant("member-1", grant["grant_id"]))


def test_what_people_gave_is_private_to_them_and_administrators(harness: Harness) -> None:
    member = PersonPrincipal("member-1", TEAM, ("member",))
    person_grant(
        harness,
        by=OWNER,
        to=SubjectRefV1(kind=SubjectKind.USER, id="member-1"),
        target=X_POSTS,
        delegation_depth=1,
        reason="Mia may post to X",
    )
    # A member gives the team lead access for their own work only (decision D13);
    # workers get it from the team lead for each task.
    given = person_grant(
        harness,
        by=member,
        to=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, LEAD)),
        target=X_POSTS,
        reason="Summaries are posted",
        for_person_id=member.person_id,
    )

    [mine] = harness.view("member-1", "people")["items"]
    assert mine["title"] == "You" and mine["is_you"] is True
    assert [(row["recipient"], row["target_name"], row["can_revoke"]) for row in mine["given"]] == [
        (LEAD, "X (xurl)", True)
    ]
    other = harness.view("member-2", "people")
    assert other["sees_everyone"] is False
    assert [(item["title"], item["given"]) for item in other["items"]] == [("You", [])]

    everyone = harness.view("admin-1", "people")
    assert everyone["sees_everyone"] is True
    assert [item["title"] for item in everyone["items"]] == ["You", "Mia Member", "Olivia Owner"]
    owner_rows = everyone["items"][2]["given"]
    assert ("Mia Member", "X (xurl)") in {(row["recipient"], row["target_name"]) for row in owner_rows}
    assert {"TeamArchitect"} <= {row["recipient"] for row in owner_rows}

    # A member's own grant to the team lead can be removed by them from the agent view.
    [summary] = holder(harness.view("member-1"), f"agent_type:{LEAD}")["grants"]
    assert summary["grant_id"] == given.grant_id and summary["can_revoke"] is True
    assert summary["chain"] == ["You", LEAD]
    assert summary["for_whose_work"] == "your work"
    # Another member never sees what this member authorized for their own work (D13).
    assert holder(harness.view("member-2"), f"agent_type:{LEAD}")["grants"] == []
    admin_rows = holder(harness.view("admin-1"), f"agent_type:{LEAD}")["grants"]
    assert [row["for_whose_work"] for row in admin_rows if row["target"] == "cli:x"] == ["Mia Member's work"]


def test_blocks_are_added_and_removed_as_the_signed_in_person(harness: Harness) -> None:
    def block(person_id: str, **fields: Any) -> dict[str, Any]:
        values = {"subject_kind": "agent_type", "subject": "WebResearchAgent", "tool_id": "fn:schedules",
                  "operation": "", "reason": "Too many searches", "hours": 24, **fields}
        return asyncio.run(harness.map.block(person_id, AccessBlockInput(**values)))

    created = block("owner-1")
    assert created["message"] == "WebResearchAgent is blocked from Schedules. It applies from the next action."
    assert harness.agents.service.blocks_on(WORKER, SCHEDULES) == (created["block"]["block_id"],), "the block is real"

    [shown] = holder(harness.view("owner-1"), "agent_type:WebResearchAgent")["blocks"]
    assert shown["blocked_by"] == "You" and shown["reason"] == "Too many searches"
    assert shown["lifetime_words"] == "until a set time" and shown["expires_at_utc"]
    assert shown["can_unblock"] is True
    member_view = holder(harness.view("member-1"), "agent_type:WebResearchAgent")["blocks"][0]
    assert member_view["blocked_by"] == "Olivia Owner" and member_view["can_unblock"] is False

    with pytest.raises(HandDownRefused, match="Only the person who added this block"):
        asyncio.run(harness.map.unblock("member-1", shown["block_id"]))
    with pytest.raises(HandDownRefused, match="you cannot give access to Schedules"):
        block("member-1")
    with pytest.raises(ValueError, match="Say why"):
        block("owner-1", reason="  ")
    with pytest.raises(HandDownRefused, match="Block an agent type"):
        block("owner-1", subject_kind="role")
    # A whole tool set can be blocked too.
    set_block = block("owner-1", tool_id="standard")
    assert set_block["message"] == "WebResearchAgent is blocked from Standard team tools. It applies from the next action."
    asyncio.run(harness.map.unblock("owner-1", set_block["block"]["block_id"]))
    with pytest.raises(HandDownRefused, match="not an operation"):
        block("owner-1", operation="no_such_operation")

    # An administrator who did not add the block may remove it.
    removed = asyncio.run(harness.map.unblock("admin-1", shown["block_id"]))
    assert removed["message"].startswith("The block on WebResearchAgent for Schedules is removed.")
    assert holder(harness.view("owner-1"), "agent_type:WebResearchAgent")["blocks"] == []
    assert harness.agents.service.blocks_on(WORKER, SCHEDULES) == ()


def test_removing_access_also_ends_what_was_handed_on_from_it(harness: Harness) -> None:
    [starter] = [
        row
        for row in holder(harness.view("owner-1"), "agent_type:TeamArchitect")["grants"]
        if row["target_name"] == "Standard team tools"
    ]

    removed = asyncio.run(harness.inbox.revoke_grant("owner-1", starter["grant_id"]))

    assert len(removed) == 2, "the hand-down to WebResearchAgent ends with it"
    page = harness.view("owner-1")
    assert [item["key"] for item in page["items"]] == ["agent_type:TeamArchitect"], "the lead is always listed"
    assert sorted(row["target_name"] for row in page["items"][0]["grants"]) == [
        "Model choice", "Sandboxed code", "Saved credentials", "Sealbox"
    ]
    assert page["items"][0]["standard"]


def test_access_that_ended_is_not_listed(harness: Harness) -> None:
    service = harness.inbox.requests().service
    person_grant(
        harness,
        by=OWNER,
        to=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "ReportAgent")),
        target=SCHEDULES,
        lifetime=GrantLifetime.UNTIL,
        expires_at_utc=service.clock() + timedelta(hours=1),
    )
    report = holder(harness.view("owner-1"), "agent_type:ReportAgent")["grants"][0]
    assert report["lifetime_words"] == "until a set time" and report["expires_at_utc"]

    later = service.clock() + timedelta(hours=2)
    service.clock = lambda: later
    assert "agent_type:ReportAgent" not in {item["key"] for item in harness.view("owner-1")["items"]}


def test_one_row_per_tool_with_its_step_up_and_a_note_about_everyones_access(harness: Harness) -> None:
    lead = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, LEAD))
    lasting = person_grant(harness, by=OWNER, to=lead, target=X_POSTS, level=AccessLevel.WRITE, for_person_id="owner-1")
    step_up = person_grant(
        harness, by=OWNER, to=lead, target=X_POSTS, level=AccessLevel.FULL,
        lifetime=GrantLifetime.TASK, task_id="task-1", for_person_id="owner-1",
    )
    person_grant(harness, by=OWNER, to=lead, target=X_POSTS, level=AccessLevel.READ, reason="For everyone")

    def x_rows():
        return [row for row in holder(harness.view("owner-1"), f"agent_type:{LEAD}")["grants"] if row["target"] == "cli:x"]

    rows = x_rows()

    [yours] = [row for row in rows if row["for_your_work"]]
    assert yours["grant_ids"] == [lasting.grant_id, step_up.grant_id]
    assert yours["level_words"] == "read-and-change, and full while one task runs"
    assert yours["lifetime_words"] == "until someone removes it" and yours["can_revoke"] is True
    assert "for everyone's work" in yours["entry_note"]
    [everyone] = [row for row in rows if row["for_person_id"] is None]
    assert "entry_note" not in everyone

    # Remove on the row takes the lasting access and the step-up together.
    removed = harness.inbox.requests().service.revoke_by_person(OWNER, yours["grant_id"])
    assert set(removed) == {lasting.grant_id, step_up.grant_id}
    assert [row["for_person_id"] for row in x_rows()] == [None]


def test_a_schedule_is_its_own_card_and_old_worker_access_is_not_listed(harness: Harness) -> None:
    from access_control.entities import schedule_subject

    service = harness.agents.service
    lead = AgentPrincipal("agent-lead", LEAD, "team_lead", TEAM)
    handed = harness.agents.give(
        lead, recipient=schedule_subject(TEAM, "digest"), target=SCHEDULES, level=AccessLevel.READ,
        reason="The digest job reads schedules", person=OWNER,
    )
    # A worker's lasting access from before access followed the work: kept in the store, never counted or listed.
    service.store.create(
        GrantV1(
            grant_id="grant_old_worker",
            team_id=TEAM,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "ReportAgent")),
            target=X_POSTS,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            created_at_utc=service.clock(),
            for_person_id=OWNER.person_id,
        )
    )
    harness.map._schedule_name = lambda schedule_id: "Daily digest" if schedule_id == "digest" else ""

    page = harness.view("owner-1")

    card = holder(page, "schedule:scheduled job digest")
    assert card["title"] == "Scheduled job Daily digest" and card["subject_kind"] == "schedule"
    [row] = card["grants"]
    assert row["grant_id"] == handed.grant.grant_id and row["chain"] == ["You", LEAD, "scheduled job Daily digest"]
    assert "agent_type:ReportAgent" not in {item["key"] for item in page["items"]}


def test_tools_list_levels_and_account_links_without_secrets(harness: Harness) -> None:
    store = harness.agents.service.store
    now = harness.agents.service.clock()
    for index, (owner_kind, owner_id, key) in enumerate((
        (LinkOwnerKind.TEAM, TEAM, "team_github_token"),
        (LinkOwnerKind.PERSON, "owner-1", "olivia_github_token"),
        (LinkOwnerKind.PERSON, "member-1", "mia_github_token"),
    )):
        store.create_link(
            AccountLinkV1(
                link_id=f"link-{index}",
                team_id=TEAM,
                tool_id="http:github",
                owner_kind=owner_kind,
                owner_id=owner_id,
                credential_key=key,
                created_by=SubjectRefV1(kind=SubjectKind.USER, id="owner-1" if owner_kind is LinkOwnerKind.TEAM else owner_id),
                created_at_utc=now,
            )
        )

    first = harness.view("member-1", "tools")
    catalog_size = len(harness.agents.service.catalog.tools)
    assert first["total"] == catalog_size and len(first["items"]) == 20 and first["has_more"] is True
    names = [item["display_name"] for item in first["items"]]
    assert names == sorted(names, key=str.lower)
    last = harness.view("member-1", "tools", offset=catalog_size + 5)
    assert last["offset"] == (catalog_size - 1) // 20 * 20 and last["has_more"] is False

    github = next(item for page in (first, harness.view("member-1", "tools", offset=20)) for item in page["items"]
                  if item["tool_id"] == "http:github")
    # The GitHub API takes only each person's own account: a team link left from before is shown as unused.
    assert github["account_words"] == "Uses only the linked account of the person the task works for"
    assert github["uses_accounts"] is True
    unused = "The team's account (not used: this service uses only each person's own account)"
    assert [(link["owner_words"], link["linked_by"]) for link in github["links"]] == [
        (unused, "Olivia Owner"),
        ("Your account", "You"),
    ]
    assert github["operations_words"].startswith(f"{github['operation_count']} operations:")
    admin_github = next(item for offset in (0, 20) for item in harness.view("admin-1", "tools", offset=offset)["items"]
                        if item["tool_id"] == "http:github")
    assert {link["owner_words"] for link in admin_github["links"]} == {
        unused, "Olivia Owner's account", "Mia Member's account",
    }
    assert "github_token" not in json.dumps([first, admin_github]), "credential keys are never shown"

    operations = asyncio.run(harness.map.tool_operations("member-1", "cli:google", offset=0))
    assert operations["tool_id"] == "cli:google" and len(operations["items"]) == 20
    assert operations["total"] == len(harness.agents.service.catalog.tools["cli:google"].operations)
    assert [op["name"] for op in operations["items"]] == sorted((op["name"] for op in operations["items"]), key=str.lower)
    assert {op["level_words"] for op in operations["items"]} <= {"read", "read-and-change", "full"}
    with pytest.raises(HandDownRefused, match="no such tool"):
        asyncio.run(harness.map.tool_operations("member-1", "cli:nothing", offset=0))


def test_an_agent_type_link_opens_the_page_that_lists_it(harness: Harness) -> None:
    for index in range(22):
        person_grant(
            harness,
            by=OWNER,
            to=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, f"Agent{index:02d}")),
            target=SCHEDULES,
        )

    page = harness.view("owner-1", agent="WebResearchAgent")
    assert page["offset"] == 20 and page["focus_key"] == "agent_type:WebResearchAgent"
    assert "agent_type:WebResearchAgent" in {item["key"] for item in page["items"]}
    missing = harness.view("owner-1", agent="NobodyAgent")
    assert missing["focus_missing"] is True and missing["offset"] == 0


def test_a_person_without_a_membership_sees_why_and_cannot_change_anything(harness: Harness) -> None:
    page = harness.view("stranger")
    assert page["notice"] == NOT_A_MEMBER_TEXT and page["items"] == []
    with pytest.raises(HandDownRefused, match="not an active member"):
        asyncio.run(harness.map.unblock("stranger", "grant-x"))


def test_with_authorization_off_the_page_only_says_so(tmp_path: Path) -> None:
    def no_service() -> AccessRequests:
        raise AssertionError("no access service is opened with authorization off")

    access_map = GatewayAccessMap(
        requests=no_service,
        person=lambda person_id: PersonPrincipal(person_id, TEAM, ("owner",)),
        display_name=lambda person_id: person_id,
        lead_agent_type=LEAD,
        authorization_enabled=False,
    )
    page = asyncio.run(access_map.view("owner-1", view="tools"))
    assert page["enabled"] is False and page["notice"] == ACCESS_OFF_TEXT and page["items"] == []
    with pytest.raises(HandDownRefused, match="Access control is off"):
        asyncio.run(access_map.block("owner-1", AccessBlockInput("agent_type", "X", "fn:schedules", "", "r", None)))


# -- HTTP routes -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_map_routes_use_the_signed_in_person_and_write_checks(tmp_path: Path) -> None:
    from tests.test_gateway_website_app import (
        InternalWebsiteInteractiveApp,
        _config,
        _UserTokenStatefulFakeGateway,
    )

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    # The web token signs in as user_alpha, the team's owner here.
    inbox = build_inbox(tmp_path, storage, {"user_alpha": "owner", "member-1": "member"})
    service = inbox.requests().service
    apply_starter_profile(service, PersonPrincipal("user_alpha", TEAM, ("owner",)), "trusted", lead_agent_type=LEAD)
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    gateway = _UserTokenStatefulFakeGateway(GatewayStateStore(str(tmp_path / "gateway.db")))
    gateway.access_inbox = inbox
    website = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())
    try:
        async with TestClient(TestServer(website.build_app())) as client:
            assert (await client.get("/api/web-chat/access-map")).status == 401
            auth = await (await client.post("/api/web-chat/auth", json={"token": "ua_user_token"})).json()
            headers = {
                "Origin": str(client.make_url("/")).rstrip("/"),
                "Sec-Fetch-Site": "same-origin",
                "X-UA-Permission-CSRF": auth["permission_csrf_token"],
            }

            read = await client.get("/api/web-chat/access-map", params={"view": "agents", "person_id": "member-1"})
            assert read.status == 200 and read.headers["Cache-Control"] == "no-store"
            page = await read.json()
            lead = page["items"][0]
            assert lead["key"] == "agent_type:TeamArchitect" and lead["grants"][0]["chain"] == ["You", "TeamArchitect"]
            assert (await client.get("/api/web-chat/access-map", params={"view": "secrets"})).status == 400
            assert (await client.get("/api/web-chat/access-map", params={"offset": "-1"})).status == 400
            tools = await (await client.get("/api/web-chat/access-map", params={"view": "tools", "offset": "20"})).json()
            assert tools["offset"] == 20 and tools["items"]
            operations = await client.get("/api/web-chat/access-map/tools/cli%3Agoogle/operations", params={"offset": "20"})
            assert operations.status == 200 and (await operations.json())["offset"] == 20

            body = {"subject_kind": "agent_type", "subject": "TeamArchitect", "tool_id": "cli:google",
                    "operation": "command:gmail.send", "reason": "No mail this week", "hours": "", "person_id": "member-1"}
            assert (await client.post("/api/web-chat/access-map/blocks", json=body)).status == 403
            refused = await client.post("/api/web-chat/access-map/blocks", headers=headers, json={**body, "reason": ""})
            assert refused.status == 409
            assert (await refused.json()) == {"status": "refused", "error": "Say why you are blocking it; people read the reason."}
            bad_hours = await client.post("/api/web-chat/access-map/blocks", headers=headers, json={**body, "hours": "soon"})
            assert (await bad_hours.json())["error"] == "Choose how long the block lasts."
            created = await client.post("/api/web-chat/access-map/blocks", headers=headers, json=body)
            assert created.status == 200
            block = (await created.json())["block"]
            assert block["blocked_by"] == "You" and block["target"] == "cli:google#command:gmail.send"
            assert service.store.get(block["block_id"]).granted_by.id == "user_alpha", "the person is the session's user"

            unblock_path = f"/api/web-chat/access-map/blocks/{block['block_id']}/remove"
            assert (await client.post(unblock_path, json={})).status == 403
            assert (await client.post(unblock_path, headers=headers, json={})).status == 200
            again = await client.post(unblock_path, headers=headers, json={})
            assert again.status == 409 and "already revoked" in (await again.json())["error"]

            grant_id = lead["grants"][0]["grant_id"]
            revoked = await client.post(f"/api/web-chat/access-inbox/grants/{grant_id}/revoke", headers=headers, json={})
            assert revoked.status == 200 and grant_id in (await revoked.json())["revoked_grant_ids"]
            assert service.store.get(grant_id).status is GrantStatus.REVOKED

            shell = await (await client.get("/webchat")).text()
            assert 'id="settings-access-tab"' in shell and 'id="settings-access-panel"' in shell
            assert shell.index('id="settings-inbox-tab"') < shell.index('id="settings-access-tab"') < shell.index(
                'id="settings-team-tab"'
            )
            module = await client.get("/access_map.mjs")
            assert module.status == 200 and "export function createAccessMap" in await module.text()
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_the_chat_card_reads_one_request_and_decides_it_as_the_signed_in_person(tmp_path: Path) -> None:
    from tests.test_gateway_website_app import (
        InternalWebsiteInteractiveApp,
        _config,
        _UserTokenStatefulFakeGateway,
    )

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    inbox = build_inbox(tmp_path, storage, {"user_alpha": "owner"})
    requests = inbox.requests()
    staged = requests.submit_change(
        requester=AgentPrincipal("agent-lead", LEAD, "team_lead", TEAM),
        person=PersonPrincipal("user_alpha", TEAM, ("owner",)),
        proposal=ChangeProposalV1(
            kind="runtime_settings",
            change_id="settings_change_1",
            plan_digest="a" * 64,
            title="Change runtime settings: current_agent.llm.model_id",
            summary="Switch TeamArchitect to b. Takes effect now.",
            effect=("current_agent.llm.model_id for TeamArchitect changes from 'a' to 'b'.",),
        ),
        reason="The person asked for it",
        task_id="task-lead",
        conversation_id="conv-1",
    ).request
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    gateway = _UserTokenStatefulFakeGateway(GatewayStateStore(str(tmp_path / "gateway.db")))
    gateway.access_inbox = inbox
    website = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())
    path = f"/api/web-chat/access-inbox/requests/{staged.request_id}"
    try:
        async with TestClient(TestServer(website.build_app())) as client:
            assert (await client.get(path)).status == 401
            auth = await (await client.post("/api/web-chat/auth", json={"token": "ua_user_token"})).json()
            headers = {
                "Origin": str(client.make_url("/")).rstrip("/"),
                "Sec-Fetch-Site": "same-origin",
                "X-UA-Permission-CSRF": auth["permission_csrf_token"],
            }
            waiting = (await (await client.get(path)).json())["review"]
            assert waiting["summary"] == "TeamArchitect asks for approval: Switch TeamArchitect to b. Takes effect now."
            assert waiting["can_decide"] is True and waiting["status"] == "waiting"
            missing = await client.get("/api/web-chat/access-inbox/requests/access_req_missing")
            assert missing.status == 404 and "not available to you" in (await missing.json())["error"]

            assert (await client.post(f"{path}/decision", json={"choice": "deny"})).status == 403
            assert (await client.post(f"{path}/decision", headers=headers, json={"choice": "deny"})).status == 200
            decided = (await (await client.get(path)).json())["review"]
            assert decided["status"] == "denied" and decided["decision"] == "deny"
            assert decided["can_decide"] is False and decided["choices"] == []

            module = await client.get("/chat_access_review.mjs")
            assert module.status == 200 and "export function createChatAccessReviews" in await module.text()
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_inbox_routes_page_notices_count_them_and_mark_them_read(tmp_path: Path) -> None:
    from tests.test_gateway_website_app import (
        InternalWebsiteInteractiveApp,
        _config,
        _UserTokenStatefulFakeGateway,
    )

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    inbox = build_inbox(tmp_path, storage, {"user_alpha": "owner"})
    for minute in range(3):
        inbox.send_notice(
            AccessNoticeV1(
                notice_id=f"n-{minute}", team_id=TEAM, kind=AccessNoticeKind.WORK_STUCK,
                recipient_person_id="user_alpha", title=f"Notice {minute}", summary="Something to check.",
                created_at_utc=datetime(2026, 10, 4, 12, minute, tzinfo=timezone.utc),
            )
        )
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    gateway = _UserTokenStatefulFakeGateway(GatewayStateStore(str(tmp_path / "gateway.db")))
    gateway.access_inbox = inbox
    website = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())
    try:
        async with TestClient(TestServer(website.build_app())) as client:
            assert (await client.get("/api/web-chat/access-inbox/counts")).status == 401
            auth = await (await client.post("/api/web-chat/auth", json={"token": "ua_user_token"})).json()
            headers = {
                "Origin": str(client.make_url("/")).rstrip("/"),
                "Sec-Fetch-Site": "same-origin",
                "X-UA-Permission-CSRF": auth["permission_csrf_token"],
            }
            counts = await client.get("/api/web-chat/access-inbox/counts")
            assert counts.status == 200 and counts.headers["Cache-Control"] == "no-store"
            assert await counts.json() == {"status": "ok", "decidable_requests": 0, "unread_notices": 3}

            page = await (await client.get("/api/web-chat/access-inbox", params={"limit": "2"})).json()
            assert [n["notice_id"] for n in page["notices"]] == ["n-2", "n-1"] and page["notices_total"] == 3
            assert (await client.get("/api/web-chat/access-inbox", params={"notices": "old"})).status == 400

            read_path = "/api/web-chat/access-inbox/notices/read"
            assert (await client.post(read_path, json={"all": True})).status == 403, "writes need the CSRF header"
            assert (await client.post(read_path, headers=headers, json={"notice_ids": []})).status == 400
            assert (await client.post(read_path, headers=headers, json={"notice_ids": ["x"] * 101})).status == 400
            some = await client.post(read_path, headers=headers, json={"notice_ids": ["n-2"]})
            assert await some.json() == {"status": "ok", "marked": 1, "unread_notices": 2}
            rest = await client.post(read_path, headers=headers, json={"all": True})
            assert await rest.json() == {"status": "ok", "marked": 2, "unread_notices": 0}
            everything = await (await client.get("/api/web-chat/access-inbox", params={"notices": "all"})).json()
            assert everything["notices_total"] == 3 and all(n["read"] for n in everything["notices"])
    finally:
        inbox.close()


def test_delegation_words_say_how_access_can_be_handed_on(harness: Harness) -> None:
    person_grant(
        harness,
        by=OWNER,
        to=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, "PlannerAgent")),
        target=SCHEDULES,
        delegation_depth=2,
        delegation_mode=DelegationMode.ASK_OWNER,
    )
    [grant] = holder(harness.view("owner-1"), "agent_type:PlannerAgent")["grants"]
    assert grant["hand_on_words"] == (
        "Can hand it on to other agents only after a person approves each time; they can hand it on once more."
    )


def test_the_history_cleanup_switch_gives_and_removes_only_the_persons_own_grant(harness: Harness) -> None:
    """Settings > Access: TeamLead may clean up the person's own history without asking (never handed on)."""

    before = harness.view("member-1")["history_cleanup"]
    assert before["available"] is True and before["on"] is False and before["grant_id"] is None
    assert "Remembered facts are kept" in before["description"]

    turned_on = asyncio.run(harness.map.set_history_cleanup("member-1", True))
    assert turned_on["history_cleanup"]["on"] is True
    grant = harness.agents.service.store.get(turned_on["history_cleanup"]["grant_id"])
    assert grant is not None and grant.target.id == "fn:history_cleanup"
    assert grant.for_person_id == "member-1" and grant.delegation_depth == 0
    assert grant.subject == SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, LEAD))
    assert harness.view("member-2")["history_cleanup"]["on"] is False, "it serves only the person who gave it"
    rows = holder(harness.view("member-1"), f"agent_type:{LEAD}")["grants"]
    assert any(row["target"] == "fn:history_cleanup" for row in rows), "listed with TeamLead access, with Remove"
    assert asyncio.run(harness.map.set_history_cleanup("member-1", True))["history_cleanup"]["grant_id"] == grant.grant_id

    turned_off = asyncio.run(harness.map.set_history_cleanup("member-1", False))
    assert turned_off["history_cleanup"]["on"] is False
    assert harness.agents.service.store.get(grant.grant_id).status is GrantStatus.REVOKED


@pytest.mark.parametrize("level", [AccessLevel.READ, AccessLevel.WRITE])
def test_cleanup_switch_upgrades_narrower_grants_and_can_remove_them(harness: Harness, level: AccessLevel) -> None:
    person = PersonPrincipal("member-1", TEAM, ("member",))
    lead = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEAM, LEAD))
    target = TargetRefV1(kind=TargetKind.TOOL, id="fn:history_cleanup")
    narrow = person_grant(harness, by=person, to=lead, target=target, level=level, for_person_id=person.person_id)
    assert harness.view(person.person_id)["history_cleanup"]["on"] is False

    enabled = asyncio.run(harness.map.set_history_cleanup(person.person_id, True))["history_cleanup"]

    full = harness.agents.service.store.get(enabled["grant_id"])
    assert enabled["on"] is True and full is not None and full.level is AccessLevel.FULL
    assert harness.agents.service.store.get(narrow.grant_id).status is GrantStatus.REVOKED
    asyncio.run(harness.map.set_history_cleanup(person.person_id, False))
    narrow = person_grant(harness, by=person, to=lead, target=target, level=level, for_person_id=person.person_id)
    asyncio.run(harness.map.set_history_cleanup(person.person_id, False))
    assert harness.agents.service.store.get(narrow.grant_id).status is GrantStatus.REVOKED


def test_other_people_cannot_remove_a_persons_history_cleanup_permission(harness: Harness) -> None:
    turned_on = asyncio.run(harness.map.set_history_cleanup("member-1", True))
    grant_id = turned_on["history_cleanup"]["grant_id"]
    for person_id in ("member-2", "admin-1", "owner-1"):
        rows = holder(harness.view(person_id), f"agent_type:{LEAD}")["grants"]
        assert all(not row["can_revoke"] for row in rows if row["grant_id"] == grant_id)
        with pytest.raises(HandDownRefused):
            asyncio.run(harness.inbox.revoke_grant(person_id, grant_id))
    assert harness.agents.service.store.get(grant_id).status is GrantStatus.ACTIVE
    assert asyncio.run(harness.inbox.revoke_grant("member-1", grant_id)) == [grant_id]
