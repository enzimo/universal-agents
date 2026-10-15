"""People's blocks, with the real Cedar evaluator and SQLite.

A block stops an agent type, one agent or a person from a tool or one
operation from the next decision on, whatever grants exist; it is recorded in
the access store's event log, listed with grants, and removable. Covers who
may block and unblock, blocks on people (which also stop their agents), time
limits, requests refused while blocked, and ``assistant_cli access
block|unblock``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver
from access_control.operator_commands import describe_grant, list_grants, summarize_grant
from access_control.requests import AccessRequests
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeV1,
    AccessRequestV1,
    GrantKind,
    GrantStatus,
    GrantV1,
    OperationResolutionV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from storage.access_repository import AccessRepository, AccessStoreConflict
from storage.config import resolve_storage_settings

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
LEAD = "TeamArchitect"
OWNER = PersonPrincipal("owner-1", TEAM, ("owner",))
ADMIN = PersonPrincipal("admin-1", TEAM, ("admin",))
MEMBER = PersonPrincipal("member-1", TEAM, ("member",))
WORKER = AgentPrincipal("agent-web", "WebResearchAgent", "worker", TEAM)
WORKER_TYPE = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent")
WEB = TargetRefV1(kind=TargetKind.TOOL, id="fn:web_research")
EXA = TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#exa_search")
START = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now


@dataclass
class RecordingInbox:
    reviews: list[AccessRequestV1] = field(default_factory=list)

    def open_review(self, request: AccessRequestV1) -> None:
        self.reviews.append(request)

    def send_notice(self, notice: AccessNoticeV1) -> None:
        pass

    def request_finished(self, request: AccessRequestV1) -> None:
        pass


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def requests(tmp_path: Path, clock: Clock):
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    service = AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        clock=clock,
        agent_role_of=agent_role_resolver(LEAD),
    )
    yield AccessRequests(service=service, inbox=RecordingInbox(), lead_agent_type=LEAD)
    service.cedar.close()


def give(requests: AccessRequests, by: PersonPrincipal, subject: SubjectRefV1, target: TargetRefV1, **fields) -> GrantV1:
    return requests.service.grant_by_person(
        by,
        GrantV1(
            grant_id="new",
            team_id=TEAM,
            subject=subject,
            target=target,
            level=fields.pop("level", AccessLevel.READ),
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=by.person_id),
            created_at_utc=START,
            **fields,
        ),
    )


def decide(requests: AccessRequests, person: PersonPrincipal, operation: str = "exa_search"):
    level = requests.service.catalog.tools["fn:web_research"].operation(operation).level
    resolution = OperationResolutionV1(tool_id="fn:web_research", operation=operation, level=level, source="trusted")
    return requests.service.decide(agent=WORKER, person=person, resolution=resolution, task_id="task-1")


def test_a_block_wins_over_a_grant_at_the_next_decision_and_unblocking_restores_it(requests) -> None:
    give(requests, OWNER, WORKER_TYPE, WEB)
    assert decide(requests, OWNER).allowed

    block = requests.block(OWNER, subject=WORKER_TYPE, target=EXA, reason="Use Brave only")

    refused = decide(requests, OWNER)
    assert not refused.allowed
    assert f"block:{block.grant_id}" in refused.blocking_policy_ids
    assert "asking for access will not change that" in refused.next_step
    # The block covers one operation; the rest of the tool still works.
    assert decide(requests, OWNER, "brave_web_search_artifact").allowed
    assert block.kind is GrantKind.BLOCK and block.grant_id in {g.grant_id for g in list_grants(requests.service.store, TEAM)}
    assert "blocked from operation fn:web_research#exa_search" in describe_grant(block)
    assert "BLOCKED - WebResearchAgent may not use" in summarize_grant(requests, block)

    removed = requests.unblock(OWNER, block.grant_id)

    assert removed.status is GrantStatus.REVOKED
    assert decide(requests, OWNER).allowed
    events = requests.service.store.events(TEAM, grant_id=block.grant_id)
    assert [(event.kind, event.actor.id) for event in events] == [
        ("grant_created", "owner-1"),
        ("grant_revoked", "owner-1"),
    ]
    assert events[0].detail["grant_kind"] == "block"


def test_a_member_cannot_block_a_team_tool_but_may_block_what_they_could_give(requests) -> None:
    with pytest.raises(HandDownRefused, match="only access you could give"):
        requests.block(MEMBER, subject=WORKER_TYPE, target=WEB, reason="Stop")

    give(requests, OWNER, SubjectRefV1(kind=SubjectKind.USER, id=MEMBER.person_id), WEB, delegation_depth=1)
    member_block = requests.block(MEMBER, subject=WORKER_TYPE, target=WEB, reason="Not for my work")
    admin_block = requests.block(ADMIN, subject=WORKER_TYPE, target=EXA, reason="Emergency stop")

    # Removing a block widens access: holding access one could give is not enough.
    with pytest.raises(HandDownRefused, match="person who added this block"):
        requests.unblock(MEMBER, admin_block.grant_id)
    requests.unblock(MEMBER, member_block.grant_id)
    requests.unblock(OWNER, admin_block.grant_id)
    with pytest.raises(HandDownRefused, match="already revoked"):
        requests.unblock(OWNER, admin_block.grant_id)


def test_revoke_does_not_remove_blocks_and_what_blocks_may_name(requests) -> None:
    block = requests.block(ADMIN, subject=WORKER_TYPE, target=WEB, reason="Stop")

    with pytest.raises(HandDownRefused, match="is a block; remove it with unblock"):
        requests.service.revoke_by_person(OWNER, block.grant_id)
    # The whole team and whole tool sets can be blocked (decision D14: internet off),
    # but only by an owner or administrator when people are covered.
    team = SubjectRefV1(kind=SubjectKind.TEAM, id=TEAM)
    internet = TargetRefV1(kind=TargetKind.TOOL_SET, id="internet")
    with pytest.raises(HandDownRefused, match="Only an owner or an administrator"):
        requests.block(MEMBER, subject=team, target=internet, reason="x")
    team_block = requests.block(OWNER, subject=team, target=internet, reason="No internet for now")
    assert team_block.subject == team
    with pytest.raises(HandDownRefused, match="tool set, a tool, one of its operations, a folder or a file"):
        requests.block(
            OWNER, subject=WORKER_TYPE, target=TargetRefV1(kind=TargetKind.CREDENTIAL, id="cred-1"), reason="x"
        )
    with pytest.raises(HandDownRefused, match="not in the tool catalog"):
        requests.block(OWNER, subject=WORKER_TYPE, target=TargetRefV1(kind=TargetKind.TOOL, id="fn:nope"), reason="x")
    with pytest.raises(ValueError, match="Say why"):
        requests.block(OWNER, subject=WORKER_TYPE, target=WEB, reason="  ")


def test_blocking_a_person_stops_their_agents_and_only_administrators_block_people(requests) -> None:
    member_subject = SubjectRefV1(kind=SubjectKind.USER, id=MEMBER.person_id)
    give(requests, OWNER, member_subject, WEB)
    give(requests, OWNER, WORKER_TYPE, WEB)
    assert decide(requests, MEMBER).allowed

    with pytest.raises(HandDownRefused, match="Only an owner or an administrator can block a person"):
        requests.block(
            PersonPrincipal("member-2", TEAM, ("member",)), subject=member_subject, target=WEB, reason="x"
        )
    with pytest.raises(HandDownRefused, match="cannot block yourself"):
        requests.block(OWNER, subject=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id), target=WEB, reason="x")
    requests.block(OWNER, subject=member_subject, target=WEB, reason="Left the project")

    refused = decide(requests, MEMBER)
    assert not refused.allowed
    assert "is blocked from" in refused.reason and "remove the block" in refused.next_step
    # Other people's tasks are unaffected.
    assert decide(requests, OWNER).allowed


def test_a_time_limited_block_ends_by_itself(requests, clock: Clock) -> None:
    give(requests, OWNER, WORKER_TYPE, WEB)
    block = requests.block(OWNER, subject=WORKER_TYPE, target=WEB, reason="Cool down", hours=2)
    assert block.expires_at_utc == START + timedelta(hours=2)
    assert not decide(requests, OWNER).allowed

    clock.now = START + timedelta(hours=2, minutes=1)

    assert decide(requests, OWNER).allowed
    with pytest.raises(ValueError, match="hours between"):
        requests.block(OWNER, subject=WORKER_TYPE, target=WEB, reason="x", hours=0)


def _stored_block(requests: AccessRequests, target: TargetRefV1, **fields) -> GrantV1:
    """A block as an older version stored it: written to the store directly, without today's catalog check."""

    return requests.service.store.create(
        GrantV1(
            grant_id=fields.pop("grant_id", "grant_old_block"),
            team_id=TEAM,
            kind=GrantKind.BLOCK,
            subject=WORKER_TYPE,
            target=target,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            reason="Use Brave only",
            created_at_utc=START,
            **fields,
        )
    )


def test_a_block_on_an_operation_an_update_removed_blocks_the_whole_tool(requests, caplog) -> None:
    give(requests, OWNER, WORKER_TYPE, WEB)
    renamed = _stored_block(requests, TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#old_exa_search"))

    with caplog.at_level("WARNING", logger="access_control.stale_blocks"):
        first, second = decide(requests, OWNER), decide(requests, OWNER, "brave_web_search_artifact")

    # Matching only the old name would allow everything again; it fails closed instead.
    assert not first.allowed and not second.allowed
    assert f"block:{renamed.grant_id}" in second.blocking_policy_ids
    assert requests.service.blocks_on(WORKER, WEB) == (renamed.grant_id,)
    warnings = [record for record in caplog.records if "event=stale_operation_block_applied" in record.getMessage()]
    assert len(warnings) == 1, "logged once per block, not on every decision"
    # Stored as its author chose; removing it restores the tool.
    assert requests.service.store.get(renamed.grant_id).target.id == "fn:web_research#old_exa_search"
    requests.unblock(OWNER, renamed.grant_id)
    assert decide(requests, OWNER).allowed


def test_administrators_hear_once_about_each_block_on_a_removed_operation(requests) -> None:
    from access_control.stale_blocks import report_stale_blocks

    store, catalog = requests.service.store, requests.service.catalog
    renamed = _stored_block(requests, TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#old_exa_search"))
    _stored_block(requests, EXA, grant_id="grant_current_block")
    ended = _stored_block(
        requests, TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#older"), grant_id="grant_ended_block"
    )
    store.revoke(ended.grant_id, actor=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id), at=START)

    assert report_stale_blocks(catalog, store, TEAM, now=START) == [renamed.grant_id]
    assert report_stale_blocks(catalog, store, TEAM, now=START) == [], "once per block"

    [(notice, _read)] = store.notices_for(TEAM, person_id=ADMIN.person_id, roles=ADMIN.roles)
    assert notice.kind.value == "block_widened" and notice.recipient_roles == ("admin",)
    assert notice.title == f"A block now stops WebResearchAgent from using all of {catalog.tools['fn:web_research'].display_name}"
    assert "old_exa_search" in notice.summary and "now blocks all of fn:web_research" in notice.summary
    assert "/access block agent-type:WebResearchAgent fn:web_research#" in notice.summary
    assert f"/access unblock {renamed.grant_id}" in notice.summary
    assert f"--replace {renamed.grant_id}`" in notice.summary
    assert "exa_search" in notice.summary.split("has these operations:", 1)[1]
    assert store.notices_for(TEAM, person_id=MEMBER.person_id, roles=MEMBER.roles) == []


def test_a_blocked_agent_asking_for_access_is_refused_without_a_request(requests) -> None:
    requests.block(OWNER, subject=WORKER_TYPE, target=WEB, reason="Stop")

    with pytest.raises(HandDownRefused, match="WebResearchAgent is blocked from"):
        requests.submit(requester=WORKER, person=OWNER, target=WEB, level=AccessLevel.READ, reason="Research")

    assert requests.inbox.reviews == []
    assert requests.service.store.list_requests(TEAM) == []


def test_a_team_block_can_be_replaced_while_its_administrator_is_blocked(requests, clock) -> None:
    from custom_messages.contracts.access_v2 import GrantLifetime

    subject = SubjectRefV1(kind=SubjectKind.TEAM, id=TEAM)
    old = _stored_block(requests, TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#old_exa_search"))
    requests.service.store.revoke(old.grant_id, actor=OWNER.subjects()[0], at=clock())
    old = requests.service.store.create(old.model_copy(update={
        "grant_id": "stale_team_block", "subject": subject,
        "lifetime": GrantLifetime.UNTIL, "expires_at_utc": START + timedelta(hours=2),
    }))
    assert not requests.service.may_give(ADMIN, EXA, AccessLevel.READ, task_id=None)
    assert not decide(requests, ADMIN, "brave_web_search_artifact").allowed
    clock.now += timedelta(minutes=30)

    replacement = requests.block(
        ADMIN, subject=subject, target=EXA, reason="Keep Exa blocked", replace_block_id=old.grant_id,
    )

    assert replacement.expires_at_utc == old.expires_at_utc
    assert replacement.created_at_utc == clock()
    assert requests.service.store.get(old.grant_id).status is GrantStatus.REVOKED
    assert not decide(requests, ADMIN).allowed
    assert decide(requests, ADMIN, "brave_web_search_artifact").allowed
    event = requests.service.store.events(TEAM, grant_id=old.grant_id)[-1]
    assert event.actor.id == ADMIN.person_id and event.detail == {"replaced_by": replacement.grant_id}


@pytest.mark.parametrize("change", ["actor", "subject", "tool", "whole_tool", "expiry", "current", "expired", "revoked"])
def test_replacement_requires_removal_authority_and_a_current_operation_of_the_same_tool(requests, clock, change) -> None:
    old = _stored_block(requests, TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#old_exa_search"))
    arguments = dict(subject=old.subject, target=EXA, reason="Replace", replace_block_id=old.grant_id)
    actor = ADMIN
    if change == "actor":
        actor = MEMBER
    elif change == "subject":
        arguments["subject"] = SubjectRefV1(kind=SubjectKind.AGENT, id="another-agent")
    elif change == "tool":
        arguments["target"] = TargetRefV1(kind=TargetKind.OPERATION, id="fn:access#request_access")
    elif change == "whole_tool":
        arguments["target"] = WEB
    elif change == "expiry":
        arguments["hours"] = 3
    elif change in {"current", "expired"}:
        requests.service.store.revoke(old.grant_id, actor=OWNER.subjects()[0], at=clock())
        updated = {"grant_id": "other-block"}
        if change == "current":
            updated["target"] = EXA
        else:
            updated["expires_at_utc"] = START - timedelta(hours=1)
        old = requests.service.store.create(old.model_copy(update=updated))
        arguments["replace_block_id"] = old.grant_id
    elif change == "revoked":
        requests.service.store.revoke(old.grant_id, actor=OWNER.subjects()[0], at=clock())
    version = requests.service.store.version(TEAM)
    with pytest.raises((HandDownRefused, ValueError)):
        requests.block(actor, **arguments)
    assert requests.service.store.version(TEAM) == version


def test_failed_replacement_keeps_the_old_block_and_rolls_back_its_events(requests, monkeypatch) -> None:
    old = _stored_block(requests, TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#old_exa_search"))
    store = requests.service.store
    version, events = store.version(TEAM), store.events(TEAM)
    insert = store._insert_grant
    observer = AccessRepository(store.settings)

    def fail_after_insert(conn, replacement):
        insert(conn, replacement)
        # Another connection still sees the committed old block throughout.
        assert observer.get(old.grant_id).status is GrantStatus.ACTIVE
        assert observer.get(replacement.grant_id) is None
        raise RuntimeError("insertion failed")

    monkeypatch.setattr(store, "_insert_grant", fail_after_insert)
    with pytest.raises(RuntimeError, match="insertion failed"):
        requests.block(ADMIN, subject=old.subject, target=EXA, reason="Replace", replace_block_id=old.grant_id)
    assert store.list_team(TEAM) == [old]
    assert store.events(TEAM) == events and store.version(TEAM) == version
    assert not decide(requests, ADMIN).allowed


def test_a_second_replacement_cannot_use_a_stale_read_of_the_old_block(requests) -> None:
    old = _stored_block(requests, TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#old_exa_search"))
    new = requests.block(ADMIN, subject=old.subject, target=EXA, reason="Replace", replace_block_id=old.grant_id)
    store = requests.service.store
    version = store.version(TEAM)
    with pytest.raises(AccessStoreConflict, match="already changed"):
        store.replace_block(old, new.model_copy(update={"grant_id": "losing-replacement"}))
    assert store.get("losing-replacement") is None
    assert store.version(TEAM) == version


def test_the_cli_blocks_and_unblocks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import assistant_cli
    from access_control.operator_commands import open_access_service
    from assistant_gateway.bootstrap import seed_default_gateway_user
    from assistant_gateway.config import GatewayInstanceConfig
    from assistant_gateway.store import GatewayStateStore

    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="cli-blocks", instance_root=str(root), team_id="team_cli", timezone="UTC"
    )
    config.authorization.cedar_binary_path = str(BINARY)
    config_path = root / "instance_config.yaml"
    config.save(config_path)
    settings = config.state_storage_settings
    seed_default_gateway_user(config, state_store=GatewayStateStore(settings.sqlite_path or "", storage_settings=settings))

    def run(*argv: str) -> str:
        args = assistant_cli.build_parser().parse_args(["access", *argv, "--config", str(config_path)])
        assistant_cli._access_command(args)
        return capsys.readouterr().out

    blocked = run(
        "block", "--agent-type", "WebResearchAgent", "--operation", "fn:web_research#exa_search", "--reason", "Brave only"
    )
    assert "Blocked " in blocked and "blocked from operation fn:web_research#exa_search" in blocked
    service = open_access_service(
        instance_root=config.instance_root_path,
        team_id="team_cli",
        storage_settings=config.manager_storage_settings,
        cedar_binary=str(BINARY),
    )
    try:
        (block,) = list_grants(service.store, "team_cli")
    finally:
        service.cedar.close()
    assert block.kind is GrantKind.BLOCK and block.granted_by.id == config.user.canonical_user_id
    assert block.grant_id in run("list", "WebResearchAgent")

    with pytest.raises(SystemExit, match="exactly one of --agent-type"):
        run("block", "--tool", "fn:web_research", "--reason", "x")
    assert "Removed " in run("unblock", block.grant_id)
    assert "No grants." in run("list")
    stale = service.store.create(block.model_copy(update={
        "grant_id": "stale-cli-team-block", "subject": SubjectRefV1(kind=SubjectKind.TEAM, id="team_cli"),
        "target": TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#old_exa_search"),
    }))
    replaced = run(
        "block", "--team", "--operation", "fn:web_research#exa_search", "--reason", "Exa only",
        "--replace", stale.grant_id,
    )
    assert f"Replaced block {stale.grant_id}:" in replaced
    assert service.store.get(stale.grant_id).status is GrantStatus.REVOKED
    [current] = service.store.for_status("team_cli", (GrantStatus.ACTIVE,))
    assert current.target == EXA


@pytest.mark.asyncio
async def test_the_gateway_reports_stale_blocks_at_start_and_a_failed_check_is_logged(requests, caplog) -> None:
    from assistant_gateway import gateway as gateway_module

    renamed = _stored_block(requests, TargetRefV1(kind=TargetKind.OPERATION, id="fn:web_research#old_exa_search"))

    class Inbox:
        def __init__(self, store) -> None:
            self.store = store

        def catalog(self):
            return requests.service.catalog

        def access_store(self):
            return self.store

    await gateway_module._report_stale_blocks(Inbox(requests.service.store), TEAM)
    [(notice, _read)] = requests.service.store.notices_for(TEAM, person_id=ADMIN.person_id, roles=ADMIN.roles)
    assert renamed.grant_id in notice.summary

    with caplog.at_level("ERROR", logger="assistant_gateway.gateway"):
        await gateway_module._report_stale_blocks(Inbox(None), TEAM)
    assert any("event=stale_block_check_failed" in record.getMessage() for record in caplog.records)
