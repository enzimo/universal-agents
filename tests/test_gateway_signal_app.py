from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import assistant_gateway.interactive_apps.signal as signal_module
from assistant_gateway.config import GatewayInstanceConfig
from tests.application_test_doubles import (
    application_from as GatewayApplicationFacade,
    interactive_app_runtime_from,
)
from assistant_gateway.interactive_apps.signal import (
    SignalInteractiveApp as _SignalInteractiveApp,
)
from assistant_gateway.store import GatewayStateStore


class SignalInteractiveApp(_SignalInteractiveApp):
    """Compose explicit host ports around aggregate test fixtures."""

    def __init__(self, config, application, bridge) -> None:
        super().__init__(
            config.interactive_apps.signal,
            application,
            bridge,
            interactive_app_runtime_from(config, application),
        )


class _FakeSignalClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.served_attachments: dict[str, tuple[bytes, str]] = {}

    async def send(
        self, *, account: str, recipients: list[str], message: str, text_mode: str
    ):
        self.sent.append(
            {
                "account": account,
                "recipients": recipients,
                "message": message,
                "text_mode": text_mode,
            }
        )
        return {"timestamp": "123"}

    async def serve_attachment(self, attachment: str):
        return self.served_attachments[attachment]


class _FakeBridge:
    def __init__(self, outbound_messages=None) -> None:
        self.outbound_messages = list(outbound_messages or [])
        self.inbound_messages = []

    async def handle_inbound(self, inbound):
        self.inbound_messages.append(inbound)
        return list(self.outbound_messages)


class _FakeGateway:
    def __init__(
        self, config: GatewayInstanceConfig, state_store: GatewayStateStore
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.session_repository = state_store.session_repository
        self.delivery_repository = state_store.delivery_repository
        self.sent_user_messages: list[dict[str, object]] = []

    def start_new_session(
        self, *, channel_name=None, user_id=None, chat_id=None, thread_id=None
    ):
        return self.state_store.create_session(
            session_key="session_new",
            conversation_id="conv_new",
            manager_agent_id="manager_1",
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
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


class _RegisteredUserGateway:
    def resolve_canonical_user(
        self, *, channel_name: str, external_user_id: str, chat_scope: str
    ):
        if (
            channel_name == "signal"
            and chat_scope == ""
            and external_user_id == "sender-uuid"
        ):
            return "user_registered"
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _config(tmp_path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="signalbot",
        instance_root=str(tmp_path / "instance"),
        team_id="team_signal",
        timezone="America/Chicago",
    )
    config.interactive_apps.signal.enabled = True
    config.interactive_apps.signal.account = "+15550001111"
    config.interactive_apps.signal.mention_markers = ["@Ava"]
    return config


def test_signal_app_warns_when_enabled_without_allowlists(tmp_path, caplog) -> None:
    config = _config(tmp_path)
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(SimpleNamespace()),
        bridge=SimpleNamespace(),
    )

    with caplog.at_level(logging.WARNING):
        app._warn_if_allow_all()

    assert "empty allowed_sender_ids and allowed_group_ids" in caplog.text


def _direct_payload(
    *, message: str = "hello from signal", timestamp: int = 123
) -> dict:
    return {
        "envelope": {
            "sourceNumber": "+15551234567",
            "sourceUuid": "sender-uuid",
            "sourceName": "Signal User",
            "timestamp": timestamp,
            "dataMessage": {
                "message": message,
                "timestamp": timestamp,
            },
        }
    }


def _group_payload(*, message: str = "@Ava hello group", timestamp: int = 123) -> dict:
    return {
        "envelope": {
            "sourceNumber": "+15551234567",
            "sourceUuid": "sender-uuid",
            "sourceName": "Signal User",
            "timestamp": timestamp,
            "dataMessage": {
                "message": message,
                "timestamp": timestamp,
                "groupInfo": {
                    "groupId": "group.signal",
                    "groupName": "Signal Group",
                    "revision": 1,
                    "type": "DELIVER",
                },
            },
        }
    }


@pytest.mark.asyncio
async def test_signal_app_start_creates_poll_task_and_stops_cleanly(
    monkeypatch, tmp_path
) -> None:
    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(config, store)),
        bridge=_FakeBridge(),
    )

    async def _about(self):
        return {"version": "test"}

    async def _receive(self, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(signal_module.SignalRestClient, "about", _about)
    monkeypatch.setattr(signal_module.SignalRestClient, "receive", _receive)
    monkeypatch.setattr(
        signal_module,
        "aiohttp",
        SimpleNamespace(ClientSession=_FakeSession, ClientTimeout=lambda total: total),
    )

    await app.start()
    fake_session = app._http_session
    assert app._poll_task is not None

    await app.stop()

    assert fake_session.closed is True
    assert app._poll_task is None


@pytest.mark.asyncio
async def test_signal_app_ignores_disallowed_direct_sender(tmp_path, caplog) -> None:
    config = _config(tmp_path)
    config.interactive_apps.signal.allowed_sender_ids = ["+15550000000"]
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    bridge = _FakeBridge()
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(config, store)),
        bridge=bridge,
    )
    app._client = _FakeSignalClient()

    with caplog.at_level(logging.INFO):
        await app._process_inbound_payload(_direct_payload())

    assert bridge.inbound_messages == []
    assert app._client.sent == []
    assert "newuser_target=sender-uuid" in caplog.text
    assert "sender-uuid" in caplog.text
    assert "+15551234567" in caplog.text


def test_signal_app_allows_registered_direct_user_without_allowlist_restart(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.signal.allowed_sender_ids = ["+15550000000"]
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_RegisteredUserGateway()),
        bridge=_FakeBridge(),
    )

    assert (
        app._is_allowed_sender(
            chat_id="+15551234567",
            user_id="sender-uuid",
            source_candidates={"+15551234567"},
        )
        is True
    )


@pytest.mark.asyncio
async def test_signal_app_routes_allowlisted_group_with_mention_marker(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.signal.allowed_group_ids = ["group.signal"]
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    bridge = _FakeBridge()
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(config, store)),
        bridge=bridge,
    )
    app._client = _FakeSignalClient()

    await app._process_inbound_payload(_group_payload())

    assert len(bridge.inbound_messages) == 1
    assert bridge.inbound_messages[0].chat_id == "group.signal"
    assert bridge.inbound_messages[0].text == "hello group"


@pytest.mark.asyncio
async def test_signal_app_attachment_only_message_routes_stored_attachment(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    bridge = _FakeBridge()
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(config, store)),
        bridge=bridge,
    )
    app._client = _FakeSignalClient()
    app._client.served_attachments["attachment-1"] = (b"png-bytes", "image/png")
    payload = _direct_payload(message="")
    payload["envelope"]["dataMessage"]["attachments"] = [
        {"id": "attachment-1", "filename": "image.png", "contentType": "image/png"}
    ]

    await app._process_inbound_payload(payload)

    assert len(bridge.inbound_messages) == 1
    inbound = bridge.inbound_messages[0]
    assert inbound.text == ""
    assert len(inbound.attachments) == 1
    assert inbound.attachments[0].origin == "signal"
    assert inbound.attachments[0].kind == "image"
    assert inbound.attachments[0].local_path()
    assert app._client.sent == []
