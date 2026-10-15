"""Agent-facing application service for immutable deterministic workloads.

Access (access plan Phase 4c-3c):

- Promoting a revision, creating and enabling jobs, and creating SSH sessions
  are ``write`` operations of ``fn:execution_control``, decided by the access
  decision hook (``custom_agents.access_runtime``) before this service runs;
  it makes no access decision of its own. When a job or SSH session gets an
  activation, the activation records who held access: the agent, the person
  its verified task works for (id and roles), the catalog operation and the
  time (``held_access``). The runtime decides again before each run and each
  SSH call (``runtime_hosting.authorization``), so revoking a grant stops later
  runs. Because a job runs later and outside this task, activating one first
  checks that the access lasts beyond the task (the same rule the runner uses).
- Managed services and typing into an SSH session need a person's approval of
  the exact change (``custom_agents.execution_changes``, applied with
  ``apply_change``).
- With authorization off, the person's agreement in the conversation
  (``confirmed=true``) is the authority and is recorded as ``agreement``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Mapping

from access_control.entities import operation_id
from custom_agents.access_runtime import ActingAgent, acting_agent, runtime_access
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeApprovalRecordV1
from custom_messages.contracts.common_v1 import contract_sha256
from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.authorization import ExecutionAccessGuard, ExecutionAuthorizationDenied
from runtime_hosting.builds import DeterministicBuilder, OpenShellRustBuilder
from runtime_hosting.execution_contracts import (
    AccessHolderV1,
    ActivationRecordV2,
    ArtifactKind,
    AutomationJobV2,
    AutomationScheduleV1,
    CodeArtifactV1,
    ExecutionActivationKind,
    ExecutionBackendKind,
    ExecutionLanguage,
    ExecutionRevisionV1,
    InteractiveSessionV1,
    ManagedServiceV1,
    OwnerScopeKind,
    OwnerScopeV1,
    SSHConnectionTargetV1,
    utc_now,
)
from runtime_hosting.profiles import ExecutionProfileRegistry
from storage.execution_control_repository import (
    ExecutionControlRepository,
    create_execution_control_repository,
)


# The catalog tool whose operations this service performs; activations name
# the operation used (``<tool>#<function>``) so the runtime can decide it again.
EXECUTION_TOOL_ID = "fn:execution_control"
CREATE_JOB_FUNCTION = "create_execution_job"
SET_JOB_ENABLED_FUNCTION = "set_execution_job_enabled"
CREATE_SSH_SESSION_FUNCTION = "create_execution_ssh_session"
_AGREEMENT_NEEDED = (
    "Authorization is off, so this needs the person's agreement: explain the exact "
    "change, and call again with confirmed=true once they agree in the conversation."
)


def execution_operation(function: str) -> str:
    """The catalog operation id of one ``fn:execution_control`` function."""

    return operation_id(EXECUTION_TOOL_ID, function)


_SECRETISH_ENV_KEY = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASS|PRIVATE_KEY|API_KEY|ACCESS_KEY|CREDENTIAL)",
    re.IGNORECASE,
)
_SECRETISH_ENV_VALUE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"
)


class ExecutionControlService:
    """Stage, activate, and inspect owner-scoped execution records."""

    def __init__(self, agent: Any):
        from assistant_gateway.config import GatewayInstanceConfig

        configured_path = str(getattr(agent, "instance_config_path", "") or "").strip()
        if not configured_path:
            raise ValueError("Execution control requires an instance config path.")
        self.agent = agent
        self.config = GatewayInstanceConfig.load_for_process(configured_path)
        if not self.config.execution_control.enabled:
            raise RuntimeError("execution_control.enabled is false.")
        # Who acts, only from the task's signed context (both authorization modes).
        self.actor: ActingAgent = acting_agent(agent)
        self.owner = self._owner_scope(self.actor)
        self.access = runtime_access(agent)
        self.repository: ExecutionControlRepository = create_execution_control_repository(
            self.config.resolve_manager_storage_settings()
        )
        workspace = self.config.workspace_path
        layout = self.config.layout
        settings = self.config.execution_control
        self.artifact_store = FilesystemArtifactStore(
            layout.execution_artifacts_dir,
            maximum_artifact_bytes=settings.artifact_store.maximum_artifact_bytes,
        )
        self.profiles = ExecutionProfileRegistry(settings.profiles)
        self.builder = DeterministicBuilder(
            artifact_store=self.artifact_store,
            builder_identity="universal-agents:python-compile-v1",
            isolated_rust_builder=OpenShellRustBuilder(
                host_workspace_root=workspace,
                build_root=layout.execution_runs_dir / "rust-builds",
            ),
        )

    @property
    def authorization_enabled(self) -> bool:
        return self.access.enabled

    def access_guard(self) -> ExecutionAccessGuard:
        """The runtime's checks, in this process (the SSH broker and early checks use them)."""

        return ExecutionAccessGuard(
            authorization_enabled=self.access.enabled,
            access=self.access.service,
            catalogs=self.access.catalogs if self.access.enabled else None,
        )

    def holder(self) -> AccessHolderV1:
        """The acting agent and its verified person, as activations record them."""

        person = self.actor.person
        return AccessHolderV1(
            agent_id=self.actor.agent.agent_id,
            agent_type=self.actor.agent.agent_type,
            person_id=person.person_id if person else None,
            person_roles=tuple(person.roles) if person else (),
            task_id=self.actor.task_id,
        )

    def list_profiles(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "profile_id": profile.profile_id,
                "profile_digest": profile.profile_digest,
                "language": profile.language.value,
                "backend": profile.backend.value,
                "toolchain_identity": profile.toolchain_identity,
                "network_enabled": profile.network_enabled,
                "controller_host_execution": profile.allow_controller_host_execution,
            }
            for profile in self.profiles.list_profiles()
        )

    def list_ssh_targets(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "target_id": target.target_id,
                "host": target.host,
                "port": target.port,
                "username": target.username,
                "host_key_algorithm": target.host_key_algorithm,
                "host_key_fingerprint": target.host_key_fingerprint,
                "credential_binding_id": target.credential_binding_id,
            }
            for target in self.config.execution_control.ssh.targets
        )

    def promote_revision(
        self,
        *,
        source: str,
        source_name: str,
        language: str,
        profile_id: str,
        args: tuple[str, ...] = (),
        non_secret_env: Mapping[str, str] | None = None,
        broker_capability_binding_ids: tuple[str, ...] = (),
    ) -> ExecutionRevisionV1:
        if broker_capability_binding_ids:
            raise ValueError(
                "Execution credential bindings are not installed for this runtime profile; "
                "raw credentials and ambient environment inheritance remain prohibited."
            )
        selected_language = ExecutionLanguage(language)
        profile = next(
            (item for item in self.profiles.list_profiles() if item.profile_id == profile_id),
            None,
        )
        if profile is None:
            raise ValueError(f"Execution profile {profile_id!r} is not installed.")
        environment = self._non_secret_environment(non_secret_env or {})
        source_reference = self.artifact_store.put_bytes(
            kind=ArtifactKind.SOURCE,
            payload=source.encode("utf-8"),
            media_type=(
                "text/x-python"
                if selected_language == ExecutionLanguage.PYTHON
                else "text/x-rust"
            ),
        )
        code = CodeArtifactV1(
            owner=self.owner,
            language=selected_language,
            source=source_reference,
            source_name=source_name,
            created_by_task_id=self.actor.task_id,
        )
        self.repository.put_code_artifact(code)
        build = self.builder.build(code=code, profile=profile)
        self.repository.put_build_attestation(build)
        revision = ExecutionRevisionV1.build_revision(
            owner=self.owner,
            language=selected_language,
            build=build,
            profile_id=profile.profile_id,
            profile_digest=profile.profile_digest,
            entrypoint=build.entrypoint,
            args=args,
            non_secret_env=environment,
            broker_capability_binding_ids=broker_capability_binding_ids,
            created_by_task_id=self.actor.task_id,
        )
        return self.repository.put_revision(revision)

    def create_job(
        self,
        *,
        name: str,
        revision_id: str,
        schedule: AutomationScheduleV1,
        description: str | None = None,
        expires_at_utc: datetime | None = None,
        activate: bool = False,
        agreement_confirmed: bool = False,
    ) -> AutomationJobV2:
        revision = self.owned_revision(revision_id)
        first_run = self._first_run(schedule)
        draft = AutomationJobV2(
            name=name,
            description=description,
            owner=self.owner,
            revision_id=revision.revision_id,
            revision_digest=revision.revision_digest,
            schedule=schedule,
            enabled=False,
            expires_at_utc=expires_at_utc,
            next_run_at_utc=first_run,
            created_by_task_id=self.actor.task_id,
        )
        if activate:
            activation = self._activation(
                kind=ExecutionActivationKind.RUN,
                function=CREATE_JOB_FUNCTION,
                resource_id=draft.job_id,
                resource_digest=draft.activation_resource_digest(),
                generation=draft.generation,
                expires_at_utc=expires_at_utc,
                agreement_confirmed=agreement_confirmed,
            )
            self._require_host_execution_access(activation, revision)
            draft = draft.model_copy(update={"activation": activation, "enabled": True})
        return self.repository.create_job(draft)

    def set_job_enabled(
        self,
        *,
        job_id: str,
        enabled: bool,
        agreement_confirmed: bool = False,
    ) -> AutomationJobV2:
        """Enable (a new activation for the next generation) or disable a job.

        With authorization off, both need the person's agreement, as before.
        """

        current = self.owned_job(job_id)
        if not self.authorization_enabled and agreement_confirmed is not True:
            raise PermissionError(_AGREEMENT_NEEDED)
        generation = current.generation + 1
        candidate = current.model_copy(
            update={
                "generation": generation,
                "activation": None,
                "enabled": enabled,
                "updated_at_utc": utc_now(),
            }
        )
        activation = None
        if enabled:
            activation = self._activation(
                kind=ExecutionActivationKind.RUN,
                function=SET_JOB_ENABLED_FUNCTION,
                resource_id=current.job_id,
                resource_digest=candidate.activation_resource_digest(),
                generation=generation,
                expires_at_utc=current.expires_at_utc,
                agreement_confirmed=agreement_confirmed,
            )
            self._require_host_execution_access(activation, self.owned_revision(current.revision_id))
        updated = candidate.model_copy(update={"activation": activation})
        return self.repository.update_job(updated, expected_generation=current.generation)

    def list_jobs(self, *, limit: int = 100) -> tuple[AutomationJobV2, ...]:
        return tuple(
            job
            for job in self.repository.list_jobs(team_id=self.owner.team_id, limit=limit)
            if job.owner == self.owner
        )

    def list_runs(self, *, job_id: str | None = None, limit: int = 100) -> tuple[Any, ...]:
        if job_id:
            self.owned_job(job_id)
        return tuple(
            run
            for run in self.repository.list_run_attempts(
                team_id=self.owner.team_id,
                job_id=job_id,
                limit=limit,
            )
            if run.owner == self.owner
        )

    def list_services(self) -> tuple[ManagedServiceV1, ...]:
        return tuple(
            service
            for service in self.repository.list_managed_services(team_id=self.owner.team_id)
            if service.owner == self.owner
        )

    def create_ssh_session(
        self,
        *,
        target_id: str,
        terminal_rows: int = 24,
        terminal_columns: int = 80,
        agreement_confirmed: bool = False,
    ) -> InteractiveSessionV1:
        """A session record for one installed target, with its connect activation.

        The session can be opened, read and resized; typing needs a person's
        approval for this session (``plan_execution_ssh_interaction``).
        """

        ssh_settings = self.config.execution_control.ssh
        if not ssh_settings.enabled:
            raise RuntimeError("execution_control.ssh.enabled is false.")
        configured = next(
            (item for item in ssh_settings.targets if item.target_id == target_id),
            None,
        )
        if configured is None:
            raise LookupError("SSH target is not installed.")
        target = SSHConnectionTargetV1(
            host=configured.host,
            port=configured.port,
            username=configured.username,
            host_key_algorithm=configured.host_key_algorithm,
            host_key_fingerprint=configured.host_key_fingerprint,
            credential_binding_id=configured.credential_binding_id,
            remote_persistent_session_name=configured.remote_persistent_session_name,
        )
        target_digest = contract_sha256(target.model_dump(mode="json"))
        draft = InteractiveSessionV1(
            owner=self.owner,
            target=target,
            target_digest=target_digest,
            terminal_rows=terminal_rows,
            terminal_columns=terminal_columns,
            created_by_task_id=self.actor.task_id,
        )
        connect = self._activation(
            kind=ExecutionActivationKind.SSH_CONNECT,
            function=CREATE_SSH_SESSION_FUNCTION,
            resource_id=draft.session_id,
            resource_digest=target_digest,
            generation=1,
            agreement_confirmed=agreement_confirmed,
        )
        return self.repository.create_interactive_session(
            draft.model_copy(update={"connect_activation": connect})
        )

    def list_ssh_sessions(self) -> tuple[InteractiveSessionV1, ...]:
        return tuple(
            session
            for session in self.repository.list_interactive_sessions(
                team_id=self.owner.team_id
            )
            if session.owner == self.owner
        )

    def ssh_interaction(self, session_id: str) -> ActivationRecordV2 | None:
        """The person's approval to type into one of this owner's sessions, if any."""

        return self.repository.get_ssh_interaction(team_id=self.owner.team_id, session_id=session_id)

    def _activation(
        self,
        *,
        kind: ExecutionActivationKind,
        function: str,
        resource_id: str,
        resource_digest: str,
        generation: int,
        agreement_confirmed: bool,
        expires_at_utc: datetime | None = None,
    ) -> ActivationRecordV2:
        """Record who held access for an operation the hook already allowed."""

        now = utc_now()
        if self.authorization_enabled:
            approval = ChangeApprovalRecordV1(
                mode=ChangeApprovalMode.HELD_ACCESS,
                approved_by=self.actor.agent.agent_type,
                approved_at_utc=now,
            )
        elif agreement_confirmed is True:
            approval = ChangeApprovalRecordV1(
                mode=ChangeApprovalMode.AGREEMENT,
                approved_by=ChangeApprovalMode.AGREEMENT.value,
                approved_at_utc=now,
            )
        else:
            raise PermissionError(_AGREEMENT_NEEDED)
        activation = ActivationRecordV2.build(
            kind=kind,
            resource_id=resource_id,
            resource_digest=resource_digest,
            generation=generation,
            owner=self.owner,
            operation=execution_operation(function),
            holder=self.holder(),
            approval=approval,
            activated_at_utc=now,
            expires_at_utc=expires_at_utc,
        )
        if self.authorization_enabled and kind is ExecutionActivationKind.RUN:
            self._require_lasting_access(activation)
        return activation

    def _require_lasting_access(self, activation: ActivationRecordV2) -> None:
        """A job runs later, outside this task: its access must outlive the task.

        This is the runner's own check, run now so a job that could never run
        is refused while the person is still there to fix it.
        """

        try:
            self.access_guard().require_authority(activation)
        except ExecutionAuthorizationDenied as exc:
            raise PermissionError(
                "A job runs later, outside this task, so it needs write access to "
                f"{EXECUTION_TOOL_ID} that lasts beyond this task (not access for this task only). "
                f"{exc} Ask for it with request_access (lifetime until_revoked or until a time), "
                "then try again."
            ) from exc

    def _require_host_execution_access(self, activation: ActivationRecordV2, revision: ExecutionRevisionV1) -> None:
        """A job on a profile that runs code on the controller host needs code:unconfined.

        The runner checks this before every run (runtime_hosting.authorization);
        checking now refuses a job that could never run while the person is
        still there to decide.
        """

        profile = self.profiles.require(profile_id=revision.profile_id, profile_digest=revision.profile_digest)
        if profile.backend is not ExecutionBackendKind.LOCAL:
            return
        try:
            self.access_guard().require_host_execution(activation)
        except ExecutionAuthorizationDenied as exc:
            raise PermissionError(
                f"{exc} Ask for it with request_access(tool_id='code:unconfined', level='full', "
                "lifetime='until_revoked', reason=...), or promote the code under a sandboxed profile."
            ) from exc

    @staticmethod
    def _owner_scope(actor: ActingAgent) -> OwnerScopeV1:
        team_id = str(actor.agent.team_id or "").strip()
        if not team_id:
            raise PermissionError("Execution control requires a trusted team identity.")
        person_id = actor.person_id
        return OwnerScopeV1(
            scope_kind=OwnerScopeKind.USER if person_id else OwnerScopeKind.TEAM,
            owner_id=person_id or team_id,
            team_id=team_id,
        )

    def owned_revision(self, revision_id: str) -> ExecutionRevisionV1:
        revision = self.repository.get_revision(
            team_id=self.owner.team_id,
            revision_id=revision_id,
        )
        if revision is None or revision.owner != self.owner:
            raise LookupError("Execution revision not found.")
        return revision

    def owned_job(self, job_id: str) -> AutomationJobV2:
        job = self.repository.get_job(team_id=self.owner.team_id, job_id=job_id)
        if job is None or job.owner != self.owner:
            raise LookupError("Execution job not found.")
        return job

    def owned_service(self, service_id: str) -> ManagedServiceV1:
        service = self.repository.get_managed_service(team_id=self.owner.team_id, service_id=service_id)
        if service is None or service.owner != self.owner:
            raise LookupError("Managed execution service not found.")
        return service

    def owned_session(self, session_id: str) -> InteractiveSessionV1:
        session = self.repository.get_interactive_session(team_id=self.owner.team_id, session_id=session_id)
        if session is None or session.owner != self.owner:
            raise LookupError("SSH session not found.")
        return session

    @staticmethod
    def _first_run(schedule: AutomationScheduleV1) -> datetime:
        if schedule.one_shot_at_utc is not None:
            return schedule.one_shot_at_utc.astimezone(timezone.utc)
        return utc_now()

    @staticmethod
    def _non_secret_environment(value: Mapping[str, str]) -> dict[str, str]:
        environment = {str(key): str(item) for key, item in value.items()}
        for key, item in environment.items():
            if _SECRETISH_ENV_KEY.search(key) or _SECRETISH_ENV_VALUE.search(item):
                raise ValueError(
                    f"Environment entry {key!r} resembles credential material; use a broker binding."
                )
        return environment


__all__ = [
    "CREATE_JOB_FUNCTION",
    "CREATE_SSH_SESSION_FUNCTION",
    "EXECUTION_TOOL_ID",
    "SET_JOB_ENABLED_FUNCTION",
    "ExecutionControlService",
    "execution_operation",
]
