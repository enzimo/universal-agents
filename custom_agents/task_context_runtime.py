"""Protocol 1 task identity helpers for scheduled runs and lead maintenance tasks.

No model-running process (neither the team lead nor any worker) holds a signing key
(Decision 58). ``team_services.scheduler`` holds the ``scheduler`` role in its host
certificate, maintains standing protocol 1 permits (``run_permit_json`` and
``owner_permit_json``) on schedule rows, and answers ``run_context.sign`` on
``svc.scheduler`` when no usable permit is stored yet.

This module holds:
- ``context_verifier``: the process's live ``AuthorityTokenVerifier``;
- ``people_directory``, ``run_owner``, ``inactive_owner``, and ``gateway_vouches``;
- the three special lead-process paths (``sign_system_escalation``,
  ``sign_for_maintenance_owner``, ``sign_memory_review``), which wrap the schedule's
  scheduler-signed standing permit in ``ScheduleRunIdentityV1`` with a fresh
  ``schedule_run_<hex>`` request id per task instead of signing locally.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from access_control.authority_verifier import (
    AuthorityTokenVerifier,
    IdentityRejected,
    get_process_verifier,
)
from access_control.people_directory import PeopleDirectory, PersonNow
from custom_messages.contracts.access_v2 import PersonStanding, TaskContextV1
from custom_messages.contracts.routing_v1 import PERSON_GATEWAY_FIELDS
from storage.access_repository import AccessRepository
from storage.config import StorageSettings

logger = logging.getLogger(__name__)


def context_verifier(agent: Any = None) -> AuthorityTokenVerifier:
    """This process's protocol 1 authority-token verifier (backed by the live host trust store)."""

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


@lru_cache(maxsize=4)
def _access_store(storage: StorageSettings) -> AccessRepository:
    return AccessRepository(storage)


def people_directory(agent: Any) -> PeopleDirectory:
    """The gateway's copy of the team's people, from the manager's side."""

    storage = getattr(getattr(agent, "task_manager", None), "storage_settings", None)
    if storage is None:
        raise RuntimeError("The agent has no task storage, so it cannot read the team's people.")
    return PeopleDirectory(_access_store(storage), str(getattr(agent, "team_id", "") or ""))


def _run_person(meta: Any) -> str | None:
    gateway_context = getattr(meta, "gateway_context", None)
    return str(getattr(gateway_context, "canonical_user_id", "") or "").strip() or None


def gateway_vouches(
    gateway_context: Any,
    person_id: str,
    *,
    team_id: str = "",
    agent: Any = None,
    verifier: AuthorityTokenVerifier | None = None,
) -> bool:
    """Whether the token saved with the schedule was signed by the gateway for this same person."""

    signed = getattr(gateway_context, "task_context", None)
    if signed is None:
        return False
    effective_team = team_id or str(getattr(agent, "team_id", "") or "")
    if not effective_team:
        audience = signed.get("audience") if isinstance(signed, dict) else getattr(signed, "audience", None)
        effective_team = str(
            (audience.get("team") if isinstance(audience, dict) else getattr(audience, "team", "")) or ""
        )
    if not effective_team:
        return False
    active_verifier = verifier if verifier is not None else context_verifier(agent)
    try:
        verified = active_verifier.verify_root_task(
            signed,
            team_id=effective_team,
            require_issuer="gateway",
            check_expiry=False,
            require_active_person=False,
        )
    except (IdentityRejected, ValueError) as exc:
        logger.warning("Ignoring an unverifiable embedded authority token. event=task_context_rejected reason=%s", exc)
        return False
    return verified.is_gateway_issued and verified.person_id == person_id


def run_owner(agent: Any, meta: Any) -> PersonNow | None:
    """The run's person as the gateway last recorded them, or None when the run names no person.

    Reads the access store, so async callers run it in a thread
    (``asyncio.to_thread``; the task operation runner deep-copies its
    arguments and cannot take the agent).
    """

    person_id = _run_person(meta)
    return people_directory(agent).person(person_id) if person_id else None


def inactive_owner(owner: PersonNow | None, *, schedule_id: str) -> bool:
    """Whether the run's person is disabled or removed; such a run must not start (logged).

    Disabling or removing a person deletes their schedules
    (``custom_agents.person_departure_runtime``); this check stops a run that
    fires before that happened. This applies with authorization on and off:
    it is about the person, not about access checks.
    """

    if owner is None or owner.standing not in (PersonStanding.DISABLED, PersonStanding.REMOVED):
        return False
    logger.info(
        "Skipped a scheduled run because its person is %s. event=scheduled_run_skipped_owner_inactive "
        "schedule_id=%s person_id=%s",
        owner.describe(),
        schedule_id,
        owner.person_id,
    )
    return True


def scheduled_owner_roles(
    gateway_context: Any,
    owner: PersonNow,
    *,
    schedule_id: str,
    system_managed: bool,
    agent: Any = None,
    owner_vouched: bool = False,
) -> tuple[str, ...]:
    """The owner's current roles when the owner is vouched for, else none (logged)."""

    if not (system_managed or owner_vouched):
        logger.warning(
            "A scheduled run's person is not vouched for, so the run carries no roles and its "
            "access checks refuse most tools. event=scheduled_owner_not_vouched schedule_id=%s person_id=%s",
            schedule_id,
            owner.person_id,
        )
        return ()
    if not owner.active:
        logger.warning(
            "A scheduled run's person has no current roles, so its access checks refuse most tools. "
            "event=scheduled_owner_roles_unavailable schedule_id=%s person_id=%s standing=%s",
            schedule_id,
            owner.person_id,
            owner.describe(),
        )
        return ()
    return owner.roles


def _system_work_context(agent: Any, meta: Any) -> TaskContextV1 | None:
    """Who the escalation works for, checked like any task, when that is system work (no person); else None.

    An unverifiable identity is left alone: the access hook refuses it anyway.
    """

    from custom_agents.access_runtime import resolve_meta_identity

    context = resolve_meta_identity(agent, meta)
    return context if context is not None and context.person_id is None else None


def _refuse_escalation(reason: str, schedule_id: str | None, task_id: str) -> None:
    logger.warning(
        "An escalation from work for no person was not given its owner's permit; the team lead's task "
        "stays work for no person and its tool calls are refused. event=system_escalation_refused "
        "reason=%s schedule_id=%s task_id=%s",
        reason,
        schedule_id,
        task_id,
    )


def _system_schedule_owner(agent: Any, schedule_id: str | None, *, task_id: str) -> tuple[Any, str] | None:
    """The system-managed schedule the verified context names and its stored owner, or None (logged)."""

    from custom_agents.schedule_management import is_system_managed_schedule
    from custom_agents.schedule_store import owner_canonical_user_id_for_schedule

    store = getattr(agent, "_schedule_store", None)
    schedule = store.get_scheduled_message(schedule_id) if store is not None and schedule_id else None
    # The owner comes from the authoritative schedule row (applied on load).
    owner = owner_canonical_user_id_for_schedule(schedule) if schedule is not None else None
    if schedule is None:
        _refuse_escalation("schedule_not_found", schedule_id, task_id)
    elif not is_system_managed_schedule(schedule):
        _refuse_escalation("schedule_not_system_managed", schedule_id, task_id)
    elif not owner:
        _refuse_escalation("schedule_owner_missing", schedule_id, task_id)
    else:
        return schedule, owner
    return None


def sign_system_escalation(agent: Any, meta: Any, *, task_id: str) -> Any:
    """Give a team lead task for an escalation from system work its maintenance schedule's owner permit.

    A maintenance run is system work for no person, and a team lead working for no
    person may call no tool, so its findings would reach nobody. When such a run
    escalates to the team lead (``manager_escalation``), the lead's task carries
    the scheduler-signed ``owner_permit_json`` from the system-managed schedule
    wrapped in ``ScheduleRunIdentityV1`` with a fresh ``schedule_run_<hex>`` id.
    Nothing the escalation itself says about a person is used, and the lead holds
    no signing key.

    Returns a copy of ``meta`` carrying the owner permit, or ``meta`` unchanged
    when this does not apply or is refused (logged).
    """

    from custom_agents.schedule_permits import attach_owner_permit

    gateway_context = getattr(meta, "gateway_context", None)
    if not getattr(agent, "is_team_lead", False) or getattr(meta, "manager_escalation", None) is None:
        return meta
    context = _system_work_context(agent, meta) if gateway_context is not None else None
    if context is None:
        return meta
    found = _system_schedule_owner(agent, context.schedule_id, task_id=task_id)
    if found is None:
        return meta
    schedule, owner = found
    owner_now = people_directory(agent).person(owner)
    if not owner_now.active:
        _refuse_escalation("owner_not_active", schedule.schedule_id, task_id)
        return meta
    stored_context = getattr(schedule.payload_meta, "gateway_context", None)
    signed = meta.model_copy(deep=True)
    # Where to reach the owner (their chat session) comes from the schedule the
    # gateway keeps refreshed (custom_agents.schedule_context_refresh), never
    # from the escalation.
    for name in PERSON_GATEWAY_FIELDS:
        setattr(signed.gateway_context, name, getattr(stored_context, name, None))
    signed.gateway_context.canonical_user_id = owner
    if not attach_owner_permit(agent, signed, schedule_id=schedule.schedule_id, person_id=owner):
        _refuse_escalation("owner_permit_missing", schedule.schedule_id, task_id)
        return meta
    logger.info(
        "An escalation from work for no person now works for the maintenance schedule's owner. "
        "event=system_escalation_signed_for_owner schedule_id=%s owner=%s task_id=%s source_task_id=%s roles=%s",
        schedule.schedule_id,
        owner,
        task_id,
        getattr(meta.manager_escalation, "source_task_id", None),
        ",".join(owner_now.roles),
    )
    return signed


def sign_for_maintenance_owner(
    agent: Any, meta: Any, *, task_id: str, maintenance_schedule_id: str
) -> Any:
    """Make a team lead task the runtime opened for the team work for the maintenance schedule's owner.

    Worker pool reviews (``custom_agents.worker_pool_reviews``) concern the whole
    team, not one person, but the team lead working for no person may call no
    tool and reach nobody. Like ``sign_system_escalation``, this wraps the
    scheduler-signed ``owner_permit_json`` from the maintenance schedule in
    ``ScheduleRunIdentityV1`` without holding a signing key in the lead.
    """

    from custom_agents.schedule_permits import attach_owner_permit
    from custom_messages.contracts.routing_v1 import GatewayContext

    if not getattr(agent, "is_team_lead", False):
        return meta
    schedule_id = maintenance_schedule_id
    found = _system_schedule_owner(agent, schedule_id, task_id=task_id)
    if found is None:
        return meta
    schedule, owner = found
    owner_now = people_directory(agent).person(owner)
    if not owner_now.active:
        _refuse_escalation("owner_not_active", schedule_id, task_id)
        return meta
    stored_context = getattr(schedule.payload_meta, "gateway_context", None)
    signed = meta.model_copy(deep=True)
    if signed.gateway_context is None:
        signed.gateway_context = GatewayContext()
    for name in PERSON_GATEWAY_FIELDS:
        setattr(signed.gateway_context, name, getattr(stored_context, name, None))
    signed.gateway_context.canonical_user_id = owner
    if not attach_owner_permit(agent, signed, schedule_id=schedule_id, person_id=owner):
        _refuse_escalation("owner_permit_missing", schedule_id, task_id)
        return meta
    logger.info(
        "A runtime task for the team now works for the maintenance schedule's owner. "
        "event=team_task_signed_for_maintenance_owner schedule_id=%s owner=%s task_id=%s roles=%s",
        schedule_id,
        owner,
        task_id,
        ",".join(owner_now.roles),
    )
    return signed


def sign_memory_review(agent: Any, meta: Any, *, person_id: str, review_job_id: str) -> Any | None:
    """Give a post-turn memory review task the person's conversation review schedule permit.

    After a person's turn the team lead sends MemReviewAgent a review of it
    (``strands_agent._publish_memory_review_task``). Rather than holding a signing
    key in the lead, this wraps the scheduler-signed ``run_permit_json`` from the
    person's stale-conversation review schedule in ``ScheduleRunIdentityV1`` with a
    fresh ``schedule_run_<hex>`` id per task.

    Returns a copy of ``meta`` carrying the schedule permit, or None (logged) when
    the person has no such schedule, is disabled/removed, or the schedule has no
    usable permit yet (the job then stays queued for that schedule's next run).
    """

    from custom_agents.schedule_permits import attach_run_permit
    from custom_messages.contracts.routing_v1 import GatewayContext

    schedule = _conversation_review_schedule(agent, person_id, review_job_id=review_job_id)
    if schedule is None:
        return None
    owner = people_directory(agent).person(person_id)
    if inactive_owner(owner, schedule_id=schedule.schedule_id):
        return None
    stored_context = getattr(schedule.payload_meta, "gateway_context", None)
    signed = meta.model_copy(deep=True)
    if signed.gateway_context is None:
        signed.gateway_context = GatewayContext()
    for name in PERSON_GATEWAY_FIELDS:
        setattr(signed.gateway_context, name, getattr(stored_context, name, None))
    signed.gateway_context.canonical_user_id = person_id
    if not attach_run_permit(agent, signed, schedule_id=schedule.schedule_id, person_id=person_id):
        logger.info(
            "A memory review sent after a turn has no usable schedule permit yet, so it waits for the "
            "person's next conversation review run. event=memory_review_not_signed reason=run_permit_missing "
            "schedule_id=%s person_id=%s review_job_id=%s",
            schedule.schedule_id,
            person_id,
            review_job_id,
        )
        return None
    logger.info(
        "A memory review sent after a turn works for the person's conversation review schedule. "
        "event=memory_review_signed_for_schedule schedule_id=%s person_id=%s review_job_id=%s",
        schedule.schedule_id,
        person_id,
        review_job_id,
    )
    return signed


def _conversation_review_schedule(agent: Any, person_id: str, *, review_job_id: str) -> Any | None:
    """The person's own stale-conversation review schedule, read from the schedule store, or None (logged)."""

    store = getattr(agent, "_schedule_store", None)
    reason, schedule = "schedule_store_missing", None
    if store is not None:
        try:
            schedule = _find_conversation_review(store, person_id)
            reason = "schedule_not_found"
        except Exception as exc:  # noqa: BLE001 - the job waits for the schedule's next run instead
            reason = f"schedule_store_unreadable:{type(exc).__name__}"
    if schedule is None:
        logger.info(
            "A memory review sent after a turn was not signed, so it waits for the person's next conversation "
            "review run. event=memory_review_not_signed reason=%s person_id=%s review_job_id=%s",
            reason,
            person_id,
            review_job_id,
        )
    return schedule


def _find_conversation_review(store: Any, person_id: str) -> Any | None:
    """The system-managed schedule named as a stale-conversation review whose stored owner is the person."""

    from custom_agents.schedule_management import is_system_managed_schedule
    from custom_messages.system_schedules import STALE_CONVERSATION_SCHEDULE_NAME_PREFIX

    for row in store.all_rows():
        name = str(row.get("schedule_name") or "")
        if not name.startswith(STALE_CONVERSATION_SCHEDULE_NAME_PREFIX) or row.get("owner_canonical_user_id") != person_id:
            continue
        schedule = store.get_scheduled_message(str(row["schedule_id"]))
        if schedule is not None and is_system_managed_schedule(schedule):
            return schedule
    return None


__all__ = [
    "context_verifier",
    "gateway_vouches",
    "inactive_owner",
    "people_directory",
    "run_owner",
    "scheduled_owner_roles",
    "sign_for_maintenance_owner",
    "sign_memory_review",
    "sign_system_escalation",
]
