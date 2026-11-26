from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
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
        self.serve_calls: list[tuple[str, int]] = []

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

    async def serve_attachment(self, attachment: str, *, max_bytes: int):
        self.serve_calls.append((attachment, max_bytes))
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_name",
    [
        "id_and_absolute_filename",
        "path",
        "localPath",
        "storedFilename",
        "stored_filename",
        "bare_string",
    ],
)
async def test_signal_app_refuses_host_disk_attachment_variants(
    tmp_path, caplog, case_name: str
) -> None:
    sentinel_bytes = b"HOST_SENTINEL_SECRET_BYTES_12345"
    sentinel_file = tmp_path / "sentinel.txt"
    sentinel_file.write_bytes(sentinel_bytes)

    variants: dict[str, object] = {
        "id_and_absolute_filename": {
            "id": "att-probe",
            "filename": str(sentinel_file),
            "contentType": "text/plain",
            "size": len(sentinel_bytes),
        },
        "path": {
            "path": str(sentinel_file),
            "filename": "note.txt",
            "contentType": "text/plain",
        },
        "localPath": {
            "localPath": str(sentinel_file),
            "filename": "note.txt",
            "contentType": "text/plain",
        },
        "storedFilename": {
            "storedFilename": str(sentinel_file),
            "filename": "note.txt",
            "contentType": "text/plain",
        },
        "stored_filename": {
            "stored_filename": str(sentinel_file),
            "filename": "note.txt",
            "contentType": "text/plain",
        },
        "bare_string": str(sentinel_file),
    }

    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    bridge = _FakeBridge()
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(config, store)),
        bridge=bridge,
    )
    fake_client = _FakeSignalClient()
    fake_client.served_attachments["att-probe"] = (b"from-signal-cli", "text/plain")
    app._client = fake_client

    stored_payloads: list[bytes] = []
    original_store_bytes = app.attachments.store_bytes

    def _spy_store_bytes(**kwargs):
        stored_payloads.append(kwargs["data"])
        return original_store_bytes(**kwargs)

    app.attachments.store_bytes = _spy_store_bytes  # type: ignore[method-assign]

    payload = _direct_payload(message="")
    payload["envelope"]["dataMessage"]["attachments"] = [variants[case_name]]

    with caplog.at_level(logging.WARNING):
        await app._process_inbound_payload(payload)

    assert bridge.inbound_messages == []
    assert stored_payloads == []
    assert fake_client.serve_calls == []
    assert len(fake_client.sent) == 1
    assert "event=signal_attachment_refused" in caplog.text
    assert "sender=sender-uuid" in caplog.text
    assert sentinel_bytes.decode() not in caplog.text
    uploads_dir = tmp_path / "instance" / "state" / "uploads"
    if uploads_dir.exists():
        for uploaded in uploads_dir.rglob("*"):
            if uploaded.is_file():
                assert sentinel_bytes not in uploaded.read_bytes()


@pytest.mark.asyncio
async def test_signal_app_accepts_id_and_base64_attachments(tmp_path) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.attachments.max_file_bytes = 64
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    bridge = _FakeBridge()
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(config, store)),
        bridge=bridge,
    )
    fake_client = _FakeSignalClient()
    fake_client.served_attachments["att-id"] = (b"id-payload", "text/plain")
    app._client = fake_client

    payload = _direct_payload(message="two attachments")
    payload["envelope"]["dataMessage"]["attachments"] = [
        {"id": "att-id", "filename": "from_id.txt", "contentType": "text/plain", "size": 10},
        {
            "base64": base64.b64encode(b"inline-payload").decode("ascii"),
            "filename": "from_b64.txt",
            "contentType": "text/plain",
        },
    ]

    await app._process_inbound_payload(payload)

    assert len(bridge.inbound_messages) == 1
    assert fake_client.serve_calls == [("att-id", 64)]
    attachments = bridge.inbound_messages[0].attachments
    assert len(attachments) == 2
    assert Path(attachments[0].local_path()).read_bytes() == b"id-payload"
    assert Path(attachments[1].local_path()).read_bytes() == b"inline-payload"


@pytest.mark.asyncio
async def test_signal_app_refuses_single_slash_filename_and_invalid_base64(
    tmp_path, caplog
) -> None:
    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    bridge = _FakeBridge()
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(config, store)),
        bridge=bridge,
    )
    fake_client = _FakeSignalClient()
    fake_client.served_attachments["att-root"] = (b"never-fetched", "text/plain")
    app._client = fake_client

    slash_payload = _direct_payload(message="", timestamp=401)
    slash_payload["envelope"]["dataMessage"]["attachments"] = [
        {"id": "att-root", "filename": "/secret", "contentType": "text/plain"}
    ]
    with caplog.at_level(logging.WARNING):
        await app._process_inbound_payload(slash_payload)

    assert fake_client.serve_calls == []
    assert bridge.inbound_messages == []
    assert "reason=invalid_filename" in caplog.text

    bad_b64_payload = _direct_payload(message="", timestamp=402)
    bad_b64_payload["envelope"]["dataMessage"]["attachments"] = [
        {"base64": "!!!not-base64!!!", "filename": "bad.txt", "contentType": "text/plain"}
    ]
    with caplog.at_level(logging.WARNING):
        await app._process_inbound_payload(bad_b64_payload)

    assert bridge.inbound_messages == []
    assert "reason=invalid_base64" in caplog.text
    assert "!!!not-base64!!!" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    ["../escape", "dir/file", "dir\\file", "x" * 300],
)
async def test_signal_app_refuses_invalid_attachment_ids(
    tmp_path, caplog, bad_id: str
) -> None:
    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    bridge = _FakeBridge()
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(config, store)),
        bridge=bridge,
    )
    fake_client = _FakeSignalClient()
    app._client = fake_client

    payload = _direct_payload(message="")
    payload["envelope"]["dataMessage"]["attachments"] = [
        {"id": bad_id, "filename": "note.txt", "contentType": "text/plain"}
    ]

    with caplog.at_level(logging.WARNING):
        await app._process_inbound_payload(payload)

    assert fake_client.serve_calls == []
    assert bridge.inbound_messages == []
    assert len(fake_client.sent) == 1
    assert "event=signal_attachment_refused" in caplog.text
    assert "reason=invalid_attachment_id" in caplog.text
    assert "sender=sender-uuid" in caplog.text


@pytest.mark.asyncio
async def test_signal_app_refuses_declared_over_limit_before_fetch(
    tmp_path, caplog
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.attachments.max_file_bytes = 16
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    bridge = _FakeBridge()
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(config, store)),
        bridge=bridge,
    )
    fake_client = _FakeSignalClient()
    fake_client.served_attachments["att-big"] = (b"x" * 17, "text/plain")
    app._client = fake_client

    payload = _direct_payload(message="")
    payload["envelope"]["dataMessage"]["attachments"] = [
        {
            "id": "att-big",
            "filename": "big.txt",
            "contentType": "text/plain",
            "size": 17,
        }
    ]

    with caplog.at_level(logging.WARNING):
        await app._process_inbound_payload(payload)

    assert fake_client.serve_calls == []
    assert bridge.inbound_messages == []
    assert len(fake_client.sent) == 1
    assert "exceeds the configured upload limit" in str(fake_client.sent[0]["message"])
    assert "event=signal_attachment_refused" in caplog.text
    assert "attachment_id=att-big" in caplog.text
    assert "sender=sender-uuid" in caplog.text


@pytest.mark.asyncio
async def test_signal_app_refuses_base64_over_limit_before_decode(
    monkeypatch, tmp_path, caplog
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.attachments.max_file_bytes = 16
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    bridge = _FakeBridge()
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(config, store)),
        bridge=bridge,
    )
    fake_client = _FakeSignalClient()
    app._client = fake_client

    decode_calls = 0
    real_b64decode = base64.b64decode

    def _spy_b64decode(*args, **kwargs):
        nonlocal decode_calls
        decode_calls += 1
        return real_b64decode(*args, **kwargs)

    monkeypatch.setattr(signal_module.base64, "b64decode", _spy_b64decode)

    secret_b64 = base64.b64encode(b"A" * 17).decode("ascii")
    payload = _direct_payload(message="")
    payload["envelope"]["dataMessage"]["attachments"] = [
        {
            "base64": secret_b64,
            "filename": "big_b64.txt",
            "contentType": "text/plain",
        }
    ]

    with caplog.at_level(logging.WARNING):
        await app._process_inbound_payload(payload)

    assert decode_calls == 0
    assert bridge.inbound_messages == []
    assert len(fake_client.sent) == 1
    assert "event=signal_attachment_refused" in caplog.text
    assert "sender=sender-uuid" in caplog.text
    assert secret_b64 not in caplog.text

    # Exact at-limit base64 payload succeeds.
    at_limit_b64 = base64.b64encode(b"B" * 16).decode("ascii")
    ok_payload = _direct_payload(message="", timestamp=124)
    ok_payload["envelope"]["dataMessage"]["attachments"] = [
        {
            "base64": at_limit_b64,
            "filename": "exact_b64.txt",
            "contentType": "text/plain",
        }
    ]
    await app._process_inbound_payload(ok_payload)
    assert decode_calls == 1
    assert len(bridge.inbound_messages) == 1
    assert bridge.inbound_messages[0].attachments[0].size_bytes == 16


@pytest.mark.asyncio
async def test_signal_rest_client_enforces_content_length_and_chunked_limits(
    tmp_path, caplog
) -> None:
    chunk_size = 1024
    total_chunks = 512
    chunked_bytes_written = 0
    cl_body_writes = 0

    async def handle_attachment(request: web.Request) -> web.StreamResponse:
        nonlocal chunked_bytes_written, cl_body_writes
        att_id = request.match_info["attachment_id"]
        if att_id == "too-big-cl":
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/plain", "Content-Length": "100000"},
            )
            await resp.prepare(request)
            try:
                for _ in range(100):
                    await resp.write(b"C" * 1000)
                    cl_body_writes += 1000
                    await asyncio.sleep(0)
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            return resp
        if att_id == "too-big-chunked":
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/plain"},
            )
            resp.enable_chunked_encoding()
            await resp.prepare(request)
            try:
                for _ in range(total_chunks):
                    await resp.write(b"D" * chunk_size)
                    chunked_bytes_written += chunk_size
                    await asyncio.sleep(0)
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            return resp
        if att_id == "at-limit-cl":
            return web.Response(body=b"E" * 64, content_type="text/plain")
        if att_id == "at-limit-chunked":
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/plain"},
            )
            resp.enable_chunked_encoding()
            await resp.prepare(request)
            await resp.write(b"F" * 32)
            await resp.write(b"G" * 32)
            await resp.write_eof()
            return resp
        return web.Response(status=404, text="not found")

    web_app = web.Application()
    web_app.router.add_get("/v1/attachments/{attachment_id}", handle_attachment)
    server = TestServer(web_app)
    await server.start_server()
    try:
        base_url = str(server.make_url("/")).rstrip("/")
        async with aiohttp.ClientSession() as http_session:
            client = signal_module.SignalRestClient(
                base_url=base_url,
                session=http_session,
                request_timeout_seconds=10.0,
            )

            # Content-Length over limit is refused immediately.
            with pytest.raises(signal_module.AttachmentValidationError):
                await client.serve_attachment("too-big-cl", max_bytes=64)

            # Chunked response without Content-Length stops well before the full stream.
            with pytest.raises(signal_module.AttachmentValidationError):
                await client.serve_attachment("too-big-chunked", max_bytes=1024)
            assert chunked_bytes_written < (chunk_size * total_chunks) // 2

            # Exact at-limit attachments succeed (both Content-Length and chunked).
            data_cl, ct_cl = await client.serve_attachment("at-limit-cl", max_bytes=64)
            assert data_cl == b"E" * 64
            assert "text/plain" in ct_cl

            data_ch, ct_ch = await client.serve_attachment("at-limit-chunked", max_bytes=64)
            assert data_ch == (b"F" * 32) + (b"G" * 32)
            assert "text/plain" in ct_ch

            # End-to-end through SignalInteractiveApp logs refusal without leaking bytes.
            config = _config(tmp_path)
            config.interactive_apps.internal_website.attachments.max_file_bytes = 64
            store = GatewayStateStore(str(tmp_path / "gateway.db"))
            bridge = _FakeBridge()
            app = SignalInteractiveApp(
                config,
                application=GatewayApplicationFacade(_FakeGateway(config, store)),
                bridge=bridge,
            )
            sent_replies: list[dict[str, object]] = []

            async def _fake_send(**kwargs):
                sent_replies.append(kwargs)
                return {"timestamp": "999"}

            client.send = _fake_send  # type: ignore[method-assign]
            app._client = client

            payload = _direct_payload(message="", timestamp=200)
            payload["envelope"]["dataMessage"]["attachments"] = [
                {"id": "too-big-chunked", "filename": "stream.txt", "contentType": "text/plain"}
            ]
            with caplog.at_level(logging.WARNING):
                await app._process_inbound_payload(payload)

            assert bridge.inbound_messages == []
            assert len(sent_replies) == 1
            assert "event=signal_attachment_refused" in caplog.text
            assert "attachment_id=too-big-chunked" in caplog.text
            assert "sender=sender-uuid" in caplog.text
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_signal_app_refused_second_attachment_leaves_no_partial_uploads(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.attachments.max_file_bytes = 32
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    bridge = _FakeBridge()
    app = SignalInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(config, store)),
        bridge=bridge,
    )
    fake_client = _FakeSignalClient()
    fake_client.served_attachments["att-1"] = (b"valid-first", "text/plain")
    fake_client.served_attachments["att-empty"] = (b"", "text/plain")
    app._client = fake_client

    uploads_dir = tmp_path / "instance" / "state" / "uploads"

    # Pre-read refusal on 2nd attachment: 1st is not even fetched or stored.
    payload1 = _direct_payload(message="", timestamp=301)
    payload1["envelope"]["dataMessage"]["attachments"] = [
        {"id": "att-1", "filename": "ok.txt", "contentType": "text/plain", "size": 11},
        {"id": "att-2", "filename": "huge.txt", "contentType": "text/plain", "size": 64},
    ]
    await app._process_inbound_payload(payload1)
    assert fake_client.serve_calls == []
    assert bridge.inbound_messages == []
    assert not any(p.is_file() for p in uploads_dir.rglob("*")) if uploads_dir.exists() else True

    # Post-read policy refusal on 2nd attachment (empty file): 1st is not stored on disk.
    payload2 = _direct_payload(message="", timestamp=302)
    payload2["envelope"]["dataMessage"]["attachments"] = [
        {"id": "att-1", "filename": "ok.txt", "contentType": "text/plain", "size": 11},
        {"id": "att-empty", "filename": "empty.txt", "contentType": "text/plain"},
    ]
    await app._process_inbound_payload(payload2)
    assert bridge.inbound_messages == []
    assert not any(p.is_file() for p in uploads_dir.rglob("*")) if uploads_dir.exists() else True
