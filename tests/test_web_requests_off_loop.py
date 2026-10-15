"""What the web UI requests on its own does no database work on the gateway's process loop.

The gateway's one asyncio loop also runs Discord, Telegram and every other web
request, so a slow database call made on it freezes all of them (owner's
report, 2026-10-02: "Connecting live updates..." on a gateway whose databases
sit on NFS, where one read was measured waiting 30 seconds). This test signs
in, loads what a Web Chat page loads and reads live-update ticks, and records
every SQLite open made on a thread that is running an event loop.
"""

from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer
import pytest

from tests.loop_storage_support import record_loop_opens
from tests.test_web_chat_live_events import live_app, payload, read_frame  # noqa: F401 - fixture

ALPHA = "website:web-chat:alpha"


@pytest.mark.asyncio
async def test_page_loads_and_live_updates_read_the_database_off_the_loop(live_app, monkeypatch, tmp_path):  # noqa: F811
    from storage.sqlite_connection import connect_sqlite
    from tests.test_gateway_website_app import _FakeGateway

    app, notice = live_app

    def user_is_active(_canonical_user_id: str) -> bool:
        # Like the real check (identity_service.resolve_user_by_id), read the gateway's database.
        with connect_sqlite(str(tmp_path / "gateway.db")) as conn:
            conn.execute("SELECT 1").fetchone()
        return True

    monkeypatch.setattr(_FakeGateway, "is_web_chat_user_active", staticmethod(user_is_active))

    class Inbox:
        """The page polls the Inbox count; like the real Inbox it reads the database in a worker thread."""

        async def counts(self, _person_id: str) -> dict[str, int]:
            await asyncio.to_thread(user_is_active, _person_id)
            return {"decidable_requests": 0, "unread_notices": 0}

    app.application.access_inbox = Inbox()
    async with TestClient(TestServer(app.build_app())) as client:
        assert (await client.post("/api/web-chat/auth", json={"token": "uaweb_test_token"})).status == 200
        with record_loop_opens(monkeypatch) as loop_opens:
            await _load_the_page(client, notice)

    assert loop_opens.where == [], loop_opens.report()


async def _load_the_page(client, notice) -> None:
    """What an open Web Chat page requests on its own: on load, on timers and live updates."""

    for path in (
        "/api/web-chat/session",
        "/api/web-chat/sessions",
        f"/api/web-chat/sessions/{ALPHA}/messages",
        "/api/web-chat/settings",
        "/api/web-chat/access-inbox/counts",
        "/api/web-chat/appearance",
    ):
        response = await client.get(path)
        assert response.status == 200, (path, response.status, await response.text())
    stream = await client.get("/api/web-chat/events/stream")
    await read_frame(stream)
    # Written the way agents' replies arrive: from outside the request.
    await asyncio.to_thread(notice, "while-watching")
    assert payload(await read_frame(stream))["messages"]
    stream.close()
