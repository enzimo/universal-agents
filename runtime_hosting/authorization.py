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
    ExecutionActivationKind,
    ExecutionRevisionV1,
    InteractiveSessionV1,
    ManagedServiceV1,
    RunAttemptV1,
    utc_now,
)

logger = logging.getLogger(__name__)


class ExecutionAuthorizationDenied(PermissionError):
    pass


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

    def require_ssh_connect(self, session: InteractiveSessionV1) -> None:
        """Opening, reading and resizing: the session's connect authority still holds."""

        activation = self._active(session.connect_activation, ExecutionActivationKind.SSH_CONNECT, "SSH")
        if not activation.binds_session(session):
            raise ExecutionAuthorizationDenied("SSH activation does not bind the exact session target.")
        self.require_authority(activation)

    def require_ssh_interact(
        self, session: InteractiveSessionV1, interaction: ActivationRecordV2 | None
    ) -> None:
        """Typing: connect authority still holds and a person approved typing in this session."""

        self.require_ssh_connect(session)
        if interaction is None:
            raise ExecutionAuthorizationDenied(
                "Typing into this SSH session needs a person's approval. Call "
                "plan_execution_ssh_interaction for this session, then apply_change."
            )
        activation = self._active(interaction, ExecutionActivationKind.SSH_INTERACT, "SSH typing")
        if not activation.binds_session(session):
            raise ExecutionAuthorizationDenied(
                "The typing approval is for another session, owner or target."
            )
        self.require_authority(activation)

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
        activation: ActivationRecordV2 | None, kind: ExecutionActivationKind, label: str
    ) -> ActivationRecordV2:
        if activation is None or not activation.is_active(now=utc_now()):
            raise ExecutionAuthorizationDenied(f"{label} activation is missing, expired, or revoked.")
        if activation.kind is not kind:
            raise ExecutionAuthorizationDenied(f"{label} activation is of the wrong kind.")
        return activation


__all__ = ["ExecutionAccessGuard", "ExecutionAuthorizationDenied"]
