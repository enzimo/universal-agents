"""Tell administrators when messages are set aside (the dead-letter stream).

A receiver ends a message it cannot read or handle on its last allowed
delivery and first copies it to the dead-letter stream, kept three days
(``custom_transport.nats_pull``, ``custom_transport.dead_letters``). The owner
decided on 2026-10-07 that an administrator must hear about every such
message, not only find it in a log. Before 0.8.1 one such message stopped its
receiver; between 0.8.1 and this change it was ended with only a log line.

The team lead's scheduler pass (``scheduled_runtime``) runs ``report_dead_letters``
with no model turn, like the stuck-work notices (``custom_agents.stuck_work``):
it reads new copies through its own durable consumer of the stream, stores one
Inbox notice for administrators per batch (once per batch id), sends one chat
message to the instance owner's delivery chat, and only then acknowledges the
batch, so a report that reached nobody is retried on the next pass. After a
report the next waits ``REPORT_QUIET_MINUTES`` and covers the next bounded batch,
so a flood gives one notice, not one per pass. Notices name
message types, ids, routes, reasons, releases and times, never message content.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from hashlib import sha256
from typing import Any, Sequence

from access_control.people import PERSON_ROLE_ADMIN, is_administrator
from custom_agents.stuck_work import ChatRoute, StuckMessage, send_chat_notice
from custom_messages.contracts.access_v2 import AccessNoticeKind, AccessNoticeV1
from custom_transport.dead_letters import (
    REASON_HANDLER_FAILED,
    REASON_UNDECODABLE,
    WATCH_CONSUMER_ROUTE,
    DeadLetterRecord,
)
from custom_transport.nats_topology import DEAD_LETTER_MAX_AGE_SECONDS
from storage.access_repository import AccessRepository

logger = logging.getLogger(__name__)

# How many set-aside messages one notice covers at most.
WATCH_BATCH = 200
# How long after a report the next one waits. A receiver that keeps failing (an
# out-of-date release still sending, say) must not send a chat message every
# pass; copies wait in the bounded stream meanwhile for the next report.
REPORT_QUIET_MINUTES = 15
# How many of them a notice lists one by one.
LISTED_IN_NOTICE = 10
# The reason the gateway sees on the chat message.
DEAD_LETTER_NOTICE_REASON = "messages_set_aside"

_REASON_WORDS = {
    REASON_UNDECODABLE: "this release could not read it",
    REASON_HANDLER_FAILED: "handling it kept failing",
}


@dataclass
class DeadLetterWatchState:
    """When the team lead last reported (in memory: a restart may report early once)."""

    reported_at: datetime | None = None


def _line(record: DeadLetterRecord) -> str:
    kind = record.message_class or "an unreadable message"
    reason = _REASON_WORDS.get(record.reason, record.reason or "unknown reason")
    receiver = record.receiver or record.original_subject or "an unknown receiver"
    sent_by = f", set aside by release {record.release}" if record.release else ""
    message_id = f" ({record.message_id})" if record.message_id else ""
    return (
        f"- {kind}{message_id} for {receiver}: {reason}{sent_by}, "
        f"at {record.set_aside_at or 'unknown time'}; copy {record.copy_id}."
    )


def dead_letter_notice(
    records: Sequence[DeadLetterRecord], *, stream: str
) -> StuckMessage:
    """Plain words for administrators: what was set aside, why, and what to do."""

    count = len(records)
    days = DEAD_LETTER_MAX_AGE_SECONDS // (24 * 60 * 60)
    listed = [_line(record) for record in records[:LISTED_IN_NOTICE]]
    if count > LISTED_IN_NOTICE:
        listed.append(f"- and {count - LISTED_IN_NOTICE} more.")
    waiting = records[-1].waiting_after if records else 0
    if waiting:
        listed.append(
            f"- {waiting} more were set aside after these; the next notice lists them."
        )
    undecodable = any(record.reason == REASON_UNDECODABLE for record in records)
    advice = (
        "A message this release cannot read usually comes from an agent or gateway running "
        "another release; run the same release everywhere, or stop the out-of-date one."
        if undecodable
        else "Its handling failed every time it was delivered; the logs name the error "
        "(event=nats_delivery_handler_failed)."
    )
    return StuckMessage(
        title=f"{count} message(s) could not be handled and were set aside",
        text=(
            "\n".join(listed)
            + f"\n\n{advice} The messages are kept for up to {days} days in the NATS stream {stream} "
            "for an operator to look at; nothing was sent to anyone in their place."
        ),
    )


def _owner_chat(agent: Any) -> ChatRoute | None:
    """The configured chat, only while its person is an active administrator."""

    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents.instance_delivery_context import configured_owner_route
    from custom_agents.task_context_runtime import people_directory

    path = getattr(agent, "instance_config_path", None)
    if not path:
        return None
    owner = GatewayInstanceConfig.load_for_process(path).user.canonical_user_id
    person = people_directory(agent).person(owner)
    if not person.active or not is_administrator(person.roles):
        return None
    configured = configured_owner_route(path, owner)
    return ChatRoute(*configured) if configured else None


def _notice_in_place(agent: Any, notice_id: str, message: StuckMessage) -> bool:
    """Whether administrators' Inbox has the notice after this call (stored now or on an earlier pass).

    ``send_runtime_notice_once`` answers False both when the notice already
    exists and when storing failed; the watcher must tell those apart to know
    whether a batch it could not acknowledge last time was already reported.
    """

    try:
        AccessRepository(agent.task_manager.storage_settings).create_notice_if_absent(
            AccessNoticeV1(
                notice_id=notice_id[:120],
                team_id=str(agent.team_id),
                kind=AccessNoticeKind.MESSAGES_SET_ASIDE,
                recipient_roles=(PERSON_ROLE_ADMIN,),
                title=message.title[:400],
                summary=message.text[:4000],
                created_at_utc=datetime.now(timezone.utc),
            )
        )
    except Exception:
        logger.exception(
            "Could not store the notice about set-aside messages. event=dead_letter_notice_store_failed "
            "notice_id=%s",
            notice_id,
        )
        return False
    return True


def dead_letter_notice_id(team_id: str, records: Sequence[DeadLetterRecord]) -> str:
    """Stable across retries, distinct after a stream restart or batch change.

    Stream sequence numbers restart with a memory-backed broker; notice history
    survives in the access store. Hash the actual copy identities, not their
    transient positions, and include the team because notice ids are global.
    """

    identity = json.dumps([team_id, sorted({record.copy_id for record in records})])
    return f"dead_letters:{sha256(identity.encode()).hexdigest()}"


async def _send_chat(agent: Any, message: StuckMessage, notice_id: str) -> bool:
    """A chat failure must not undo successful durable Inbox reporting."""

    try:
        route = await agent.task_manager.operations_runtime.run(
            partial(_owner_chat, agent)
        )
        return await send_chat_notice(
            agent,
            route,
            message,
            reason=DEAD_LETTER_NOTICE_REASON,
            correlation_id=notice_id,
            source_task_id=None,
        )
    except Exception:
        agent._logger.exception(
            "Could not send the chat report about set-aside messages. "
            "event=dead_letter_notice_chat_failed notice_id=%s",
            notice_id,
        )
        return False


async def report_dead_letters(agent: Any) -> None:
    """One pass on the team lead: tell administrators about newly set-aside messages."""

    if not getattr(agent, "is_team_lead", False) or not getattr(agent, "team_id", None):
        return
    # Nothing here may fail the scheduler pass, which also fires due schedules.
    try:
        state: DeadLetterWatchState = agent._dead_letter_watch
        moment = datetime.now(timezone.utc)
        if state.reported_at is not None and moment - state.reported_at < timedelta(
            minutes=REPORT_QUIET_MINUTES
        ):
            return
        adapter = agent._ensure_nats_transport_adapter()
        if not adapter.connected:
            # The agent's start connects the transport; the watch never does.
            return
        records = await adapter.fetch_dead_letters(
            consumer_route=WATCH_CONSUMER_ROUTE, batch=WATCH_BATCH
        )
        if not records:
            return
        message = dead_letter_notice(
            records, stream=adapter.mapper.dead_letter_stream_name()
        )
        notice_id = dead_letter_notice_id(str(agent.team_id), records)
        stored = await agent.task_manager.operations_runtime.run(
            partial(_notice_in_place, agent, notice_id, message)
        )
        sent = await _send_chat(agent, message, notice_id)
        if not stored and not sent:
            # Nobody was told; leave the batch unacknowledged for the next pass.
            agent._logger.error(
                "Could not tell administrators about set-aside messages; retrying on the next pass. "
                "event=dead_letter_notice_failed notice_id=%s count=%s",
                notice_id,
                len(records),
            )
            return
        for record in records:
            await record.ack()
        state.reported_at = moment
        agent._logger.warning(
            "Told administrators about set-aside messages. event=dead_letters_reported notice_id=%s "
            "count=%s inbox=%s chat=%s message_classes=%s",
            notice_id,
            len(records),
            stored,
            sent,
            ",".join(sorted({record.message_class or "-" for record in records})),
        )
    except Exception:
        agent._logger.exception(
            "Could not check for set-aside messages; retrying on the next pass. event=dead_letter_watch_failed "
            "team_id=%s",
            getattr(agent, "team_id", None),
        )
