"""Agent processes flush their records' SQLite commits before they send (decision 82).

``storage.sqlite_connection`` ("Flushing before sending") explains why: an
agent's task runtime, conversation, agent status and storage usage records
are written many times per task, and only what leaves the process needs them
on the disk. Both halves are switched on together here, so a process can
never skip the per-commit flush without also flushing before it sends:
first the transport's barrier is installed, then the stores defer.

Flushes run on a thread of their own: Strands runs synchronous tools on
asyncio's shared pool, and a tool that sends a message would otherwise wait
for a pool thread that tools hold.

A flush that fails stops every later send and acknowledgement of the process
for good. On Linux a failed ``fsync`` marks the pages clean, so a retry can
report success without writing them (PostgreSQL stops on the same error for
this reason). The agent keeps running but sends nothing; restarting it
recovers from what is on the disk, and NATS delivers again what it never
acknowledged.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from custom_transport.send_barrier import before_send, install_before_send
from storage import sqlite_connection

_LOGGER = logging.getLogger(__name__)
# One thread, never asyncio's shared pool (module docstring).
_FLUSH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite-flush")
# The error of the flush that failed; from then on nothing is sent.
_FAILED: Optional[BaseException] = None


class CommitFlushFailed(RuntimeError):
    """An earlier flush failed, so this process sends and acknowledges nothing more."""


async def _flush_before_send() -> None:
    if _FAILED is not None:
        raise CommitFlushFailed(
            "This agent process could not flush its records to disk earlier and sends nothing more; restart it."
        ) from _FAILED
    if not sqlite_connection.has_unflushed_commits():
        return
    try:
        await asyncio.get_running_loop().run_in_executor(
            _FLUSH_EXECUTOR, sqlite_connection.flush_unflushed_commits
        )
    except Exception as error:
        _stop_sending(error)
        raise


def _stop_sending(error: BaseException) -> None:
    global _FAILED
    if _FAILED is None:
        _FAILED = error
        _LOGGER.critical(
            "SQLite commits could not be flushed to disk, so this agent process sends and acknowledges "
            "nothing more. Check the disk, then restart the agent: it recovers from what is on the disk. "
            "event=sqlite_flush_failed_sending_stopped error_type=%s error=%s",
            type(error).__name__,
            error,
        )


def flush_commits_before_sending() -> None:
    """Switch this agent process to flushing before it sends; call once, before any agent starts."""

    install_before_send(_flush_before_send)
    sqlite_connection.defer_flushes_until_sent()


def flush_commits_at_exit(logger: logging.Logger) -> None:
    """Flush what the agent recorded after its last send, so a clean exit loses nothing."""

    if _FAILED is not None:
        logger.warning(
            "Exiting without flushing again: an earlier flush failed and a retry can wrongly succeed. "
            "event=sqlite_flush_at_exit_skipped"
        )
        return
    if not sqlite_connection.has_unflushed_commits():
        return
    try:
        sqlite_connection.flush_unflushed_commits(logger=logger)
    except Exception:
        # Already logged with the database (event=sqlite_flush_failed); the
        # process is ending and these records were never sent anywhere.
        logger.warning("Exiting with SQLite commits that are not flushed. event=sqlite_flush_at_exit_failed")


async def flush_local_pause_commits(
    logger: logging.Logger,
    *,
    agent_id: str,
    task_id: str,
    pause_kind: str,
) -> bool:
    """Flush deferred SQLite commits after a local pause (``WAITING``) that may send nothing to NATS.

    Reuses the process's send barrier (``custom_transport.send_barrier.before_send``)
    so it is a no-op where deferral is off and a failed flush still stops future
    sends as decision 82 requires. Never raises or undoes the pause: logs the
    failure with identifiers and returns False.
    """

    try:
        await before_send()
    except Exception as exc:  # noqa: BLE001 - keep the local pause; send barrier already stopped future sends
        logger.warning(
            "Could not flush deferred SQLite commits after local pause; keeping the pause in place. "
            "event=local_pause_commit_flush_failed agent_id=%s task_id=%s pause_kind=%s error_type=%s",
            agent_id,
            task_id,
            pause_kind,
            type(exc).__name__,
            exc_info=True,
        )
        return False
    return True
