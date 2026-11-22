"""Execution configuration and contracts."""

from __future__ import annotations
import ipaddress
from typing import Any
from pydantic import BaseModel, Field, field_validator, model_validator
from default_values import EXECUTION_CONTROL_DEFAULTS
from runtime_hosting.execution_contracts import (
    ExecutionProfileV1,
    validate_ssh_host_syntax,
    validate_ssh_username,
)
from runtime_hosting.ssh_network import normalize_ssh_hostname


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
    allowed_cidrs: tuple[str, ...] = Field(
        default_factory=lambda: tuple(
            EXECUTION_CONTROL_DEFAULTS.ssh.value["allowed_cidrs"]
        )
    )
    allowed_domains: tuple[str, ...] = Field(
        default_factory=lambda: tuple(
            EXECUTION_CONTROL_DEFAULTS.ssh.value["allowed_domains"]
        )
    )
    denied_cidrs: tuple[str, ...] = Field(
        default_factory=lambda: tuple(
            EXECUTION_CONTROL_DEFAULTS.ssh.value["denied_cidrs"]
        )
    )
    max_delegation_ttl_seconds: int = EXECUTION_CONTROL_DEFAULTS.ssh.value[
        "max_delegation_ttl_seconds"
    ]
    allow_password_bootstrap: bool = EXECUTION_CONTROL_DEFAULTS.ssh.value[
        "allow_password_bootstrap"
    ]
    targets: tuple["ExecutionSSHTargetSettings", ...] = ()

    @field_validator(
        "lease_ttl_seconds",
        "transcript_limit_bytes",
        "max_delegation_ttl_seconds",
        mode="before",
    )
    @classmethod
    def _normalize_positive_int(cls, value: Any, info: Any) -> int:
        if value is None or isinstance(value, (bool, float)):
            raise ValueError(
                f"execution_control.ssh.{info.field_name} must be a positive integer."
            )
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str):
            stripped = value.strip()
            if not stripped or not stripped.lstrip("+-").isdigit():
                raise ValueError(
                    f"execution_control.ssh.{info.field_name} must be a positive integer."
                )
            parsed = int(stripped)
        else:
            raise ValueError(
                f"execution_control.ssh.{info.field_name} must be a positive integer."
            )
        if parsed <= 0:
            raise ValueError(f"execution_control.ssh.{info.field_name} must be > 0.")
        return parsed

    @field_validator("allowed_cidrs", "denied_cidrs", mode="before")
    @classmethod
    def _normalize_cidrs(cls, value: Any, info: Any) -> tuple[str, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError(
                f"execution_control.ssh.{info.field_name} must be a list of CIDR strings."
            )
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"execution_control.ssh.{info.field_name} entries must be non-empty CIDR strings."
                )
            try:
                network = ipaddress.ip_network(item.strip(), strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"execution_control.ssh.{info.field_name} contains invalid CIDR {item!r}."
                ) from exc
            canonical = str(network)
            if canonical not in seen:
                seen.add(canonical)
                normalized.append(canonical)
        return tuple(normalized)

    @field_validator("allowed_domains", mode="before")
    @classmethod
    def _normalize_domains(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError(
                "execution_control.ssh.allowed_domains must be a list of domain names."
            )
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise ValueError(
                    "execution_control.ssh.allowed_domains entries must be domain strings."
                )
            stripped = item.strip().rstrip(".")
            try:
                ipaddress.ip_address(stripped)
            except ValueError:
                pass
            else:
                raise ValueError(
                    f"execution_control.ssh.allowed_domains entry {item!r} is an IP address; use allowed_cidrs instead."
                )
            try:
                candidate = normalize_ssh_hostname(item)
            except ValueError as exc:
                raise ValueError(
                    f"execution_control.ssh.allowed_domains contains invalid domain {item!r}."
                ) from exc
            if candidate not in seen:
                seen.add(candidate)
                normalized.append(candidate)
        return tuple(normalized)

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

    @field_validator("host", mode="after")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        return validate_ssh_host_syntax(value)

    @field_validator("username", mode="after")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        return validate_ssh_username(value)


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
