from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import assistant_gateway.interactive_apps.discord as discord_module
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
from assistant_gateway.runtime_ports import OwnershipPrivateResourceAuthorizer
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


class _FakeInteractionResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.defer_calls: list[dict] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs) -> None:
        self.defer_calls.append(kwargs)
        self._done = True

    async def send_message(self, text: str) -> None:
        self.messages.append(text)
        self._done = True


class _FakeInteractionFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


class _FakeInteraction:
    def __init__(self, *, user_id: int, channel) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.channel = channel
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
