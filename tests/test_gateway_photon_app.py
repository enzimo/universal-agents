from __future__ import annotations

import asyncio
import json
import logging
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import GatewayDeliveryEvent
from tests.application_test_doubles import (
    application_from as GatewayApplicationFacade,
    interactive_app_runtime_from,
)
from assistant_gateway.interactive_apps.photon import (
    PhotonInteractiveApp as _PhotonInteractiveApp,
)
from tests.staged_attachment_helpers import change_staged_copy, staged_attachment


class PhotonInteractiveApp(_PhotonInteractiveApp):
    """Compose explicit host ports around aggregate test fixtures."""

    def __init__(self, config, application, bridge) -> None:
        super().__init__(
            config.interactive_apps.photon,
            application,
            bridge,
            interactive_app_runtime_from(config, application),
        )


class _FakeInboundContent:
    def __init__(self, events: list[dict]) -> None:
        self._lines = [f"{json.dumps(event)}\n".encode() for event in events]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeInboundResponse:
    status = 200

    def __init__(self, events: list[dict]) -> None:
        self.content = _FakeInboundContent(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def text(self) -> str:
        return ""


class _FakeHttpSession:
    def __init__(self, events: list[dict]) -> None:
        self._events = events
        self.closed = False

    def get(self, *args, **kwargs):
        return _FakeInboundResponse(self._events)

    async def close(self) -> None:
        self.closed = True


class _RegisteredUserGateway:
    def resolve_canonical_user(self, *, channel_name: str, external_user_id: str, chat_scope: str):
        if channel_name == "photon" and chat_scope == "" and external_user_id == "sender-id":
            return "user_registered"
        return None


def _config(tmp_path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="photonbot",
        instance_root=str(tmp_path / "instance"),
        team_id="team_photon",
        timezone="America/Chicago",
    )
    config.interactive_apps.photon.enabled = True
    return config


def test_photon_app_logs_newuser_target_for_disallowed_sender(tmp_path, caplog) -> None:
    config = _config(tmp_path)
    config.interactive_apps.photon.allowed_sender_ids = ["imessage:+15550000000"]
    app = PhotonInteractiveApp(config, application=GatewayApplicationFacade(SimpleNamespace()), bridge=SimpleNamespace())

    with caplog.at_level(logging.INFO):
        allowed = app._is_allowed_sender(chat_id="imessage:+15551234567", user_id="sender-id")

    assert allowed is False
    assert "Ignoring Photon message from non-allowed sender" in caplog.text
    assert "chat_id=imessage:+15551234567" in caplog.text
    assert "user_id=sender-id" in caplog.text
    assert "newuser_target=sender-id" in caplog.text


def test_photon_app_allows_registered_direct_user_without_allowlist_restart(tmp_path) -> None:
    config = _config(tmp_path)
    config.interactive_apps.photon.allowed_sender_ids = ["imessage:+15550000000"]
    app = PhotonInteractiveApp(config, application=GatewayApplicationFacade(_RegisteredUserGateway()), bridge=SimpleNamespace())

    assert app._is_allowed_sender(chat_id="imessage:+15551234567", user_id="sender-id") is True


@pytest.mark.asyncio
async def test_photon_read_receipt_does_not_create_manager_task(tmp_path) -> None:
    application = SimpleNamespace(
        resolve_session=lambda **_route: SimpleNamespace(
            session_key="photon:read-receipt",
            conversation_id="conversation_read_receipt",
        )
    )
    bridge = SimpleNamespace(handle_inbound=AsyncMock(return_value=[]))
    app = PhotonInteractiveApp(
        _config(tmp_path),
        application=GatewayApplicationFacade(application),
        bridge=bridge,
    )

    await app._process_inbound_event(
        {
            "messageId": "read_receipt_1",
            "space": {"id": "any;-;+15551234567", "type": "dm"},
            "sender": {"id": "+15551234567"},
            "content": {
                "type": "read",
                "targetMessageId": "outbound_message_1",
            },
        }
    )

    bridge.handle_inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_photon_message_failure_does_not_fail_inbound_stream(tmp_path, caplog) -> None:
    app = PhotonInteractiveApp(_config(tmp_path), application=GatewayApplicationFacade(SimpleNamespace()), bridge=SimpleNamespace())
    app._http_session = _FakeHttpSession(
        [{"messageId": "broken"}, {"messageId": "healthy"}]
    )
    healthy_processed = asyncio.Event()

    async def process_event(event: dict) -> None:
        if event["messageId"] == "broken":
            raise RuntimeError("simulated outbound failure")
        healthy_processed.set()

    app._process_inbound_event = process_event

    with caplog.at_level(logging.ERROR):
        await app._consume_inbound_stream()
        await asyncio.wait_for(healthy_processed.wait(), timeout=1)
        await asyncio.sleep(0)

    assert "inbound stream remains active" in caplog.text
    assert "simulated outbound failure" in caplog.text


@pytest.mark.asyncio
async def test_photon_stop_cancels_inflight_message_tasks(tmp_path) -> None:
    app = PhotonInteractiveApp(_config(tmp_path), application=GatewayApplicationFacade(SimpleNamespace()), bridge=SimpleNamespace())
    session = _FakeHttpSession([])
    app._http_session = session
    task_started = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def wait_forever() -> None:
        task_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            task_cancelled.set()

    app._spawn_background_task(wait_forever(), name="photon_test_inflight")
    await task_started.wait()

    await app.stop()

    assert task_cancelled.is_set()
    assert not app._background_tasks
    assert session.closed is True


@pytest.mark.asyncio
async def test_photon_sends_a_private_copy_of_the_checked_attachment(tmp_path) -> None:
    """The sidecar reads a path: it gets a 0600 copy of the checked bytes, removed afterwards."""

    config = _config(tmp_path)
    app = PhotonInteractiveApp(
        config, application=GatewayApplicationFacade(SimpleNamespace()), bridge=SimpleNamespace()
    )
    calls: list[tuple[str, dict, bytes, int]] = []

    async def fake_sidecar_call(path: str, payload: dict) -> dict:
        if path == "/send-attachment":
            private = Path(payload["path"])
            calls.append((path, payload, private.read_bytes(), stat.S_IMODE(private.stat().st_mode)))
        return {}

    app._sidecar_call = fake_sidecar_call
    chart = staged_attachment(
        config, data=b"png-bytes", filename="chart.png", content_type="image/png", kind="image"
    )
    changed = staged_attachment(config, data=b"notes", filename="notes.txt", content_type="text/plain")
    change_staged_copy(changed, b"changed by agent code")

    await app.deliver_event(
        GatewayDeliveryEvent(
            text="",
            session_key="session_1",
            channel_name="photon",
            user_id="sender-id",
            chat_id="imessage:+15551234567",
            thread_id=None,
            conversation_id="conv_1",
            scheduled_job_id=None,
            correlation_id="corr_1",
            original_message_id=None,
            attachments=(chart, changed),
        )
    )

    assert len(calls) == 1
    _path, payload, data, mode = calls[0]
    private = Path(payload["path"])
    assert data == b"png-bytes"
    assert mode == 0o600
    assert payload["name"] == "chart.png"
    assert payload["mimeType"] == "image/png"
    assert config.layout.attachment_copies_dir in private.parents
    assert not private.exists()


@pytest.mark.asyncio
async def test_photon_sidecar_gets_only_its_own_values_and_basic_settings(tmp_path, monkeypatch) -> None:
    """The sidecar is a third-party Node program: no signing key, broker token or other secret."""

    import assistant_gateway.interactive_apps.photon as photon_module

    sidecar_dir = tmp_path / "sidecar"
    (sidecar_dir / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(photon_module, "PHOTON_SIDECAR_DIR", sidecar_dir)
    monkeypatch.setattr(photon_module.shutil, "which", lambda _name: "/usr/bin/node")
    for name, value in {
        "ACCESS_GATEWAY_SIGNING_KEY": "gateway-key",
        "ACCESS_GATEWAY_SIGNING_KEY_FILE": "/run/secrets/gateway-key",
        "SECURE_TOOL_BROKER_TOKEN": "broker-token",
        "WEB_CHAT_AUTH_TOKEN": "web-chat",
        "TELEGRAM_BOT_TOKEN": "telegram",
        "HTTPS_PROXY": "http://proxy.example.test:3128",
        "LC_CTYPE": "C.UTF-8",
        "PATH": "/usr/bin",
    }.items():
        monkeypatch.setenv(name, value)
    launched: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        launched["args"] = args
        launched["env"] = kwargs["env"]
        return SimpleNamespace(returncode=None, stdout=None)

    monkeypatch.setattr(photon_module.asyncio, "create_subprocess_exec", fake_exec)
    app = PhotonInteractiveApp(
        _config(tmp_path), application=GatewayApplicationFacade(SimpleNamespace()), bridge=SimpleNamespace()
    )
    monkeypatch.setattr(app, "_wait_for_sidecar", AsyncMock())
    monkeypatch.setattr(app, "_log_sidecar_output", AsyncMock())

    await app._start_sidecar(project_id="project", project_secret="project-secret")
    await asyncio.sleep(0)

    env = launched["env"]
    assert env["PHOTON_PROJECT_ID"] == "project"
    assert env["PHOTON_PROJECT_SECRET"] == "project-secret"
    assert env["HTTPS_PROXY"] == "http://proxy.example.test:3128"
    assert env["LC_CTYPE"] == "C.UTF-8" and env["PATH"] == "/usr/bin"
    assert not {
        "ACCESS_GATEWAY_SIGNING_KEY",
        "ACCESS_GATEWAY_SIGNING_KEY_FILE",
        "SECURE_TOOL_BROKER_TOKEN",
        "WEB_CHAT_AUTH_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    } & set(env)
