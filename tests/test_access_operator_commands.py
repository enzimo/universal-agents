"""Operator grant commands with the real Cedar evaluator and SQLite store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import AgentPrincipal
from access_control.operator_commands import (
    GrantRequest,
    OperatorCommandError,
    build_grant_spec,
    describe_grant,
    list_grants,
    parse_subject,
    person_for,
)
from custom_messages.contracts.access_v2 import GrantLifetime, GrantStatus, SubjectKind
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team_ua"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


class Memberships:
    def __init__(self, roles: dict[str, tuple[str, ...]]) -> None:
        self._roles = roles

    def roles_of(self, person_id: str, team_id: str) -> tuple[str, ...] | None:
        return self._roles.get(person_id) if team_id == TEAM else None


MEMBERS = Memberships({"alice": ("owner",), "bob": ("member",)})


@pytest.fixture
def service(tmp_path):
    cedar = CedarAccessClient(binary=str(BINARY))
    service = AccessService(
        store=AccessRepository(
            resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
        ),
        catalog=load_catalog(None),
        cedar=cedar,
        team_id=TEAM,
        clock=lambda: NOW,
    )
    yield service
    cedar.close()


def give(service, by: str, **fields) -> object:
    request = GrantRequest(**{"tool": None, "tool_set": None, **fields})
    person = person_for(MEMBERS, by, TEAM)
    return service.grant_by_person(person, build_grant_spec(request, team_id=TEAM, by=by, now=NOW))


def test_owner_gives_an_agent_type_read_access_that_then_decides(service) -> None:
    # A worker type gets lasting access only as a team default (everyone's work);
    # for one person's work it gets access through its tasks ("access follows the work").
    with pytest.raises(HandDownRefused, match="Workers keep no lasting access"):
        give(service, "alice", to="agent-type:BackgroundOperationsAgent", tool="cli:google", level="read")
    grant = give(
        service, "alice", to="agent-type:BackgroundOperationsAgent", tool="cli:google", level="read", duration="7d",
        everyone=True,
    )

    assert grant.lifetime is GrantLifetime.UNTIL and grant.expires_at_utc == NOW + timedelta(days=7)
    assert "read on tool cli:google" in describe_grant(grant)
    agent = AgentPrincipal("agent_boa", "BackgroundOperationsAgent", "worker", TEAM)
    search = service.catalog.resolve_broker_action("google", "gog.exec", {"argv": ["gmail", "search", "x"]})
    assert service.decide(agent=agent, person=person_for(MEMBERS, "alice", TEAM), resolution=search, task_id="t").allowed
    assert [g.grant_id for g in list_grants(service.store, TEAM)] == [grant.grant_id]


def test_a_member_cannot_give_team_tools_and_non_members_are_refused(service) -> None:
    with pytest.raises(HandDownRefused, match="beyond your own authority"):
        give(service, "bob", to="agent-type:TeamArchitect", tool="cli:bitwarden", level="read")
    with pytest.raises(OperatorCommandError, match="not an active member"):
        person_for(MEMBERS, "mallory", TEAM)


def test_revocation_needs_the_giver_or_equal_authority_and_cascades(service) -> None:
    lead = give(
        service, "alice", to="agent-type:TeamArchitect", tool="cli:google", level="full", delegate_depth=1
    )
    # Alice's grant serves her own work (decision D13), so the team lead hands
    # it on only on a task for her, and the child serves her work too.
    assert lead.for_person_id == "alice"
    lead_agent = AgentPrincipal("agent_lead", "TeamArchitect", "team_lead", TEAM)
    # Workers get it for one task (the request "task-1").
    spec = build_grant_spec(
        GrantRequest(to="agent-type:BackgroundOperationsAgent", tool="cli:google", tool_set=None, level="read"),
        team_id=TEAM,
        by="alice",
        now=NOW,
    ).model_copy(update={"lifetime": GrantLifetime.TASK, "task_id": "task-1"})
    with pytest.raises(HandDownRefused, match="holds no read access"):
        service.hand_down(lead_agent, spec, task_id="task-1", person_id="bob")
    child = service.hand_down(lead_agent, spec, task_id="task-1", person_id="alice")
    assert child.for_person_id == "alice" and child.parent_grant_id == lead.grant_id

    with pytest.raises(HandDownRefused, match="Only the person who gave"):
        service.revoke_by_person(person_for(MEMBERS, "bob", TEAM), lead.grant_id)
    revoked = service.revoke_by_person(person_for(MEMBERS, "alice", TEAM), lead.grant_id)

    assert set(revoked) == {lead.grant_id, child.grant_id}
    assert list_grants(service.store, TEAM) == []
    assert {g.status for g in list_grants(service.store, TEAM, include_ended=True)} == {GrantStatus.REVOKED}


def test_argument_parsing_is_strict() -> None:
    assert parse_subject("team", TEAM).kind is SubjectKind.TEAM
    assert parse_subject("role:member", TEAM).id == f"{TEAM}/member"
    for bad in ("agent-type:", "group:x"):
        with pytest.raises(OperatorCommandError):
            parse_subject(bad, TEAM)
    with pytest.raises(OperatorCommandError, match="exactly one"):
        build_grant_spec(GrantRequest(to="team", tool=None, tool_set=None, level="read"), team_id=TEAM, by="a", now=NOW)
    with pytest.raises(OperatorCommandError, match="duration"):
        build_grant_spec(
            GrantRequest(to="team", tool="cli:google", tool_set=None, level="read", duration="soon"),
            team_id=TEAM,
            by="a",
            now=NOW,
        )


def test_a_person_removes_their_own_link_and_administrators_remove_any(service) -> None:
    from custom_messages.contracts.access_v2 import (
        AccountLinkStatus,
        AccountLinkV1,
        LinkOwnerKind,
        SubjectRefV1,
    )
    from access_control.operator_commands import describe_link, unlink

    def stored(owner_kind: LinkOwnerKind, owner_id: str, key: str) -> AccountLinkV1:
        return service.store.create_link(
            AccountLinkV1(
                link_id=f"account_link_{key}",
                team_id=TEAM,
                tool_id="cli:x",
                owner_kind=owner_kind,
                owner_id=owner_id,
                credential_key=key,
                created_by=SubjectRefV1(kind=SubjectKind.USER, id=owner_id),
                created_at_utc=NOW,
            )
        )

    bobs = stored(LinkOwnerKind.PERSON, "bob", "bob_x")
    teams = stored(LinkOwnerKind.TEAM, TEAM, "team_x")
    assert "cli:x uses person bob's credential bob_x" in describe_link(bobs)

    with pytest.raises(OperatorCommandError, match="team administrator"):
        unlink(service.store, person_for(MEMBERS, "bob", TEAM), teams.link_id, now=NOW)
    assert unlink(service.store, person_for(MEMBERS, "bob", TEAM), bobs.link_id, now=NOW).status is AccountLinkStatus.REVOKED
    assert unlink(service.store, person_for(MEMBERS, "alice", TEAM), teams.link_id, now=NOW).status is AccountLinkStatus.REVOKED
    assert service.store.list_links(TEAM) == []


BINARY = Path(__file__).resolve().parents[1] / "target" / "debug" / "ua-cedar-authorizer"


@pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_the_cli_lists_and_decides_waiting_access_requests(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import assistant_cli
    from access_control.entities import AgentPrincipal
    from access_control.inbox import StoredAccessInbox
    from access_control.requests import AccessRequests
    from assistant_gateway.bootstrap import seed_default_gateway_user
    from assistant_gateway.config import GatewayInstanceConfig
    from assistant_gateway.store import GatewayStateStore
    from custom_messages.contracts.access_v2 import AccessLevel, AccessRequestStatus, TargetKind, TargetRefV1

    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="cli-requests", instance_root=str(root), team_id="team_cli", timezone="UTC"
    )
    config.authorization.cedar_binary_path = str(BINARY)
    config_path = root / "instance_config.yaml"
    config.save(config_path)
    settings = config.state_storage_settings
    seed_default_gateway_user(config, state_store=GatewayStateStore(settings.sqlite_path or "", storage_settings=settings))
    service = assistant_cli_service(config)
    requests = AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type="TeamArchitect")
    waiting = requests.submit(
        requester=AgentPrincipal("agent-web", "WebResearchAgent", "worker", "team_cli"),
        person=None,
        target=TargetRefV1(kind=TargetKind.TOOL, id="fn:heartbeat"),
        level=AccessLevel.READ,
        reason="Check the heartbeat list",
    ).request
    service.cedar.close()

    def run(*argv: str) -> str:
        args = assistant_cli.build_parser().parse_args(["access", *argv, "--config", str(config_path)])
        assistant_cli._access_command(args)
        return capsys.readouterr().out

    listed = run("requests")
    assert waiting.request_id in listed and "WebResearchAgent asks for read access to" in listed
    decided = run("decide", waiting.request_id, "approve", "--note", "Fine")
    assert "granted (approve)" in decided
    assert "No access requests are waiting." in run("requests")
    stored = AccessRepository(config.manager_storage_settings).get_request(waiting.request_id)
    assert stored.status is AccessRequestStatus.GRANTED and stored.decision_note == "Fine"


def assistant_cli_service(config):
    from access_control.operator_commands import open_access_service

    return open_access_service(
        instance_root=config.instance_root_path,
        team_id=config.manager.team_id,
        storage_settings=config.manager_storage_settings,
        cedar_binary=config.authorization.cedar_binary_path,
        lead_agent_type=config.manager.named_agent_type,
    )
