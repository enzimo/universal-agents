"""Manager-owned schedule worker policy, preflight and guarded retargeting.

The tools that use this service are decided per call by the access hook
(``fn:schedules``); the schedule must belong to the person the task works for
(``access_control.ownership``, person from the signed task context). Saving a
policy goes through ``custom_agents.system_schedule_changes.configure_worker``,
which applies a person's own schedule at once and stages a change to a
schedule the runtime manages (memory review) for a person's approval.
"""

from __future__ import annotations

import logging
from typing import Any

from access_control.decisions import HandDownRefused
from access_control.ownership import PRIVATE_READ, PRIVATE_UPDATE
from custom_agents.agent_instantiation_service import (
    AgentInstantiationService,
    effective_worker_rules,
)
from custom_agents.pending_work_runtime import wake_scheduler
from custom_agents.schedule_access import OWNER, ScheduleAccessCheck
from custom_agents.schedule_changes import (
    CONFIGURE_WORKER_ACTION,
    PreparedScheduleChange,
    ScheduleNotFound,
    WorkerConfigurationRequestV1,
    owned_schedule,
    request_binding,
)
from custom_agents.schedule_definition_identity import (
    build_definition_record,
    schedule_definition_digest,
)
from custom_agents.schedule_management import (
    ScheduleMutationPermit,
    schedule_control_state_digest,
)
from custom_agents.schedule_readiness import (
    effective_requirements,
    prepare_schedule_access,
)
from custom_agents.schedule_repair_guidance import KNOWN_CAPABILITY_BLOCKER
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from access_control.entities import schedule_subject
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    GrantLifetime,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleCapabilityRequirementV1,
    ScheduleCapabilityRequirementsV1,
    ScheduleReadinessCheckV1,
    ScheduleReadinessV1,
    ScheduleWorkerAuthorityMode,
    ScheduleWorkerBindingV1,
    ScheduleWorkerPolicySpecV1,
    utc_now,
)

logger = logging.getLogger(__name__)

SCHEDULE_NOT_IN_OWNER_SCOPE = "schedule_not_found_in_owner_scope"


class ScheduleWorkerService:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.store = agent._schedule_store
        self.launcher = AgentInstantiationService(agent)

    def owned_schedule(self, schedule_id: str, *, action: str = PRIVATE_UPDATE) -> Any:
        """The schedule, when it belongs to the person the task works for."""

        from custom_agents.access_runtime import verified_person_id

        try:
            return owned_schedule(self.store, schedule_id, verified_person_id(self.agent), action=action)
        except ScheduleNotFound as exc:
            raise PermissionError(SCHEDULE_NOT_IN_OWNER_SCOPE) from exc

    def _access_check(self, worker_type: str, schedule_id: str) -> ScheduleAccessCheck | None:
        return ScheduleAccessCheck.for_agent(self.agent, worker_type=worker_type, schedule_id=schedule_id)

    def _authority_mode(self) -> ScheduleWorkerAuthorityMode:
        """How the policy was allowed: the access hook decided the tool call (on), or agreement (off)."""

        from custom_agents.access_runtime import runtime_access

        return "held_access" if runtime_access(self.agent).enabled else "agreement"

    def prepare(
        self,
        schedule_id: str,
        *,
        agent_type: str | None = None,
        capabilities: ScheduleCapabilityRequirementsV1 | None = None,
    ) -> dict[str, Any]:
        """Readiness of the target worker, including access it and the owner lack."""

        schedule = self.owned_schedule(schedule_id, action=PRIVATE_READ)
        binding = self.store.worker_records.get_binding(
            schedule_id, team_id=self.agent.team_id
        )
        requirements = capabilities or (
            effective_requirements(binding)
            if binding
            else ScheduleCapabilityRequirementsV1()
        )
        if agent_type:
            schedule = schedule.model_copy(
                update={"claim_named_agent_type": agent_type, "claim_agent_id": None}
            )
        result = prepare_schedule_access(
            self.agent,
            schedule,
            requirements,
            binding=binding,
            access=self._access_check(schedule.claim_named_agent_type, schedule_id),
        )
        return {
            **result.model_dump(mode="json"),
            "missing_access": [_missing_view(check) for check in result.checks if check.missing_access_for],
        }

    def prepare_configuration(self, request: WorkerConfigurationRequestV1) -> PreparedScheduleChange:
        """Build a policy change against the schedule as it is now (``configure_worker`` commits it)."""

        schedule = self.owned_schedule(request.schedule_id)
        from custom_agents.local_schedule_policy import local_system_timer_spec, supports_local_worker

        if not supports_local_worker(schedule):
            raise PermissionError("local_workers_require_supported_root_schedule")
        owner_id = owner_canonical_user_id_for_schedule(schedule)
        if not owner_id:
            raise PermissionError("schedule_requires_canonical_owner")
        if schedule.claim_agent_id and not request.convert_exact_target:
            raise ValueError("exact_target_requires_explicit_conversion")
        # A supported system timer runs only on its own agent type (the system
        # schedule table's row, e.g. memory review on MemReviewAgent).
        system_timer = local_system_timer_spec(schedule)
        if system_timer is not None and request.target_agent_type != system_timer.agent_type:
            raise ValueError(f"{system_timer.name_family}_requires_{system_timer.agent_type}")
        existing = self.store.worker_records.get_binding(
            request.schedule_id, team_id=self.agent.team_id
        )
        policy, capabilities = self._saved_unless_given(existing, request.policy, request.capabilities)
        candidate = self._target_candidate(schedule, request.target_agent_type)
        readiness = prepare_schedule_access(
            self.agent,
            candidate,
            capabilities,
            binding=existing,
            access=self._access_check(request.target_agent_type, request.schedule_id),
        )
        if request.activate_schedule:
            candidate.enabled = readiness.status != "blocked"
        commit = _ConfigurationCommit(
            self,
            request=request,
            schedule=schedule,
            candidate=candidate,
            existing=existing,
            policy=policy,
            capabilities=capabilities,
            readiness=readiness,
        )
        return PreparedScheduleChange(
            action=CONFIGURE_WORKER_ACTION,
            schedule=schedule,
            candidate=candidate,
            owner_id=owner_id,
            binding=request_binding(request),
            changes={"target_agent_type": request.target_agent_type, "enabled": candidate.enabled},
            commit=commit,
        )

    def authorize_access(self, schedule_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Hand on what TeamLead may give the worker type; name the rest for a person."""

        from custom_agents.access_runtime import acting_agent, runtime_requests

        schedule = self.owned_schedule(schedule_id)
        binding = self.store.worker_records.get_binding(schedule_id, team_id=self.agent.team_id)
        if binding is None or binding.revision != expected_revision:
            raise ValueError("schedule_worker_policy_revision_conflict")
        check = self._access_check(binding.target_agent_type, schedule_id)
        if check is None:
            return {
                "schedule_id": schedule_id,
                "status": "not_needed",
                "message": "Authorization is off, so the worker may use every tool; nothing to hand on.",
            }
        preview = schedule.model_copy(
            update={"claim_named_agent_type": binding.target_agent_type, "claim_agent_id": None}
        )
        readiness = prepare_schedule_access(
            self.agent, preview, effective_requirements(binding), binding=binding, access=check
        )
        requests = runtime_requests(self.agent)
        actor = acting_agent(self.agent)
        handed = _HandOn(requests, actor, binding.target_agent_type, schedule_id)
        needed = handed.needed_levels(list(readiness.checks))
        handed.reconcile(needed)
        for item in readiness.checks:
            if item.missing_access_for:
                handed.add(item)
        handed.give(needed)
        return {
            "schedule_id": schedule_id,
            "policy_revision": binding.revision,
            "worker_type": binding.target_agent_type,
            **handed.result(),
        }

    @staticmethod
    def _saved_unless_given(
        existing: ScheduleWorkerBindingV1 | None,
        policy: ScheduleWorkerPolicySpecV1 | None,
        capabilities: ScheduleCapabilityRequirementsV1 | None,
    ) -> tuple[ScheduleWorkerPolicySpecV1, ScheduleCapabilityRequirementsV1]:
        """Omitted policy or requirements keep the saved ones (defaults if none)."""
        if existing is None:
            return (
                ScheduleWorkerPolicySpecV1() if policy is None else policy,
                ScheduleCapabilityRequirementsV1()
                if capabilities is None
                else capabilities,
            )
        return (
            existing.policy if policy is None else policy,
            existing.capabilities if capabilities is None else capabilities,
        )

    @staticmethod
    def _target_candidate(schedule: Any, target_agent_type: str) -> Any:
        candidate = schedule.model_copy(
            deep=True,
            update={
                "claim_named_agent_type": target_agent_type,
                "claim_agent_id": None,
            },
        )
        if candidate.payload_meta:
            candidate.payload_meta.to_named_agent_type = target_agent_type
            candidate.payload_meta.to_agent_name = None
        return candidate


class _ConfigurationCommit:
    """Saves one prepared worker policy (the store checks any permit)."""

    def __init__(
        self,
        service: ScheduleWorkerService,
        *,
        request: WorkerConfigurationRequestV1,
        schedule: Any,
        candidate: Any,
        existing: ScheduleWorkerBindingV1 | None,
        policy: ScheduleWorkerPolicySpecV1,
        capabilities: ScheduleCapabilityRequirementsV1,
        readiness: ScheduleReadinessV1,
    ) -> None:
        self.service = service
        self.request = request
        self.schedule = schedule
        self.candidate = candidate
        self.existing = existing
        self.policy = policy
        self.capabilities = capabilities
        self.readiness = readiness

    def __call__(self, permit: ScheduleMutationPermit | None) -> dict[str, Any]:
        agent = self.service.agent
        request = self.request
        existing = self.existing
        if existing is not None and not self.policy.enabled and request.target_agent_type == existing.target_agent_type:
            return self._suspend(permit)
        if self.policy.expires_at_utc and self.policy.expires_at_utc <= utc_now():
            raise ValueError("worker_policy_already_expired")
        definition, _ = self.service.launcher.validate_type(request.target_agent_type, automatic=self.policy.enabled)
        effective_worker_rules(agent, request.target_agent_type, self.policy.rules)
        keep_observed = existing is not None and existing.execution_digest == self.readiness.execution_digest
        # A known blocker keeps a newly created schedule inert. Existing jobs
        # retain their cadence; the admission gate blocks their unsafe target.
        binding = ScheduleWorkerBindingV1(
            schedule_id=request.schedule_id,
            team_id=agent.team_id,
            owner_canonical_user_id=owner_canonical_user_id_for_schedule(self.schedule),
            target_agent_type=request.target_agent_type,
            definition_digest=schedule_definition_digest(existing, definition),
            execution_digest=self.readiness.execution_digest,
            revision=request.expected_revision + 1,
            policy=self.policy,
            capabilities=self.capabilities,
            actor_principal_id=agent.id,
            observed_capabilities=existing.observed_capabilities if keep_observed and existing else (),
            authority_mode=self.service._authority_mode(),
            blocked_reason=KNOWN_CAPABILITY_BLOCKER if self.readiness.status == "blocked" else None,
        )
        pending = self.service.store.worker_records.configure(
            binding,
            self.candidate,
            expected_revision=request.expected_revision,
            expected_control_digest=schedule_control_state_digest(self.schedule),
            system_mutation_permit=permit,
            system_request_binding=request_binding(request),
            definition_record=build_definition_record(
                definition,
                schedule_id=request.schedule_id,
                team_id=agent.team_id,
                policy_revision=binding.revision,
            ),
        )
        # A sync tool (configure_scheduled_worker) reaches this from its worker thread.
        wake_scheduler(agent)
        return self._result(binding, pending)

    def _suspend(self, permit: ScheduleMutationPermit | None) -> dict[str, Any]:
        """Turn automatic startup off for the same target, keeping the saved identity.

        The saved definition and execution identity stay as they were, so turning
        startup off never fails on a type the operator no longer allows or a
        definition that changed. Requirements are not part of that identity
        (``execution_digest``): given ones replace the saved ones here too, the
        same as a full save, and readiness was checked against exactly them.
        """

        assert self.existing is not None
        binding = self.existing.model_copy(
            update={
                "revision": self.request.expected_revision + 1,
                "policy": self.policy,
                "capabilities": self.capabilities,
                "authority_mode": self.service._authority_mode(),
                "actor_principal_id": self.service.agent.id,
                "blocked_reason": None,
            }
        )
        pending = self.service.store.worker_records.configure(
            binding,
            self.candidate,
            expected_revision=self.request.expected_revision,
            expected_control_digest=schedule_control_state_digest(self.schedule),
            system_mutation_permit=permit,
            system_request_binding=request_binding(self.request),
        )
        return self._result(binding, pending)

    def _result(self, binding: ScheduleWorkerBindingV1, pending: bool) -> dict[str, Any]:
        """What was saved, read back from the saved record, with its readiness in plain words."""

        summary = configuration_summary(
            binding.capabilities,
            self.readiness,
            schedule_on=self.candidate.enabled,
            automatic_startup=binding.policy.enabled,
        )
        logger.info(
            "Saved a scheduled job's worker settings. event=schedule_worker_configured schedule_id=%s "
            "revision=%s worker_type=%s automatic_startup=%s schedule_on=%s requirements=%s readiness=%s",
            binding.schedule_id,
            binding.revision,
            binding.target_agent_type,
            binding.policy.enabled,
            self.candidate.enabled,
            ",".join(_requirement_label(item) for item in binding.capabilities.requirements) or "none",
            self.readiness.status,
        )
        return {
            "schedule_id": binding.schedule_id,
            "configured": True,
            "policy_revision": binding.revision,
            "pending_current_claim": pending,
            "enabled": self.candidate.enabled,
            "automation_enabled": binding.policy.enabled,
            "worker_policy": binding.policy.model_dump(mode="json"),
            "saved_requirements": binding.capabilities.model_dump(mode="json"),
            "readiness": self.readiness.model_dump(mode="json"),
            "missing_access": [_missing_view(check) for check in self.readiness.checks if check.missing_access_for],
            "summary": summary,
        }


def _requirement_label(item: ScheduleCapabilityRequirementV1) -> str:
    return f"{item.capability_id} {item.operation}"


def configuration_summary(
    capabilities: ScheduleCapabilityRequirementsV1,
    readiness: ScheduleReadinessV1,
    *,
    schedule_on: bool,
    automatic_startup: bool,
) -> str:
    """One plain sentence set a model can repeat: what is saved, whether it can run, and why not."""

    saved = ", ".join(_requirement_label(item) for item in capabilities.requirements) or "none"
    blocked = [
        f"{check.capability_id} {check.operation} ({check.reason})"
        for check in readiness.checks
        if check.required and check.status == "blocked"
    ]
    if blocked:
        ready = "no, blocked by " + "; ".join(blocked)
    elif readiness.status == "provisional":
        ready = "yes, provisionally (some needs are checked when the tool runs)"
    else:
        ready = "yes"
    return (
        f"Saved requirements: {saved}. Ready: {ready}. "
        f"Schedule on: {'yes' if schedule_on else 'no'}. "
        f"Automatic worker startup: {'on' if automatic_startup else 'off'}."
    )


def _missing_view(check: ScheduleReadinessCheckV1) -> dict[str, Any]:
    return {
        "tool_id": check.tool_id,
        "operation": check.access_operation,
        "level": check.access_level,
        "missing_for": list(check.missing_access_for),
        "required": check.required,
        "capability_id": check.capability_id,
    }


class _HandOn:
    """Gives the worker type a schedule's access that TeamLead may give; collects what a person must give.

    Access is one grant per tool, held by the schedule (``SubjectKind.SCHEDULE``,
    "access follows the work": every agent in one of its runs uses it, and
    only there) at the highest level the schedule's requirements need for that tool (owner's
    decision 2026-09-30: a level per tool, never single operations, because an
    operation name is fragile — sending mail is both ``gmail_send`` and
    ``command:gmail.send``). What one schedule needs never widens another
    schedule of the same worker type. Setup grants the requirements no longer
    need, or that are higher than they need now, end first (``reconcile``);
    access a person approved during a run stays.
    """

    def __init__(self, requests: Any, actor: Any, worker_type: str, schedule_id: str) -> None:
        self.requests = requests
        self.actor = actor
        self.worker_type = worker_type
        self.schedule_id = schedule_id
        self.missing: dict[str, list[ScheduleReadinessCheckV1]] = {}
        self.given: list[dict[str, Any]] = []
        self.waiting: list[dict[str, Any]] = []
        self.ask: list[dict[str, Any]] = []
        self.ended: list[str] = []

    @staticmethod
    def needed_levels(checks: list[ScheduleReadinessCheckV1]) -> dict[str, AccessLevel]:
        """The level each tool needs: the highest of its requirements' operations."""

        needed: dict[str, AccessLevel] = {}
        for check in checks:
            if not check.tool_id or not check.access_level:
                continue
            level = AccessLevel(str(check.access_level))
            held = needed.get(str(check.tool_id))
            needed[str(check.tool_id)] = level if held is None or level.rank > held.rank else held
        return needed

    def reconcile(self, needed: dict[str, AccessLevel]) -> None:
        """End this schedule's setup grants after authorize_access checks its owner.

        These lasting grants belong to the schedule, so the generic
        request-bound revoke operation cannot end them. Reconciliation names
        the exact schedule and person and ends only grants this lead type
        gave. A person's grants and every other schedule's grants stay.
        """

        service = self.requests.service
        lead = self.actor.agent.type_subject()
        holder = schedule_subject(service.team_id, self.schedule_id)
        person_id = self.actor.person.person_id if self.actor.person else None
        for grant in service.store.for_subjects(service.team_id, [holder]):
            if grant.granted_by != lead:
                continue  # a person approved it in a run; it stays
            if grant.for_person_id != person_id:
                # Given for another person's work (the schedule had another
                # owner then); it never counts in this owner's runs, and an
                # agent ends only what it gave for the person it works for.
                continue
            tool_id = grant.target.id.partition("#")[0]
            wanted = needed.get(tool_id)
            if wanted is not None and wanted.rank >= grant.level.rank:
                continue
            self.ended += service.store.revoke_all(
                service.team_id, [grant.grant_id], actor=lead, at=service.clock()
            )
            logger.info(
                "A scheduled job no longer needs this access; it ended. event=schedule_access_ended "
                "schedule_id=%s worker_type=%s tool_id=%s level=%s needed=%s",
                self.schedule_id,
                self.worker_type,
                tool_id,
                grant.level.value,
                wanted.value if wanted else None,
            )

    def add(self, check: ScheduleReadinessCheckV1) -> None:
        self.missing.setdefault(str(check.tool_id), []).append(check)

    def give(self, needed: dict[str, AccessLevel]) -> None:
        """Give the schedule each missing tool at the level it needs, or name what a person must give."""

        for tool_id, checks in self.missing.items():
            view = self._view(tool_id, checks, needed)
            if any(OWNER in check.missing_access_for for check in checks):
                self.ask.append({**view, "message": self._ask_text(view, owner_lacks=True)})
            else:
                self._give_one(view)

    @staticmethod
    def _view(
        tool_id: str, checks: list[ScheduleReadinessCheckV1], needed: dict[str, AccessLevel]
    ) -> dict[str, Any]:
        """One tool's missing access, at the highest level its requirements need."""

        level = needed.get(tool_id) or max(
            (AccessLevel(str(check.access_level)) for check in checks), key=lambda held: held.rank
        )
        return {
            "tool_id": tool_id,
            "level": level.value,
            "operations": [str(check.access_operation) for check in checks],
            "missing_for": sorted({holder for check in checks for holder in check.missing_access_for}),
            "required": any(check.required for check in checks),
        }

    def _give_one(self, view: dict[str, Any]) -> None:
        try:
            outcome = self.requests.give(
                self.actor.agent,
                recipient=schedule_subject(self.requests.service.team_id, self.schedule_id),
                target=TargetRefV1(kind=TargetKind.TOOL, id=view["tool_id"]),
                level=AccessLevel(view["level"]),
                reason=f"Scheduled job {self.schedule_id} runs as {self.worker_type} and needs this.",
                lifetime=GrantLifetime.UNTIL_REVOKED,
                task_id=None,
                person=self.actor.person,
            )
        except HandDownRefused as exc:
            logger.info(
                "TeamLead cannot hand scheduled-worker access on; a person decides. "
                "event=schedule_access_needs_person schedule_id=%s worker_type=%s tool_id=%s why=%s",
                self.schedule_id,
                self.worker_type,
                view["tool_id"],
                exc,
            )
            self.ask.append({**view, "message": self._ask_text(view, owner_lacks=False)})
            return
        entry = {
            **view,
            "grant_id": outcome.grant.grant_id if outcome.grant else None,
            "request_id": outcome.request.request_id if outcome.request else None,
            "message": outcome.message,
        }
        (self.waiting if outcome.waiting else self.given).append(entry)

    def _ask_text(self, view: dict[str, Any], *, owner_lacks: bool) -> str:
        call = (
            f"request_access(tool_id={view['tool_id']!r}, level={view['level']!r}, "
            "reason='<why the scheduled job needs it>')"
        )
        if owner_lacks:
            return (
                "The person this schedule works for does not have this access either, and agents never "
                f"exceed their person. A team administrator gives it to both; the worker can ask with {call} "
                "when it first needs it, and that run waits for the decision."
            )
        return (
            f"TeamLead cannot give {self.worker_type} this access. Ask a team administrator to give it, "
            f"or let the worker ask with {call} when it first needs it; that run waits for a person's decision. "
            "Access approved in a run with Keep for later tasks is kept for this schedule's runs only."
        )

    def result(self) -> dict[str, Any]:
        if self.ask:
            next_step = (
                "Tell the person which access a person still has to give (ask_a_person); a run that needs it "
                "waits for a decision. Then run prepare_scheduled_job_access and activate the schedule."
            )
        elif self.waiting:
            next_step = "A person approves the waiting hand-downs in the Inbox; then run prepare_scheduled_job_access."
        else:
            next_step = "Run prepare_scheduled_job_access to confirm readiness, then activate the schedule."
        return {
            "given": self.given,
            "waiting": self.waiting,
            "ask_a_person": self.ask,
            "ended": self.ended,
            "next_step": next_step,
        }


__all__ = ["SCHEDULE_NOT_IN_OWNER_SCOPE", "ScheduleWorkerService", "configuration_summary"]
