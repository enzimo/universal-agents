import asyncio
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from custom_agents.activity_client import ActivityCollectorClient, ActivityCollectorClientSettings
from custom_messages.contracts.activity_v1 import TaskActivityEvent
from custom_messages.enums import TaskActivityStage, TaskActivityType


def _make_event() -> TaskActivityEvent:
    return TaskActivityEvent(
        activity_type=TaskActivityType.TASK_STARTED,
        stage=TaskActivityStage.QUEUED,
        summary="Queued task.",
    )


@pytest.mark.asyncio
async def test_activity_client_posts_batched_events(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[dict] = []
    received_auth_headers: list[str | None] = []
    monkeypatch.setenv("ACTIVITY_COLLECTOR_INGEST_TOKEN", "ingest-token")

    async def handle_batch(request: web.Request) -> web.Response:
        received_auth_headers.append(request.headers.get("Authorization"))
        payload = await request.json()
        received.extend(payload.get("events") or [])
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/api/activity/batch", handle_batch)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        activity_client = ActivityCollectorClient(
            ActivityCollectorClientSettings(
                base_url=str(client.make_url("/")).rstrip("/"),
                flush_interval_seconds=0.05,
                batch_max_events=2,
                request_timeout_seconds=0.2,
                connect_timeout_seconds=0.2,
            ),
            logger=logging.getLogger("test.activity.client"),
        )
        await activity_client.start()
        activity_client.emit(_make_event())
        activity_client.emit(_make_event())
        await asyncio.sleep(0.15)
        await activity_client.close()

        assert len(received) == 2
        assert received_auth_headers == ["Bearer ingest-token"]
        assert all(item["message_class"] == "TaskActivityEvent" for item in received)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_activity_client_drops_when_collector_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIVITY_COLLECTOR_INGEST_TOKEN", "ingest-token")
    activity_client = ActivityCollectorClient(
        ActivityCollectorClientSettings(
            base_url="http://127.0.0.1:9",
            flush_interval_seconds=0.05,
            batch_max_events=1,
            request_timeout_seconds=0.05,
            connect_timeout_seconds=0.05,
            drop_when_unavailable=True,
        ),
        logger=logging.getLogger("test.activity.client.unavailable"),
    )
    await activity_client.start()
    activity_client.emit(_make_event())
    await asyncio.sleep(0.15)
    assert activity_client._dropped_events >= 1
    await activity_client.close()


@pytest.mark.asyncio
async def test_activity_client_requeues_when_configured_not_to_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIVITY_COLLECTOR_INGEST_TOKEN", "ingest-token")
    activity_client = ActivityCollectorClient(
        ActivityCollectorClientSettings(
            base_url="http://127.0.0.1:9",
            flush_interval_seconds=0.05,
            batch_max_events=1,
            request_timeout_seconds=0.05,
            connect_timeout_seconds=0.05,
            drop_when_unavailable=False,
            max_buffered_events=4,
        ),
        logger=logging.getLogger("test.activity.client.requeue"),
    )
    await activity_client.start()
    activity_client.emit(_make_event())
    await asyncio.sleep(0.12)
    assert len(activity_client._buffer) == 1
    assert activity_client._dropped_events == 0
    await activity_client.close()


@pytest.mark.asyncio
async def test_activity_client_disables_submission_when_ingest_token_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("ACTIVITY_COLLECTOR_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("ACTIVITY_COLLECTOR_INGEST_TOKEN_FILE", raising=False)
    # A newly started WSL host can have a monotonic clock below the warning
    # interval. The first warning must still be emitted.
    monkeypatch.setattr("custom_agents.activity_client.monotonic", lambda: 0.0)
    activity_client = ActivityCollectorClient(
        ActivityCollectorClientSettings(
            base_url="http://127.0.0.1:9",
            flush_interval_seconds=0.05,
            batch_max_events=1,
            request_timeout_seconds=0.05,
            connect_timeout_seconds=0.05,
        ),
        logger=logging.getLogger("test.activity.client.missing_token"),
    )
    with caplog.at_level(logging.WARNING, logger="test.activity.client.missing_token"):
        await activity_client.start()
    activity_client.emit(_make_event())
    await asyncio.sleep(0.05)

    assert activity_client._session is None
    assert activity_client._loop is None
    assert "configured ingest token env var" in caplog.text
