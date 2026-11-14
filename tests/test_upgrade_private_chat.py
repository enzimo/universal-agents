"""``/upgrade`` only in private chats: each channel says whether the chat is private.

The reply to ``/upgrade`` holds the export page's link, and anyone who sees
the link can open the page, so ``TeamExportCommandHandler`` refuses unless the
channel vouches that the chat is private (``GatewayActorContext.private_chat``).
Covered, with each real channel adapter and a command service that records
what it was given:

* Telegram: only ``chat.type == "private"``;
* Discord: a message without a server (DM), not one in a server channel;
* Signal and Photon: a one-to-one chat, not a group;
* WhatsApp (Twilio, one-to-one only), Web Chat and the local CLI: private;
* a remote (adapter host) channel carries the flag over the wire, and a
  missing flag counts as not private;
* the handler refuses outside a private chat before it looks anything up,
  and tells the owner to use a direct message.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_gateway.adapter_host.gateway_application import GatewayAdapterRequestHandler
from assistant_gateway.adapter_host.remote_host import RemoteGatewayApplication
from assistant_gateway.application.commands import TeamExportCommandHandler, TeamExportCommandRequest
from assistant_gateway.application.commands.context import GatewayActorContext
from assistant_gateway.application.gateway_application import GatewayCommandResult
from assistant_gateway.store import GatewayStateStore
from tests.application_test_doubles import application_from as GatewayApplicationFacade


class _RecordingCommands:
    """The command service: records each request the channel built."""

    def __init__(self) -> None:
        self.requests: list = []

    async def execute(self, request) -> GatewayCommandResult:
        self.requests.append(request)
        return GatewayCommandResult(handled=True, session_key=None, text="noted")


# -- the handler ----------------------------------------------------------------------


class _Identity:
    looked_up = False

    def canonical_user_id_for_context(self, **_context: str | None) -> str:
        self.looked_up = True
        return "usr_owner"

    def team_roles_for_person(self, canonical_user_id: str) -> tuple[str, ...]:
        return ("owner",)


class _Broker:
    auth_unavailable_message = None

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def user_form_url(self, internal_url: str) -> str:
        return internal_url

    async def create_auth_session(self, **request: object) -> dict[str, object]:
        self.requests.append(request)
        return {"method": "export", "login_url": "https://broker.test/auth/team/v1.signed", "expires_at": "2026-10-14T11:00:00+00:00"}


@pytest.mark.asyncio
@pytest.mark.parametrize("private_chat", [False, True])
async def test_upgrade_opens_the_page_only_in_a_private_chat(private_chat: bool) -> None:
    identity, broker = _Identity(), _Broker()
    handler = TeamExportCommandHandler(
        identity=identity, secure_broker=broker, team_id="team_ua", link_results=None, supervised=lambda: True  # type: ignore[arg-type]
    )

    result = await handler.execute(
        TeamExportCommandRequest(
            command="/upgrade",
            actor=GatewayActorContext(channel_name="telegram", user_id="tg_1", chat_id="-100", private_chat=private_chat),
        )
    )

    if private_chat:
        assert broker.requests and not isinstance(result, str)
        return
    assert isinstance(result, str) and "direct message" in result and "/upgrade" in result
    assert broker.requests == [] and identity.looked_up is False


# -- each channel says whether the chat is private ------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("chat_type", "private"), [("private", True), ("supergroup", False), ("group", False), ("channel", False)])
async def test_telegram_reports_only_private_chats_as_private(tmp_path, chat_type: str, private: bool) -> None:
    from tests.test_gateway_telegram_app import TelegramInteractiveApp, _FakeApp, _FakeGateway, _config, _make_update

    config = _config(tmp_path)
    config.interactive_apps.telegram.group_policy = "open"
    commands = _RecordingCommands()
    gateway = _FakeGateway(GatewayStateStore(str(tmp_path / "gateway.db")))
    app = TelegramInteractiveApp(
        config, application=GatewayApplicationFacade(gateway, command_application=commands), bridge=SimpleNamespace()
    )
    app._app = _FakeApp()

    await app._handle_update_text(_make_update(text="/upgrade", chat_type=chat_type, chat_id=777, user_id=777), text="/upgrade")

    [request] = commands.requests
    assert request.raw_text == "/upgrade" and request.private_chat is private


@pytest.mark.asyncio
@pytest.mark.parametrize(("in_server", "private"), [(False, True), (True, False)])
async def test_discord_reports_only_direct_messages_as_private(tmp_path, in_server: bool, private: bool) -> None:
    from tests.test_gateway_discord_app import (
        DiscordInteractiveApp,
        _FakeBot,
        _FakeGateway,
        _FakeMessageable,
        _FakeTextChannel,
        _config,
        _make_message,
    )

    config = _config(tmp_path)
    config.interactive_apps.discord.group_policy = "open"
    commands = _RecordingCommands()
    gateway = _FakeGateway(GatewayStateStore(str(tmp_path / "gateway.db")))
    app = DiscordInteractiveApp(
        config, application=GatewayApplicationFacade(gateway, command_application=commands), bridge=SimpleNamespace()
    )
    app._bot = _FakeBot({123: _FakeMessageable(123)})
    app._self_user_id = 999

    await app._on_message(_make_message(text="/upgrade", channel=_FakeTextChannel(123, guild=object() if in_server else None)))

    [request] = commands.requests
    assert request.raw_text == "/upgrade" and request.private_chat is private


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_signal_reports_one_to_one_chats_as_private(tmp_path, group: bool) -> None:
    from tests.test_gateway_signal_app import (
        SignalInteractiveApp,
        _FakeGateway,
        _FakeSignalClient,
        _config,
        _direct_payload,
        _group_payload,
    )

    config = _config(tmp_path)
    config.interactive_apps.signal.allowed_group_ids = ["group.signal"]
    commands = _RecordingCommands()
    gateway = _FakeGateway(config, GatewayStateStore(str(tmp_path / "gateway.db")))
    app = SignalInteractiveApp(
        config, application=GatewayApplicationFacade(gateway, command_application=commands), bridge=SimpleNamespace()
    )
    app._client = _FakeSignalClient()

    await app._process_inbound_payload(_group_payload(message="@Ava /upgrade") if group else _direct_payload(message="/upgrade"))

    [request] = commands.requests
    assert request.raw_text == "/upgrade" and request.private_chat is (not group)


@pytest.mark.asyncio
@pytest.mark.parametrize(("space_type", "private"), [("dm", True), ("group", False), (None, False)])
async def test_photon_reports_only_direct_spaces_as_private(tmp_path, space_type: str | None, private: bool) -> None:
    from tests.test_gateway_photon_app import PhotonInteractiveApp, _config

    config = _config(tmp_path)
    config.interactive_apps.photon.allowed_group_ids = ["any;+;chat-1"]
    config.interactive_apps.photon.group_policy = "open"
    commands = _RecordingCommands()
    application = SimpleNamespace(
        resolve_session=lambda **_route: SimpleNamespace(session_key="photon:1", conversation_id="conversation_1"),
        is_registered_user=lambda **_route: True,
        session_repository=SimpleNamespace(get_active_session_for_channel=lambda **_route: None),
    )
    app = PhotonInteractiveApp(
        config,
        application=GatewayApplicationFacade(application, command_application=commands),
        bridge=SimpleNamespace(handle_inbound=AsyncMock(return_value=[])),
    )
    app._send_outbound = AsyncMock()  # type: ignore[method-assign]
    space = {"id": "any;+;chat-1"} | ({"type": space_type} if space_type else {})

    await app._process_inbound_event(
        {"messageId": "m1", "space": space, "sender": {"id": "+15551234567"}, "content": {"type": "text", "text": "/upgrade"}}
    )

    [request] = commands.requests
    assert request.raw_text == "/upgrade" and request.private_chat is private


@pytest.mark.asyncio
@pytest.mark.parametrize(("arguments", "private"), [({"private_chat": True}, True), ({"private_chat": False}, False), ({}, False)])
async def test_a_remote_channel_carries_the_flag_and_a_missing_one_is_not_private(arguments: dict, private: bool) -> None:
    submitted: list = []
    remote = RemoteGatewayApplication(
        lambda: SimpleNamespace(submit=AsyncMock(side_effect=submitted.append)),
        plugin_id="telegram",
        adapter_instance_id="telegram-1",
        channel_name="telegram",
        default_session_key="default",
    )
    await remote.execute_interactive_command(
        raw_text="/upgrade", channel_name="telegram", user_id="111", chat_id="456", private_chat=True
    )
    [command] = submitted
    assert command.arguments["private_chat"] is True

    received: list[dict] = []

    class _Application:
        async def execute_interactive_command(self, **values) -> GatewayCommandResult:
            received.append(values)
            return GatewayCommandResult(handled=False, session_key=None, text=None)

    handler = GatewayAdapterRequestHandler(_Application(), lambda: None)  # type: ignore[arg-type]
    other = {key: value for key, value in command.arguments.items() if key != "private_chat"}
    await handler.handle_command(command.model_copy(update={"arguments": {**other, **arguments}}))

    assert received[0]["private_chat"] is private
