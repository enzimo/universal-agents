"""The deferred delivery loop must not stop the gateway's event loop.

Regression for a live stall: the loop read due deliveries from SQLite directly
on the event loop while another connection held the database lock, and the
Discord heartbeat on the same loop was blocked for more than 10 seconds.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from assistant_gateway.application.deferred_delivery import (
    GatewayDeferredDeliveryApplication,
    GatewayDeferredDeliverySettings,
)
from assistant_gateway.application.delivery_service import GatewayDeliveryService
from storage.gateway_state_repository import SqliteGatewayStateRepository

LOCK_HELD_SECONDS = 0.8
TICK_SECONDS = 0.02
# Far below the lock wait: a loop blocked by the storage call fails this.
MAX_TICK_GAP_SECONDS = 0.4


class _Clock:
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class _Output:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


class _Manager:
    manager_id = None


def _hold_database_lock(db_path: Path) -> sqlite3.Connection:
    """Take the exclusive file lock every new connection must wait for.

    This stands in for SQLite's close-time checkpoint (or any other exclusive
    holder) in another process.
    """

    from storage.sqlite_connection import close_held_open_databases

    # Exclusive locking mode cannot start while any connection to the file is
    # open, so the idle connection this process holds is closed first: the
    # lock stands for another process that has none of ours.
    close_held_open_databases()
    holder = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("DELETE FROM gateway_deliveries WHERE 0")
    return holder


def _release(holder: sqlite3.Connection) -> None:
    holder.execute("COMMIT")
    holder.close()


@pytest.mark.asyncio
async def test_release_due_keeps_the_event_loop_running_while_the_database_is_locked(tmp_path: Path):
    db_path = tmp_path / "gateway_state.db"
    repository = SqliteGatewayStateRepository(str(db_path), logger=logging.getLogger("test"))
    repository.create_session(session_key="session_alpha", conversation_id="conv_alpha", manager_agent_id="manager_1")
    repository.add_deferred_delivery(
        session_key="session_alpha",
        origin_kind="cron",
        payload={"kind": "cron"},
        rendered_text="Deferred result",
        available_after="2000-01-01T00:00:00+00:00",
    )
    output = _Output()
    application = GatewayDeferredDeliveryApplication(
        settings=GatewayDeferredDeliverySettings(instance_name="test", response_route="route"),
        delivery=GatewayDeliveryService(repository=repository, unit_of_work_factory=repository),
        sessions=repository,
        manager=_Manager(),
        event_output=output,
        clock=_Clock(),
    )
    holder = _hold_database_lock(db_path)
    release_timer = threading.Timer(LOCK_HELD_SECONDS, _release, args=(holder,))

    ticks: list[float] = []

    async def heartbeat(done: asyncio.Event) -> None:
        while not done.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(TICK_SECONDS)

    done = asyncio.Event()
    beating = asyncio.create_task(heartbeat(done))
    started = time.monotonic()
    release_timer.start()
    try:
        released = await application.release_due()
    finally:
        release_timer.join()
        done.set()
        await beating
    elapsed = time.monotonic() - started

    assert elapsed >= LOCK_HELD_SECONDS * 0.75, "the call must actually have waited for the lock"
    gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:])]
    assert max(gaps) < MAX_TICK_GAP_SECONDS
    assert len(released) == 1 and "Deferred result" in released[0]
    assert output.events[0].conversation_id == "conv_alpha"
    assert repository.get_due_deliveries() == []
