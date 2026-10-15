"""Execution configuration and contracts."""

from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field, field_validator, model_validator
from default_values import EXECUTION_CONTROL_DEFAULTS
from runtime_hosting.execution_contracts import ExecutionProfileV1


class ExecutionControlRunnerSettings(BaseModel):
    enabled: bool = EXECUTION_CONTROL_DEFAULTS.runner.value["enabled"]
    tick_interval_seconds: float = EXECUTION_CONTROL_DEFAULTS.runner.value[
        "tick_interval_seconds"
    ]
    max_concurrent_jobs: int = EXECUTION_CONTROL_DEFAULTS.runner.value[
        "max_concurrent_jobs"
    ]
    lease_ttl_seconds: int = EXECUTION_CONTROL_DEFAULTS.runner.value[
        "lease_ttl_seconds"
    ]
    service_reconcile_interval_seconds: float = EXECUTION_CONTROL_DEFAULTS.runner.value[
        "service_reconcile_interval_seconds"
    ]

    @field_validator(
        "tick_interval_seconds", "service_reconcile_interval_seconds", mode="before"
    )
    @classmethod
    def _normalize_positive_float(cls, value: Any) -> float:
        parsed = float(value)
        if parsed <= 0:
            raise ValueError("execution_control.runner interval settings must be > 0.")
        return parsed

    @field_validator("max_concurrent_jobs", "lease_ttl_seconds", mode="before")
    @classmethod
    def _normalize_positive_int(cls, value: Any, info: Any) -> int:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError(f"execution_control.runner.{info.field_name} must be > 0.")
        return parsed


class ExecutionArtifactStoreSettings(BaseModel):
    maximum_artifact_bytes: int = EXECUTION_CONTROL_DEFAULTS.artifact_store.value[
        "maximum_artifact_bytes"
    ]

    @field_validator("maximum_artifact_bytes", mode="before")
    @classmethod
    def _normalize_positive_int(cls, value: Any) -> int:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError(
                "execution_control.artifact_store.maximum_artifact_bytes must be > 0."
            )
        return parsed


class ExecutionKubernetesSettings(BaseModel):
    kubectl_executable: str = EXECUTION_CONTROL_DEFAULTS.kubernetes.value[
        "kubectl_executable"
    ]
    network_policy_enforced: bool = EXECUTION_CONTROL_DEFAULTS.kubernetes.value[
        "network_policy_enforced"
    ]


class ExecutionSSHSettings(BaseModel):
    enabled: bool = EXECUTION_CONTROL_DEFAULTS.ssh.value["enabled"]
    ssh_executable: str = EXECUTION_CONTROL_DEFAULTS.ssh.value["ssh_executable"]
    lease_ttl_seconds: int = EXECUTION_CONTROL_DEFAULTS.ssh.value["lease_ttl_seconds"]
    transcript_limit_bytes: int = EXECUTION_CONTROL_DEFAULTS.ssh.value[
        "transcript_limit_bytes"
    ]
    targets: tuple["ExecutionSSHTargetSettings", ...] = ()

    @field_validator("lease_ttl_seconds", "transcript_limit_bytes", mode="before")
    @classmethod
    def _normalize_positive_int(cls, value: Any, info: Any) -> int:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError(f"execution_control.ssh.{info.field_name} must be > 0.")
        return parsed

    @model_validator(mode="after")
    def _validate_unique_targets(self) -> "ExecutionSSHSettings":
        identifiers = [target.target_id for target in self.targets]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(
                "execution_control.ssh.targets contains duplicate target ids."
            )
        return self


class ExecutionSSHTargetSettings(BaseModel):
    target_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
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
    identity_file: str = Field(min_length=1, max_length=2048)
    known_hosts_file: str = Field(min_length=1, max_length=2048)


class ExecutionControlSettings(BaseModel):
    enabled: bool = EXECUTION_CONTROL_DEFAULTS.enabled.value
    runner: ExecutionControlRunnerSettings = Field(
        default_factory=ExecutionControlRunnerSettings
    )
    artifact_store: ExecutionArtifactStoreSettings = Field(
        default_factory=ExecutionArtifactStoreSettings
    )
    profiles: tuple[ExecutionProfileV1, ...] = ()
    kubernetes: ExecutionKubernetesSettings = Field(
        default_factory=ExecutionKubernetesSettings
    )
    ssh: ExecutionSSHSettings = Field(default_factory=ExecutionSSHSettings)

    @model_validator(mode="after")
    def _validate_unique_profiles(self) -> "ExecutionControlSettings":
        ids = [profile.profile_id for profile in self.profiles]
        if len(ids) != len(set(ids)):
            raise ValueError(
                "execution_control.profiles contains duplicate profile ids."
            )
        return self
