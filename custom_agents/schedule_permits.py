"""One manager-signed permit per schedule, so workers start runs without asking the manager.

Every scheduled run must say whom it works for, or the access checks refuse
every tool call. Only the manager process holds the scheduler's signing key
(``docker/secret_env_vars.yaml``), and workers started from team composition
run as their own processes without it. Asking the manager to sign each run
(``custom_agents.scheduled_run_contexts``) puts the manager on the path of
every run of every worker, which does not scale. So (owner's decision
2026-09-27) the manager signs one permit per schedule, and workers never
contact it for a normal run:

- Issuing (manager only, ``renew_schedule_permits``): the team lead's
  scheduler pass, in the process that holds the key, looks for turned-on
  schedules that were never checked, whose permit expires within
  ``RENEW_WITHIN`` (7 days), or whose owner changed, with one query of at most
  ``PASS_LIMIT`` rows, at most every ``PASS_INTERVAL_SECONDS``. A person's
  schedule gets a permit only when that person is vouched for, by the same
  rule as a locally signed run (``task_context_runtime``): the schedule is
  runtime-managed, or the context saved with it was signed by the gateway for
  that same person. A person who is not active gets none. The runtime's own
  maintenance schedule for InternalImprovementAgent gets a permit for no
  person (system work). A refusal is recorded too, and looked at again after
  ``RECHECK_REFUSED_AFTER`` or when the owner changes, so the pass does not
  repeat the same work. The permit is stored on the schedule row
  (``run_permit_json``), never in the schedule itself, and setting it needs
  no schedule permit: it is bookkeeping, like the next run time.
- Running (``attach_run_permit``): an agent without the key that claimed a
  schedule reads the row's permit, checks its signature, schedule and person,
  and puts it with the run's own fields (a fresh context id, the conversation,
  the start time) in ``GatewayContext.schedule_run``. Delegated tasks copy it.
  Only when the row has no usable permit (a new schedule the manager has not
  looked at yet, an unvouched owner) does the worker ask the manager as before.
- Checking: every enforcement point checks the run identity with
  ``access_control.task_identity.resolve_task_identity``, which reads the
  schedule and the person live (cached 30 seconds), so a role change, a
  disable or removal, or a deleted or turned-off schedule applies to the next
  check without waiting for the permit to expire.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from access_control.identity import ContextSigner, IdentityRejected
from custom_agents.schedule_management import effective_system_schedule_kind, is_system_managed_schedule
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from custom_agents.task_context_runtime import (
    context_verifier,
    gateway_vouches,
    people_directory,
    scheduler_signer,
    sign_system_context,
)
from custom_messages.contracts.access_v2 import (
    SCHEDULE_RUN_ID_PREFIX,
    ScheduleRunIdentityV1,
    SignedSchedulePermitV1,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.routing_v1 import PERSON_GATEWAY_FIELDS
from custom_messages.system_schedules import is_system_work_kind
from id_generation import new_id

logger = logging.getLogger(__name__)

RENEW_WITHIN = timedelta(days=7)
RECHECK_REFUSED_AFTER = timedelta(hours=1)
PASS_INTERVAL_SECONDS = 30.0
PASS_LIMIT = 200

REFUSED_NOT_FOUND = "schedule_not_found"
REFUSED_NO_OWNER = "no_owner"
REFUSED_OWNER_NOT_ACTIVE = "owner_not_active"
REFUSED_NOT_VOUCHED = "owner_not_vouched"


@dataclass
class PermitPassResult:
    """What one issuing pass did, by schedule id."""

    issued: list[str] = field(default_factory=list)
    renewed: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)


# -- the manager: issue and renew -----------------------------------------------------


def _is_system_work(schedule: ScheduledMessage) -> bool:
    return is_system_work_kind(effective_system_schedule_kind(schedule))


def _permit_person(agent: Any, schedule: ScheduledMessage, owner: str | None) -> tuple[str | None, str | None]:
    """Whom the schedule's permit names, or why it gets none: (person id, refusal reason)."""

    if _is_system_work(schedule):
        return None, None
    if not owner:
        return None, REFUSED_NO_OWNER
    if not people_directory(agent).person(owner).active:
        return None, REFUSED_OWNER_NOT_ACTIVE
    gateway_context = getattr(schedule.payload_meta, "gateway_context", None)
    if not (is_system_managed_schedule(schedule) or gateway_vouches(gateway_context, owner)):
        return None, REFUSED_NOT_VOUCHED
    return owner, None


def _log_refusal(schedule_id: str, owner: str | None, reason: str) -> None:
    logger.info(
        "The manager issued no run permit for a schedule, so a worker without the scheduler key asks the "
        "manager for each of its runs. event=schedule_permit_refused schedule_id=%s owner=%s reason=%s",
        schedule_id,
        owner,
        reason,
    )


def _check_one(
    agent: Any, signer: ContextSigner, schedule_id: str, result: PermitPassResult, now: datetime
) -> None:
    """Issue, renew or refuse the permit of one schedule, and record the outcome on its row."""

    store = agent._schedule_store
    schedule = store.get_scheduled_message(schedule_id)
    if schedule is None:
        result.refused[schedule_id] = REFUSED_NOT_FOUND
        return
    # The owner comes from the authoritative schedule row (applied on load).
    owner = owner_canonical_user_id_for_schedule(schedule)
    person, refusal = _permit_person(agent, schedule, owner)
    if refusal is not None:
        store.set_run_permit(schedule_id, permit=None, checked_owner=owner, recheck_at=now + RECHECK_REFUSED_AFTER)
        result.refused[schedule_id] = refusal
        _log_refusal(schedule_id, owner, refusal)
        return
    renewing = store.run_permit(schedule_id) is not None
    permit = signer.issue_permit(team_id=agent.team_id, schedule_id=schedule_id, person_id=person, now=now)
    if not store.set_run_permit(
        schedule_id, permit=permit, checked_owner=owner, recheck_at=permit.permit.expires_at_utc - RENEW_WITHIN
    ):
        return  # the owner changed or the row went away meanwhile; the next pass sees it
    (result.renewed if renewing else result.issued).append(schedule_id)
    logger.info(
        "Issued a schedule's run permit. event=%s schedule_id=%s person_id=%s permit_id=%s expires_at=%s",
        "schedule_permit_renewed" if renewing else "schedule_permit_issued",
        schedule_id,
        person,
        permit.permit.permit_id,
        permit.permit.expires_at_utc.isoformat(),
    )


def renew_schedule_permits(
    agent: Any, *, signer: ContextSigner | None = None, now: datetime | None = None, limit: int = PASS_LIMIT
) -> PermitPassResult:
    """One issuing pass (module docstring); synchronous, so async callers run it in a thread.

    ``signer`` defaults to this process's scheduler signer; without one
    nothing is issued. A failure for one schedule is logged and the pass goes on.
    """

    signer = signer or scheduler_signer()
    result = PermitPassResult()
    if signer is None:
        return result
    moment = now or datetime.now(timezone.utc)
    for schedule_id in agent._schedule_store.rows_needing_run_permit(now=moment, limit=limit):
        try:
            _check_one(agent, signer, schedule_id, result, moment)
        except Exception:
            logger.exception(
                "Could not check a schedule's run permit; the next pass tries again. "
                "event=schedule_permit_check_failed schedule_id=%s",
                schedule_id,
            )
    return result


def issues_permits(agent: Any) -> bool:
    """Whether this agent issues run permits: the team lead, in the process that holds the scheduler key."""

    return bool(getattr(agent, "is_team_lead", False)) and scheduler_signer() is not None


async def maybe_renew_schedule_permits(agent: Any) -> None:
    """Run an issuing pass from the scheduler loop, at most every ``PASS_INTERVAL_SECONDS``."""

    if not issues_permits(agent):
        return
    last = getattr(agent, "_schedule_permit_pass_at", None)
    now = time.monotonic()
    if last is not None and now - last < PASS_INTERVAL_SECONDS:
        return
    agent._schedule_permit_pass_at = now
    try:
        await asyncio.to_thread(renew_schedule_permits, agent)
    except Exception:  # noqa: BLE001 - a failed pass must not stop schedules from firing
        logger.exception(
            "Could not look for schedules that need a run permit; the next pass tries again. "
            "event=schedule_permit_pass_failed agent_id=%s",
            getattr(agent, "id", None),
        )


# -- the claiming agent: attach the permit to a run -------------------------------------


def _schedule_store(agent: Any) -> Any:
    """The agent's schedule store, opened on first use when the agent opens it lazily; None without one."""

    store = getattr(agent, "_schedule_store", None)
    if store is None and callable(getattr(agent, "_ensure_schedule_store", None)):
        store = agent._ensure_schedule_store()
    return store


def _usable_permit(agent: Any, schedule_id: str, person_id: str | None) -> SignedSchedulePermitV1 | None:
    """The row's permit when it verifies and names this schedule and person, else None (logged)."""

    store = _schedule_store(agent)
    permit: SignedSchedulePermitV1 | None = store.run_permit(schedule_id) if store is not None else None
    if permit is None:
        return None
    try:
        verified = context_verifier().verify_permit(permit, team_id=agent.team_id)
    except (IdentityRejected, ValueError) as exc:
        reason = str(exc)
    else:
        if verified.schedule_id == schedule_id and verified.person_id == person_id:
            return permit
        reason = "the permit names another schedule or person"
    logger.warning(
        "A schedule's stored run permit cannot be used for this run; asking the manager instead. "
        "event=schedule_permit_unusable schedule_id=%s person_id=%s reason=%s",
        schedule_id,
        person_id,
        reason,
    )
    return None


def attach_run_permit(agent: Any, meta: Any, *, schedule_id: str, person_id: str | None) -> bool:
    """Give a run its identity from the schedule's permit (module docstring); False when there is none.

    ``schedule_id`` is the claimed schedule row and ``person_id`` whom the run
    works for (the row's owner, or None for system work). Reads storage, so
    async callers run it in a thread.
    """

    gateway_context = getattr(meta, "gateway_context", None)
    if gateway_context is None:
        return False
    permit = _usable_permit(agent, schedule_id, person_id)
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
        "A scheduled run carries its schedule's permit; the manager was not asked. "
        "event=scheduled_run_permit_attached schedule_id=%s person_id=%s agent_id=%s permit_id=%s",
        schedule_id,
        person_id,
        getattr(agent, "id", None),
        permit.permit.permit_id,
    )
    return True


def sign_system_run(agent: Any, meta: Any, *, schedule_id: str) -> None:
    """A maintenance run's identity as system work for no person: signed here, or else the schedule's permit.

    Without the key and without a usable permit, ``sign_system_context``
    leaves the run unsigned and says so; every tool call is then refused.
    Reads storage, so async callers run it in a thread.
    """

    gateway_context = getattr(meta, "gateway_context", None)
    if scheduler_signer() is None and gateway_context is not None:
        for name in PERSON_GATEWAY_FIELDS:
            setattr(gateway_context, name, None)
        if attach_run_permit(agent, meta, schedule_id=schedule_id, person_id=None):
            return
    sign_system_context(meta, team_id=str(getattr(agent, "team_id", "") or ""), schedule_id=schedule_id)


__all__ = [
    "PASS_INTERVAL_SECONDS",
    "PASS_LIMIT",
    "RECHECK_REFUSED_AFTER",
    "RENEW_WITHIN",
    "PermitPassResult",
    "attach_run_permit",
    "issues_permits",
    "maybe_renew_schedule_permits",
    "renew_schedule_permits",
    "sign_system_run",
]
