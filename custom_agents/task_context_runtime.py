"""Sign task contexts for work the manager starts on its own (scheduled runs).

The gateway signs contexts for people's requests. A schedule keeps firing long
after the request that created it, so each firing gets a fresh context signed
by the scheduler (``access_control.identity``, issuer ``scheduler``) for the
schedule's owner, taken from the authoritative schedule row.

Only the manager process holds the scheduler's signing key. An agent running
inside it (the team lead, and workers the manager starts on demand as
threads) signs its runs here. A worker running as its own process (started
from team composition) has no key. It gives the run its schedule's permit
instead, which the manager signed once for the schedule
(``custom_agents.schedule_permits``; every check reads the owner's current
roles and standing, ``access_control.task_identity``). Only when the schedule
has no usable permit does it ask the manager to sign the run it claimed
(``custom_agents.scheduled_run_contexts``), and the manager signs with
``issue_scheduled_context`` below, by the same rules. Permits follow the same
vouching rule (``gateway_vouches``).

The owner's roles are their current ones, read at every run from the gateway's
copy of the team's people (``access_control.people_directory``), so a role
change, a disable or a removal applies at the next run. A disabled or removed
owner's schedules do not run at all (``inactive_owner``). Roles are given only
when the owner is vouched for: the schedule is system-managed (trusted runtime
code sets its owner), or the context saved with it was signed by the gateway
for that same person (a person's schedule is saved from the task that made
it; the signature is checked, its expiry ignored). Otherwise the run carries
no roles, so a person field nobody vouched for gains no one's roles.

Runs of the runtime's own maintenance schedule for built-in system agents are
the exception: they are system work, signed for no person
(``sign_system_context``). When such a run escalates to the team lead, the
lead's task works for that schedule's stored owner (``sign_system_escalation``),
so maintenance findings reach a person.

A memory review the team lead sends right after a person's turn is not part
of that person's request, which has usually ended. It works for the person's
own stale-conversation review schedule, like that schedule's runs, which
review the same queued jobs (``sign_memory_review``).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from access_control.identity import (
    ISSUER_GATEWAY,
    ISSUER_SCHEDULER,
    ContextSigner,
    ContextVerifier,
    IdentityRejected,
)
from access_control.people_directory import PeopleDirectory, PersonNow
from custom_messages.contracts.access_v2 import PersonStanding, SignedTaskContextV1, TaskContextV1
from custom_messages.contracts.routing_v1 import PERSON_GATEWAY_FIELDS
from storage.access_repository import AccessRepository
from storage.config import StorageSettings

logger = logging.getLogger(__name__)

@lru_cache(maxsize=1)
def _scheduler_signer() -> ContextSigner | None:
    return ContextSigner.from_env(ISSUER_SCHEDULER)


@lru_cache(maxsize=1)
def _verifier() -> ContextVerifier:
    return ContextVerifier.from_env()


def scheduler_signer() -> ContextSigner | None:
    """This process's scheduler signer; only the manager process has one."""

    return _scheduler_signer()


def context_verifier() -> ContextVerifier:
    """This process's task-context verifier (public keys from ``ACCESS_CONTEXT_VERIFY_KEYS``)."""

    return _verifier()


def scheduler_contexts_verified() -> bool:
    """Whether this process trusts a scheduler key, so its scheduled runs need a scheduler-signed context."""

    return any(issuer == ISSUER_SCHEDULER for issuer, _key in _verifier().keys.values())


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


def gateway_vouches(gateway_context: Any, person_id: str) -> bool:
    """Whether the context saved with the schedule was signed by the gateway for this same person."""

    signed = getattr(gateway_context, "task_context", None)
    if signed is None:
        return False
    try:
        context = _verifier().verify(signed, check_expiry=False)
    except IdentityRejected as exc:
        logger.warning("Ignoring an unverifiable embedded task context. event=task_context_rejected reason=%s", exc)
        return False
    return context.issuer == ISSUER_GATEWAY and context.person_id == person_id


def run_owner(agent: Any, meta: Any) -> PersonNow | None:
    """The run's person as the gateway last recorded them, or None when the run names no person.

    Reads the access store, so async callers run it in a thread
    (``asyncio.to_thread``; the task operation runner deep-copies its
    arguments and cannot take the agent).
    """

    person_id = _run_person(meta)
    return people_directory(agent).person(person_id) if person_id else None


def scheduled_owner_roles(gateway_context: Any, owner: PersonNow, *, schedule_id: str, system_managed: bool) -> tuple[str, ...]:
    """The owner's current roles when the owner is vouched for (module docstring), else none (logged)."""

    if not (system_managed or gateway_vouches(gateway_context, owner.person_id)):
        logger.warning(
            "A scheduled run's person is not vouched for by the gateway, so the run carries no roles and its "
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


def _signer_for(schedule_id: str) -> ContextSigner | None:
    """The scheduler's signer; without one the run is unsigned and every tool call is refused, so say so."""

    signer = _scheduler_signer()
    if signer is None:
        logger.warning(
            "No scheduler task-context signing key; scheduled work carries no signed task context. "
            "event=task_context_signer_missing schedule_id=%s",
            schedule_id,
        )
    return signer


def issue_scheduled_context(
    signer: ContextSigner,
    gateway_context: Any,
    *,
    team_id: str,
    schedule_id: str,
    conversation_id: str | None,
    owner: PersonNow | None,
    system_managed: bool,
) -> SignedTaskContextV1:
    """A scheduler-signed context for one run of a schedule, for its owner (module docstring).

    ``gateway_context`` is the one saved with the schedule (it may vouch for
    the owner) and ``owner`` is ``run_owner`` for that schedule. Used both
    where the run is signed in this process (``sign_scheduled_context``) and
    where the manager signs for a worker process
    (``custom_agents.scheduled_run_contexts``).
    """

    roles = (
        scheduled_owner_roles(gateway_context, owner, schedule_id=schedule_id, system_managed=system_managed)
        if owner is not None
        else ()
    )
    return signer.issue(
        team_id=team_id,
        person_id=owner.person_id if owner is not None else None,
        person_roles=roles,
        conversation_id=conversation_id,
        schedule_id=schedule_id,
    )


def sign_scheduled_context(
    meta: Any, *, team_id: str, schedule_id: str, owner: PersonNow | None, system_managed: bool
) -> None:
    """Replace ``meta.gateway_context.task_context`` with a scheduler-signed one for the owner (module docstring).

    ``owner`` is ``run_owner`` for the same ``meta``. Signs in this process;
    a worker process without the key asks the manager instead
    (``custom_agents.scheduled_run_contexts.sign_scheduled_run``).
    """

    gateway_context = getattr(meta, "gateway_context", None)
    if gateway_context is None:
        return
    signer = _signer_for(schedule_id)
    # A run carries a signed context or a permit-backed run identity, never both.
    gateway_context.schedule_run = None
    gateway_context.task_context = (
        issue_scheduled_context(
            signer,
            gateway_context,
            team_id=team_id,
            schedule_id=schedule_id,
            conversation_id=getattr(meta, "conversation_id", None),
            owner=owner,
            system_managed=system_managed,
        )
        if signer is not None
        else None
    )


def sign_system_context(meta: Any, *, team_id: str, schedule_id: str) -> None:
    """Sign a run of a runtime-owned maintenance schedule as system work (no person).

    Used only where trusted code hands a maintenance run to a built-in system
    agent (InternalImprovementAgent's maintenance, access plan 7c); every other
    scheduled run works for its schedule's owner (``sign_scheduled_context``).
    With no person, the access hook lets only system agents act, on their
    duties, and the context names the schedule, so the run may record that
    schedule's run and nothing of anyone's schedules
    (``access_control.ownership.system_work_schedule_access``). The person
    and session fields are cleared too (``PERSON_GATEWAY_FIELDS``), so no part
    of the run names or reaches a person it does not work for (the broker
    refuses a request whose person differs from its signed context).
    """

    gateway_context = getattr(meta, "gateway_context", None)
    if gateway_context is None:
        return
    for name in PERSON_GATEWAY_FIELDS:
        setattr(gateway_context, name, None)
    signer = _signer_for(schedule_id)
    gateway_context.task_context = (
        signer.issue(
            team_id=team_id,
            person_id=None,
            conversation_id=getattr(meta, "conversation_id", None),
            schedule_id=schedule_id,
        )
        if signer is not None
        else None
    )


def _system_work_context(agent: Any, meta: Any) -> TaskContextV1 | None:
    """Who the escalation works for, checked like any task (a signed context or a
    scheduled run identity), when that is system work (no person); else None.

    An unverifiable identity is left alone: the access hook refuses it anyway.
    """

    from custom_agents.access_runtime import resolve_meta_identity

    context = resolve_meta_identity(agent, meta)
    return context if context is not None and context.person_id is None else None


def _refuse_escalation(reason: str, schedule_id: str | None, task_id: str) -> None:
    logger.warning(
        "An escalation from work for no person was not signed for a person; the team lead's task "
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
    """Who the team lead's task for an escalation from system work works for.

    A maintenance run is system work for no person (``sign_system_context``),
    and a team lead working for no person may call no tool, so its findings
    would reach nobody. When such a run escalates to the team lead
    (``manager_escalation``), the lead's task works instead for the owner
    stored on the system-managed schedule that the verified context names:
    the first user, an administrator. The scheduler signs it for that owner,
    with their current roles as for any scheduled run of a system-managed
    schedule (``scheduled_owner_roles``). Nothing the escalation itself says about a
    person is used. Runs only in the manager, which holds the scheduler key.

    Returns a re-signed copy of ``meta``, or ``meta`` unchanged when this does
    not apply or is refused (logged): the schedule is missing, not
    system-managed, has no owner, or there is no signing key.
    """

    gateway_context = getattr(meta, "gateway_context", None)
    if not getattr(agent, "is_team_lead", False) or getattr(meta, "manager_escalation", None) is None:
        return meta
    context = _system_work_context(agent, meta) if gateway_context is not None else None
    if context is None:
        return meta
    found = _system_schedule_owner(agent, context.schedule_id, task_id=task_id)
    signer = _signer_for(context.schedule_id or "") if found is not None else None
    if found is None or signer is None:
        return meta
    schedule, owner = found
    stored_context = getattr(schedule.payload_meta, "gateway_context", None)
    roles = scheduled_owner_roles(
        stored_context, people_directory(agent).person(owner), schedule_id=schedule.schedule_id, system_managed=True
    )
    signed = meta.model_copy(deep=True)
    # Where to reach the owner (their chat session) comes from the schedule the
    # gateway keeps refreshed (custom_agents.schedule_context_refresh), never
    # from the escalation.
    for name in PERSON_GATEWAY_FIELDS:
        setattr(signed.gateway_context, name, getattr(stored_context, name, None))
    signed.gateway_context.canonical_user_id = owner
    signed.gateway_context.task_context = signer.issue(
        team_id=context.team_id,
        person_id=owner,
        person_roles=roles,
        conversation_id=getattr(meta, "conversation_id", None),
        schedule_id=schedule.schedule_id,
    )
    logger.info(
        "An escalation from work for no person now works for the maintenance schedule's owner. "
        "event=system_escalation_signed_for_owner schedule_id=%s owner=%s task_id=%s source_task_id=%s roles=%s",
        schedule.schedule_id,
        owner,
        task_id,
        getattr(meta.manager_escalation, "source_task_id", None),
        ",".join(roles),
    )
    return signed


def sign_for_maintenance_owner(
    agent: Any, meta: Any, *, task_id: str, maintenance_schedule_id: str
) -> Any:
    """Make a team lead task the runtime opened for the team work for an administrator.

    Worker pool reviews (``custom_agents.worker_pool_reviews``) concern the whole
    team, not one person, but the team lead working for no person may call no
    tool and reach nobody. Like an escalation from maintenance work
    (``sign_system_escalation``), such a task works for the owner stored on the
    team's system-managed maintenance schedule (the first user, an
    administrator), signed by the scheduler with their current roles; the chat
    route comes from that schedule. Returns a signed copy, or ``meta`` unchanged
    (logged) when there is no such schedule, owner or signing key.

    The caller passes the schedule id
    (``internal_improvement_runtime.internal_improvement_schedule_id``):
    that module imports this one.
    """

    from custom_messages.contracts.routing_v1 import GatewayContext

    if not getattr(agent, "is_team_lead", False):
        return meta
    schedule_id = maintenance_schedule_id
    found = _system_schedule_owner(agent, schedule_id, task_id=task_id)
    signer = _signer_for(schedule_id) if found is not None else None
    if found is None or signer is None:
        return meta
    schedule, owner = found
    stored_context = getattr(schedule.payload_meta, "gateway_context", None)
    roles = scheduled_owner_roles(
        stored_context, people_directory(agent).person(owner), schedule_id=schedule_id, system_managed=True
    )
    signed = meta.model_copy(deep=True)
    if signed.gateway_context is None:
        signed.gateway_context = GatewayContext()
    for name in PERSON_GATEWAY_FIELDS:
        setattr(signed.gateway_context, name, getattr(stored_context, name, None))
    signed.gateway_context.canonical_user_id = owner
    signed.gateway_context.task_context = signer.issue(
        team_id=str(agent.team_id),
        person_id=owner,
        person_roles=roles,
        conversation_id=getattr(meta, "conversation_id", None),
        schedule_id=schedule_id,
    )
    logger.info(
        "A runtime task for the team now works for the maintenance schedule's owner. "
        "event=team_task_signed_for_maintenance_owner schedule_id=%s owner=%s task_id=%s roles=%s",
        schedule_id,
        owner,
        task_id,
        ",".join(roles),
    )
    return signed


def sign_memory_review(agent: Any, meta: Any, *, person_id: str, review_job_id: str) -> Any | None:
    """Make a memory review sent right after a turn work of the person's own conversation review schedule.

    After a person's turn the team lead sends MemReviewAgent a review of it
    (``strands_agent._publish_memory_review_task``). The review is not part of
    the person's request, which has usually ended by then: under the turn's
    own signed context the change ledger refuses its memory changes as
    ``request_closed``. It is the same work the person's stale-conversation
    review schedule does with queued jobs, so it works for that schedule's
    owner, like its runs: the scheduler signs it for them with their current
    roles, and the chat route comes from that schedule (as for
    ``sign_system_escalation``). ``person_id`` is the job's person, whom the
    turn's verified context named when the job was saved
    (``memory_review_runtime.enqueue_conversation_window_review_job``).

    Returns a signed copy of ``meta``, or None (logged) when the person has no
    such schedule, is disabled or removed, or there is no signing key. The job
    then stays queued for that schedule's next run, which reviews it.
    """

    from custom_messages.contracts.routing_v1 import GatewayContext

    schedule = _conversation_review_schedule(agent, person_id, review_job_id=review_job_id)
    if schedule is None:
        return None
    owner = people_directory(agent).person(person_id)
    if inactive_owner(owner, schedule_id=schedule.schedule_id):
        return None
    signer = _signer_for(schedule.schedule_id)
    if signer is None:
        return None
    stored_context = getattr(schedule.payload_meta, "gateway_context", None)
    signed = meta.model_copy(deep=True)
    if signed.gateway_context is None:
        signed.gateway_context = GatewayContext()
    for name in PERSON_GATEWAY_FIELDS:
        setattr(signed.gateway_context, name, getattr(stored_context, name, None))
    signed.gateway_context.canonical_user_id = person_id
    signed.gateway_context.schedule_run = None
    signed.gateway_context.task_context = issue_scheduled_context(
        signer,
        stored_context,
        team_id=str(agent.team_id),
        schedule_id=schedule.schedule_id,
        conversation_id=getattr(meta, "conversation_id", None),
        owner=owner,
        system_managed=True,
    )
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
    "issue_scheduled_context",
    "people_directory",
    "run_owner",
    "scheduler_contexts_verified",
    "scheduler_signer",
    "sign_for_maintenance_owner",
    "sign_memory_review",
    "sign_scheduled_context",
    "sign_system_context",
    "sign_system_escalation",
]
