"""Pin, snooze and dismiss through Web Chat's thread route, on the real gateway.

The person places a thread with ``PATCH /api/web-chat/sessions/{key}`` and
``thread_action`` (the same route and checks as a rename). The state is saved
on the thread, so a second browser session of the same person and a reload
show it; a snoozed thread wakes at its time or when the team posts, and a
dismissed one comes back with the next message (A4-A7).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from tests.test_gateway_parallel_threads import WEB_CHAT_TOKEN, _AnsweringTransport, _send
from tests.test_gateway_task_steering import _config as steering_config
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp

OTHER_PERSON = "user_other"
OTHER_THREAD = "website:web-chat:other"


class _Clock:
    """The gateway's clock, moved by the test to reach a snooze's wake time."""

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def now_utc(self) -> datetime:
        return self.now


def _gateway(tmp_path: Path, monkeypatch, clock: _Clock) -> UserGateway:
    config = steering_config(tmp_path)
    config.network.public_origin = "http://127.0.0.1"
    config.interactive_apps.internal_website.enabled = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = "WEB_CHAT_AUTH_TOKEN"
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", WEB_CHAT_TOKEN)
    transport = _AnsweringTransport()
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
        manager_transport=transport,
        clock=clock,
    )
    transport.gateway = gateway
    return gateway


class _Browser:
    """One signed-in browser session of the person (its own session cookie)."""

    def __init__(self, client: TestClient, cookie: str) -> None:
        self.client = client
        self.headers = {"Cookie": f"ua_web_chat_session={cookie}"}

    @classmethod
    async def sign_in(cls, client: TestClient) -> _Browser:
        response = await client.post("/api/web-chat/auth", json={"token": WEB_CHAT_TOKEN})
        assert response.status == 200, await response.text()
        return cls(client, response.cookies["ua_web_chat_session"].value)

    async def new_thread(self) -> dict:
        response = await self.client.post("/api/web-chat/sessions", headers=self.headers)
        assert response.status == 201
        return (await response.json())["session"]

    async def listed(self, session_key: str) -> dict:
        sessions = await self.sessions()
        return next(item for item in sessions if item["session_key"] == session_key)

    async def sessions(self) -> list[dict]:
        response = await self.client.get("/api/web-chat/sessions", headers=self.headers)
        assert response.status == 200
        return (await response.json())["sessions"]

    async def revision(self) -> str:
        page = await (await self.client.get("/api/web-chat/events", headers=self.headers)).json()
        return page["threads_revision"]

    async def place(self, session_key: str, action: str, **fields):
        return await self.client.patch(
            f"/api/web-chat/sessions/{session_key}",
            headers=self.headers,
            json={"thread_action": action, **fields},
        )


async def _thread_with_reply(client: TestClient, browser: _Browser) -> dict:
    """A thread with the person's message and the team's answer (sequences 1 and 2).

    ``_send`` uses the client's cookie jar, which holds the latest sign-in of
    the same person.
    """

    thread = await browser.new_thread()
    response = await _send(client, thread, "Check the backups", request_id="first")
    assert response.status == 200, await response.text()
    return thread


@pytest.mark.asyncio
async def test_placement_is_saved_on_the_thread_and_every_browser_session_shows_it(tmp_path: Path, monkeypatch) -> None:
    """A4: pin, snooze and dismiss survive a reload and show in a second browser session."""

    clock = _Clock()
    gateway = _gateway(tmp_path, monkeypatch, clock)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        laptop = await _Browser.sign_in(client)
        phone = await _Browser.sign_in(client)
        thread = await _thread_with_reply(client, laptop)
        key = thread["session_key"]
        row = await laptop.listed(key)
        assert row["last_message"] == {"role": "assistant", "text": "Done.", "created_at": row["last_message"]["created_at"]}
        assert (row["last_sequence"], row["pinned"], row["list_state"], row["needs_you"]) == (2, False, None, None)
        assert row["last_activity_at"]

        before = await phone.revision()
        assert (await laptop.place(key, "pin")).status == 200
        assert (await phone.listed(key))["pinned"] is True
        assert await phone.revision() != before, "the phone's open page reloads the list"

        wake = (clock.now + timedelta(hours=1)).isoformat()
        response = await laptop.place(key, "snooze", snooze_until=wake, seen_sequence=row["last_sequence"])
        assert response.status == 200, await response.text()
        snoozed = await phone.listed(key)
        assert (snoozed["list_state"], snoozed["pinned"]) == ("snoozed", True)
        assert datetime.fromisoformat(snoozed["snoozed_until"]) == datetime.fromisoformat(wake)

        assert (await laptop.place(key, "dismiss", seen_sequence=row["last_sequence"])).status == 200
        assert (await phone.listed(key))["list_state"] == "dismissed"
        assert (await laptop.place(key, "restore")).status == 200
        assert (await laptop.place(key, "unpin")).status == 200
        restored = await phone.listed(key)
        assert (restored["pinned"], restored["list_state"]) == (False, None)


@pytest.mark.asyncio
async def test_a_snooze_wakes_at_its_time_and_opening_the_thread_clears_the_mark(tmp_path: Path, monkeypatch) -> None:
    """A5: the next live page after the wake time carries a new revision and the thread reads woke."""

    clock = _Clock()
    gateway = _gateway(tmp_path, monkeypatch, clock)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        browser = await _Browser.sign_in(client)
        thread = await _thread_with_reply(client, browser)
        key = thread["session_key"]
        wake = (clock.now + timedelta(hours=1)).isoformat()
        await browser.place(key, "snooze", snooze_until=wake, seen_sequence=2)
        snoozed_revision = await browser.revision()

        clock.now += timedelta(minutes=59)
        assert await browser.revision() == snoozed_revision
        clock.now += timedelta(minutes=2)
        assert await browser.revision() != snoozed_revision, "open pages see the wake without a reload"
        woke = await browser.listed(key)
        assert (woke["list_state"], woke["snoozed_until"]) == ("woke", None)

        assert (await browser.place(key, "restore")).status == 200
        assert (await browser.listed(key))["list_state"] is None


@pytest.mark.asyncio
async def test_a_team_post_wakes_a_snooze_and_brings_back_a_dismissed_thread(tmp_path: Path, monkeypatch) -> None:
    """A5 and A6: a reply posted after the person set the thread aside brings it back."""

    clock = _Clock()
    gateway = _gateway(tmp_path, monkeypatch, clock)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        browser = await _Browser.sign_in(client)
        snoozed = await _thread_with_reply(client, browser)
        dismissed = await browser.new_thread()
        assert (await _send(client, dismissed, "Draft the email", request_id="second")).status == 200

        await browser.place(snoozed["session_key"], "snooze", snooze_until=(clock.now + timedelta(days=7)).isoformat(), seen_sequence=2)
        await browser.place(dismissed["session_key"], "dismiss", seen_sequence=2)
        assert (await browser.listed(snoozed["session_key"]))["list_state"] == "snoozed"
        assert (await browser.listed(dismissed["session_key"]))["list_state"] == "dismissed"
        before = await browser.revision()

        # The person writes from another device; the team (the fake team lead) answers.
        assert (await _send(client, snoozed, "Any news?", request_id="third")).status == 200
        assert (await _send(client, dismissed, "One more thing", request_id="fourth")).status == 200
        assert (await browser.listed(snoozed["session_key"]))["list_state"] == "woke"
        returned = await browser.listed(dismissed["session_key"])
        assert returned["list_state"] is None and returned["last_message"]["text"] == "Done."
        assert await browser.revision() != before


@pytest.mark.asyncio
async def test_one_person_cannot_place_another_persons_thread(tmp_path: Path, monkeypatch) -> None:
    """A7: someone else's thread reads as not found, whatever the payload holds."""

    clock = _Clock()
    gateway = _gateway(tmp_path, monkeypatch, clock)
    gateway.session_repository.create_session(
        session_key=OTHER_THREAD,
        conversation_id="conv_other",
        manager_agent_id="manager_agent_1",
        channel_name="website",
        user_id=OTHER_PERSON,
        chat_id="web-chat",
        owner_canonical_user_id=OTHER_PERSON,
    )
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        browser = await _Browser.sign_in(client)
        wake = (clock.now + timedelta(hours=1)).isoformat()
        for action, fields in (
            ("pin", {}),
            ("snooze", {"snooze_until": wake, "seen_sequence": 0}),
            ("snooze", {"snooze_until": "not a time"}),
            ("dismiss", {"seen_sequence": 0}),
            ("restore", {}),
        ):
            response = await browser.place(OTHER_THREAD, action, **fields)
            assert response.status == 404, (action, await response.text())
        client.session.cookie_jar.clear()
        unsigned = await client.patch(f"/api/web-chat/sessions/{OTHER_THREAD}", json={"thread_action": "pin"})
        assert unsigned.status == 401
    (record,) = [
        thread
        for thread in gateway.session_repository.list_web_chat_threads(
            owner_canonical_user_id=OTHER_PERSON, channel_name="website", chat_id="web-chat", limit=10
        )
    ]
    assert (record.pinned, record.hidden_as, record.hidden_until) == (False, None, None)


@pytest.mark.asyncio
async def test_bad_placement_requests_are_refused_with_a_reason(tmp_path: Path, monkeypatch) -> None:
    clock = _Clock()
    gateway = _gateway(tmp_path, monkeypatch, clock)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        browser = await _Browser.sign_in(client)
        key = (await browser.new_thread())["session_key"]
        past = (clock.now - timedelta(minutes=1)).isoformat()
        cases = (
            ({"thread_action": "archive"}, "thread_action must be"),
            ({"thread_action": "snooze", "snooze_until": past, "seen_sequence": 0}, "in the future"),
            ({"thread_action": "snooze", "seen_sequence": 0}, "Choose a date and time"),
            ({"thread_action": "dismiss"}, "Reload the chat"),
            ({"thread_action": "dismiss", "seen_sequence": 2**64}, "Reload the chat"),
            ({"thread_action": "pin", "session_alias": "Both"}, "one thing about a thread at a time"),
            ({"thread_action": "pin", "parallel_thread": True}, "one thing about a thread at a time"),
        )
        for body, reason in cases:
            response = await client.patch(f"/api/web-chat/sessions/{key}", headers=browser.headers, json=body)
            assert response.status == 400, body
            assert reason in await response.text(), body
        row = await browser.listed(key)
        assert (row["pinned"], row["list_state"], row["session_alias"]) == (False, None, None)


@pytest.mark.asyncio
async def test_the_history_route_counts_turns_only_when_asked(tmp_path: Path, monkeypatch) -> None:
    clock = _Clock()
    gateway = _gateway(tmp_path, monkeypatch, clock)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        browser = await _Browser.sign_in(client)
        thread = await _thread_with_reply(client, browser)
        url = f"/api/web-chat/sessions/{thread['session_key']}/messages"
        counted = await (await client.get(url + "?limit=12&turn_count=1", headers=browser.headers)).json()
        assert counted["turn_count"] == 2 and len(counted["messages"]) == 2
        plain = await (await client.get(url + "?limit=12", headers=browser.headers)).json()
        assert "turn_count" not in plain
        missing = await client.get(f"/api/web-chat/sessions/{OTHER_THREAD}/messages?turn_count=1", headers=browser.headers)
        assert missing.status == 404
