"""Managed services and SSH typing: exact changes a person approves.

Two kinds of staged change (access plan Phase 4c-3c), applied only through
``apply_change`` (``access_control.changes``, adapters in
``custom_agents.change_approvals``):

- ``managed_execution_service``: creating a managed service (it starts at
  once) or changing its desired state (start, stop, remove). The plan binds the
  exact desired-state digest (``ManagedServiceV1.activation_resource_digest``)
  and, for a change, the generation and digest of the state it replaces.
- ``execution_ssh_interaction``: typing into one SSH session. The plan binds
  the session, its exact target digest and pinned host key.

Both are ``full`` on the platform tool ``fn:execution_control``. Agents never
hold ``full`` on platform tools (guardrail ``guard:agents-no-platform-full``),
so with authorization on a person (an administrator) approves each exact
change in the Inbox; with it off, the person's agreement in the conversation
applies it. Applying records an ``ActivationRecordV2`` whose approval
(``ChangeApprovalRecordV1``) names the approved access request; that record is
the authority the execution controller and SSH broker check before each use.

These services describe and apply plans; they never decide authority. Each
runs one ``_verify_current`` check (not applied yet; the service, revision,
profile or session unchanged) in ``describe``, before a person is asked, and
again in ``apply``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from access_catalog.catalog import change_operation
from access_control.changes import ChangeApproval
from access_control.entities import operation_id
from custom_agents.execution_control_service import EXECUTION_TOOL_ID, ExecutionControlService
from custom_agents.governance.authorized_operations import FilePlanStore, GovernanceAuditEvent, model_digest
from custom_agents.governance.runtime_adapters import LoggingGovernanceAuditSink
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeProposalV1, summary_line
from custom_messages.contracts.runtime_v1 import (
    AUTHORITY_MODE_AUTHORIZATION_DISABLED,
    AUTHORITY_MODE_PERSON_APPROVAL,
)
from id_generation import new_id
from runtime_hosting.execution_contracts import (
    AccessHolderV1,
    ActivationRecordV2,
    ExecutionActivationKind,
    ExecutionBackendKind,
    InteractiveSessionV1,
    ManagedServiceV1,
    OwnerScopeV1,
    ServiceDesiredState,
    ServiceKind,
    ServiceRestartPolicy,
    SSHConnectionTargetV1,
    utc_now,
)
from runtime_settings.instance_layout import RUNTIME_DIRNAME

MANAGED_SERVICE_CHANGE_KIND = "managed_execution_service"
SSH_INTERACTION_CHANGE_KIND = "execution_ssh_interaction"
# Change id prefixes (at most 15 characters, id_generation); adapters match them.
MANAGED_SERVICE_CHANGE_PREFIX = "exec_svc_change"
SSH_INTERACTION_CHANGE_PREFIX = "ssh_interaction"
# Long enough for a person to approve later; the digests still refuse a plan
# whose service, revision, profile or session changed meanwhile.
EXECUTION_CHANGE_PLAN_TTL_SECONDS = 24 * 60 * 60
EXECUTION_CHANGE_PLAN_SCHEMA_VERSION: Literal[1] = 1
_MANAGED_SERVICE_CHANGE_ID = re.compile(rf"{MANAGED_SERVICE_CHANGE_PREFIX}_[0-9a-f]{{12}}\Z")
_SSH_INTERACTION_CHANGE_ID = re.compile(rf"{SSH_INTERACTION_CHANGE_PREFIX}_[0-9a-f]{{12}}\Z")
_STALE = "If this changes before approval, the change is refused and must be staged again."


class _ExecutionChangePlan(BaseModel):
    """What every execution change plan binds: the staging agent, person, task and owner."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = EXECUTION_CHANGE_PLAN_SCHEMA_VERSION
    change_id: str
    plan_digest: str = ""
    created_at_utc: datetime
    expires_at_utc: datetime
    actor_principal_id: str
    actor_named_agent_type: str
    team_id: str
    owner_canonical_user_id: str
    source_task_id: str
    authority_mode: Literal["person_approval", "authorization_disabled_agreement"]
    owner: OwnerScopeV1
    reason: str = Field(min_length=1, max_length=1000)


class ManagedServiceChangePlan(_ExecutionChangePlan):
    service_id: str
    service_name: str
    service_kind: ServiceKind
    revision_id: str
    revision_digest: str
    profile_id: str
    profile_digest: str
    desired_state: ServiceDesiredState
    desired_state_digest: str
    # 0 when the plan creates the service; otherwise the generation it replaces.
    previous_generation: int = Field(ge=0)
    previous_state_digest: str | None = None
    previous_desired_state: ServiceDesiredState | None = None
    # The new service record (creation only), saved stopped; applying starts it.
    draft: ManagedServiceV1 | None = None

    @field_validator("change_id")
    @classmethod
    def _valid_change_id(cls, value: str) -> str:
        if not _MANAGED_SERVICE_CHANGE_ID.fullmatch(str(value or "")):
            raise ValueError("Invalid managed service change id.")
        return value


class SSHInteractionChangePlan(_ExecutionChangePlan):
    session_id: str
    target: SSHConnectionTargetV1
    target_digest: str

    @field_validator("change_id")
    @classmethod
    def _valid_change_id(cls, value: str) -> str:
        if not _SSH_INTERACTION_CHANGE_ID.fullmatch(str(value or "")):
            raise ValueError("Invalid SSH typing change id.")
        return value


class _ExecutionChanges:
    """Shared staging, binding and approval steps of the two execution change kinds."""

    kind: str
    label: str

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.execution = ExecutionControlService(agent)
        self.audit = LoggingGovernanceAuditSink()

    def _plans_dir(self, name: str) -> Path:
        path = self.execution.config.layout.root / RUNTIME_DIRNAME / "execution-changes" / name
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        return path

    def _authority_mode(self) -> str:
        return (
            AUTHORITY_MODE_PERSON_APPROVAL
            if self.execution.authorization_enabled
            else AUTHORITY_MODE_AUTHORIZATION_DISABLED
        )

    def _binding(self, reason: str) -> dict[str, Any]:
        """The plan fields that bind it to this agent, person, task and owner."""

        actor = self.execution.actor
        if not actor.person_id:
            raise PermissionError(f"A {self.label} must be staged while the agent works for a person.")
        if not actor.task_id:
            raise PermissionError(f"A {self.label} must be staged from an active task.")
        normalized_reason = " ".join(str(reason or "").split())
        if not normalized_reason:
            raise ValueError("Give a reason people will read.")
        now = utc_now()
        return {
            "created_at_utc": now,
            "expires_at_utc": now + timedelta(seconds=EXECUTION_CHANGE_PLAN_TTL_SECONDS),
            "actor_principal_id": actor.agent.agent_id,
            "actor_named_agent_type": actor.agent.agent_type,
            "team_id": actor.agent.team_id,
            "owner_canonical_user_id": actor.person_id,
            "source_task_id": actor.task_id,
            "authority_mode": self._authority_mode(),
            "owner": self.execution.owner,
            "reason": normalized_reason[:1000],
        }

    def _check_binding(self, plan: _ExecutionChangePlan) -> None:
        actor = self.execution.actor
        if plan.plan_digest != model_digest(plan):
            raise PermissionError(f"The {self.label} plan was changed after staging.")
        if plan.expires_at_utc <= utc_now():
            raise PermissionError(f"The {self.label} plan has expired. Stage a fresh plan.")
        if plan.authority_mode != self._authority_mode():
            raise PermissionError("Authorization was turned on or off after planning. Stage a fresh plan.")
        if (
            plan.actor_principal_id != actor.agent.agent_id
            or plan.actor_named_agent_type != actor.agent.agent_type
            or plan.team_id != actor.agent.team_id
            or plan.owner_canonical_user_id != (actor.person_id or "")
            or plan.owner != self.execution.owner
        ):
            raise PermissionError(f"The {self.label} plan belongs to another agent, person or team.")
        if plan.source_task_id != (actor.task_id or ""):
            raise PermissionError(f"A {self.label} must continue in the task that staged it.")

    def _check_approval(self, plan: _ExecutionChangePlan, approval: ChangeApproval) -> None:
        agreement = approval.mode is ChangeApprovalMode.AGREEMENT
        if agreement != (plan.authority_mode == AUTHORITY_MODE_AUTHORIZATION_DISABLED):
            raise PermissionError("This change needs a person's approval, not agreement (or the reverse).")

    def _activation(
        self,
        plan: _ExecutionChangePlan,
        approval: ChangeApproval,
        *,
        kind: ExecutionActivationKind,
        resource_id: str,
        resource_digest: str,
        generation: int,
    ) -> ActivationRecordV2:
        """The record of this approved change; the runtime checks it before each use."""

        holder = self.execution.holder()
        return ActivationRecordV2.build(
            kind=kind,
            resource_id=resource_id,
            resource_digest=resource_digest,
            generation=generation,
            owner=plan.owner,
            operation=operation_id(EXECUTION_TOOL_ID, change_operation(self.kind)),
            holder=AccessHolderV1(
                agent_id=plan.actor_principal_id,
                agent_type=plan.actor_named_agent_type,
                person_id=plan.owner_canonical_user_id or None,
                person_roles=holder.person_roles,
                task_id=plan.source_task_id,
            ),
            approval=approval.to_record(),
        )

    def _staged(self, plan: _ExecutionChangePlan, proposal: ChangeProposalV1) -> dict[str, Any]:
        self._audit(plan, "staged")
        return {
            "status": "staged",
            "change_id": plan.change_id,
            "plan_digest": plan.plan_digest,
            "expires_at_utc": plan.expires_at_utc.isoformat(),
            "title": proposal.title,
            "effect": list(proposal.effect),
            "next_step": (
                "Show this to the person, then call apply_change with this change_id"
                + (
                    "; an administrator approves the exact change in the Inbox."
                    if plan.authority_mode == AUTHORITY_MODE_PERSON_APPROVAL
                    else " and confirmed=true once they agree."
                )
            ),
        }

    def _audit(self, plan: _ExecutionChangePlan, phase: str) -> None:
        self.audit.record(
            GovernanceAuditEvent(
                operation=self.kind,
                phase=phase,
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=plan.source_task_id,
                authority_mode=plan.authority_mode,
            )
        )


class ManagedServiceChangeService(_ExecutionChanges):
    """Create a managed service, or change its desired state, once a person approves."""

    kind = MANAGED_SERVICE_CHANGE_KIND
    label = "managed service change"

    def _plan_store(self) -> FilePlanStore[ManagedServiceChangePlan]:
        return FilePlanStore(
            self._plans_dir("managed-services"),
            ManagedServiceChangePlan,
            change_id_pattern=_MANAGED_SERVICE_CHANGE_ID,
            invalid_change_id_message="Invalid managed service change id.",
            missing_plan_message="Managed service change plan not found: {change_id}",
        )

    def stage_create(
        self,
        *,
        name: str,
        revision_id: str,
        service_kind: ServiceKind,
        restart_policy: ServiceRestartPolicy,
        reason: str,
        port: int | None = None,
        mcp_path: str = "/mcp",
        mcp_tool_names: tuple[str, ...] = (),
        mcp_target_agent_types: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Stage creating a service that starts at once (one approval)."""

        binding = self._binding(reason)
        revision = self.execution.owned_revision(revision_id)
        profile = self.execution.profiles.require(
            profile_id=revision.profile_id,
            profile_digest=revision.profile_digest,
        )
        if profile.backend != ExecutionBackendKind.KUBERNETES:
            raise ValueError("Managed services require a Kubernetes execution profile.")
        if restart_policy != ServiceRestartPolicy.ALWAYS:
            raise ValueError("Kubernetes Deployments support only the managed-service restart policy 'always'.")
        draft = ManagedServiceV1(
            name=name,
            service_kind=service_kind,
            owner=self.execution.owner,
            revision_id=revision.revision_id,
            revision_digest=revision.revision_digest,
            restart_policy=restart_policy,
            port=port,
            mcp_path=mcp_path,
            mcp_tool_names=mcp_tool_names,
            mcp_target_agent_types=mcp_target_agent_types,
            created_by_task_id=self.execution.actor.task_id,
        )
        running = draft.model_copy(update={"desired_state": ServiceDesiredState.RUNNING})
        return self._stage(
            ManagedServiceChangePlan(
                change_id=new_id(MANAGED_SERVICE_CHANGE_PREFIX),
                **binding,
                service_id=draft.service_id,
                service_name=draft.name,
                service_kind=draft.service_kind,
                revision_id=revision.revision_id,
                revision_digest=revision.revision_digest,
                profile_id=revision.profile_id,
                profile_digest=revision.profile_digest,
                desired_state=ServiceDesiredState.RUNNING,
                desired_state_digest=running.activation_resource_digest(),
                previous_generation=0,
                draft=draft,
            )
        )

    def stage_state(self, *, service_id: str, desired_state: ServiceDesiredState, reason: str) -> dict[str, Any]:
        """Stage starting, stopping or removing an existing service."""

        binding = self._binding(reason)
        current = self.execution.owned_service(service_id)
        revision = self.execution.owned_revision(current.revision_id)
        candidate = self._candidate(current, desired_state)
        return self._stage(
            ManagedServiceChangePlan(
                change_id=new_id(MANAGED_SERVICE_CHANGE_PREFIX),
                **binding,
                service_id=current.service_id,
                service_name=current.name,
                service_kind=current.service_kind,
                revision_id=revision.revision_id,
                revision_digest=revision.revision_digest,
                profile_id=revision.profile_id,
                profile_digest=revision.profile_digest,
                desired_state=desired_state,
                desired_state_digest=candidate.activation_resource_digest(),
                previous_generation=current.generation,
                previous_state_digest=current.activation_resource_digest(),
                previous_desired_state=current.desired_state,
            )
        )

    def _stage(self, plan: ManagedServiceChangePlan) -> dict[str, Any]:
        plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
        self._plan_store().save(plan)
        return self._staged(plan, self.describe(plan.change_id))

    @staticmethod
    def _candidate(current: ManagedServiceV1, desired_state: ServiceDesiredState) -> ManagedServiceV1:
        """The next generation with the new desired state (not validated: no activation yet)."""

        return current.model_copy(
            update={
                "generation": current.generation + 1,
                "activation": None,
                "desired_state": desired_state,
                "updated_at_utc": utc_now(),
            }
        )

    def load_plan(self, change_id: str) -> ManagedServiceChangePlan:
        plan = self._plan_store().load(str(change_id or "").strip())
        self._check_binding(plan)
        return plan

    def _verify_current(self, plan: ManagedServiceChangePlan) -> ManagedServiceV1 | None:
        """The change can still apply: not applied yet, and service, revision and profile unchanged.

        Returns the current service (None when the plan creates it).
        """

        revision = self.execution.owned_revision(plan.revision_id)
        if revision.revision_digest != plan.revision_digest:
            raise PermissionError("The service's revision changed after planning. Stage a fresh plan.")
        if plan.desired_state == ServiceDesiredState.RUNNING:
            self.execution.profiles.require(profile_id=plan.profile_id, profile_digest=plan.profile_digest)
        current = self.execution.repository.get_managed_service(
            team_id=plan.owner.team_id, service_id=plan.service_id
        )
        if plan.previous_generation == 0:
            if current is not None:
                raise FileExistsError(f"Managed service change {plan.change_id} was already applied.")
            return None
        if current is None or current.owner != plan.owner:
            raise LookupError("Managed execution service not found.")
        if (
            current.generation == plan.previous_generation + 1
            and current.activation is not None
            and current.activation.resource_digest == plan.desired_state_digest
        ):
            raise FileExistsError(f"Managed service change {plan.change_id} was already applied.")
        if (
            current.generation != plan.previous_generation
            or current.activation_resource_digest() != plan.previous_state_digest
        ):
            raise PermissionError(
                "The managed service changed after planning. Inspect it and stage a fresh plan."
            )
        return current

    def describe(self, change_id: str) -> ChangeProposalV1:
        plan = self.load_plan(change_id)
        self._verify_current(plan)
        return ChangeProposalV1(
            kind=self.kind,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=self._title(plan),
            summary=summary_line(f"{self._title(plan)}. Takes effect when approved."),
            effect=tuple(line[:1000] for line in self._effect(plan)),
        )

    @staticmethod
    def _title(plan: ManagedServiceChangePlan) -> str:
        if plan.previous_generation == 0:
            return f"Create and start managed service {plan.service_name}"
        verb = {
            ServiceDesiredState.RUNNING: "Start",
            ServiceDesiredState.STOPPED: "Stop",
            ServiceDesiredState.REMOVED: "Remove",
        }[plan.desired_state]
        return f"{verb} managed service {plan.service_name}"

    def _effect(self, plan: ManagedServiceChangePlan) -> list[str]:
        name = f"{plan.service_name} ({plan.service_id})"
        lines: list[str] = []
        if plan.desired_state == ServiceDesiredState.RUNNING:
            profile = self.execution.profiles.require(profile_id=plan.profile_id, profile_digest=plan.profile_digest)
            lines.append(
                ("Creates and starts" if plan.previous_generation == 0 else "Starts")
                + f" managed service {name} in Kubernetes, running revision {plan.revision_id} "
                f"with profile {plan.profile_id}. It is restarted whenever it stops, until someone "
                "approves stopping or removing it."
            )
            lines.append(f"Its profile {'allows' if profile.network_enabled else 'blocks'} network access.")
            if plan.draft is not None and plan.draft.service_kind == ServiceKind.MCP_SERVER:
                draft = plan.draft
                lines.append(
                    f"It serves an MCP server on port {draft.port} at {draft.mcp_path} with the tools "
                    f"{', '.join(draft.mcp_tool_names)} for "
                    f"{', '.join(draft.mcp_target_agent_types) or 'no agent type yet'}. The MCP connection "
                    "it publishes needs its own approval before any agent can use it."
                )
            elif plan.draft is not None and plan.draft.port is not None:
                lines.append(f"It listens on port {plan.draft.port}.")
        elif plan.desired_state == ServiceDesiredState.STOPPED:
            lines.append(
                f"Stops managed service {name}. It stays defined and can be started again with another approval."
            )
        else:
            also = " and Service" if plan.service_kind == ServiceKind.MCP_SERVER else ""
            lines.append(
                f"Removes managed service {name}: its Kubernetes Deployment{also} are deleted; "
                "its data volume is kept."
            )
        if plan.previous_desired_state is not None:
            lines.append(f"Its desired state is now {plan.previous_desired_state.value}.")
        lines += [f"Reason: {plan.reason}", _STALE]
        return lines

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Apply an approved plan (only the change mechanism calls this)."""

        plan = self.load_plan(change_id)
        self._check_approval(plan, approval)
        current = self._verify_current(plan)
        if current is None:
            assert plan.draft is not None
            target = plan.draft.model_copy(update={"desired_state": plan.desired_state, "updated_at_utc": utc_now()})
            generation = target.generation
        else:
            target = self._candidate(current, plan.desired_state)
            generation = target.generation
        if target.activation_resource_digest() != plan.desired_state_digest:
            raise PermissionError("The service's exact desired state no longer matches the approved plan.")
        activation = self._activation(
            plan,
            approval,
            kind=ExecutionActivationKind.SERVICE,
            resource_id=plan.service_id,
            resource_digest=plan.desired_state_digest,
            generation=generation,
        )
        record = ManagedServiceV1.model_validate({**target.model_dump(mode="python"), "activation": activation})
        if current is None:
            saved = self.execution.repository.create_managed_service(record)
        else:
            saved = self.execution.repository.update_managed_service(record, expected_generation=current.generation)
        self._audit(plan, "applied")
        return {
            "status": "applied",
            "change_id": plan.change_id,
            "plan_digest": plan.plan_digest,
            "service": saved.model_dump(mode="json"),
            "next_step": (
                "The execution controller reconciles the new desired state; check it with "
                "list_managed_execution_services."
            ),
        }


class SSHInteractionChangeService(_ExecutionChanges):
    """Let the agent type into one SSH session once a person approves."""

    kind = SSH_INTERACTION_CHANGE_KIND
    label = "SSH typing change"

    def _plan_store(self) -> FilePlanStore[SSHInteractionChangePlan]:
        return FilePlanStore(
            self._plans_dir("ssh-interactions"),
            SSHInteractionChangePlan,
            change_id_pattern=_SSH_INTERACTION_CHANGE_ID,
            invalid_change_id_message="Invalid SSH typing change id.",
            missing_plan_message="SSH typing change plan not found: {change_id}",
        )

    def stage(self, *, session_id: str, reason: str) -> dict[str, Any]:
        binding = self._binding(reason)
        session = self._session_that_can_open(session_id)
        if self.execution.ssh_interaction(session.session_id) is not None:
            raise ValueError("Typing into this session is already approved.")
        plan = SSHInteractionChangePlan(
            change_id=new_id(SSH_INTERACTION_CHANGE_PREFIX),
            **binding,
            session_id=session.session_id,
            target=session.target,
            target_digest=session.target_digest,
        )
        plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
        self._plan_store().save(plan)
        return self._staged(plan, self.describe(plan.change_id))

    def _session_that_can_open(self, session_id: str) -> InteractiveSessionV1:
        session = self.execution.owned_session(session_id)
        if session.connect_activation is None:
            raise PermissionError("This SSH session cannot be opened any more; create a new session.")
        return session

    def load_plan(self, change_id: str) -> SSHInteractionChangePlan:
        plan = self._plan_store().load(str(change_id or "").strip())
        self._check_binding(plan)
        return plan

    def _verify_current(self, plan: SSHInteractionChangePlan) -> InteractiveSessionV1:
        """Not approved yet, and the session and its exact target unchanged."""

        if self.execution.ssh_interaction(plan.session_id) is not None:
            raise FileExistsError(f"SSH typing change {plan.change_id} was already applied.")
        session = self._session_that_can_open(plan.session_id)
        if session.target != plan.target or session.target_digest != plan.target_digest:
            raise PermissionError("The SSH session's target changed after planning. Stage a fresh plan.")
        return session

    def describe(self, change_id: str) -> ChangeProposalV1:
        plan = self.load_plan(change_id)
        self._verify_current(plan)
        target = plan.target
        agent = plan.actor_named_agent_type
        return ChangeProposalV1(
            kind=self.kind,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=f"Let {agent} type into an SSH session on {target.username}@{target.host}",
            summary=summary_line(
                f"Let {agent} type commands into one SSH session as {target.username}@{target.host}, "
                "with that account's rights."
            ),
            effect=(
                f"Lets {agent} type into SSH session {plan.session_id} as {target.username}@{target.host}:"
                f"{target.port} (pinned host key {target.host_key_algorithm} {target.host_key_fingerprint}).",
                "Whatever it types runs on that host with that account's rights, for as long as this "
                "session exists. Other sessions need their own approval.",
                f"Reason: {plan.reason}",
                _STALE,
            ),
        )

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Record the approved typing permission (only the change mechanism calls this)."""

        plan = self.load_plan(change_id)
        self._check_approval(plan, approval)
        session = self._verify_current(plan)
        activation = self._activation(
            plan,
            approval,
            kind=ExecutionActivationKind.SSH_INTERACT,
            resource_id=session.session_id,
            resource_digest=session.target_digest,
            generation=1,
        )
        saved = self.execution.repository.put_ssh_interaction(activation)
        self._audit(plan, "applied")
        return {
            "status": "applied",
            "change_id": plan.change_id,
            "plan_digest": plan.plan_digest,
            "session_id": session.session_id,
            "interaction": saved.model_dump(mode="json"),
            "next_step": "Type with write_execution_ssh_session; close the session when the task is done.",
        }


__all__ = [
    "EXECUTION_CHANGE_PLAN_TTL_SECONDS",
    "MANAGED_SERVICE_CHANGE_KIND",
    "MANAGED_SERVICE_CHANGE_PREFIX",
    "SSH_INTERACTION_CHANGE_KIND",
    "SSH_INTERACTION_CHANGE_PREFIX",
    "ManagedServiceChangePlan",
    "ManagedServiceChangeService",
    "SSHInteractionChangePlan",
    "SSHInteractionChangeService",
]
