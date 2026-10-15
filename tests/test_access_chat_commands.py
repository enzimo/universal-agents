"""The ``/access`` chat command with the real Cedar evaluator and SQLite.

The command runs as the signed-in person with their current team roles, on
the gateway Inbox's access service: an owner gives, lists, revokes, blocks and
unblocks; a member stays within their own authority and sees only agents'
access and their own; waiting requests come with the Inbox link; with
authorization off the command says nothing is needed. The last test runs it
through the whole gateway command application.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver
from access_control.inbox import StoredAccessInbox
from access_control.operator_commands import list_grants
from access_control.requests import AccessRequests
from assistant_gateway.access_commands import GatewayAccessCommands
from assistant_gateway.access_inbox import GatewayAccessInbox, GatewayAccessInboxSettings
from assistant_gateway.access_setup import MembershipRoles
from assistant_gateway.application.commands.access import (
    ACCESS_AUTHORIZATION_OFF_NOTICE,
    AccessCommandHandler,
    AccessCommandRequest,
)
from assistant_gateway.application.commands.context import GatewayActorContext
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    GrantKind,
    OperationResolutionV1,
    TargetKind,
    TargetRefV1,
)
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
LEAD = "TeamArchitect"
# work_id: the request its task works on (task-only access is bound to it).
WORKER = AgentPrincipal("agent-web", "WebResearchAgent", "worker", TEAM, work_id="task-1")
LEAD_AGENT = AgentPrincipal("agent-lead", LEAD, "team_lead", TEAM, work_id="task-1")
INBOX_URL = "https://ua.example/webchat"
# A tool every person holds (the standard set) and agents need a grant for.
SCHEDULES = "fn:schedules"
SCHEDULES_READ = "prepare_scheduled_job_access"
# A tool members do not hold (in no standard set).
X_POSTS = "cli:x"

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


class Identity:
    """Active users and their team roles, as the gateway identity store keeps them."""

    def __init__(self, roles: dict[str, str]) -> None:
        self.roles = roles

    def get_gateway_user(self, person_id: str) -> Any:
        return SimpleNamespace(status="active") if person_id in self.roles else None

    def list_team_memberships(self, person_id: str, status: str = "active") -> list[Any]:
        role = self.roles.get(person_id)
        return [SimpleNamespace(team_id=TEAM, role=role)] if role else []


class NoChannelIdentities:
    def canonical_user_id_for_context(self, **_context: Any) -> str | None:
        return None


class World:
    def __init__(self, tmp_path: Path, *, enabled: bool = True) -> None:
        storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})

        async def emit(_event: Any) -> None:
            return None

        self.inbox = GatewayAccessInbox(
            settings=GatewayAccessInboxSettings(
                team_id=TEAM,
                instance_name="test",
                response_route="gateway-route",
                lead_agent_type=LEAD,
                inbox_url=INBOX_URL,
                instance_root=None,
                storage_settings=storage,
                cedar_binary=str(BINARY),
                authorization_enabled=True,
            ),
            memberships=MembershipRoles(Identity({"owner-1": "owner", "member-1": "member", "member-2": "member"})),
            manager_client=SimpleNamespace(connected=False),
            session_repository=SimpleNamespace(get_session_by_conversation_id=lambda _id: None),
            proactive_session=lambda: None,
            emit_delivery_event=emit,
        )
        self.handler = AccessCommandHandler(
            identity=NoChannelIdentities(),
            access=GatewayAccessCommands(
                requests=self.inbox.requests,
                person=self.inbox.person,
                review_url=self.inbox.review_url,
                authorization_enabled=enabled,
            ),
        )
        # The agents' side: its own service on the same store, as in an agent process.
        self.agents_service = AccessService(
            store=AccessRepository(storage),
            catalog=load_catalog(None),
            cedar=CedarAccessClient(binary=str(BINARY)),
            team_id=TEAM,
            agent_role_of=agent_role_resolver(LEAD),
        )
        self.agents = AccessRequests(
            service=self.agents_service, inbox=StoredAccessInbox(self.agents_service.store), lead_agent_type=LEAD
        )

    async def run(self, command: str, person: str | None = "owner-1") -> str:
        return await self.handler.execute(
            AccessCommandRequest(command=command, actor=GatewayActorContext(channel_name="website", canonical_user_id=person))
        )

    def worker_may_use_schedules(self, person: str = "owner-1", agent: AgentPrincipal = WORKER) -> bool:
        """Whether the agent (the worker by default) may prepare a schedule's access while working for ``person``.

        Schedules are in the standard tool set: every person holds them, and an
        agent needs a grant for the person's work (decision D13).
        """

        level = self.agents_service.catalog.tools[SCHEDULES].operation(SCHEDULES_READ).level
        return self.agents_service.decide(
            agent=agent,
            person=PersonPrincipal(person, TEAM, ("owner",) if person == "owner-1" else ("member",)),
            resolution=OperationResolutionV1(tool_id=SCHEDULES, operation=SCHEDULES_READ, level=level, source="trusted"),
            task_id="task-1",
        ).allowed

    def lead_may_use_schedules(self, person: str = "owner-1") -> bool:
        return self.worker_may_use_schedules(person, agent=LEAD_AGENT)

    def close(self) -> None:
        self.inbox.close()
        self.agents_service.cedar.close()


@pytest.fixture
def world(tmp_path: Path):
    world = World(tmp_path)
    yield world
    world.close()


@pytest.mark.asyncio
async def test_with_authorization_off_the_command_says_nothing_is_needed(tmp_path: Path) -> None:
    world = World(tmp_path, enabled=False)
    try:
        assert await world.run("/access list") == ACCESS_AUTHORIZATION_OFF_NOTICE
        assert await world.run("/access block agent-type:X fn:web_research --reason x") == ACCESS_AUTHORIZATION_OFF_NOTICE
        help_text = await world.run("/access")
        assert help_text.startswith(ACCESS_AUTHORIZATION_OFF_NOTICE) and "/access block" in help_text
        assert list_grants(world.agents_service.store, TEAM) == []
    finally:
        world.close()


@pytest.mark.asyncio
async def test_an_owner_gives_lists_and_revokes(world: World) -> None:
    # Workers keep no lasting access for a person's work ("access follows the work").
    refused = await world.run(f"/access grant agent-type:WebResearchAgent {SCHEDULES} read")
    assert refused.startswith("Not done: Workers keep no lasting access")
    assert not world.lead_may_use_schedules()
    given = await world.run(
        f'/access grant agent-type:TeamArchitect {SCHEDULES} read --for 7d --reason "Research for Olive"'
    )

    assert given.startswith("Done. grant_") and "TeamArchitect has read access to" in given
    assert world.lead_may_use_schedules()
    # Without --everyone the grant serves the giver's own work only (decision D13).
    assert not world.lead_may_use_schedules(person="member-1")
    (grant,) = list_grants(world.agents_service.store, TEAM)
    assert grant.granted_by.id == "owner-1" and grant.reason == "Research for Olive"
    assert grant.for_person_id == "owner-1"
    listed = await world.run("/access list TeamArchitect")
    assert grant.grant_id in listed and "Given by person owner-1" in listed
    assert "No grants or blocks about cli:google" in await world.run("/access list cli:google")

    removed = await world.run(f"/access revoke {grant.grant_id}")

    assert removed == f"Removed {grant.grant_id}. It applies from the next action."
    assert not world.lead_may_use_schedules()

    # An administrator may give agents access for everyone's work.
    await world.run(f"/access grant agent-type:WebResearchAgent {SCHEDULES} read --everyone")
    (everyone,) = list_grants(world.agents_service.store, TEAM)
    assert everyone.for_person_id is None
    assert world.worker_may_use_schedules() and world.worker_may_use_schedules(person="member-1")


@pytest.mark.asyncio
async def test_giving_more_replaces_what_was_there_and_giving_less_changes_nothing(world: World) -> None:
    await world.run(f'/access grant agent-type:TeamArchitect {SCHEDULES} read --for 7d --reason "First"')
    broader = await world.run(f'/access grant agent-type:TeamArchitect {SCHEDULES} write --reason "More"')
    assert broader.startswith("Done.")

    (grant,) = list_grants(world.agents_service.store, TEAM)
    assert grant.level.value == "write", "one entry: the broader grant replaced the narrower one"
    listed = await world.run("/access list TeamArchitect")
    assert listed.startswith("Grants and blocks about TeamArchitect (1):")

    again = await world.run(f'/access grant agent-type:TeamArchitect {SCHEDULES} read --reason "Less"')
    assert again.startswith("Already given; nothing changed.") and grant.grant_id in again
    assert [g.grant_id for g in list_grants(world.agents_service.store, TEAM)] == [grant.grant_id]


@pytest.mark.asyncio
async def test_a_member_stays_within_their_authority_and_sees_agents_access_and_their_own(world: World) -> None:
    # A member cannot give what they do not hold, nor give agents access for everyone's work.
    refused = await world.run(f"/access grant agent-type:TeamArchitect {X_POSTS} read", person="member-1")
    assert refused.startswith(f"Not done: You cannot give read access to {X_POSTS}")
    everyone = await world.run(
        f"/access grant agent-type:WebResearchAgent {SCHEDULES} read --everyone", person="member-1"
    )
    assert everyone == "Not done: Only an administrator can give agents access for everyone's work."
    assert list_grants(world.agents_service.store, TEAM) == []

    # What the member holds, they may give agents for their own work (decision D13).
    own = await world.run(f"/access grant agent-type:TeamArchitect {SCHEDULES} read", person="member-1")
    assert own.startswith("Done. grant_")
    (member_grant,) = list_grants(world.agents_service.store, TEAM)
    assert member_grant.for_person_id == "member-1"
    assert world.lead_may_use_schedules(person="member-1")
    assert not world.lead_may_use_schedules(person="member-2") and not world.lead_may_use_schedules()

    await world.run(f"/access grant agent-type:TeamArchitect {X_POSTS} read")
    await world.run(f"/access grant user:member-2 {X_POSTS} read")
    await world.run(f"/access grant user:member-1 {X_POSTS} read")

    member_view = await world.run("/access list", person="member-1")
    owner_view = await world.run("/access list")
    assert "(3):" in member_view and "person member-2" not in member_view and "person member-1 has" in member_view
    assert "(4):" in owner_view and "person member-2" in owner_view
    assert "not an active member" in await world.run("/access list", person="stranger")


@pytest.mark.asyncio
async def test_chat_can_atomically_replace_a_stale_team_block(world: World) -> None:
    from custom_messages.contracts.access_v2 import GrantV1, GrantStatus, SubjectKind, SubjectRefV1

    service = world.agents_service
    old = service.store.create(GrantV1(
        grant_id="stale-team-block", team_id=TEAM, kind=GrantKind.BLOCK,
        subject=SubjectRefV1(kind=SubjectKind.TEAM, id=TEAM),
        target=TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#old_exa_search"),
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id="owner-1"), created_at_utc=service.clock(),
    ))
    command = f'/access block team fn:web_research#exa_search --reason "Keep Exa blocked" --replace {old.grant_id}'
    assert (await world.run(command, person="member-1")).startswith("Not done:")
    answer = await world.run(command)
    assert answer.startswith(f"Replaced block {old.grant_id}.")
    assert service.store.get(old.grant_id).status is GrantStatus.REVOKED
    [replacement] = service.store.for_status(TEAM, (GrantStatus.ACTIVE,))
    assert replacement.target.id == "fn:web_research#exa_search"


@pytest.mark.asyncio
async def test_blocking_and_unblocking_from_chat(world: World) -> None:
    # A team default: the worker may use it for everyone's work.
    await world.run(f"/access grant agent-type:WebResearchAgent {SCHEDULES} read --everyone")
    assert world.worker_may_use_schedules()

    operation = f"{SCHEDULES}#{SCHEDULES_READ}"
    refused = await world.run(
        f'/access block agent-type:WebResearchAgent {operation} --reason "Not now"', person="member-1"
    )
    assert refused.startswith("Not done: You can block only access you could give")
    blocked = await world.run(f'/access block agent-type:WebResearchAgent {operation} --reason "Not now"')

    assert blocked.startswith("Blocked. grant_") and "It applies from the next action" in blocked
    assert not world.worker_may_use_schedules()
    (block,) = [g for g in list_grants(world.agents_service.store, TEAM) if g.kind is GrantKind.BLOCK]
    assert "BLOCKED - WebResearchAgent may not use" in await world.run("/access list")
    assert "is a block; remove it with unblock" in await world.run(f"/access revoke {block.grant_id}")
    assert "person who added this block" in await world.run(f"/access unblock {block.grant_id}", person="member-1")

    assert (await world.run(f"/access unblock {block.grant_id}")).startswith(f"Removed the block {block.grant_id}")
    assert world.worker_may_use_schedules()


@pytest.mark.asyncio
async def test_requests_lists_what_the_person_may_decide_with_the_inbox_link(world: World) -> None:
    def ask(person: PersonPrincipal, tool_id: str, reason: str):
        return world.agents.submit(
            requester=WORKER,
            person=person,
            target=TargetRefV1(kind=TargetKind.TOOL, id=tool_id),
            level=AccessLevel.READ,
            reason=reason,
        ).request

    owner = PersonPrincipal("owner-1", TEAM, ("owner",))
    member = PersonPrincipal("member-1", TEAM, ("member",))
    for_owner = ask(owner, SCHEDULES, "Look up the release notes")
    # Decision D13: the member holds schedules, so only the member decides for
    # their own work; they lack X, so an administrator decides that one.
    for_member = ask(member, SCHEDULES, "Plan the member's reminders")
    member_lacks = ask(member, X_POSTS, "Post the member's update")
    assert member_lacks.person_lacks_access and not for_member.person_lacks_access

    owner_view = await world.run("/access requests")
    member_view = await world.run("/access requests", person="member-1")
    other_member_view = await world.run("/access requests", person="member-2")

    assert for_owner.request_id in owner_view and "Look up the release notes" in owner_view
    assert member_lacks.request_id in owner_view and for_member.request_id not in owner_view
    assert f"Decide them in the Inbox: {INBOX_URL}?view=inbox" in owner_view
    assert for_member.request_id in member_view and "(1):" in member_view
    assert for_owner.request_id not in member_view and member_lacks.request_id not in member_view
    assert other_member_view == "No access requests are waiting for your decision."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected_start"),
    [
        ("/access grant agent-type:X", "Usage: /access grant"),
        ("/access block agent-type:X fn:web_research", "Usage: /access block"),
        ("/access grant agent-type:X fn:web_research read --forever 1", "Unknown option --forever"),
        ("/access grant agent-type:X fn:web_research read --delegate-depth one", "--delegate-depth takes a whole number"),
        ("/access revoke", "Usage: /access revoke <grant id>"),
        ("/access sideways", "Unknown /access command 'sideways'"),
        ('/access grant "unclosed', "Could not read the /access command"),
    ],
)
async def test_usage_mistakes_are_explained(world: World, command: str, expected_start: str) -> None:
    assert (await world.run(command)).startswith(expected_start)
    assert list_grants(world.agents_service.store, TEAM) == []


@pytest.mark.asyncio
async def test_unregistered_people_are_told_so(world: World) -> None:
    assert await world.run("/access list", person=None) == "Access commands are available only to registered users."


def test_web_chat_allows_the_command_and_every_help_lists_it() -> None:
    from assistant_gateway.application.command_help import build_interactive_session_help, build_web_chat_help
    from assistant_gateway.interactive_apps.website_support import _WEB_CHAT_ALLOWED_COMMANDS

    assert "/access" in _WEB_CHAT_ALLOWED_COMMANDS
    assert "/access [ list | grant | block | requests ... ]" in build_web_chat_help()
    help_text = build_interactive_session_help()
    assert "/access [list|grant|revoke|block|unblock|requests]" in help_text
    assert "/user role <usr_...> owner|admin|member" in help_text


def test_chat_app_help_lists_the_file_sharing_commands() -> None:
    """`/share`, `/shares`, `/unshare` and `/send` work in chat apps, so their `/help` names them."""

    from assistant_gateway.application.command_help import build_interactive_session_help

    help_text = build_interactive_session_help()
    assert "/share <file or folder> with <person> [read|write] [--for 7d]" in help_text
    assert "/shares - list what you share; /unshare <share id>" in help_text
    assert "/send <file> to <person|team/folder> [--move]" in help_text


@pytest.mark.asyncio
async def test_access_and_user_role_through_the_gateway_command_application(tmp_path: Path) -> None:
    """The gateway seeds its user as owner and serves /access on the Inbox's access service."""

    from assistant_gateway.application.interactive_commands import GatewayCommandRequest
    from assistant_gateway.gateway import UserGateway
    from assistant_gateway.store import GatewayStateStore
    from tests.test_gateway_phase1 import _build_test_config, _CaptureGatewayManagerTransport

    config = _build_test_config(tmp_path)
    config.authorization.enabled = True
    config.authorization.cedar_binary_path = str(BINARY)
    config.authorization.starter_profile = "manual"
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = UserGateway(
        config, output_sink=None, state_store=store, manager_transport=_CaptureGatewayManagerTransport()
    )
    owner = config.user.canonical_user_id
    member = store.create_gateway_user(display_name="Mia Member", timezone="UTC")
    store.create_team_membership(canonical_user_id=member.canonical_user_id, team_id=config.gateway.team_id, role="member")

    async def run(text: str, person: str) -> str:
        result = await gateway.command_application.execute(
            GatewayCommandRequest(
                raw_text=text,
                current_session_key=None,
                channel_name="website",
                user_id=person,
                chat_id="web-chat",
                canonical_user_id=person,
            )
        )
        assert result.handled is True
        return str(result.text)

    try:
        assert [m.role for m in store.list_team_memberships(owner)] == ["owner"]
        given = await run(f"/access grant agent-type:TeamArchitect {X_POSTS} read", owner)
        assert given.startswith("Done. ")
        # A member holds no X access, so they cannot give it to agents.
        assert (await run(f"/access grant agent-type:TeamArchitect {X_POSTS} read", member.canonical_user_id)).startswith(
            "Not done: "
        )
        promoted = await run(f"/user role {member.canonical_user_id} admin", owner)
        assert "is now an administrator" in promoted
        # The new role applies at the next decision.
        assert (await run(f"/access block agent-type:WebResearchAgent {X_POSTS} --reason Stop", member.canonical_user_id)).startswith(
            "Blocked. "
        )
        assert "Only an owner can make someone an owner" in await run(f"/user role {member.canonical_user_id} owner", member.canonical_user_id)
    finally:
        gateway.access_inbox.close()
