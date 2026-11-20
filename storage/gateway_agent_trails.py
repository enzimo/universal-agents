"""The gateway store's part of Web Chat's agent trail (``assistant_gateway.domain.agent_trail``).

Two writes, both inside the caller's write transaction:

- ``record_request_agent``: an agent type worked on a running interactive
  request, found only by its task id (``gateway_requests.active_task_id``, the
  root task the gateway signed into the person's context). The request row is
  read ``FOR UPDATE`` on PostgreSQL; an agent type already recorded, a request
  that is not running, not interactive or not this gateway's changes nothing.
- ``rebuild_thread_trail``: the thread's stored copy, recomputed from its
  recorded requests. Recording and history cleanup both call it.

What keeps the copy right (plan review C03): the caller has taken the store's
write lock (``begin_write``: BEGIN IMMEDIATE on SQLite), and on PostgreSQL the
rebuild locks the thread's session row ``FOR UPDATE`` before it reads the
requests. Two writers of one thread's copy therefore take turns, and the one
that waited reads the other's committed requests, so a rebuild never writes a
copy made before another writer's change. The gateway's in-process writer queue
only keeps first-reported order. Lock order is request row, then session row;
history cleanup deletes only requests that are not running, which recording
never locks.

A rebuild reads every recorded request of the thread through the session
index: it runs only when a new agent type is recorded for a request (a few
times per request) and after cleanup, never on a page read.
"""

from __future__ import annotations

import logging
from typing import Any

from assistant_gateway.domain.agent_trail import (
    TRAIL_REQUESTS_READ,
    AgentTrailUnreadable,
    TrailAgent,
    decode_request_agents,
    encode_request_agents,
    encode_thread_trail,
    thread_trail,
    with_agent,
)
from assistant_gateway.domain.request_state import REQUEST_KIND_INTERACTIVE, RUN_STATE_RUNNING
from storage.sql_dialect import POSTGRES, SqlDialect


def _row_lock(dialect: SqlDialect) -> str:
    # SQLite has no row locks; the caller's BEGIN IMMEDIATE already serializes writers.
    return " FOR UPDATE" if dialect is POSTGRES else ""


def _agents(raw: str | None, correlation_id: str, logger: logging.Logger) -> tuple[TrailAgent, ...] | None:
    try:
        return decode_request_agents(raw)
    except AgentTrailUnreadable as error:
        logger.warning(
            "A request's recorded agents are unreadable; the thread's trail leaves them out. "
            "event=gateway_request_agents_unreadable correlation_id=%s error=%s",
            correlation_id,
            error,
        )
        return None


def record_request_agent(
    conn: Any, dialect: SqlDialect, *, active_task_id: str, agent: TrailAgent, logger: logging.Logger
) -> str | None:
    """Add ``agent`` to the running request whose task is ``active_task_id``; its session key when it was added."""

    bind = dialect.bind
    row = conn.execute(
        f"""
        SELECT correlation_id, session_key, agent_types_json FROM gateway_requests
        WHERE active_task_id = {bind} AND run_state = {bind} AND request_kind = {bind}
        LIMIT 1{_row_lock(dialect)}
        """,
        (active_task_id, RUN_STATE_RUNNING, REQUEST_KIND_INTERACTIVE),
    ).fetchone()
    if row is None:
        return None
    agents = _agents(row["agent_types_json"], str(row["correlation_id"]), logger)
    added = with_agent(agents, agent) if agents is not None else None
    if added is None:
        return None
    conn.execute(
        f"UPDATE gateway_requests SET agent_types_json = {bind} WHERE correlation_id = {bind}",
        (encode_request_agents(added), row["correlation_id"]),
    )
    session_key = str(row["session_key"])
    rebuild_thread_trail(conn, dialect, session_key, logger=logger)
    return session_key


def rebuild_thread_trail(conn: Any, dialect: SqlDialect, session_key: str, *, logger: logging.Logger) -> None:
    """Recompute one thread's stored trail from its recorded requests (module docstring); nothing without the
    thread."""

    bind = dialect.bind
    session = conn.execute(
        f"SELECT session_key FROM gateway_sessions WHERE session_key = {bind}{_row_lock(dialect)}", (session_key,)
    ).fetchone()
    if session is None:
        return
    rows = conn.execute(
        f"""
        SELECT correlation_id, agent_types_json FROM gateway_requests
        WHERE session_key = {bind} AND request_kind = {bind} AND agent_types_json IS NOT NULL
        ORDER BY created_at, correlation_id
        """,
        (session_key, REQUEST_KIND_INTERACTIVE),
    ).fetchall()
    # An unreadable list still counts as the person's turn; its agents are left out (logged).
    recorded = [_agents(row["agent_types_json"], str(row["correlation_id"]), logger) or () for row in rows]
    trail = thread_trail(list(reversed(recorded[-TRAIL_REQUESTS_READ:])), recorded)
    conn.execute(
        f"UPDATE gateway_sessions SET agent_trail_json = {bind} WHERE session_key = {bind}",
        (encode_thread_trail(trail) if trail.entries else None, session_key),
    )


__all__ = ["rebuild_thread_trail", "record_request_agent"]
