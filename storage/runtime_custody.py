"""Saved-work custody checks shared by transactions that complete deferred handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
import logging
from typing import Any

from storage.sql_dialect import POSTGRES, SqlDialect


class RuntimeRecoveryConflict(RuntimeError):
    """A source changed or another runtime completed this recovery first."""


class CustodyStore(StrEnum):
    TASK_RUNTIME = "task_runtime"
    DEFERRED_MESSAGES = "deferred_messages"


class RuntimeCustodyLost(RuntimeRecoveryConflict):
    """The saving agent no longer holds its task row or the dispatched message.

    Takeover deletes the previous owner's row atomically with giving the work
    to its new owner. Writes expecting that row are refused before any effect.
    A deferred dispatch also needs its exact message: a newly created row for
    later messages cannot authorize transferred work. Transaction owners decide
    when a complete durable effect may consume that message.
    """

    def __init__(self, owner_agent_id: str, store: CustodyStore) -> None:
        super().__init__(f"Saved {store.value} of {owner_agent_id} now belongs to another agent.")
        self.owner_agent_id = owner_agent_id
        self.store = store


@dataclass(frozen=True)
class DeferredMessageClaim:
    """The exact inbox message a trusted transaction completes."""

    owner_agent_id: str
    message_id: str


def check_deferred_message(
    conn: Any, dialect: SqlDialect, claim: DeferredMessageClaim,
    *, consume: bool, logger: logging.Logger,
) -> None:
    """Check under the caller's transaction lock, optionally consuming the message.

    The row stays after its last message leaves: an incoming admission may
    still expect it. The inbox deletes an empty row with its checked removal.
    SQLite callers must already hold BEGIN IMMEDIATE; PostgreSQL locks the row.
    """
    lock = " FOR UPDATE" if dialect == POSTGRES else ""
    row = conn.execute(
        f"SELECT messages_json FROM agent_deferred_messages WHERE owner_agent_id = {dialect.bind}{lock}",
        (claim.owner_agent_id,),
    ).fetchone()
    messages = None if row is None else json.loads(row[0])
    if messages is not None and not isinstance(messages, list):
        raise ValueError("Deferred snapshot must contain a message list.")
    remaining = [] if messages is None else [
        item for item in messages if deferred_message_id(item) != claim.message_id
    ]
    if messages is None or len(remaining) == len(messages):
        logger.warning(
            "The dispatched message is no longer in this agent's deferred row. "
            "event=deferred_message_custody_refused owner_agent_id=%s message_id=%s row_exists=%s",
            claim.owner_agent_id, claim.message_id, messages is not None,
        )
        raise RuntimeCustodyLost(claim.owner_agent_id, CustodyStore.DEFERRED_MESSAGES)
    if consume:
        conn.execute(
            f"UPDATE agent_deferred_messages SET messages_json = {dialect.bind}, updated_at = {dialect.bind} WHERE owner_agent_id = {dialect.bind}",
            (json.dumps(remaining), datetime.now(timezone.utc).isoformat(), claim.owner_agent_id),
        )


def deferred_message_id(item: Any) -> str | None:
    """The message id of one stored deferred envelope."""
    header = item.get("msg_header") if isinstance(item, dict) else None
    value = header.get("message_id") if isinstance(header, dict) else None
    return value if isinstance(value, str) else None
