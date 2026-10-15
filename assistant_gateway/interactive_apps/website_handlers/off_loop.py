"""Keep Web Chat's database work off the gateway's process loop.

The gateway runs Discord, Telegram, the agents' replies and every web request
on one asyncio loop, and its repositories are synchronous. A database call made
on that loop stops all of them until it returns. On an instance whose databases
sit on NFS one read was measured waiting 30 seconds, and the page showed
"Connecting live updates..." meanwhile (owner's report, 2026-10-02). So:

- ``off_loop`` turns a handler whose work is all synchronous into an async
  handler that runs it in a worker thread. Such a handler only reads the
  request (address, query, cookies, headers), which is safe from a thread.
- ``check_signed_in_person`` runs the database half of the sign-in check, whether
  the person may still use Web Chat, in a worker thread before any ``/api/``
  handler, and leaves the answer on the request. The handler's own check
  (``_require_web_chat_session``) takes that answer once (``take_checked_person``);
  later checks in the same request, such as each tick of the live-updates
  stream, read the database again, from their worker thread.

Code these run in a worker thread must copy shared in-memory collections
before iterating them (``tuple(mapping.items())``), because the loop may change
them meanwhile.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Awaitable, Callable

from aiohttp import web

# The request item holding (person id, may use Web Chat) from check_signed_in_person.
CHECKED_PERSON_KEY = web.RequestKey("ua_web_chat_checked_person", tuple)
# Requests whose handlers check the sign-in; static files and docs do not.
SIGNED_IN_PATH_PREFIXES = ("/api/", "/workflows")


def off_loop(build: Callable[[Any, web.Request], web.StreamResponse]) -> Callable[[Any, web.Request], Awaitable[web.StreamResponse]]:
    """An async handler that runs the synchronous ``build`` in a worker thread (module docstring)."""

    @functools.wraps(build)
    async def handler(owner: Any, request: web.Request) -> web.StreamResponse:
        return await asyncio.to_thread(build, owner, request)

    return handler


def check_signed_in_person(
    signed_in_person: Callable[[web.Request], str | None], may_use_web_chat: Callable[[str], bool]
) -> Any:
    """Middleware: check in a worker thread that the signed-in person may still use Web Chat.

    ``signed_in_person`` reads the person from the request's session without
    the database (None without one); ``may_use_web_chat`` is the database check.
    """

    @web.middleware
    async def middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        if request.path.startswith(SIGNED_IN_PATH_PREFIXES):
            person_id = signed_in_person(request)
            if person_id:
                request[CHECKED_PERSON_KEY] = (person_id, await asyncio.to_thread(may_use_web_chat, person_id))
        return await handler(request)

    return middleware


def take_checked_person(request: web.Request, person_id: str) -> bool | None:
    """The middleware's answer for this person, used once; None when there is none."""

    checked = request.get(CHECKED_PERSON_KEY)
    if checked is None or checked[0] != person_id:
        return None
    del request[CHECKED_PERSON_KEY]
    return bool(checked[1])


__all__ = ["CHECKED_PERSON_KEY", "check_signed_in_person", "off_loop", "take_checked_person"]
