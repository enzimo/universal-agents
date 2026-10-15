"""The gateway's copy of people's standing in the access store (``assistant_gateway.people_directory_sync``).

Real SQLite gateway and access stores. Covers how standing follows the
gateway's records, that every kind of change is copied after it is stored,
the full copy at gateway start (before the maintenance schedules are sent),
and that a failed write is logged without undoing the change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from access_control.people import person_standing
from access_control.people_directory import PeopleDirectory
from assistant_gateway.application.identity_service import (
    SCHEDULER_NOT_TOLD,
    GatewayIdentityService,
    GatewayIdentitySettings,
)
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.nats_manager_runtime import NatsGatewayManagerRuntime
from assistant_gateway.people_directory_sync import PeopleDirectorySync
from assistant_gateway.runtime_ports import MembershipRoleIdentityAuthorizer
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from storage.access_repository import AccessRepository
from tests.people_directory_support import people_directory

TEAM = "team_1"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

    def now_utc(self) -> datetime:
        return self.now


class _Secrets:
    def hash_secret(self, raw_secret: str) -> str:
        return f"hashed:{raw_secret}"

    def new_web_user_token(self) -> str:
        return "uauser_x"

    def new_web_user_invite(self) -> str:
        return "uainvite_x"

    def new_link_code(self) -> str:
        return "LINKCODE1"


def _identity(store: GatewayStateStore, directory: object, clock: _Clock) -> GatewayIdentityService:
    return GatewayIdentityService(
        repository=store.identity_repository,
        unit_of_work_factory=store,
        authorizer=MembershipRoleIdentityAuthorizer(),
        secrets=_Secrets(),
        clock=clock,
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
        people_directory=directory,  # type: ignore[arg-type]
    )


def _seed(store: GatewayStateStore, people: dict[str, tuple[str, str]], *, team: str = TEAM) -> None:
    """people: id -> (role, membership status); every person is active."""

    with store.identity_unit_of_work() as unit_of_work:
        for person_id, (role, status) in people.items():
            unit_of_work.identity.create_gateway_user(canonical_user_id=person_id, display_name=person_id.title(), timezone="UTC")
            unit_of_work.identity.create_team_membership(canonical_user_id=person_id, team_id=team, role=role, status=status)
        unit_of_work.commit()


def _copy(access: AccessRepository, person_id: str) -> tuple[PersonStanding, tuple[str, ...]] | None:
    record = access.team_person(TEAM, person_id)
    return (record.standing, tuple(record.roles)) if record is not None else None


def test_standing_follows_the_membership_then_the_person() -> None:
    assert person_standing(membership_status=None, person_status="active") is None
    assert person_standing(membership_status="active", person_status="active") is PersonStanding.ACTIVE
    assert person_standing(membership_status="active", person_status="disabled") is PersonStanding.DISABLED
    # A removed membership wins over a disabled person.
    assert person_standing(membership_status="removed", person_status="disabled") is PersonStanding.REMOVED
    # Any other membership status never lets the person act.
    assert person_standing(membership_status="suspended", person_status="active") is PersonStanding.DISABLED


def test_every_change_to_a_person_is_copied_after_it_is_stored(tmp_path: Path) -> None:
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    _seed(store, {"olive": ("owner", "active")})
    clock = _Clock()
    directory, access = people_directory(tmp_path, store.identity_repository, team_id=TEAM, clock=clock)
    service = _identity(store, directory, clock)
    with store.identity_unit_of_work() as unit_of_work:
        unit_of_work.identity.create_channel_binding(canonical_user_id="olive", channel_name="telegram", external_user_id="tg_olive")
        unit_of_work.commit()
    olive = service.resolve_user_by_id("olive")

    service.register_user_for_channel(
        actor=olive, display_name="Mia", channel_name="telegram", external_user_id="tg_mia", chat_scope="", thread_id=None
    )
    mia = store.get_active_channel_binding(channel_name="telegram", external_user_id="tg_mia").canonical_user_id
    assert _copy(access, mia) == (PersonStanding.ACTIVE, ("member",))
    first_seen = access.team_person(TEAM, mia).changed_at_utc

    clock.now += timedelta(minutes=5)
    service.set_user_role(actor=olive, target=mia, role="owner", channel_name=None, chat_scope="")
    assert _copy(access, mia) == (PersonStanding.ACTIVE, ("admin", "owner"))
    assert access.team_person(TEAM, mia).changed_at_utc > first_seen

    service.set_user_enabled_status(actor=olive, target=mia, enabled=False, channel_name=None, chat_scope="")
    assert _copy(access, mia) == (PersonStanding.DISABLED, ())
    service.set_user_enabled_status(actor=olive, target=mia, enabled=True, channel_name=None, chat_scope="")
    assert _copy(access, mia) == (PersonStanding.ACTIVE, ("admin", "owner"))

    service.remove_user(actor=olive, target=mia, channel_name=None, chat_scope="")
    assert _copy(access, mia) == (PersonStanding.REMOVED, ())
    service.register_user_for_channel(
        actor=olive, display_name="Mia", channel_name="telegram", external_user_id="tg_mia", chat_scope="", thread_id=None
    )
    assert _copy(access, mia) == (PersonStanding.ACTIVE, ("member",))

    service.create_web_invite(
        actor=olive, target=None, display_name="Webby", label="Laptop", create_new=True, channel_name=None, chat_scope=""
    )
    (webby,) = [user.canonical_user_id for user in store.list_gateway_users() if user.display_name == "Webby"]
    assert _copy(access, webby) == (PersonStanding.ACTIVE, ("member",))
    # The scheduler's reader sees the same copy.
    assert PeopleDirectory(access, TEAM).person(mia).active


def test_an_unchanged_person_keeps_their_change_time(tmp_path: Path) -> None:
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    _seed(store, {"olive": ("owner", "active")})
    clock = _Clock()
    directory, access = people_directory(tmp_path, store.identity_repository, team_id=TEAM, clock=clock)

    directory.record("olive")
    first = access.team_person(TEAM, "olive").changed_at_utc
    clock.now += timedelta(hours=1)
    directory.sync_all()
    directory.record("olive")

    assert access.team_person(TEAM, "olive").changed_at_utc == first


def test_the_full_copy_replaces_the_team_and_marks_it_synced(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    _seed(store, {"olive": ("owner", "active"), "adam": ("admin", "active"), "rex": ("member", "removed")})
    _seed(store, {"dora": ("member", "active")})
    store.set_gateway_user_status("dora", "disabled")
    _seed(store, {"otto": ("member", "active")}, team="other_team")
    clock = _Clock()
    directory, access = people_directory(tmp_path, store.identity_repository, team_id=TEAM, clock=clock)
    access.record_person(TeamPersonV1(team_id=TEAM, person_id="gone", standing=PersonStanding.ACTIVE, changed_at_utc=clock.now))
    assert PeopleDirectory(access, TEAM).synced() is False

    with caplog.at_level(logging.INFO, logger="assistant_gateway.people_directory_sync"):
        counts = directory.sync_all()

    assert counts == {PersonStanding.ACTIVE: 2, PersonStanding.DISABLED: 1, PersonStanding.REMOVED: 1}
    assert {person.person_id: (person.standing, tuple(person.roles)) for person in access.team_people(TEAM)} == {
        "adam": (PersonStanding.ACTIVE, ("admin",)),
        "dora": (PersonStanding.DISABLED, ()),
        "olive": (PersonStanding.ACTIVE, ("admin", "owner")),
        "rex": (PersonStanding.REMOVED, ()),
    }
    # Someone of another team is not recorded, and nobody the gateway no longer knows stays.
    assert access.team_person(TEAM, "otto") is None and access.team_person(TEAM, "gone") is None
    assert PeopleDirectory(access, TEAM).synced() is True
    assert "event=people_directory_synced team_id=team_1 active=2 disabled=1 removed=1" in caplog.text


class _BrokenStore:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError("access store unavailable")


def test_a_failed_write_is_logged_and_does_not_undo_the_change(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    _seed(store, {"olive": ("owner", "active"), "mia": ("member", "active")})
    clock = _Clock()
    directory = PeopleDirectorySync(
        identity=store.identity_repository, open_store=_BrokenStore, team_id=TEAM, clock=clock  # type: ignore[arg-type]
    )
    service = _identity(store, directory, clock)

    with caplog.at_level(logging.ERROR, logger="assistant_gateway.people_directory_sync"):
        disabled = service.set_user_enabled_status(
            actor=service.resolve_user_by_id("olive"), target="mia", enabled=False, channel_name=None, chat_scope=""
        ).text
        removed = service.remove_user(actor=service.resolve_user_by_id("olive"), target="mia", channel_name=None, chat_scope="").text
        synced = directory.sync_all()

    assert store.get_gateway_user("mia").status == "disabled"
    assert store.list_team_memberships("mia")[0].status == "removed"
    assert disabled.startswith("Disabled Mia (mia)") and disabled.endswith(SCHEDULER_NOT_TOLD)
    assert removed.startswith("Removed Mia (mia)") and removed.endswith(SCHEDULER_NOT_TOLD)
    assert synced is None
    failures = [record for record in caplog.records if "event=people_directory_write_failed" in record.getMessage()]
    assert [record.levelno for record in failures] == [logging.ERROR] * 3
    assert "person_id=mia" in failures[0].getMessage() and "person_id=(everyone)" in failures[2].getMessage()


@pytest.mark.asyncio
async def test_gateway_start_copies_everyone_before_sending_maintenance_schedules(tmp_path: Path) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
        manager_agent_id=None,
        heartbeat_interval_seconds=2,
    )
    adapter = SimpleNamespace(
        connect=AsyncMock(), subscribe_route=AsyncMock(), subscribe_subject=AsyncMock(), publish=AsyncMock(), close=AsyncMock()
    )
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_runtime=NatsGatewayManagerRuntime.from_gateway_config(config, adapter=adapter),
    )
    gateway._manager_client.discover_manager_id = AsyncMock(side_effect=TimeoutError("no manager"))
    access = AccessRepository(config.manager_storage_settings)
    seen_when_schedules_sent: list[object] = []

    async def ensure_all() -> None:
        seen_when_schedules_sent.append(access.team_person("team_test", config.user.canonical_user_id))

    gateway._maintenance_schedules.ensure_all = ensure_all  # type: ignore[method-assign]
    await gateway.connect(start_services=True)
    try:
        (first_user,) = seen_when_schedules_sent
        assert first_user is not None
        assert (first_user.standing, tuple(first_user.roles)) == (PersonStanding.ACTIVE, ("admin", "owner"))
        assert access.people_synced_at("team_test") is not None
    finally:
        await gateway.close()
