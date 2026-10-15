from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import assistant_gateway.interactive_apps.telegram as telegram_module
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.command_composition import build_gateway_command_application
from tests.application_test_doubles import (
    RecordingLinkResults,
    application_from as GatewayApplicationFacade,
    interactive_app_runtime_from,
)
from assistant_gateway.gateway import GatewayDeliveryEvent
from assistant_gateway.interactive_apps.manager import InteractiveAppManager
from assistant_gateway.interactive_apps.telegram import (
    TelegramInteractiveApp as _TelegramInteractiveApp,
    _markdown_to_telegram_html,
)
from assistant_gateway.runtime_ports import OwnershipPrivateResourceAuthorizer
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.voice import SpeechResponse, TranscriptionResponse
from tests.staged_attachment_helpers import change_staged_copy, staged_attachment


class TelegramInteractiveApp(_TelegramInteractiveApp):
    """Compose explicit host ports around aggregate test fixtures."""

    def __init__(self, config, application, bridge) -> None:
        factory = getattr(telegram_module, "build_voice_client", None)
        voice_client = factory(config.voice) if callable(factory) else None
        super().__init__(
            config.interactive_apps.telegram,
            application,
            bridge,
            interactive_app_runtime_from(
                config, application, voice_client=voice_client
            ),
        )


class _FakeHTTPXRequest:
    instances: list["_FakeHTTPXRequest"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    @classmethod
    def clear(cls) -> None:
        cls.instances.clear()


class _FakeUpdater:
    def __init__(self) -> None:
        self.start_kwargs = None
        self.start_call_count = 0
        self.stopped = False
        self.stop_call_count = 0

    async def start_polling(self, **kwargs) -> None:
        self.start_kwargs = kwargs
        self.start_call_count += 1

    async def stop(self) -> None:
        self.stopped = True
        self.stop_call_count += 1


class _FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self.sent_audios: list[dict] = []
        self.sent_photos: list[dict] = []
        self.sent_documents: list[dict] = []
        self.chat_actions: list[dict] = []
        self.commands = None
        self.files: dict[str, bytes] = {}

    async def get_me(self):
        return SimpleNamespace(id=999, username="team_test_bot")

    async def set_my_commands(self, commands):
        self.commands = list(commands)

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent_messages))

    async def send_audio(self, **kwargs):
        self.sent_audios.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent_audios))

    async def send_photo(self, **kwargs):
        self.sent_photos.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent_photos))

    async def send_document(self, **kwargs):
        self.sent_documents.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent_documents))

    async def send_chat_action(self, **kwargs):
        self.chat_actions.append(kwargs)

    async def get_file(self, file_id: str):
        return _FakeTelegramFile(self.files[file_id])


class _FakeRichBot(_FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.api_requests: list[dict] = []
        self.api_request_error: Exception | None = None

    async def _post(self, endpoint: str, data=None, *, api_kwargs=None, **kwargs):
        self.api_requests.append(
            {
                "endpoint": endpoint,
                "data": data,
                "api_kwargs": dict(api_kwargs or {}),
                "kwargs": kwargs,
            }
        )
        if self.api_request_error is not None:
            raise self.api_request_error
        return SimpleNamespace(message_id=len(self.api_requests))


class _FakeTelegramFile:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def download_as_bytearray(self):
        return bytearray(self.body)


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()
        self.updater = _FakeUpdater()
        self.handlers = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)

    async def initialize(self) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class _FakeRichApp(_FakeApp):
    def __init__(self) -> None:
        super().__init__()
        self.bot = _FakeRichBot()


class _FakeBuilder:
    def __init__(self, app: _FakeApp) -> None:
        self.app = app
        self.request_value = None
        self.get_updates_request_value = None
        self.token_value = None
        self.concurrent_updates_value = None

    def token(self, token: str):
        self.token_value = token
        return self

    def request(self, request):
        self.request_value = request
        return self

    def get_updates_request(self, request):
        self.get_updates_request_value = request
        return self

    def concurrent_updates(self, value):
        self.concurrent_updates_value = value
        return self

    def build(self):
        return self.app


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

    def requester_timezone_for_context(self, **_context) -> str:
        return "UTC"

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

    async def send_user_message(self, text: str, **kwargs):
        self.sent_user_messages.append({"text": text, **kwargs})
        return SimpleNamespace(
            text=f"Reply: {text}",
            conversation_id=kwargs.get("conversation_id") or "conv_reply",
        )

    def switch_session_conversation(
        self,
        *,
        session_key: str,
        conversation_id: str,
        last_task_id: str | None,
    ):
        self.session_repository.upsert_session(
            session_key=session_key,
            conversation_id=conversation_id,
            last_task_id=last_task_id,
            manager_agent_id=None,
        )
        return self.session_repository.get_session(session_key)


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
            channel_name == "telegram"
            and chat_scope == ""
            and external_user_id in self.registered_user_ids
        ):
            return "user_registered"
        return None


def _make_update(
    *,
    text: str,
    caption: str | None = None,
    chat_id: int = -100123,
    user_id: int = 777,
    message_id: int = 9,
    thread_id: int | None = None,
    chat_type: str = "supergroup",
    voice=None,
    photo=None,
    document=None,
):
    message = SimpleNamespace(
        chat_id=chat_id,
        message_id=message_id,
        message_thread_id=thread_id,
        text=text,
        caption=caption,
        voice=voice,
        audio=None,
        photo=list(photo or []),
        document=document,
        video=None,
        animation=None,
        sticker=None,
        entities=[],
        reply_to_message=None,
        sender_chat=None,
    )
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        channel_post=None,
    )


def _config(tmp_path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="mybot",
        instance_root=str(tmp_path / "instance"),
        team_id="team_303",
        timezone="America/Phoenix",
    )
    config.interactive_apps.telegram.enabled = True
    return config


def test_telegram_app_warns_when_enabled_without_allowlists(tmp_path, caplog) -> None:
    config = _config(tmp_path)
    app = TelegramInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )

    with caplog.at_level(logging.WARNING):
        app._warn_if_allow_all()

    assert "empty allowed_user_ids and allowed_chat_ids" in caplog.text


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
        return TranscriptionResponse(text="transcribed voice request")

    async def synthesize(self, request):
        if self.synthesize_error is not None:
            raise self.synthesize_error
        self.speeches.append(request.text)
        return SpeechResponse(audio=b"mp3-reply", content_type="audio/mpeg")


@pytest.mark.asyncio
async def test_telegram_app_start_builds_separate_request_pools(
    monkeypatch, tmp_path
) -> None:
    config = _config(tmp_path)
    app = TelegramInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )
    fake_app = _FakeApp()
    builder = _FakeBuilder(fake_app)
    _FakeHTTPXRequest.clear()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(telegram_module, "HTTPXRequest", _FakeHTTPXRequest)
    monkeypatch.setattr(
        telegram_module,
        "Application",
        SimpleNamespace(builder=lambda: builder),
    )
    monkeypatch.setattr(
        telegram_module,
        "BotCommand",
        lambda *, command, description: SimpleNamespace(
            command=command, description=description
        ),
    )
    monkeypatch.setattr(
        telegram_module,
        "CommandHandler",
        lambda *args, **kwargs: ("command", args, kwargs),
    )
    monkeypatch.setattr(
        telegram_module,
        "MessageHandler",
        lambda *args, **kwargs: ("message", args, kwargs),
    )
    monkeypatch.setattr(telegram_module, "filters", SimpleNamespace(ALL=object()))

    await app.start()
    await app.stop()

    assert len(_FakeHTTPXRequest.instances) == 2
    assert _FakeHTTPXRequest.instances[0].kwargs["connection_pool_size"] == 32
    assert _FakeHTTPXRequest.instances[1].kwargs["connection_pool_size"] == 4
    assert builder.request_value is _FakeHTTPXRequest.instances[0]
    assert builder.get_updates_request_value is _FakeHTTPXRequest.instances[1]
    assert (
        builder.concurrent_updates_value
        == config.interactive_apps.telegram.concurrent_updates
    )
    assert fake_app.updater.start_kwargs["allowed_updates"] == [
        "message",
        "channel_post",
        "callback_query",
    ]
    assert fake_app.updater.start_call_count == 1
    # Commands, access review buttons, and messages.
    assert len(fake_app.handlers) == 3
    assert [command.command for command in fake_app.bot.commands] == list(
        telegram_module.TELEGRAM_COMMANDS
    )
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


@pytest.mark.asyncio
async def test_telegram_app_falls_back_when_rich_message_is_rejected(
    monkeypatch, tmp_path, caplog
) -> None:
    config = _config(tmp_path)
    app = TelegramInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )
    app._app = _FakeRichApp()
    app._app.bot.api_request_error = RuntimeError(
        "Bad Request: can't parse rich_message markdown"
    )
    monkeypatch.setattr(telegram_module, "ParseMode", SimpleNamespace(HTML="HTML"))

    with caplog.at_level(logging.WARNING):
        await app.deliver_event(
            GatewayDeliveryEvent(
                text="**Done**",
                session_key="session_1",
                channel_name="telegram",
                user_id="u1",
                chat_id="-100123",
                thread_id=None,
                conversation_id="conv_1",
                scheduled_job_id=None,
                correlation_id="corr_1",
                original_message_id=None,
            )
        )

    assert len(app._app.bot.api_requests) == 1
    assert len(app._app.bot.sent_messages) == 1
    assert app._app.bot.sent_messages[0]["text"] == "<b>Done</b>"
    assert app._rich_messages_disabled is False
    assert "falling back to legacy HTML sendMessage" in caplog.text


@pytest.mark.asyncio
async def test_telegram_app_disables_rich_messages_after_capability_rejection(
    monkeypatch, tmp_path, caplog
) -> None:
    config = _config(tmp_path)
    app = TelegramInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )
    app._app = _FakeRichApp()
    app._app.bot.api_request_error = RuntimeError(
        "Endpoint sendRichMessage does not exist"
    )
    monkeypatch.setattr(telegram_module, "ParseMode", SimpleNamespace(HTML="HTML"))

    with caplog.at_level(logging.WARNING):
        await app._send_text(
            chat_id=-100123,
            text="**First**",
            thread_id=None,
            reply_to_message_id=None,
        )
    app._app.bot.api_request_error = None
    await app._send_text(
        chat_id=-100123,
        text="**Second**",
        thread_id=None,
        reply_to_message_id=None,
    )

    assert len(app._app.bot.api_requests) == 1
    assert [message["text"] for message in app._app.bot.sent_messages] == [
        "<b>First</b>",
        "<b>Second</b>",
    ]
    assert app._rich_messages_disabled is True
    assert "rich messages disabled" in caplog.text


@pytest.mark.asyncio
async def test_telegram_app_delivers_image_attachment(monkeypatch, tmp_path) -> None:
    class _ReplyParameters:
        def __init__(
            self, *, message_id: int, allow_sending_without_reply: bool
        ) -> None:
            self.message_id = message_id
            self.allow_sending_without_reply = allow_sending_without_reply

    config = _config(tmp_path)
    screenshot = staged_attachment(
        config,
        data=b"png-data",
        filename="site.png",
        content_type="image/png",
        kind="image",
        caption="Screenshot",
    )
    changed = staged_attachment(
        config, data=b"report", filename="report.pdf", content_type="application/pdf"
    )
    # Agent code changes this copy after staging: it must not be sent.
    change_staged_copy(changed, b"not the checked report")
    app = TelegramInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )
    app._app = _FakeApp()
    monkeypatch.setattr(telegram_module, "ReplyParameters", _ReplyParameters)

    await app.deliver_event(
        GatewayDeliveryEvent(
            text="Screenshot attached.",
            session_key="session_1",
            channel_name="telegram",
            user_id="u1",
            chat_id="-100123",
            thread_id="42",
            conversation_id="conv_1",
            scheduled_job_id=None,
            correlation_id="corr_1",
            original_message_id="9",
            attachments=(screenshot, changed),
        )
    )

    assert len(app._app.bot.sent_messages) == 1
    assert len(app._app.bot.sent_photos) == 1
    assert getattr(app._app.bot, "sent_documents", []) == []
    sent = app._app.bot.sent_photos[0]
    assert sent["chat_id"] == -100123
    assert sent["message_thread_id"] == 42
    assert sent["reply_parameters"].message_id == 9
    assert sent["caption"] == "Screenshot"
    assert sent["photo"].getvalue() == b"png-data"
    assert sent["photo"].name == "site.png"


@pytest.mark.asyncio
async def test_interactive_app_manager_raises_for_explicit_app_start_failure(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    manager = InteractiveAppManager(
        config,
        application=SimpleNamespace(),
        output_sink=SimpleNamespace(),
        include_apps=["discord"],
    )
    events: list[str] = []

    class _FailingApp:
        enabled = True

        async def start(self) -> None:
            events.append("discord.start")
            raise RuntimeError("Discord app did not become ready within 30 seconds.")

        async def stop(self) -> None:
            events.append("discord.stop")

    manager.apps = {"discord": _FailingApp()}

    with pytest.raises(RuntimeError, match="Discord app did not become ready"):
        await manager.start()

    assert events == ["discord.start", "discord.stop"]
    assert manager.running_app_names == []


def test_telegram_app_logs_private_dm_chat_and_user_ids_clearly(
    caplog, tmp_path
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.telegram.allowed_chat_ids = ["-100123"]
    app = TelegramInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=100200300, type="private"),
        effective_user=SimpleNamespace(id=100200300),
        effective_message=None,
        channel_post=None,
    )

    with caplog.at_level("INFO"):
        allowed = app._is_allowed(update)

    assert allowed is False
    assert "non-allowed source chat" in caplog.text
    assert "chat_id=100200300" in caplog.text
    assert "chat_type=private" in caplog.text
    assert "user_id=100200300" in caplog.text
    assert "newuser_target=100200300" in caplog.text
    assert "private chats" in caplog.text


def test_telegram_app_allows_registered_direct_user_without_allowlist_restart(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.telegram.allowed_user_ids = ["111"]
    config.interactive_apps.telegram.allowed_chat_ids = ["-100123"]
    app = TelegramInteractiveApp(
        config,
        application=GatewayApplicationFacade(_RegisteredUserGateway({"100200300"})),
        bridge=SimpleNamespace(),
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=100200300, type="private"),
        effective_user=SimpleNamespace(id=100200300),
        effective_message=None,
        channel_post=None,
    )

    assert app._is_allowed(update) is True


@pytest.mark.asyncio
async def test_telegram_app_reply_command_is_cli_only(tmp_path) -> None:
    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _FakeGateway(store)
    app = TelegramInteractiveApp(
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
    app._app = _FakeApp()

    await app._handle_update_text(
        _make_update(
            text="/reply msg_1 continue this thread",
            chat_type="private",
            chat_id=777,
            user_id=777,
        ),
        text="/reply msg_1 continue this thread",
    )

    assert gateway.sent_user_messages == []
    assert (
        "The /reply command is available only in the local CLI."
        in app._app.bot.sent_messages[0]["text"]
    )


@pytest.mark.asyncio
async def test_telegram_voice_input_gets_audio_reply(monkeypatch, tmp_path) -> None:
    fake_voice = _FakeVoiceClient()
    monkeypatch.setattr(
        telegram_module,
        "build_voice_client",
        lambda _settings: fake_voice,
        raising=False,
    )
    config = _config(tmp_path)
    config.voice.enabled = True
    config.voice.input_enabled = True
    config.voice.output_enabled = True
    config.interactive_apps.telegram.group_policy = "open"
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _FakeGateway(store)
    bridge = _FakeBridge(
        [
            SimpleNamespace(
                channel_name="telegram",
                chat_id="-100123",
                thread_id=None,
                text="Manager voice reply",
                in_reply_to_message_id="9",
                session_key="session_1",
                conversation_id="conv_1",
                access_request_id=None,
            )
        ]
    )
    app = TelegramInteractiveApp(
        config, application=GatewayApplicationFacade(gateway), bridge=bridge
    )
    app._app = _FakeApp()
    app._app.bot.files["voice_file"] = b"ogg-voice"
    voice = SimpleNamespace(file_id="voice_file", mime_type="audio/ogg")

    await app._on_message(_make_update(text="", voice=voice), SimpleNamespace())

    assert fake_voice.transcriptions == [
        {
            "audio": b"ogg-voice",
            "filename": "telegram_voice.ogg",
            "content_type": "audio/ogg",
        }
    ]
    assert bridge.inbound_messages[0].text == "transcribed voice request"
    assert fake_voice.speeches == ["Manager voice reply"]
    assert len(app._app.bot.sent_messages) == 1
    sent_text = app._app.bot.sent_messages[0]["text"]
    assert "Voice request:" in sent_text
    assert "transcribed voice request" in sent_text
    assert "Assistant reply:" in sent_text
    assert "Manager voice reply" in sent_text
    assert len(app._app.bot.sent_audios) == 1
    sent_audio = app._app.bot.sent_audios[0]
    assert sent_audio["chat_id"] == -100123
    assert sent_audio["audio"].getvalue() == b"mp3-reply"


@pytest.mark.asyncio
async def test_telegram_disabled_voice_input_routes_audio_attachment(tmp_path) -> None:
    config = _config(tmp_path)
    config.voice.enabled = False
    config.voice.input_enabled = False
    config.interactive_apps.telegram.group_policy = "open"
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _FakeGateway(store)
    bridge = _FakeBridge()
    app = TelegramInteractiveApp(
        config, application=GatewayApplicationFacade(gateway), bridge=bridge
    )
    app._app = _FakeApp()
    app._app.bot.files["voice_file"] = b"ogg-voice"
    voice = SimpleNamespace(file_id="voice_file", mime_type="audio/ogg")

    await app._on_message(_make_update(text="", voice=voice), SimpleNamespace())

    assert len(bridge.inbound_messages) == 1
    inbound = bridge.inbound_messages[0]
    assert inbound.text == ""
    assert len(inbound.attachments) == 1
    assert inbound.attachments[0].origin == "telegram"
    assert inbound.attachments[0].kind == "audio"
    assert app._app.bot.sent_audios == []
    assert app._app.bot.sent_messages == []


@pytest.mark.asyncio
async def test_telegram_app_sends_timeout_notice_when_manager_wait_expires(
    tmp_path,
) -> None:
    class _TimeoutBridge:
        async def handle_inbound(self, inbound):
            raise TimeoutError(
                "Timed out after 3600s waiting for a terminal manager response."
            )

    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _FakeGateway(store)
    app = TelegramInteractiveApp(
        config, application=GatewayApplicationFacade(gateway), bridge=_TimeoutBridge()
    )
    app._app = _FakeApp()

    await app._handle_update_text(
        _make_update(
            text="hello from telegram", chat_type="private", chat_id=777, user_id=777
        ),
        text="hello from telegram",
    )

    assert app._app.bot.chat_actions == [
        {
            "chat_id": 777,
            "action": "typing",
        }
    ]
    assert (
        "did not complete within the allotted 3600s timeout"
        in app._app.bot.sent_messages[-1]["text"]
    )


def test_telegram_underlines_double_underscores_like_the_web_chat() -> None:
    html = _markdown_to_telegram_html(
        "- **Exa web search** (`http:exa`): shared. __Team account__: linked. __Your own__: linked.\n"
        "Files like __init__.py stay as typed. `__code__` and `**literal** <tag> &` stay code."
    )

    assert html.startswith("• <b>Exa web search</b> (<code>http:exa</code>)")
    assert "<u>Team account</u>: linked. <u>Your own</u>: linked." in html
    assert "__init__.py" in html
    assert "<code>__code__</code>" in html
    assert "<code>**literal** &lt;tag&gt; &amp;</code>" in html


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["rich", "legacy", "rejected_rich"])
async def test_telegram_delivery_underlines_labels_and_preserves_code(
    delivery, monkeypatch, tmp_path
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.telegram.rich_messages = delivery != "legacy"
    app = TelegramInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )
    app._app = _FakeRichApp()
    if delivery == "rejected_rich":
        app._app.bot.api_request_error = RuntimeError("Bad Request: can't parse rich_message markdown")
    monkeypatch.setattr(telegram_module, "ParseMode", SimpleNamespace(HTML="HTML"))
    text = (
        "- **Exa web search** (`http:exa`): __Team account__: linked. __Your own__: linked.\n"
        "Run `print(__name__)` and `echo **literal**`.\n"
        "```python\nvalue = '__Team account__'\nprint(`__literal__`)\n```"
    )

    await app._send_text(chat_id=123, text=text, thread_id=None, reply_to_message_id=None)

    if delivery == "rich":
        assert not app._app.bot.sent_messages
        request = app._app.bot.api_requests[0]
        assert request["endpoint"] == "sendRichMessage"
        rendered = request["api_kwargs"]["rich_message"]["markdown"]
        assert "**Exa web search** (`http:exa`)" in rendered
        assert "`print(__name__)` and `echo **literal**`" in rendered
        assert "```python\nvalue = '__Team account__'\nprint(`__literal__`)\n```" in rendered
    else:
        message = app._app.bot.sent_messages[0]
        assert message["parse_mode"] == "HTML"
        rendered = message["text"]
        assert "<b>Exa web search</b> (<code>http:exa</code>)" in rendered
        assert "<code>print(__name__)</code> and <code>echo **literal**</code>" in rendered
        assert "<pre><code>value = &#x27;__Team account__&#x27;\nprint(`__literal__`)</code></pre>" in rendered
    assert "<u>Team account</u>: linked. <u>Your own</u>: linked." in rendered
    assert rendered.count("<u>") == 2


def test_telegram_rich_markdown_preserves_names_and_link_destinations() -> None:
    text = (
        "__Label__ and [__Docs__](https://example.com/__token__).\n"
        "https://example.com/__token__ __init__.py café__cache__ __label__界"
    )

    rendered = telegram_module._telegram_rich_message_payload(text)["markdown"]

    assert "<u>Label</u> and [<u>Docs</u>](https://example.com/__token__)." in rendered
    assert rendered.endswith("https://example.com/__token__ __init__.py café__cache__ __label__界")


@pytest.mark.parametrize("destination", [
    "mailto:person+__filter__@example.org", "tel:+123__456__", "tg://resolve?domain=__name__",
    "/nested((__token__))path",
])
def test_telegram_rich_markdown_keeps_supported_link_destinations(destination) -> None:
    text = f"[__Link__]({destination})"
    assert telegram_module._telegram_rich_message_payload(text)["markdown"] == f"[<u>Link</u>]({destination})"


@pytest.mark.parametrize("block", [
    "```python\n__literal__\n````",
    "~~~python\n__literal__\n~~~",
    "```python\n__literal__",
    "~~~python\n__literal__",
])
def test_telegram_keeps_fenced_code_while_underlining_prose(block) -> None:
    text = f"__Label__\n{block}"
    assert telegram_module._telegram_rich_message_payload(text)["markdown"] == f"<u>Label</u>\n{block}"
    assert _markdown_to_telegram_html(text) == "<u>Label</u>\n<pre><code>__literal__</code></pre>"


def test_telegram_multiline_inline_code_keeps_every_line() -> None:
    text = "Use ```first line\nsecond line``` as code."
    assert _markdown_to_telegram_html(text) == "Use <code>first line\nsecond line</code> as code."
    assert telegram_module._telegram_rich_message_payload(text)["markdown"] == text


@pytest.mark.asyncio
async def test_telegram_legacy_delivery_preserves_code_across_message_splits(monkeypatch, tmp_path) -> None:
    from html.parser import HTMLParser

    class MessageText(HTMLParser):
        def __init__(self):
            super().__init__()
            self.text = ""
            self.tags = []

        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)

        def handle_endtag(self, tag):
            assert self.tags.pop() == tag

        def handle_data(self, data):
            self.text += data

    config = _config(tmp_path)
    config.interactive_apps.telegram.rich_messages = False
    app = TelegramInteractiveApp(
        config, application=GatewayApplicationFacade(SimpleNamespace()), bridge=SimpleNamespace()
    )
    app._app = _FakeRichApp()
    monkeypatch.setattr(telegram_module, "ParseMode", SimpleNamespace(HTML="HTML"))
    code = "x = 1\n" * 680 + "value = __literal__ & '<tag>'"

    await app._send_text(chat_id=123, text=f"```python\n{code}\n```", thread_id=None, reply_to_message_id=None)

    messages = app._app.bot.sent_messages
    assert len(messages) > 1
    decoded = []
    for message in messages:
        rendered = message["text"]
        assert message["parse_mode"] == "HTML"
        assert len(rendered) <= telegram_module.TELEGRAM_MAX_MESSAGE_LEN
        assert rendered.startswith("<pre><code>") and rendered.endswith("</code></pre>")
        assert "<u>" not in rendered and "```" not in rendered
        parser = MessageText()
        parser.feed(rendered)
        assert not parser.tags
        decoded.append(parser.text)
    assert "".join(decoded) == code
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("__Before `x__` after__", "__Before <code>x__</code> after__"),
        ("__Your `cli:github`__", "__Your <code>cli:github</code>__"),
        ("**Bold `__code__` and __label__**", "**Bold <code>__code__</code> and <u>label</u>**"),
        ("`subtotal`- `discount`", "<code>subtotal</code>- <code>discount</code>"),
        ("`code`\n- list\n`- literal\n- unchanged`", "<code>code</code>\n• list\n<code>- literal\n- unchanged</code>"),
    ],
)
def test_telegram_formatting_respects_code_spans(text: str, expected: str) -> None:
    assert _markdown_to_telegram_html(text) == expected
