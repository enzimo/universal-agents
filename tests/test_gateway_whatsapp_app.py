from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import assistant_gateway.interactive_apps.whatsapp as whatsapp_module
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.command_composition import build_gateway_command_application
from tests.application_test_doubles import (
    RecordingLinkResults,
    application_from as GatewayApplicationFacade,
    interactive_app_runtime_from,
)
from assistant_gateway.gateway import GatewayDeliveryEvent
from assistant_gateway.interactive_apps.base import DEFAULT_PROGRESS_NOTICE
from assistant_gateway.interactive_apps.whatsapp import (
    WhatsAppInteractiveApp as _WhatsAppInteractiveApp,
)
from assistant_gateway.runtime_ports import OwnershipPrivateResourceAuthorizer
from assistant_gateway.store import GatewayStateStore


class WhatsAppInteractiveApp(_WhatsAppInteractiveApp):
    """Compose explicit host ports around aggregate test fixtures."""

    def __init__(self, config, application, bridge) -> None:
        super().__init__(
            config.interactive_apps.whatsapp,
            application,
            bridge,
            interactive_app_runtime_from(config, application),
        )


class _FakeMessageResource:
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(sid=f"SM{len(self.created)}")


class _FakeTwilioClient:
    instances: list["_FakeTwilioClient"] = []

    def __init__(self, account_sid: str, auth_token: str) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.messages = _FakeMessageResource()
        self.__class__.instances.append(self)


class _FakeRequestValidator:
    instances: list["_FakeRequestValidator"] = []

    def __init__(self, auth_token: str) -> None:
        self.auth_token = auth_token
        self.calls: list[tuple[str, dict[str, str], str]] = []
        self.__class__.instances.append(self)

    def validate(self, url: str, payload: dict[str, str], signature: str) -> bool:
        self.calls.append((url, payload, signature))
        return True


class _FakeRouter:
    def __init__(self) -> None:
        self.routes: list[tuple[str, object]] = []

    def add_post(self, path: str, handler) -> None:
        self.routes.append((path, handler))


class _FakeWebApplication:
    def __init__(self) -> None:
        self.router = _FakeRouter()


class _FakeAppRunner:
    def __init__(self, app) -> None:
        self.app = app
        self.setup_called = False
        self.cleanup_called = False

    async def setup(self) -> None:
        self.setup_called = True

    async def cleanup(self) -> None:
        self.cleanup_called = True


class _FakeTCPSite:
    instances: list["_FakeTCPSite"] = []

    def __init__(self, runner, *, host: str, port: int) -> None:
        self.runner = runner
        self.host = host
        self.port = port
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _FakeGateway:
    def __init__(self, state_store: GatewayStateStore) -> None:
        self.state_store = state_store
        self.session_repository = state_store.session_repository
        self.request_repository = state_store.request_repository
        self.delivery_repository = state_store.delivery_repository
        self.sent_user_messages: list[dict[str, str | None]] = []

    @staticmethod
    def canonical_user_id_for_context(**_context):
        return None

    @staticmethod
    def gateway_users_enabled() -> bool:
        return False

    @staticmethod
    def requester_timezone_for_context(**_context) -> str:
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

    async def send_user_message(self, text: str, **kwargs):
        self.sent_user_messages.append({"text": text, **kwargs})
        return SimpleNamespace(
            text=f"Reply: {text}",
            conversation_id=kwargs.get("conversation_id") or "conv_reply",
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
            channel_name == "whatsapp"
            and chat_scope == ""
            and external_user_id in self.registered_user_ids
        ):
            return "user_registered"
        return None


def _config(tmp_path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="mybot",
        instance_root=str(tmp_path / "instance"),
        team_id="team_303",
        timezone="America/Phoenix",
    )
    config.interactive_apps.whatsapp.enabled = True
    return config


def test_whatsapp_app_warns_when_enabled_without_allowlists(tmp_path, caplog) -> None:
    config = _config(tmp_path)
    app = WhatsAppInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )

    with caplog.at_level(logging.WARNING):
        app._warn_if_allow_all()

    assert "empty allowed_sender_ids" in caplog.text


def test_whatsapp_app_logs_newuser_target_for_disallowed_sender(
    tmp_path, caplog
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.whatsapp.allowed_sender_ids = ["+15550000000"]
    app = WhatsAppInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )

    with caplog.at_level(logging.INFO):
        allowed = app._is_allowed(
            chat_id="+15551234567",
            user_id="+15551234567",
            external_user_id="whatsapp:+15551234567",
            wa_id="15551234567",
        )

    assert allowed is False
    assert "Ignoring WhatsApp message from non-allowed sender" in caplog.text
    assert "newuser_target=+15551234567" in caplog.text


def test_whatsapp_app_allows_registered_direct_user_without_allowlist_restart(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.whatsapp.allowed_sender_ids = ["+15550000000"]
    app = WhatsAppInteractiveApp(
        config,
        application=GatewayApplicationFacade(_RegisteredUserGateway({"+15551234567"})),
        bridge=SimpleNamespace(),
    )

    assert (
        app._is_allowed(
            chat_id="+15551234567",
            user_id="+15551234567",
            external_user_id="whatsapp:+15551234567",
            wa_id="15551234567",
        )
        is True
    )


@pytest.mark.asyncio
async def test_whatsapp_app_start_builds_webhook_listener(
    monkeypatch, tmp_path
) -> None:
    config = _config(tmp_path)
    app = WhatsAppInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )
    fake_app = _FakeWebApplication()
    _FakeTwilioClient.instances.clear()
    _FakeRequestValidator.instances.clear()
    _FakeTCPSite.instances.clear()

    def _application() -> _FakeWebApplication:
        return fake_app

    monkeypatch.setattr(whatsapp_module, "TwilioClient", _FakeTwilioClient)
    monkeypatch.setattr(whatsapp_module, "RequestValidator", _FakeRequestValidator)
    monkeypatch.setattr(
        whatsapp_module,
        "web",
        SimpleNamespace(
            Application=_application,
            AppRunner=_FakeAppRunner,
            TCPSite=_FakeTCPSite,
            Response=lambda **kwargs: SimpleNamespace(**kwargs),
        ),
    )
    monkeypatch.setattr(
        whatsapp_module, "_configure_twilio_library_logging", lambda: None
    )
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "whatsapp:+15550001111")

    await app.start()
    await app.stop()

    assert len(_FakeTwilioClient.instances) == 1
    assert _FakeTwilioClient.instances[0].account_sid == "AC123"
    assert len(_FakeRequestValidator.instances) == 1
    assert fake_app.router.routes[0][0] == "/webhooks/whatsapp"
    assert _FakeTCPSite.instances[-1].host == "0.0.0.0"
    assert _FakeTCPSite.instances[-1].port == 8080
    assert _FakeTCPSite.instances[-1].started is True
    assert _FakeTCPSite.instances[-1].stopped is True


@pytest.mark.asyncio
async def test_whatsapp_app_routes_inbound_messages_and_delivers_outbound(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _FakeGateway(store)
    bridge = _FakeBridge(
        [
            SimpleNamespace(
                channel_name="whatsapp",
                chat_id="+15551234567",
                thread_id=None,
                text="Manager says hello",
                in_reply_to_message_id="SM123",
                session_key="session_1",
                conversation_id="conv_1",
                suppress_delivery=False,
            )
        ]
    )
    app = WhatsAppInteractiveApp(
        config, application=GatewayApplicationFacade(gateway), bridge=bridge
    )
    app._client = _FakeTwilioClient("AC123", "auth-secret")
    app._from_address = "whatsapp:+15550001111"
    store.create_session(
        session_key="session_1",
        conversation_id="conv_1",
        manager_agent_id="manager_1",
        channel_name="whatsapp",
        user_id="+15551234567",
        chat_id="+15551234567",
        thread_id=None,
    )

    await app._process_inbound_payload(
        {
            "From": "whatsapp:+15551234567",
            "Body": "hello from whatsapp",
            "MessageSid": "SM123",
            "WaId": "15551234567",
        }
    )

    assert bridge.inbound_messages[0].channel_name == "whatsapp"
    assert bridge.inbound_messages[0].chat_id == "+15551234567"
    assert bridge.inbound_messages[0].text == "hello from whatsapp"
    assert app._client.messages.created[0]["to"] == "whatsapp:+15551234567"
    assert app._client.messages.created[0]["from_"] == "whatsapp:+15550001111"
    assert app._client.messages.created[0]["body"] == "Manager says hello"


@pytest.mark.asyncio
async def test_whatsapp_app_routes_media_attachment_to_manager(
    monkeypatch, tmp_path
) -> None:
    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _FakeGateway(store)
    bridge = _FakeBridge()
    app = WhatsAppInteractiveApp(
        config, application=GatewayApplicationFacade(gateway), bridge=bridge
    )
    app._client = _FakeTwilioClient("AC123", "auth-secret")
    app._from_address = "whatsapp:+15550001111"

    async def _download(self, media):
        return b"whatsapp-png", media.content_type

    monkeypatch.setattr(WhatsAppInteractiveApp, "_download_twilio_media", _download)

    await app._process_inbound_payload(
        {
            "From": "whatsapp:+15551234567",
            "Body": "see image",
            "MessageSid": "SM124",
            "WaId": "15551234567",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages/SM124/Media/ME123",
            "MediaContentType0": "image/png",
            "MediaSid0": "ME123",
        }
    )

    assert len(bridge.inbound_messages) == 1
    inbound = bridge.inbound_messages[0]
    assert inbound.text == "see image"
    assert len(inbound.attachments) == 1
    attachment = inbound.attachments[0]
    assert attachment.origin == "whatsapp"
    assert attachment.kind == "image"
    assert attachment.caption == "see image"
    assert attachment.local_path()
    assert gateway.state_store.get_attachment(attachment.attachment_id) is not None


@pytest.mark.asyncio
async def test_whatsapp_app_deliver_event_sends_proactive_notice(tmp_path) -> None:
    config = _config(tmp_path)
    app = WhatsAppInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )
    app._client = _FakeTwilioClient("AC123", "auth-secret")
    app._from_address = "whatsapp:+15550001111"

    await app.deliver_event(
        GatewayDeliveryEvent(
            text="Gateway notice",
            session_key="session_notice",
            channel_name="whatsapp",
            user_id="+15551234567",
            chat_id="+15551234567",
            thread_id=None,
            conversation_id=None,
            scheduled_job_id=None,
            correlation_id=None,
            original_message_id=None,
        )
    )

    assert app._client.messages.created == [
        {
            "body": "Gateway notice",
            "from_": "whatsapp:+15550001111",
            "to": "whatsapp:+15551234567",
        }
    ]


@pytest.mark.asyncio
async def test_whatsapp_app_reply_command_is_cli_only(tmp_path) -> None:
    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _FakeGateway(store)
    app = WhatsAppInteractiveApp(
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
        bridge=_FakeBridge(),
    )
    app._client = _FakeTwilioClient("AC123", "auth-secret")
    app._from_address = "whatsapp:+15550001111"

    await app._process_inbound_payload(
        {
            "From": "whatsapp:+15551234567",
            "Body": "/reply msg_1 Continue that thread",
            "MessageSid": "SM124",
        }
    )

    assert gateway.sent_user_messages == []
    assert (
        app._client.messages.created[-1]["body"]
        == "The /reply command is available only in the local CLI."
    )


@pytest.mark.asyncio
async def test_whatsapp_app_rejects_invalid_twilio_signature(tmp_path) -> None:
    class _InvalidValidator:
        def validate(self, url: str, payload: dict[str, str], signature: str) -> bool:
            return False

    class _FakeWebhookRequest:
        headers = {"X-Twilio-Signature": "bad-signature"}
        path_qs = "/webhooks/whatsapp"
        url = "https://gateway.example.com/webhooks/whatsapp"

        async def post(self):
            return {
                "From": "whatsapp:+15551234567",
                "Body": "hello",
                "MessageSid": "SM125",
            }

    config = _config(tmp_path)
    app = WhatsAppInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )
    app._validator = _InvalidValidator()

    response = await app._handle_webhook(_FakeWebhookRequest())

    assert response.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_delay", [0, 0.05])
async def test_whatsapp_app_sends_periodic_progress_updates_without_native_indicator(
    tmp_path, monkeypatch, startup_delay
) -> None:
    release = asyncio.Event()

    class _SlowBridge:
        def __init__(self) -> None:
            self.inbound_messages = []

        async def handle_inbound(self, inbound):
            self.inbound_messages.append(inbound)
            await release.wait()
            return [
                SimpleNamespace(
                    channel_name="whatsapp",
                    chat_id="+15551234567",
                    thread_id=None,
                    text="Manager says hello",
                    in_reply_to_message_id="SM123",
                    session_key="session_1",
                    conversation_id="conv_1",
                    suppress_delivery=False,
                )
            ]

    monkeypatch.setattr(
        WhatsAppInteractiveApp,
        "progress_notice_interval_seconds",
        lambda self: 0.01,
    )

    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _FakeGateway(store)
    bridge = _SlowBridge()
    app = WhatsAppInteractiveApp(
        config, application=GatewayApplicationFacade(gateway), bridge=bridge
    )
    app._client = _FakeTwilioClient("AC123", "auth-secret")
    app._from_address = "whatsapp:+15550001111"

    progress_sent = asyncio.Event()
    loop = asyncio.get_running_loop()
    create_message = app._client.messages.create

    def observe_message(**kwargs):
        result = create_message(**kwargs)
        if kwargs.get("body") == DEFAULT_PROGRESS_NOTICE:
            # The Twilio adapter can invoke this synchronous client in a worker.
            loop.call_soon_threadsafe(progress_sent.set)
        return result

    monkeypatch.setattr(app._client.messages, "create", observe_message)

    async def process_inbound():
        # Exercise startup slower than the old test's 30 ms release deadline.
        await asyncio.sleep(startup_delay)
        await app._process_inbound_payload(
            {
                "From": "whatsapp:+15551234567",
                "Body": "hello from whatsapp",
                "MessageSid": "SM123",
                "WaId": "15551234567",
            }
        )

    task = asyncio.create_task(process_inbound())
    try:
        await asyncio.wait_for(progress_sent.wait(), timeout=5)
    finally:
        release.set()
        await task

    assert any(
        message["body"] == DEFAULT_PROGRESS_NOTICE
        for message in app._client.messages.created
    )
    assert app._client.messages.created[-1]["body"] == "Manager says hello"


@pytest.mark.asyncio
async def test_whatsapp_app_sends_timeout_notice_when_manager_wait_expires(
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
    app = WhatsAppInteractiveApp(
        config, application=GatewayApplicationFacade(gateway), bridge=_TimeoutBridge()
    )
    app._client = _FakeTwilioClient("AC123", "auth-secret")
    app._from_address = "whatsapp:+15550001111"

    await app._process_inbound_payload(
        {
            "From": "whatsapp:+15551234567",
            "Body": "hello from whatsapp",
            "MessageSid": "SM123",
            "WaId": "15551234567",
        }
    )

    assert app._client.messages.created[-1]["body"] == (
        "I'm still waiting on that task, but it did not complete within the allotted 3600s timeout."
    )
