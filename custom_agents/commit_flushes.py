"""Agent processes flush their records' SQLite commits before they send (decision 82).

``storage.sqlite_connection`` ("Flushing before sending") explains why: an
agent's task runtime, conversation, agent status and storage usage records
are written many times per task, and only what leaves the process needs them
on the disk. Both halves are switched on together here, so a process can
never skip the per-commit flush without also flushing before it sends:
first the transport's barrier is installed, then the stores defer.
"""

from __future__ import annotations

import asyncio
import logging

from custom_transport.send_barrier import install_before_send
from storage import sqlite_connection


async def _flush_before_send() -> None:
    if sqlite_connection.has_unflushed_commits():
        # Off the event loop: a flush on a slow disk takes hundreds of milliseconds.
        await asyncio.to_thread(sqlite_connection.flush_unflushed_commits)


def flush_commits_before_sending() -> None:
    """Switch this agent process to flushing before it sends; call once, before any agent starts."""

    install_before_send(_flush_before_send)
    sqlite_connection.defer_flushes_until_sent()


def flush_commits_at_exit(logger: logging.Logger) -> None:
    """Flush what the agent recorded after its last send, so a clean exit loses nothing."""

    if not sqlite_connection.has_unflushed_commits():
        return
    try:
        sqlite_connection.flush_unflushed_commits(logger=logger)
    except Exception:
        # Already logged with the database (event=sqlite_flush_failed); the
        # process is ending and these records were never sent anywhere.
        logger.warning("Exiting with SQLite commits that are not flushed. event=sqlite_flush_at_exit_failed")
