"""The Access page's Files view and whose-work labels, with real Cedar and SQLite.

Files: what a person shared and what was shared with them (decision D15),
stopping a share, and the team's data folders (decision D16), which only
owners and administrators add and remove. By agent type: each grant says whose
work it serves, and a member never sees what another person authorized for
their own work (decision D13).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_control.entities import PersonPrincipal
from assistant_gateway.access_inbox import GatewayAccessInbox, GatewayAccessInboxSettings
from assistant_gateway.access_setup import MembershipRoles
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from runtime_settings.instance_layout import InstanceLayout
from storage.config import resolve_storage_settings

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
ROLES = {"owner-1": "owner", "member-1": "member", "member-2": "member"}
NAMES = {"owner-1": "Olive", "member-1": "Max", "member-2": "Mia"}

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


class Identity:
    def get_gateway_user(self, person_id: str) -> Any:
        return SimpleNamespace(status="active", display_name=NAMES.get(person_id, "")) if person_id in ROLES else None

    def list_team_memberships(self, person_id: str, status: str = "active") -> list[Any]:
        return [SimpleNamespace(team_id=TEAM, role=ROLES[person_id])] if person_id in ROLES else []

    def list_team_members(self, team_id: str) -> list[Any]:
        return [SimpleNamespace(canonical_user_id=pid, status="active") for pid in ROLES]


def make_inbox(tmp_path: Path, *, enabled: bool = True) -> SimpleNamespace:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()

    async def emit(_event: Any) -> None:
        return None

    inbox = GatewayAccessInbox(
        settings=GatewayAccessInboxSettings(
            team_id=TEAM,
            instance_name="test",
            response_route="gateway-route",
            lead_agent_type="TeamArchitect",
            inbox_url="https://ua.example/webchat",
            instance_root=str(layout.root),
            storage_settings=resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "m.db")}}),
            cedar_binary=str(BINARY),
            authorization_enabled=enabled,
            protected_locations=(layout.root,),
        ),
        memberships=MembershipRoles(Identity()),
        manager_client=SimpleNamespace(connected=False),
        session_repository=SimpleNamespace(get_session_by_conversation_id=lambda _id: None),
        proactive_session=lambda: None,
        emit_delivery_event=emit,
    )
    return SimpleNamespace(inbox=inbox, layout=layout, tmp=tmp_path)


@pytest.fixture
def inbox(tmp_path: Path):
    world = make_inbox(tmp_path)
    yield world
    world.inbox.close()


def files(world, person_id: str) -> dict[str, Any]:
    return asyncio.run(world.inbox.access_map.view(person_id, view="files"))


def test_the_files_view_lists_shares_both_ways_and_stops_a_share(inbox) -> None:
    home = inbox.layout.person_dir("member-1")
    (home / "reports").mkdir(parents=True)
    share = inbox.inbox.file_sharing.share("member-1", "reports", "Mia", "read")

    mine, theirs = files(inbox, "member-1"), files(inbox, "member-2")
    assert [row["share_id"] for row in mine["shared_by_you"]] == [share.grant_id]
    assert mine["shared_by_you"][0]["person"] == "Mia" and mine["shared_with_you"] == []
    assert [row["shared_by"] for row in theirs["shared_with_you"]] == ["Max"]
    assert files(inbox, "owner-1")["shared_by_you"] == [], "administrators do not see other people's shares here"

    with pytest.raises(ValueError):
        asyncio.run(inbox.inbox.access_map.stop_sharing("member-2", share.grant_id))
    asyncio.run(inbox.inbox.access_map.stop_sharing("member-1", share.grant_id))
    assert files(inbox, "member-2")["shared_with_you"] == []


def test_only_administrators_add_and_remove_data_folders(inbox) -> None:
    reports = inbox.tmp / "reports"
    reports.mkdir()
    access_map = inbox.inbox.access_map
    assert files(inbox, "member-1")["can_manage_folders"] is False
    with pytest.raises(ValueError, match="owner or an administrator"):
        asyncio.run(access_map.add_data_folder("member-1", "reports", str(reports), writable=False))
    with pytest.raises(ValueError, match="is inside"):
        asyncio.run(access_map.add_data_folder("owner-1", "instance", str(inbox.layout.root), writable=False))

    added = asyncio.run(access_map.add_data_folder("owner-1", "reports", str(reports), writable=False))

    assert "data/reports" in added["message"]
    [folder] = files(inbox, "member-1")["data_folders"]
    assert folder["folder_id"] == "data/reports" and folder["access_words"] == "read only"
    asyncio.run(access_map.remove_data_folder("owner-1", "reports"))
    assert files(inbox, "owner-1")["data_folders"] == []


def test_data_folders_are_listed_and_managed_with_access_control_off(tmp_path: Path) -> None:
    world = make_inbox(tmp_path, enabled=False)
    try:
        reports = tmp_path / "reports"
        reports.mkdir()
        added = asyncio.run(world.inbox.access_map.add_data_folder("owner-1", "reports", str(reports), writable=True))
        assert added["message"].endswith("Every agent can use it now.")
        page = files(world, "member-1")
        assert page["enabled"] is False and page["shared_by_you"] == [] and page["can_manage_folders"] is False
        assert [folder["access_words"] for folder in page["data_folders"]] == ["read and change"]
    finally:
        world.inbox.close()


def test_grants_say_whose_work_they_serve_and_members_see_only_their_own(inbox) -> None:
    service = inbox.inbox.requests().service
    # The team lead keeps each person's access (workers get it for their tasks).
    lead = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect")
    for person_id in ("member-1", "member-2"):
        person = PersonPrincipal(person_id, TEAM, ("member",))
        service.grant_by_person(
            person,
            GrantV1(
                grant_id="new",
                team_id=TEAM,
                subject=lead,
                target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"),
                level=AccessLevel.WRITE,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=person_id),
                created_at_utc=service.clock(),
                for_person_id=person_id,
            ),
        )

    def works(person_id: str) -> list[str]:
        page = asyncio.run(inbox.inbox.access_map.view(person_id, view="agents"))
        card = next(item for item in page["items"] if item["subject"] == "TeamArchitect")
        return sorted(grant["for_whose_work"] for grant in card["grants"] if grant["target"] == "fn:schedules")

    assert works("member-1") == ["your work"]
    assert works("member-2") == ["your work"]
    assert works("owner-1") == ["Max's work", "Mia's work"]
