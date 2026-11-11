"""Who may change schedules the runtime manages (access plan 4c-4).

One rule, ``PersonChangeCheck``: the people who could approve a
``system_schedule`` change in the Inbox, so administrators with authorization
on; any active member with it off. The gateway uses it to offer controls; the
team services decide each change with it, with the person's current roles from
the people copy (``team_services.schedules.SystemScheduleCheck``, applied only to
schedules the runtime manages: tests/test_team_schedules.py). Commands carry no
authority of their own.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from assistant_gateway.access_setup import PersonChangeCheck
from assistant_gateway.application.schedule_management import (
    GatewayScheduleApplication,
    GatewayScheduleSettings,
)
from assistant_gateway.application.request_context import GatewayCommandPerson, GatewayRequestRoute
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.domain.sessions import SessionRecord
from access_control.people_directory import PeopleDirectory
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.schedule_v1 import SYSTEM_SCHEDULE_CHANGE_KIND
from custom_messages.wire.frame import body_model
from team_services.people import CommandPerson
from team_services.schedules import SystemScheduleCheck
from tests.change_approval_support import CEDAR_BINARY, needs_cedar
from tests.people_directory_support import access_store_at

TEAM = "team_gateway_sched"
PERSON = "user_carol"


class _Memberships:
    def __init__(self, roles: tuple[str, ...]) -> None:
        self.roles = roles

    def roles_of(self, person_id: str, team_id: str) -> tuple[str, ...] | None:
        return self.roles if person_id == PERSON and team_id == TEAM else None


class _TeamCommands:
    """The gateway's team commands port, recording each command's kind and body."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def request(self, kind: str, body: dict[str, Any], *, person_id: str) -> dict[str, Any]:
        assert person_id == PERSON
        body_model(kind)[0].validate_python({"request_id": "cmd-1", **body})
        self.commands.append((kind, body))
        if kind == "schedules.list":
            return {"outcome": "ok", "schedules": [], "total": 0}
        return {"outcome": "ok", "state": "removed", "schedule_id": body["schedule_id"]}


def _config(tmp_path: Path, *, authorization_enabled: bool) -> GatewayInstanceConfig:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="gateway-schedules",
        instance_root=str(root),
        team_id=TEAM,
        timezone="UTC",
        manager_storage_sqlite_path="runtime/task_history.db",
    )
    config.authorization.enabled = authorization_enabled
    config.authorization.cedar_binary_path = str(CEDAR_BINARY)
    return config


def _application(check: PersonChangeCheck | None, roles: tuple[str, ...]) -> tuple[GatewayScheduleApplication, _TeamCommands]:
    session = SessionRecord(
        session_key="session_carol",
        session_alias=None,
        conversation_id="conversation_carol",
        last_task_id=None,
        manager_agent_id="manager",
        channel_name="web",
        user_id=PERSON,
        chat_id=None,
        thread_id=None,
        status="active",
        created_at="2026-09-25T00:00:00+00:00",
        updated_at="2026-09-25T00:00:00+00:00",
        owner_canonical_user_id=PERSON,
    )
    commands = _TeamCommands()
    application = GatewayScheduleApplication(
        settings=GatewayScheduleSettings(
            instance_name="gateway-schedules",
            response_route="gateway",
            user_display_name="Carol",
            request_timezone="UTC",
            default_session_key="session_carol",
            default_owner_canonical_user_id=PERSON,
            manager_unavailable_text="unavailable",
        ),
        sessions=SimpleNamespace(resolve_session=lambda **_kwargs: session),
        request_context=SimpleNamespace(
            canonical_user_id=lambda route: PERSON,
            manager_principal=lambda route: GatewayCommandPerson(
                canonical_user_id=PERSON, team_id=TEAM, roles=tuple(roles)
            ),
            users_enabled=True,
        ),
        delivery=SimpleNamespace(remove_scheduled_target=lambda schedule_id: None),
        manager=SimpleNamespace(manager_id="manager"),
        team_commands=commands,  # type: ignore[arg-type]
        id_generator=SimpleNamespace(),  # type: ignore[arg-type]
        person_changes=check,
    )
    return application, commands


@needs_cedar
@pytest.mark.asyncio
@pytest.mark.parametrize(("roles", "allowed"), [(("member",), False), (("admin",), True)])
async def test_only_an_administrator_may_change_system_schedules_with_authorization_on(
    tmp_path: Path, roles: tuple[str, ...], allowed: bool
) -> None:
    check = PersonChangeCheck(_config(tmp_path, authorization_enabled=True), _Memberships(roles))
    try:
        assert check.may_change(PERSON, SYSTEM_SCHEDULE_CHANGE_KIND) is allowed
        application, commands = _application(check, roles)
        assert application.may_change_system_schedules(GatewayRequestRoute()) is allowed

        await application.pause("hb_sched_team", paused_until="2026-09-26T00:00:00+00:00",
                                expected_record_digest="a" * 64)
        await application.list(owner_wide=True)
    finally:
        check.close()

    # Commands carry no authority (their bodies are checked against the contract above):
    # the team services decide with the token's person
    # (test_the_team_services_decide_system_schedule_changes_from_current_roles).
    (pause, pause_body), (listing, _) = commands.commands
    assert (pause, listing) == ("schedules.pause", "schedules.list")
    assert pause_body == {"schedule_id": "hb_sched_team", "seen_version": "a" * 64,
                          "paused_until": "2026-09-26T00:00:00.000Z"}


@pytest.mark.asyncio
async def test_any_member_may_change_system_schedules_with_authorization_off(tmp_path: Path) -> None:
    check = PersonChangeCheck(_config(tmp_path, authorization_enabled=False), _Memberships(("member",)))
    application, commands = _application(check, ("member",))

    await application.remove("hb_sched_team", expected_record_digest="a" * 64)

    assert commands.commands[0][0] == "schedules.remove"


def _team_services_check(tmp_path: Path, roles: tuple[str, ...], *, authorization_enabled: bool) -> SystemScheduleCheck:
    from datetime import datetime, timezone

    config = _config(tmp_path, authorization_enabled=authorization_enabled)
    access = access_store_at(tmp_path)
    access.record_person(TeamPersonV1(team_id=TEAM, person_id=PERSON, person_ref="p-CarolCarolCarolCarolCa",
                                      standing=PersonStanding.ACTIVE, roles=roles,
                                      changed_at_utc=datetime.now(timezone.utc)))
    return SystemScheduleCheck(config, PeopleDirectory(access, TEAM))


@needs_cedar
@pytest.mark.parametrize(("roles", "allowed"), [(("member",), False), (("admin",), True)])
def test_the_team_services_decide_system_schedule_changes_from_current_roles(
    tmp_path: Path, roles: tuple[str, ...], allowed: bool
) -> None:
    check = _team_services_check(tmp_path, roles, authorization_enabled=True)
    try:
        # The command's roles do not count: the people copy's current roles do.
        assert check(CommandPerson(person_id=PERSON, team_id=TEAM, roles=("owner",))) is allowed
    finally:
        check.close()


def test_with_authorization_off_any_active_member_may_change_system_schedules(tmp_path: Path) -> None:
    check = _team_services_check(tmp_path, ("member",), authorization_enabled=False)

    assert check(CommandPerson(person_id=PERSON, team_id=TEAM, roles=("member",))) is True
    assert check(CommandPerson(person_id="user_unknown", team_id=TEAM, roles=("member",))) is False
