"""Which people's requests an agent's model turns run for, for its signed record.

Web Chat's thread list shows who works on a person's request now (the agent
trail). The agent says it in its signed state record (``requests``, contract
README "State records"), and the gateway maps each id only through its own
record of a running request (``assistant_gateway.application.request_agents_service``).

Why it is decided once per turn, at its start (plan review C01): the request a
turn serves cannot change while the turn runs, and the agent's state is reported
often (lane entry and exit, turn start and end, restore). Checking each running
turn's signature at every report would repeat work, and resolving a scheduled
run's identity reads the schedule and its person from the store. So
``start_turn`` asks ``turn_request_id`` once and keeps the answer on the turn's
work record (``TaskWork.request_task_id``); ``working_request_ids`` only reads
those records.

What counts: the turn's task carries a gateway-signed context
(``access_control.task_identity``) that names a person, no schedule and a root
task. That root task id is the gateway's own request task: the gateway signs it
when it admits the person's message, and delegated tasks copy the parent's
context unchanged (``custom_agents.delegation_runtime``). A task with a schedule
run identity, a scheduler-signed context, a gateway context for a scheduled job
(``schedule_id``) or no signed context at all reports nothing: scheduled runs
and system work have no gateway request. Nothing here trusts a message field.
"""

from __future__ import annotations

import logging
from typing import Any

from access_control.authority_verifier import IdentityRejected
from access_control.task_identity import resolve_task_identity
from custom_messages.wire.authority_bindings import GATEWAY_ISSUER_ROLE

logger = logging.getLogger(__name__)


def turn_request_id(agent: Any, task_id: str | None) -> str | None:
    """The person's request ``task_id``'s turn runs for, or None (module docstring).

    Never raises: a turn that cannot say whom it works for reports nothing, and
    an unexpected failure is logged with the agent and task, so the turn and the
    agent's state report go on.
    """

    if not task_id:
        return None
    try:
        task_info = agent.task_manager.get_task_info(task_id)
        gateway_context = getattr(getattr(task_info, "meta", None), "gateway_context", None)
        signed = getattr(gateway_context, "task_context", None)
        # A schedule run identity or no signed context: no gateway request, and no store read to find that out.
        if signed is None or getattr(gateway_context, "schedule_run", None) is not None:
            return None
        from custom_agents.access_runtime import runtime_access

        access = runtime_access(agent)
        # Gateway-only: scheduler-signed tokens never represent a person's gateway request (C04).
        context = resolve_task_identity(
            verifier=access.verifier,
            team_id=access.team_id,
            task_context=signed,
            schedule_run=None,
            schedule_runs=None,
            require_issuer="gateway",
        )
    except (IdentityRejected, ValueError):
        return None
    except Exception:
        logger.warning(
            "Could not tell which person's request a turn works for; the agent's record leaves it out. "
            "event=working_request_unresolved agent_id=%s task_id=%s",
            getattr(agent, "id", None),
            task_id,
            exc_info=True,
        )
        return None
    if (
        context.issuer != GATEWAY_ISSUER_ROLE
        or (context.issuer_roles and GATEWAY_ISSUER_ROLE not in context.issuer_roles)
        or context.person_id is None
        or context.schedule_id is not None
    ):
        return None
    return context.root_task_id or None


def working_request_ids(agent: Any) -> tuple[str, ...]:
    """The person's requests the agent's model turns run for now, in turn order, each once."""

    # The agent's running turns (``custom_agents.turn_registry``); an agent built without a runtime has none.
    turns = getattr(agent, "_running_turns", None) or {}
    ids = (getattr(work, "request_task_id", None) for work in list(turns.values()))
    return tuple(dict.fromkeys(item for item in ids if item))


__all__ = ["turn_request_id", "working_request_ids"]
