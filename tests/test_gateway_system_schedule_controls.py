"""Who may change schedules the runtime manages from the gateway (access plan 4c-4).

The gateway checks, before it publishes a schedule command, whether the
signed-in person may make ``system_schedule`` changes (``PersonChangeCheck``:
the people who could approve one in the Inbox, so administrators with
authorization on; any active member with it off). The manager applies that
answer only to schedules the runtime manages (tests/test_manager_control_runtime.py).
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
from custom_messages.contracts.schedule_v1 import SYSTEM_SCHEDULE_CHANGE_KIND, ManagerScheduleCommand
from tests.change_approval_support import CEDAR_BINARY, needs_cedar

TEAM = "team_gateway_sched"
PERSON = "user_carol"


class _Memberships:
    def __init__(self, roles: tuple[str, ...]) -> None:
        self.roles = roles

    def roles_of(self, person_id: str, team_id: str) -> tuple[str, ...] | None:
        return self.roles if person_id == PERSON and team_id == TEAM else None


class _ManagerCommands:
    def __init__(self) -> None:
        self.commands: list[ManagerScheduleCommand] = []

    async def request(self, request: Any) -> ManagerScheduleCommand:
        self.commands.append(request.command)
        return request.command.model_copy(update={"success": True})


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


def _application(check: PersonChangeCheck | None, roles: tuple[str, ...]) -> tuple[GatewayScheduleApplication, _ManagerCommands]:
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
    commands = _ManagerCommands()
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
        manager_commands=commands,  # type: ignore[arg-type]
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

        await application.pause("hb_sched_team", paused_until="2026-09-26T00:00:00+00:00")
        await application.list()
    finally:
        check.close()

    pause, listing = commands.commands
    assert pause.action == "pause" and listing.action == "list"
    # Commands carry no authority: the manager decides with the signed person
    # (test_the_manager_decides_system_schedule_changes_from_the_signed_roles).
    assert "may_change_system_schedules" not in ManagerScheduleCommand.model_fields
    assert "principal" not in ManagerScheduleCommand.model_fields


@pytest.mark.asyncio
async def test_any_member_may_change_system_schedules_with_authorization_off(tmp_path: Path) -> None:
    check = PersonChangeCheck(_config(tmp_path, authorization_enabled=False), _Memberships(("member",)))
    application, commands = _application(check, ("member",))

    await application.remove("hb_sched_team")

    assert commands.commands[0].action == "remove"


def _manager(tmp_path: Path, *, authorization_enabled: bool) -> SimpleNamespace:
    config = _config(tmp_path, authorization_enabled=authorization_enabled)
    return SimpleNamespace(
        id="manager",
        team_id=TEAM,
        instance_root=str(config.instance_root_path),
        authorization_config={
            "enabled": authorization_enabled,
            "cedar_binary_path": str(CEDAR_BINARY),
        },
        task_manager=SimpleNamespace(storage_settings=config.manager_storage_settings),
    )


@needs_cedar
@pytest.mark.parametrize(("roles", "allowed"), [(("member",), False), (("admin",), True)])
def test_the_manager_decides_system_schedule_changes_from_the_signed_roles(
    tmp_path: Path, roles: tuple[str, ...], allowed: bool
) -> None:
    from custom_agents.access_runtime import _runtime_access
    from custom_agents.manager_control_runtime import CommandPerson, _may_change_system_schedules

    _runtime_access.cache_clear()
    try:
        person = CommandPerson(canonical_user_id=PERSON, team_id=TEAM, roles=roles)
        assert _may_change_system_schedules(_manager(tmp_path, authorization_enabled=True), person) is allowed
    finally:
        _runtime_access.cache_clear()


def test_with_authorization_off_any_signed_member_may_change_system_schedules(tmp_path: Path) -> None:
    from custom_agents.access_runtime import _runtime_access
    from custom_agents.manager_control_runtime import CommandPerson, _may_change_system_schedules

    _runtime_access.cache_clear()
    try:
        person = CommandPerson(canonical_user_id=PERSON, team_id=TEAM, roles=("member",))
        assert _may_change_system_schedules(_manager(tmp_path, authorization_enabled=False), person) is True
    finally:
        _runtime_access.cache_clear()
