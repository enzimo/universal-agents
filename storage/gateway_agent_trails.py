"""The gateway store's part of Web Chat's agent trail (``assistant_gateway.domain.agent_trail``).

Two writes, both inside the caller's write transaction, and one plain read:

- ``record_request_agent``: an agent type worked on a person's request, found
  only by its task id (``gateway_requests.active_task_id``, the root task the
  gateway signed into the person's context). The request row is read ``FOR
  UPDATE`` on PostgreSQL; an agent type already recorded, or an id that names
  no request of this gateway, changes nothing.
- ``rebuild_thread_trail``: the thread's stored copy, recomputed from its
  recorded requests. Recording and history cleanup both call it.
- ``would_record``: the same check as a plain read, so an id that changes
  nothing never takes the write lock (review F15).

Which rows are the person's requests (review F1): every request the gateway
admitted as a thread's request has a run state (running, then finished), and
nothing else does. Its kind does not tell: the answer to the lead's question
and the result of a setup page or credential form are recorded on the
request's own row and rewrite its ``request_kind`` and ``created_at``
(``GatewayRequestService.record``, which other readers rely on). So a request
is found by its task and a run state, of any kind, running or finished: an
agent record that arrives after the request finished (the record's two-second
interval, or a write that waited behind a failed one, review F4) still counts.
The trail is ordered by ``first_agent_at``, written once when the request's
first agent is recorded (the team lead, at hand-over) and never rewritten.

What keeps the copy right (plan review C03): the caller has taken the store's
write lock (``begin_write``: BEGIN IMMEDIATE on SQLite), and on PostgreSQL the
rebuild locks the thread's session row ``FOR UPDATE`` before it reads the
requests. Two writers of one thread's copy therefore take turns, and the one
that waited reads the other's committed requests, so a rebuild never writes a
copy made before another writer's change. The gateway's in-process writer queue
only keeps first-reported order. Lock order is request row, then session row,
for recording and for history cleanup (which deletes requests before it
changes sessions), and recording waits holding nothing else, so they cannot
deadlock; a request cleanup deleted first is simply not found.

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


def _request_row(conn: Any, dialect: SqlDialect, active_task_id: str, *, lock: bool) -> Any:
    """The person's request whose task is ``active_task_id`` (module docstring), or None."""

    bind = dialect.bind
    return conn.execute(
        f"""
        SELECT correlation_id, session_key, agent_types_json FROM gateway_requests
        WHERE active_task_id = {bind} AND run_state IS NOT NULL
        LIMIT 1{_row_lock(dialect) if lock else ""}
        """,
        (active_task_id,),
    ).fetchone()


def would_record(conn: Any, dialect: SqlDialect, *, active_task_id: str, agent: TrailAgent) -> bool:
    """Whether recording ``agent`` could change anything, read without locks; ``record_request_agent`` decides."""

    row = _request_row(conn, dialect, active_task_id, lock=False)
    if row is None:
        return False
    try:
        return with_agent(decode_request_agents(row["agent_types_json"]), agent) is not None
    except AgentTrailUnreadable:
        return True  # the locked path logs it


def record_request_agent(
    conn: Any, dialect: SqlDialect, *, active_task_id: str, agent: TrailAgent, logger: logging.Logger, now: str
) -> str | None:
    """Add ``agent`` to the person's request whose task is ``active_task_id``; its session key when it was added.

    ``now`` is when the request's first agent was recorded, kept only if none was before (``first_agent_at``).
    """

    bind = dialect.bind
    row = _request_row(conn, dialect, active_task_id, lock=True)
    if row is None:
        return None
    agents = _agents(row["agent_types_json"], str(row["correlation_id"]), logger)
    added = with_agent(agents, agent) if agents is not None else None
    if added is None:
        return None
    conn.execute(
        f"""
        UPDATE gateway_requests SET agent_types_json = {bind}, first_agent_at = COALESCE(first_agent_at, {bind})
        WHERE correlation_id = {bind}
        """,
        (encode_request_agents(added), now, row["correlation_id"]),
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
        WHERE session_key = {bind} AND run_state IS NOT NULL AND agent_types_json IS NOT NULL
        ORDER BY first_agent_at, correlation_id
        """,
        (session_key,),
    ).fetchall()
    # An unreadable list still counts as the person's turn; its agents are left out (logged).
    recorded = [_agents(row["agent_types_json"], str(row["correlation_id"]), logger) or () for row in rows]
    trail = thread_trail(list(reversed(recorded[-TRAIL_REQUESTS_READ:])), recorded)
    conn.execute(
        f"UPDATE gateway_sessions SET agent_trail_json = {bind} WHERE session_key = {bind}",
        (encode_thread_trail(trail) if trail.entries else None, session_key),
    )


__all__ = ["rebuild_thread_trail", "record_request_agent", "would_record"]
