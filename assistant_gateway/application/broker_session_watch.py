"""Wait for one secure broker page to finish: the one watcher for every broker page.

The broker's one-time pages (credential collection forms, account link forms
and sign-in pages) finish in the person's browser. The gateway learns how a
page ended only by reading the page's status from the broker until it reports
a final state or the page's time runs out. Two callers use this module:

- ``credential_workflow``: a form an agent task asked for; the result goes
  back to the waiting task;
- ``link_results``: a page a person opened with ``/link``; the result is
  posted as a plain message in the chat where they ran it.

``watch_broker_session`` owns the waiting (how often to ask, when to stop, and
what an unanswered or vanished page means); ``BrokerSessionWatches`` owns the
background tasks so the gateway can stop every watch when it shuts down.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from assistant_gateway.application.ports import (
    GatewayClock,
    GatewaySecureBrokerRefusal,
)

# The page reported a final state (the payload says which).
WATCH_FINISHED: Final = "finished"
# The page was still open when its time ran out.
WATCH_EXPIRED: Final = "expired"
# The gateway could not read the page's status at the end, or the broker no
# longer knows the page (it restarted or dropped it), so nobody can tell how it
# ended.
WATCH_UNCONFIRMED: Final = "unconfirmed"
WatchEnding = Literal["finished", "expired", "unconfirmed"]

# Keep watching a little past a page's own expiry: the broker marks an expired
# account link form unavailable itself, and a submission claimed just before
# expiry may still finish.
WATCH_GRACE = timedelta(seconds=60)

# The broker answers 404 for a page it does not know and 410 for one it
# removed; asking again cannot bring it back.
_PAGE_GONE_STATUSES = frozenset({404, 410})


@dataclass(frozen=True)
class BrokerSessionOutcome:
    """How one watch ended; ``payload`` is the last status the broker sent."""

    ending: WatchEnding
    payload: dict[str, Any] = field(default_factory=dict)


def watch_deadline(expires_at_utc: str) -> datetime:
    """When the gateway stops watching a page: its expiry plus ``WATCH_GRACE``."""

    return datetime.fromisoformat(expires_at_utc.replace("Z", "+00:00")) + WATCH_GRACE


def _page_gone(error: Exception) -> bool:
    return isinstance(error, GatewaySecureBrokerRefusal) and error.status in _PAGE_GONE_STATUSES


async def watch_broker_session(
    *,
    read_status: Callable[[], Awaitable[dict[str, Any]]],
    is_final: Callable[[dict[str, Any]], bool],
    deadline: datetime,
    clock: GatewayClock,
    poll_interval_seconds: float,
    on_read_failed: Callable[[Exception], None],
) -> BrokerSessionOutcome:
    """Read the page's status until it is final, gone, or ``deadline`` passes.

    A failed read is reported to ``on_read_failed`` (the caller logs it with
    its own identifiers) and retried at the next interval; only the last read
    decides between ``expired`` and ``unconfirmed`` at the deadline.
    """

    last_payload: dict[str, Any] = {}
    last_read_failed = False
    while clock.now_utc() < deadline:
        await asyncio.sleep(poll_interval_seconds)
        try:
            payload = await read_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            on_read_failed(exc)
            if _page_gone(exc):
                return BrokerSessionOutcome(WATCH_UNCONFIRMED, last_payload)
            last_read_failed = True
            continue
        last_read_failed = False
        last_payload = payload
        if is_final(payload):
            return BrokerSessionOutcome(WATCH_FINISHED, payload)
    return BrokerSessionOutcome(
        WATCH_UNCONFIRMED if last_read_failed else WATCH_EXPIRED, last_payload
    )


class BrokerSessionWatches:
    """The gateway's running watches, stopped together when it shuts down."""

    def __init__(self, *, owner: str, logger: logging.Logger | None = None) -> None:
        # ``owner`` names the caller in the failure log (credential_workflow, link_results).
        self._owner = owner
        self._logger = logger or logging.getLogger(__name__)
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def count(self) -> int:
        return len(self._tasks)

    def start(self, coroutine: Coroutine[Any, Any, None], *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    async def close(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.error(
                "A broker page watch failed. event=gateway_broker_watch_failed "
                "owner=%s task_name=%s error=%s",
                self._owner,
                task.get_name(),
                error,
                exc_info=error,
            )


__all__ = [
    "WATCH_EXPIRED",
    "WATCH_FINISHED",
    "WATCH_GRACE",
    "WATCH_UNCONFIRMED",
    "BrokerSessionOutcome",
    "BrokerSessionWatches",
    "watch_broker_session",
    "watch_deadline",
]
