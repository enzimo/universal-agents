"""People sharing files and sending files to each other (decision D15), with real Cedar and SQLite.

A share is a grant only the owner can give; agents working for the recipient
use the shared item at the shared level and nobody else's agents do. Sending a
file into another person's folder waits for the recipient (or an
administrator), and the gateway copies exactly the content that was sent,
without following links.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver
from access_control.requests import AccessRequests
from assistant_gateway.file_sharing import FILE_DELIVERY_KIND, FileSharingRefused, GatewayFileSharing
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeKind,
    AccessNoticeV1,
    AccessRequestStatus,
    AccessRequestV1,
    AccessReviewChoice,
)
from runtime_settings.instance_layout import InstanceLayout, person_folder_name
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
LEAD = "TeamArchitect"
OWNER = PersonPrincipal("owner-1", TEAM, ("owner",))
ALICE = PersonPrincipal("alice", TEAM, ("member",))
BOB = PersonPrincipal("bob", TEAM, ("member",))
CAROL = PersonPrincipal("carol", TEAM, ("member",))
PEOPLE = {person.person_id: person for person in (OWNER, ALICE, BOB, CAROL)}
NAMES = {"owner-1": "Olive", "alice": "Alice", "bob": "Bob", "carol": "Carol"}
WORKER = AgentPrincipal("agent-web", "WebResearchAgent", "worker", TEAM)

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


@dataclass
class RecordingInbox:
    notices: list[AccessNoticeV1] = field(default_factory=list)
    finished: list[AccessRequestV1] = field(default_factory=list)

    def open_review(self, request: AccessRequestV1) -> None:
        return None

    def send_notice(self, notice: AccessNoticeV1) -> None:
        self.notices.append(notice)

    def request_finished(self, request: AccessRequestV1) -> None:
        self.finished.append(request)


@dataclass
class World:
    layout: InstanceLayout
    service: AccessService
    inbox: RecordingInbox
    sharing: GatewayFileSharing
    appliers: dict

    def requests(self) -> AccessRequests:
        return AccessRequests(service=self.service, inbox=self.inbox, lead_agent_type=LEAD, appliers=dict(self.appliers))

    def home(self, person: PersonPrincipal) -> Path:
        path = self.layout.person_dir(person.person_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def folder_use(self, person: PersonPrincipal, target: str, level: AccessLevel, *, is_file: bool = True) -> bool:
        return self.service.decide_folder(
            agent=WORKER, person=person, target_id=target, is_file=is_file, level=level, task_id="task-1"
        ).allowed


@pytest.fixture
def world(tmp_path: Path):
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    service = AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver(LEAD),
    )
    inbox = RecordingInbox()
    appliers: dict = {}
    holder: list[World] = []
    sharing = GatewayFileSharing(
        requests=lambda: holder[0].requests(),
        person=PEOPLE.get,
        workspaces_dir=layout.workspaces_dir,
        team_people=lambda: [(pid, NAMES[pid]) for pid in PEOPLE],
        display_name=lambda pid: NAMES.get(pid, pid),
        authorization_enabled=True,
    )
    appliers[FILE_DELIVERY_KIND] = sharing
    holder.append(World(layout, service, inbox, sharing, appliers))
    yield holder[0]
    service.cedar.close()


def alice_file(world: World, relative: str = "reports/q1.md", content: str = "quarter one") -> Path:
    path = world.home(ALICE) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_a_shared_folder_reaches_the_recipients_agents_at_the_shared_level_only(world: World) -> None:
    alice_file(world)
    reports = f"users/{person_folder_name('alice')}/reports"
    assert not world.folder_use(BOB, f"{reports}/q1.md", AccessLevel.READ), "private until shared"

    share = world.sharing.share("alice", "reports", "Bob", "read")

    assert share.subject.id == "bob" and share.target.id == reports and share.for_person_id is None
    assert world.folder_use(BOB, f"{reports}/q1.md", AccessLevel.READ)
    assert not world.folder_use(BOB, f"{reports}/q1.md", AccessLevel.WRITE), "shared to read only"
    assert not world.folder_use(CAROL, f"{reports}/q1.md", AccessLevel.READ), "only Bob's work"
    assert not world.folder_use(BOB, f"users/{person_folder_name('alice')}/private.md", AccessLevel.READ)
    [notice] = world.inbox.notices
    assert notice.kind is AccessNoticeKind.SHARED and notice.recipient_person_id == "bob"
    assert notice.grant_id is None, "the recipient cannot remove someone else's share"

    shares = world.sharing.shares("alice")
    assert [grant.grant_id for grant in shares["by_me"]] == [share.grant_id]
    assert [grant.grant_id for grant in world.sharing.shares("bob")["with_me"]] == [share.grant_id]
    with pytest.raises(FileSharingRefused):
        world.sharing.stop_sharing("bob", share.grant_id)
    world.sharing.stop_sharing("alice", share.grant_id)
    assert not world.folder_use(BOB, f"{reports}/q1.md", AccessLevel.READ)


def test_only_the_owner_shares_and_never_links_or_credentials(world: World) -> None:
    alice_file(world)
    alice_home = world.home(ALICE)
    (alice_home / ".env").write_text("SECRET=1", encoding="utf-8")
    os.symlink(world.layout.root / "team.env", alice_home / "link.md")
    reports = f"users/{person_folder_name('alice')}/reports"
    spec = world.sharing.share("alice", "reports/q1.md", "bob", "write")
    assert spec.target.kind.value == "file"
    shared_file = f"users/{person_folder_name('alice')}/reports/q1.md"
    assert world.folder_use(BOB, shared_file, AccessLevel.WRITE), "a shared file opens for the recipient's agents"
    assert not world.folder_use(BOB, f"users/{person_folder_name('alice')}/reports/q2.md", AccessLevel.READ)
    assert not world.folder_use(CAROL, shared_file, AccessLevel.READ)

    # An administrator cannot open someone else's folder, not even to share it.
    with pytest.raises(FileSharingRefused, match="own folder"):
        world.sharing.share("owner-1", reports, "bob", "read")
    from custom_messages.contracts.access_v2 import GrantV1, SubjectKind, SubjectRefV1, TargetKind, TargetRefV1

    forged = GrantV1(
        grant_id="new",
        team_id=TEAM,
        subject=SubjectRefV1(kind=SubjectKind.USER, id="bob"),
        target=TargetRefV1(kind=TargetKind.FOLDER, id=reports),
        level=AccessLevel.READ,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id="owner-1"),
        created_at_utc=world.service.clock(),
    )
    with pytest.raises(HandDownRefused, match="beyond your own authority"):
        world.service.grant_by_person(OWNER, forged)
    with pytest.raises(FileSharingRefused, match="credential"):
        world.sharing.share("alice", ".env", "bob", "read")
    with pytest.raises(FileSharingRefused, match="links are never shared"):
        world.sharing.share("alice", "link.md", "bob", "read")
    with pytest.raises(FileSharingRefused, match="That is you"):
        world.sharing.share("alice", "reports", "Alice", "read")


def test_sending_waits_for_the_recipient_and_delivers_exactly_what_was_sent(world: World) -> None:
    source = alice_file(world, "notes/plan.md", "the plan")
    message = world.sharing.send("alice", "notes/plan.md", "Bob", move=True)
    [request] = world.service.store.list_requests(TEAM, status=AccessRequestStatus.WAITING)
    assert "received/" in message and request.requested_by_person_id == "alice"
    requests = world.requests()
    assert requests.may_decide(BOB, request) and requests.may_decide(OWNER, request)
    assert not requests.may_decide(CAROL, request) and not requests.may_decide(ALICE, request)

    decided = requests.decide(request.request_id, BOB, AccessReviewChoice.APPROVE)

    delivered = world.home(BOB) / "received" / person_folder_name("alice") / "plan.md"
    assert decided.status is AccessRequestStatus.GRANTED and decided.applied_at_utc is not None
    assert delivered.read_text(encoding="utf-8") == "the plan"
    assert not source.exists(), "a move removes the original after the copy"
    assert any(notice.kind is AccessNoticeKind.FILE_RECEIVED for notice in world.inbox.notices)


def test_a_busy_database_while_delivering_removes_the_copy_and_a_later_deny_delivers_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before 2026-10-17 the copy was removed only when another decision won;
    # after a busy-store error it stayed in the recipient's folder.
    import sqlite3

    alice_file(world, "plan.md", "the plan")
    world.sharing.send("alice", "plan.md", "Bob")
    [request] = world.service.store.list_requests(TEAM)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(world.service.store, "finish_request", locked)
    with pytest.raises(sqlite3.OperationalError):
        world.requests().decide(request.request_id, BOB, AccessReviewChoice.APPROVE)
    monkeypatch.undo()

    received = world.home(BOB) / "received"
    assert not any(path.is_file() for path in received.rglob("*")), "the copy was removed"
    assert world.service.store.get_request(request.request_id).status is AccessRequestStatus.WAITING
    world.requests().decide(request.request_id, BOB, AccessReviewChoice.DENY)
    assert not any(path.is_file() for path in received.rglob("*"))


def test_a_file_changed_after_sending_is_not_delivered_and_the_team_folder_needs_no_approval(world: World) -> None:
    source = alice_file(world, "draft.md", "first")
    world.sharing.send("alice", "draft.md", "bob")
    [request] = world.service.store.list_requests(TEAM, status=AccessRequestStatus.WAITING)
    source.write_text("changed", encoding="utf-8")

    with pytest.raises(HandDownRefused, match="changed"):
        world.requests().decide(request.request_id, OWNER, AccessReviewChoice.APPROVE)
    assert not (world.home(BOB) / "received").exists()
    assert world.service.store.get_request(request.request_id).status is AccessRequestStatus.WAITING

    answer = world.sharing.send("alice", "draft.md", "team/handoff")
    assert (world.layout.team_dir / "handoff" / "draft.md").read_text(encoding="utf-8") == "changed"
    assert source.exists() and "Copied" in answer
    # A second copy never overwrites the first.
    world.sharing.send("alice", "draft.md", "team/handoff", move=True)
    assert (world.layout.team_dir / "handoff" / "draft (2).md").exists() and not source.exists()


def test_a_delivery_can_only_be_decided_where_it_is_applied(world: World) -> None:
    alice_file(world, "a.md", "x")
    world.sharing.send("alice", "a.md", "bob")
    [request] = world.service.store.list_requests(TEAM, status=AccessRequestStatus.WAITING)
    bare = AccessRequests(service=world.service, inbox=world.inbox, lead_agent_type=LEAD)

    with pytest.raises(HandDownRefused, match="Inbox"):
        bare.decide(request.request_id, BOB, AccessReviewChoice.APPROVE)
