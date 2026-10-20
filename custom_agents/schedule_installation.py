"""Prepare and install a schedule that arrives as a ``ScheduledMessage``.

Two paths install such schedules, and both prepare them here first
(``prepare_install``):

- ``scheduled_runtime.handle_scheduled_message``: a schedule another runtime
  process sent (the gateway's schedule creation and maintenance installs,
  InternalImprovementAgent's own timer). Its ``install_mode`` decides what
  happens to a stored schedule with the same id.
- ``create_schedule_from_turn_action``: a model's ``schedule_message`` turn
  action, run by the turn-action outbox. It only ever creates a schedule.

Kept apart from ``scheduled_runtime`` so the outbox can use it without
importing the scheduler loop, which depends on the outbox. For the same reason
the reserved internal improvement schedule is checked in
``handle_scheduled_message``, the only path that can carry its name.
"""

from __future__ import annotations

from functools import partial
from typing import Any, Optional

from custom_agents.schedule_conversation_context import ensure_dedicated_schedule_conversation
from custom_agents.schedule_store import (
    SCHEDULE_KEPT,
    ScheduleClaimScope,
    owner_canonical_user_id_for_schedule,
    prepare_schedule_for_add,
    schedule_claim_scope_for_agent,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.enums import RequesterType


async def _preserve_inbound_routing(
    agent: Any,
    schedule_msg: ScheduledMessage,
    msg_header: Optional[MessageHeader],
    msg_meta: Optional[MessageMeta],
) -> None:
    """Keep the inbound header and metadata when the schedule omitted its own (recorded as a repair)."""

    repair_recorder = getattr(agent, "_record_runtime_repair_anomaly", None)
    related_task_id = getattr(schedule_msg.payload, "task_id", None) or getattr(
        schedule_msg.payload, "parent_task_id", None
    )
    if schedule_msg.payload_header is None and msg_header is not None:
        agent._logger.warning(
            f"[{agent.id}] ScheduledMessage {schedule_msg.schedule_id} omitted payload_header; "
            "preserving the inbound header at runtime."
        )
        if repair_recorder is not None:
            await repair_recorder(
                "scheduled_message_payload_header_preserved",
                "ScheduledMessage omitted payload_header; runtime preserved the inbound header.",
                related_task_id=related_task_id,
                details={
                    "schedule_id": schedule_msg.schedule_id,
                    "schedule_name": schedule_msg.schedule_name,
                    "payload_type": type(schedule_msg.payload).__name__,
                    "from_agent_id": msg_header.from_agent_id,
                    "response_routing_key": msg_header.response_routing_key,
                },
            )
        schedule_msg.payload_header = msg_header.model_copy(deep=True)
    if schedule_msg.payload_meta is None and msg_meta is not None:
        agent._logger.warning(
            f"[{agent.id}] ScheduledMessage {schedule_msg.schedule_id} omitted payload_meta; "
            "preserving the inbound metadata at runtime."
        )
        if repair_recorder is not None:
            await repair_recorder(
                "scheduled_message_payload_meta_preserved",
                "ScheduledMessage omitted payload_meta; runtime preserved the inbound metadata.",
                related_task_id=related_task_id,
                details={
                    "schedule_id": schedule_msg.schedule_id,
                    "schedule_name": schedule_msg.schedule_name,
                    "payload_type": type(schedule_msg.payload).__name__,
                    "correlation_id": getattr(msg_meta, "msg_correlation_id", None),
                    "conversation_id": getattr(msg_meta, "conversation_id", None),
                },
            )
        normalized_meta = msg_meta.model_copy(deep=True)
        normalized_meta.requester_type = RequesterType.TIMER_REQUESTER
        schedule_msg.payload_meta = normalized_meta


async def prepare_install(
    agent: Any,
    msg: ScheduledMessage,
    msg_header: Optional[MessageHeader],
    msg_meta: Optional[MessageMeta],
) -> tuple[ScheduledMessage, ScheduleClaimScope]:
    """A copy of the schedule as it will be stored, and who may claim it.

    Fills what trusted code owns: the inbound routing a schedule omitted, a
    recurring schedule's own conversation, the claim scope and the store's
    defaults.
    """

    schedule_msg = msg.model_copy(deep=True)
    await _preserve_inbound_routing(agent, schedule_msg, msg_header, msg_meta)
    schedule_msg, schedule_conversation = ensure_dedicated_schedule_conversation(
        agent,
        schedule_msg,
        source_header=msg_header,
        source_meta=msg_meta,
    )
    if schedule_conversation:
        agent._logger.info(
            "[%s] Forked recurring schedule %s into dedicated conversation %s seeded by %s.",
            agent.id,
            schedule_msg.schedule_id,
            schedule_conversation.get("conversation_id"),
            schedule_conversation.get("seed_task_id"),
        )
    claim_scope = schedule_claim_scope_for_agent(agent)
    prepare_schedule_for_add(schedule_msg, **claim_scope, default_cron_timezone=getattr(agent, "instance_timezone", None))
    return schedule_msg, claim_scope


async def create_schedule_from_turn_action(
    agent: Any,
    msg: ScheduledMessage,
    *,
    msg_header: Optional[MessageHeader] = None,
    msg_meta: Optional[MessageMeta] = None,
) -> None:
    """Create the schedule a model's ``schedule_message`` action describes; never change a stored one.

    The model names no schedule: the runtime gave the action a new id before
    the outbox saved it (``structured_llm_messages.ModelScheduledMessagePayload``),
    and existing schedules change only through the schedule tools, which check
    the owner and the version read. Until 2026-10-20 this path replaced a
    stored schedule with the action's id, owner included, so one person's
    request could overwrite another person's schedule.

    A stored schedule with the action's id is normally the action's own: the
    outbox runs a saved action again after a restart or a failed later step.
    It is kept as it is. One that belongs to someone else did not come from
    this action; it is kept too and the anomaly is logged.

    The action's payload has no schedule name, so it never names the reserved
    internal improvement schedule that ``handle_scheduled_message`` checks.
    """

    schedule_msg, claim_scope = await prepare_install(agent, msg, msg_header, msg_meta)
    store = agent._schedule_store
    outcome = await agent.task_manager.operations_runtime.run(
        partial(store.add, schedule_msg, create_only=True, **claim_scope)
    )
    if outcome == SCHEDULE_KEPT:
        await _log_kept_schedule(agent, store, schedule_msg)
    if agent._scheduler_wake:
        agent._scheduler_wake.set()


async def _log_kept_schedule(agent: Any, store: Any, schedule_msg: ScheduledMessage) -> None:
    stored = await agent.task_manager.operations_runtime.run(
        store.get_scheduled_message, schedule_msg.schedule_id
    )
    stored_owner = owner_canonical_user_id_for_schedule(stored) if stored is not None else None
    action_owner = owner_canonical_user_id_for_schedule(schedule_msg)
    if stored_owner == action_owner:
        agent._logger.info(
            "[%s] The schedule this action creates already exists (an earlier attempt saved it); kept it. "
            "event=schedule_action_already_saved schedule_id=%s",
            agent.id,
            schedule_msg.schedule_id,
        )
        return
    agent._logger.warning(
        "[%s] A schedule action named a schedule that belongs to someone else; nothing was changed "
        "or created. event=schedule_action_id_in_use schedule_id=%s stored_owner=%s action_owner=%s",
        agent.id,
        schedule_msg.schedule_id,
        stored_owner,
        action_owner,
    )
