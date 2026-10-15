"""Requests for access, hand-downs by agents, restrictions and revocations.

The access plan's standard flows (section 5.5) F3–F7, on top of the access
service (``access_control.decisions``):

- **An agent asks** (``submit``). When the agent and the person the task works
  for already hold the access, nothing is created. A worker's request is
  handed down at once when the team lead holds an automatic, delegable grant
  covering it, and the person at the root of that grant's chain gets a notice;
  this is trusted code using authority a person gave the team lead, not the
  team lead's model deciding. Everything else waits for a person: the request
  is stored and a review opens in the Inbox. The person must also hold the
  access (agents never exceed their person), so when they do not, the review
  says so and only someone who can give it to both can approve it.
- **A person decides** (``decide``): approve, approve read only, approve for
  this task only, or deny. Approving uses the reviewer's own authority
  (``AccessService.grant_by_person``); a hand-down that waits under an
  "ask me each time" grant is approved in place.
- **An agent hands on access** (``give``), **restricts** it (a block, which
  anyone who could have given the access may add) or **revokes** what its
  agent type handed out.
- **A person blocks** an agent type, an agent or a person from a tool or one
  operation (``block``), and removes a block (``unblock``). A block wins over
  every grant from the next decision on, is recorded in the access store's
  event log, and replaces the old emergency deny (decision D10: immediate and
  audited, no offline signature). Blocking a person also stops every agent
  working for them (agents never exceed their person).

Grants that agents give are recorded under the giver's agent type, because
access goes to agent types and an agent's instance id changes on restart. The
team lead acts under its configured agent type.

The Inbox is reached through ``AccessInbox`` so this module stays free of
gateway and messaging code. Trusted code that shows requests elsewhere (the
access workflows of ``custom_agents.access_workflows``) watches them through
``RequestWatcher``, so this module never imports it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import (
    AGENT_ROLE_TEAM_LEAD,
    AGENT_ROLE_WORKER,
    AgentPrincipal,
    PersonPrincipal,
    agent_type_id,
    schedule_subject,
)
from access_control.people import PERSON_ROLE_ADMIN, is_administrator
from access_control.standard_grants import is_standard_grant
from access_catalog.catalog import change_operation, missing_operation
from custom_messages.contracts.access_v2 import (
    TASK_BOUND_LIFETIMES,
    AccessLevel,
    AccessNoticeKind,
    AccessNoticeV1,
    AccessRequestKind,
    AccessRequestStatus,
    AccessRequestV1,
    AccessReviewChoice,
    ChangeProposalV1,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from id_generation import new_id
from storage.access_repository import AccessStoreConflict

logger = logging.getLogger(__name__)

LEVEL_WORDS = {AccessLevel.READ: "read", AccessLevel.WRITE: "read-and-change", AccessLevel.FULL: "full"}
REQUESTABLE_LIFETIMES = (GrantLifetime.ONE_TIME, GrantLifetime.TASK, GrantLifetime.UNTIL, GrantLifetime.UNTIL_REVOKED)
MAX_REQUEST_HOURS = 24 * 90
# What people may block: an agent type, one agent, a person, a role or the
# whole team, from a tool set, a tool, one of its operations, a folder or a
# file (access plan section 6.7). Blocking the team from the internet set turns
# internet access off for every agent (decision D14).
BLOCKABLE_SUBJECTS = frozenset(
    {SubjectKind.AGENT_TYPE, SubjectKind.AGENT, SubjectKind.USER, SubjectKind.ROLE, SubjectKind.TEAM}
)
# Blocking these affects people, so only owners and administrators may.
PEOPLE_SUBJECTS = frozenset({SubjectKind.USER, SubjectKind.ROLE, SubjectKind.TEAM})
# Changes trusted code applies at the decision itself (ChangeApplier): a file a
# person sends into another person's folder (D15) and an access proposal
# (Phase 6). Deciding them anywhere without their applier would record an
# approval nothing applies, so it is refused.
FILE_DELIVERY_CHANGE = "file_delivery"
ACCESS_PROPOSAL_CHANGE = "access_proposal"
APPLIED_AT_DECISION = frozenset({FILE_DELIVERY_CHANGE, ACCESS_PROPOSAL_CHANGE})
BLOCKABLE_TARGETS = frozenset(
    {TargetKind.TOOL_SET, TargetKind.TOOL, TargetKind.OPERATION, TargetKind.FOLDER, TargetKind.FILE}
)
SAVED_BEFORE_WORK_IDS = (
    "This request was made before an upgrade, for a task that has ended, so it can only be denied. "
    "The agent asks again if it still needs the access."
)


def before_work_ids(request: AccessRequestV1) -> bool:
    """A request for one task saved before access for one task covered the whole request (v0.6.0).

    It names only the agent's task, whose access ended with that task, so
    nothing can be approved for it; it can still be shown and denied.
    """

    return request.lifetime in TASK_BOUND_LIFETIMES and not request.work_id


class AccessInbox(Protocol):
    """Where reviews and notices go (the gateway's Inbox, in production)."""

    def open_review(self, request: AccessRequestV1) -> None: ...

    def send_notice(self, notice: AccessNoticeV1) -> None: ...

    def request_finished(self, request: AccessRequestV1) -> None:
        """Close the request's review and wake the task waiting on it."""


class ChangeApplier(Protocol):
    """Trusted code that applies one kind of change when a person approves it.

    Most staged changes are applied later by the agent that asked
    (``apply_change``). Some are applied at the decision itself, where the
    reviewer's verified roles are known: access proposals (Phase 6) and files
    a person sends into another person's folder (decision D15). ``apply``
    re-checks everything against the reviewer's authority, stores its effects
    together with ``finished`` (the request's outcome), and returns the stored
    outcome; it raises ``HandDownRefused`` when anything fails, leaving
    nothing applied.
    """

    def apply(self, request: AccessRequestV1, reviewer: PersonPrincipal, finished: AccessRequestV1) -> AccessRequestV1: ...


class RequestWatcher(Protocol):
    """Trusted code told about each request an agent makes (see the module docstring).

    Grants nothing and decides nothing about access. ``check`` may only refuse:
    an access workflow refuses a request for a step the person already denied,
    so an agent cannot ask again automatically.
    """

    def check(self, request: AccessRequestV1) -> None:
        """Raise ``HandDownRefused`` to refuse the request before it is stored."""

    def stored(self, request: AccessRequestV1) -> None:
        """The request was stored (waiting, or granted at once by a hand-down)."""


@dataclass(frozen=True)
class RequestOutcome:
    """What happened to a request or hand-down, in words the agent can act on."""

    message: str
    request: AccessRequestV1 | None = None
    grant: GrantV1 | None = None
    already_held: bool = False

    @property
    def waiting(self) -> bool:
        return self.request is not None and self.request.status is AccessRequestStatus.WAITING


@dataclass
class AccessRequests:
    service: AccessService
    inbox: AccessInbox
    lead_agent_type: str
    watcher: RequestWatcher | None = None
    # Change kind -> trusted code that applies it at the decision (see ChangeApplier).
    appliers: dict[str, ChangeApplier] = field(default_factory=dict)

    # -- watching ---------------------------------------------------------------

    def _check(self, request: AccessRequestV1) -> None:
        """Let the watcher refuse a request; its own failure never blocks asking (logged)."""

        if self.watcher is None:
            return
        try:
            self.watcher.check(request)
        except HandDownRefused:
            raise
        except Exception:  # noqa: BLE001 - a view must not stop access requests
            logger.exception(
                "Could not check an access request against its task's workflow; asking anyway. "
                "event=access_request_watch_check_failed request_id=%s task_id=%s",
                request.request_id,
                request.task_id,
            )

    def _stored(self, request: AccessRequestV1) -> None:
        if self.watcher is None:
            return
        try:
            self.watcher.stored(request)
        except Exception:  # noqa: BLE001 - the request stands; only its display is missing
            logger.exception(
                "Could not show an access request on its task's workflow. "
                "event=access_request_watch_link_failed request_id=%s task_id=%s",
                request.request_id,
                request.task_id,
            )

    # -- descriptions ---------------------------------------------------------

    def target_name(self, target: TargetRefV1) -> str:
        catalog = self.service.catalog
        if target.kind is TargetKind.FOLDER:
            return f"the {target.id}/ folder"
        if target.kind is TargetKind.FILE:
            return f"the file {target.id}"
        if target.kind is TargetKind.TOOL_SET and target.id in catalog.tool_sets:
            return catalog.tool_sets[target.id].display_name
        tool_id, _, operation = target.id.partition("#")
        tool = catalog.tools.get(tool_id)
        name = tool.display_name if tool is not None else tool_id
        return f"{name} ({operation})" if operation else name

    def subject_name(self, subject: SubjectRefV1) -> str:
        if subject.kind is SubjectKind.SCHEDULE:
            return f"scheduled job {subject.id.split('/', 1)[-1]}"
        return subject.id.split("/", 1)[-1] if subject.kind in {SubjectKind.AGENT_TYPE, SubjectKind.ROLE} else subject.id

    def lead(self) -> AgentPrincipal:
        return AgentPrincipal(self.lead_agent_type, self.lead_agent_type, AGENT_ROLE_TEAM_LEAD, self.service.team_id)

    # -- asking ---------------------------------------------------------------

    def _spec(
        self,
        *,
        subject: SubjectRefV1,
        target: TargetRefV1,
        level: AccessLevel,
        lifetime: GrantLifetime,
        task_id: str | None,
        granted_by: SubjectRefV1,
        reason: str,
        expires_at: datetime | None = None,
        delegation_depth: int = 0,
        for_person_id: str | None = None,
    ) -> GrantV1:
        task_bound = lifetime in {GrantLifetime.TASK, GrantLifetime.ONE_TIME}
        return GrantV1(
            grant_id="new",
            team_id=self.service.team_id,
            subject=subject,
            target=target,
            level=level,
            lifetime=lifetime,
            expires_at_utc=expires_at if lifetime is GrantLifetime.UNTIL else None,
            task_id=task_id if task_bound else None,
            uses_remaining=1 if lifetime is GrantLifetime.ONE_TIME else None,
            delegation_depth=delegation_depth,
            granted_by=granted_by,
            reason=reason,
            created_at_utc=self.service.clock(),
            for_person_id=for_person_id if subject.kind is not SubjectKind.USER else None,
        )

    def approval_specs(
        self,
        request: AccessRequestV1,
        *,
        level: AccessLevel,
        lifetime: GrantLifetime,
        expires_at: datetime | None,
        granted_by: SubjectRefV1,
        hand_on: bool = False,
    ) -> list[GrantV1]:
        """The grants approving an access request creates, main holder first ("access follows the work").

        - For one task only: the requester's grant for that task (the whole
          request); the team lead's may be handed on to the workers helping
          with the same request.
        - Kept, the team lead's own request: its lasting access (it counts in
          all its work for the person), handed on only when the reviewer
          allowed it.
        - Kept, a worker's request in a scheduled job's run: the schedule's
          lasting access; every agent in its runs is in the schedule.
        - Kept, another worker's request: the team lead's lasting access for the
          person, which it may hand on without asking, so the worker's later
          tasks get it at once; and the worker's grant for this task, handed
          down from it (``_approve``), so removing the team lead's access ends
          it too. Workers keep no lasting access of their own
          (``AccessService.agent_grants``).
        - The person too, when they lack the access (agents never exceed their person).
        """

        reason = f"Approved request from {request.requester_agent_type}: {request.reason}"
        lasting = lifetime not in TASK_BOUND_LIFETIMES
        is_lead = request.requester_agent_type == self.lead_agent_type

        def grant(subject: SubjectRefV1, when: GrantLifetime, *, depth: int = 0) -> GrantV1:
            return self._spec(
                subject=subject,
                target=request.target,
                level=level,
                lifetime=when,
                # Access for one task covers the whole request (every agent on it).
                task_id=request.work_id,
                granted_by=granted_by,
                reason=reason,
                expires_at=expires_at,
                for_person_id=request.person_id,
                delegation_depth=depth,
            )

        if not lasting:
            # For the request only; the team lead may hand it on to the workers helping with it.
            specs = [grant(request.subject, lifetime, depth=1 if is_lead and hand_on and lifetime is GrantLifetime.TASK else 0)]
        elif is_lead:
            specs = [grant(request.subject, lifetime, depth=1 if hand_on else 0)]
        elif request.schedule_id:
            specs = [grant(schedule_subject(self.service.team_id, request.schedule_id), lifetime)]
        else:
            specs = [grant(self.lead().type_subject(), lifetime, depth=1)]
            if request.work_id:
                specs.append(grant(request.subject, GrantLifetime.TASK))
        if request.person_lacks_access and request.person_id:
            specs.append(grant(SubjectRefV1(kind=SubjectKind.USER, id=request.person_id), lifetime))
        return specs

    def access_beside(self, request: AccessRequestV1) -> tuple[SubjectRefV1 | None, list[GrantV1], list[GrantV1]]:
        """What approving an access request as asked does to its main holder's access on the same tool.

        Returns (holder, replaced, kept): who keeps the access (``approval_specs``),
        grants in force that the approved grant would replace
        (``access_control.grant_entries``), and grants in the same entry that
        stay beside it (the approval is a step-up). No holder for a change.
        """

        if request.kind is not AccessRequestKind.ACCESS or request.subject.kind is SubjectKind.USER:
            return None, [], []
        [spec, *_rest] = self.approval_specs(
            request,
            level=request.level,
            lifetime=request.lifetime,
            expires_at=request.expires_at_utc,
            granted_by=request.decided_by or SubjectRefV1(kind=SubjectKind.TEAM, id=self.service.team_id),
            # As the card starts: the team lead may hand it on.
            hand_on=True,
        )
        spec = spec.model_copy(update={"grant_id": f"review_{request.request_id}"})
        replaced_ids = set(self.service.plan_grants([spec]).replaced_by(spec.grant_id))
        entry = [
            grant
            for grant in self.service.entry_grants(spec)
            if grant.grant_id != spec.grant_id and grant.status is GrantStatus.ACTIVE
        ]
        return (
            spec.subject,
            [grant for grant in entry if grant.grant_id in replaced_ids],
            [grant for grant in entry if grant.grant_id not in replaced_ids],
        )

    def _check_request(
        self, target: TargetRefV1, lifetime: GrantLifetime, task_id: str | None, hours: int | None
    ) -> datetime | None:
        """Validate what is asked for; returns the expiry for access until a time."""

        # Access is a level on a whole tool (owner's decision 2026-09-30):
        # operation names are fragile (sending mail is both gmail_send and
        # command:gmail.send), so asking for one operation is refused. Blocks
        # may still name one operation.
        if target.kind is TargetKind.OPERATION or (target.kind is TargetKind.TOOL and "#" in target.id):
            tool_id = target.id.partition("#")[0]
            raise HandDownRefused(
                f"Access is given for a whole tool at a level, not for one operation: ask for "
                f"tool_id={tool_id!r} at the level the operation needs (find_tools lists the levels)."
            )
        # Raises HandDownRefused for targets outside the catalog.
        self.service.target_entities_for_ref(target)
        if lifetime not in REQUESTABLE_LIFETIMES:
            raise ValueError(f"Unknown lifetime {lifetime!r}.")
        if lifetime in {GrantLifetime.TASK, GrantLifetime.ONE_TIME} and not task_id:
            raise ValueError("Access for one task can only be asked for while working on a task.")
        if lifetime is GrantLifetime.UNTIL and not (hours and 1 <= hours <= MAX_REQUEST_HOURS):
            raise ValueError(f"Access for a limited time needs hours between 1 and {MAX_REQUEST_HOURS}.")
        return self.service.clock() + timedelta(hours=hours) if lifetime is GrantLifetime.UNTIL and hours else None

    def submit(
        self,
        *,
        requester: AgentPrincipal,
        person: PersonPrincipal | None,
        target: TargetRefV1,
        level: AccessLevel,
        reason: str,
        lifetime: GrantLifetime = GrantLifetime.UNTIL_REVOKED,
        hours: int | None = None,
        task_id: str | None = None,
        conversation_id: str | None = None,
        tool_use_id: str | None = None,
        hand_on: bool = False,
    ) -> RequestOutcome:
        """Ask for access for the requester's agent type (see module docstring).

        ``task_id`` is the asking agent's own task (it pauses and resumes);
        access for one task is bound to the request it works on
        (``requester.work_id``).
        """

        work_id = requester.work_id
        expires_at = self._check_request(target, lifetime, work_id, hours)
        name = self.target_name(target)
        self._refuse_when_blocked(requester, person, target, work_id)
        person_id = person.person_id if person else None
        # hand_on: only the team lead asks for access its workers' tasks can draw on.
        hand_on = hand_on and requester.agent_type == self.lead_agent_type
        agent_holds = self.service.holds(requester, target, level, task_id=work_id, person_id=person_id) and (
            not hand_on or self.service.may_give(requester, target, level, task_id=work_id, person_id=person_id)
        )
        person_lacks = person is not None and not self.service.holds(person, target, level, task_id=work_id)
        if agent_holds and not person_lacks:
            return RequestOutcome(
                f"You already have {LEVEL_WORDS[level]} access to {name}; use it.", already_held=True
            )
        request = AccessRequestV1(
            request_id=new_id("access_req"),
            team_id=self.service.team_id,
            requester_agent_id=requester.agent_id,
            requester_agent_type=requester.agent_type,
            person_id=person.person_id if person else None,
            subject=requester.type_subject(),
            target=target,
            level=level,
            lifetime=lifetime,
            expires_at_utc=expires_at,
            reason=reason,
            task_id=task_id,
            work_id=work_id,
            schedule_id=requester.schedule_id,
            conversation_id=conversation_id,
            tool_use_id=tool_use_id,
            person_lacks_access=person_lacks,
            hand_on=hand_on,
            created_at_utc=self.service.clock(),
        )
        self._check(request)
        if requester.role == AGENT_ROLE_WORKER and not person_lacks:
            handed = self._lead_hands_down(request)
            if handed is not None:
                return handed
        return self._wait_for_review(request)

    def _refuse_when_blocked(
        self, requester: AgentPrincipal, person: PersonPrincipal | None, target: TargetRefV1, task_id: str | None
    ) -> None:
        """A grant never overrides a block, so a request for blocked access is refused at once."""

        name = self.target_name(target)
        agent_blocks = self.service.blocks_on(
            requester, target, task_id=task_id, person_id=person.person_id if person else None
        )
        person_blocks = self.service.blocks_on(person, target, task_id=task_id) if person is not None else ()
        if not (agent_blocks or person_blocks):
            return
        logger.info(
            "Access request refused because a block applies. event=access_request_blocked agent_type=%s "
            "person_id=%s target=%s blocks=%s",
            requester.agent_type,
            person.person_id if person else None,
            target.id,
            ",".join(agent_blocks + person_blocks),
        )
        who = requester.agent_type if agent_blocks else "The person this task works for"
        raise HandDownRefused(
            f"{who} is blocked from {name}, and asking for access cannot override a block. "
            "Tell the person you work for; only a person can remove the block."
        )

    def _lead_hands_down(self, request: AccessRequestV1) -> RequestOutcome | None:
        """The team lead adds access it may hand on to the worker's task (for that task only).

        Not in a scheduled job's run: there the worker works for the schedule,
        so a person decides and "Keep" adds it to the schedule.
        """

        if request.schedule_id or not request.work_id:
            return None
        lead = self.lead()
        spec = self._spec(
            subject=request.subject,
            target=request.target,
            level=request.level,
            lifetime=GrantLifetime.TASK,
            task_id=request.work_id,
            granted_by=lead.type_subject(),
            reason=f"Asked for by {request.requester_agent_type}: {request.reason}",
            for_person_id=request.person_id,
        )
        try:
            grant = self.service.hand_down(lead, spec, task_id=request.work_id, person_id=request.person_id)
        except HandDownRefused as exc:
            logger.info(
                "The team lead cannot hand this access down; a person decides. "
                "event=access_request_to_person request_id=%s target=%s level=%s why=%s",
                request.request_id,
                request.target.id,
                request.level.value,
                exc,
            )
            return None
        if grant.status is GrantStatus.PENDING:
            return self._wait_for_review(request.model_copy(update={"grant_ids": (grant.grant_id,)}))
        granted = request.model_copy(
            update={
                "status": AccessRequestStatus.GRANTED,
                "grant_ids": (grant.grant_id,),
                "decided_by": lead.type_subject(),
                "decided_at_utc": self.service.clock(),
            }
        )
        self.service.store.create_request(granted)
        self._stored(granted)
        self._notify_hand_down(grant)
        return RequestOutcome(
            f"{self.lead_agent_type} gave you {LEVEL_WORDS[grant.level]} access to "
            f"{self.target_name(grant.target)} for this task. Retry the call.",
            request=granted,
            grant=grant,
        )

    def _wait_for_review(self, request: AccessRequestV1) -> RequestOutcome:
        self.service.store.create_request(request)
        self.inbox.open_review(request)
        self._stored(request)
        logger.info(
            "Access request waits for a person. event=access_request_waiting request_id=%s agent_type=%s "
            "target=%s level=%s person_lacks_access=%s",
            request.request_id,
            request.requester_agent_type,
            request.target.id,
            request.level.value,
            request.person_lacks_access,
        )
        who = "a team administrator" if request.person_lacks_access else "a person who can give it"
        return RequestOutcome(
            f"Your request for {LEVEL_WORDS[request.level]} access to {self.target_name(request.target)} "
            f"waits for {who}.",
            request=request,
        )

    def _notify_hand_down(self, grant: GrantV1) -> None:
        """Tell the person at the root of the grant's chain about lasting access handed on (decision D3).

        Access added to one task is not announced ("access follows the work":
        workers get access for each task from what the person let the team
        lead hand on, and a notice per task would bury the ones that matter);
        the Access page lists it while the task runs.
        """

        if grant.lifetime in TASK_BOUND_LIFETIMES:
            return
        root = self.service.chain(grant)[0]
        giver = self.subject_name(grant.granted_by)
        recipient = self.subject_name(grant.subject)
        person = root.granted_by.id if root.granted_by.kind is SubjectKind.USER else None
        notice = AccessNoticeV1(
            notice_id=new_id("access_notice"),
            team_id=self.service.team_id,
            kind=AccessNoticeKind.HAND_DOWN,
            recipient_person_id=person,
            recipient_roles=() if person else (PERSON_ROLE_ADMIN,),
            title=f"{giver} gave {recipient} {LEVEL_WORDS[grant.level]} access to {self.target_name(grant.target)}",
            summary=(
                f"{giver} used access you gave it to hand this on without asking. "
                f"Lifetime: {grant.lifetime.value.replace('_', ' ')}. Reason: {grant.reason or 'none given'}. "
                "You can revoke it; nothing else is needed."
            ),
            grant_id=grant.grant_id,
            created_at_utc=self.service.clock(),
        )
        try:
            self.inbox.send_notice(notice)
        except Exception:  # noqa: BLE001 - the grant stands; the missing notice is logged
            logger.exception(
                "Could not send a hand-down notice. event=access_notice_failed grant_id=%s", grant.grant_id
            )

    # -- staged changes (access_control.changes) ----------------------------------

    def change_target(self, kind: str) -> tuple[TargetRefV1, AccessLevel]:
        tool_id, level = self.service.catalog.changes.get(kind, (None, None))
        if tool_id is None or level is None:
            raise HandDownRefused(f"{kind} is not a kind of change in the tool catalog.")
        return TargetRefV1(kind=TargetKind.OPERATION, id=f"{tool_id}#{change_operation(kind)}"), level

    def submit_change(
        self,
        *,
        requester: AgentPrincipal,
        person: PersonPrincipal | None,
        proposal: ChangeProposalV1,
        reason: str,
        task_id: str | None = None,
        conversation_id: str | None = None,
        tool_use_id: str | None = None,
    ) -> RequestOutcome:
        """Ask to apply one exact staged change; applies at once only with held access."""

        target, level = self.change_target(proposal.kind)
        agent_holds = self.service.holds(
            requester, target, level, task_id=requester.work_id, person_id=person.person_id if person else None
        )
        person_holds = person is None or self.service.holds(person, target, level, task_id=requester.work_id)
        if agent_holds and person_holds:
            return RequestOutcome(f"You may make this change: {proposal.title}.", already_held=True)
        request = AccessRequestV1(
            request_id=new_id("access_req"),
            team_id=self.service.team_id,
            kind=AccessRequestKind.CHANGE,
            change=proposal,
            requester_agent_id=requester.agent_id,
            requester_agent_type=requester.agent_type,
            person_id=person.person_id if person else None,
            subject=requester.type_subject(),
            target=target,
            level=level,
            reason=reason,
            task_id=task_id,
            work_id=requester.work_id,
            conversation_id=conversation_id,
            tool_use_id=tool_use_id,
            created_at_utc=self.service.clock(),
        )
        self._check(request)
        self.service.store.create_request(request)
        self.inbox.open_review(request)
        self._stored(request)
        logger.info(
            "Staged change waits for a person. event=change_request_waiting request_id=%s kind=%s change_id=%s",
            request.request_id,
            proposal.kind,
            proposal.change_id,
        )
        return RequestOutcome(f"{proposal.title}: waiting for a person to approve it.", request=request)

    def submit_person_change(
        self, *, requester: PersonPrincipal, proposal: ChangeProposalV1, reason: str
    ) -> AccessRequestV1:
        """A person asks for a change someone else decides (a file sent into their folder).

        Only kinds trusted code applies at the decision may be asked for this
        way, because no agent waits to apply them afterwards.
        """

        if proposal.kind not in self.appliers:
            raise HandDownRefused(f"{proposal.kind} is not a change a person can ask for.")
        target, level = self.change_target(proposal.kind)
        request = AccessRequestV1(
            request_id=new_id("access_req"),
            team_id=self.service.team_id,
            kind=AccessRequestKind.CHANGE,
            change=proposal,
            requested_by_person_id=requester.person_id,
            person_id=requester.person_id,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=requester.person_id),
            target=target,
            level=level,
            reason=str(reason or "").strip() or proposal.title,
            created_at_utc=self.service.clock(),
        )
        self.service.store.create_request(request)
        self.inbox.open_review(request)
        logger.info(
            "A person's change waits for a decision. event=person_change_waiting request_id=%s kind=%s by=%s owner=%s",
            request.request_id,
            proposal.kind,
            requester.person_id,
            proposal.owner_person_id,
        )
        return request

    def may_decide(self, person: PersonPrincipal, request: AccessRequestV1) -> bool:
        """Who decides a request (decision D13: people authorize their own work).

        - A staged change: its owner, or anyone who could give the level
          (administrators for team and platform kinds).
        - Access for work for a person who holds it themselves: only that
          person. What an agent may do for someone is theirs to authorize;
          an administrator does not decide it for them.
        - Access the person lacks, and work for nobody: anyone who could give
          it (administrators for team targets); approving then gives the
          person the access too.
        """

        if request.change is not None and request.change.owner_person_id == person.person_id:
            return True
        if request.kind is AccessRequestKind.ACCESS and request.person_id and not request.person_lacks_access:
            if request.person_id != person.person_id:
                return False
            return self.service.holds(person, request.target, request.level, task_id=request.task_id)
        return self.service.may_give(person, request.target, request.level, task_id=request.task_id)

    def may_change_directly(self, person: PersonPrincipal, kind: str) -> bool:
        """Whether a person may make a change of this kind in the gateway's own controls.

        The same people who could approve it in the Inbox: anyone who could
        give the change kind's level (administrators for platform kinds).
        """

        target, level = self.change_target(kind)
        return self.service.may_give(person, target, level, task_id=None)

    def claim_change(self, request_id: str) -> AccessRequestV1:
        """Use an approved change's approval once, just before trusted code applies it."""

        try:
            return self.service.store.claim_change(request_id, at=self.service.clock())
        except AccessStoreConflict as exc:
            raise HandDownRefused(str(exc)) from exc

    # -- deciding ---------------------------------------------------------------

    def waiting_request(self, request_id: str) -> AccessRequestV1:
        request = self.service.store.get_request(request_id)
        if request is None or request.team_id != self.service.team_id:
            raise HandDownRefused("There is no such access request in this team.")
        if request.status is not AccessRequestStatus.WAITING:
            raise HandDownRefused(f"That access request was already {request.status.value}.")
        return request

    def decide(
        self,
        request_id: str,
        reviewer: PersonPrincipal,
        choice: AccessReviewChoice,
        *,
        note: str = "",
        kept_item_ids: tuple[str, ...] = (),
        keep: bool | None = None,
        keep_hours: int | None = None,
        hand_on: bool | None = None,
    ) -> AccessRequestV1:
        """Record a person's decision on a waiting request and create its grants.

        ``keep`` is the review card's "Keep for later tasks" box for an access
        request: off gives the access for the task only, on as asked; None
        follows the choice (``approve_task`` is the task-only choice chat
        buttons use). ``keep_hours`` limits kept access to that many hours
        (None: as asked, normally until removed). ``hand_on`` lets the team
        lead hand the access on to its workers' tasks without asking (only on
        the team lead's own requests); None means yes, because workers get
        access only from the team lead ("access follows the work"; owner's
        decision 2026-09-30: the box starts ticked). The web card sends the
        box as ticked or not. ``kept_item_ids`` are the rows of a change (a
        proposal) the reviewer kept; the others last for the task.
        """

        if keep_hours is not None and not 1 <= keep_hours <= MAX_REQUEST_HOURS:
            raise HandDownRefused(f"Keep access for between 1 and {MAX_REQUEST_HOURS} hours, or until removed.")

        request = self.waiting_request(request_id)
        if request.kind is AccessRequestKind.CHANGE:
            return self._decide_change(request, reviewer, choice, note, kept_item_ids, keep_hours)
        if not self.may_decide(reviewer, request):
            raise HandDownRefused(
                "Only the person this work is for decides this request."
                if request.person_id and not request.person_lacks_access
                else "Only someone who can give this access decides this request."
            )
        if choice is not AccessReviewChoice.DENY and before_work_ids(request):
            raise HandDownRefused(SAVED_BEFORE_WORK_IDS)
        if choice is AccessReviewChoice.APPROVE_TASK and not request.work_id:
            raise HandDownRefused("This request is not for a task, so it cannot be approved for one task only.")
        if choice is AccessReviewChoice.APPROVE_ALL:
            choice = AccessReviewChoice.APPROVE
        decided = {
            "decided_by": SubjectRefV1(kind=SubjectKind.USER, id=reviewer.person_id),
            "decision": choice,
            "decision_note": note,
            "decided_at_utc": self.service.clock(),
        }
        if choice is AccessReviewChoice.DENY:
            self._deny_pending(request, reviewer)
            return self._finish(request.model_copy(update={**decided, "status": AccessRequestStatus.DENIED}), [])
        created = self._approve(request, reviewer, choice, keep, keep_hours=keep_hours, hand_on=hand_on is not False)
        finished = request.model_copy(
            update={**decided, "status": AccessRequestStatus.GRANTED, "grant_ids": tuple(g.grant_id for g in created)}
        )
        return self._finish(finished, created)

    def _decide_change(
        self,
        request: AccessRequestV1,
        reviewer: PersonPrincipal,
        choice: AccessReviewChoice,
        note: str,
        kept_item_ids: tuple[str, ...] = (),
        keep_hours: int | None = None,
    ) -> AccessRequestV1:
        """A change is approved as staged or denied.

        The approval itself creates no grant, except for kinds trusted code
        applies at the decision (``appliers``): those are applied here, with
        the outcome stored in the same transaction.
        """

        if choice not in {AccessReviewChoice.APPROVE, AccessReviewChoice.APPROVE_ALL, AccessReviewChoice.DENY}:
            raise HandDownRefused("A staged change is approved as it is or denied.")
        if not self.may_decide(reviewer, request):
            raise HandDownRefused("Only a team administrator, or the person the change is for, can decide it.")
        status = AccessRequestStatus.DENIED if choice is AccessReviewChoice.DENY else AccessRequestStatus.GRANTED
        assert request.change is not None
        keepable = {item.item_id for item in request.change.items if item.keepable}
        if choice is AccessReviewChoice.APPROVE_ALL:
            kept_item_ids = tuple(sorted(keepable))
        finished = request.model_copy(
            update={
                "status": status,
                "decided_by": SubjectRefV1(kind=SubjectKind.USER, id=reviewer.person_id),
                "decision": choice,
                "decision_note": note,
                "decided_at_utc": self.service.clock(),
                "kept_item_ids": tuple(item for item in kept_item_ids if item in keepable),
                "keep_hours": keep_hours,
            }
        )
        applier = self.appliers.get(request.change.kind)
        if choice is AccessReviewChoice.DENY:
            return self._finish(finished, [])
        if applier is None:
            if request.change.kind in APPLIED_AT_DECISION:
                raise HandDownRefused(
                    "This change is applied when it is approved in the gateway; decide it in the Inbox or in chat."
                )
            return self._finish(finished, [])
        applied = applier.apply(request, reviewer, finished)
        logger.info(
            "Change approved and applied. event=change_applied_at_decision request_id=%s kind=%s by=%s grants=%s",
            applied.request_id,
            request.change.kind,
            reviewer.person_id,
            ",".join(applied.grant_ids),
        )
        self.inbox.request_finished(applied)
        return applied

    def _approve(
        self,
        request: AccessRequestV1,
        reviewer: PersonPrincipal,
        choice: AccessReviewChoice,
        keep: bool | None = None,
        *,
        keep_hours: int | None = None,
        hand_on: bool = False,
    ) -> list[GrantV1]:
        pending = [grant for grant in self._grants(request) if grant.status is GrantStatus.PENDING]
        if choice is AccessReviewChoice.APPROVE and keep is not False and pending:
            return [self.service.approve_pending(reviewer, grant.grant_id) for grant in pending]
        self._deny_pending(request, reviewer)
        level = AccessLevel.READ if choice is AccessReviewChoice.APPROVE_READ else request.level
        lifetime, expires_at = self._chosen_lifetime(request, choice, keep, keep_hours)
        reviewer_ref = SubjectRefV1(kind=SubjectKind.USER, id=reviewer.person_id)
        specs = self.approval_specs(
            request, level=level, lifetime=lifetime, expires_at=expires_at, granted_by=reviewer_ref, hand_on=hand_on
        )
        # The person's own grant first: an agent's grant for their work needs it.
        specs.sort(key=lambda spec: spec.subject.kind is not SubjectKind.USER)
        # What this approval stored: a grant already in force that covers the
        # request is used as it is (access_control.grant_entries) and is never
        # undone if a later step fails.
        created: list[GrantV1] = []
        given: list[GrantV1] = []
        try:
            lead_subject = self.lead().type_subject()
            kept_by_lead = any(spec.subject == lead_subject for spec in specs) and request.subject != lead_subject
            for spec in specs:
                spec = spec.model_copy(update={"approval_id": request.request_id})
                if kept_by_lead and spec.subject == request.subject:
                    # The worker's access for this task comes from what the team
                    # lead now keeps, so removing that also ends it.
                    grant = self.service.hand_down(
                        self.lead(), spec, task_id=request.work_id, person_id=request.person_id
                    )
                else:
                    grant = self.service.grant_by_person(reviewer, spec)
                given.append(grant)
                if grant.approval_id == request.request_id:
                    created.append(grant)
        except HandDownRefused:
            self._undo(created, reviewer_ref)
            raise
        return given

    def _chosen_lifetime(
        self, request: AccessRequestV1, choice: AccessReviewChoice, keep: bool | None, keep_hours: int | None
    ) -> tuple[GrantLifetime, datetime | None]:
        """How long approved access lasts: this task, a set time, or as asked."""

        if (choice is AccessReviewChoice.APPROVE_TASK or keep is False) and request.work_id:
            return GrantLifetime.TASK, None
        if keep_hours is not None:
            return GrantLifetime.UNTIL, self.service.clock() + timedelta(hours=keep_hours)
        return request.lifetime, request.expires_at_utc

    def _grants(self, request: AccessRequestV1) -> list[GrantV1]:
        return [grant for grant_id in request.grant_ids if (grant := self.service.store.get(grant_id)) is not None]

    def _deny_pending(self, request: AccessRequestV1, actor: PersonPrincipal | None) -> None:
        ref = (
            SubjectRefV1(kind=SubjectKind.USER, id=actor.person_id)
            if actor
            else SubjectRefV1(kind=SubjectKind.TEAM, id=self.service.team_id)
        )
        for grant in self._grants(request):
            if grant.status is GrantStatus.PENDING:
                self.service.store.set_status(grant.grant_id, GrantStatus.DENIED, actor=ref, at=self.service.clock())

    def _undo(self, grants: list[GrantV1], actor: SubjectRefV1) -> None:
        for grant in grants:
            self.service.revoke(grant.grant_id, actor=actor)

    def _finish(self, finished: AccessRequestV1, created: list[GrantV1]) -> AccessRequestV1:
        try:
            self.service.store.finish_request(finished)
        except AccessStoreConflict as exc:
            self._undo(created, finished.decided_by or SubjectRefV1(kind=SubjectKind.TEAM, id=self.service.team_id))
            raise HandDownRefused(str(exc)) from exc
        logger.info(
            "Access request decided. event=access_request_decided request_id=%s status=%s decision=%s by=%s grants=%s",
            finished.request_id,
            finished.status.value,
            finished.decision.value if finished.decision else None,
            finished.decided_by.id if finished.decided_by else None,
            ",".join(finished.grant_ids),
        )
        self.inbox.request_finished(finished)
        return finished

    def close_requests_saved_before_work_ids(self) -> list[str]:
        """Withdraw waiting requests for one task saved before the upgrade (the team lead's startup check).

        Their tasks ended with the restart, so nobody can approve them
        (``before_work_ids``); each one is logged as a warning.
        """

        closed: list[str] = []
        for request in self.service.store.list_requests(self.service.team_id, status=AccessRequestStatus.WAITING):
            if not before_work_ids(request):
                continue
            logger.warning(
                "A waiting request for one task was saved before the upgrade; withdrawing it. "
                "event=access_request_before_work_ids_withdrawn request_id=%s requester=%s target=%s task_id=%s",
                request.request_id,
                request.requester_name,
                request.target.id,
                request.task_id,
            )
            self.withdraw(request.request_id, why=SAVED_BEFORE_WORK_IDS)
            closed.append(request.request_id)
        return closed

    def withdraw(self, request_id: str, *, why: str) -> AccessRequestV1:
        """The waiting task ended first; close the review."""

        request = self.waiting_request(request_id)
        self._deny_pending(request, None)
        return self._finish(
            request.model_copy(
                update={
                    "status": AccessRequestStatus.WITHDRAWN,
                    "decision_note": why,
                    "decided_at_utc": self.service.clock(),
                }
            ),
            [],
        )

    # -- agents handing on, restricting and revoking ---------------------------

    def give(
        self,
        grantor: AgentPrincipal,
        *,
        recipient: SubjectRefV1,
        target: TargetRefV1,
        level: AccessLevel,
        reason: str,
        lifetime: GrantLifetime = GrantLifetime.UNTIL_REVOKED,
        hours: int | None = None,
        task_id: str | None = None,
        person: PersonPrincipal | None = None,
        conversation_id: str | None = None,
    ) -> RequestOutcome:
        """Hand on access the grantor holds; waits for a person under "ask me each time".

        The recipient is a schedule (a scheduled job's access, given when the
        job is set up) or an agent type for one task; workers keep no lasting
        access of their own (``AccessService.worker_lasting_refusal``).
        """

        expires_at = self._check_request(target, lifetime, task_id, hours)
        recipient_name = self.subject_name(recipient)
        grant = self.service.hand_down(
            grantor,
            self._spec(
                subject=recipient,
                target=target,
                level=level,
                lifetime=lifetime,
                task_id=task_id,
                granted_by=grantor.type_subject(),
                reason=reason,
                expires_at=expires_at,
            ),
            task_id=task_id,
            person_id=person.person_id if person else None,
        )
        name = self.target_name(target)
        if grant.status is GrantStatus.ACTIVE:
            self._notify_hand_down(grant)
            return RequestOutcome(
                f"{recipient_name} now has {LEVEL_WORDS[level]} access to {name} (grant {grant.grant_id}).",
                grant=grant,
            )
        request = AccessRequestV1(
            request_id=new_id("access_req"),
            team_id=self.service.team_id,
            requester_agent_id=grantor.agent_id,
            requester_agent_type=grantor.agent_type,
            person_id=person.person_id if person else None,
            subject=recipient,
            target=target,
            level=level,
            lifetime=lifetime,
            expires_at_utc=expires_at,
            reason=reason,
            task_id=task_id,
            # give's task_id is the request a task grant is bound to.
            work_id=task_id,
            schedule_id=recipient.id.split("/", 1)[-1] if recipient.kind is SubjectKind.SCHEDULE else None,
            conversation_id=conversation_id,
            grant_ids=(grant.grant_id,),
            created_at_utc=self.service.clock(),
        )
        outcome = self._wait_for_review(request)
        return RequestOutcome(
            f"Giving {recipient_name} {LEVEL_WORDS[level]} access to {name} waits for a person's approval, "
            "because your access says to ask each time.",
            request=outcome.request,
            grant=grant,
        )

    def restrict(
        self,
        grantor: AgentPrincipal,
        *,
        recipient_agent_type: str,
        target: TargetRefV1,
        reason: str,
        task_id: str | None = None,
        person_id: str | None = None,
    ) -> GrantV1:
        """Block a worker from a target the grantor could have given it (section 4.8)."""

        if recipient_agent_type == grantor.agent_type:
            raise HandDownRefused("Restrict other agents; to stop using a tool yourself, just do not call it.")
        if self.service.agent_role_of(recipient_agent_type) != AGENT_ROLE_WORKER:
            raise HandDownRefused("Agents can restrict only workers; people restrict team leads and system agents.")
        if not self.service.may_give(grantor, target, AccessLevel.READ, task_id=task_id, person_id=person_id):
            raise HandDownRefused(
                f"You can restrict only access you could give; you cannot give access to {self.target_name(target)}."
            )
        now = self.service.clock()
        block = GrantV1(
            grant_id=new_id("grant"),
            team_id=self.service.team_id,
            kind=GrantKind.BLOCK,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(self.service.team_id, recipient_agent_type)),
            target=target,
            granted_by=grantor.type_subject(),
            reason=reason,
            created_at_utc=now,
        )
        created = self.service.store.create(block)
        logger.info(
            "Access restricted. event=access_restricted grant_id=%s by=%s recipient=%s target=%s",
            created.grant_id,
            grantor.agent_type,
            recipient_agent_type,
            target.id,
        )
        return created

    # -- people blocking and unblocking ------------------------------------------

    def block(
        self,
        person: PersonPrincipal,
        *,
        subject: SubjectRefV1,
        target: TargetRefV1,
        reason: str,
        hours: int | None = None,
        replace_block_id: str | None = None,
    ) -> GrantV1:
        """A person blocks an agent type, an agent or a person from a tool or operation.

        The person must be able to give access to the target (the access
        service decides: administrators for team tools, or a person holding
        access they may hand on). Blocking a person is administering people,
        so only owners and administrators may, and nobody blocks themselves.
        Replacing a stale operation block narrows an existing restriction:
        only whoever may remove it can do that, atomically and for the same
        subject and tool, keeping its original expiry.
        """

        if subject.kind not in BLOCKABLE_SUBJECTS:
            raise HandDownRefused("Block an agent type, one agent, a person, a role or the whole team.")
        if target.kind not in BLOCKABLE_TARGETS:
            raise HandDownRefused("Block a tool set, a tool, one of its operations, a folder or a file.")
        if not str(reason or "").strip():
            raise ValueError("Say why you are blocking it; people read the reason.")
        if hours is not None and not 1 <= hours <= MAX_REQUEST_HOURS:
            raise ValueError(f"A block for a limited time needs hours between 1 and {MAX_REQUEST_HOURS}.")
        # Raises HandDownRefused for targets outside the catalog.
        self.service.target_entities_for_ref(target)
        name = self.target_name(target)
        previous = self._replacement_block(person, replace_block_id, subject, target) if replace_block_id else None
        if previous is not None and hours is not None:
            raise ValueError("A replacement keeps the old block's expiry; leave out --hours.")
        if previous is None and subject.kind is SubjectKind.USER and subject.id == person.person_id:
            raise HandDownRefused("You cannot block yourself. Block the agent types you want to stop instead.")
        if previous is None and subject.kind in PEOPLE_SUBJECTS and not is_administrator(person.roles):
            raise HandDownRefused("Only an owner or an administrator can block a person, a role or the whole team.")
        if previous is None and not self.service.may_give(person, target, AccessLevel.READ, task_id=None):
            raise HandDownRefused(f"You can block only access you could give, and you cannot give access to {name}.")
        now = self.service.clock()
        block = GrantV1(
            grant_id=new_id("grant"),
            team_id=self.service.team_id,
            kind=GrantKind.BLOCK,
            subject=subject,
            target=target,
            lifetime=previous.lifetime if previous else (GrantLifetime.UNTIL if hours else GrantLifetime.UNTIL_REVOKED),
            expires_at_utc=previous.expires_at_utc if previous else (now + timedelta(hours=hours) if hours else None),
            task_id=previous.task_id if previous else None,
            uses_remaining=previous.uses_remaining if previous else None,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=person.person_id),
            reason=str(reason).strip(),
            created_at_utc=now,
        )
        created = self.service.store.replace_block(previous, block) if previous else self.service.store.create(block)
        logger.warning(
            "A person blocked access. event=access_blocked grant_id=%s by=%s subject=%s:%s target=%s expires_at=%s "
            "replaced_block_id=%s",
            created.grant_id,
            person.person_id,
            subject.kind.value,
            subject.id,
            target.id,
            created.expires_at_utc.isoformat() if created.expires_at_utc else None,
            replace_block_id,
        )
        return created

    def _replacement_block(
        self, person: PersonPrincipal, grant_id: str, subject: SubjectRefV1, target: TargetRefV1
    ) -> GrantV1:
        """Removal authority permits narrowing a stale block, never changing whom or what it covers."""

        previous = self._removable_block(person, grant_id)
        if previous.expires_at_utc is not None and previous.expires_at_utc <= self.service.clock():
            raise HandDownRefused("That block has expired; add a new block instead.")
        if missing_operation(self.service.catalog.tools, previous.target) is None:
            raise HandDownRefused("Replace only a block on an operation the tool no longer has.")
        if (
            subject != previous.subject
            or target.kind is not TargetKind.OPERATION
            or target.id.partition("#")[0] != previous.target.id.partition("#")[0]
        ):
            raise HandDownRefused("A replacement must block the same subject from a current operation of the same tool.")
        return previous

    @staticmethod
    def may_unblock(person: PersonPrincipal, block: GrantV1) -> bool:
        """The person who added a block, or an owner or administrator (see ``unblock``)."""

        return block.granted_by == SubjectRefV1(kind=SubjectKind.USER, id=person.person_id) or is_administrator(
            person.roles
        )

    def unblock(self, person: PersonPrincipal, grant_id: str) -> GrantV1:
        """Remove a block: the person who added it, or an owner or administrator.

        Removing a block widens access again, so the rule is narrower than for
        revoking a grant: holding access one could give is not enough, and an
        administrator who is blocked from the same tool can still remove it.
        """

        grant = self._removable_block(person, grant_id)
        actor = SubjectRefV1(kind=SubjectKind.USER, id=person.person_id)
        self.service.store.revoke(grant_id, actor=actor, at=self.service.clock())
        logger.warning(
            "A person removed a block. event=access_unblocked grant_id=%s by=%s subject=%s:%s target=%s",
            grant_id,
            person.person_id,
            grant.subject.kind.value,
            grant.subject.id,
            grant.target.id,
        )
        return self.service.store.get(grant_id) or grant

    def _removable_block(self, person: PersonPrincipal, grant_id: str) -> GrantV1:
        grant = self.service.store.get(grant_id)
        if grant is None or grant.team_id != self.service.team_id or grant.kind is not GrantKind.BLOCK:
            raise HandDownRefused("There is no such block in this team.")
        if grant.status is not GrantStatus.ACTIVE:
            raise HandDownRefused(f"That block was already {grant.status.value}.")
        if not self.may_unblock(person, grant):
            raise HandDownRefused("Only the person who added this block, or an owner or administrator, can remove it.")
        return grant

    def revoke(self, grantor: AgentPrincipal, grant_id: str) -> list[str]:
        """Revoke access or a restriction the grantor's agent type gave.

        Access goes as a whole entry (``access_control.grant_entries``): every
        grant in it that the grantor's type gave. Parts a person gave stay,
        and the grantor is told to ask a person about them.
        """

        grant = self.service.store.get(grant_id)
        if grant is None or grant.team_id != self.service.team_id or is_standard_grant(grant_id):
            raise HandDownRefused("There is no such grant in this team.")
        if grant.granted_by != grantor.type_subject():
            raise HandDownRefused("You can revoke only access your agent type gave; ask a person for anything else.")
        entry = self.service.entry_grants(grant)
        own = [held.grant_id for held in entry if held.granted_by == grantor.type_subject()]
        others = [held.grant_id for held in entry if held.granted_by != grantor.type_subject()]
        if others:
            logger.info(
                "An agent removed the access it gave; a person's grants in the same entry stay. "
                "event=access_entry_partly_revoked by=%s revoked=%s remaining=%s",
                grantor.agent_type,
                ",".join(own),
                ",".join(others),
            )
        return self.service.store.revoke_all(
            self.service.team_id, own, actor=grantor.type_subject(), at=self.service.clock()
        )


__all__ = [
    "BLOCKABLE_SUBJECTS",
    "BLOCKABLE_TARGETS",
    "LEVEL_WORDS",
    "ACCESS_PROPOSAL_CHANGE",
    "APPLIED_AT_DECISION",
    "AccessInbox",
    "AccessRequests",
    "ChangeApplier",
    "FILE_DELIVERY_CHANGE",
    "RequestOutcome",
    "RequestWatcher",
]
