"""Adding a person: model key choice, shared folders, and the settings report (owner's request 2026-10-02).

Real gateway identity service and stores, real access store, real folders.
Covered:

* a new person gets the team default for the team's model keys, or the
  administrator's ``--team-keys`` / ``--own-keys`` choice, and the reply ends
  with a report (role, how they were added, timezone, folders, model keys,
  own model);
* ``--shared <name>`` creates ``team/<name>`` and records it; everyone has
  ``team/team-shared``; a link planted at ``team/<name>`` is not followed;
* ``/user team-keys`` is for administrators only and changes the switch;
* ``/user status`` shows the person's model settings and shared folders;
* people added before the switch existed keep the team's keys.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path


from access_catalog.catalog import CatalogSource
from access_control.account_sharing import TeamAccountSharing
from access_control.person_settings import PersonSettings
from assistant_gateway.application.command_operations import (
    GatewayCommandOperationsApplication,
)
from assistant_gateway.application.identity_service import GatewayIdentityService, GatewayIdentitySettings
from assistant_gateway.application.person_lifecycle import GatewayPersonLifecycle, GatewayPersonLifecycleSettings
from assistant_gateway.application.person_setup import PersonSetupChoices
from assistant_gateway.people_directory_sync import PeopleDirectorySync
from assistant_gateway.person_setup import GatewayPersonSetupStore
from assistant_gateway.personal_setup import GatewayPersonalSetup, PersonalSetupSteps
from assistant_gateway.runtime_ports import MembershipRoleIdentityAuthorizer
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.access_v2 import AccountSharing
from runtime_settings.instance_layout import InstanceLayout, person_folder_name
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

TEAM = "team_setup"
ADMIN = "usr_admin"
MEMBER = "usr_member"


class _Clock:
    def now_utc(self):
        from datetime import datetime, timezone

        return datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class _Secrets:
    def hash_secret(self, raw: str) -> str:
        return f"hash:{raw}"

    def new_web_user_invite(self) -> str:
        return "uainv_test_invite"

    def new_web_user_token(self) -> str:
        return "uauser_test_token"

    def new_link_code(self) -> str:
        return "LINK1234"


class _Output:
    """The gateway's delivery output: keeps what was posted in a chat."""

    def __init__(self, posted: list) -> None:
        self.posted = posted

    async def emit(self, event) -> None:
        self.posted.append(event)


class World:
    def __init__(self, tmp_path: Path, *, model_keys: AccountSharing | None = AccountSharing.PRIVATE) -> None:
        """``model_keys``: the team's model-key sharing an administrator chose; None leaves it unchosen (shared)."""

        self.layout = InstanceLayout.at(tmp_path / "instance")
        self.layout.ensure_directories()
        storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(self.layout.task_database_path)}})
        self.gateway = GatewayStateStore(str(tmp_path / "gateway_state.db"))
        with self.gateway.identity_unit_of_work() as unit_of_work:
            for person_id, role in ((ADMIN, "admin"), (MEMBER, "member")):
                unit_of_work.identity.create_gateway_user(canonical_user_id=person_id, display_name=person_id, timezone="UTC")
                unit_of_work.identity.create_team_membership(canonical_user_id=person_id, team_id=TEAM, role=role)
            unit_of_work.commit()
        self.access = AccessRepository(storage)
        if model_keys is not None:
            TeamAccountSharing(self.access, TEAM).choose_model_keys(model_keys, by=ADMIN)
        self.posted: list = []
        self.personal_setup = GatewayPersonalSetup(
            steps=PersonalSetupSteps(store=lambda: self.access, catalog=CatalogSource(None).current, team_id=TEAM),
            events=self.gateway,
            emit=_Output(self.posted).emit,
        )
        directory = PeopleDirectorySync(
            identity=self.gateway.identity_repository, open_store=lambda: self.access, team_id=TEAM, clock=_Clock()
        )
        self.identity = GatewayIdentityService(
            repository=self.gateway.identity_repository,
            unit_of_work_factory=self.gateway,
            authorizer=MembershipRoleIdentityAuthorizer(),
            secrets=_Secrets(),
            clock=_Clock(),
            settings=GatewayIdentitySettings(
                enabled=True,
                allow_newuser_registration=True,
                web_user_tokens_enabled=True,
                web_user_token_ttl_days=0,
                web_user_invite_ttl_seconds=900,
                link_code_ttl_seconds=600,
                default_timezone="America/Chicago",
                team_id=TEAM,
                web_chat_url="https://agents.example.test/webchat",
            ),
            people_directory=directory,
        )
        self.setup = GatewayPersonSetupStore(
            access=lambda: self.access,
            layout=self.layout,
            team_id=TEAM,
            personal_setup=self.personal_setup,
        )
        self.lifecycle = GatewayPersonLifecycle(
            settings=GatewayPersonLifecycleSettings(team_id=TEAM, removed_data_retention_days=7),
            identity=self.identity,
            records=None,  # type: ignore[arg-type] - adding someone new touches no held records
            work=None,  # type: ignore[arg-type]
            broker=None,
            person_schedules=None,
            clock=_Clock(),
            setup=self.setup,
        )
        self.settings = PersonSettings(self.access, TEAM)

    def actor(self, person_id: str = ADMIN):
        return self.identity.resolve_user_by_id(person_id)

    def add(self, external: str, name: str, choices: PersonSetupChoices | None = None, *, by: str = ADMIN) -> str:
        change = self.identity.register_user_for_channel(
            actor=self.actor(by),
            display_name=name,
            channel_name="telegram",
            external_user_id=external,
            chat_scope="",
            thread_id=None,
        )
        return asyncio.run(self.lifecycle.after_added(self.actor(by), change, choices))

    def person_id(self, reply: str) -> str:
        return reply.split("Registered user ", 1)[1].split(" ", 1)[0]

    def admin_uses(self, *tool_ids: str) -> None:
        """The administrator set these up for their own work: signed in (sign-in tools) or linked their own account."""

        from datetime import datetime, timezone

        from access_control.tool_setup_states import HOW_SIGNED_IN, ToolSetupStates
        from custom_messages.contracts.access_v2 import AccountLinkV1, LinkOwnerKind, SubjectKind, SubjectRefV1
        from secure_tools.auth_methods import tool_sign_in

        tools = CatalogSource(None).current().tools
        for tool_id in tool_ids:
            if tool_sign_in(tool_id) is not None or tools[tool_id].account.value == "personal_login":
                ToolSetupStates(self.access).note_ready(TEAM, ADMIN, tool_id, how=HOW_SIGNED_IN, source="test")
                continue
            self.access.create_link(
                AccountLinkV1(
                    link_id=f"link-admin-{tool_id.replace(':', '-')}", team_id=TEAM, tool_id=tool_id,
                    owner_kind=LinkOwnerKind.PERSON, owner_id=ADMIN, credential_key=f"{tool_id}-admin",
                    created_by=SubjectRefV1(kind=SubjectKind.USER, id=ADMIN),
                    created_at_utc=datetime(2026, 10, 8, tzinfo=timezone.utc),
                )
            )


def test_a_new_person_gets_the_team_default_and_a_settings_report(tmp_path: Path) -> None:
    world = World(tmp_path)

    reply = world.add("1001", "Alice")

    person = world.person_id(reply)
    assert f"Settings for Alice ({person}):" in reply
    assert "- Role: member" in reply
    assert "- Added through: telegram identity 1001" in reply
    assert "- Timezone: America/Chicago" in reply
    assert f"- May use the team's model keys: no (change with /user team-keys {person} on)" in reply
    assert "- Own model: not set up yet. Their requests do not run until they set it up with /auth model me" in reply
    assert f"- Personal folder: users/{person_folder_name(person)}" in reply
    assert "- Shared folders: team/team-shared (open to the whole team" in reply
    assert world.settings.may_use_team_keys(person) is False
    assert world.layout.team_shared_dir.is_dir()


def test_a_new_person_may_use_the_team_model_keys_while_the_team_shares_them(tmp_path: Path) -> None:
    unchosen = World(tmp_path / "unchosen", model_keys=None)
    shared = World(tmp_path / "shared", model_keys=AccountSharing.SHARED)

    first = unchosen.person_id(unchosen.add("1101", "Ann"))
    second = shared.person_id(shared.add("1102", "Ben"))

    # Model keys are shared until an administrator makes them private (/accounts private model).
    assert unchosen.settings.may_use_team_keys(first) is True
    assert shared.settings.may_use_team_keys(second) is True


def test_the_administrators_choice_wins_over_the_default_and_a_shared_folder_is_made(tmp_path: Path) -> None:
    world = World(tmp_path, model_keys=AccountSharing.PRIVATE)

    reply = world.add("1002", "Bob", PersonSetupChoices(team_model_keys=True, shared_folder="research"))

    person = world.person_id(reply)
    assert "- May use the team's model keys: yes" in reply
    assert "- Own model: not set up (optional; they can add one with /auth model me)" in reply
    assert "- Shared folders: team/team-shared, team/research (open to the whole team" in reply
    assert (world.layout.team_dir / "research").is_dir()
    record = world.settings.settings(person)
    assert record is not None and record.uses_team_keys is True and record.shared_folders == ("team/research",)


def test_a_link_planted_where_the_shared_folder_goes_is_not_followed(tmp_path: Path) -> None:
    world = World(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, world.layout.team_dir / "research")

    reply = world.add("1003", "Carol", PersonSetupChoices(shared_folder="research"))

    assert "- Not done: team/research could not be created" in reply
    assert "team/research (open" not in reply
    assert list(outside.iterdir()) == []


def test_only_administrators_change_the_team_keys_switch_and_status_shows_it(tmp_path: Path) -> None:
    world = World(tmp_path)
    person = world.person_id(world.add("1004", "Dana"))

    refused = world.lifecycle.set_team_keys(actor=world.actor(MEMBER), target=person, allowed=True, channel_name=None, chat_scope="")
    changed = world.lifecycle.set_team_keys(actor=world.actor(ADMIN), target=person, allowed=True, channel_name=None, chat_scope="")

    assert refused == "Only an owner or an administrator can decide whether someone uses the team's model keys."
    assert changed.startswith(f"Updated Dana ({person}). Their agents may use the team's model keys")
    assert world.settings.may_use_team_keys(person) is True
    assert "- May use the team's model keys: yes" in "\n".join(world.lifecycle.settings_lines(person))


def test_people_added_before_the_switch_existed_keep_the_teams_keys(tmp_path: Path) -> None:
    world = World(tmp_path)

    lines = "\n".join(world.lifecycle.settings_lines(MEMBER))

    assert "- May use the team's model keys: yes (added before this setting existed" in lines
    assert world.settings.settings(MEMBER) is None


def test_user_status_lists_the_persons_model_settings(tmp_path: Path) -> None:
    world = World(tmp_path)
    operations = GatewayCommandOperationsApplication.__new__(GatewayCommandOperationsApplication)
    operations._identity = world.identity  # type: ignore[attr-defined]
    operations._person_lifecycle = world.lifecycle  # type: ignore[attr-defined]
    operations._requesting_user = lambda **_kwargs: world.actor(MEMBER)  # type: ignore[attr-defined]
    operations._request_context = type("Route", (), {"chat_scope": lambda self, _route: ""})()  # type: ignore[attr-defined]

    text = operations.render_new_user_status(channel_name="website", user_id=MEMBER, chat_id="web", canonical_user_id=MEMBER)

    assert "Canonical user: usr_member" in text
    assert "- May use the team's model keys: yes" in text and "- Shared folders: team/team-shared" in text


# -- first-time setup (assistant_gateway.personal_setup) -------------------------------


def _session(person_id: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        session_key=f"session_{person_id}", channel_name="telegram", user_id="1001", chat_id="1001",
        thread_id=None, conversation_id=None,
    )


def _first_message(world: World, person_id: str):
    return asyncio.run(world.personal_setup.before_request(person_id, session=_session(person_id)))


def test_a_new_person_without_a_model_sees_the_setup_first_and_the_message_waits(tmp_path: Path) -> None:
    world = World(tmp_path, model_keys=AccountSharing.PRIVATE)
    # The team uses GitHub and X; nobody uses Google or the GitHub API, so they are not listed.
    world.admin_uses("cli:github", "cli:x")
    reply = world.add("1201", "Cara")
    person = world.person_id(reply)

    first = _first_message(world, person)
    second = _first_message(world, person)

    assert (
        "- Still to set up: their own model (needed) and optionally their own GitHub (gh), X (xurl) (/setup lists them)"
    ) in reply
    assert reply.endswith("- First message: they are shown what is still to set up for themselves.")
    assert first is not None and first.startswith("Welcome to the team. Before your agents can work for you")
    assert "Google" not in first and "GitHub API" not in first
    assert '- Your own model (needed first: your requests do not run without it): send /auth model me' in first
    assert "- X (xurl): send /link cli:x" in first and "- GitHub (gh): send /link cli:github" in first
    assert "Your message was not sent to the team yet." in first
    assert second is None, "shown once; later messages go to the team, where the broker decides"
    assert world.posted == [], "the list was the reply, not a separate message"


def test_a_new_person_with_the_teams_model_gets_the_list_posted_and_their_message_runs(tmp_path: Path) -> None:
    from assistant_gateway.domain.message_types import SYSTEM_NOTIFICATION_MESSAGE_TYPE

    world = World(tmp_path, model_keys=AccountSharing.SHARED)
    world.admin_uses("cli:x")
    person = world.person_id(world.add("1202", "Dev"))

    assert _first_message(world, person) is None
    [posted] = world.posted
    assert posted.message_type == SYSTEM_NOTIFICATION_MESSAGE_TYPE and posted.session_key == f"session_{person}"
    assert posted.text.startswith("Welcome to the team. Your message is on its way to your agents.")
    assert "These services are private in this team" in posted.text and "Your own model" not in posted.text
    assert _first_message(world, person) is None and len(world.posted) == 1


def test_a_new_person_in_a_team_that_uses_no_private_service_sees_no_list(tmp_path: Path) -> None:
    # GitHub and Google are always private, but nobody in this team uses them.
    world = World(tmp_path, model_keys=AccountSharing.SHARED)
    reply = world.add("1205", "Gus")
    person = world.person_id(reply)

    assert "Still to set up" not in reply and "First message" not in reply
    assert _first_message(world, person) is None
    assert world.posted == []


def test_the_report_promises_a_first_time_setup_only_when_one_was_recorded(tmp_path: Path) -> None:
    from assistant_gateway.application.person_setup import settings_report

    world = World(tmp_path, model_keys=AccountSharing.PRIVATE)
    store = world.setup

    readded = store.set_up(MEMBER, PersonSetupChoices(), actor_id=ADMIN, new_person=False)

    def failing(person_id: str) -> None:
        raise RuntimeError("gateway state is read-only")

    world.personal_setup.mark_due = failing  # type: ignore[method-assign]
    unrecorded = store.set_up("usr_new", PersonSetupChoices(), actor_id=ADMIN, new_person=True)

    for view in (readded, unrecorded):
        report = settings_report(view, display_name="Pat", role="member", added_through="", timezone="")
        assert view.setup_due is False
        assert not any(line.startswith("- First message") for line in report)
    assert "their first-time setup could not be recorded; they can still use /setup." in unrecorded.problems


def test_people_already_in_the_team_never_get_a_first_time_setup(tmp_path: Path) -> None:
    world = World(tmp_path)

    assert _first_message(world, MEMBER) is None
    assert world.posted == []


def test_setup_shows_what_is_left_after_the_person_set_some_of_it_up(tmp_path: Path) -> None:
    from custom_messages.contracts.access_v2 import AccountLinkV1, LinkOwnerKind, SubjectKind, SubjectRefV1
    from custom_messages.contracts.person_settings_v1 import PersonModelProfileV1
    from datetime import datetime, timezone
    from model_providers.endpoints import EndpointKind

    world = World(tmp_path, model_keys=AccountSharing.PRIVATE)
    world.admin_uses("cli:github")
    person = world.person_id(world.add("1203", "Eve"))
    TeamAccountSharing(world.access, TEAM).choose_tool(
        CatalogSource(None).current().tools["http:exa"], AccountSharing.PRIVATE, by=ADMIN
    )
    world.access.create_link(
        AccountLinkV1(
            link_id="link-eve-exa", team_id=TEAM, tool_id="http:exa", owner_kind=LinkOwnerKind.PERSON, owner_id=person,
            credential_key="http-exa-personal", created_by=SubjectRefV1(kind=SubjectKind.USER, id=person),
            created_at_utc=datetime(2026, 10, 8, tzinfo=timezone.utc),
        )
    )
    world.settings.set_own_model(
        person,
        PersonModelProfileV1(
            kind=EndpointKind.OPENAI_COMPATIBLE, base_url="https://openrouter.ai/api/v1", model_id="x/y",
            tested_at_utc=datetime(2026, 10, 8, tzinfo=timezone.utc),
        ),
        by=person,
    )

    text = world.personal_setup.steps.text(person)

    assert text.startswith("Still to set up for your own work:")
    assert "Your own model" not in text and "Already set up: Exa web search." in text
    assert "- GitHub (gh): send /link cli:github" in text and "Send /setup to see this list again." in text


def test_a_failing_setup_check_never_stops_the_message(tmp_path: Path, caplog) -> None:
    world = World(tmp_path)
    person = world.person_id(world.add("1204", "Finn"))

    def broken(**_kwargs):
        raise RuntimeError("state store down")

    world.gateway.gateway_user_event_types = broken  # type: ignore[method-assign]

    assert _first_message(world, person) is None
    assert "event=personal_setup_failed" in caplog.text
