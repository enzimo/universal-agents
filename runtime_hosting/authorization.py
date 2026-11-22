"""Checks, immediately before each use, that an execution activation still gives authority.

The execution controller calls this before every job run and service
reconcile, and the SSH broker before opening, reading, resizing and typing.
It rebuilds the exact resource from trusted records (ids, digests, generation,
owner) and then checks the activation's approval
(``ActivationRecordV2.approval``) with the access service
(``access_control.decisions``, real Cedar):

- **held access** (jobs, SSH sessions): the recorded agent type and person
  must still hold the recorded catalog operation's current level, so revoking
  or letting a grant expire stops the next run or SSH call. A job runs later
  and unattended, so only access that outlives the task that created it counts
  (task-bound and one-time grants do not). An SSH session is used by its agent
  inside its task, so the check keeps that task;
- **a person's approval** (managed services, typing into SSH): the approval
  record is the authority for that exact change;
- **agreement**: honored only while authorization stays off; with it on, the
  change needs one of the above. Authorization off honors agreement only.

A job whose profile runs code on the controller host, outside any sandbox,
also needs the recorded agent type and person to hold full access to
``code:unconfined`` (decision D9), like a shell command run unconfined; only
access that outlives the creating task counts. With authorization off the
operator's profile setting alone allows it.

Any failure, including an access check that cannot run, refuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import logging

from access_catalog.catalog import CatalogSource, ToolCatalog
from access_control.decisions import AccessService, UNCONFINED_EXECUTION_OPERATION, UNCONFINED_EXECUTION_TOOL_ID
from access_control.entities import AgentPrincipal, PersonPrincipal
from custom_messages.contracts.access_v2 import (
    AccessDecisionV1,
    AccessLevel,
    ChangeApprovalMode,
    ClassificationSource,
    OperationResolutionV1,
)
from runtime_hosting.execution_contracts import (
    ActivationRecordV2,
    AutomationJobV2,
    EnrolledSSHTargetV1,
    ExecutionActivationKind,
    ExecutionRevisionV1,
    InteractiveSessionV1,
    ManagedServiceV1,
    OwnerScopeKind,
    RunAttemptV1,
    SSHInteractionRecordV1,
    SSHRevocationStateV1,
    SSHSecretRecipe,
    SSHTargetPlatform,
    SSHTargetStatus,
    utc_now,
)

logger = logging.getLogger(__name__)


class ExecutionAuthorizationDenied(PermissionError):
    pass


SSH_SINGLE_REQUEST_ENDED_MESSAGE = (
    "This approval is for another request. Create a new session; "
    "to keep its approval across messages, stage it with this_request_only=false."
)


class SSHCapability(StrEnum):
    """Granular SSH operations checked by ``ExecutionAccessGuard`` before every call."""

    CONNECT = "connect"
    COMMAND = "command"
    TYPING = "typing"
    PASSWORD_BOOTSTRAP = "password_bootstrap"
    BROKER_SUDO = "broker_sudo"
    WORKSPACE_TRANSFER = "workspace_transfer"


@dataclass(frozen=True)
class SSHCallerContext:
    """Verified agent, person, and request identity attempting an SSH broker operation.

    ``task_id`` is the verified request id (``AgentPrincipal.work_id`` /
    ``TaskContextV1.context_id``), which travels unchanged across the team
    lead's task and every worker subtask delegated for that person's request.
    It is never an agent-local task id or agent-supplied string.
    """

    agent_type: str
    task_id: str | None = None
    person_id: str | None = None


@dataclass
class ExecutionAccessGuard:
    """Re-checks activations; ``access`` and ``catalogs`` are None with authorization off."""

    authorization_enabled: bool
    access: AccessService | None = None
    catalogs: CatalogSource | None = None

    def __post_init__(self) -> None:
        if self.authorization_enabled and (self.access is None or self.catalogs is None):
            raise ValueError("With authorization on, execution checks need the access service and catalog.")

    # -- uses -------------------------------------------------------------------

    def require_run(
        self,
        *,
        job: AutomationJobV2,
        attempt: RunAttemptV1,
        revision: ExecutionRevisionV1,
        host_execution: bool = False,
    ) -> None:
        """``host_execution``: the job's profile runs it on the controller host."""

        if attempt.owner != job.owner or revision.owner != job.owner:
            raise ExecutionAuthorizationDenied("Execution owner scope changed across trusted records.")
        if (
            attempt.revision_id != job.revision_id
            or revision.revision_id != job.revision_id
            or attempt.revision_digest != job.revision_digest
            or revision.revision_digest != job.revision_digest
        ):
            raise ExecutionAuthorizationDenied("Run attempt no longer binds the exact job revision.")
        activation = self._active(job.activation, ExecutionActivationKind.RUN, "Execution")
        if (
            activation.resource_id != job.job_id
            or activation.resource_digest != job.activation_resource_digest()
            or activation.generation != job.generation
        ):
            raise ExecutionAuthorizationDenied("Execution activation no longer binds the exact job.")
        self.require_authority(activation)
        if host_execution:
            self.require_host_execution(activation)

    def require_host_execution(self, activation: ActivationRecordV2) -> None:
        """Code outside any sandbox needs full access to code:unconfined (see the module docstring)."""

        if not self.authorization_enabled:
            return
        decision = self._decide_for_holder(
            activation,
            OperationResolutionV1(
                tool_id=UNCONFINED_EXECUTION_TOOL_ID,
                operation=UNCONFINED_EXECUTION_OPERATION,
                level=AccessLevel.FULL,
                source=ClassificationSource.TRUSTED,
            ),
            task_id=None,
        )
        if not decision.allowed:
            raise ExecutionAuthorizationDenied(
                f"This job's profile runs its code on the controller host, outside any sandbox, so "
                f"{activation.holder.agent_type} and the person it works for need full access to "
                f"{UNCONFINED_EXECUTION_TOOL_ID} that lasts beyond one task: {decision.reason}"
            )

    def require_service(self, service: ManagedServiceV1, revision: ExecutionRevisionV1) -> None:
        activation = self._active(service.activation, ExecutionActivationKind.SERVICE, "Managed-service")
        if service.owner != revision.owner or service.revision_id != revision.revision_id:
            raise ExecutionAuthorizationDenied("Managed service no longer binds its exact revision.")
        if (
            activation.resource_id != service.service_id
            or activation.resource_digest != service.activation_resource_digest()
            or activation.generation != service.generation
        ):
            raise ExecutionAuthorizationDenied(
                "Managed-service activation no longer binds the exact desired state."
            )
        self.require_authority(activation)

    def require_ssh_target(
        self,
        target: EnrolledSSHTargetV1,
        *,
        session: InteractiveSessionV1 | None = None,
        now: datetime | None = None,
    ) -> None:
        """Verify an enrolled SSH target is active, unexpired, unrevoked, and matches its pinned host key."""

        current = now or utc_now()
        if target.status == SSHTargetStatus.REVOKED or target.revoked_at_utc is not None:
            raise ExecutionAuthorizationDenied("Enrolled SSH target has been revoked.")
        if target.effective_status(now=current) != SSHTargetStatus.ACTIVE:
            raise ExecutionAuthorizationDenied("Enrolled SSH target has expired or is no longer active.")
        activation = self._active(
            target.activation,
            ExecutionActivationKind.SSH_TARGET_ENROLL,
            "SSH target enrollment",
            now=current,
        )
        if (
            activation.resource_id != target.target_id
            or activation.owner != target.owner
            or activation.generation != 1
            or activation.resource_digest != target.enrollment_digest()
        ):
            raise ExecutionAuthorizationDenied(
                "SSH target enrollment activation no longer binds the exact enrolled target."
            )
        self.require_authority(activation)
        if session is not None:
            if session.target.target_id != target.target_id:
                raise ExecutionAuthorizationDenied(
                    "SSH session does not belong to this enrolled target."
                )
            if session.owner.team_id != target.owner.team_id or (
                target.owner != session.owner and target.owner.scope_kind != OwnerScopeKind.TEAM
            ):
                raise ExecutionAuthorizationDenied(
                    "SSH session owner is not allowed to use this enrolled target."
                )
            expected_connection = target.to_connection_target()
            if (
                session.target != expected_connection
                or session.target_digest != expected_connection.calculate_digest()
            ):
                raise ExecutionAuthorizationDenied(
                    "SSH session target no longer matches the enrolled target's pinned host key and binding."
                )

    def require_ssh_connect(
        self,
        session: InteractiveSessionV1,
        *,
        caller: SSHCallerContext,
        enrolled_target: EnrolledSSHTargetV1 | None = None,
        session_revocation: SSHRevocationStateV1 | None = None,
        interaction: SSHInteractionRecordV1 | None = None,
        now: datetime | None = None,
    ) -> None:
        """Opening, reading and resizing: session, target, caller, and connect authority still hold."""

        if caller is None:
            raise ExecutionAuthorizationDenied("SSH operations require verified caller identity.")
        current = now or utc_now()
        if session_revocation is not None:
            raise ExecutionAuthorizationDenied("SSH session has been revoked.")
        if session.target.target_id is not None:
            if enrolled_target is None:
                raise ExecutionAuthorizationDenied(
                    "Enrolled SSH target record is missing or no longer available."
                )
            self.require_ssh_target(enrolled_target, session=session, now=current)
        activation = self._active(
            session.connect_activation,
            ExecutionActivationKind.SSH_CONNECT,
            "SSH",
            now=current,
        )
        if not activation.binds_session(session):
            raise ExecutionAuthorizationDenied("SSH activation does not bind the exact session target.")
        self.require_authority(activation)
        self._require_connect_caller(
            session=session,
            connect_activation=activation,
            interaction=interaction,
            caller=caller,
            now=current,
        )

    def _require_connect_caller(
        self,
        *,
        session: InteractiveSessionV1,
        connect_activation: ActivationRecordV2,
        interaction: SSHInteractionRecordV1 | None,
        caller: SSHCallerContext,
        now: datetime,
    ) -> None:
        holder = connect_activation.holder
        if holder.person_id is not None and caller.person_id != holder.person_id:
            raise ExecutionAuthorizationDenied("SSH session belongs to another person.")
        if caller.agent_type == holder.agent_type:
            return
        if interaction is None or interaction.bounds is None:
            raise ExecutionAuthorizationDenied(
                "Only the owning agent or an explicitly delegated agent type may use this SSH session."
            )
        if interaction.revoked_at_utc is not None or not interaction.is_active(now=now):
            raise ExecutionAuthorizationDenied(
                "The SSH delegation for this agent type has expired or been revoked."
            )
        if not interaction.activation.binds_session(session, bounds=interaction.bounds):
            raise ExecutionAuthorizationDenied("SSH delegation does not bind this session.")
        self.require_authority(interaction.activation)
        if interaction.bounds.delegated_agent_type != caller.agent_type:
            raise ExecutionAuthorizationDenied(
                f"This SSH session is delegated only to agent type {interaction.bounds.delegated_agent_type!r}."
            )
        if (
            interaction.bounds.delegated_task_id is not None
            and caller.task_id != interaction.bounds.delegated_task_id
        ):
            raise ExecutionAuthorizationDenied(SSH_SINGLE_REQUEST_ENDED_MESSAGE)

    def require_ssh_close(
        self,
        session: InteractiveSessionV1,
        *,
        caller: SSHCallerContext,
        interaction: SSHInteractionRecordV1 | None = None,
    ) -> None:
        """Who may close an SSH session: owning agent or the session's delegated worker.

        Closing only ends remote access and saves the redacted transcript, so
        expired or revoked sessions and delegations may still be closed by
        their owner or delegated worker.
        """

        if caller is None:
            raise ExecutionAuthorizationDenied("Closing an SSH session requires verified caller identity.")
        activation = session.connect_activation
        if activation is None:
            raise ExecutionAuthorizationDenied("SSH session is missing its connect activation.")
        holder = activation.holder
        if holder.person_id is not None and caller.person_id != holder.person_id:
            raise ExecutionAuthorizationDenied("SSH session belongs to another person.")
        if caller.agent_type == holder.agent_type:
            return
        bounds = interaction.bounds if interaction is not None else None
        if bounds is None or bounds.delegated_agent_type != caller.agent_type:
            raise ExecutionAuthorizationDenied(
                "Only the owning agent or the session's delegated worker may close this SSH session."
            )
        if bounds.delegated_task_id is not None and caller.task_id != bounds.delegated_task_id:
            raise ExecutionAuthorizationDenied(SSH_SINGLE_REQUEST_ENDED_MESSAGE)

    def require_ssh_capability(
        self,
        session: InteractiveSessionV1,
        interaction: SSHInteractionRecordV1 | None,
        *,
        capability: SSHCapability,
        caller: SSHCallerContext,
        enrolled_target: EnrolledSSHTargetV1 | None = None,
        session_revocation: SSHRevocationStateV1 | None = None,
        allow_password_bootstrap_setting: bool = False,
        secret_recipe: SSHSecretRecipe | None = None,
        now: datetime | None = None,
    ) -> None:
        """Enforce connect authority, delegation bounds, capability flags, and caller scope."""

        if caller is None:
            raise ExecutionAuthorizationDenied(
                f"SSH capability {capability.value!r} requires verified caller identity."
            )
        current = now or utc_now()
        self.require_ssh_connect(
            session,
            caller=caller,
            enrolled_target=enrolled_target,
            session_revocation=session_revocation,
            interaction=interaction,
            now=current,
        )
        if capability is SSHCapability.CONNECT:
            return
        if interaction is None:
            raise ExecutionAuthorizationDenied(
                "This SSH operation needs a person's approval. Call "
                "plan_execution_ssh_interaction for this session, then apply_change."
            )
        if interaction.revoked_at_utc is not None:
            raise ExecutionAuthorizationDenied("SSH session delegation has been revoked.")
        activation = self._active(
            interaction.activation,
            ExecutionActivationKind.SSH_INTERACT,
            "SSH delegation",
            now=current,
        )
        if not activation.binds_session(session, bounds=interaction.bounds):
            raise ExecutionAuthorizationDenied(
                "The SSH approval is for another session, owner, target, or delegation scope."
            )
        self.require_authority(activation)
        if activation.holder.person_id is not None and caller.person_id != activation.holder.person_id:
            raise ExecutionAuthorizationDenied("SSH delegation belongs to another person.")

        bounds = interaction.bounds
        if bounds is None:
            # Legacy 0.9 approval without delegation bounds covers interactive typing only.
            if capability is not SSHCapability.TYPING:
                raise ExecutionAuthorizationDenied(
                    f"SSH capability {capability.value!r} requires a bounded delegation approval."
                )
            if caller.agent_type != activation.holder.agent_type:
                raise ExecutionAuthorizationDenied(
                    "This SSH typing approval belongs to another agent type."
                )
            return

        if bounds.delegated_agent_type is not None:
            if caller.agent_type != bounds.delegated_agent_type:
                raise ExecutionAuthorizationDenied(
                    f"This SSH session is delegated only to agent type {bounds.delegated_agent_type!r}."
                )
        elif caller.agent_type != activation.holder.agent_type:
            raise ExecutionAuthorizationDenied(
                "This SSH delegation was approved only for its requesting agent type."
            )
        if bounds.delegated_task_id is not None and caller.task_id != bounds.delegated_task_id:
            raise ExecutionAuthorizationDenied(SSH_SINGLE_REQUEST_ENDED_MESSAGE)

        if capability is SSHCapability.COMMAND:
            if not bounds.allow_commands:
                raise ExecutionAuthorizationDenied(
                    "Remote command execution (allow_commands) was not approved on this SSH delegation."
                )
        elif capability is SSHCapability.TYPING:
            if not bounds.allow_typing:
                raise ExecutionAuthorizationDenied(
                    "Interactive typing (allow_typing) was not approved on this SSH delegation."
                )
        elif capability is SSHCapability.PASSWORD_BOOTSTRAP:
            if not bounds.allow_password_bootstrap:
                raise ExecutionAuthorizationDenied(
                    "Password key bootstrap (allow_password_bootstrap) was not approved on this SSH delegation."
                )
            if not allow_password_bootstrap_setting:
                raise ExecutionAuthorizationDenied(
                    "execution_control.ssh.allow_password_bootstrap is disabled by operator policy."
                )
            if enrolled_target is None or enrolled_target.bootstrap_credential_key is None:
                raise ExecutionAuthorizationDenied(
                    "Password bootstrap requires an enrolled SSH target with a bootstrap credential."
                )
            if enrolled_target.password_bootstrap_completed_at_utc is not None:
                raise ExecutionAuthorizationDenied(
                    "Password bootstrap has already completed and is permanently latched off for this target."
                )
        elif capability is SSHCapability.BROKER_SUDO:
            if not bounds.allow_broker_sudo:
                raise ExecutionAuthorizationDenied(
                    "Broker-assisted sudo (allow_broker_sudo) was not approved on this SSH delegation."
                )
            if enrolled_target is None or enrolled_target.bootstrap_credential_key is None:
                raise ExecutionAuthorizationDenied(
                    "Broker-assisted sudo requires an enrolled SSH target with a bootstrap credential."
                )
            if enrolled_target.platform == SSHTargetPlatform.WINDOWS:
                raise ExecutionAuthorizationDenied(
                    "Broker-assisted sudo is not supported on Windows targets."
                )
            if secret_recipe is not None and secret_recipe not in bounds.allowed_secret_recipes:
                raise ExecutionAuthorizationDenied(
                    f"Secret recipe {secret_recipe.value!r} was not approved on this SSH delegation."
                )
        elif capability is SSHCapability.WORKSPACE_TRANSFER:
            if not bounds.allow_workspace_transfer:
                raise ExecutionAuthorizationDenied(
                    "Workspace bundle transfer (allow_workspace_transfer) was not approved on this SSH delegation."
                )

    def require_ssh_interact(
        self,
        session: InteractiveSessionV1,
        interaction: SSHInteractionRecordV1 | None,
        *,
        caller: SSHCallerContext,
        enrolled_target: EnrolledSSHTargetV1 | None = None,
        session_revocation: SSHRevocationStateV1 | None = None,
        now: datetime | None = None,
    ) -> None:
        """Typing: connect authority still holds and a person approved typing in this session."""

        self.require_ssh_capability(
            session,
            interaction,
            capability=SSHCapability.TYPING,
            caller=caller,
            enrolled_target=enrolled_target,
            session_revocation=session_revocation,
            now=now,
        )

    # -- authority --------------------------------------------------------------

    def require_authority(self, activation: ActivationRecordV2) -> None:
        """Whether the activation's approval still counts (see the module docstring)."""

        mode = activation.approval.mode
        if not self.authorization_enabled:
            if mode is not ChangeApprovalMode.AGREEMENT:
                raise ExecutionAuthorizationDenied(
                    "Authorization is off, so only an activation recorded with the person's "
                    "agreement counts; activate it again with confirmed=true."
                )
            return
        if mode is ChangeApprovalMode.AGREEMENT:
            raise ExecutionAuthorizationDenied(
                "This activation was recorded with conversational agreement while authorization "
                "was off. Authorization is on now, so it must be activated again."
            )
        if mode is ChangeApprovalMode.PERSON_APPROVAL:
            return  # the person approved this exact change; the caller checked its digests
        decision = self.held_access(activation)
        if not decision.allowed:
            raise ExecutionAuthorizationDenied(
                f"{activation.holder.agent_type} or the person it works for no longer holds "
                f"{decision.level.value} access for {activation.operation}: {decision.reason}"
            )

    def held_access(self, activation: ActivationRecordV2) -> AccessDecisionV1:
        """Decide the recorded operation again for the recorded agent type and person."""

        # Jobs run later and unattended: only access that outlives the creating task counts.
        task_id = None if activation.kind is ExecutionActivationKind.RUN else activation.holder.task_id
        tool_id, _, function = activation.operation.partition("#")
        try:
            resolution = self._catalog().resolve_function(function)
        except Exception as exc:  # noqa: BLE001 - a check that cannot run refuses
            logger.exception(
                "Execution access re-check failed; refusing. event=execution_access_check_failed "
                "activation_id=%s resource_id=%s",
                activation.activation_id,
                activation.resource_id,
            )
            raise ExecutionAuthorizationDenied("The access check could not run, so the use was refused.") from exc
        if resolution.tool_id != tool_id:
            raise ExecutionAuthorizationDenied(
                f"{activation.operation} is no longer an operation of {tool_id} in the tool catalog."
            )
        return self._decide_for_holder(activation, resolution, task_id=task_id)

    def _decide_for_holder(
        self, activation: ActivationRecordV2, resolution: OperationResolutionV1, *, task_id: str | None
    ) -> AccessDecisionV1:
        """Decide one operation for the activation's recorded agent type and person."""

        assert self.access is not None and self.catalogs is not None
        holder = activation.holder
        team_id = activation.owner.team_id
        if team_id != self.access.team_id or not holder.person_id:
            raise ExecutionAuthorizationDenied("The activation belongs to another team or names no person.")
        try:
            self._catalog()  # decisions use the current catalog
            return self.access.decide(
                agent=AgentPrincipal(
                    holder.agent_id, holder.agent_type, self.access.agent_role_of(holder.agent_type), team_id
                ),
                person=PersonPrincipal(holder.person_id, team_id, holder.person_roles),
                resolution=resolution,
                task_id=task_id,
                consume=False,  # a re-check never uses up a one-time grant
            )
        except ExecutionAuthorizationDenied:
            raise
        except Exception as exc:  # noqa: BLE001 - a check that cannot run refuses
            logger.exception(
                "Execution access re-check failed; refusing. event=execution_access_check_failed "
                "activation_id=%s resource_id=%s",
                activation.activation_id,
                activation.resource_id,
            )
            raise ExecutionAuthorizationDenied("The access check could not run, so the use was refused.") from exc

    def _catalog(self) -> ToolCatalog:
        assert self.access is not None and self.catalogs is not None
        catalog = self.catalogs.current()
        if self.access.catalog is not catalog:
            self.access.catalog = catalog
        return catalog

    @staticmethod
    def _active(
        activation: ActivationRecordV2 | None,
        kind: ExecutionActivationKind,
        label: str,
        *,
        now: datetime | None = None,
    ) -> ActivationRecordV2:
        if activation is None or not activation.is_active(now=now or utc_now()):
            raise ExecutionAuthorizationDenied(f"{label} activation is missing, expired, or revoked.")
        if activation.kind is not kind:
            raise ExecutionAuthorizationDenied(f"{label} activation is of the wrong kind.")
        return activation


__all__ = [
    "ExecutionAccessGuard",
    "ExecutionAuthorizationDenied",
    "SSH_SINGLE_REQUEST_ENDED_MESSAGE",
    "SSHCallerContext",
    "SSHCapability",
]
