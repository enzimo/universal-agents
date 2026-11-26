"""Attach scheduler-signed standing protocol 1 permits to scheduled runs and lead maintenance tasks.

Standing permits are issued and renewed solely by ``team_services.scheduler`` (the
``team-services`` process holds the ``scheduler`` role in its host certificate; no
model-running process holds any signing key, Decision 58):

- ``attach_run_permit``: attaches the schedule row's ``run_permit_json`` (a scheduler-signed
  protocol 1 ``AuthorityToken`` with ``bound_to.id = "schedule_permit:<schedule_id>"``)
  wrapped in ``ScheduleRunIdentityV1`` with a fresh ``schedule_run_<hex>`` id per run.
  This keeps ``team_services`` off the hot path of normal runs (owner's 2026-09-27
  permit-first decision).
- ``attach_owner_permit``: attaches ``owner_permit_json`` (``schedule_owner_permit:<schedule_id>``)
  for system-work schedules that also name a vouched administrator owner (used by
  ``sign_system_escalation`` and ``sign_for_maintenance_owner``).
- ``sign_system_run``: synchronous permit-only helper for system-work runs (clears
  ``PERSON_GATEWAY_FIELDS`` and attaches the system-work run permit). Async callers with
  a claim use ``scheduled_run_contexts.sign_system_run_async`` so they fall back to
  ``run_context.sign`` on ``svc.scheduler`` when no permit is stored yet.

Every enforcement point verifies the wrapped permit through
``access_control.task_identity.resolve_task_identity``, which checks the schedule row
and the owner's live standing and roles (cached 30 seconds).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from access_control.authority_verifier import AuthorityTokenVerifier, IdentityRejected, get_process_verifier
from custom_messages.contracts.access_v2 import SCHEDULE_RUN_ID_PREFIX, ScheduleRunIdentityV1
from custom_messages.contracts.routing_v1 import PERSON_GATEWAY_FIELDS
from custom_messages.wire.authority_bindings import (
    AuthorityTokenRecord,
    SCHEDULE_OWNER_PERMIT_PREFIX,
    is_schedule_permit_binding,
)
from id_generation import new_id

logger = logging.getLogger(__name__)


def _verifier_for(agent: Any) -> AuthorityTokenVerifier:
    if agent is not None:
        try:
            from custom_agents.access_runtime import runtime_access

            return runtime_access(agent).verifier
        except RuntimeError:
            pass
    process_verifier = get_process_verifier()
    if process_verifier is not None:
        return process_verifier
    from custom_agents.agent_requests import process_host

    return AuthorityTokenVerifier(process_host)


def _schedule_store(agent: Any) -> Any:
    """The agent's schedule store, opened on first use when the agent opens it lazily; None without one."""

    store = getattr(agent, "_schedule_store", None)
    if store is None and callable(getattr(agent, "_ensure_schedule_store", None)):
        store = agent._ensure_schedule_store()
    return store


def _usable_permit(
    agent: Any,
    schedule_id: str,
    person_id: str | None,
    *,
    owner_permit: bool = False,
) -> AuthorityTokenRecord | None:
    """The row's protocol 1 permit when it verifies and names this schedule and person, else None (logged)."""

    store = _schedule_store(agent)
    if store is None:
        return None
    permit: AuthorityTokenRecord | None = (
        store.owner_permit(schedule_id) if owner_permit else store.run_permit(schedule_id)
    )
    if permit is None:
        return None
    try:
        verified = _verifier_for(agent).verify_schedule_permit(permit, team_id=agent.team_id)
    except (IdentityRejected, ValueError) as exc:
        reason = str(exc)
    else:
        bound_id = str(getattr(permit.bound_to, "id", "") or "")
        is_owner = bound_id.startswith(SCHEDULE_OWNER_PERMIT_PREFIX)
        binding_ok = is_owner if owner_permit else (is_schedule_permit_binding(bound_id) and not is_owner)
        if binding_ok and verified.schedule_id == schedule_id and verified.person_id == person_id:
            return permit
        reason = "the permit names another schedule, person, or binding kind"
    logger.warning(
        "A schedule's stored %s cannot be used for this run. "
        "event=schedule_permit_unusable schedule_id=%s person_id=%s reason=%s",
        "owner permit" if owner_permit else "run permit",
        schedule_id,
        person_id,
        reason,
    )
    return None


def attach_run_permit(agent: Any, meta: Any, *, schedule_id: str, person_id: str | None) -> bool:
    """Give a run its identity from the schedule's standing run permit; False when none is usable.

    Reads storage, so async callers run it in a thread.
    """

    gateway_context = getattr(meta, "gateway_context", None)
    if gateway_context is None:
        return False
    permit = _usable_permit(agent, schedule_id, person_id, owner_permit=False)
    if permit is None:
        return False
    gateway_context.task_context = None
    gateway_context.schedule_run = ScheduleRunIdentityV1(
        permit=permit,
        context_id=new_id(SCHEDULE_RUN_ID_PREFIX),
        conversation_id=getattr(meta, "conversation_id", None),
        started_at_utc=datetime.now(timezone.utc),
    )
    logger.info(
        "A scheduled run carries its schedule's standing permit; svc.scheduler was not asked. "
        "event=scheduled_run_permit_attached schedule_id=%s person_id=%s agent_id=%s token_id=%s",
        schedule_id,
        person_id,
        getattr(agent, "id", None),
        permit.token_id,
    )
    return True


def attach_owner_permit(agent: Any, meta: Any, *, schedule_id: str, person_id: str) -> bool:
    """Give a lead maintenance task its identity from the maintenance schedule's owner permit."""

    gateway_context = getattr(meta, "gateway_context", None)
    if gateway_context is None or not person_id:
        return False
    permit = _usable_permit(agent, schedule_id, person_id, owner_permit=True)
    if permit is None:
        return False
    gateway_context.task_context = None
    gateway_context.schedule_run = ScheduleRunIdentityV1(
        permit=permit,
        context_id=new_id(SCHEDULE_RUN_ID_PREFIX),
        conversation_id=getattr(meta, "conversation_id", None),
        started_at_utc=datetime.now(timezone.utc),
    )
    logger.info(
        "A lead maintenance task carries its schedule's owner permit. "
        "event=schedule_owner_permit_attached schedule_id=%s person_id=%s agent_id=%s token_id=%s",
        schedule_id,
        person_id,
        getattr(agent, "id", None),
        permit.token_id,
    )
    return True


def sign_system_run(agent: Any, meta: Any, *, schedule_id: str) -> bool:
    """Attach a system-work run's identity for no person from the schedule's stored permit.

    Clears person and session fields so system work never names a person. Returns True
    when a usable permit was attached, False otherwise (async callers with a claim use
    ``scheduled_run_contexts.sign_system_run_async`` to fall back to ``run_context.sign``).
    """

    gateway_context = getattr(meta, "gateway_context", None)
    if gateway_context is None:
        return False
    for name in PERSON_GATEWAY_FIELDS:
        setattr(gateway_context, name, None)
    gateway_context.task_context = None
    gateway_context.schedule_run = None
    return attach_run_permit(agent, meta, schedule_id=schedule_id, person_id=None)


__all__ = [
    "attach_owner_permit",
    "attach_run_permit",
    "sign_system_run",
]
