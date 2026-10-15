"""Real HTTP streaming, durable replay, and browser delivery of review notices."""

import asyncio
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.store import GatewayStateStore
from tests.test_gateway_transcript import _create_web_session, _service
from tests.test_gateway_website_app import (
    InternalWebsiteInteractiveApp,
    _config,
    _StatefulFakeGateway,
)


@pytest.fixture
def live_app(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    _create_web_session(store, owner=config.user.canonical_user_id)
    _create_web_session(store, session_key="other-session", owner="other-owner")
    app = InternalWebsiteInteractiveApp(
        config, gateway=_StatefulFakeGateway(store), bridge=SimpleNamespace()
    )
    transcript = _service(store)

    def notice(event_id, session="website:web-chat:alpha"):
        return transcript.append_delivery_event(
            GatewayDeliveryEvent(
                text=f"Permission review {event_id}",
                session_key=session,
                channel_name="website",
                user_id=config.user.canonical_user_id,
                chat_id="web-chat",
                thread_id=None,
                conversation_id="conversation_alpha",
                scheduled_job_id=None,
                correlation_id=event_id,
                original_message_id=None,
                event_id=event_id,
                message_type="human_interaction_request",
            )
        )

    return app, notice


async def read_frame(response):
    async def read():
        lines = []
        while True:
            line = await response.content.readline()
            if not line:
                raise AssertionError("Stream closed before the next frame")
            if line == b"\n":
                return b"".join(lines).decode()
            lines.append(line)

    return await asyncio.wait_for(read(), 6)


def payload(frame):
    return json.loads(frame.removeprefix("data: "))


@pytest.mark.asyncio
async def test_live_review_delivery_replay_and_owner_isolation(live_app):
    app, notice = live_app
    async with TestClient(TestServer(app.build_app())) as client:
        denied = await client.get("/api/web-chat/events/stream")
        assert denied.status == 401
        await client.post("/api/web-chat/auth", json={"token": "uaweb_test_token"})
        stream = await client.get("/api/web-chat/events/stream")
        assert stream.headers["Content-Type"] == "text/event-stream"
        assert stream.headers["Cache-Control"] == "no-store"
        initial = payload(await read_frame(stream))
        assert initial["messages"] == []
        notice("visible")
        notice("private", "other-session")
        delivered = payload(await read_frame(stream))
        assert [row["content"] for row in delivered["messages"]] == [
            "Permission review visible"
        ]
        assert delivered["messages"][0]["message_type"] == "human_interaction_request"
        stream.close()
        notice("while-offline")
        replay = await client.get(
            "/api/web-chat/events/stream", params={"after": delivered["next_cursor"]}
        )
        recovered = payload(await read_frame(replay))
        assert [row["content"] for row in recovered["messages"]] == [
            "Permission review while-offline"
        ]
        heartbeat = payload(await read_frame(replay))
        assert heartbeat["messages"] == []
        assert heartbeat["next_cursor"] == recovered["next_cursor"]
        replay.close()


@pytest.mark.asyncio
async def test_live_stream_reset_and_revoked_login(live_app):
    app, _ = live_app
    async with TestClient(TestServer(app.build_app())) as client:
        await client.post("/api/web-chat/auth", json={"token": "uaweb_test_token"})
        invalid = await client.get("/api/web-chat/events/stream?after=invalid")
        assert payload(await read_frame(invalid))["status"] == "cursor_reset_required"
        invalid.close()
        stream = await client.get("/api/web-chat/events/stream")
        await read_frame(stream)
        await client.post("/api/web-chat/logout")
        assert "event: unauthorized" in await read_frame(stream)
        stream.close()


@pytest.mark.asyncio
async def test_live_pages_carry_thread_list_state(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    _create_web_session(store, owner=config.user.canonical_user_id)
    _create_web_session(store, session_key="other-session", owner="other-owner")
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        await client.post("/api/web-chat/auth", json={"token": "uaweb_test_token"})
        listed = await (await client.get("/api/web-chat/sessions")).json()
        stream = await client.get("/api/web-chat/events/stream")
        initial = payload(await read_frame(stream))
        assert initial["threads_revision"] == listed["threads_revision"]
        assert initial["busy_session_keys"] == []

        # Another person's running task never shows on this person's page.
        gateway.sessions_awaiting_reply = {"website:web-chat:alpha", "other-session"}
        busy = payload(await read_frame(stream))
        assert busy["busy_session_keys"] == ["website:web-chat:alpha"]

        renamed = await client.patch(
            "/api/web-chat/sessions/website:web-chat:alpha",
            json={"session_alias": "Repair Memory"},
        )
        assert renamed.status == 200
        after_rename = payload(await read_frame(stream))
        assert after_rename["threads_revision"] != initial["threads_revision"]
        relisted = await (await client.get("/api/web-chat/sessions")).json()
        assert relisted["threads_revision"] == after_rename["threads_revision"]
        stream.close()


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("RUN_CHROMIUM_TESTS") != "1", reason="Requires Playwright Chromium"
)
async def test_chromium_live_review_delivery(live_app):
    adapter, notice = live_app
    app = adapter.build_app()
    transports = set()

    @web.middleware
    async def track_stream(request, handler):
        if request.path != "/api/web-chat/events/stream":
            return await handler(request)
        transport = request.transport
        transports.add(transport)
        try:
            return await handler(request)
        finally:
            transports.discard(transport)

    async def drop_streams(_request):
        for transport in tuple(transports):
            transport.abort()
        return web.json_response({"status": "ok"})

    async def publish(request):
        notice(request.match_info["id"])
        return web.json_response({"status": "ok"})

    app.router.add_post("/test/notice/{id}", publish)
    app.router.add_post("/test/disconnect", drop_streams)
    app.middlewares.append(track_stream)
    async with TestServer(app) as server:
        process = await asyncio.create_subprocess_exec(
            "rtk",
            "proxy",
            shutil.which("node") or "node",
            str(Path(__file__).with_name("chat_live_events_probe.cjs")),
            str(server.make_url("/")),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 90)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, output.decode()
