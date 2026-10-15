"""Removing a person from the team (``/user remove``) and adding them back.

Real SQLite gateway and access stores. Covers the rules in
``access_control.people.removal_refusal``, the store's last-owner and
last-administrator guard, what removal changes (membership, sign-ins, the
access store's copy), that a removed person can no longer use the team from
chat channels or Web Chat, adding them back, and ``/user list``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from access_control.people import removal_refusal
from assistant_gateway.application.commands.context import GatewayActorContext
from assistant_gateway.application.commands.identity import IdentityCommandHandler, IdentityCommandRequest
from assistant_gateway.application.identity_service import GatewayIdentityService, GatewayIdentitySettings
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestContextSettings,
    GatewayRequestRoute,
)
from assistant_gateway.domain.identity import TeamRoleHolderRequired
from assistant_gateway.runtime_ports import MembershipRoleIdentityAuthorizer
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.access_v2 import PersonStanding
from storage.access_repository import AccessRepository
from tests.people_directory_support import people_directory

TEAM = "team_1"
KEEP = (("owner",), ("owner", "admin"))


class _Clock:
    def now_utc(self) -> datetime:
        return datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


class _Secrets:
    def __init__(self) -> None:
        self._count = 0

    def hash_secret(self, raw_secret: str) -> str:
        return f"hashed:{raw_secret}"

    def new_web_user_token(self) -> str:
        self._count += 1
        return f"uauser_{self._count}"

    def new_web_user_invite(self) -> str:
        self._count += 1
        return f"uainvite_{self._count}"

    def new_link_code(self) -> str:
        return "LINKCODE1"


def _team(
    tmp_path: Path, roles: dict[str, str]
) -> tuple[GatewayIdentityService, GatewayStateStore, AccessRepository]:
    """People with roles, each bound to telegram as ``tg_<id>``."""

    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    with store.identity_unit_of_work() as unit_of_work:
        for person_id, role in roles.items():
            unit_of_work.identity.create_gateway_user(
                canonical_user_id=person_id, display_name=person_id.title(), timezone="UTC"
            )
            unit_of_work.identity.create_team_membership(canonical_user_id=person_id, team_id=TEAM, role=role)
            unit_of_work.identity.create_channel_binding(
                canonical_user_id=person_id, channel_name="telegram", external_user_id=f"tg_{person_id}"
            )
        unit_of_work.commit()
    directory, access = people_directory(tmp_path, store.identity_repository, team_id=TEAM, clock=_Clock())
    service = GatewayIdentityService(
        repository=store.identity_repository,
        unit_of_work_factory=store,
        authorizer=MembershipRoleIdentityAuthorizer(),
        secrets=_Secrets(),
        clock=_Clock(),
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
        people_directory=directory,
    )
    return service, store, access


def _remove(service: GatewayIdentityService, actor: str, target: str) -> str:
    return service.remove_user(actor=service.resolve_user_by_id(actor), target=target, channel_name=None, chat_scope="").text


def _membership_status(store: GatewayStateStore, person_id: str) -> str:
    (membership,) = store.list_team_memberships(person_id)
    return membership.status


def test_removal_rules() -> None:
    assert removal_refusal(actor_id="m", actor_roles=("member",), target_id="x", target_role="member") == (
        "Only an owner or an administrator can remove people."
    )
    assert removal_refusal(actor_id="a", actor_roles=("admin",), target_id="x", target_role="member") is None
    assert removal_refusal(actor_id="a", actor_roles=("admin",), target_id="o", target_role="owner") == (
        "Only an owner can remove an owner."
    )
    assert removal_refusal(actor_id="o", actor_roles=("owner",), target_id="p", target_role="owner") is None
    assert "cannot remove yourself" in removal_refusal(actor_id="o", actor_roles=("owner",), target_id="o", target_role="owner")


def test_who_may_remove_whom(tmp_path: Path) -> None:
    service, store, _access = _team(tmp_path, {"olive": "owner", "otto": "owner", "adam": "admin", "mia": "member", "max": "member"})

    assert _remove(service, "mia", "max") == "Only an owner or an administrator can remove people."
    assert _remove(service, "adam", "olive") == "Only an owner can remove an owner."
    assert "cannot remove yourself" in _remove(service, "olive", "olive")
    assert _remove(service, "adam", "nobody") == "User not found: nobody"
    assert _remove(service, "adam", "max").startswith("Removed Max (max) from the team.")
    assert _remove(service, "adam", "max") == "Max (max) was already removed from the team."
    assert _remove(service, "olive", "otto").startswith("Removed Otto (otto)")
    assert (_membership_status(store, "olive"), _membership_status(store, "mia")) == ("active", "active")


def test_the_store_keeps_the_last_owner_and_the_last_administrator(tmp_path: Path) -> None:
    _service, owners, _access = _team(tmp_path / "owners", {"olive": "owner", "otto": "owner", "adam": "admin"})
    owners.remove_team_membership(canonical_user_id="otto", team_id=TEAM, keep_holders_of=KEEP)
    with pytest.raises(TeamRoleHolderRequired) as last_owner:
        owners.remove_team_membership(canonical_user_id="olive", team_id=TEAM, keep_holders_of=KEEP)
    assert last_owner.value.roles == ("owner",)
    assert _membership_status(owners, "olive") == "active"

    _service, admins, _access = _team(tmp_path / "admins", {"adam": "admin", "ann": "admin"})
    admins.remove_team_membership(canonical_user_id="ann", team_id=TEAM, keep_holders_of=KEEP)
    with pytest.raises(TeamRoleHolderRequired) as last_admin:
        admins.remove_team_membership(canonical_user_id="adam", team_id=TEAM, keep_holders_of=KEEP)
    assert last_admin.value.roles == ("owner", "admin")
    assert admins.remove_team_membership(canonical_user_id="nobody", team_id=TEAM, keep_holders_of=KEEP) is None


def test_a_removal_made_at_the_same_moment_as_another_change_is_refused(tmp_path: Path) -> None:
    service, store, _access = _team(tmp_path, {"olive": "owner", "otto": "owner"})
    olive = service.resolve_user_by_id("olive")
    # Olive was disabled a moment ago by someone else; her removal of Otto,
    # the only other owner, must not leave the team without an active owner.
    store.set_gateway_user_status("olive", "disabled")

    reply = service.remove_user(actor=olive, target="otto", channel_name=None, chat_scope="").text

    assert reply == "The team must keep at least one owner. Make someone else an owner first."
    assert _membership_status(store, "otto") == "active"


def test_removal_ends_every_way_in_and_records_the_new_standing(tmp_path: Path) -> None:
    service, store, access = _team(tmp_path, {"olive": "owner", "mia": "member"})
    mia = service.resolve_user_by_id("mia")
    token = service.issue_web_token(canonical_user_id="mia", label="laptop")
    olive = service.resolve_user_by_id("olive")
    service.create_web_invite(
        actor=olive, target="mia", display_name=None, label="phone", create_new=False, channel_name=None, chat_scope=""
    )
    service.create_link_code(resolved_user=mia, channel_name="telegram")
    context = GatewayRequestContextService(
        settings=GatewayRequestContextSettings(
            instance_name="test",
            team_id=TEAM,
            default_canonical_user_id="olive",
            default_display_name="Olive",
            default_timezone="UTC",
            heartbeat_source_id=None,
            users_enabled=True,
        ),
        identity=service,
    )
    telegram = GatewayRequestRoute(channel_name="telegram", user_id="tg_mia", chat_id="chat_mia")
    assert token is not None and context.admit_interactive_route(telegram).allowed

    reply = _remove(service, "olive", "mia")

    # What follows for her work and records is person_lifecycle's (tests/test_person_departure.py).
    assert reply == "Removed Mia (mia) from the team. They can no longer use it, and their sign-ins ended."
    # The membership is kept for history, marked removed; the person record is untouched.
    assert _membership_status(store, "mia") == "removed"
    assert store.get_gateway_user("mia").status == "active"
    assert (access.team_person(TEAM, "mia").standing, access.team_person(TEAM, "mia").roles) == (PersonStanding.REMOVED, ())
    # Web Chat: the token, the session check and the unused invite all refuse.
    assert service.resolve_web_token(token.raw_token) is None
    assert not service.is_web_chat_user_active("mia")
    assert service.consume_web_invite("uainvite_2") is None
    # Chat channels: the identity is still bound but resolves to nobody and is refused.
    assert service.resolve_canonical_user(channel_name="telegram", external_user_id="tg_mia") is None
    admission = context.admit_interactive_route(telegram)
    assert (admission.allowed, admission.rejection_code) == (False, "not_team_member")
    assert context.team_roles("mia") == ()
    # The link code made before removal no longer links a new identity.
    assert "invalid, expired" in service.link_code_for_channel(
        code="LINKCODE1", channel_name="signal", external_user_id="+1555", chat_scope="", thread_id=None
    )
    with sqlite3.connect(tmp_path / "gateway_state.db") as conn:
        (payload,) = conn.execute(
            "SELECT payload_json FROM gateway_user_events WHERE event_type = 'team_membership_remove'"
        ).fetchone()
    assert json.loads(payload) == {
        "actor_canonical_user_id": "olive",
        "team_id": TEAM,
        "previous_role": "member",
        "web_tokens_revoked": 1,
        "web_invites_expired": 1,
        "link_codes_expired": 1,
    }


def test_enable_and_disable_explain_that_they_do_not_apply_to_removed_people(tmp_path: Path) -> None:
    service, _store, _access = _team(tmp_path, {"olive": "owner", "mia": "member"})
    _remove(service, "olive", "mia")
    olive = service.resolve_user_by_id("olive")

    enabled = service.set_user_enabled_status(actor=olive, target="mia", enabled=True, channel_name=None, chat_scope="").text
    disabled = service.set_user_enabled_status(actor=olive, target="mia", enabled=False, channel_name=None, chat_scope="").text

    assert enabled.startswith("Mia (mia) was removed from the team; /user enable is only for disabled people.")
    assert "/user invite mia" in enabled
    assert disabled == "Mia (mia) was removed from the team, so there is nothing to disable."


def test_disable_says_what_it_did_and_names_the_person(tmp_path: Path) -> None:
    service, _store, _access = _team(tmp_path, {"olive": "owner", "mia": "member"})

    change = service.set_user_enabled_status(
        actor=service.resolve_user_by_id("olive"), target="mia", enabled=False, channel_name=None, chat_scope=""
    )

    assert change.text == "Disabled Mia (mia). They cannot use the team until an administrator enables them again."
    assert (change.person_id, change.standing, change.previous) == ("mia", PersonStanding.DISABLED, PersonStanding.ACTIVE)


def test_the_usual_add_flow_adds_a_removed_person_back_as_a_member(tmp_path: Path) -> None:
    service, store, access = _team(tmp_path, {"olive": "owner", "adam": "admin"})
    olive = service.resolve_user_by_id("olive")
    _remove(service, "olive", "adam")
    store.set_gateway_user_status("adam", "disabled")

    change = service.register_user_for_channel(
        actor=olive, display_name="Adam", channel_name="telegram", external_user_id="tg_adam", chat_scope="", thread_id=None
    )

    assert change.text == "Added Adam (adam) back to the team as a member."
    assert (change.person_id, change.standing, change.previous) == ("adam", PersonStanding.ACTIVE, PersonStanding.REMOVED)
    # The same person, as a member (not their old role), and enabled again.
    (membership,) = store.list_team_memberships("adam")
    assert (membership.role, membership.status, store.get_gateway_user("adam").status) == ("member", "active", "active")
    assert service.resolve_canonical_user(channel_name="telegram", external_user_id="tg_adam") is not None
    assert access.team_person(TEAM, "adam").standing is PersonStanding.ACTIVE
    # An identity bound to someone still on the team is not re-added.
    assert "already bound to user adam" in service.register_user_for_channel(
        actor=olive, display_name="Adam", channel_name="telegram", external_user_id="tg_adam", chat_scope="", thread_id=None
    ).text


def test_a_web_invite_adds_a_removed_person_back(tmp_path: Path) -> None:
    service, store, _access = _team(tmp_path, {"olive": "owner", "mia": "member"})
    olive = service.resolve_user_by_id("olive")
    _remove(service, "olive", "mia")

    reply = service.create_web_invite(
        actor=olive, target="mia", display_name=None, label="laptop", create_new=False, channel_name=None, chat_scope=""
    ).text

    assert reply.startswith("Added Mia (mia) back to the team as a member.")
    assert "Web Chat invitation for mia (Mia)." in reply
    assert _membership_status(store, "mia") == "active"
    invite_code = reply.split("Invite code: ", 1)[1].splitlines()[0]
    assert service.consume_web_invite(invite_code) is not None


def test_user_list_shows_where_each_person_stands(tmp_path: Path) -> None:
    service, store, _access = _team(tmp_path, {"olive": "owner", "mia": "member", "dora": "member"})
    _remove(service, "olive", "mia")
    store.set_gateway_user_status("dora", "disabled")
    olive = service.resolve_user_by_id("olive")

    everyone = service.render_user_list(actor=olive)
    removed = service.render_user_list(actor=olive, status="removed")
    active = service.render_user_list(actor=olive, status="active")

    assert everyone.startswith("Registered users: 3")
    assert "status: removed from the team" in everyone and "memberships: team_1:member:removed" in everyone
    assert "status: disabled" in everyone
    assert removed.startswith("Registered users (removed): 1") and "canonical_user_id: mia" in removed
    assert active.startswith("Registered users (active): 1") and "canonical_user_id: olive" in active
    assert service.render_user_list(actor=olive, status="gone") == "Usage: /user list [active|disabled|removed]"


@pytest.mark.asyncio
async def test_the_user_remove_command(tmp_path: Path) -> None:
    service, store, _access = _team(tmp_path, {"olive": "owner", "mia": "member"})

    class Operations:
        async def remove_gateway_user(self, **request: object) -> str:
            return _remove(service, str(request["canonical_user_id"]), str(request["target"]))

    handler = IdentityCommandHandler(Operations())  # type: ignore[arg-type]
    actor = GatewayActorContext(channel_name="web", canonical_user_id="olive")

    reply = await handler.execute(IdentityCommandRequest(command="/user remove mia", actor=actor))
    usage = await handler.execute(IdentityCommandRequest(command="/user remove", actor=actor))

    assert reply.startswith("Removed Mia (mia)") and _membership_status(store, "mia") == "removed"
    assert usage.startswith("Usage: /user remove")
