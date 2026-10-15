"""Live browser delivery reuses the authenticated durable event read."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging

from aiohttp import web

LOGGER = logging.getLogger(__name__)
HEARTBEAT_SECONDS = 2
WRITE_TIMEOUT_SECONDS = 10


async def stream_events(
    request: web.Request, read_events: Callable[[str | None], web.Response]
) -> web.StreamResponse:
    cursor = str(request.query.get("after") or "").strip() or None
    # Reject unauthenticated requests before sending streaming headers. Each
    # read runs in a worker thread: it reads the database several times, every
    # HEARTBEAT_SECONDS for every open tab (website_handlers.off_loop).
    page = await asyncio.to_thread(read_events, cursor)
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        }
    )
    await response.prepare(request)
    try:
        while request.transport is not None and not request.transport.is_closing():
            payload = json.loads(page.text or "{}")
            await asyncio.wait_for(
                response.write(("data: " + json.dumps(payload) + "\n\n").encode()),
                timeout=WRITE_TIMEOUT_SECONDS,
            )
            if page.status != 200:
                break
            cursor = payload.get("next_cursor") or cursor
            # Empty pages are heartbeats. Drain a backlog without idle delays.
            await asyncio.sleep(0 if payload.get("has_more") else HEARTBEAT_SECONDS)
            try:
                # Recheck the cookie, user, and current ownership on every read.
                page = await asyncio.to_thread(read_events, cursor)
            except (web.HTTPUnauthorized, web.HTTPForbidden):
                await asyncio.wait_for(
                    response.write(b"event: unauthorized\ndata: {}\n\n"),
                    timeout=WRITE_TIMEOUT_SECONDS,
                )
                break
    except (ConnectionError, TimeoutError):
        LOGGER.info("event=web_chat_stream_disconnected reason=connection_lost")
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.exception("event=web_chat_stream_failed")
    return response
