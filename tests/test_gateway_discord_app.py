from __future__ import annotations

import inspect
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import assistant_gateway.interactive_apps.discord as discord_module
from assistant_gateway.application.identity_service import (
    GatewayIdentityService,
    GatewayIdentitySettings,
)
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestContextSettings,
)
from assistant_gateway.application.runtime_application import (
    GatewayRuntimeApplication,
    GatewayRuntimeApplicationSettings,
)
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.command_composition import build_gateway_command_application
from tests.application_test_doubles import (
    RecordingLinkResults,
    application_from as GatewayApplicationFacade,
    interactive_app_runtime_from,
)
from assistant_gateway.gateway import GatewayDeliveryEvent
from assistant_gateway.interactive_apps.discord import (
    DiscordInteractiveApp as _DiscordInteractiveApp,
)
from assistant_gateway.runtime_ports import (
    OwnershipPrivateResourceAuthorizer,
    SystemGatewayClock,
)
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.voice import SpeechResponse, TranscriptionResponse
from tests.staged_attachment_helpers import staged_attachment


class DiscordInteractiveApp(_DiscordInteractiveApp):
    """Compose explicit host ports around aggregate test fixtures."""

    def __init__(self, config, application, bridge) -> None:
        factory = getattr(discord_module, "build_voice_client", None)
        voice_client = factory(config.voice) if callable(factory) else None
        super().__init__(
            config.interactive_apps.discord,
            application,
            bridge,
            interactive_app_runtime_from(
                config, application, voice_client=voice_client
            ),
        )


def test_live_voice_warning_explains_attachment_support(caplog):
    discord_module._configure_discord_library_logging()
    discord_module._configure_discord_library_logging()
    client_logger = logging.getLogger("discord.client")
    assert (
        sum(
            isinstance(item, discord_module._DiscordVoiceDependencyContext)
            for item in client_logger.filters
        )
        == 1
    )
    with caplog.at_level(logging.WARNING, logger="discord.client"):
        client_logger.warning("davey is not installed, voice will NOT be supported")
        client_logger.warning("Unrelated Discord failure")
    assert "Discord live voice-channel support is unavailable: davey" in caplog.text
    assert "audio attachments, which do not require it" in caplog.text
    assert "Unrelated Discord failure" in caplog.text
    assert all(record.levelno == logging.WARNING for record in caplog.records)


class _FakeMessageable:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent_messages: list[dict] = []
        self.typing_call_count = 0

    def get_partial_message(self, message_id: int):
        return SimpleNamespace(id=message_id)

    async def send(self, **kwargs):
        self.sent_messages.append(kwargs)
        return SimpleNamespace(id=len(self.sent_messages))

    async def trigger_typing(self) -> None:
        self.typing_call_count += 1


class _FakeBot:
    def __init__(self, channels: dict[int, _FakeMessageable] | None = None) -> None:
        self.channels = channels or {}

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        return self.channels[channel_id]


class _FakeGateway:
    def __init__(self, state_store: GatewayStateStore) -> None:
        self.state_store = state_store
        self.session_repository = state_store.session_repository
        self.request_repository = state_store.request_repository
        self.delivery_repository = state_store.delivery_repository
        self.sent_user_messages: list[dict] = []

    @staticmethod
    def canonical_user_id_for_context(**_context):
        return None

    @staticmethod
    def gateway_users_enabled() -> bool:
        return False

    @staticmethod
    def requester_timezone_for_context(**_context) -> str:
        return "UTC"

    async def send_user_message(self, text: str, **kwargs):
        self.sent_user_messages.append({"text": text, **kwargs})
        return SimpleNamespace(
            text=f"Reply: {text}",
            conversation_id=kwargs.get("conversation_id") or "conv_reply",
        )

    def start_new_session(
        self,
        *,
        channel_name=None,
        user_id=None,
        chat_id=None,
        thread_id=None,
        canonical_user_id=None,
    ):
        return self.state_store.create_session(
            session_key="session_new",
            conversation_id="conv_new",
            manager_agent_id="manager_1",
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            owner_canonical_user_id=canonical_user_id,
        )

    def switch_session_conversation(
        self,
        *,
        session_key: str,
        conversation_id: str,
        last_task_id: str | None,
    ):
        existing = self.session_repository.get_session(session_key)
        if existing is None:
            return None
        self.session_repository.upsert_session(
            session_key=session_key,
            conversation_id=conversation_id,
            last_task_id=last_task_id,
            manager_agent_id=existing.manager_agent_id,
        )
        return self.session_repository.get_session(session_key)

    def resolve_session(
        self,
        *,
        session_key=None,
        channel_name=None,
        user_id=None,
        chat_id=None,
        thread_id=None,
    ):
        if session_key:
            existing = self.state_store.get_session(session_key)
            if existing is not None:
                return existing
        existing = self.state_store.get_active_session_for_channel(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        if existing is not None:
            return existing
        return self.start_new_session(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )


class _FakeBridge:
    def __init__(self, outbound_messages=None) -> None:
        self.outbound_messages = list(outbound_messages or [])
        self.inbound_messages = []

    async def handle_inbound(self, inbound):
        self.inbound_messages.append(inbound)
        return list(self.outbound_messages)


class _RegisteredUserGateway:
    def __init__(self, registered_user_ids: set[str]) -> None:
        self.registered_user_ids = registered_user_ids

    def resolve_canonical_user(
        self, *, channel_name: str, external_user_id: str, chat_scope: str
    ):
        if (
            channel_name == "discord"
            and chat_scope == ""
            and external_user_id in self.registered_user_ids
        ):
            return "user_registered"
        return None


_UNSET = object()


class _FakeInteractionResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.message_calls: list[dict[str, object]] = []
        self.defer_calls: list[dict] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs) -> None:
        self.defer_calls.append(kwargs)
        self._done = True

    async def send_message(self, text: str, **kwargs) -> None:
        self.messages.append(text)
        self.message_calls.append({"text": text, **kwargs})
        self._done = True


class _FakeInteractionFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.message_calls: list[dict[str, object]] = []

    async def send(self, text: str, **kwargs) -> None:
        self.messages.append(text)
        self.message_calls.append({"text": text, **kwargs})


class _FakeInteraction:
    def __init__(
        self,
        *,
        user_id: int,
        channel,
        guild_id: object = _UNSET,
        context: object = _UNSET,
    ) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.channel = channel
        if guild_id is _UNSET:
            self.guild_id = (
                None
                if channel is not None and getattr(channel, "guild", None) is None
                else 999
            )
        else:
            self.guild_id = guild_id
        self.channel_id = getattr(channel, "id", None) if channel is not None else None
        if context is _UNSET:
            self.context = SimpleNamespace(
                guild=self.guild_id is not None,
                dm_channel=self.guild_id is None,
                private_channel=False,
            )
        else:
            self.context = context
        self.response = _FakeInteractionResponse()
        self.followup = _FakeInteractionFollowup()


class _FakeAuthor:
    def __init__(self, user_id: int, *, is_bot: bool = False) -> None:
        self.id = user_id
        self.bot = is_bot


class _FakeTextChannel:
    def __init__(self, channel_id: int, *, guild: object | None = object()) -> None:
        self.id = channel_id
        self.guild = guild
        self.type = "text"
        self.typing_call_count = 0

    async def trigger_typing(self) -> None:
        self.typing_call_count += 1


class _FakeDiscordFile:
    def __init__(self, fp, *, filename: str) -> None:
        self.fp = fp
        self.filename = filename


class _FakeThread:
    def __init__(
        self,
        thread_id: int,
        *,
        parent: _FakeTextChannel,
        guild: object | None = object(),
    ) -> None:
        self.id = thread_id
        self.parent = parent
        self.guild = guild
        self.type = "public_thread"
        self.typing_call_count = 0

    async def trigger_typing(self) -> None:
        self.typing_call_count += 1


def _make_message(
    *,
    text: str,
    author_id: int = 777,
    channel=None,
    mentions=None,
    reference=None,
    attachments=None,
):
    return SimpleNamespace(
        id=9,
        content=text,
        author=_FakeAuthor(author_id),
        channel=channel or _FakeTextChannel(123),
        guild=getattr(channel, "guild", None) if channel is not None else object(),
        mentions=list(mentions or []),
        reference=reference,
        attachments=list(attachments or []),
    )


class _FakeAttachment:
    def __init__(
        self,
        *,
        body: bytes,
        filename: str = "voice.ogg",
        content_type: str = "audio/ogg",
    ) -> None:
        self.body = body
        self.filename = filename
        self.content_type = content_type

    async def read(self) -> bytes:
        return self.body


def _config(tmp_path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="mybot",
        instance_root=str(tmp_path / "instance"),
        team_id="team_303",
        timezone="America/Phoenix",
    )
    config.interactive_apps.discord.enabled = True
    return config


def test_discord_app_warns_when_enabled_without_allowlists(tmp_path, caplog) -> None:
    config = _config(tmp_path)
    app = DiscordInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )

    with caplog.at_level(logging.WARNING):
        app._warn_if_allow_all()

    assert "empty allowed_user_ids and allowed_channel_ids" in caplog.text


def test_discord_app_logs_newuser_target_for_disallowed_user(tmp_path, caplog) -> None:
    config = _config(tmp_path)
    config.interactive_apps.discord.allowed_user_ids = ["allowed-user"]
    app = DiscordInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )
    message = SimpleNamespace(
        author=SimpleNamespace(id=987654321012345678),
        channel=SimpleNamespace(id=111222333444555666, type="text"),
    )

    with caplog.at_level(logging.INFO):
        allowed = app._is_allowed(message)

    assert allowed is False
    assert "non-allowed user" in caplog.text
    assert "user_id=987654321012345678" in caplog.text
    assert "newuser_target=987654321012345678" in caplog.text


def test_discord_app_allows_registered_dm_user_without_allowlist_restart(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.discord.allowed_user_ids = ["111"]
    config.interactive_apps.discord.allowed_channel_ids = ["222"]
    app = DiscordInteractiveApp(
        config,
        application=GatewayApplicationFacade(
            _RegisteredUserGateway({"987654321012345678"})
        ),
        bridge=SimpleNamespace(),
    )
    message = SimpleNamespace(
        author=SimpleNamespace(id=987654321012345678),
        channel=SimpleNamespace(id=111222333444555666, type="private"),
        guild=None,
    )

    assert app._is_allowed(message) is True


class _FakeVoiceClient:
    def __init__(self) -> None:
        self.transcriptions: list[dict] = []
        self.speeches: list[str] = []
        self.synthesize_error: Exception | None = None

    async def transcribe(self, request):
        self.transcriptions.append(
            {
                "audio": request.audio,
                "filename": request.filename,
                "content_type": request.content_type,
            }
        )
        return TranscriptionResponse(text="transcribed discord voice")

    async def synthesize(self, request):
        if self.synthesize_error is not None:
            raise self.synthesize_error
        self.speeches.append(request.text)
        return SpeechResponse(audio=b"mp3-reply", content_type="audio/mpeg")


@pytest.mark.asyncio
async def test_discord_app_reads_fixed_token_env_var(monkeypatch, tmp_path) -> None:
    config = _config(tmp_path)
    gateway = _FakeGateway(GatewayStateStore(str(tmp_path / "gateway.db")))
    app = DiscordInteractiveApp(
        config, application=GatewayApplicationFacade(gateway), bridge=_FakeBridge()
    )

    async def _fake_wait_until_ready(self) -> None:
        return None

    class _FakeGatewayBot:
        def __init__(self, _app) -> None:
            pass

        async def start(self, token: str) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(discord_module, "discord", SimpleNamespace())
    monkeypatch.setattr(discord_module, "discord_commands", SimpleNamespace())
    monkeypatch.setattr(discord_module, "app_commands", SimpleNamespace())
    monkeypatch.setattr(
        discord_module, "_configure_discord_library_logging", lambda: None
    )
    monkeypatch.setattr(discord_module, "_DiscordGatewayBot", _FakeGatewayBot)
    monkeypatch.setattr(
        DiscordInteractiveApp, "_wait_until_ready", _fake_wait_until_ready
    )

    seen: dict[str, str] = {}

    def _fake_get_env_with_file_fallback(name: str, env=None):
        seen["name"] = name
        return "discord-token"

    monkeypatch.setattr(
        discord_module, "get_env_with_file_fallback", _fake_get_env_with_file_fallback
    )

    await app.start()

    assert seen["name"] == "DISCORD_BOT_TOKEN"


@pytest.mark.asyncio
async def test_discord_app_routes_thread_scoped_messages(
    tmp_path, monkeypatch, caplog
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.discord.group_policy = "open"
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _FakeGateway(store)
    bridge = _FakeBridge(
        [
            SimpleNamespace(
                channel_name="discord",
                chat_id="123",
                thread_id="456",
                text="Manager says hello",
                in_reply_to_message_id="9",
                session_key="session_1",
                conversation_id="conv_1",
                suppress_delivery=False,
            )
        ]
    )
    app = DiscordInteractiveApp(
        config, application=GatewayApplicationFacade(gateway), bridge=bridge
    )
    parent_channel = _FakeTextChannel(123)
    thread_channel = _FakeThread(456, parent=parent_channel)
    outbound_target = _FakeMessageable(456)
    app._bot = _FakeBot({456: outbound_target})
    app._self_user_id = 999
    store.create_session(
        session_key="session_1",
        conversation_id="conv_1",
        manager_agent_id="manager_1",
        channel_name="discord",
        user_id="777",
        chat_id="123",
        thread_id="456",
    )

    monkeypatch.setattr(discord_module, "discord", SimpleNamespace(Thread=_FakeThread))

    with caplog.at_level("INFO"):
        await app._on_message(
            _make_message(
                text="hello from discord",
                channel=thread_channel,
            )
        )

    assert bridge.inbound_messages[0].chat_id == "123"
    assert bridge.inbound_messages[0].thread_id == "456"
    assert "Discord inbound to manager" in caplog.text
    assert "session=session_1" in caplog.text
    assert "thread_id=456" in caplog.text
    assert outbound_target.typing_call_count == 1
    assert outbound_target.sent_messages[0]["content"] == "Manager says hello"
    assert outbound_target.sent_messages[0]["reference"].id == 9


@pytest.mark.asyncio
async def test_discord_deliver_event_sends_attachment(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        discord_module,
        "discord",
        SimpleNamespace(Thread=_FakeThread, File=_FakeDiscordFile),
    )
    config = _config(tmp_path)
    screenshot = staged_attachment(
        config,
        data=b"png-data",
        filename="site.png",
        content_type="image/png",
        kind="image",
        caption="Screenshot",
    )
    linked = staged_attachment(
        config, data=b"notes", filename="notes.txt", content_type="text/plain"
    )
    # Agent code swaps the copy for a link after staging: it must not be sent.
    linked_copy = Path(linked.path)
    linked_copy.unlink()
    linked_copy.symlink_to(config.layout.soul_path)
    target = _FakeMessageable(123)
    app = DiscordInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )
    app._bot = _FakeBot({123: target})

    await app.deliver_event(
        GatewayDeliveryEvent(
            text="Screenshot attached.",
            session_key="session_1",
            channel_name="discord",
            user_id="u1",
            chat_id="123",
            thread_id=None,
            conversation_id="conv_1",
            scheduled_job_id=None,
            correlation_id="corr_1",
            original_message_id="9",
            attachments=(screenshot, linked),
        )
    )

    assert len(target.sent_messages) == 2
    assert target.sent_messages[0]["content"] == "Screenshot attached."
    attachment_message = target.sent_messages[1]
    assert attachment_message["content"] == "Screenshot"
    assert attachment_message["reference"].id == 9
    assert attachment_message["file"].filename == "site.png"
    assert attachment_message["file"].fp.getvalue() == b"png-data"


@pytest.mark.asyncio
async def test_discord_voice_input_gets_audio_reply(monkeypatch, tmp_path) -> None:
    fake_voice = _FakeVoiceClient()
    monkeypatch.setattr(
        discord_module, "build_voice_client", lambda _settings: fake_voice, raising=False
    )
    monkeypatch.setattr(
        discord_module,
        "discord",
        SimpleNamespace(Thread=_FakeThread, File=_FakeDiscordFile),
    )
    config = _config(tmp_path)
    config.interactive_apps.discord.group_policy = "open"
    config.voice.enabled = True
    config.voice.input_enabled = True
    config.voice.output_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _FakeGateway(store)
    bridge = _FakeBridge(
        [
            SimpleNamespace(
                channel_name="discord",
                chat_id="123",
                thread_id=None,
                text="Manager voice reply",
                in_reply_to_message_id="9",
                session_key="session_1",
                conversation_id="conv_1",
                suppress_delivery=False,
            )
        ]
    )
    target = _FakeMessageable(123)
    app = DiscordInteractiveApp(
        config, application=GatewayApplicationFacade(gateway), bridge=bridge
    )
    app._bot = _FakeBot({123: target})
    app._self_user_id = 999
    attachment = _FakeAttachment(body=b"ogg-voice")

    await app._on_message(
        _make_message(
            text="",
            channel=_FakeTextChannel(123, guild=None),
            attachments=[attachment],
        )
    )

    assert fake_voice.transcriptions == [
        {"audio": b"ogg-voice", "filename": "voice.ogg", "content_type": "audio/ogg"}
    ]
    assert bridge.inbound_messages[0].text == "transcribed discord voice"
    assert fake_voice.speeches == ["Manager voice reply"]
    assert len(target.sent_messages) == 2
    sent_text = target.sent_messages[0]["content"]
    assert "Voice request:" in sent_text
    assert "transcribed discord voice" in sent_text
    assert "Assistant reply:" in sent_text
    assert "Manager voice reply" in sent_text
    sent = target.sent_messages[1]
    assert sent["file"].filename == "reply.mp3"
    assert sent["file"].fp.getvalue() == b"mp3-reply"
    assert sent["content"] == ""


@pytest.mark.asyncio
async def test_discord_app_reply_command_is_cli_only(tmp_path) -> None:
    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _FakeGateway(store)
    app = DiscordInteractiveApp(
        config,
        application=GatewayApplicationFacade(
            gateway,
            command_application=build_gateway_command_application(
                config=config,
                operations=gateway,
                session_repository=gateway.session_repository,
                request_repository=gateway.request_repository,
                private_authorizer=OwnershipPrivateResourceAuthorizer(),
                operational_controls=gateway,
                link_results=RecordingLinkResults(),
            ),
        ),
        bridge=SimpleNamespace(),
    )
    interaction = _FakeInteraction(
        user_id=777, channel=_FakeTextChannel(123, guild=None)
    )

    await app._handle_slash_command(interaction, "/reply msg_1 continue this thread")

    assert gateway.sent_user_messages == []
    assert interaction.response.defer_calls == [{"thinking": True}]
    assert interaction.response.messages == []
    assert interaction.followup.messages == [
        "The /reply command is available only in the local CLI."
    ]


class _SlashCommandOperations(_FakeGateway):
    """Operations adapter backed by real GatewayRequestContextService."""

    def __init__(
        self,
        state_store: GatewayStateStore,
        request_context: GatewayRequestContextService,
    ) -> None:
        super().__init__(state_store)
        self._request_context = request_context
        self.stop_calls: list[object] = []

    def gateway_users_enabled(self) -> bool:
        return self._request_context.users_enabled

    def canonical_user_id_for_context(
        self,
        *,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        canonical_user_id: str | None = None,
    ) -> str | None:
        from assistant_gateway.application.request_context import GatewayRequestRoute

        return self._request_context.canonical_user_id(
            GatewayRequestRoute(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                canonical_user_id=canonical_user_id,
            )
        )

    def team_roles_for_person(self, _person_id: str) -> tuple[str, ...]:
        return ()

    async def stop(self, request):
        self.stop_calls.append(request)
        return SimpleNamespace(text="Stopped active work.")


class _SpyGatewayRuntimeApplication(GatewayRuntimeApplication):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.command_calls: list[dict[str, object]] = []

    async def execute_interactive_command(self, **kwargs):
        self.command_calls.append(kwargs)
        return await super().execute_interactive_command(**kwargs)


def _build_slash_allowlist_app(
    tmp_path: Path,
    *,
    users_enabled: bool,
) -> tuple[DiscordInteractiveApp, _SpyGatewayRuntimeApplication, _SlashCommandOperations]:
    config = _config(tmp_path)
    config.gateway.users.enabled = users_enabled
    config.interactive_apps.discord.allowed_user_ids = ["100"]
    config.interactive_apps.discord.allowed_channel_ids = ["200"]

    store = GatewayStateStore(str(tmp_path / f"gateway_{users_enabled}.db"))
    with store.identity_unit_of_work() as uow:
        uow.identity.create_gateway_user(
            canonical_user_id="owner_1",
            display_name="Owner",
            timezone="UTC",
        )
        uow.identity.create_team_membership(
            canonical_user_id="owner_1",
            team_id="team_303",
            role="owner",
        )
        uow.identity.create_channel_binding(
            canonical_user_id="owner_1",
            channel_name="discord",
            external_user_id="100",
        )
        uow.identity.create_gateway_user(
            canonical_user_id="registered_1",
            display_name="Registered Member",
            timezone="UTC",
        )
        uow.identity.create_team_membership(
            canonical_user_id="registered_1",
            team_id="team_303",
            role="member",
        )
        uow.identity.create_channel_binding(
            canonical_user_id="registered_1",
            channel_name="discord",
            external_user_id="300",
        )
        uow.commit()

    store.create_session(
        session_key="session_owner",
        conversation_id="conv_owner",
        manager_agent_id="manager_1",
        channel_name="discord",
        user_id="100",
        chat_id="200",
        owner_canonical_user_id="owner_1",
    )
    store.record_request(
        correlation_id="corr_owner",
        session_key="session_owner",
        request_kind="user_message",
        request_text="SECRET_OWNER_PROMPT",
        conversation_id="conv_owner",
        followup_to_task_id=None,
        status="completed",
        owner_canonical_user_id="owner_1",
    )

    identity_service = GatewayIdentityService(
        repository=store.identity_repository,
        unit_of_work_factory=store,
        authorizer=SimpleNamespace(),  # type: ignore[arg-type]
        secrets=SimpleNamespace(hash_secret=lambda raw: f"hash:{raw}"),  # type: ignore[arg-type]
        clock=SystemGatewayClock(),
        people_directory=SimpleNamespace(),  # type: ignore[arg-type]
        settings=GatewayIdentitySettings(
            enabled=users_enabled,
            allow_newuser_registration=True,
            web_user_tokens_enabled=False,
            web_user_token_ttl_days=7,
            web_user_invite_ttl_seconds=900,
            link_code_ttl_seconds=600,
            default_timezone="UTC",
            team_id="team_303",
            web_chat_url="https://agents.example.test/webchat",
        ),
    )
    request_context = GatewayRequestContextService(
        settings=GatewayRequestContextSettings(
            instance_name="mybot",
            team_id="team_303",
            default_canonical_user_id="owner_1",
            default_display_name="Owner",
            default_timezone="UTC",
            heartbeat_source_id=None,
            users_enabled=users_enabled,
        ),
        identity=identity_service,
    )
    operations = _SlashCommandOperations(store, request_context)
    commands = build_gateway_command_application(
        config=config,
        operations=operations,
        session_repository=store.session_repository,
        request_repository=store.request_repository,
        private_authorizer=OwnershipPrivateResourceAuthorizer(),
        operational_controls=operations,
        link_results=RecordingLinkResults(),
    )
    application = _SpyGatewayRuntimeApplication(
        settings=GatewayRuntimeApplicationSettings(
            default_session_key="session_owner",
            default_owner_canonical_user_id="owner_1",
        ),
        sessions=SimpleNamespace(),  # type: ignore[arg-type]
        session_repository=store.session_repository,
        delivery_repository=store.delivery_repository,
        request_context=request_context,
        manager=SimpleNamespace(),  # type: ignore[arg-type]
        commands=commands,
        progress=SimpleNamespace(),  # type: ignore[arg-type]
        clarification=SimpleNamespace(),  # type: ignore[arg-type]
        schedules=SimpleNamespace(),  # type: ignore[arg-type]
        deferred_delivery=SimpleNamespace(),  # type: ignore[arg-type]
        submission=SimpleNamespace(),  # type: ignore[arg-type]
        access_reviews=SimpleNamespace(),  # type: ignore[arg-type]
    )
    app = DiscordInteractiveApp(config, application=application, bridge=_FakeBridge())
    return app, application, operations


@pytest.mark.asyncio
@pytest.mark.parametrize("users_enabled", [False, True])
@pytest.mark.parametrize(
    "command_text",
    ["/list_sessions", "/preview_session session_owner", "/stop"],
)
@pytest.mark.parametrize(
    "case_name",
    [
        "non_allowed_user_in_allowed_channel",
        "allowed_user_in_non_allowed_channel",
        "registered_user_in_non_allowed_guild_channel",
    ],
)
async def test_discord_slash_commands_refuse_non_allowed_user_or_channel(
    tmp_path: Path,
    caplog,
    users_enabled: bool,
    command_text: str,
    case_name: str,
) -> None:
    app, application, operations = _build_slash_allowlist_app(
        tmp_path, users_enabled=users_enabled
    )
    cases = {
        "non_allowed_user_in_allowed_channel": (999, _FakeTextChannel(200), 555),
        "allowed_user_in_non_allowed_channel": (100, _FakeTextChannel(999), 555),
        "registered_user_in_non_allowed_guild_channel": (
            300,
            _FakeTextChannel(999),
            555,
        ),
    }
    user_id, channel, guild_id = cases[case_name]
    interaction = _FakeInteraction(user_id=user_id, channel=channel, guild_id=guild_id)

    with caplog.at_level(logging.INFO):
        await app._handle_slash_command(interaction, command_text)

    assert application.command_calls == []
    assert operations.stop_calls == []
    assert interaction.response.defer_calls == []
    assert interaction.followup.messages == []
    assert len(interaction.response.message_calls) == 1
    assert interaction.response.message_calls[0]["ephemeral"] is True
    reply_text = str(interaction.response.message_calls[0]["text"])
    assert "session_owner" not in reply_text
    assert "SECRET_OWNER" not in reply_text
    assert f"newuser_target={user_id}" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("users_enabled", [False, True])
async def test_discord_slash_commands_allow_allowlisted_user_and_thread_parent(
    tmp_path: Path,
    monkeypatch,
    users_enabled: bool,
) -> None:
    monkeypatch.setattr(discord_module, "discord", SimpleNamespace(Thread=_FakeThread))
    app, application, operations = _build_slash_allowlist_app(
        tmp_path, users_enabled=users_enabled
    )

    for command_text in ("/list_sessions", "/preview_session session_owner", "/stop"):
        interaction = _FakeInteraction(
            user_id=100, channel=_FakeTextChannel(200), guild_id=555
        )
        await app._handle_slash_command(interaction, command_text)
        assert interaction.response.defer_calls == [{"thinking": True}]
        assert len(interaction.followup.messages) == 1
        assert interaction.followup.message_calls[0].get("ephemeral") is not True

    assert len(application.command_calls) == 3
    assert len(operations.stop_calls) == 1

    # Thread under an allowed parent channel passes.
    parent = _FakeTextChannel(200)
    thread = _FakeThread(201, parent=parent)
    thread_interaction = _FakeInteraction(user_id=100, channel=thread, guild_id=555)
    await app._handle_slash_command(thread_interaction, "/list_sessions")
    assert len(application.command_calls) == 4
    assert "session_owner" in thread_interaction.followup.messages[0]


def _build_real_discord_interaction(
    state,
    *,
    user_id: str,
    channel: dict | None,
    guild_id: str | None,
    owners: dict[str, str],
    context: int | None,
):
    import discord as real_discord

    user = {
        "id": user_id,
        "username": f"user_{user_id}",
        "discriminator": "0",
        "avatar": None,
        "global_name": None,
    }
    data: dict[str, object] = {
        "id": "1",
        "type": 2,
        "token": "test-token",
        "version": 1,
        "application_id": "42",
        "attachment_size_limit": 1024,
        "authorizing_integration_owners": owners,
        "data": {"id": "5", "name": "list_sessions", "type": 1},
    }
    if channel is not None:
        data["channel"] = channel
        data["channel_id"] = channel["id"]
    if context is not None:
        data["context"] = context
    if guild_id is not None:
        data["guild_id"] = guild_id
        data["member"] = {
            "user": user,
            "roles": [],
            "joined_at": "2020-01-01T00:00:00+00:00",
            "deaf": False,
            "mute": False,
            "flags": 0,
            "permissions": "0",
        }
    else:
        data["user"] = user
    interaction = real_discord.Interaction(data=data, state=state)  # type: ignore[arg-type]
    interaction._cs_response = _FakeInteractionResponse()  # type: ignore[assignment]
    interaction._cs_followup = _FakeInteractionFollowup()  # type: ignore[assignment]
    return interaction


@pytest.mark.asyncio
@pytest.mark.parametrize("users_enabled", [False, True])
async def test_discord_slash_commands_real_interaction_dm_and_channel_rules(
    tmp_path: Path,
    caplog,
    users_enabled: bool,
) -> None:
    import discord as real_discord

    app, application, _operations = _build_slash_allowlist_app(
        tmp_path, users_enabled=users_enabled
    )
    client = real_discord.Client(intents=real_discord.Intents.default())
    try:
        state = client._connection
        state.user = real_discord.ClientUser(
            state=state,
            data={
                "id": "42",
                "username": "bot",
                "discriminator": "0",
                "avatar": None,
                "global_name": None,
                "bot": True,
            },
        )
        registered_user = {
            "id": "300",
            "username": "member",
            "discriminator": "0",
            "avatar": None,
            "global_name": None,
        }
        owner_user = {
            "id": "100",
            "username": "owner",
            "discriminator": "0",
            "avatar": None,
            "global_name": None,
        }

        cases = [
            # Registered user in bot DM, server-installed app: passes only when users.enabled=True.
            (
                "bot_dm_guild_install",
                dict(
                    user_id="300",
                    channel={"id": "900", "type": 1, "recipients": [registered_user]},
                    guild_id=None,
                    owners={"0": "0"},
                    context=1,
                ),
                True,
                users_enabled,
            ),
            # Registered user in bot DM, user-installed app: passes only when users.enabled=True.
            (
                "bot_dm_user_install",
                dict(
                    user_id="300",
                    channel={"id": "900", "type": 1, "recipients": [registered_user]},
                    guild_id=None,
                    owners={"0": "0", "1": "300"},
                    context=1,
                ),
                True,
                users_enabled,
            ),
            # DM between two people through a user install: never a bot DM; refused in both modes.
            (
                "private_dm_user_install",
                dict(
                    user_id="300",
                    channel={"id": "901", "type": 1, "recipients": [registered_user]},
                    guild_id=None,
                    owners={"1": "300"},
                    context=2,
                ),
                False,
                False,
            ),
            # Missing interaction context: fails closed in both modes.
            (
                "bot_dm_missing_context",
                dict(
                    user_id="300",
                    channel={"id": "900", "type": 1, "recipients": [registered_user]},
                    guild_id=None,
                    owners={"0": "0"},
                    context=None,
                ),
                False,
                False,
            ),
            # Owner on allowed_user_ids in bot DM: refused when users.enabled=False (unregistered)
            # because allowed_channel_ids is non-empty; passes when users.enabled=True.
            (
                "owner_bot_dm",
                dict(
                    user_id="100",
                    channel={"id": "902", "type": 1, "recipients": [owner_user]},
                    guild_id=None,
                    owners={"0": "0"},
                    context=1,
                ),
                True,
                users_enabled,
            ),
            # Allowed user in allowed guild channel: passes in both modes.
            (
                "guild_channel_allowed",
                dict(
                    user_id="100",
                    channel={
                        "id": "200",
                        "type": 0,
                        "name": "allowed",
                        "position": 0,
                        "guild_id": "555",
                    },
                    guild_id="555",
                    owners={"0": "555"},
                    context=0,
                ),
                False,
                True,
            ),
            # Allowed user in disallowed guild channel: refused in both modes.
            (
                "guild_channel_other",
                dict(
                    user_id="100",
                    channel={
                        "id": "999",
                        "type": 0,
                        "name": "other",
                        "position": 0,
                        "guild_id": "555",
                    },
                    guild_id="555",
                    owners={"0": "555"},
                    context=0,
                ),
                False,
                False,
            ),
            # Missing channel payload: channel and channel_id are both None; refused in both modes.
            (
                "no_channel",
                dict(
                    user_id="100",
                    channel=None,
                    guild_id="555",
                    owners={"0": "555"},
                    context=0,
                ),
                False,
                False,
            ),
        ]

        expected_calls = 0
        for case_name, kwargs, expected_direct, expected_allowed in cases:
            inter = _build_real_discord_interaction(state, **kwargs)
            if case_name == "bot_dm_user_install":
                assert inter.is_user_integration() is True
                assert inter.context.dm_channel is True
            if case_name == "private_dm_user_install":
                assert inter.is_user_integration() is True
                assert inter.context.private_channel is True
            if case_name == "no_channel":
                assert inter.channel is None
                assert inter.channel_id is None

            assert discord_module._is_direct_interaction(inter) is expected_direct, case_name
            assert app._is_allowed_interaction(inter) is expected_allowed, case_name

            caplog.clear()
            with caplog.at_level(logging.WARNING):
                await app._handle_slash_command(inter, "/list_sessions")
            missing_ctx_warnings = [
                record
                for record in caplog.records
                if record.levelno == logging.WARNING
                and "event=discord_interaction_context_missing" in record.getMessage()
            ]
            if case_name == "bot_dm_missing_context":
                assert len(missing_ctx_warnings) == 1, case_name
                assert "user_id=300" in missing_ctx_warnings[0].getMessage(), case_name
            else:
                assert missing_ctx_warnings == [], case_name
            if expected_allowed:
                expected_calls += 1
                assert len(application.command_calls) == expected_calls, case_name
                assert application.command_calls[-1]["private_chat"] is expected_direct, case_name
                assert inter.response.defer_calls == [{"thinking": True}], case_name
                assert len(inter.followup.messages) == 1, case_name
                assert inter.followup.message_calls[0].get("ephemeral") is not True, case_name
            else:
                assert len(application.command_calls) == expected_calls, case_name
                assert inter.response.defer_calls == [], case_name
                assert inter.followup.messages == [], case_name
                assert len(inter.response.message_calls) == 1, case_name
                assert inter.response.message_calls[0]["ephemeral"] is True, case_name
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_discord_every_registered_slash_command_enforces_allowlist(
    tmp_path: Path,
) -> None:
    app, application, operations = _build_slash_allowlist_app(
        tmp_path, users_enabled=False
    )
    assert discord_module.app_commands is not None

    registered: list[object] = []

    class _FakeTree:
        def command(self, *, name: str, description: str):
            def _decorator(func):
                registered.append((name, func))
                return func

            return _decorator

        def add_command(self, group) -> None:
            for sub in group.commands:
                registered.append((f"{group.name} {sub.name}", sub.callback))

    await app._register_slash_commands(SimpleNamespace(tree=_FakeTree()))  # type: ignore[arg-type]
    assert len(registered) == len(discord_module.DISCORD_COMMAND_SPECS)

    for _name, callback in registered:
        interaction = _FakeInteraction(
            user_id=999, channel=_FakeTextChannel(200), guild_id=555
        )
        sig = inspect.signature(callback)
        kwargs = {}
        for param_name, param in list(sig.parameters.items())[1:]:
            if param.default is inspect.Parameter.empty:
                kwargs[param_name] = "session_owner"
        await callback(interaction, **kwargs)
        assert interaction.response.defer_calls == []
        assert len(interaction.response.message_calls) == 1
        assert interaction.response.message_calls[0]["ephemeral"] is True

    assert application.command_calls == []
    assert operations.stop_calls == []
