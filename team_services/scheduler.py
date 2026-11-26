"""The scheduler role inside team services: ``run_context.sign`` on ``svc.scheduler`` and schedule permit upkeep.

Decision 58 forbids any model-running process (the team lead or workers) from holding a
signing key. The ``team-services`` process holds the ``scheduler`` role in its host
certificate and serves two duties:

1. **Standing schedule permits (``renew_permits`` / ``issue_permit_for_schedule``):**
   Maintains one scheduler-signed protocol 1 permit token (``run_permit_json``) per active
   vouched or system-work schedule (lifetime capped at ``AUTHORITY_TOKEN_MAX_SECONDS``,
   renewed when less than ``RENEW_WITHIN`` remains or when the owner changes). System-work
   schedules that also name a vouched administrator owner (such as ``internal_improvement``)
   also receive ``owner_permit_json`` so the team lead can open maintenance escalations and
   worker pool reviews for that owner without holding a signing key.
2. **Per-run signing (``run_context.sign`` on ``svc.scheduler``):**
   Whenever a claiming agent (lead or worker) has no usable stored permit — a new schedule
   before permit upkeep, a heartbeat item targeting ``heartbeat_item_id``, an expired or
   mismatched permit — it asks ``svc.scheduler`` with its live ``claim_token``. The
   scheduler checks the claim against ``verified.frame.issuer.agent`` (cryptographically
   proven by the agent's session, refusing others as ``claim_not_held``), verifies the
   schedule is not stopped, checks heartbeat targets, verifies the owner is active and
   vouched (or marks ``system_work: true`` for system-work schedules), and returns a fresh
   scheduler-signed protocol 1 root-task ``AuthorityToken``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any, Callable, Optional
from uuid import uuid4

from access_control.authority_verifier import AuthorityTokenVerifier
from access_control.people_directory import PeopleDirectory, PersonNow
from custom_agents.heartbeat_store import HeartbeatStore, is_heartbeat_launcher
from custom_agents.schedule_management import effective_system_schedule_kind, is_system_managed_schedule
from custom_agents.schedule_store import (
    ScheduleStore,
    owner_canonical_user_id_for_schedule,
    schedule_stopped_reason,
)
from custom_agents.task_context_runtime import gateway_vouches, inactive_owner
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.system_schedules import is_system_work_kind
from custom_messages.wire.authority_bindings import AuthorityTokenRecord, SCHEDULER_ISSUER_ROLE
from custom_messages.wire.frame import VerifiedFrame
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.authority_tokens import root_task_token, schedule_permit_token
from custom_transport.protocol1.identity import HostIdentity
from team_services.service import OperationFamily

SCHEDULER_ADDRESS = "svc.scheduler"
SCHEDULER_KINDS = ("run_context.sign",)

# Protocol 1 authority tokens last at most 7 days (AUTHORITY_TOKEN_MAX_SECONDS);
# renew standing permits when fewer than 2 days remain.
RENEW_WITHIN = timedelta(days=2)
RECHECK_REFUSED_AFTER = timedelta(hours=1)
PASS_LIMIT = 200

REFUSED_NOT_FOUND = "schedule_not_found"
REFUSED_NO_OWNER = "no_owner"
REFUSED_OWNER_NOT_ACTIVE = "owner_not_active"
REFUSED_NOT_VOUCHED = "owner_not_vouched"
REFUSED_NO_PERSON_REF = "owner_person_ref_missing"


@dataclass
class PermitPassResult:
    """What one permit upkeep pass did, by schedule id."""

    issued: list[str] = field(default_factory=list)
    renewed: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)


def _is_system_work(schedule: ScheduledMessage) -> bool:
    return is_system_work_kind(effective_system_schedule_kind(schedule))


def _has_scheduler_role(identity: Optional[HostIdentity]) -> bool:
    return identity is not None and SCHEDULER_ISSUER_ROLE in identity.certificate.roles


def _person_ref_of(people: PeopleDirectory, person_id: str) -> Optional[str]:
    record = people.store.team_person(people.team_id, person_id)
    if record is None:
        return None
    ref = str(getattr(record, "person_ref", "") or "").strip()
    return ref or None


def is_schedule_vouched(
    schedule_id: str,
    schedule: ScheduledMessage,
    owner_id: str,
    *,
    store: ScheduleStore,
    team_id: str,
    verifier: Optional[AuthorityTokenVerifier] = None,
) -> bool:
    """Whether ``owner_id`` on ``schedule`` is vouched for by trusted installation state.

    True when the schedule row carries ``owner_vouched = 1`` (from ``schedules.install``,
    vouched import, or upgrade conversion), when the schedule is runtime/system-managed,
    or when its stored ``payload_meta.gateway_context.task_context`` is a verified
    gateway-signed authority token for ``owner_id``.
    """

    if store.is_owner_vouched(schedule_id):
        return True
    if is_system_managed_schedule(schedule):
        return True
    gateway_context = getattr(schedule.payload_meta, "gateway_context", None)
    return gateway_vouches(gateway_context, owner_id, team_id=team_id, verifier=verifier)


class SchedulerOperations:
    """Answers ``run_context.sign`` on ``svc.scheduler`` and maintains schedule permits."""

    def __init__(
        self,
        store: ScheduleStore,
        people: PeopleDirectory,
        team_id: str,
        *,
        identity_fn: Callable[[], Optional[HostIdentity]],
        verifier_fn: Callable[[], Optional[AuthorityTokenVerifier]],
        heartbeat_store_fn: Callable[[], HeartbeatStore],
        clock: Callable[[], datetime],
        logger: logging.Logger,
    ) -> None:
        self._store = store
        self._people = people
        self._team_id = team_id
        self._identity_fn = identity_fn
        self._verifier_fn = verifier_fn
        self._heartbeat_store_fn = heartbeat_store_fn
        self._clock = clock
        self._logger = logger

    def family(self) -> OperationFamily:
        return OperationFamily(
            handlers={"run_context.sign": self.handle},
            side_by_side=frozenset(SCHEDULER_KINDS),
            upkeep=self.maintain_permits,
            prepare=self.maintain_permits,
            address=SCHEDULER_ADDRESS,
        )

    async def maintain_permits(self) -> None:
        """Issue or renew standing schedule permits for active schedules."""

        await asyncio.to_thread(self.renew_permits)

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        cancelled = Event()
        running = asyncio.create_task(asyncio.to_thread(self.answer, verified))
        cancellation = None
        while not running.done():
            try:
                await asyncio.shield(running)
            except asyncio.CancelledError as error:
                cancelled.set()
                cancellation = error
        try:
            return running.result()
        finally:
            if cancellation is not None:
                raise cancellation

    def answer(self, verified: VerifiedFrame) -> dict[str, Any]:
        """Answer one ``run_context.sign`` request frame (contract: run_context.sign.reply)."""

        body = verified.raw["body"]
        request_id = str(body["request_id"])
        schedule_id = str(body["schedule_id"])
        claim_token = str(body["claim_token"])
        scheduled_job_id = str(body["scheduled_job_id"])
        heartbeat_item_id = body.get("heartbeat_item_id")
        asking_agent = verified.frame.issuer.agent
        now = self._clock().astimezone(timezone.utc)

        try:
            reply = self._sign_or_refuse(
                request_id=request_id,
                schedule_id=schedule_id,
                claim_token=claim_token,
                scheduled_job_id=scheduled_job_id,
                heartbeat_item_id=str(heartbeat_item_id) if heartbeat_item_id else None,
                asking_agent=str(asking_agent) if asking_agent else "",
                now=now,
            )
        except Exception:
            self._logger.exception(
                "Could not check a run_context.sign request. event=scheduler_run_context_check_failed "
                "schedule_id=%s agent_id=%s request_id=%s",
                schedule_id,
                asking_agent,
                request_id,
            )
            return {"outcome": "check_failed", "message": "The scheduler could not verify this run claim."}

        outcome = reply["outcome"]
        if outcome == "ok":
            self._logger.info(
                "Signed a scheduled run's authority token. event=scheduler_run_context_signed "
                "schedule_id=%s scheduled_job_id=%s agent_id=%s system_work=%s request_id=%s",
                schedule_id,
                scheduled_job_id,
                asking_agent,
                reply["system_work"],
                request_id,
            )
        else:
            self._logger.warning(
                "Refused to sign a scheduled run's authority token. event=scheduler_run_context_refused "
                "schedule_id=%s scheduled_job_id=%s agent_id=%s request_id=%s outcome=%s",
                schedule_id,
                scheduled_job_id,
                asking_agent,
                request_id,
                outcome,
            )
        return reply

    def _sign_or_refuse(
        self,
        *,
        request_id: str,
        schedule_id: str,
        claim_token: str,
        scheduled_job_id: str,
        heartbeat_item_id: Optional[str],
        asking_agent: str,
        now: datetime,
    ) -> dict[str, Any]:
        identity = self._identity_fn()
        if not _has_scheduler_role(identity):
            return {"outcome": "signer_missing", "message": "The scheduler signing certificate is not ready."}
        assert identity is not None
        if not asking_agent:
            return {"outcome": "claim_not_held", "message": "Only an agent session may ask to sign a scheduled run."}
        schedule = self._store.get_scheduled_message(schedule_id)
        if schedule is None:
            return {"outcome": "not_found", "message": f"Schedule {schedule_id} was not found."}
        if not self._store.holds_claim(
            schedule_id, claim_token=claim_token, claimer_agent_id=asking_agent, now=now
        ):
            return {"outcome": "claim_not_held", "message": "The schedule claim is not held by this agent."}
        if schedule_stopped_reason(schedule, now) is not None:
            return {"outcome": "schedule_stopped", "message": "The schedule is paused, turned off, or ended."}

        target_error = self._check_target(
            schedule=schedule,
            scheduled_job_id=scheduled_job_id,
            heartbeat_item_id=heartbeat_item_id,
        )
        if target_error is not None:
            return target_error

        root_task_id = f"run_{uuid4().hex}"
        owner_id = owner_canonical_user_id_for_schedule(schedule)
        if _is_system_work(schedule) or not owner_id:
            token = root_task_token(
                identity,
                team=self._team_id,
                root_task_id=root_task_id,
                request_id=f"task_context_{uuid4().hex}",
                person_ref=None,
                schedule_id=scheduled_job_id,
                issued=now,
            )
            return {"outcome": "ok", "system_work": True, "authority": token}
        owner: PersonNow = self._people.person(owner_id)
        if inactive_owner(owner, schedule_id=scheduled_job_id) or not owner.active:
            return {"outcome": "owner_inactive", "message": "The schedule's owner is not active."}
        if not is_schedule_vouched(
            schedule_id,
            schedule,
            owner_id,
            store=self._store,
            team_id=self._team_id,
            verifier=self._verifier_fn(),
        ):
            return {"outcome": "owner_inactive", "message": "The schedule's owner is not vouched for."}
        person_ref = _person_ref_of(self._people, owner_id)
        if person_ref is None:
            return {"outcome": "owner_inactive", "message": "The schedule's owner has no person reference."}

        token = root_task_token(
            identity,
            team=self._team_id,
            root_task_id=root_task_id,
            request_id=f"schedule_run_{uuid4().hex}",
            person_ref=person_ref,
            schedule_id=scheduled_job_id,
            issued=now,
        )
        return {"outcome": "ok", "system_work": False, "authority": token}

    def _check_target(
        self,
        *,
        schedule: ScheduledMessage,
        scheduled_job_id: str,
        heartbeat_item_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        if heartbeat_item_id:
            if not is_heartbeat_launcher(schedule):
                return {"outcome": "heartbeat_item_not_found", "message": "Schedule is not a heartbeat launcher."}
            source_id = getattr(getattr(schedule.payload_meta, "gateway_context", None), "heartbeat_source_id", None)
            item = self._heartbeat_store_fn().get_item(heartbeat_item_id)
            if item is None or not item.enabled or item.source_id != source_id:
                return {"outcome": "heartbeat_item_not_found", "message": "Heartbeat item was not found or is disabled."}
            if scheduled_job_id != item.heartbeat_item_id:
                return {"outcome": "scheduled_job_mismatch", "message": "Scheduled job id does not match heartbeat item."}
            return None

        gateway_context = getattr(schedule.payload_meta, "gateway_context", None)
        expected_job_id = getattr(gateway_context, "scheduled_job_id", None) or schedule.schedule_id
        if scheduled_job_id != expected_job_id:
            return {"outcome": "scheduled_job_mismatch", "message": "Scheduled job id does not match schedule."}
        return None

    # -- standing schedule permits -----------------------------------------------------

    def renew_permits(self, *, now: Optional[datetime] = None, limit: int = PASS_LIMIT) -> PermitPassResult:
        """Issue or renew standing protocol 1 permits for due schedules."""

        identity = self._identity_fn()
        result = PermitPassResult()
        if not _has_scheduler_role(identity):
            return result
        assert identity is not None
        moment = (now or self._clock()).astimezone(timezone.utc)
        for schedule_id in self._store.rows_needing_run_permit(now=moment, limit=limit):
            try:
                self._check_one_permit(identity, schedule_id, result, moment)
            except Exception:
                self._logger.exception(
                    "Could not check a schedule's run permit; the next pass tries again. "
                    "event=schedule_permit_check_failed schedule_id=%s",
                    schedule_id,
                )
        return result

    def issue_permit_for_schedule(self, schedule_id: str, *, now: Optional[datetime] = None) -> bool:
        """Synchronously issue or refresh one schedule's permit (called by ``schedules.install``)."""

        identity = self._identity_fn()
        if not _has_scheduler_role(identity):
            return False
        assert identity is not None
        moment = (now or self._clock()).astimezone(timezone.utc)
        result = PermitPassResult()
        self._check_one_permit(identity, schedule_id, result, moment)
        return schedule_id in result.issued or schedule_id in result.renewed

    def _permit_person_ref(
        self,
        schedule_id: str,
        schedule: ScheduledMessage,
        owner: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """Return ``(person_ref, refusal_reason)`` for a person's schedule permit."""

        if not owner:
            return None, REFUSED_NO_OWNER
        if not self._people.person(owner).active:
            return None, REFUSED_OWNER_NOT_ACTIVE
        if not is_schedule_vouched(
            schedule_id,
            schedule,
            owner,
            store=self._store,
            team_id=self._team_id,
            verifier=self._verifier_fn(),
        ):
            return None, REFUSED_NOT_VOUCHED
        person_ref = _person_ref_of(self._people, owner)
        if person_ref is None:
            return None, REFUSED_NO_PERSON_REF
        return person_ref, None

    def _check_one_permit(
        self,
        identity: HostIdentity,
        schedule_id: str,
        result: PermitPassResult,
        now: datetime,
    ) -> None:
        schedule = self._store.get_scheduled_message(schedule_id)
        if schedule is None:
            result.refused[schedule_id] = REFUSED_NOT_FOUND
            return
        owner = owner_canonical_user_id_for_schedule(schedule)
        renewing = self._store.run_permit(schedule_id) is not None

        if _is_system_work(schedule):
            permit_dict = schedule_permit_token(
                identity,
                team=self._team_id,
                schedule_id=schedule_id,
                person_ref=None,
                owner_permit=False,
                issued=now,
            )
            permit = AuthorityTokenRecord.model_validate(permit_dict)
            owner_permit: Optional[AuthorityTokenRecord] = None
            if owner:
                owner_ref, owner_refusal = self._permit_person_ref(schedule_id, schedule, owner)
                if owner_refusal is None and owner_ref is not None:
                    owner_permit_dict = schedule_permit_token(
                        identity,
                        team=self._team_id,
                        schedule_id=schedule_id,
                        person_ref=owner_ref,
                        owner_permit=True,
                        issued=now,
                    )
                    owner_permit = AuthorityTokenRecord.model_validate(owner_permit_dict)
            expires_at = parse_timestamp(permit.expires_at)
            if not self._store.set_run_permit(
                schedule_id,
                permit=permit,
                owner_permit=owner_permit,
                checked_owner=owner,
                recheck_at=expires_at - RENEW_WITHIN,
            ):
                return
            (result.renewed if renewing else result.issued).append(schedule_id)
            self._logger.info(
                "Issued a system-work schedule's run permit. event=%s schedule_id=%s owner=%s "
                "has_owner_permit=%s token_id=%s expires_at=%s",
                "schedule_permit_renewed" if renewing else "schedule_permit_issued",
                schedule_id,
                owner,
                owner_permit is not None,
                permit.token_id,
                permit.expires_at,
            )
            return

        person_ref, refusal = self._permit_person_ref(schedule_id, schedule, owner)
        if refusal is not None or person_ref is None:
            self._store.set_run_permit(
                schedule_id,
                permit=None,
                owner_permit=None,
                checked_owner=owner,
                recheck_at=now + RECHECK_REFUSED_AFTER,
            )
            reason = refusal or REFUSED_NO_PERSON_REF
            result.refused[schedule_id] = reason
            self._logger.info(
                "The scheduler issued no run permit for a schedule. event=schedule_permit_refused "
                "schedule_id=%s owner=%s reason=%s",
                schedule_id,
                owner,
                reason,
            )
            return

        permit_dict = schedule_permit_token(
            identity,
            team=self._team_id,
            schedule_id=schedule_id,
            person_ref=person_ref,
            owner_permit=False,
            issued=now,
        )
        permit = AuthorityTokenRecord.model_validate(permit_dict)
        expires_at = parse_timestamp(permit.expires_at)
        if not self._store.set_run_permit(
            schedule_id,
            permit=permit,
            owner_permit=None,
            checked_owner=owner,
            recheck_at=expires_at - RENEW_WITHIN,
        ):
            return
        (result.renewed if renewing else result.issued).append(schedule_id)
        self._logger.info(
            "Issued a schedule's run permit. event=%s schedule_id=%s person_id=%s token_id=%s expires_at=%s",
            "schedule_permit_renewed" if renewing else "schedule_permit_issued",
            schedule_id,
            owner,
            permit.token_id,
            permit.expires_at,
        )


__all__ = [
    "PASS_LIMIT",
    "PermitPassResult",
    "RECHECK_REFUSED_AFTER",
    "REFUSED_NOT_FOUND",
    "REFUSED_NOT_VOUCHED",
    "REFUSED_NO_OWNER",
    "REFUSED_NO_PERSON_REF",
    "REFUSED_OWNER_NOT_ACTIVE",
    "RENEW_WITHIN",
    "SCHEDULER_ADDRESS",
    "SCHEDULER_KINDS",
    "SchedulerOperations",
    "is_schedule_vouched",
]
