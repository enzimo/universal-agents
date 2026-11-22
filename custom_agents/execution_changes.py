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

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
from runtime_hosting.authorization import SSH_SINGLE_REQUEST_ENDED_MESSAGE
from runtime_hosting.execution_contracts import (
    AccessHolderV1,
    ActivationRecordV2,
    EnrolledSSHTargetV1,
    ExecutionActivationKind,
    ExecutionBackendKind,
    InteractiveSessionV1,
    ManagedServiceV1,
    OwnerScopeKind,
    OwnerScopeV1,
    SSHConnectionTargetV1,
    SSHDelegationBoundsV1,
    SSHSecretRecipe,
    SSHTargetLifecycle,
    SSHTargetPlatform,
    ServiceDesiredState,
    ServiceKind,
    ServiceRestartPolicy,
    enrolled_ssh_target_digest,
    ssh_interaction_resource_digest,
    ssh_public_key_sha256_fingerprint,
    utc_now,
    validate_ssh_host_syntax,
    validate_ssh_username,
)
from runtime_hosting.ssh_network import (
    LocalAddressChecker,
    SSHHostResolver,
    SSHNetworkPolicy,
    validate_ssh_host,
)
from runtime_settings.instance_layout import RUNTIME_DIRNAME
from secure_tool_broker.credential_catalog import read_credential_metadata_readonly
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope

logger = logging.getLogger(__name__)

MANAGED_SERVICE_CHANGE_KIND = "managed_execution_service"
SSH_TARGET_ENROLL_CHANGE_KIND = "execution_ssh_target_enroll"
SSH_INTERACTION_CHANGE_KIND = "execution_ssh_interaction"
# Change id prefixes (at most 15 characters, id_generation); adapters match them.
MANAGED_SERVICE_CHANGE_PREFIX = "exec_svc_change"
SSH_TARGET_ENROLL_CHANGE_PREFIX = "ssh_target_enr"
SSH_INTERACTION_CHANGE_PREFIX = "ssh_interaction"
# Long enough for a person to approve later; the digests still refuse a plan
# whose service, revision, profile or session changed meanwhile.
EXECUTION_CHANGE_PLAN_TTL_SECONDS = 24 * 60 * 60
EXECUTION_CHANGE_PLAN_SCHEMA_VERSION: Literal[1] = 1
_MANAGED_SERVICE_CHANGE_ID = re.compile(rf"{MANAGED_SERVICE_CHANGE_PREFIX}_[0-9a-f]{{12}}\Z")
_SSH_TARGET_ENROLL_CHANGE_ID = re.compile(rf"{SSH_TARGET_ENROLL_CHANGE_PREFIX}_[0-9a-f]{{12}}\Z")
_SSH_INTERACTION_CHANGE_ID = re.compile(rf"{SSH_INTERACTION_CHANGE_PREFIX}_[0-9a-f]{{12}}\Z")
_TARGET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
_CREDENTIAL_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
_STALE = "If this changes before approval, the change is refused and must be staged again."
_REMOTE_HOST_SECRET_BOUNDARY_NOTE = (
    "Security note: The broker never places secrets in prompts, tool arguments, logs, command lines, "
    "transcripts, or agent-written commands. Any secret sent to the remote host can still be captured "
    "by whoever controls that host; enroll SSH keys before running arbitrary commands, prefer single-use "
    "ephemeral Tailscale auth keys, and rotate bootstrap passwords afterward."
)


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


class SSHTargetEnrollChangePlan(_ExecutionChangePlan):
    target_id: str
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    platform: SSHTargetPlatform
    host_key_algorithm: str = Field(min_length=1, max_length=64)
    host_key_fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{16,}={0,2}$")
    host_key_public_key: str = Field(min_length=16, max_length=4096)
    credential_key: str = Field(min_length=1, max_length=232)
    credential_owner_scope: OwnerScopeV1
    credential_owner_label: str = Field(min_length=1, max_length=240)
    credential_display_name: str = Field(min_length=1, max_length=240)
    credential_purpose: str = Field(min_length=1, max_length=1000)
    credential_public_key_fingerprint: str = Field(
        pattern=r"^SHA256:[A-Za-z0-9+/]{16,}={0,2}$"
    )
    bootstrap_credential_key: str | None = Field(default=None, min_length=1, max_length=232)
    bootstrap_credential_owner_scope: OwnerScopeV1 | None = None
    bootstrap_credential_owner_label: str | None = Field(default=None, min_length=1, max_length=240)
    bootstrap_credential_display_name: str | None = Field(default=None, min_length=1, max_length=240)
    bootstrap_credential_purpose: str | None = Field(default=None, min_length=1, max_length=1000)
    bootstrap_credential_username: str | None = Field(default=None, min_length=1, max_length=128)
    lifecycle: SSHTargetLifecycle
    target_expires_at_utc: datetime | None = None
    enrollment_digest: str

    @field_validator("change_id")
    @classmethod
    def _valid_change_id(cls, value: str) -> str:
        if not _SSH_TARGET_ENROLL_CHANGE_ID.fullmatch(str(value or "")):
            raise ValueError("Invalid SSH target enrollment change id.")
        return value

    @field_validator("host", mode="after")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        return validate_ssh_host_syntax(value)

    @field_validator("username", mode="after")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        return validate_ssh_username(value)


SSH_INTERACTION_PLAN_SCHEMA_VERSION: Literal[2] = 2


class SSHInteractionChangePlan(_ExecutionChangePlan):
    schema_version: Literal[2] = SSH_INTERACTION_PLAN_SCHEMA_VERSION  # type: ignore[assignment]
    session_id: str
    target: SSHConnectionTargetV1
    target_digest: str
    delegation_ttl_seconds: int | None = Field(default=None, gt=0)
    delegation_not_after_utc: datetime | None = None
    bounds: SSHDelegationBoundsV1
    resource_digest: str

    @field_validator("change_id")
    @classmethod
    def _valid_change_id(cls, value: str) -> str:
        if not _SSH_INTERACTION_CHANGE_ID.fullmatch(str(value or "")):
            raise ValueError("Invalid SSH delegation change id.")
        return value

    @model_validator(mode="after")
    def _validate_window(self) -> Self:
        if self.delegation_ttl_seconds is None and self.delegation_not_after_utc is None:
            raise ValueError(
                "SSH delegation plan requires delegation_ttl_seconds or delegation_not_after_utc."
            )
        if self.delegation_not_after_utc is not None and self.delegation_not_after_utc.tzinfo is None:
            raise ValueError("delegation_not_after_utc must include a timezone.")
        return self


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

    def _read_credential_metadata(
        self, credential_key: str, *, scope: CredentialOwnerScope
    ) -> dict[str, Any] | None:
        return read_credential_metadata_readonly(
            credential_key,
            scope=scope,
            fallback_db_path=self.execution.config.credential_catalog_database_path,
        )

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
        expires_at_utc: datetime | None = None,
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
            expires_at_utc=expires_at_utc,
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


def _catalog_scope(owner: OwnerScopeV1) -> CredentialOwnerScope:
    if owner.scope_kind == OwnerScopeKind.USER:
        return CredentialOwnerScope.user(owner.owner_id)
    return CredentialOwnerScope.team(owner.team_id)


def _format_duration(seconds: int) -> str:
    """Format a positive duration in seconds into concise plain English."""

    total = max(1, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour" if hours == 1 else f"{hours} hours")
    if minutes:
        parts.append(f"{minutes} minute" if minutes == 1 else f"{minutes} minutes")
    if secs or not parts:
        parts.append(f"{secs} second" if secs == 1 else f"{secs} seconds")
    return " ".join(parts)


def _credential_owner_label(agent: Any, config: Any, scope: OwnerScopeV1) -> str:
    """Human-readable owner label for an Admin Inbox card ('the team' or the person's name)."""

    if scope.scope_kind == OwnerScopeKind.TEAM:
        return "the team"
    from custom_tools.runtime_context import active_task_context

    _task_id, meta = active_task_context(agent)
    requester_type = getattr(meta, "requester_type", None)
    requester_kind = str(getattr(requester_type, "value", requester_type) or "").strip().lower()
    if requester_kind == "user":
        sender_name = str(getattr(meta, "from_agent_name", "") or "").strip()
        if sender_name:
            return sender_name
    user_cfg = getattr(config, "user", None)
    if user_cfg is not None and str(getattr(user_cfg, "canonical_user_id", "") or "").strip() == scope.owner_id:
        display = str(getattr(user_cfg, "display_name", "") or "").strip()
        if display and (display.lower() != "user" or scope.owner_id.lower() == "user"):
            return display
    return "the requesting person"


@dataclass(frozen=True)
class _InspectedSSHCredentials:
    credential_display_name: str
    credential_purpose: str
    credential_public_key_fingerprint: str
    bootstrap_credential_display_name: str | None = None
    bootstrap_credential_purpose: str | None = None
    bootstrap_credential_username: str | None = None


def _host_key_verification_command(platform: SSHTargetPlatform, algorithm: str) -> str:
    filename_by_algo = {
        "ssh-ed25519": "ssh_host_ed25519_key.pub",
        "ssh-rsa": "ssh_host_rsa_key.pub",
        "ecdsa-sha2-nistp256": "ssh_host_ecdsa_key.pub",
        "ecdsa-sha2-nistp384": "ssh_host_ecdsa_key.pub",
        "ecdsa-sha2-nistp521": "ssh_host_ecdsa_key.pub",
    }
    filename = filename_by_algo.get(algorithm, "ssh_host_*.pub")
    if platform == SSHTargetPlatform.WINDOWS:
        return f"ssh-keygen -lf C:\\ProgramData\\ssh\\{filename}"
    return f"ssh-keygen -lf /etc/ssh/{filename}"


def _require_secret_field(
    metadata: dict[str, Any],
    *,
    credential_key: str,
    field_name: str,
    expected_kind: str | None = None,
) -> dict[str, Any]:
    fields = metadata.get("fields")
    if not isinstance(fields, dict) or field_name not in fields or not isinstance(fields[field_name], dict):
        raise ValueError(
            f"Credential {credential_key!r} must include a secret {field_name!r} field."
        )
    field_info: dict[str, Any] = dict(fields[field_name])
    if not field_info.get("secret") or str(field_info.get("visibility") or "").strip().lower() != "secret":
        raise ValueError(
            f"Credential {credential_key!r} field {field_name!r} must be stored as a secret field."
        )
    if expected_kind is not None and str(field_info.get("kind") or "").strip().lower() != expected_kind:
        raise ValueError(
            f"Credential {credential_key!r} field {field_name!r} must be a secret {expected_kind!r} field."
        )
    return field_info


def _require_metadata_value(
    metadata: dict[str, Any],
    *,
    credential_key: str,
    field_name: str,
) -> str:
    fields = metadata.get("fields")
    if not isinstance(fields, dict) or field_name not in fields or not isinstance(fields[field_name], dict):
        raise ValueError(
            f"Credential {credential_key!r} must include a non-secret {field_name!r} field."
        )
    field_info = fields[field_name]
    if field_info.get("secret"):
        raise ValueError(
            f"Credential {credential_key!r} field {field_name!r} must be non-secret metadata."
        )
    value = str(field_info.get("value") or "").strip()
    if not value:
        raise ValueError(
            f"Credential {credential_key!r} field {field_name!r} must not be empty."
        )
    return value


class SSHTargetEnrollChangeService(_ExecutionChanges):
    """Stage and enroll a pinned, Sealbox-backed SSH target after administrator approval."""

    kind = SSH_TARGET_ENROLL_CHANGE_KIND
    label = "SSH target enrollment change"

    def __init__(
        self,
        agent: Any,
        *,
        resolver: SSHHostResolver | None = None,
        local_address_checker: LocalAddressChecker | None = None,
    ) -> None:
        super().__init__(agent)
        self._resolver = resolver
        self._local_address_checker = local_address_checker

    def _plan_store(self) -> FilePlanStore[SSHTargetEnrollChangePlan]:
        return FilePlanStore(
            self._plans_dir("ssh-target-enrollments"),
            SSHTargetEnrollChangePlan,
            change_id_pattern=_SSH_TARGET_ENROLL_CHANGE_ID,
            invalid_change_id_message="Invalid SSH target enrollment change id.",
            missing_plan_message="SSH target enrollment change plan not found: {change_id}",
        )

    def _resolve_credential_scope(
        self,
        scope: OwnerScopeV1 | Literal["user", "team"] | None,
    ) -> OwnerScopeV1:
        owner = self.execution.owner
        if scope is None or scope == "user":
            return owner
        if scope == "team":
            return OwnerScopeV1(
                scope_kind=OwnerScopeKind.TEAM,
                owner_id=owner.team_id,
                team_id=owner.team_id,
            )
        if scope.team_id != owner.team_id or (
            scope != owner
            and not (scope.scope_kind == OwnerScopeKind.TEAM and scope.owner_id == owner.team_id)
        ):
            raise PermissionError(
                "Enrolled SSH target credentials must belong to the target owner or the team scope."
            )
        return scope

    def _inspect_catalog_credentials(
        self,
        *,
        target_username: str,
        credential_key: str,
        credential_owner_scope: OwnerScopeV1,
        requested_public_key_fingerprint: str | None,
        bootstrap_credential_key: str | None,
        bootstrap_credential_owner_scope: OwnerScopeV1 | None,
    ) -> _InspectedSSHCredentials:
        if not _CREDENTIAL_KEY_PATTERN.fullmatch(credential_key):
            raise ValueError("credential_key contains invalid characters.")
        metadata = self._read_credential_metadata(
            credential_key,
            scope=_catalog_scope(credential_owner_scope),
        )
        if metadata is None:
            raise PermissionError(
                f"SSH credential {credential_key!r} is not registered in the "
                f"{credential_owner_scope.scope_kind.value} credential catalog."
            )
        _require_secret_field(
            metadata,
            credential_key=credential_key,
            field_name="ssh_private_key",
            expected_kind="credential_file",
        )
        raw_public_key = _require_metadata_value(
            metadata,
            credential_key=credential_key,
            field_name="ssh_public_key",
        )
        _algo, catalog_fp = ssh_public_key_sha256_fingerprint(raw_public_key)
        if requested_public_key_fingerprint is not None and requested_public_key_fingerprint != catalog_fp:
            raise ValueError(
                "Requested credential_public_key_fingerprint does not match the catalog ssh_public_key metadata."
            )
        cred_display = str(metadata.get("display_name") or "").strip() or credential_key
        cred_purpose = str(metadata.get("purpose") or "").strip() or "No purpose recorded"

        boot_display: str | None = None
        boot_purpose: str | None = None
        boot_username: str | None = None
        if bootstrap_credential_key is not None:
            if not _CREDENTIAL_KEY_PATTERN.fullmatch(bootstrap_credential_key):
                raise ValueError("bootstrap_credential_key contains invalid characters.")
            assert bootstrap_credential_owner_scope is not None
            boot_meta = self._read_credential_metadata(
                bootstrap_credential_key,
                scope=_catalog_scope(bootstrap_credential_owner_scope),
            )
            if boot_meta is None:
                raise PermissionError(
                    f"Bootstrap credential {bootstrap_credential_key!r} is not registered in the "
                    f"{bootstrap_credential_owner_scope.scope_kind.value} credential catalog."
                )
            _require_secret_field(
                boot_meta,
                credential_key=bootstrap_credential_key,
                field_name="admin_password",
            )
            boot_username = _require_metadata_value(
                boot_meta,
                credential_key=bootstrap_credential_key,
                field_name="username",
            )
            if boot_username != target_username:
                raise ValueError(
                    f"Bootstrap credential {bootstrap_credential_key!r} is for username "
                    f"{boot_username!r}, which does not match target username {target_username!r}."
                )
            boot_display = str(boot_meta.get("display_name") or "").strip() or bootstrap_credential_key
            boot_purpose = str(boot_meta.get("purpose") or "").strip() or "No purpose recorded"

        return _InspectedSSHCredentials(
            credential_display_name=cred_display[:240],
            credential_purpose=cred_purpose[:1000],
            credential_public_key_fingerprint=catalog_fp,
            bootstrap_credential_display_name=boot_display[:240] if boot_display else None,
            bootstrap_credential_purpose=boot_purpose[:1000] if boot_purpose else None,
            bootstrap_credential_username=boot_username,
        )

    def stage(
        self,
        *,
        host: str,
        username: str,
        platform: SSHTargetPlatform | str,
        host_key_public_key: str,
        credential_key: str,
        reason: str,
        port: int = 22,
        target_id: str | None = None,
        host_key_fingerprint: str | None = None,
        credential_owner_scope: OwnerScopeV1 | Literal["user", "team"] | None = None,
        credential_public_key_fingerprint: str | None = None,
        bootstrap_credential_key: str | None = None,
        bootstrap_credential_owner_scope: OwnerScopeV1 | Literal["user", "team"] | None = None,
        lifecycle: SSHTargetLifecycle | str = SSHTargetLifecycle.TEMPORARY,
        expires_at_utc: datetime | None = None,
    ) -> dict[str, Any]:
        ssh_settings = self.execution.config.execution_control.ssh
        if not ssh_settings.enabled:
            raise RuntimeError("execution_control.ssh.enabled is false.")
        binding = self._binding(reason)
        policy = SSHNetworkPolicy.from_settings(ssh_settings)
        validated_host = validate_ssh_host(
            host,
            port,
            policy,
            resolver=self._resolver,
            local_address_checker=self._local_address_checker,
        )
        normalized_host = validate_ssh_host_syntax(validated_host.host)
        normalized_username = validate_ssh_username(username)
        parsed_platform = SSHTargetPlatform(platform)
        parsed_lifecycle = SSHTargetLifecycle(lifecycle)
        normalized_target_id = str(target_id or "").strip() or new_id("ssh_tgt")
        if not _TARGET_ID_PATTERN.fullmatch(normalized_target_id):
            raise ValueError("target_id must contain only letters, numbers, underscore, dot, colon, or dash.")
        self._ensure_target_id_available(normalized_target_id)

        cleaned_host_key = host_key_public_key.strip()
        algorithm, derived_fingerprint = ssh_public_key_sha256_fingerprint(cleaned_host_key)
        if host_key_fingerprint is not None and host_key_fingerprint.strip() != derived_fingerprint:
            raise ValueError("host_key_fingerprint does not match host_key_public_key.")

        normalized_cred_key = str(credential_key or "").strip()
        resolved_cred_scope = self._resolve_credential_scope(credential_owner_scope)
        normalized_boot_key = str(bootstrap_credential_key or "").strip() or None
        resolved_boot_scope = (
            self._resolve_credential_scope(
                bootstrap_credential_owner_scope
                if bootstrap_credential_owner_scope is not None
                else resolved_cred_scope
            )
            if normalized_boot_key is not None
            else None
        )
        inspected = self._inspect_catalog_credentials(
            target_username=normalized_username,
            credential_key=normalized_cred_key,
            credential_owner_scope=resolved_cred_scope,
            requested_public_key_fingerprint=credential_public_key_fingerprint,
            bootstrap_credential_key=normalized_boot_key,
            bootstrap_credential_owner_scope=resolved_boot_scope,
        )

        now = utc_now()
        normalized_expiry: datetime | None = None
        if expires_at_utc is not None:
            if expires_at_utc.tzinfo is None:
                raise ValueError("expires_at_utc must include a timezone.")
            normalized_expiry = expires_at_utc.astimezone(timezone.utc)
            if normalized_expiry <= now:
                raise ValueError("Target expires_at_utc must be in the future.")
        elif parsed_lifecycle == SSHTargetLifecycle.TEMPORARY:
            raise ValueError("Temporary enrolled SSH targets require expires_at_utc.")

        enrollment_digest = enrolled_ssh_target_digest(
            target_id=normalized_target_id,
            owner=binding["owner"],
            host=normalized_host,
            port=validated_host.port,
            username=normalized_username,
            platform=parsed_platform,
            host_key_algorithm=algorithm,
            host_key_fingerprint=derived_fingerprint,
            host_key_public_key=cleaned_host_key,
            credential_key=normalized_cred_key,
            credential_owner_scope=resolved_cred_scope,
            credential_public_key_fingerprint=inspected.credential_public_key_fingerprint,
            bootstrap_credential_key=normalized_boot_key,
            bootstrap_credential_owner_scope=resolved_boot_scope,
            lifecycle=parsed_lifecycle,
            expires_at_utc=normalized_expiry,
            reason=binding["reason"],
        )
        cred_owner_label = _credential_owner_label(
            self.agent, self.execution.config, resolved_cred_scope
        )
        boot_owner_label = (
            _credential_owner_label(self.agent, self.execution.config, resolved_boot_scope)
            if resolved_boot_scope is not None
            else None
        )
        plan = SSHTargetEnrollChangePlan(
            change_id=new_id(SSH_TARGET_ENROLL_CHANGE_PREFIX),
            **binding,
            target_id=normalized_target_id,
            host=normalized_host,
            port=validated_host.port,
            username=normalized_username,
            platform=parsed_platform,
            host_key_algorithm=algorithm,
            host_key_fingerprint=derived_fingerprint,
            host_key_public_key=cleaned_host_key,
            credential_key=normalized_cred_key,
            credential_owner_scope=resolved_cred_scope,
            credential_owner_label=cred_owner_label,
            credential_display_name=inspected.credential_display_name,
            credential_purpose=inspected.credential_purpose,
            credential_public_key_fingerprint=inspected.credential_public_key_fingerprint,
            bootstrap_credential_key=normalized_boot_key,
            bootstrap_credential_owner_scope=resolved_boot_scope,
            bootstrap_credential_owner_label=boot_owner_label,
            bootstrap_credential_display_name=inspected.bootstrap_credential_display_name,
            bootstrap_credential_purpose=inspected.bootstrap_credential_purpose,
            bootstrap_credential_username=inspected.bootstrap_credential_username,
            lifecycle=parsed_lifecycle,
            target_expires_at_utc=normalized_expiry,
            enrollment_digest=enrollment_digest,
        )
        plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
        self._plan_store().save(plan)
        return self._staged(plan, self.describe(plan.change_id))

    def _ensure_target_id_available(self, target_id: str) -> None:
        if any(
            item.target_id == target_id
            for item in self.execution.config.execution_control.ssh.targets
        ):
            raise ValueError(f"SSH target id {target_id!r} conflicts with an operator-configured target.")
        if self.execution.repository.get_ssh_target(
            team_id=self.execution.owner.team_id, target_id=target_id
        ) is not None:
            raise ValueError(f"SSH target id {target_id!r} is already enrolled.")

    def load_plan(self, change_id: str) -> SSHTargetEnrollChangePlan:
        plan = self._plan_store().load(str(change_id or "").strip())
        self._check_binding(plan)
        return plan

    def _verify_current(self, plan: SSHTargetEnrollChangePlan) -> None:
        ssh_settings = self.execution.config.execution_control.ssh
        if not ssh_settings.enabled:
            raise RuntimeError("execution_control.ssh.enabled is false.")
        if self.execution.repository.get_ssh_target(
            team_id=plan.owner.team_id, target_id=plan.target_id
        ) is not None:
            raise FileExistsError(f"SSH target enrollment change {plan.change_id} was already applied.")
        if any(item.target_id == plan.target_id for item in ssh_settings.targets):
            raise PermissionError(
                f"SSH target id {plan.target_id!r} now conflicts with an operator-configured target."
            )
        if plan.target_expires_at_utc is not None and plan.target_expires_at_utc <= utc_now():
            raise PermissionError(
                "The target expiry time passed before approval. Stage a fresh enrollment plan."
            )
        validate_ssh_host(
            plan.host,
            plan.port,
            SSHNetworkPolicy.from_settings(ssh_settings),
            resolver=self._resolver,
            local_address_checker=self._local_address_checker,
        )
        self._inspect_catalog_credentials(
            target_username=plan.username,
            credential_key=plan.credential_key,
            credential_owner_scope=plan.credential_owner_scope,
            requested_public_key_fingerprint=plan.credential_public_key_fingerprint,
            bootstrap_credential_key=plan.bootstrap_credential_key,
            bootstrap_credential_owner_scope=plan.bootstrap_credential_owner_scope,
        )

    def describe(self, change_id: str) -> ChangeProposalV1:
        plan = self.load_plan(change_id)
        self._verify_current(plan)
        verify_cmd = _host_key_verification_command(plan.platform, plan.host_key_algorithm)
        lifecycle_line = (
            f"Lifecycle: temporary — expires at {plan.target_expires_at_utc.isoformat()}."
            if plan.lifecycle == SSHTargetLifecycle.TEMPORARY and plan.target_expires_at_utc is not None
            else (
                f"Lifecycle: persistent (expires at {plan.target_expires_at_utc.isoformat()})."
                if plan.target_expires_at_utc is not None
                else "Lifecycle: persistent until revoked by an administrator."
            )
        )
        cred_line = (
            f"SSH key credential: {plan.credential_display_name} ({plan.credential_key}; "
            f"owner: {plan.credential_owner_label}; purpose: {plan.credential_purpose}; "
            f"public key {plan.credential_public_key_fingerprint})."
        )
        effect = [
            f"Enrolls SSH target {plan.target_id} for {plan.username}@{plan.host}:{plan.port} (platform: {plan.platform.value}).",
            f"Pinned host key: {plan.host_key_algorithm} {plan.host_key_fingerprint}. Any future host-key mismatch fails closed.",
            f"Verify on the target machine before approving: {verify_cmd}",
            cred_line,
        ]
        if plan.bootstrap_credential_key is not None and plan.bootstrap_credential_owner_scope is not None:
            effect.append(
                f"Bootstrap credential: {plan.bootstrap_credential_display_name or plan.bootstrap_credential_key} "
                f"({plan.bootstrap_credential_key}; owner: {plan.bootstrap_credential_owner_label or 'the team'}; "
                f"username: {plan.bootstrap_credential_username or plan.username}; "
                f"purpose: {plan.bootstrap_credential_purpose or 'No purpose recorded'}; "
                "usable only once if password bootstrap is approved)."
            )
        effect.extend(
            [
                lifecycle_line,
                "Enrolling this host does not allow typing, commands, sudo, or password bootstrap; each session needs its own approval.",
                f"Reason: {plan.reason}",
                _STALE,
            ]
        )
        return ChangeProposalV1(
            kind=self.kind,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=f"Enroll SSH target {plan.username}@{plan.host}:{plan.port} ({plan.platform.value})",
            summary=summary_line(
                f"Pin host key {plan.host_key_fingerprint} for {plan.username}@{plan.host}:{plan.port} "
                f"({plan.lifecycle.value}) using {plan.credential_display_name} ({plan.credential_key})."
            ),
            effect=tuple(line[:1000] for line in effect),
        )

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Persist the approved enrolled SSH target (only the change mechanism calls this)."""

        plan = self.load_plan(change_id)
        self._check_approval(plan, approval)
        self._verify_current(plan)
        activation = self._activation(
            plan,
            approval,
            kind=ExecutionActivationKind.SSH_TARGET_ENROLL,
            resource_id=plan.target_id,
            resource_digest=plan.enrollment_digest,
            generation=1,
            expires_at_utc=plan.target_expires_at_utc,
        )
        record = EnrolledSSHTargetV1(
            target_id=plan.target_id,
            owner=plan.owner,
            host=plan.host,
            port=plan.port,
            username=plan.username,
            platform=plan.platform,
            host_key_algorithm=plan.host_key_algorithm,
            host_key_fingerprint=plan.host_key_fingerprint,
            host_key_public_key=plan.host_key_public_key,
            credential_key=plan.credential_key,
            credential_owner_scope=plan.credential_owner_scope,
            credential_public_key_fingerprint=plan.credential_public_key_fingerprint,
            bootstrap_credential_key=plan.bootstrap_credential_key,
            bootstrap_credential_owner_scope=plan.bootstrap_credential_owner_scope,
            lifecycle=plan.lifecycle,
            expires_at_utc=plan.target_expires_at_utc,
            reason=plan.reason,
            activation=activation,
        )
        if record.enrollment_digest() != plan.enrollment_digest:
            raise PermissionError("Enrolled SSH target digest no longer matches the approved plan.")
        saved = self.execution.repository.put_ssh_target(record)
        self._audit(plan, "applied")
        logger.info(
            "Enrolled admin-approved SSH target. event=execution_ssh_target_enrolled "
            "target_id=%s host=%s port=%s platform=%s lifecycle=%s",
            saved.target_id,
            saved.host,
            saved.port,
            saved.platform.value,
            saved.lifecycle.value,
        )
        return {
            "status": "applied",
            "change_id": plan.change_id,
            "plan_digest": plan.plan_digest,
            "target": saved.model_dump(mode="json"),
            "next_step": (
                f"Create a session with create_execution_ssh_session(target_id='{saved.target_id}'), "
                "then stage bounded capabilities with plan_execution_ssh_interaction."
            ),
        }


class SSHInteractionChangeService(_ExecutionChanges):
    """Approve time-bounded SSH capabilities and optional single-request worker delegation for one session."""

    kind = SSH_INTERACTION_CHANGE_KIND
    label = "SSH delegation change"

    def _known_agent_types(self) -> frozenset[str]:
        from custom_tools.agent_team_port import current_agent_type_map

        return frozenset(str(key).strip() for key in current_agent_type_map() if str(key).strip())

    def _plan_store(self) -> FilePlanStore[SSHInteractionChangePlan]:
        return FilePlanStore(
            self._plans_dir("ssh-interactions"),
            SSHInteractionChangePlan,
            change_id_pattern=_SSH_INTERACTION_CHANGE_ID,
            invalid_change_id_message="Invalid SSH delegation change id.",
            missing_plan_message="SSH delegation change plan not found: {change_id}",
        )

    def _resolve_delegation_window(
        self,
        *,
        expires_at_utc: datetime | None,
        ttl_seconds: int | None,
        now: datetime,
    ) -> tuple[int | None, datetime | None]:
        max_ttl = self.execution.config.execution_control.ssh.max_delegation_ttl_seconds
        if expires_at_utc is None and ttl_seconds is None:
            raise ValueError(
                "SSH session delegation requires a duration (ttl_seconds) or hard deadline (expires_at_utc)."
            )
        resolved_ttl: int | None = None
        if ttl_seconds is not None:
            if isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
                raise ValueError("ttl_seconds must be a positive integer.")
            if ttl_seconds > max_ttl:
                raise ValueError(
                    f"Requested delegation TTL ({ttl_seconds}s) exceeds execution_control.ssh.max_delegation_ttl_seconds ({max_ttl}s)."
                )
            resolved_ttl = int(ttl_seconds)
        resolved_deadline: datetime | None = None
        if expires_at_utc is not None:
            if expires_at_utc.tzinfo is None:
                raise ValueError("expires_at_utc must include a timezone.")
            normalized = expires_at_utc.astimezone(timezone.utc)
            if normalized <= now:
                raise ValueError("expires_at_utc must be in the future.")
            if resolved_ttl is None and normalized > now + timedelta(seconds=max_ttl):
                raise ValueError(
                    f"Requested delegation expires_at_utc exceeds execution_control.ssh.max_delegation_ttl_seconds ({max_ttl}s)."
                )
            resolved_deadline = normalized
        return resolved_ttl, resolved_deadline

    def _check_existing_interaction(
        self,
        session_id: str,
        *,
        change_id: str | None = None,
        now: datetime,
    ) -> None:
        existing = self.execution.ssh_interaction(session_id)
        if existing is None:
            return
        if existing.revoked_at_utc is not None:
            raise PermissionError(
                "This SSH session's delegation was revoked; create a new session."
            )
        if not existing.is_active(now=now):
            raise PermissionError(
                "This SSH session's approval expired; create a new session."
            )
        current_work_id = str(self.execution.actor.agent.work_id or "").strip() or None
        if (
            existing.bounds is not None
            and existing.bounds.delegated_task_id is not None
            and existing.bounds.delegated_task_id != current_work_id
        ):
            raise PermissionError(SSH_SINGLE_REQUEST_ENDED_MESSAGE)
        if change_id is not None:
            raise FileExistsError(f"SSH delegation change {change_id} was already applied.")
        raise ValueError("Delegation for this SSH session is already approved.")

    def _validate_target_capabilities(
        self,
        session: InteractiveSessionV1,
        bounds: SSHDelegationBoundsV1,
    ) -> None:
        ssh_settings = self.execution.config.execution_control.ssh
        enrolled_target: EnrolledSSHTargetV1 | None = None
        if session.target.target_id is not None:
            enrolled_target = self.execution.usable_ssh_target(session.target.target_id)
            if session.target != enrolled_target.to_connection_target():
                raise PermissionError(
                    "The SSH session's target no longer matches the enrolled target record."
                )

        if bounds.allow_password_bootstrap:
            if enrolled_target is None:
                raise ValueError(
                    "Password bootstrap is only supported for dynamically enrolled SSH targets."
                )
            if not ssh_settings.allow_password_bootstrap:
                raise PermissionError(
                    "execution_control.ssh.allow_password_bootstrap is false; password bootstrap is disabled by operator policy."
                )
            if enrolled_target.bootstrap_credential_key is None or enrolled_target.bootstrap_credential_owner_scope is None:
                raise ValueError(
                    "Password bootstrap requires an enrolled SSH target with a bootstrap_credential_key."
                )
            if enrolled_target.password_bootstrap_completed_at_utc is not None:
                raise PermissionError(
                    "Password bootstrap has already completed and is permanently latched off for this target."
                )

        if bounds.allow_broker_sudo or bounds.allowed_secret_recipes:
            if enrolled_target is None:
                raise ValueError(
                    "Broker-assisted sudo and secret recipes require a dynamically enrolled SSH target with a bootstrap credential."
                )
            if enrolled_target.platform == SSHTargetPlatform.WINDOWS:
                raise ValueError("Broker-assisted sudo is not supported on Windows targets.")
            if enrolled_target.bootstrap_credential_key is None or enrolled_target.bootstrap_credential_owner_scope is None:
                raise ValueError(
                    "Broker-assisted sudo requires an enrolled SSH target with a bootstrap_credential_key."
                )

        needs_bootstrap_cred = bool(
            bounds.allow_password_bootstrap or bounds.allow_broker_sudo or bounds.allowed_secret_recipes
        )
        if needs_bootstrap_cred:
            assert enrolled_target is not None
            assert enrolled_target.bootstrap_credential_key is not None
            assert enrolled_target.bootstrap_credential_owner_scope is not None
            boot_meta = self._read_credential_metadata(
                enrolled_target.bootstrap_credential_key,
                scope=_catalog_scope(enrolled_target.bootstrap_credential_owner_scope),
            )
            if boot_meta is None:
                raise PermissionError(
                    f"Bootstrap credential {enrolled_target.bootstrap_credential_key!r} is not registered in the "
                    f"{enrolled_target.bootstrap_credential_owner_scope.scope_kind.value} credential catalog."
                )
            _require_secret_field(
                boot_meta,
                credential_key=enrolled_target.bootstrap_credential_key,
                field_name="admin_password",
            )
            if SSHSecretRecipe.TAILSCALE_UP in bounds.allowed_secret_recipes:
                _require_secret_field(
                    boot_meta,
                    credential_key=enrolled_target.bootstrap_credential_key,
                    field_name="tailscale_auth_key",
                )

    def stage(
        self,
        *,
        session_id: str,
        reason: str,
        ttl_seconds: int | None = None,
        expires_at_utc: datetime | None = None,
        delegated_agent_type: str | None = None,
        this_request_only: bool | None = None,
        allow_commands: bool = False,
        allow_typing: bool = False,
        allow_password_bootstrap: bool = False,
        allow_broker_sudo: bool = False,
        allow_workspace_transfer: bool = False,
        allowed_secret_recipes: tuple[SSHSecretRecipe | str, ...] = (),
    ) -> dict[str, Any]:
        binding = self._binding(reason)
        now = utc_now()
        resolved_ttl, resolved_deadline = self._resolve_delegation_window(
            expires_at_utc=expires_at_utc,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        normalized_agent_type = str(delegated_agent_type or "").strip() or None
        if normalized_agent_type is not None:
            known_types = self._known_agent_types()
            if normalized_agent_type not in known_types:
                raise ValueError(
                    f"Unknown agent type {normalized_agent_type!r}. Choose a registered agent type for this team."
                )
        restrict_to_request = (
            normalized_agent_type is not None
            if this_request_only is None
            else bool(this_request_only)
        )
        delegated_work_id: str | None = None
        if restrict_to_request:
            work_id = str(self.execution.actor.agent.work_id or "").strip()
            if not work_id:
                raise PermissionError(
                    "Cannot restrict SSH delegation to this request without a verified signed task context."
                )
            delegated_work_id = work_id
        parsed_recipes = tuple(SSHSecretRecipe(recipe) for recipe in allowed_secret_recipes)
        bounds = SSHDelegationBoundsV1(
            delegated_agent_type=normalized_agent_type,
            delegated_task_id=delegated_work_id,
            allow_commands=bool(allow_commands),
            allow_typing=bool(allow_typing),
            allow_password_bootstrap=bool(allow_password_bootstrap),
            allow_broker_sudo=bool(allow_broker_sudo),
            allow_workspace_transfer=bool(allow_workspace_transfer),
            allowed_secret_recipes=parsed_recipes,
            reason=binding["reason"],
        )
        if not any(
            (
                bounds.allow_commands,
                bounds.allow_typing,
                bounds.allow_password_bootstrap,
                bounds.allow_broker_sudo,
                bounds.allow_workspace_transfer,
            )
        ):
            raise ValueError(
                "Enable at least one SSH capability flag (allow_commands, allow_typing, "
                "allow_password_bootstrap, allow_broker_sudo, or allow_workspace_transfer)."
            )
        session = self._session_that_can_open(session_id)
        self._check_existing_interaction(session.session_id, now=now)
        self._validate_target_capabilities(session, bounds)
        resource_digest = ssh_interaction_resource_digest(
            target_digest=session.target_digest, bounds=bounds
        )
        plan = SSHInteractionChangePlan(
            change_id=new_id(SSH_INTERACTION_CHANGE_PREFIX),
            **binding,
            session_id=session.session_id,
            target=session.target,
            target_digest=session.target_digest,
            delegation_ttl_seconds=resolved_ttl,
            delegation_not_after_utc=resolved_deadline,
            bounds=bounds,
            resource_digest=resource_digest,
        )
        plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
        self._plan_store().save(plan)
        return self._staged(plan, self.describe(plan.change_id))

    def _session_that_can_open(self, session_id: str) -> InteractiveSessionV1:
        session = self.execution.owned_session(session_id)
        if self.execution.repository.get_interactive_session_revocation(
            team_id=self.execution.owner.team_id, session_id=session.session_id
        ) is not None:
            raise PermissionError("This SSH session was revoked; create a new session.")
        if session.connect_activation is None:
            raise PermissionError("This SSH session cannot be opened any more; create a new session.")
        return session

    def load_plan(self, change_id: str) -> SSHInteractionChangePlan:
        store = self._plan_store()
        normalized = str(change_id or "").strip()
        path = store.path_for(normalized)
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, dict) and (
                raw.get("schema_version", 1) == 1
                or (
                    "delegation_ttl_seconds" not in raw
                    and "delegation_not_after_utc" not in raw
                    and "bounds" not in raw
                )
            ):
                raise PermissionError(
                    "This SSH interaction request was staged before the upgrade; stage it again with a time limit."
                )
        plan = store.load(normalized)
        self._check_binding(plan)
        return plan

    def _verify_current(self, plan: SSHInteractionChangePlan, *, now: datetime | None = None) -> InteractiveSessionV1:
        """Not approved/revoked/expired yet, delegation window within policy, and session/target unchanged."""

        current = now or utc_now()
        self._check_existing_interaction(plan.session_id, change_id=plan.change_id, now=current)
        max_ttl = self.execution.config.execution_control.ssh.max_delegation_ttl_seconds
        if plan.delegation_ttl_seconds is not None and plan.delegation_ttl_seconds > max_ttl:
            raise PermissionError(
                f"The requested SSH delegation duration ({plan.delegation_ttl_seconds}s) now exceeds "
                f"execution_control.ssh.max_delegation_ttl_seconds ({max_ttl}s). Stage a fresh plan."
            )
        if plan.delegation_not_after_utc is not None:
            if plan.delegation_not_after_utc <= current:
                raise PermissionError(
                    "The requested SSH delegation deadline passed before approval. Stage a fresh plan."
                )
            if (
                plan.delegation_ttl_seconds is None
                and plan.delegation_not_after_utc > current + timedelta(seconds=max_ttl)
            ):
                raise PermissionError(
                    f"The requested SSH delegation expiry now exceeds execution_control.ssh.max_delegation_ttl_seconds ({max_ttl}s). Stage a fresh plan."
                )
        if plan.bounds.delegated_agent_type is not None:
            if plan.bounds.delegated_agent_type not in self._known_agent_types():
                raise PermissionError(
                    f"Delegated agent type {plan.bounds.delegated_agent_type!r} is no longer registered for this team. Stage a fresh plan."
                )
        session = self._session_that_can_open(plan.session_id)
        if session.target != plan.target or session.target_digest != plan.target_digest:
            raise PermissionError("The SSH session's target changed after planning. Stage a fresh plan.")
        expected_digest = ssh_interaction_resource_digest(
            target_digest=session.target_digest, bounds=plan.bounds
        )
        if plan.resource_digest != expected_digest:
            raise PermissionError("The SSH delegation plan digest no longer matches its bounds.")
        self._validate_target_capabilities(session, plan.bounds)
        return session

    @staticmethod
    def _window_phrase(plan: SSHInteractionChangePlan) -> str:
        if plan.delegation_ttl_seconds is not None and plan.delegation_not_after_utc is not None:
            return (
                f"for {_format_duration(plan.delegation_ttl_seconds)} from approval "
                f"(not later than {plan.delegation_not_after_utc.isoformat()})"
            )
        if plan.delegation_ttl_seconds is not None:
            return f"for {_format_duration(plan.delegation_ttl_seconds)} from approval"
        assert plan.delegation_not_after_utc is not None
        return f"until {plan.delegation_not_after_utc.isoformat()}"

    def _compute_applied_expiry(
        self,
        plan: SSHInteractionChangePlan,
        approval: ChangeApproval,
        *,
        now: datetime,
    ) -> datetime:
        max_ttl = self.execution.config.execution_control.ssh.max_delegation_ttl_seconds
        approved_at = approval.approved_at_utc.astimezone(timezone.utc)
        if plan.delegation_ttl_seconds is not None:
            if plan.delegation_ttl_seconds > max_ttl:
                raise PermissionError(
                    f"The requested SSH delegation duration ({plan.delegation_ttl_seconds}s) now exceeds "
                    f"execution_control.ssh.max_delegation_ttl_seconds ({max_ttl}s). Stage a fresh plan."
                )
            computed = approved_at + timedelta(seconds=plan.delegation_ttl_seconds)
            if plan.delegation_not_after_utc is not None:
                computed = min(computed, plan.delegation_not_after_utc)
        else:
            assert plan.delegation_not_after_utc is not None
            computed = plan.delegation_not_after_utc
            if computed > approved_at + timedelta(seconds=max_ttl):
                raise PermissionError(
                    f"The requested SSH delegation expiry now exceeds execution_control.ssh.max_delegation_ttl_seconds ({max_ttl}s). Stage a fresh plan."
                )
        if computed <= now:
            raise PermissionError(
                "The approved SSH delegation window already expired before apply. Stage a fresh plan."
            )
        return computed

    def describe(self, change_id: str) -> ChangeProposalV1:
        plan = self.load_plan(change_id)
        self._verify_current(plan)
        target = plan.target
        bounds = plan.bounds
        acting_agent = bounds.delegated_agent_type or plan.actor_named_agent_type
        enabled_caps: list[str] = []
        if bounds.allow_commands:
            enabled_caps.append("commands")
        if bounds.allow_typing:
            enabled_caps.append("interactive typing")
        if bounds.allow_password_bootstrap:
            enabled_caps.append("one-time password key bootstrap")
        if bounds.allow_broker_sudo:
            enabled_caps.append("broker-assisted sudo")
        if bounds.allow_workspace_transfer:
            enabled_caps.append("workspace bundle transfer")
        caps_summary = ", ".join(enabled_caps)

        def _flag(value: bool) -> str:
            return "ALLOWED" if value else "disabled"

        scope_line = (
            f"Delegated agent type: {bounds.delegated_agent_type} (requested by {plan.actor_named_agent_type})"
            if bounds.delegated_agent_type
            else f"Allowed agent type: {plan.actor_named_agent_type} only"
        )
        task_line = (
            "Request scope: this request only."
            if bounds.delegated_task_id
            else "Request scope: any request by this person on this session until expiry."
        )
        window_phrase = self._window_phrase(plan)
        effect = [
            (
                f"Authorizes {acting_agent} on SSH session {plan.session_id} for "
                f"{target.username}@{target.host}:{target.port} "
                f"(pinned host key {target.host_key_algorithm} {target.host_key_fingerprint})."
            ),
            f"{scope_line}. {task_line}",
            f"Time limit: {window_phrase} (can be revoked immediately in Settings).",
            (
                "Capabilities: "
                f"allow_commands={_flag(bounds.allow_commands)}, "
                f"allow_typing={_flag(bounds.allow_typing)}, "
                f"allow_password_bootstrap={_flag(bounds.allow_password_bootstrap)}, "
                f"allow_broker_sudo={_flag(bounds.allow_broker_sudo)}, "
                f"allow_workspace_transfer={_flag(bounds.allow_workspace_transfer)}."
            ),
        ]
        if bounds.allow_broker_sudo:
            recipes = (
                ", ".join(recipe.value for recipe in bounds.allowed_secret_recipes)
                if bounds.allowed_secret_recipes
                else "none"
            )
            effect.append(f"Approved broker secret recipes (allowed_secret_recipes): {recipes}.")
        if (
            bounds.allow_password_bootstrap
            or bounds.allow_broker_sudo
            or bounds.allowed_secret_recipes
        ):
            effect.append(_REMOTE_HOST_SECRET_BOUNDARY_NOTE)
        effect.extend([f"Reason: {plan.reason}", _STALE])
        return ChangeProposalV1(
            kind=self.kind,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=f"Approve SSH delegation ({caps_summary}) for {acting_agent} on {target.username}@{target.host}",
            summary=summary_line(
                f"Grant {acting_agent} {caps_summary} on {target.username}@{target.host} {window_phrase}."
            ),
            effect=tuple(line[:1000] for line in effect),
        )

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Record the approved bounded SSH delegation (only the change mechanism calls this)."""

        plan = self.load_plan(change_id)
        self._check_approval(plan, approval)
        now = utc_now()
        session = self._verify_current(plan, now=now)
        computed_expiry = self._compute_applied_expiry(plan, approval, now=now)
        activation = self._activation(
            plan,
            approval,
            kind=ExecutionActivationKind.SSH_INTERACT,
            resource_id=session.session_id,
            resource_digest=plan.resource_digest,
            generation=1,
            expires_at_utc=computed_expiry,
        )
        saved = self.execution.repository.put_ssh_interaction(activation, bounds=plan.bounds)
        self._audit(plan, "applied")
        logger.info(
            "Approved bounded SSH delegation. event=execution_ssh_delegation_approved "
            "session_id=%s delegated_agent_type=%s delegated_task_id=%s expires_at_utc=%s",
            session.session_id,
            plan.bounds.delegated_agent_type or plan.actor_named_agent_type,
            plan.bounds.delegated_task_id or "",
            computed_expiry.isoformat(),
        )
        return {
            "status": "applied",
            "change_id": plan.change_id,
            "plan_digest": plan.plan_digest,
            "session_id": session.session_id,
            "interaction": saved.activation.model_dump(mode="json"),
            "bounds": saved.bounds.model_dump(mode="json") if saved.bounds is not None else None,
            "expires_at_utc": computed_expiry.isoformat(),
            "next_step": "Use the approved SSH capabilities within the expiry window; close the session when the task is done.",
        }


__all__ = [
    "EXECUTION_CHANGE_PLAN_TTL_SECONDS",
    "MANAGED_SERVICE_CHANGE_KIND",
    "MANAGED_SERVICE_CHANGE_PREFIX",
    "SSH_INTERACTION_CHANGE_KIND",
    "SSH_INTERACTION_CHANGE_PREFIX",
    "SSH_TARGET_ENROLL_CHANGE_KIND",
    "SSH_TARGET_ENROLL_CHANGE_PREFIX",
    "ManagedServiceChangePlan",
    "ManagedServiceChangeService",
    "SSHInteractionChangePlan",
    "SSHInteractionChangeService",
    "SSHTargetEnrollChangePlan",
    "SSHTargetEnrollChangeService",
]
