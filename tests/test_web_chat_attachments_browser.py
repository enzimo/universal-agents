"""Real-browser (Chromium) check for Web Chat attachment sandboxing and media playback.

Covers the attachment route on a probe page, each attachment opened as its own
page, and the real /webchat page rendering a transcript reply with attachments.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import shutil
import struct
import wave
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from assistant_gateway.application.gateway_application import GatewayApplicationResponse
from assistant_gateway.application.request_context import GatewayRequestRoute
from assistant_gateway.application.transcript_service import GatewayTranscriptService
from assistant_gateway.interactive_apps.website_support import _WEB_CHAT_CSP
from assistant_gateway.runtime_ports import UuidGatewayIdGenerator
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.delivery_v1 import UserNotificationAttachment
from tests.browser_probe import requires_chromium
from tests.staged_attachment_helpers import staged_attachment
from tests.test_gateway_website_app import (
    InternalWebsiteInteractiveApp,
    _StatefulFakeGateway,
    _config,
)

PROBE = Path(__file__).with_name("web_chat_attachments_probe.cjs")

# Valid 16x16 0.1s VP8 WebM clip (559 bytes) so the browser test needs no ffmpeg at runtime.
_TINY_WEBM_BYTES = base64.b64decode(
    "GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAH/EU2bdLpNu4tTq4QV"
    "SalmU6yBoU27i1OrhBZUrmtTrIHWTbuMU6uEElTDZ1OsggEjTbuMU6uEHFO7a1OsggHp7AEAAAAAAABZ"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsCrXsYMPQkBNgIxMYXZmNjEuNy4xMDNXQYxM"
    "YXZmNjEuNy4xMDNEiYhAWQAAAAAAABZUrmvIrgEAAAAAAAA/14EBc8WI3VJAnoNONsGcgQAitZyDdW5k"
    "iIEAhoVWX1ZQOIOBASPjg4QF9eEA4JCwgRC6gRCagQJVsIRVuYEBElTDZ/tzc59jwIBnyJlFo4dFTkNP"
    "REVSRIeMTGF2ZjYxLjcuMTAzc3PWY8CLY8WI3VJAnoNONsFnyKFFo4dFTkNPREVSRIeUTGF2YzYxLjE5"
    "LjEwMSBsaWJ2cHhnyKFFo4hEVVJBVElPTkSHkzAwOjAwOjAwLjEwMDAwMDAwMAAfQ7Z1weeBAKO8gQAA"
    "gLACAJ0BKhAAEAAARwiFhYiFhIgCAgJ1qgP4Agz9KAD+/00S//xYV/FhX8WFf/FhX/z8zu3F/OYAHFO7"
    "a5G7j7OBALeK94EB8YIBo/CBAw=="
)

_PROBE_PAGE_HTML = (
    '<!doctype html><meta charset="utf-8"><title>attachment-probe</title>'
    '<div id="rail"></div>'
)


def _tiny_png_bytes(width: int = 8, height: int = 8) -> bytes:
    """Build a valid solid-colour RGB PNG using only stdlib zlib and struct."""

    def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)

    raw_rows = b"".join(b"\x00" + b"\x20\x80\xc0" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw_rows))
        + _chunk(b"IEND", b"")
    )


def _tiny_wav_bytes() -> bytes:
    """Build a valid 0.1s mono PCM WAV clip using stdlib wave."""

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"\x00\x00" * 800)
    return buf.getvalue()


@requires_chromium
@pytest.mark.asyncio
async def test_web_chat_attachments_in_chromium(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "uaweb_test_token"
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", token)
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = "WEB_CHAT_AUTH_TOKEN"
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    session_key = "website:web-chat:browser-probe"
    owner = config.user.canonical_user_id
    session = store.create_session(
        session_key=session_key,
        conversation_id="conv_browser_probe",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=owner,
        chat_id="web-chat",
        owner_canonical_user_id=owner,
    )
    gateway = _StatefulFakeGateway(store)
    interactive = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    records = [
        ("att_png", "pic.png", _tiny_png_bytes(), "image/png", "image"),
        ("att_wav", "tone.wav", _tiny_wav_bytes(), "audio/wav", "audio"),
        ("att_webm", "clip.webm", _TINY_WEBM_BYTES, "video/webm", "video"),
        (
            "att_svg",
            "vector.svg",
            b"<svg xmlns='http://www.w3.org/2000/svg' width='20' height='20'>"
            b"<script>document.title='SVG-SCRIPT-RAN'</script>"
            b"<rect width='20' height='20' fill='red'/></svg>",
            "image/svg+xml",
            "image",
        ),
        (
            "att_html",
            "fake.png",
            b"<!doctype html><title>start</title><script>document.title='HTML-SCRIPT-RAN'</script>",
            "text/html",
            "image",
        ),
        ("att_js", "script.js", b"window.__attachment_script_ran = true;\n", "text/javascript", "document"),
        ("att_css", "style.css", b"body { --attachment-style-applied: 1; }\n", "text/css", "document"),
    ]
    transcript_attachments: list[UserNotificationAttachment] = []
    for att_id, filename, data, content_type, kind in records:
        staged = staged_attachment(
            config,
            data=data,
            filename=filename,
            content_type="application/octet-stream",
        )
        sha256 = staged.sha256 or hashlib.sha256(data).hexdigest()
        store.upsert_attachment(
            attachment_id=att_id,
            session_key=session_key,
            conversation_id="conv_browser_probe",
            correlation_id="corr_browser_probe",
            filename=filename,
            content_type=content_type,
            kind=kind,
            size_bytes=len(data),
            sha256=sha256,
            local_path=staged.path,
            owner_canonical_user_id=owner,
        )
        if att_id not in {"att_js", "att_css"}:
            transcript_attachments.append(
                UserNotificationAttachment(
                    path=staged.path,
                    filename=filename,
                    content_type=content_type,
                    kind=kind,
                    size_bytes=len(data),
                    sha256=sha256,
                )
            )

    transcript = GatewayTranscriptService(
        repository=store.transcript_repository,
        unit_of_work_factory=store,
        session_repository=store.session_repository,
        id_generator=UuidGatewayIdGenerator(),
    )
    route = GatewayRequestRoute(
        channel_name="website",
        user_id=owner,
        chat_id="web-chat",
        canonical_user_id=owner,
        original_message_id="req_browser_probe",
    )
    accepted = transcript.accept_user_message(
        session=session,
        route=route,
        conversation_id="conv_browser_probe",
        text="Show the attachments",
        attachments=(),
    )
    assert accepted is not None
    projected = transcript.append_gateway_response(
        session=session,
        route=route,
        response=GatewayApplicationResponse(
            text="Here are the files.",
            conversation_id="conv_browser_probe",
            correlation_id="corr_browser_reply",
            message_type="user_notification",
            attachments=tuple(transcript_attachments),
        ),
        reply_to=accepted,
    )
    assert projected is not None

    async def probe_page(_request: web.Request) -> web.Response:
        return web.Response(
            text=_PROBE_PAGE_HTML,
            content_type="text/html",
            headers={
                "Content-Security-Policy": _WEB_CHAT_CSP,
                "Cache-Control": "no-store",
            },
        )

    app = interactive.build_app()
    app.router.add_get("/probe/page", probe_page)

    async with TestServer(app) as server:
        origin = str(server.make_url("/")).rstrip("/")
        process = await asyncio.create_subprocess_exec(
            "rtk",
            "proxy",
            shutil.which("node") or "node",
            str(PROBE),
            origin,
            token,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
    assert process.returncode == 0, stdout.decode() + stderr.decode()
    report = json.loads(stdout.decode().strip().splitlines()[-1])
    assert report["on_page"]["img_png"]["naturalWidth"] == 8
    assert report["on_page"]["audio_wav"]["state"] == "loaded"
    assert report["on_page"]["video_webm"]["state"] == "loaded"
    assert report["on_page"]["script_ran"] is False
    assert report["own_page"]["png"]["naturalWidth"] == 8
    assert report["own_page"]["png"]["page_origin"] == "null"
    assert report["own_page"]["att_wav"]["downloaded_as"] == "tone.wav"
    assert report["own_page"]["att_webm"]["downloaded_as"] == "clip.webm"
    assert report["own_page"]["att_svg"]["downloaded_as"] == "vector.svg"
    assert report["own_page"]["att_html"]["downloaded_as"] == "fake.png"
    assert report["web_chat"]["imageAlts"] == ["pic.png"]
    assert report["web_chat"]["imageNaturalWidths"] == [8]
    assert report["web_chat"]["audioDurations"][0] > 0
    assert report["web_chat"]["videoDurations"][0] > 0
    assert report["web_chat"]["chipLabels"] == ["vector.svg", "fake.png"]
    assert report["web_chat"]["thumb_tab"] == {"page_origin": "null", "naturalWidth": 8}
    assert report["web_chat"]["chip_downloads"] == {
        "vector.svg": "vector.svg",
        "fake.png": "fake.png",
    }
    assert report["web_chat"]["page_errors"] == []
