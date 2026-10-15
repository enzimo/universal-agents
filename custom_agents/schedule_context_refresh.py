"""Refresh the person and session of a runtime-managed schedule (install mode ``refresh``).

The gateway installs the runtime's maintenance schedules and signs their
context for the person they belong to. A stored schedule keeps the context it
was saved with, including where to reach that person (their chat session),
which maintenance findings and memory-review results use. Sessions change, so
at each gateway start, and whenever the manager becomes available again, the
gateway sends a ``refresh`` for each maintenance schedule (memory review,
internal-improvement maintenance). Roles do not come from here: every run
reads the owner's current roles from the gateway's copy of the team's people
(``access_control.people_directory``, ``task_context_runtime``), so a schedule
the manager creates after the last refresh already runs with the right roles.

A refresh replaces only the person and session fields of the stored gateway
context (``PERSON_GATEWAY_FIELDS``), under the trusted-runtime permit that
runtime installs use, and keeps everything an operator or the scheduler
controls: pause, enabled state, cadence, next fire time, run counts, schedule
memory, instructions and claims. The schedule also takes the incoming
conversation, the session conversation of the person the gateway built it for
(a new one when the person changed and the gateway sent none): a conversation
belongs to one person, and runs never continue someone else's
(``custom_agents.conversation_runtime``). For the same person that is
normally the stored conversation, so their runs keep their history; it also
moves a team schedule an older release handed to another administrator, still
naming the departed person's conversation, into the new owner's at the next
gateway start. Why not ``replace``: replacing the row
re-enabled paused or disabled schedules and reset their cadence at every
gateway start. A refresh creates nothing (the schedule's own installer does
that) and never touches a schedule a person manages. Agents cannot send one:
``send_message`` refuses system-managed schedule messages and the structured
schedule action creates person-managed ones only.
"""

from __future__ import annotations

import logging
from typing import Any

from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    SystemScheduleMutationDenied,
    build_schedule_mutation_permit,
    effective_system_schedule_kind,
    is_system_managed_schedule,
)
from custom_agents.schedule_execution_identity import follow_authorized_change
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from custom_messages.contracts.routing_v1 import PERSON_GATEWAY_FIELDS
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from id_generation import new_id

logger = logging.getLogger(__name__)


def _gateway_context(schedule: Any) -> Any:
    return getattr(getattr(schedule, "payload_meta", None), "gateway_context", None)


def _conversation_id(schedule: Any) -> str | None:
    return getattr(getattr(schedule, "payload_meta", None), "conversation_id", None)


def _refusal(existing: ScheduledMessage | None, incoming: ScheduledMessage) -> str | None:
    """Why this refresh does not apply, or None."""

    if existing is None:
        return "schedule_missing"
    if not (is_system_managed_schedule(existing) and is_system_managed_schedule(incoming)):
        return "not_system_managed"
    if (
        existing.schedule_name != incoming.schedule_name
        or effective_system_schedule_kind(existing) != effective_system_schedule_kind(incoming)
    ):
        return "schedule_mismatch"
    if _gateway_context(existing) is None or _gateway_context(incoming) is None:
        return "no_gateway_context"
    return None


def refreshed_schedule(existing: ScheduledMessage, incoming: ScheduledMessage) -> ScheduledMessage:
    """The stored schedule with the incoming person, session and conversation (module docstring).

    The conversation is the incoming one, the session conversation of the
    person the gateway built the schedule for, or a new one when the person
    changed and none came, because a conversation belongs to one person.
    """

    candidate = existing.model_copy(deep=True)
    stored, fresh = _gateway_context(candidate), _gateway_context(incoming)
    person_changed = owner_canonical_user_id_for_schedule(existing) != owner_canonical_user_id_for_schedule(incoming)
    for name in PERSON_GATEWAY_FIELDS:
        setattr(stored, name, getattr(fresh, name, None))
    meta = candidate.payload_meta
    conversation_id = _conversation_id(incoming) or (new_id("conversation_id") if person_changed else None)
    if meta is not None and conversation_id and conversation_id != meta.conversation_id:
        meta.conversation_id = conversation_id
        meta.followup_to_task_id = None
    return candidate


def refresh_schedule_gateway_context(agent: Any, incoming: ScheduledMessage) -> bool:
    """Apply one ``refresh`` in the manager; True when the stored schedule changed."""

    store = agent._schedule_store
    existing = store.get_scheduled_message(incoming.schedule_id)
    reason = _refusal(existing, incoming)
    if reason is not None:
        log = logger.info if reason == "schedule_missing" else logger.warning
        log(
            "A schedule context refresh was not applied; nothing was created or changed. "
            "event=schedule_context_refresh_skipped reason=%s schedule_id=%s schedule_name=%s",
            reason,
            incoming.schedule_id,
            incoming.schedule_name,
        )
        return False
    candidate = refreshed_schedule(existing, incoming)
    owner_before = owner_canonical_user_id_for_schedule(existing)
    permit = build_schedule_mutation_permit(
        schedule=existing,
        operation=ScheduleMutationOperation.UPDATE,
        authority_mode=ScheduleMutationAuthorityMode.TRUSTED_RUNTIME,
        actor_principal_id=str(getattr(agent, "id", "") or "runtime"),
        owner_canonical_user_id=str(owner_before or ""),
        team_id=str(getattr(agent, "team_id", "") or ""),
        candidate=candidate,
    )
    row = store.get_row(incoming.schedule_id) or {}
    try:
        store.add(candidate, remaining_count_override=row.get("remaining_count"), system_mutation_permit=permit)
    except SystemScheduleMutationDenied as exc:
        # The schedule changed in between (for example it just fired); the next refresh applies.
        logger.warning(
            "A schedule context refresh lost a race with another change and was not applied. "
            "event=schedule_context_refresh_conflict schedule_id=%s reason=%s",
            incoming.schedule_id,
            exc.reason_code,
        )
        return False
    # Trusted runtime code moved the person's chat route; the schedule still does
    # the same work for the same person, so its worker policy follows (a new
    # owner keeps the review block).
    follow_authorized_change(
        store.worker_records,
        str(getattr(agent, "team_id", "") or ""),
        existing,
        store.get_scheduled_message(incoming.schedule_id),
        source="gateway_context_refresh",
    )
    logger.info(
        "Refreshed the person and session of a runtime-managed schedule. event=schedule_context_refreshed "
        "schedule_id=%s schedule_name=%s owner_before=%s owner_after=%s signed=%s "
        "conversation_before=%s conversation_after=%s",
        incoming.schedule_id,
        incoming.schedule_name,
        owner_before,
        owner_canonical_user_id_for_schedule(candidate),
        getattr(_gateway_context(candidate), "task_context", None) is not None,
        _conversation_id(existing),
        _conversation_id(candidate),
    )
    return True


__all__ = ["refresh_schedule_gateway_context", "refreshed_schedule"]
