"""People's team roles: the owner seed, ``/user role``, and gateway user administration.

Real SQLite through the gateway state store. Covers the role rules in
``access_control.people`` (only an owner changes owners; an administrator may
name the first owner of a team without one), the store's protection of the
last owner and last administrator, the audit record, the ``/user role``
command, owner protection when disabling people, and the identity authorizer
that replaced the old evaluator (owners and admins administer people).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from access_control.ownership import RESOURCE_FILE_PATH, RESOURCE_TEAM_MEMORY, WORKSPACE_FILE_READ, private_access
from access_control.people import (
    effective_roles,
    is_administrator,
    role_change_refusal,
    status_change_refusal,
)
from assistant_gateway.application.commands.context import GatewayActorContext
from assistant_gateway.application.commands.identity import IdentityCommandHandler, IdentityCommandRequest
from assistant_gateway.application.identity_service import GatewayIdentityService, GatewayIdentitySettings
from assistant_gateway.domain.identity import TeamRoleHolderRequired
from assistant_gateway.runtime_ports import MembershipRoleIdentityAuthorizer
from assistant_gateway.store import GatewayStateStore
from tests.people_directory_support import people_directory

TEAM = "team_1"


class _Clock:
    def now_utc(self) -> datetime:
        return datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


class _Secrets:
    def hash_secret(self, raw_secret: str) -> str:
        return f"hashed:{raw_secret}"

    def new_web_user_token(self) -> str:
        return "uauser_x"

    def new_web_user_invite(self) -> str:
        return "uainvite_x"

    def new_link_code(self) -> str:
        return "LINKCODE1"


def _people(tmp_path: Path, roles: dict[str, str]) -> tuple[GatewayIdentityService, GatewayStateStore]:
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    with store.identity_unit_of_work() as unit_of_work:
        for person_id, role in roles.items():
            unit_of_work.identity.create_gateway_user(
                canonical_user_id=person_id, display_name=person_id.title(), timezone="UTC"
            )
            unit_of_work.identity.create_team_membership(canonical_user_id=person_id, team_id=TEAM, role=role)
        unit_of_work.commit()
    directory, _people_copy = people_directory(tmp_path, store.identity_repository, team_id=TEAM, clock=_Clock())
    service = GatewayIdentityService(
        repository=store.identity_repository,
        unit_of_work_factory=store,
        authorizer=MembershipRoleIdentityAuthorizer(),
        secrets=_Secrets(),
        clock=_Clock(),
        people_directory=directory,
        settings=GatewayIdentitySettings(
            enabled=True,
            allow_newuser_registration=True,
            web_user_tokens_enabled=True,
            web_user_token_ttl_days=7,
            web_user_invite_ttl_seconds=900,
            link_code_ttl_seconds=600,
            default_timezone="UTC",
            team_id=TEAM,
            web_chat_url="https://agents.example.test/webchat",
        ),
    )
    return service, store


def _set_role(service: GatewayIdentityService, actor: str, target: str, role: str) -> str:
    return service.set_user_role(
        actor=service.resolve_user_by_id(actor), target=target, role=role, channel_name=None, chat_scope=""
    )


def _role(store: GatewayStateStore, person_id: str) -> str:
    (membership,) = store.list_team_memberships(person_id)
    return membership.role


def test_owner_implies_administrator_wherever_roles_are_checked() -> None:
    assert effective_roles(["Owner"]) == ("admin", "owner")
    assert is_administrator(("owner",)) and is_administrator(("admin",)) and not is_administrator(("member",))
    # The new model's private-record rules treat an owner as an administrator.
    assert private_access(
        person_id="o", owner_id=None, resource_type=RESOURCE_TEAM_MEMORY, action="private.update", team_id=TEAM, roles=("owner",)
    ).allowed
    assert private_access(
        person_id="o", owner_id=None, resource_type=RESOURCE_FILE_PATH, action=WORKSPACE_FILE_READ, team_id=TEAM, roles=("owner",)
    ).allowed


def test_role_rules() -> None:
    assert role_change_refusal(actor_roles=("member",), current_role="member", new_role="admin", team_has_owner=True)
    assert role_change_refusal(actor_roles=("admin",), current_role="member", new_role="admin", team_has_owner=True) is None
    assert "Only an owner" in role_change_refusal(
        actor_roles=("admin",), current_role="member", new_role="owner", team_has_owner=True
    )
    assert "Only an owner" in role_change_refusal(
        actor_roles=("admin",), current_role="owner", new_role="member", team_has_owner=True
    )
    # A team set up before owners existed: an administrator names the first owner.
    assert role_change_refusal(actor_roles=("admin",), current_role="admin", new_role="owner", team_has_owner=False) is None
    assert role_change_refusal(actor_roles=("owner",), current_role="member", new_role="guest", team_has_owner=True)
    assert "Only an owner" in status_change_refusal(actor_roles=("admin",), target_role="owner")
    assert status_change_refusal(actor_roles=("owner",), target_role="owner") is None


def test_gateway_user_administration_checks_match_the_old_rule_and_include_owners() -> None:
    """The old evaluator allowed these three actions to the admin role only."""

    authorizer = MembershipRoleIdentityAuthorizer()
    for check in (authorizer.can_create_user, authorizer.can_list_users):
        assert check(actor_canonical_user_id="a", team_id=TEAM, roles=("admin",))
        assert check(actor_canonical_user_id="a", team_id=TEAM, roles=("owner",))
        assert not check(actor_canonical_user_id="a", team_id=TEAM, roles=("member",))
        assert not check(actor_canonical_user_id="", team_id=TEAM, roles=("admin",))
    assert authorizer.can_create_web_invite(
        actor_canonical_user_id="a", target_canonical_user_id=None, team_id=TEAM, roles=("owner",)
    )
    assert not authorizer.can_create_web_invite(
        actor_canonical_user_id="a", target_canonical_user_id="b", team_id=TEAM, roles=("member",)
    )


def test_owners_and_admins_change_roles_within_the_rules_and_changes_are_recorded(tmp_path: Path) -> None:
    service, store = _people(tmp_path, {"olive": "owner", "adam": "admin", "mia": "member", "max": "member"})

    assert "Mia (mia) is now an administrator (was a member)" in _set_role(service, "olive", "mia", "admin")
    assert "Only an owner can make someone an owner" in _set_role(service, "adam", "max", "owner")
    assert "Only an owner can make someone an owner or change an owner's role" in _set_role(service, "adam", "olive", "admin")
    assert "Only an owner or an administrator" in _set_role(service, "max", "max", "admin")
    assert "already an administrator" in _set_role(service, "olive", "adam", "admin")
    assert "Usage: /user role" in _set_role(service, "olive", "max", "superuser")
    assert "User not found" in _set_role(service, "olive", "nobody", "admin")
    assert _set_role(service, "adam", "max", "admin").startswith("Max (max) is now an administrator")

    assert (_role(store, "mia"), _role(store, "max"), _role(store, "olive")) == ("admin", "admin", "owner")
    with sqlite3.connect(tmp_path / "gateway_state.db") as conn:
        rows = conn.execute(
            "SELECT canonical_user_id, payload_json FROM gateway_user_events WHERE event_type = 'team_role_change' "
            "ORDER BY rowid"
        ).fetchall()
    assert [(person, json.loads(payload)["actor_canonical_user_id"], json.loads(payload)["new_role"]) for person, payload in rows] == [
        ("mia", "olive", "admin"),
        ("max", "adam", "admin"),
    ]


def test_the_last_owner_and_the_last_administrator_cannot_be_removed(tmp_path: Path) -> None:
    service, store = _people(tmp_path, {"olive": "owner", "mia": "member"})

    assert "must keep at least one owner" in _set_role(service, "olive", "olive", "admin")
    assert _role(store, "olive") == "owner"

    _set_role(service, "olive", "mia", "owner")
    assert "is now an administrator" in _set_role(service, "mia", "olive", "admin")
    # The store refuses even when called directly (two changes at the same moment).
    with pytest.raises(TeamRoleHolderRequired):
        store.change_team_membership_role(
            canonical_user_id="mia", team_id=TEAM, role="member", keep_holders_of=(("owner",), ("owner", "admin"))
        )
    assert _role(store, "mia") == "owner"


def test_a_team_without_an_owner_lets_an_administrator_name_one(tmp_path: Path) -> None:
    service, store = _people(tmp_path, {"adam": "admin", "ann": "admin"})

    assert "is now a member" in _set_role(service, "adam", "ann", "member")
    assert "must keep at least one owner or administrator" in _set_role(service, "adam", "adam", "member")
    assert "is now an owner" in _set_role(service, "adam", "adam", "owner")
    assert (_role(store, "adam"), _role(store, "ann")) == ("owner", "member")
    # Once the team has an owner, only an owner changes owners.
    _set_role(service, "adam", "ann", "admin")
    assert "Only an owner" in _set_role(service, "ann", "ann", "owner")


def test_only_an_owner_disables_an_owner(tmp_path: Path) -> None:
    service, store = _people(tmp_path, {"olive": "owner", "otto": "owner", "adam": "admin", "mia": "member"})

    def disable(actor: str, target: str) -> str:
        return service.set_user_enabled_status(
            actor=service.resolve_user_by_id(actor), target=target, enabled=False, channel_name=None, chat_scope=""
        ).text

    assert "Only an owner can disable or enable an owner" in disable("adam", "olive")
    assert disable("adam", "mia").startswith("Disabled Mia (mia)")
    assert disable("otto", "olive").startswith("Disabled Olive (olive)")
    assert store.get_gateway_user("olive").status == "disabled"


@pytest.mark.asyncio
async def test_the_user_role_command(tmp_path: Path) -> None:
    service, store = _people(tmp_path, {"olive": "owner", "mia": "member"})

    class Operations:
        def set_gateway_user_role(self, **request: object) -> str:
            return _set_role(service, str(request["canonical_user_id"]), str(request["target"]), str(request["role"]))

    handler = IdentityCommandHandler(Operations())  # type: ignore[arg-type]
    actor = GatewayActorContext(channel_name="web", canonical_user_id="olive")

    reply = await handler.execute(IdentityCommandRequest(command="/user role mia admin", actor=actor))
    usage = await handler.execute(IdentityCommandRequest(command="/user role mia", actor=actor))

    assert "is now an administrator" in reply and _role(store, "mia") == "admin"
    assert usage.startswith("Usage: /user role")
