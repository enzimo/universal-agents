"""Versioned contracts for deterministic code execution and managed runtimes.

These models do not expose process, database, NATS, Kubernetes, or Strands
implementation types. Control-plane services persist and exchange them while
execution adapters translate them into workload operations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
import re
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeApprovalRecordV1
from custom_messages.contracts.common_v1 import contract_sha256
from id_generation import new_id


EXECUTION_CONTRACT_SCHEMA_VERSION: Literal[1] = 1
# Version 2 (access model 2, Phase 4c-3c): jobs, managed services and SSH
# sessions carry ``ActivationRecordV2``, which records who held access and the
# approval (``ChangeApprovalRecordV1``) instead of the removed permission
# store's decision references. Records of version 1 are refused when loaded
# (``storage.execution_control_repository``) and retired with
# ``assistant_cli.py execution-retire-old-records``.
EXECUTION_AUTHORITY_SCHEMA_VERSION: Literal[2] = 2
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMANTIC_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OCI_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_KUBERNETES_PVC_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return value.astimezone(timezone.utc)


class OwnerScopeKind(StrEnum):
    USER = "user"
    TEAM = "team"


class ArtifactKind(StrEnum):
    SOURCE = "source"
    BUILD = "build"
    LOG = "log"
    RESULT = "result"


class ArtifactStorageBackend(StrEnum):
    FILESYSTEM = "filesystem"
    NATS_OBJECT_STORE = "nats_object_store"
    OCI_REGISTRY = "oci_registry"


class ExecutionLanguage(StrEnum):
    PYTHON = "python"
    RUST = "rust"


class ExecutionBackendKind(StrEnum):
    LOCAL = "local"
    OPENSHELL = "openshell"
    KUBERNETES = "kubernetes"


class AutomationScheduleKind(StrEnum):
    INTERVAL = "interval"
    CRON = "cron"
    ONE_SHOT = "one_shot"


class MissedRunPolicy(StrEnum):
    COALESCE = "coalesce"
    SKIP = "skip"
    CATCH_UP = "catch_up"


class OverlapPolicy(StrEnum):
    SKIP_IF_RUNNING = "skip_if_running"
    QUEUE = "queue"


class RunAttemptStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SKIPPED_MISSED = "skipped_missed"
    SKIPPED_OVERLAP = "skipped_overlap"
    AUTHORIZATION_EXPIRED = "authorization_expired"


TERMINAL_RUN_ATTEMPT_STATUSES = frozenset(
    {
        RunAttemptStatus.SUCCEEDED,
        RunAttemptStatus.FAILED,
        RunAttemptStatus.TIMED_OUT,
        RunAttemptStatus.CANCELLED,
        RunAttemptStatus.OUTCOME_UNKNOWN,
        RunAttemptStatus.SKIPPED_MISSED,
        RunAttemptStatus.SKIPPED_OVERLAP,
        RunAttemptStatus.AUTHORIZATION_EXPIRED,
    }
)


class ServiceKind(StrEnum):
    PROCESS = "process"
    MCP_SERVER = "mcp_server"


class ServiceDesiredState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    REMOVED = "removed"


class ServiceObservedState(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"
    AUTHORIZATION_EXPIRED = "authorization_expired"


class ServiceRestartPolicy(StrEnum):
    ALWAYS = "always"


class InteractiveSessionStatus(StrEnum):
    PENDING = "pending"
    CONNECTING = "connecting"
    OPEN = "open"
    DISCONNECTED = "disconnected"
    CLOSED = "closed"
    LOST = "lost"
    FAILED = "failed"
    EXPIRED = "expired"


class ExecutionActivationKind(StrEnum):
    """What one activation lets the runtime do."""

    RUN = "execution.run"  # start each occurrence of one exact job generation
    SERVICE = "execution.service"  # reconcile one exact managed-service desired state
    SSH_CONNECT = "execution.ssh.connect"  # open, read and resize one SSH session
    SSH_INTERACT = "execution.ssh.interact"  # type into one SSH session


# Which approvals may back each kind with authorization on. Jobs and SSH
# sessions are ``write`` operations decided by the access decision hook, so the
# agent and its person held the access (and must still hold it at each use).
# Managed services and typing into a remote shell are exact changes a person
# approves (change kinds ``managed_execution_service`` and
# ``execution_ssh_interaction``); the approval record is their authority. With
# authorization off every kind is backed by the person's agreement.
HELD_ACCESS_ACTIVATION_KINDS = frozenset(
    {ExecutionActivationKind.RUN, ExecutionActivationKind.SSH_CONNECT}
)
PERSON_APPROVED_ACTIVATION_KINDS = frozenset(
    {ExecutionActivationKind.SERVICE, ExecutionActivationKind.SSH_INTERACT}
)


class OwnerScopeV1(BaseModel):
    """Repository and policy ownership derived from trusted runtime context."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    scope_kind: OwnerScopeKind
    owner_id: str = Field(min_length=1, max_length=240)
    team_id: str = Field(min_length=1, max_length=240)


class AccessHolderV1(BaseModel):
    """Who acted when an activation was recorded, from the verified task context.

    The agent (instance id and type) and the person its task worked for, with
    that person's team roles at the time. The runtime re-checks held access
    for exactly this agent type and person before each use.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    agent_id: str = Field(min_length=1, max_length=240)
    agent_type: str = Field(min_length=1, max_length=240)
    person_id: str | None = Field(default=None, max_length=240)
    person_roles: tuple[str, ...] = ()
    task_id: str | None = Field(default=None, max_length=240)


class ArtifactReferenceV1(BaseModel):
    """Opaque immutable object reference. ``storage_key`` is never a host path."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal[1] = EXECUTION_CONTRACT_SCHEMA_VERSION
    artifact_id: str = Field(default_factory=lambda: new_id("exec_art"))
    kind: ArtifactKind
    sha256: str
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=240)
    storage_backend: ArtifactStorageBackend
    storage_key: str = Field(min_length=1, max_length=2048)
    created_at_utc: datetime = Field(default_factory=utc_now)

    @field_validator("sha256", mode="before")
    @classmethod
    def _normalize_sha256(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower().removeprefix("sha256:")
        if not _SHA256.fullmatch(normalized):
            raise ValueError("Artifact sha256 must contain 64 lowercase hexadecimal characters.")
        return normalized

    @field_validator("created_at_utc")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="created_at_utc")

    @field_validator("storage_key")
    @classmethod
    def _validate_storage_key(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError("Artifact storage_key must be a relative canonical object key.")
        return value


class CodeArtifactV1(BaseModel):
    """Owner-scoped source accepted into quarantine before a build."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal[1] = EXECUTION_CONTRACT_SCHEMA_VERSION
    code_artifact_id: str = Field(default_factory=lambda: new_id("exec_code"))
    owner: OwnerScopeV1
    language: ExecutionLanguage
    source: ArtifactReferenceV1
    source_name: str = Field(min_length=1, max_length=240)
    created_by_task_id: str | None = Field(default=None, max_length=240)
    created_at_utc: datetime = Field(default_factory=utc_now)

    @field_validator("source_name")
    @classmethod
    def _validate_source_name(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("CodeArtifact source_name must be a single filename.")
        return value

    @model_validator(mode="after")
    def _validate_source_kind(self) -> Self:
        if self.source.kind != ArtifactKind.SOURCE:
            raise ValueError("CodeArtifact source must reference a source artifact.")
        return self


class ResourceLimitsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    wall_time_seconds: int = Field(default=300, ge=1, le=86_400)
    cpu_time_seconds: int = Field(default=300, ge=1, le=86_400)
    memory_bytes: int = Field(default=536_870_912, ge=16_777_216)
    process_count: int = Field(default=64, ge=1, le=4096)
    writable_bytes: int = Field(default=268_435_456, ge=1_048_576)
    stdout_bytes: int = Field(default=65_536, ge=1024)
    stderr_bytes: int = Field(default=65_536, ge=1024)
    artifact_bytes: int = Field(default=268_435_456, ge=1024)


class ExecutionProfileV1(BaseModel):
    """Operator-authored build and runtime policy selected by stable id."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal[1] = EXECUTION_CONTRACT_SCHEMA_VERSION
    profile_id: str
    profile_digest: str = ""
    language: ExecutionLanguage
    backend: ExecutionBackendKind
    runtime_executable: str = Field(min_length=1, max_length=1024)
    builder_executable: str | None = Field(default=None, max_length=1024)
    toolchain_identity: str = Field(min_length=1, max_length=512)
    runtime_image: str | None = Field(default=None, max_length=1024)
    artifact_fetcher_executable: str | None = Field(default=None, max_length=1024)
    kubernetes_artifact_pvc_name: str | None = Field(default=None, max_length=253)
    kubernetes_workload_pvc_name: str | None = Field(default=None, max_length=253)
    kubernetes_namespace: str | None = Field(default=None, max_length=240)
    kubernetes_service_account_name: str | None = Field(default=None, max_length=240)
    openshell_sandbox_name: str | None = Field(default=None, max_length=240)
    openshell_workspace_mount: str = Field(default="/workspace", min_length=1, max_length=1024)
    allow_controller_host_execution: bool = False
    network_enabled: bool = False
    read_only_root: bool = True
    run_as_non_root: bool = True
    limits: ResourceLimitsV1 = Field(default_factory=ResourceLimitsV1)

    @field_validator("profile_id")
    @classmethod
    def _validate_profile_id(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _SEMANTIC_ID.fullmatch(normalized):
            raise ValueError("Execution profile ids must be lowercase semantic identifiers.")
        return normalized

    @model_validator(mode="after")
    def _validate_profile(self) -> Self:
        expected = self.calculate_digest()
        if not self.profile_digest:
            object.__setattr__(self, "profile_digest", expected)
        elif self.profile_digest != expected:
            raise ValueError("Execution profile digest mismatch.")
        if self.backend == ExecutionBackendKind.OPENSHELL and not self.openshell_sandbox_name:
            raise ValueError("OpenShell profiles require openshell_sandbox_name.")
        if self.backend == ExecutionBackendKind.KUBERNETES:
            self._validate_kubernetes_profile()
        return self

    def _validate_kubernetes_profile(self) -> None:
        if not self.runtime_image or not _OCI_DIGEST.fullmatch(self.runtime_image):
            raise ValueError("Kubernetes profiles require an OCI image pinned by sha256 digest.")
        if not self.artifact_fetcher_executable:
            raise ValueError("Kubernetes profiles require artifact_fetcher_executable.")
        if not self.kubernetes_artifact_pvc_name or not _KUBERNETES_PVC_NAME.fullmatch(
            self.kubernetes_artifact_pvc_name
        ):
            raise ValueError("Kubernetes profiles require a valid shared artifact PVC name.")
        if self.kubernetes_workload_pvc_name and not _KUBERNETES_PVC_NAME.fullmatch(
            self.kubernetes_workload_pvc_name
        ):
            raise ValueError("Kubernetes workload PVC names must be valid PVC names.")
        if not self.kubernetes_namespace or not self.kubernetes_service_account_name:
            raise ValueError(
                "Kubernetes profiles require an exact namespace and service account."
            )

    def calculate_digest(self) -> str:
        return contract_sha256(self.model_dump(mode="json", exclude={"profile_digest"}))

    @classmethod
    def build(cls, **values: Any) -> Self:
        seed = cls.model_construct(profile_digest="", **values)
        return cls.model_validate(
            {**seed.model_dump(mode="python"), "profile_digest": seed.calculate_digest()}
        )


class BuildAttestationV1(BaseModel):
    """Trusted build receipt binding source, policy, toolchain, and output."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal[1] = EXECUTION_CONTRACT_SCHEMA_VERSION
    build_id: str = Field(default_factory=lambda: new_id("exec_build"))
    attestation_digest: str = ""
    owner: OwnerScopeV1
    source_artifact_id: str
    source_sha256: str
    profile_id: str
    profile_digest: str
    toolchain_identity: str
    dependency_lock_sha256: str | None = None
    builder_identity: str
    output: ArtifactReferenceV1
    entrypoint: str
    built_at_utc: datetime = Field(default_factory=utc_now)

    @field_validator("source_sha256", "profile_digest", "dependency_lock_sha256", mode="before")
    @classmethod
    def _normalize_digests(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        normalized = str(value).strip().lower().removeprefix("sha256:")
        if not _SHA256.fullmatch(normalized):
            raise ValueError("Build digests must contain 64 lowercase hexadecimal characters.")
        return normalized

    @model_validator(mode="after")
    def _validate_attestation(self) -> Self:
        if self.output.kind != ArtifactKind.BUILD:
            raise ValueError("Build attestation output must be a build artifact.")
        expected = self.calculate_digest()
        if self.attestation_digest and self.attestation_digest != expected:
            raise ValueError("Build attestation digest mismatch.")
        return self

    def calculate_digest(self) -> str:
        return contract_sha256(self.model_dump(mode="json", exclude={"attestation_digest"}))

    @classmethod
    def build(cls, **values: Any) -> Self:
        seed = cls.model_construct(attestation_digest="", **values)
        return cls.model_validate(
            {**seed.model_dump(mode="python"), "attestation_digest": seed.calculate_digest()}
        )


class ActivationRecordV2(BaseModel):
    """Exact durable authority for one job generation, service state or SSH session.

    It binds the resource (id, digest, generation, owner), names the catalog
    operation that was used (``fn:execution_control#create_execution_job``, or
    ``fn:execution_control#change:<kind>`` for a person-approved change), who
    held access (``holder``) and how it was approved (``approval``):

    - ``held_access``: the agent and its person held the operation's level; the
      runtime decides again before each use, so revoking a grant stops later
      runs (``runtime_hosting.authorization``);
    - ``person_approval``: a person approved this exact change in the Inbox
      (``access_request_id``); the record itself is the authority;
    - ``agreement``: authorization was off and the person agreed in the
      conversation; honored only while authorization stays off.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal[2] = EXECUTION_AUTHORITY_SCHEMA_VERSION
    activation_id: str = Field(default_factory=lambda: new_id("exec_activate"))
    activation_digest: str = ""
    kind: ExecutionActivationKind
    resource_id: str = Field(min_length=1, max_length=240)
    resource_digest: str
    generation: int = Field(ge=1)
    owner: OwnerScopeV1
    operation: str = Field(min_length=3, max_length=320, pattern=r"^[a-z]+:[A-Za-z0-9_.-]+#.+$")
    holder: AccessHolderV1
    approval: ChangeApprovalRecordV1
    activated_at_utc: datetime = Field(default_factory=utc_now)
    expires_at_utc: datetime | None = None
    revoked_at_utc: datetime | None = None

    @field_validator("resource_digest", mode="before")
    @classmethod
    def _normalize_resource_digest(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower().removeprefix("sha256:")
        if not _SHA256.fullmatch(normalized):
            raise ValueError("Activation resource_digest must be a sha256 digest.")
        return normalized

    @model_validator(mode="after")
    def _validate_activation(self) -> Self:
        self._validate_authority()
        if self.expires_at_utc is not None:
            _require_aware(self.expires_at_utc, field_name="expires_at_utc")
            if self.expires_at_utc <= self.activated_at_utc:
                raise ValueError("Activation expiry must be after activation time.")
        if self.revoked_at_utc is not None:
            _require_aware(self.revoked_at_utc, field_name="revoked_at_utc")
        expected = self.calculate_digest()
        if self.activation_digest and self.activation_digest != expected:
            raise ValueError("Activation digest mismatch.")
        return self

    def _validate_authority(self) -> None:
        mode = self.approval.mode
        if (
            self.owner.scope_kind == OwnerScopeKind.USER
            and self.holder.person_id != self.owner.owner_id
        ):
            raise ValueError("A person's activation must be held for that same person.")
        if mode is ChangeApprovalMode.HELD_ACCESS:
            if self.kind not in HELD_ACCESS_ACTIVATION_KINDS:
                raise ValueError(f"{self.kind.value} needs a person's approval, not held access.")
            if not self.holder.person_id:
                raise ValueError("Held access is re-checked for a person, so it must name one.")
        elif mode is ChangeApprovalMode.PERSON_APPROVAL:
            if self.kind not in PERSON_APPROVED_ACTIVATION_KINDS:
                raise ValueError(f"{self.kind.value} is decided by held access, not a person's approval.")
            if not self.approval.access_request_id:
                raise ValueError("A person's approval of an execution change names its access request.")

    def calculate_digest(self) -> str:
        return contract_sha256(self.model_dump(mode="json", exclude={"activation_digest"}))

    @classmethod
    def build(cls, **values: Any) -> Self:
        seed = cls.model_construct(activation_digest="", **values)
        return cls.model_validate(
            {**seed.model_dump(mode="python"), "activation_digest": seed.calculate_digest()}
        )

    def is_active(self, *, now: datetime | None = None) -> bool:
        current = now or utc_now()
        return self.revoked_at_utc is None and (
            self.expires_at_utc is None or current < self.expires_at_utc
        )

    def binds_session(self, session: "InteractiveSessionV1") -> bool:
        """Whether this activation is for exactly this SSH session and target."""

        return (
            self.owner == session.owner
            and self.resource_id == session.session_id
            and self.resource_digest == session.target_digest
            and self.generation == 1
        )


class ExecutionRevisionV1(BaseModel):
    """Immutable runnable produced from one promoted build."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal[1] = EXECUTION_CONTRACT_SCHEMA_VERSION
    revision_id: str
    revision_digest: str = ""
    owner: OwnerScopeV1
    language: ExecutionLanguage
    build: BuildAttestationV1
    profile_id: str
    profile_digest: str
    entrypoint: str = Field(min_length=1, max_length=1024)
    args: tuple[str, ...] = ()
    non_secret_env: dict[str, str] = Field(default_factory=dict)
    broker_capability_binding_ids: tuple[str, ...] = ()
    created_by_task_id: str | None = Field(default=None, max_length=240)
    created_at_utc: datetime = Field(default_factory=utc_now)

    @field_validator("entrypoint")
    @classmethod
    def _validate_entrypoint(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("Execution entrypoint must be a single filename.")
        return value

    @field_validator("non_secret_env")
    @classmethod
    def _validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, item in value.items():
            name = str(key or "").strip()
            if not _ENV_NAME.fullmatch(name):
                raise ValueError(f"Invalid non-secret environment name: {name!r}.")
            if name.startswith("UA_"):
                raise ValueError(
                    "Environment names beginning with UA_ are reserved for the runtime."
                )
            normalized[name] = str(item)
        return normalized

    @model_validator(mode="after")
    def _validate_revision(self) -> Self:
        if self.owner != self.build.owner:
            raise ValueError("Execution revision and build owners must match exactly.")
        if self.profile_id != self.build.profile_id or self.profile_digest != self.build.profile_digest:
            raise ValueError("Execution revision profile must match the build attestation.")
        if self.entrypoint != self.build.entrypoint:
            raise ValueError("Execution entrypoint must match the promoted build.")
        expected = self.calculate_digest()
        if self.revision_digest and self.revision_digest != expected:
            raise ValueError("Execution revision digest mismatch.")
        expected_id = f"exec_rev_{expected[:16]}"
        if self.revision_id != expected_id:
            raise ValueError("Execution revision id does not match its digest.")
        return self

    def calculate_digest(self) -> str:
        return contract_sha256(
            self.model_dump(mode="json", exclude={"revision_id", "revision_digest", "created_at_utc"})
        )

    @classmethod
    def build_revision(cls, **values: Any) -> Self:
        seed = cls.model_construct(revision_id="pending", revision_digest="", **values)
        digest = seed.calculate_digest()
        return cls.model_validate(
            {
                **seed.model_dump(mode="python"),
                "revision_id": f"exec_rev_{digest[:16]}",
                "revision_digest": digest,
            }
        )


class AutomationScheduleV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schedule_kind: AutomationScheduleKind
    interval_seconds: int | None = Field(default=None, ge=1)
    cron_expression: str | None = Field(default=None, max_length=240)
    one_shot_at_utc: datetime | None = None
    timezone: str = Field(default="UTC", min_length=1, max_length=128)
    missed_run_policy: MissedRunPolicy = MissedRunPolicy.COALESCE
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP_IF_RUNNING
    maximum_backlog: int = Field(default=1, ge=1, le=1000)
    start_deadline_seconds: int | None = Field(default=None, ge=1, le=604_800)

    @model_validator(mode="after")
    def _validate_schedule(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {self.timezone!r}.") from exc
        if self.schedule_kind == AutomationScheduleKind.INTERVAL:
            self._validate_interval_schedule()
        elif self.schedule_kind == AutomationScheduleKind.CRON:
            self._validate_cron_schedule()
        else:
            self._validate_one_shot_schedule()
        return self

    def _validate_interval_schedule(self) -> None:
        if self.interval_seconds is None or self.cron_expression or self.one_shot_at_utc:
            raise ValueError("Interval schedules require only interval_seconds.")

    def _validate_cron_schedule(self) -> None:
        if not self.cron_expression or self.interval_seconds or self.one_shot_at_utc:
            raise ValueError("Cron schedules require only cron_expression.")
        if not croniter.is_valid(self.cron_expression):
            raise ValueError("Cron expression is invalid.")

    def _validate_one_shot_schedule(self) -> None:
        if self.interval_seconds or self.cron_expression:
            raise ValueError("One-shot schedules cannot include interval or cron fields.")
        if self.one_shot_at_utc is None:
            raise ValueError("One-shot schedules require one_shot_at_utc.")
        _require_aware(self.one_shot_at_utc, field_name="one_shot_at_utc")


class AutomationJobV2(BaseModel):
    """Finite desired work. Every occurrence becomes one RunAttempt."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[2] = EXECUTION_AUTHORITY_SCHEMA_VERSION
    job_id: str = Field(default_factory=lambda: new_id("exec_job"))
    name: str = Field(min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=4000)
    owner: OwnerScopeV1
    revision_id: str
    revision_digest: str
    activation: ActivationRecordV2 | None = None
    generation: int = Field(default=1, ge=1)
    schedule: AutomationScheduleV1
    enabled: bool = False
    paused_until_utc: datetime | None = None
    expires_at_utc: datetime | None = None
    next_run_at_utc: datetime | None = None
    created_by_task_id: str | None = Field(default=None, max_length=240)
    objective_id: str | None = Field(default=None, max_length=240)
    created_at_utc: datetime = Field(default_factory=utc_now)
    updated_at_utc: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_job(self) -> Self:
        for field_name in ("paused_until_utc", "expires_at_utc", "next_run_at_utc"):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware(value, field_name=field_name)
        if self.enabled:
            if self.activation is None:
                raise ValueError("Enabled automation jobs require an exact activation.")
        if self.activation is not None:
            if self.activation.kind != ExecutionActivationKind.RUN:
                raise ValueError("Automation activation must be a run activation.")
            if self.activation.owner != self.owner:
                raise ValueError("Automation activation owner scope does not match the job.")
            if self.activation.resource_id != self.job_id:
                raise ValueError("Automation activation targets another job.")
            if self.activation.generation != self.generation:
                raise ValueError("Automation activation generation is stale.")
            if self.activation.resource_digest != self.activation_resource_digest():
                raise ValueError("Automation activation does not bind this exact job revision.")
        return self

    def activation_resource_digest(self) -> str:
        return contract_sha256(
            {
                "job_id": self.job_id,
                "owner": self.owner.model_dump(mode="json"),
                "revision_id": self.revision_id,
                "revision_digest": self.revision_digest,
                "generation": self.generation,
                "schedule": self.schedule.model_dump(mode="json"),
                "expires_at_utc": self.expires_at_utc,
            }
        )


class RunAttemptV1(BaseModel):
    """One fenced batch attempt with durable result and artifact references."""

    # Output previews are evidence and must preserve exact leading/trailing
    # whitespace. Identifiers are created by trusted runtime code.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    schema_version: Literal[1] = EXECUTION_CONTRACT_SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: new_id("exec_run"))
    occurrence_id: str = Field(min_length=1, max_length=300)
    job_id: str
    owner: OwnerScopeV1
    revision_id: str
    revision_digest: str
    job_generation: int = Field(ge=1)
    scheduled_for_utc: datetime
    status: RunAttemptStatus = RunAttemptStatus.PENDING
    fence: int = Field(default=0, ge=0)
    lease_owner: str | None = Field(default=None, max_length=240)
    lease_token: str | None = Field(default=None, max_length=240)
    lease_expires_at_utc: datetime | None = None
    heartbeat_at_utc: datetime | None = None
    backend_kind: ExecutionBackendKind | None = None
    backend_workload_id: str | None = Field(default=None, max_length=512)
    backend_workload_uid: str | None = Field(default=None, max_length=512)
    backend_spec_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    started_at_utc: datetime | None = None
    finished_at_utc: datetime | None = None
    exit_code: int | None = None
    stdout_preview: str = ""
    stderr_preview: str = ""
    log_artifact: ArtifactReferenceV1 | None = None
    result_artifacts: tuple[ArtifactReferenceV1, ...] = ()
    coalesced_occurrences: int = Field(default=1, ge=1)
    error_code: str | None = Field(default=None, max_length=160)
    error_message: str | None = Field(default=None, max_length=4000)
    idempotency_key: str | None = Field(default=None, max_length=512)
    created_at_utc: datetime = Field(default_factory=utc_now)
    updated_at_utc: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_attempt(self) -> Self:
        _require_aware(self.scheduled_for_utc, field_name="scheduled_for_utc")
        lease_values = (self.lease_owner, self.lease_token, self.lease_expires_at_utc)
        if any(value is not None for value in lease_values) and not all(
            value is not None for value in lease_values
        ):
            raise ValueError("Run attempt lease fields must be set together.")
        if self.status in TERMINAL_RUN_ATTEMPT_STATUSES and self.finished_at_utc is None:
            raise ValueError("Terminal run attempts require finished_at_utc.")
        return self


class ManagedServiceV1(BaseModel):
    """Long-lived desired state reconciled independently from batch attempts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[2] = EXECUTION_AUTHORITY_SCHEMA_VERSION
    service_id: str = Field(default_factory=lambda: new_id("exec_svc"))
    name: str = Field(min_length=1, max_length=240)
    service_kind: ServiceKind = ServiceKind.PROCESS
    owner: OwnerScopeV1
    revision_id: str
    revision_digest: str
    activation: ActivationRecordV2 | None = None
    desired_state: ServiceDesiredState = ServiceDesiredState.STOPPED
    observed_state: ServiceObservedState = ServiceObservedState.PENDING
    generation: int = Field(default=1, ge=1)
    observed_generation: int = Field(default=0, ge=0)
    restart_policy: ServiceRestartPolicy = ServiceRestartPolicy.ALWAYS
    health_check_interval_seconds: int = Field(default=30, ge=1, le=3600)
    startup_timeout_seconds: int = Field(default=30, ge=1, le=3600)
    bind_host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    mcp_path: str = Field(default="/mcp", pattern=r"^/[A-Za-z0-9/_-]*$")
    mcp_tool_names: tuple[str, ...] = ()
    mcp_target_agent_types: tuple[str, ...] = ()
    active_fence: int = Field(default=0, ge=0)
    lease_owner: str | None = Field(default=None, max_length=240)
    lease_token: str | None = Field(default=None, max_length=240)
    lease_expires_at_utc: datetime | None = None
    backend_workload_id: str | None = Field(default=None, max_length=512)
    backend_workload_uid: str | None = Field(default=None, max_length=512)
    last_health_at_utc: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=160)
    last_error_message: str | None = Field(default=None, max_length=4000)
    created_by_task_id: str | None = Field(default=None, max_length=240)
    created_at_utc: datetime = Field(default_factory=utc_now)
    updated_at_utc: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_service(self) -> Self:
        lease_values = (self.lease_owner, self.lease_token, self.lease_expires_at_utc)
        if any(value is not None for value in lease_values) and not all(
            value is not None for value in lease_values
        ):
            raise ValueError("Managed-service lease fields must be set together.")
        if self.desired_state == ServiceDesiredState.RUNNING and self.activation is None:
            raise ValueError("Running managed services require an exact activation.")
        if self.activation is not None:
            if self.activation.kind != ExecutionActivationKind.SERVICE:
                raise ValueError("Managed-service activation must be a service activation.")
            if self.activation.owner != self.owner:
                raise ValueError("Managed-service activation owner scope does not match.")
            if self.activation.resource_id != self.service_id:
                raise ValueError("Managed-service activation targets another service.")
            if self.activation.generation != self.generation:
                raise ValueError("Managed-service activation generation is stale.")
            if self.activation.resource_digest != self.activation_resource_digest():
                raise ValueError("Managed-service activation does not bind this exact revision.")
        if self.service_kind == ServiceKind.MCP_SERVER:
            if self.port is None or not self.mcp_tool_names:
                raise ValueError("Managed MCP services require a port and explicit tool names.")
        return self

    def activation_resource_digest(self) -> str:
        return contract_sha256(
            {
                "service_id": self.service_id,
                "service_kind": self.service_kind.value,
                "owner": self.owner.model_dump(mode="json"),
                "revision_id": self.revision_id,
                "revision_digest": self.revision_digest,
                "generation": self.generation,
                "desired_state": self.desired_state.value,
                "restart_policy": self.restart_policy.value,
                "health_check_interval_seconds": self.health_check_interval_seconds,
                "startup_timeout_seconds": self.startup_timeout_seconds,
                "bind_host": self.bind_host,
                "port": self.port,
                "mcp_path": self.mcp_path,
                "mcp_tool_names": self.mcp_tool_names,
                "mcp_target_agent_types": self.mcp_target_agent_types,
            }
        )


class SSHConnectionTargetV1(BaseModel):
    """Exact SSH authority target. Keys remain broker-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    host_key_algorithm: str = Field(min_length=1, max_length=64)
    host_key_fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{16,}={0,2}$")
    credential_binding_id: str = Field(min_length=1, max_length=240)
    remote_persistent_session_name: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    allow_port_forwarding: bool = False
    allow_agent_forwarding: bool = False
    allow_x11_forwarding: bool = False


class InteractiveSessionV1(BaseModel):
    """Persisted metadata for one non-replayable live PTY lease.

    ``connect_activation`` lets the session be opened, read and resized. Typing
    needs a separate, person-approved ``SSH_INTERACT`` activation, stored in
    its own insert-once table (``execution_ssh_interactions``) because the
    broker rewrites this record on every lease renewal.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[2] = EXECUTION_AUTHORITY_SCHEMA_VERSION
    session_id: str = Field(default_factory=lambda: new_id("ssh_sess"))
    owner: OwnerScopeV1
    target: SSHConnectionTargetV1
    target_digest: str
    connect_activation: ActivationRecordV2 | None = None
    status: InteractiveSessionStatus = InteractiveSessionStatus.PENDING
    fence: int = Field(default=0, ge=0)
    lease_owner: str | None = Field(default=None, max_length=240)
    lease_token: str | None = Field(default=None, max_length=240)
    lease_expires_at_utc: datetime | None = None
    terminal_rows: int = Field(default=24, ge=1, le=1000)
    terminal_columns: int = Field(default=80, ge=1, le=1000)
    transcript_bytes: int = Field(default=0, ge=0)
    transcript_artifact: ArtifactReferenceV1 | None = None
    created_by_task_id: str | None = Field(default=None, max_length=240)
    created_at_utc: datetime = Field(default_factory=utc_now)
    updated_at_utc: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_session(self) -> Self:
        expected = contract_sha256(self.target.model_dump(mode="json"))
        if self.target_digest != expected:
            raise ValueError("SSH target digest mismatch.")
        if (
            self.status in {InteractiveSessionStatus.CONNECTING, InteractiveSessionStatus.OPEN}
            and self.connect_activation is None
        ):
            raise ValueError("Active SSH sessions require an exact activation.")
        activation = self.connect_activation
        if activation is not None:
            if activation.kind != ExecutionActivationKind.SSH_CONNECT:
                raise ValueError("SSH session activation must be a connect activation.")
            if not activation.binds_session(self):
                raise ValueError(
                    "SSH activation does not bind this session: its owner scope, id, "
                    "exact target or generation differs."
                )
        return self


__all__ = [
    "EXECUTION_AUTHORITY_SCHEMA_VERSION",
    "EXECUTION_CONTRACT_SCHEMA_VERSION",
    "HELD_ACCESS_ACTIVATION_KINDS",
    "PERSON_APPROVED_ACTIVATION_KINDS",
    "AccessHolderV1",
    "ActivationRecordV2",
    "ArtifactKind",
    "ArtifactReferenceV1",
    "ArtifactStorageBackend",
    "AutomationJobV2",
    "AutomationScheduleKind",
    "AutomationScheduleV1",
    "BuildAttestationV1",
    "CodeArtifactV1",
    "ExecutionActivationKind",
    "ExecutionBackendKind",
    "ExecutionLanguage",
    "ExecutionProfileV1",
    "ExecutionRevisionV1",
    "InteractiveSessionStatus",
    "InteractiveSessionV1",
    "ManagedServiceV1",
    "MissedRunPolicy",
    "OverlapPolicy",
    "OwnerScopeKind",
    "OwnerScopeV1",
    "ResourceLimitsV1",
    "RunAttemptStatus",
    "RunAttemptV1",
    "SSHConnectionTargetV1",
    "ServiceDesiredState",
    "ServiceKind",
    "ServiceObservedState",
    "ServiceRestartPolicy",
    "TERMINAL_RUN_ATTEMPT_STATUSES",
    "utc_now",
]
