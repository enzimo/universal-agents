"""The access decision service: one answer for every enforcement point.

For one operation (a catalog resolution: tool, operation, level) it:

1. collects the grants valid *now* for the agent's subjects (the agent, its
   type, its role, its team): active, unexpired, uses left, bound to this task
   when task-bound, and every grant up the parent chain valid too;
2. links one Cedar template per valid grant and asks for ``use.<level>`` on the
   operation (the agent side);
3. asks the same for the person the task works for (the person side, decision
   D6), so an agent never exceeds its person; a personal-login tool counts as
   the person's own there;
4. consumes one-time grants that justified the agent side, immediately before
   the effect; a use taken for one tool call is bound to that call
   (``operation_id``), so when Strands runs a paused call again as it resumes
   and the call is decided again, it keeps its use and takes no second one,
   while every other call finds the grant used up;
5. records the decision and returns a reason with a next step.

Any evaluation error denies. Hand-downs follow the plan's rules (section 5.2):
Cedar proves the grantor holds delegable access covering the target, and
trusted code checks that the child only narrows a covering parent, trying every
covering parent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from access_catalog.catalog import ToolCatalog
from access_control.cedar import AccessEvaluationError, CedarAccessClient
from access_control.folders import FOLDER_ROOT_USERS, covers, folder_chain, folder_owner, grantable_folder_id
from access_control.grant_entries import GrantPlan, entry_key, plan_grants
from access_control.people import is_administrator
from access_control.standard_grants import is_standard_grant, standard_grants
from access_control.stale_blocks import decided_grant
from access_control.entities import (
    AGENT_ROLE_SYSTEM,
    AGENT_ROLE_TEAM_LEAD,
    AGENT_ROLE_WORKER,
    agent_role_resolver,
    AgentPrincipal,
    PersonPrincipal,
    agent_entities,
    folder_entities,
    grant_links,
    person_entities,
    target_entities,
    target_uid,
    uid,
)
from custom_messages.contracts.access_v2 import (
    LIFETIME_RANK,
    TASK_BOUND_LIFETIMES,
    AccessDecisionV1,
    AccessLevel,
    AccountMode,
    ClassificationSource,
    DelegationMode,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    OperationResolutionV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from id_generation import new_id
from storage.access_repository import AccessRepository, AccessStoreConflict

logger = logging.getLogger(__name__)

# The tool id recorded on file and folder decisions (the catalog's file tools).
FILES_TOOL_ID = "fn:files"
# Running code outside any sandbox (decision D9): the code sandbox launcher
# (custom_agents.code_access) and jobs on a host-execution profile
# (runtime_hosting.authorization) decide this operation at full.
UNCONFINED_EXECUTION_TOOL_ID = "code:unconfined"
UNCONFINED_EXECUTION_OPERATION = "run"
# Ids of the per-decision copies of what people shared with the task's person.
SHARED_GRANT_PREFIX = "shared-"


class AccessDenied(PermissionError):
    """An access check denied the operation; ``decision`` says why."""

    def __init__(self, decision: AccessDecisionV1) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class HandDownRefused(PermissionError):
    """A grant could not be created or handed down; the message says why."""


@dataclass(frozen=True)
class _SideResult:
    allowed: bool
    grant_ids: tuple[str, ...]
    blocking: tuple[str, ...]
    error: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AccessService:
    """Decides, creates hand-downs, and revokes, for one team."""

    store: AccessRepository
    catalog: ToolCatalog
    cedar: CedarAccessClient
    team_id: str
    clock: Callable[[], datetime] = field(default=_now)
    # The role an agent type holds (team_lead or worker); callers pass the
    # configured lead type through entities.agent_role_resolver.
    agent_role_of: Callable[[str], str] = field(default_factory=lambda: agent_role_resolver("TeamArchitect"))
    # The team lead's agent type: the one holder of lasting access for a
    # person, and where workers' task access comes from (hand_on_for_task).
    lead_agent_type: str = "TeamArchitect"

    # -- validity -----------------------------------------------------------------

    def _valid(
        self,
        grant: GrantV1,
        *,
        now: datetime,
        task_id: str | None,
        person_id: str | None,
        depth: int = 0,
        operation_id: str | None = None,
    ) -> bool:
        if depth > 8 or grant.status is not GrantStatus.ACTIVE:
            return False
        # Decision D13: a grant for one person's work is valid only while the
        # task works for that person.
        if grant.for_person_id is not None and grant.for_person_id != person_id:
            return False
        if grant.expires_at_utc is not None and now >= grant.expires_at_utc:
            return False
        if grant.uses_remaining is not None and grant.uses_remaining <= 0 and not self._use_held(grant, operation_id):
            return False
        if grant.lifetime in {GrantLifetime.TASK, GrantLifetime.ONE_TIME} and grant.task_id != task_id:
            return False
        if grant.parent_grant_id is None:
            return True
        parent = self.store.get(grant.parent_grant_id)
        if parent is None:
            return False
        # A task-bound child of a parent without a task keeps the child's task.
        # A parent is never one-time (one-time grants are never delegable), so
        # the call's uses do not apply to it.
        return self._valid(
            parent,
            now=now,
            task_id=parent.task_id or task_id,
            person_id=person_id,
            depth=depth + 1,
        )

    def _use_held(self, grant: GrantV1, operation_id: str | None) -> bool:
        """Whether this tool call already took a use of the grant (``AccessRepository.consume``)."""

        return operation_id is not None and self.store.use_held(grant.grant_id, operation_id)

    def valid_grants(
        self,
        subjects: Iterable[SubjectRefV1],
        *,
        task_id: str | None,
        person_id: str | None = None,
        now: datetime | None = None,
        operation_id: str | None = None,
    ) -> list[GrantV1]:
        """Grants valid now for the subjects, on a task working for ``person_id``.

        For an agent, ``person_id`` is the person its task works for (None for
        work for nobody): grants for another person's work are left out
        (decision D13). A person's own grants never name a person to work for.
        Agents' grants come from ``agent_grants``, which also applies where
        an agent's access comes from ("access follows the work").
        ``operation_id`` (one tool call, see ``decide``) keeps a used-up grant
        valid for the call that took its use.
        """

        moment = now or self.clock()
        wanted = list(subjects)
        standard = [grant for grant in standard_grants(self.team_id) if grant.subject in wanted]
        return standard + [
            grant
            for grant in self.store.for_subjects(self.team_id, wanted)
            if self._valid(grant, now=moment, task_id=task_id, person_id=person_id, operation_id=operation_id)
        ]

    def _principal_grants(
        self,
        principal: AgentPrincipal | PersonPrincipal,
        *,
        task_id: str | None,
        person_id: str | None,
        operation_id: str | None = None,
    ) -> list[GrantV1]:
        """Valid grants of an agent (on work for ``person_id``) or of a person."""

        if isinstance(principal, AgentPrincipal):
            return self.agent_grants(principal, task_id=task_id, person_id=person_id, operation_id=operation_id)
        return self.valid_grants(principal.subjects(), task_id=task_id)

    def agent_grants(
        self,
        agent: AgentPrincipal,
        *,
        task_id: str | None,
        person_id: str | None,
        now: datetime | None = None,
        operation_id: str | None = None,
    ) -> list[GrantV1]:
        """The grants an agent's current work counts ("access follows the work", owner's decision 2026-09-30).

        An agent's access is its role's standard access, the team's defaults
        (access given for everyone's work) and what its current task was given,
        plus the lasting access of whom it works for: the team lead's lasting
        access for a person counts in all the team lead's work for that person
        (the person gave it to the team lead, also for its scheduled runs); in
        a scheduled job's run the schedule's access counts as well (the agent
        is in the schedule only then). Lasting access for one person's work
        never counts for a worker: a worker inherits what the team lead hands
        on to its task (``hand_on_for_task``), or, in a job run, the
        schedule's access. Blocks always count. ``operation_id`` as in
        ``valid_grants``.
        """

        lead = agent.role == AGENT_ROLE_TEAM_LEAD
        return [
            grant
            for grant in self.valid_grants(
                agent.subjects(), task_id=task_id, person_id=person_id, now=now, operation_id=operation_id
            )
            if lead or not _lasting_consent(grant)
        ]

    def shared_with(self, agent: AgentPrincipal, person: PersonPrincipal | None, task_id: str | None) -> list[GrantV1]:
        """What people shared with the task's person, as grants for the agent (decision D15).

        An agent may use the folder of the person its task works for
        (``std:task-person-folder``); in the same way it may use what other
        people shared with that person, at the shared level. These copies live
        for one decision only; blocks on the agent still apply to them.
        """

        if person is None:
            return []
        agent_subject = SubjectRefV1(kind=SubjectKind.AGENT, id=agent.agent_id)
        return [
            grant.model_copy(update={"grant_id": f"{SHARED_GRANT_PREFIX}{grant.grant_id}", "subject": agent_subject})
            for grant in self.valid_grants(person.subjects(), task_id=task_id)
            if grant.kind is GrantKind.ACCESS
            and grant.target.kind in {TargetKind.FOLDER, TargetKind.FILE}
            and grant.target.id.split("/", 1)[0] == FOLDER_ROOT_USERS
        ]

    # -- evaluation -------------------------------------------------------------

    def _evaluate(
        self,
        *,
        principal: dict[str, str],
        principal_entities: list[dict[str, Any]],
        grants: list[GrantV1],
        action: str,
        resource: dict[str, str],
        resource_entities: list[dict[str, Any]],
        task_id: str | None,
        context_person: str | None = None,
    ) -> _SideResult:
        # A block on an operation the catalog no longer has blocks its whole
        # tool (access_control.stale_blocks), so a rename never lifts it.
        links = [link for grant in grants for link in grant_links(decided_grant(self.catalog, grant))]
        entities = _merge_entities(principal_entities + resource_entities)
        context: dict[str, Any] = {"taskId": task_id} if task_id else {}
        if context_person:
            # Who the task works for, from its signed context (std:task-person-folder).
            context["person"] = {"__entity": uid("User", context_person)}
        try:
            decision = self.cedar.decide(
                links=links,
                entities=entities,
                principal=principal,
                action=action,
                resource=resource,
                context=context,
            )
        except AccessEvaluationError as exc:
            logger.error("Access evaluation failed; denying. event=access_evaluation_failed error=%s", exc)
            return _SideResult(False, (), (), error=str(exc))
        grant_ids = tuple(
            item.split(":", 1)[1] for item in decision.determining_policy_ids if item.startswith(("grant:", "deleg:", "cred:"))
        )
        blocking = tuple(
            item for item in decision.determining_policy_ids
            if not decision.allowed and (item.startswith("block:") or item.startswith(("guard:", "std:team")))
        )
        if decision.errors:
            logger.error(
                "Access evaluation reported errors; denying. event=access_evaluation_errors errors=%s",
                "; ".join(decision.errors)[:500],
            )
        return _SideResult(decision.allowed, grant_ids, blocking, error="; ".join(decision.errors))

    def _agent_side(
        self,
        agent: AgentPrincipal,
        resolution: OperationResolutionV1,
        task_id: str | None,
        person_id: str | None,
        operation_id: str | None = None,
    ) -> tuple[_SideResult, list[GrantV1]]:
        grants = self._principal_grants(agent, task_id=task_id, person_id=person_id, operation_id=operation_id)
        resource, entities = target_entities(self.catalog, self.team_id, resolution)
        result = self._evaluate(
            principal=agent.uid(),
            principal_entities=agent_entities(agent),
            grants=grants,
            action=f"use.{resolution.level.value}",
            resource=resource,
            resource_entities=entities,
            task_id=task_id,
        )
        return result, grants

    def _person_side(self, person: PersonPrincipal, resolution: OperationResolutionV1, task_id: str | None) -> _SideResult:
        tool = self.catalog.tools.get(resolution.tool_id)
        personal = tool is not None and tool.account is AccountMode.PERSONAL_LOGIN
        resource, entities = target_entities(
            self.catalog, self.team_id, resolution, owner=person.person_id if personal else None
        )
        return self._evaluate(
            principal=person.uid(),
            principal_entities=person_entities(person),
            grants=self.valid_grants(person.subjects(), task_id=task_id),
            action=f"use.{resolution.level.value}",
            resource=resource,
            resource_entities=entities,
            task_id=task_id,
        )

    def decide(
        self,
        *,
        agent: AgentPrincipal | None,
        person: PersonPrincipal | None,
        resolution: OperationResolutionV1,
        task_id: str | None,
        consume: bool = True,
        hand_on_for_task: bool = False,
        operation_id: str | None = None,
    ) -> AccessDecisionV1:
        """Decide one operation for an agent working for a person (or a person alone).

        ``hand_on_for_task`` (the decision points for agents' calls): when a
        worker lacks access its task needs and the team lead may hand it on
        for this person's work, trusted code adds it to the worker's task and
        decides again (``hand_on_for_task``), with no extra turn for the agent.

        ``operation_id`` names the one tool call decided
        (``custom_agents.access_runtime.call_operation_id``). A one-time use
        it takes is bound to it, and a decision of the same call again (a
        paused call that Strands runs again as it resumes) counts the grant
        whose use it holds and takes no second use. ``consumed_grant_ids``
        then names that grant too: the decision rests on a one-time use.
        Without an id, each decision that relies on a one-time grant takes its
        own use, as at the broker, where every request is decided once.
        """

        tool = self.catalog.tools.get(resolution.tool_id)
        tool_name = tool.display_name if tool is not None else resolution.tool_id
        base = {"tool_id": resolution.tool_id, "operation": resolution.operation, "level": resolution.level}
        if tool is None:
            return self._finish(
                AccessDecisionV1(
                    allowed=False,
                    **base,
                    reason=f"{resolution.tool_id} is not in the tool catalog, so nobody can be given access to it.",
                    next_step="Ask TeamLead to propose a tool definition for it.",
                ),
                agent,
                person,
                task_id,
            )
        # Work with no person runs only for built-in system agents, on the agent
        # side alone (their fixed duties); anything else needs an accountable person.
        system_work = person is None and agent is not None and agent.role == AGENT_ROLE_SYSTEM
        if person is None and not system_work:
            return self._finish(
                AccessDecisionV1(
                    allowed=False,
                    **base,
                    reason="This task works for no person, so no access can be checked against one.",
                    next_step="Run it for a person; only built-in system agents work without one.",
                ),
                agent,
                person,
                task_id,
            )
        agent_result: _SideResult | None = None
        agent_grants: list[GrantV1] = []
        if agent is not None:
            agent_result, agent_grants = self._agent_side(
                agent, resolution, task_id, _person_id(person), operation_id
            )
            if (
                hand_on_for_task
                and not agent_result.allowed
                and not agent_result.blocking
                and self.hand_on_for_task(
                    agent, person, TargetRefV1(kind=TargetKind.TOOL, id=resolution.tool_id), resolution.level, task_id
                )
                is not None
            ):
                agent_result, agent_grants = self._agent_side(
                    agent, resolution, task_id, _person_id(person), operation_id
                )
            if not agent_result.allowed:
                blocked = _blocks(agent_result.blocking)
                return self._finish(
                    AccessDecisionV1(
                        allowed=False,
                        **base,
                        reason=(
                            f"{agent.agent_type} needs {resolution.level.value} access to {tool_name} "
                            f"for {resolution.operation}."
                            + (" It is blocked." if agent_result.blocking else "")
                            + (" The access check could not run." if agent_result.error else "")
                        ),
                        next_step=(
                            # A grant never overrides a block, so asking again cannot help.
                            f"A block stops {agent.agent_type} from using {tool_name}; asking for access "
                            "will not change that. Tell the person you work for; only a person can "
                            "remove the block."
                            if blocked
                            else f"Call request_access with tool_id={resolution.tool_id!r} and "
                            f"level={resolution.level.value!r}, saying why you need it; TeamLead or a "
                            f"person who can give {resolution.level.value} access to {tool_name} decides."
                        ),
                        blocking_policy_ids=agent_result.blocking,
                    ),
                    agent,
                    person,
                    task_id,
                )
        person_result = (
            _SideResult(True, (), ()) if person is None else self._person_side(person, resolution, task_id)
        )
        if not person_result.allowed:
            person_blocked = bool(_blocks(person_result.blocking))
            return self._finish(
                AccessDecisionV1(
                    allowed=False,
                    **base,
                    reason=(
                        f"The person this task works for is blocked from {tool_name}, and agents never "
                        "exceed their person."
                        if person_blocked
                        else f"The person this task works for does not have {resolution.level.value} access to "
                        f"{tool_name}, and agents never exceed their person."
                    ),
                    next_step=(
                        f"Ask a team administrator to remove the block on {tool_name} for them."
                        if person_blocked
                        else f"Ask a team administrator to give them {resolution.level.value} access to {tool_name}."
                    ),
                    agent_grant_ids=agent_result.grant_ids if agent_result else (),
                    blocking_policy_ids=person_result.blocking,
                ),
                agent,
                person,
                task_id,
            )
        consumed: tuple[str, ...] = ()
        if consume and agent_result is not None:
            try:
                consumed = self._consume(agent_result.grant_ids, agent_grants, operation_id=operation_id)
            except AccessStoreConflict as exc:
                return self._finish(
                    AccessDecisionV1(
                        allowed=False,
                        **base,
                        reason=f"The one-time access for this was already used ({exc}).",
                        next_step="Ask again for access.",
                    ),
                    agent,
                    person,
                    task_id,
                )
        decision = AccessDecisionV1(
            allowed=True,
            **base,
            reason="allowed",
            agent_grant_ids=agent_result.grant_ids if agent_result else (),
            person_grant_ids=person_result.grant_ids,
            consumed_grant_ids=consumed,
        )
        return self._finish(decision, agent, person, task_id)

    def hand_on_sources(self, agent: AgentPrincipal, person_id: str | None) -> list[GrantV1]:
        """The team lead's access a worker's delegated task can draw on without asking (``hand_on_for_task``).

        Empty for anyone but a worker on a task for a person outside a
        scheduled job's runs (``task_identity.job_schedule_id``). The worker's tool list shows these tools, so the first call to
        one adds it to the task; the call is decided as usual.
        """

        if agent.role != AGENT_ROLE_WORKER or agent.schedule_id is not None or person_id is None:
            return []
        lead = AgentPrincipal(self.lead_agent_type, self.lead_agent_type, AGENT_ROLE_TEAM_LEAD, self.team_id)
        return [
            grant
            for grant in self.agent_grants(lead, task_id=None, person_id=person_id)
            if grant.kind is GrantKind.ACCESS
            and grant.delegation_depth >= 1
            and grant.delegation_mode is DelegationMode.AUTOMATIC
            and not is_standard_grant(grant.grant_id)
        ]

    def _task_hand_on_spec(
        self,
        agent: AgentPrincipal,
        person: PersonPrincipal | None,
        target: TargetRefV1,
        level: AccessLevel,
        task_id: str | None,
    ) -> tuple[AgentPrincipal, GrantV1] | None:
        """The bounded grant a delegated task needs; identity rules are shared by preview and decision."""

        if agent.role != AGENT_ROLE_WORKER or agent.schedule_id is not None or person is None or not task_id:
            return None
        if target.kind is TargetKind.TOOL and target.id not in self.catalog.tools:
            return None
        lead = AgentPrincipal(self.lead_agent_type, self.lead_agent_type, AGENT_ROLE_TEAM_LEAD, self.team_id)
        return lead, GrantV1(
            grant_id="new",
            team_id=self.team_id,
            subject=agent.type_subject(),
            target=target,
            level=level,
            lifetime=GrantLifetime.TASK,
            task_id=task_id,
            granted_by=lead.type_subject(),
            reason=f"{agent.agent_type}'s task needed it; {self.lead_agent_type} may hand it on.",
            created_at_utc=self.clock(),
        )

    def may_hand_on_for_task(
        self,
        agent: AgentPrincipal,
        person: PersonPrincipal | None,
        target: TargetRefV1,
        level: AccessLevel,
        task_id: str | None,
    ) -> bool:
        """Read-only eligibility for showing/loading a tool before its first call.

        Use the actual hand-down preparation and Cedar checks, including the
        worker's blocks and the person's access. The candidate exists only in
        memory: no grant, audit event or one-time use is written. Every actual
        call must still reach the decision, which rechecks and stores access.
        """

        prepared = self._task_hand_on_spec(agent, person, target, level, task_id)
        if prepared is None:
            return False
        lead, spec = prepared
        try:
            child = self._prepare_hand_down(lead, spec, task_id=task_id, person_id=person.person_id, automatic_only=True)
        except HandDownRefused:
            return False
        grants = self.agent_grants(agent, task_id=task_id, person_id=person.person_id)
        return self.holds(person, target, level, task_id=task_id) and self.holds(
            agent, target, level, task_id=task_id, person_id=person.person_id, grants=[*grants, child]
        )

    def hand_on_for_task(
        self,
        agent: AgentPrincipal,
        person: PersonPrincipal | None,
        target: TargetRefV1,
        level: AccessLevel,
        task_id: str | None,
    ) -> GrantV1 | None:
        """Add access the team lead may hand on to a worker's task ("access follows the work").

        Only for a worker on a task delegated for a person, outside a
        scheduled job's runs (there the worker works for the schedule, whose
        access is its source). The access is the
        whole tool (or the folder used) at the level the call needs, for this
        task only; returns the grant, or None when the team lead may not hand it
        on without asking (the worker then asks with request_access and a person
        decides). Not announced: the person's consent is the team lead's lasting
        access it comes from.
        """

        prepared = self._task_hand_on_spec(agent, person, target, level, task_id)
        if prepared is None:
            return None
        lead, spec = prepared
        try:
            grant = self.hand_down(lead, spec, task_id=task_id, person_id=person.person_id, automatic_only=True)
        except HandDownRefused as exc:
            logger.info(
                "The team lead may not hand this on to the worker's task; the worker can ask. "
                "event=task_hand_on_refused agent_type=%s target=%s level=%s task_id=%s why=%s",
                agent.agent_type,
                target.id,
                level.value,
                task_id,
                exc,
            )
            return None
        logger.info(
            "Access added to a worker's task from what the team lead may hand on. event=task_hand_on "
            "agent_type=%s target=%s level=%s task_id=%s grant_id=%s",
            agent.agent_type,
            target.id,
            level.value,
            task_id,
            grant.grant_id,
        )
        return grant

    def _consume(
        self, determining: tuple[str, ...], grants: list[GrantV1], *, operation_id: str | None = None
    ) -> tuple[str, ...]:
        """Use one-time grants that justified the decision (raises if one is used up).

        A use the call (``operation_id``) already holds is kept, not taken
        again; it is still named in the result.
        """

        by_id = {grant.grant_id: grant for grant in grants}
        consumed: list[str] = []
        for grant_id in dict.fromkeys(determining):
            grant = by_id.get(grant_id)
            if grant is None or grant.uses_remaining is None:
                continue
            if not self.store.consume(grant_id, at=self.clock(), operation_id=operation_id):
                logger.info(
                    "A tool call decided again keeps the one-time use it took. event=one_time_use_kept "
                    "grant_id=%s operation_id=%s",
                    grant_id,
                    operation_id,
                )
            consumed.append(grant_id)
        return tuple(consumed)

    def _finish(
        self,
        decision: AccessDecisionV1,
        agent: AgentPrincipal | None,
        person: PersonPrincipal | None,
        task_id: str | None,
    ) -> AccessDecisionV1:
        detail = {
            **decision.model_dump(mode="json"),
            "agent_id": agent.agent_id if agent else None,
            "agent_type": agent.agent_type if agent else None,
            "person_id": person.person_id if person else None,
            "task_id": task_id,
        }
        try:
            self.store.record_decision(self.team_id, detail, at=self.clock())
        except Exception:  # noqa: BLE001 - audit failure must not hide the decision
            logger.exception("Could not record an access decision. event=access_decision_record_failed")
        log = logger.info if decision.allowed else logger.warning
        log(
            "Access decision. event=access_decision allowed=%s tool_id=%s operation=%s level=%s agent_id=%s person_id=%s task_id=%s",
            decision.allowed,
            decision.tool_id,
            decision.operation,
            decision.level.value,
            detail["agent_id"],
            detail["person_id"],
            task_id,
        )
        return decision

    def require(self, **kwargs: Any) -> AccessDecisionV1:
        decision = self.decide(**kwargs)
        if not decision.allowed:
            raise AccessDenied(decision)
        return decision

    def decide_folder(
        self,
        *,
        agent: AgentPrincipal | None,
        person: PersonPrincipal | None,
        target_id: str,
        is_file: bool,
        level: AccessLevel,
        task_id: str | None,
        consume: bool = True,
        hand_on_for_task: bool = False,
    ) -> AccessDecisionV1:
        """Decide one file or folder use (``target_id`` from ``access_control.folders``).

        Same order as ``decide``: the agent side, with the task's person in the
        context so the agent may use that person's own folder; then the person
        side, so an agent never exceeds its person. ``hand_on_for_task`` as in
        ``decide``, for the folder used (a file's own folder).
        """

        chain = folder_chain(target_id)
        folder_id = chain[1] if is_file and len(chain) > 1 else chain[0]
        base = {
            "tool_id": FILES_TOOL_ID,
            "operation": f"{'file' if is_file else 'folder'}.{level.value}",
            "level": level,
            "folder_id": chain[0],
        }
        person_id = _person_id(person)
        system_work = person is None and agent is not None and agent.role == AGENT_ROLE_SYSTEM
        if person is None and not system_work:
            return self._finish(
                AccessDecisionV1(
                    allowed=False,
                    **base,
                    reason="This task works for no person, so no access can be checked against one.",
                    next_step="Run it for a person; only built-in system agents work without one.",
                ),
                agent,
                person,
                task_id,
            )
        resource, entities = folder_entities(
            self.team_id, chain[0], is_file=is_file, owner=folder_owner(chain[0], person_id)
        )
        other_person = chain[0].split("/", 1)[0] == FOLDER_ROOT_USERS and folder_owner(chain[0], person_id) != person_id
        agent_result: _SideResult | None = None
        agent_grants: list[GrantV1] = []
        if agent is not None:

            def agent_side() -> tuple[_SideResult, list[GrantV1]]:
                grants = self._principal_grants(agent, task_id=task_id, person_id=person_id)
                grants += self.shared_with(agent, person, task_id)
                result = self._evaluate(
                    principal=agent.uid(),
                    principal_entities=agent_entities(agent),
                    grants=grants,
                    action=f"use.{level.value}",
                    resource=resource,
                    resource_entities=entities,
                    task_id=task_id,
                    context_person=person_id,
                )
                return result, grants

            agent_result, agent_grants = agent_side()
            if (
                hand_on_for_task
                and not agent_result.allowed
                and not agent_result.blocking
                and not other_person
                and self.hand_on_for_task(agent, person, TargetRefV1(kind=TargetKind.FOLDER, id=folder_id), level, task_id)
                is not None
            ):
                agent_result, agent_grants = agent_side()
            if not agent_result.allowed:
                return self._finish(
                    AccessDecisionV1(
                        allowed=False,
                        **base,
                        reason=_folder_refusal(agent.agent_type, level, folder_id, agent_result, other_person),
                        next_step=_folder_next_step(agent.agent_type, level, folder_id, agent_result, other_person),
                        blocking_policy_ids=agent_result.blocking,
                    ),
                    agent,
                    person,
                    task_id,
                )
        person_result = (
            _SideResult(True, (), ())
            if person is None
            else self._evaluate(
                principal=person.uid(),
                principal_entities=person_entities(person),
                grants=self.valid_grants(person.subjects(), task_id=task_id),
                action=f"use.{level.value}",
                resource=resource,
                resource_entities=entities,
                task_id=task_id,
            )
        )
        if not person_result.allowed:
            blocked = bool(_blocks(person_result.blocking))
            return self._finish(
                AccessDecisionV1(
                    allowed=False,
                    **base,
                    reason=(
                        f"The person this task works for is blocked from {folder_id}, and agents never exceed their person."
                        if blocked
                        else f"The person this task works for does not have {level.value} access to {folder_id}, "
                        "and agents never exceed their person."
                    ),
                    next_step=(
                        f"Ask a team administrator to remove the block on {folder_id} for them."
                        if blocked
                        else f"Ask a team administrator to give them {level.value} access to {folder_id}."
                    ),
                    agent_grant_ids=agent_result.grant_ids if agent_result else (),
                    blocking_policy_ids=person_result.blocking,
                ),
                agent,
                person,
                task_id,
            )
        consumed: tuple[str, ...] = ()
        if consume and agent_result is not None:
            try:
                consumed = self._consume(agent_result.grant_ids, agent_grants)
            except AccessStoreConflict as exc:
                return self._finish(
                    AccessDecisionV1(
                        allowed=False,
                        **base,
                        reason=f"The one-time access for this was already used ({exc}).",
                        next_step="Ask again for access.",
                    ),
                    agent,
                    person,
                    task_id,
                )
        return self._finish(
            AccessDecisionV1(
                allowed=True,
                **base,
                reason="allowed",
                agent_grant_ids=agent_result.grant_ids if agent_result else (),
                person_grant_ids=person_result.grant_ids,
                consumed_grant_ids=consumed,
            ),
            agent,
            person,
            task_id,
        )

    def holds(
        self,
        principal: AgentPrincipal | PersonPrincipal,
        target: TargetRefV1,
        level: AccessLevel,
        *,
        task_id: str | None,
        person_id: str | None = None,
        grants: list[GrantV1] | None = None,
    ) -> bool:
        """Whether the agent or person may already use the whole target at the level.

        For an agent, ``person_id`` is the person its task works for (decision
        D13). Pass ``grants`` (from ``valid_grants``) when checking many
        targets at once.
        """

        owner_hint = principal.person_id if isinstance(principal, PersonPrincipal) else person_id
        resource, entities = self.target_entities_for_ref(target, person_id=owner_hint)
        if isinstance(principal, AgentPrincipal):
            principal_entities = agent_entities(principal)
        else:
            principal_entities = person_entities(principal)
        return self._evaluate(
            principal=principal.uid(),
            principal_entities=principal_entities,
            grants=grants if grants is not None else self._principal_grants(principal, task_id=task_id, person_id=person_id),
            action=f"use.{level.value}",
            resource=resource,
            resource_entities=entities,
            task_id=task_id,
            context_person=person_id if isinstance(principal, AgentPrincipal) else None,
        ).allowed

    def blocks_on(
        self,
        principal: AgentPrincipal | PersonPrincipal,
        target: TargetRefV1,
        *,
        task_id: str | None = None,
        person_id: str | None = None,
    ) -> tuple[str, ...]:
        """Ids of the blocks that stop the agent or person from using the target at all.

        A block denies every action, so asking for the lowest level shows it.
        """

        resource, entities = self.target_entities_for_ref(target)
        if isinstance(principal, AgentPrincipal):
            principal_entities = agent_entities(principal)
        else:
            principal_entities = person_entities(principal)
        result = self._evaluate(
            principal=principal.uid(),
            principal_entities=principal_entities,
            grants=self._principal_grants(principal, task_id=task_id, person_id=person_id),
            action=f"use.{AccessLevel.READ.value}",
            resource=resource,
            resource_entities=entities,
            task_id=task_id,
        )
        return _blocks(result.blocking)

    def may_give(
        self,
        grantor: AgentPrincipal | PersonPrincipal,
        target: TargetRefV1,
        level: AccessLevel,
        *,
        task_id: str | None,
        person_id: str | None = None,
    ) -> bool:
        """Whether the agent or person could give (so also restrict) the access.

        For an agent, ``person_id`` is the person its task works for: it may
        hand on only what it holds for that person's work (decision D13).
        """

        if isinstance(grantor, AgentPrincipal):
            principal_entities = agent_entities(grantor)
        else:
            principal_entities = person_entities(grantor)
        return self._delegate_check(
            principal=grantor.uid(),
            principal_entities=principal_entities,
            grants=self._principal_grants(grantor, task_id=task_id, person_id=person_id),
            level=level,
            target=target,
            task_id=task_id,
            owner_hint=grantor.person_id if isinstance(grantor, PersonPrincipal) else None,
        ).allowed

    def chain(self, grant: GrantV1) -> list[GrantV1]:
        """The grant and its parents, root first."""

        chain = [grant]
        while chain[0].parent_grant_id and len(chain) <= 8:
            parent = self.store.get(chain[0].parent_grant_id)
            if parent is None:
                break
            chain.insert(0, parent)
        return chain

    # -- creating grants ---------------------------------------------------------

    def _delegate_check(
        self,
        *,
        principal: dict[str, str],
        principal_entities: list[dict[str, Any]],
        grants: list[GrantV1],
        level: AccessLevel,
        target: TargetRefV1,
        task_id: str | None,
        owner_hint: str | None = None,
    ) -> _SideResult:
        resource, entities = self.target_entities_for_ref(target, person_id=owner_hint)
        return self._evaluate(
            principal=principal,
            principal_entities=principal_entities,
            grants=grants,
            action=f"delegate.{level.value}",
            resource=resource,
            resource_entities=entities,
            task_id=task_id,
        )

    def may_use_folder(
        self,
        *,
        agent: AgentPrincipal,
        person: PersonPrincipal | None,
        folder_id: str,
        level: AccessLevel,
        task_id: str | None,
    ) -> bool:
        """Whether the agent and its person may both use a whole folder at the level.

        For planning code sandbox mounts: nothing is recorded or used up, and
        one-time grants never count, because a mount lasts a whole command.
        """

        chain = folder_chain(folder_id)
        person_id = person.person_id if person else None
        if person is None and agent.role != AGENT_ROLE_SYSTEM:
            return False
        resource, entities = folder_entities(
            self.team_id, chain[0], is_file=False, owner=folder_owner(chain[0], person_id)
        )
        agent_grants = self._principal_grants(agent, task_id=task_id, person_id=person_id)
        sides: list[tuple[dict[str, str], list[dict[str, Any]], list[GrantV1], str | None]] = [
            (agent.uid(), agent_entities(agent), agent_grants + self.shared_with(agent, person, task_id), person_id)
        ]
        if person is not None:
            sides.append(
                (person.uid(), person_entities(person), self.valid_grants(person.subjects(), task_id=task_id), None)
            )
        for principal, principal_entities, side_grants, context_person in sides:
            grants = [grant for grant in side_grants if grant.uses_remaining is None]
            if not self._evaluate(
                principal=principal,
                principal_entities=principal_entities,
                grants=grants,
                action=f"use.{level.value}",
                resource=resource,
                resource_entities=entities,
                task_id=task_id,
                context_person=context_person,
            ).allowed:
                return False
        return True

    def target_entities_for_ref(
        self, target: TargetRefV1, *, person_id: str | None = None
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """Cedar entities for a grant target.

        ``person_id`` is the person acting (or the person an agent's task works
        for): a folder or file inside their own folder is marked as theirs, so
        the owner policy lets them share it (decision D15). Anyone else's
        folder gets an owner nobody is.
        """

        if target.kind in {TargetKind.FOLDER, TargetKind.FILE}:
            return self._folder_target_entities(target, person_id)
        if target.kind is TargetKind.OPERATION:
            tool_id, _, operation = target.id.partition("#")
            tool = self.catalog.tools.get(tool_id)
            definition = tool.operation(operation) if tool else None
            if definition is None:
                raise HandDownRefused(f"{target.id} is not an operation in the tool catalog.")
            resolution = OperationResolutionV1(
                tool_id=tool_id, operation=operation, level=definition.level, source=definition.source
            )
            return target_entities(self.catalog, self.team_id, resolution)
        if target.kind is TargetKind.TOOL:
            if target.id not in self.catalog.tools:
                raise HandDownRefused(f"{target.id} is not in the tool catalog.")
            probe = OperationResolutionV1(
                tool_id=target.id, operation="probe", level=AccessLevel.FULL, source=ClassificationSource.TRUSTED
            )
            _operation, entities = target_entities(self.catalog, self.team_id, probe)
            return target_uid(target), [entity for entity in entities if entity["uid"]["type"] != "Operation"]
        if target.kind is TargetKind.TOOL_SET:
            if target.id not in self.catalog.tool_sets:
                raise HandDownRefused(f"{target.id} is not a tool set.")
            return target_uid(target), [
                {
                    "uid": target_uid(target),
                    "attrs": {
                        "team": {"__entity": {"type": "Team", "id": self.team_id}},
                        "adminRole": {"__entity": {"type": "Role", "id": f"{self.team_id}/admin"}},
                    },
                    "parents": [],
                }
            ]
        raise HandDownRefused(f"Grants on {target.kind.value} targets are not supported yet.")

    def _folder_target_entities(
        self, target: TargetRefV1, person_id: str | None
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        try:
            folder_id = grantable_folder_id(target.id)
        except ValueError as exc:
            raise HandDownRefused(str(exc)) from exc
        return folder_entities(
            self.team_id, folder_id, is_file=target.kind is TargetKind.FILE, owner=folder_owner(folder_id, person_id)
        )

    def grant_by_person(self, person: PersonPrincipal, spec: GrantV1) -> GrantV1:
        """A person gives access they may give (see ``grant_refusal``)."""

        refusal = self.grant_refusal(person, spec)
        if refusal:
            raise HandDownRefused(refusal)
        grant = spec.model_copy(
            update={
                "grant_id": new_id("grant"),
                "team_id": self.team_id,
                "granted_by": SubjectRefV1(kind=SubjectKind.USER, id=person.person_id),
                "status": GrantStatus.ACTIVE,
                "created_at_utc": self.clock(),
            }
        )
        return self._store_grant(GrantV1.model_validate(grant.model_dump()))

    # -- one entry per tool (access_control.grant_entries) -------------------------

    def _tools_in_set(self, tool_set_id: str) -> frozenset[str]:
        return frozenset(tool.tool_id for tool in self.catalog.tools_in_set(tool_set_id))

    def _in_force(self, grant: GrantV1, now: datetime, depth: int = 0) -> bool:
        """Active, unexpired, with uses left, and so is every grant up its chain (task and person aside)."""

        if depth > 8 or grant.status is not GrantStatus.ACTIVE:
            return False
        if grant.expires_at_utc is not None and now >= grant.expires_at_utc:
            return False
        if grant.uses_remaining is not None and grant.uses_remaining <= 0:
            return False
        if grant.parent_grant_id is None:
            return True
        parent = self.store.get(grant.parent_grant_id)
        return parent is not None and self._in_force(parent, now, depth + 1)

    def plan_grants(self, grants: list[GrantV1], *, ending: Iterable[str] = ()) -> GrantPlan:
        """How new grants combine with their holders' access in force (``grant_entries.plan_grants``).

        ``ending`` are grants the same change removes (a proposal that lowers
        access removes the old grant and gives a narrower one), so they
        neither cover nor get replaced by the new grants.
        """

        now = self.clock()
        gone = set(ending)
        subjects = list(dict.fromkeys(grant.subject for grant in grants))
        existing = [
            grant
            for grant in self.store.for_subjects(self.team_id, subjects)
            if grant.kind is GrantKind.ACCESS and grant.grant_id not in gone and self._in_force(grant, now)
        ]
        return plan_grants(grants, existing, self._tools_in_set)

    def covering_grant(self, spec: GrantV1) -> GrantV1 | None:
        """The grant in force that already gives everything ``spec`` would, or None."""

        return self.plan_grants([spec.model_copy(update={"status": GrantStatus.ACTIVE})]).covered.get(spec.grant_id)

    def _store_grant(self, grant: GrantV1) -> GrantV1:
        """Store one new grant as one entry: returns the stored grant, or the one that already covers it.

        The plan runs inside the store's write transaction
        (``AccessRepository.store_planned``), so a grant stored at the same
        moment by another process is seen: the two never sit side by side in
        one entry, and a tool grant never stays outside its tool set's entry.
        Inside a person's decision (``AccessRepository.decision_transaction``)
        the whole decision is that transaction.
        """

        stored, plan = self.store.store_planned(grant, lambda: self.plan_grants([grant]))
        cover = plan.covered.get(grant.grant_id)
        # Logged once stored: inside a decision that rolls back, nothing was.
        if cover is not None:
            self.store.after_commit(
                lambda: logger.info(
                    "Access already covered; nothing new stored. event=grant_already_covered subject=%s:%s "
                    "target=%s level=%s covered_by=%s",
                    grant.subject.kind.value,
                    grant.subject.id,
                    grant.target.id,
                    grant.level.value,
                    cover.grant_id,
                )
            )
            return cover
        replaced = plan.replaced_by(grant.grant_id)
        if replaced:
            self.store.after_commit(
                lambda: logger.info(
                    "Broader access replaced narrower access. event=grants_replaced grant_id=%s replaced=%s",
                    stored.grant_id,
                    ",".join(replaced),
                )
            )
        return stored

    def entry_grants(self, grant: GrantV1) -> list[GrantV1]:
        """Every active or pending access grant in ``grant``'s entry (itself included)."""

        if grant.kind is not GrantKind.ACCESS:
            return [grant]
        key = entry_key(grant)
        found = [
            held
            for held in self.store.for_subjects(
                self.team_id, [grant.subject], statuses=(GrantStatus.ACTIVE, GrantStatus.PENDING)
            )
            if held.kind is GrantKind.ACCESS and entry_key(held) == key
        ]
        return found if any(held.grant_id == grant.grant_id for held in found) else [grant, *found]

    def _is_lead_subject(self, subject: SubjectRefV1) -> bool:
        if subject.kind is SubjectKind.AGENT_TYPE:
            return self.agent_role_of(subject.id.split("/", 1)[-1]) == AGENT_ROLE_TEAM_LEAD
        return subject.kind is SubjectKind.ROLE and subject.id.split("/", 1)[-1] == AGENT_ROLE_TEAM_LEAD

    @staticmethod
    def operation_refusal(spec: GrantV1) -> str | None:
        """Access is a level on a whole tool, never one operation (owner's decision 2026-09-30).

        Operation names are fragile (sending mail is both ``gmail_send`` and
        ``command:gmail.send``), so a grant on one operation could refuse the
        very thing it was meant to allow. Blocks may still name one operation.
        """

        if spec.kind is GrantKind.ACCESS and spec.target.kind is TargetKind.OPERATION:
            return (
                f"Access is given for a whole tool at a level, not for one operation: give "
                f"{spec.target.id.partition('#')[0]} at the level the operation needs."
            )
        return None

    def worker_lasting_refusal(self, spec: GrantV1) -> str | None:
        """Why lasting access for a person's work may not go to this holder, or None.

        Only the team lead keeps lasting access for a person ("access follows
        the work"): workers get access through their tasks and schedules, so
        such a grant to a worker would never count (``agent_grants``). Refusing
        it here keeps every path from creating access that silently does nothing.
        """

        if not _lasting_consent(spec) or spec.subject.kind is SubjectKind.USER or self._is_lead_subject(spec.subject):
            return None
        return (
            "Workers keep no lasting access for a person's work; they get it through their tasks, from what the "
            "team lead may hand on, or through a scheduled job. Give it to the team lead (with handing on) or to "
            "the schedule instead."
        )

    def grant_refusal(self, person: PersonPrincipal, spec: GrantV1) -> str | None:
        """Why the person may not create this grant, or None when they may.

        - Agents working for the person, for the person's own work (decision
          D13): the person authorizes what they hold themselves. Agents never
          exceed their person, so this is the person's consent, not new
          authority.
        - Agents working for someone else: only who could give the access
          (administrators for team targets) and only when approving that
          person's request (``AccessRequests``), which also gives the person
          the access when they lack it.
        - Agents for everyone's work: administrators only.
        - People, and blocks: who could give the access (administrator or
          owner authority, or a delegable grant). A person shares their own
          folder through owner authority (decision D15).
        """

        worker_refusal = self.operation_refusal(spec) or self.worker_lasting_refusal(spec)
        if worker_refusal:
            return worker_refusal
        agent_subject = spec.subject.kind in {
            SubjectKind.AGENT,
            SubjectKind.AGENT_TYPE,
            SubjectKind.ROLE,
            SubjectKind.TEAM,
            SubjectKind.SCHEDULE,
        }
        if agent_subject and spec.kind is GrantKind.ACCESS and _in_person_folder(spec.target):
            return (
                "Agents reach a person's folder only while working for that person, or for someone it "
                "was shared with; share it with a person instead."
            )
        if agent_subject and spec.kind is GrantKind.ACCESS:
            if spec.for_person_id is None and not is_administrator(person.roles):
                return "Only an administrator can give agents access for everyone's work."
            if spec.for_person_id == person.person_id and self.holds(
                person, spec.target, spec.level, task_id=spec.task_id
            ):
                return None
        check = self._delegate_check(
            principal=person.uid(),
            principal_entities=person_entities(person),
            grants=self.valid_grants(person.subjects(), task_id=spec.task_id),
            level=spec.level,
            target=spec.target,
            task_id=spec.task_id,
            owner_hint=person.person_id,
        )
        if not check.allowed:
            return f"You cannot give {spec.level.value} access to {spec.target.id}: it is beyond your own authority."
        return None

    def hand_down(
        self,
        grantor: AgentPrincipal,
        spec: GrantV1,
        *,
        task_id: str | None,
        person_id: str | None = None,
        automatic_only: bool = False,
    ) -> GrantV1:
        """An agent hands on access it holds for ``person_id``'s work; returns the grant (active or pending).

        The child serves the same person's work as its parent (decision D13,
        rule 8 of section 5.2), so a worker never gets from a hand-down what
        another person authorized.
        """

        stored = self._prepare_hand_down(
            grantor, spec, task_id=task_id, person_id=person_id, automatic_only=automatic_only
        )
        # A hand-down waiting for approval is not access yet; it joins the
        # entry when approved (approve_pending).
        return self._store_grant(stored) if stored.status is GrantStatus.ACTIVE else self.store.create(stored)

    def _prepare_hand_down(
        self,
        grantor: AgentPrincipal,
        spec: GrantV1,
        *,
        task_id: str | None,
        person_id: str | None,
        automatic_only: bool,
    ) -> GrantV1:
        """Check delegation and construct its child without changing stored authority."""

        if spec.subject in grantor.subjects()[:2]:
            raise HandDownRefused("Nobody hands access to themselves.")
        if spec.lifetime is GrantLifetime.ONE_TIME and spec.delegation_depth:
            raise HandDownRefused("One-time access cannot be handed on.")
        if spec.kind is not GrantKind.ACCESS:
            raise HandDownRefused("Only access can be handed on.")
        worker_refusal = self.operation_refusal(spec) or self.worker_lasting_refusal(
            spec.model_copy(update={"for_person_id": person_id})
        )
        if worker_refusal:
            raise HandDownRefused(worker_refusal)
        grants = self._principal_grants(grantor, task_id=task_id, person_id=person_id)
        check = self._delegate_check(
            principal=grantor.uid(),
            principal_entities=agent_entities(grantor),
            grants=grants,
            level=spec.level,
            target=spec.target,
            task_id=task_id,
        )
        if not check.allowed:
            raise HandDownRefused(
                f"{grantor.agent_type} holds no {spec.level.value} access to {spec.target.id} that it may hand on."
            )
        parents = check.grant_ids
        if automatic_only:
            # Only access that may be handed on without asking a person.
            parents = tuple(
                grant.grant_id
                for grant in grants
                if grant.grant_id in parents and grant.delegation_mode is DelegationMode.AUTOMATIC
            )
            if not parents:
                raise HandDownRefused("The access it could come from says to ask a person each time.")
        parent, spec = self._hand_down_parent(
            grantor, spec.model_copy(update={"for_person_id": person_id}), grants, parents
        )
        status = GrantStatus.PENDING if parent.delegation_mode is DelegationMode.ASK_OWNER else GrantStatus.ACTIVE
        child = spec.model_copy(
            update={
                "grant_id": new_id("grant"),
                "team_id": self.team_id,
                "parent_grant_id": parent.grant_id,
                "for_person_id": parent.for_person_id,
                # Recorded by agent type: grants go to types, and an agent's
                # instance id changes when it restarts.
                "granted_by": grantor.type_subject(),
                "status": status,
                "created_at_utc": self.clock(),
            }
        )
        return GrantV1.model_validate(child.model_dump())

    def _hand_down_parent(
        self, grantor: AgentPrincipal, spec: GrantV1, grants: list[GrantV1], covering_ids: tuple[str, ...]
    ) -> tuple[GrantV1, GrantV1]:
        """The broadest covering delegable grant whose rules the child satisfies, and the child for it.

        Every covering parent is tried, not only the broadest (lab finding 6).
        Access for one task handed down from access that lasts until a set time
        ends then at the latest (its expiry is capped at the parent's), since a
        hand-down never outlives what it comes from.
        """

        by_id = {grant.grant_id: grant for grant in grants}
        parents = [by_id[grant_id] for grant_id in covering_ids if grant_id in by_id and by_id[grant_id].delegation_depth >= 1]
        parents.sort(
            key=lambda grant: (
                LIFETIME_RANK[grant.lifetime],
                grant.expires_at_utc or datetime.max.replace(tzinfo=timezone.utc),
                grant.delegation_depth,
            ),
            reverse=True,
        )
        def capped(parent: GrantV1) -> GrantV1:
            if spec.lifetime not in TASK_BOUND_LIFETIMES or parent.expires_at_utc is None:
                return spec
            ends = min(spec.expires_at_utc or parent.expires_at_utc, parent.expires_at_utc)
            return spec.model_copy(update={"expires_at_utc": ends})

        problems_by_parent = [
            (parent, child, attenuation_problems(parent, child, grantor=grantor, role_of=self.agent_role_of))
            for parent in parents
            for child in [capped(parent)]
        ]
        passing = [(parent, child) for parent, child, problems in problems_by_parent if not problems]
        if not passing:
            detail = ", ".join(problems_by_parent[0][2]) if problems_by_parent else "no covering grant"
            raise HandDownRefused(f"The hand-down must narrow the access it comes from ({detail}).")
        return passing[0]

    def approve_pending(self, person: PersonPrincipal, grant_id: str) -> GrantV1:
        """The target's approver approves a pending hand-down within the rules."""

        grant = self.store.get(grant_id)
        if grant is None or grant.status is not GrantStatus.PENDING:
            raise HandDownRefused("That hand-down is not waiting for approval.")
        if self.grant_refusal(person, grant) is not None:
            raise HandDownRefused("Only the target's approver can approve this hand-down.")
        actor = SubjectRefV1(kind=SubjectKind.USER, id=person.person_id)
        active = grant.model_copy(update={"status": GrantStatus.ACTIVE})
        # Planned inside the store's write transaction, as in _store_grant.
        approved, activated = self.store.approve_pending(
            grant_id, actor=actor, at=self.clock(), plan=lambda: self.plan_grants([active])
        )
        if not activated:
            # Access already in force covers it: the waiting hand-down was not needed and is denied.
            self.store.after_commit(
                lambda: logger.info(
                    "An approved hand-down was already covered. event=grant_already_covered grant_id=%s "
                    "covered_by=%s",
                    grant_id,
                    approved.grant_id,
                )
            )
        return approved

    def revoke(self, grant_id: str, *, actor: SubjectRefV1) -> list[str]:
        """Revoke without an authority check; for trusted code that already checked."""

        return self.store.revoke(grant_id, actor=actor, at=self.clock())

    def end_task_grants(self, task_ids: Iterable[str]) -> list[str]:
        """End the grants bound to tasks that ended (lifetime task or one time).

        Such a grant only ever counted inside its own task, so ending it takes
        nothing away; it keeps the Access page and ``/access list`` from
        showing access that no longer works.
        """

        wanted = sorted({task_id for task_id in task_ids if task_id})
        ended = self.store.end_task_grants(self.team_id, wanted, at=self.clock())
        if ended:
            logger.info(
                "Task-bound access ended with its task. event=task_grants_ended task_ids=%s grant_ids=%s",
                ",".join(wanted),
                ",".join(ended),
            )
        return ended

    def may_revoke(
        self,
        person: PersonPrincipal,
        grant: GrantV1,
        *,
        could_give: Callable[[TargetRefV1, AccessLevel, str | None], bool] | None = None,
    ) -> bool:
        """Whether a person may revoke a grant: they gave it, or they could give it.

        Blocks have their own, narrower rule (``AccessRequests.may_unblock``).
        ``could_give`` lets a caller that checks many grants reuse its own
        cached ``may_give`` answers; the rule stays this one.
        """

        if grant.kind is GrantKind.BLOCK:
            return False
        if grant.granted_by == SubjectRefV1(kind=SubjectKind.USER, id=person.person_id):
            return True
        # A person may always withdraw what serves their own work (decision D13).
        if grant.for_person_id == person.person_id:
            return True
        check = could_give or (lambda target, level, task_id: self.may_give(person, target, level, task_id=task_id))
        return check(grant.target, grant.level, grant.task_id)

    def revoke_by_person(self, person: PersonPrincipal, grant_id: str) -> list[str]:
        """A person removes access: the grant's whole entry (``entry_grants``) and what was handed on from it.

        Removing one grant of an entry while another kept giving the access is
        what people could not see, so the entry goes as a whole. The person
        must be allowed to revoke every grant in it; otherwise nothing changes.
        """

        grant = self.store.get(grant_id)
        if grant is None or grant.team_id != self.team_id:
            raise HandDownRefused("There is no such grant in this team.")
        if grant.kind is GrantKind.BLOCK:
            # Removing a block widens access, so it has its own rule
            # (access_control.requests.AccessRequests.unblock).
            raise HandDownRefused(f"{grant_id} is a block; remove it with unblock.")
        entry = self.entry_grants(grant)
        refused = [held for held in entry if not self.may_revoke(person, held)]
        if refused:
            if any(held.grant_id == grant_id for held in refused):
                raise HandDownRefused(
                    "Only the person who gave this access, or someone who could give it, can revoke it."
                )
            raise HandDownRefused(
                "Part of this access was given by someone whose access you cannot remove, so none of it was "
                "removed. Ask a team administrator to remove it."
            )
        actor = SubjectRefV1(kind=SubjectKind.USER, id=person.person_id)
        return self.store.revoke_all(self.team_id, [held.grant_id for held in entry], actor=actor, at=self.clock())


def _person_id(person: PersonPrincipal | None) -> str | None:
    return person.person_id if person is not None else None


def _in_person_folder(target: TargetRefV1) -> bool:
    return target.kind in {TargetKind.FOLDER, TargetKind.FILE} and target.id.split("/", 1)[0] == FOLDER_ROOT_USERS


def _lasting_consent(grant: GrantV1) -> bool:
    """A lasting agent grant for one person's work: the team lead's standing access (see ``agent_grants``).

    Task-bound grants, a schedule's access, the team's defaults (for everyone's
    work) and standard access are not.
    """

    return (
        grant.kind is GrantKind.ACCESS
        and grant.for_person_id is not None
        and grant.lifetime not in TASK_BOUND_LIFETIMES
        and grant.subject.kind is not SubjectKind.SCHEDULE
        and not is_standard_grant(grant.grant_id)
    )


def _target_covers(parent: TargetRefV1, child: TargetRefV1) -> bool:
    if parent == child:
        return True
    folder_kinds = {TargetKind.FOLDER, TargetKind.FILE}
    if parent.kind in folder_kinds or child.kind in folder_kinds:
        # A folder covers itself and everything below it; a file covers only itself.
        if parent.kind is TargetKind.FILE:
            return parent == child
        return parent.kind is TargetKind.FOLDER and child.kind in folder_kinds and covers(parent.id, child.id)
    if parent.kind is TargetKind.TOOL and child.kind is TargetKind.OPERATION:
        return child.id.split("#", 1)[0] == parent.id
    # Tool sets cover their tools; the catalog decides membership and Cedar
    # already proved coverage, so a narrower child under a set is accepted.
    return parent.kind is TargetKind.TOOL_SET and child.kind in {TargetKind.TOOL, TargetKind.OPERATION, TargetKind.TOOL_SET} and parent != child


def attenuation_problems(
    parent: GrantV1,
    child: GrantV1,
    *,
    grantor: AgentPrincipal,
    role_of: Callable[[str], str] = lambda agent_type: "worker",
) -> list[str]:
    """Why ``child`` does not narrow ``parent`` (empty when it does); rules 2-9."""

    problems: list[str] = []
    if not _target_covers(parent.target, child.target):
        problems.append("target")
    if child.level.rank > parent.level.rank:
        problems.append("level")
    if LIFETIME_RANK[child.lifetime] > LIFETIME_RANK[parent.lifetime]:
        problems.append("lifetime")
    if parent.expires_at_utc is not None and (
        child.expires_at_utc is None or child.expires_at_utc > parent.expires_at_utc
    ):
        problems.append("expiry")
    if parent.task_id and child.task_id != parent.task_id:
        problems.append("task")
    if parent.for_person_id is not None and child.for_person_id != parent.for_person_id:
        problems.append("person")
    if child.delegation_depth > parent.delegation_depth - 1:
        problems.append("delegation_depth")
    if parent.delegate_to and not _recipient_allowed(parent.delegate_to, child.subject, grantor.team_id, role_of):
        problems.append("recipient")
    return problems


def _recipient_allowed(
    allowed: tuple[SubjectRefV1, ...], recipient: SubjectRefV1, team_id: str, role_of: Callable[[str], str]
) -> bool:
    """Whether ``recipient`` is one of the allowed subjects or inside one of them."""

    if recipient in allowed:
        return True
    covering = {SubjectRefV1(kind=SubjectKind.TEAM, id=team_id)}
    if recipient.kind is SubjectKind.AGENT_TYPE:
        agent_type = recipient.id.split("/", 1)[-1]
        covering.add(SubjectRefV1(kind=SubjectKind.ROLE, id=f"{team_id}/{role_of(agent_type)}"))
    return any(subject in covering for subject in allowed)


def _folder_refusal(agent_type: str, level: AccessLevel, folder_id: str, result: _SideResult, other_person: bool) -> str:
    if other_person:
        return f"{folder_id} is in another person's private folder, and its owner has not shared it at this level."
    text = f"{agent_type} needs {level.value} access to the folder {folder_id}."
    if _blocks(result.blocking):
        text += " It is blocked."
    if result.error:
        text += " The access check could not run."
    return text


def _folder_next_step(agent_type: str, level: AccessLevel, folder_id: str, result: _SideResult, other_person: bool) -> str:
    if other_person:
        return (
            "Only its owner can share it, with the person you work for (/share in chat or the workspace "
            "browser). Work in this person's folder or the shared team/ and agents/ folders meanwhile."
        )
    if _blocks(result.blocking):
        return (
            f"A block stops {agent_type} from using {folder_id}; asking for access will not change that. "
            "Tell the person you work for; only a person can remove the block."
        )
    return (
        f"Call request_access with folder={folder_id!r} and level={level.value!r}, saying why you need it; "
        f"TeamLead or a person who can give {level.value} access to it decides."
    )


def _blocks(policy_ids: tuple[str, ...]) -> tuple[str, ...]:
    """The block grant ids among the policies that denied a request."""

    return tuple(item.split(":", 1)[1] for item in policy_ids if item.startswith("block:"))


def _merge_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate entities by uid (the same role or team may appear twice)."""

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for entity in entities:
        key = (entity["uid"]["type"], entity["uid"]["id"])
        if key in merged:
            existing = merged[key]
            parents = existing["parents"] + [p for p in entity["parents"] if p not in existing["parents"]]
            merged[key] = {**existing, "attrs": {**entity["attrs"], **existing["attrs"]}, "parents": parents}
        else:
            merged[key] = entity
    return list(merged.values())


__all__ = [
    "FILES_TOOL_ID",
    "UNCONFINED_EXECUTION_OPERATION",
    "UNCONFINED_EXECUTION_TOOL_ID",
    "AccessDenied",
    "AccessService",
    "HandDownRefused",
    "attenuation_problems",
]
