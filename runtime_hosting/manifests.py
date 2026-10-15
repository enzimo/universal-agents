"""Manifests configuration and contracts."""

from __future__ import annotations
import hashlib
import json
from typing import Literal, Self, TypeVar
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import to_jsonable_python
from runtime_settings.execution import ExecutionSSHTargetSettings
from secure_tool_broker.mcp_config import BrokerMCPConfig
from storage.backends import STORAGE_BACKEND_SQLITE
from runtime_hosting.execution_contracts import ExecutionProfileV1


RUNTIME_MANIFEST_SCHEMA_VERSION: Literal[1] = 1


RUNTIME_MANIFEST_FILENAMES = {
    "gateway": "gateway.runtime-manifest.v1.json",
    "activity_collector": "activity-collector.runtime-manifest.v1.json",
    "secure_broker": "secure-broker.runtime-manifest.v1.json",
    "execution_controller": "execution-controller.runtime-manifest.v1.json",
    "adapter_host": "adapter-host.runtime-manifest.v1.json",
}


def _canonical_json(payload: object) -> str:
    return json.dumps(
        to_jsonable_python(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class RuntimeManifestModel(BaseModel):
    """Strict immutable base for service runtime configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class HashedRuntimeManifestModel(RuntimeManifestModel):
    """Base for manifests whose canonical payload carries an integrity hash."""

    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


_ManifestT = TypeVar("_ManifestT", bound=HashedRuntimeManifestModel)


class RuntimeSecretReferenceV1(RuntimeManifestModel):
    """Environment/file reference; secret material never enters a manifest."""

    env_var: str = Field(min_length=1, max_length=240)
    file_env_var: str = Field(min_length=1, max_length=245)

    @classmethod
    def for_env(cls, env_var: str) -> Self:
        normalized = str(env_var or "").strip()
        return cls(env_var=normalized, file_env_var=f"{normalized}_FILE")


class RuntimeStorageManifestV1(RuntimeManifestModel):
    backend: Literal["sqlite", "postgres"]
    sqlite_path: str | None = None
    postgres_dsn: RuntimeSecretReferenceV1 | None = None

    @model_validator(mode="after")
    def _validate_backend_fields(self) -> Self:
        if self.backend == STORAGE_BACKEND_SQLITE:
            if not self.sqlite_path or self.postgres_dsn is not None:
                raise ValueError("sqlite storage requires only sqlite_path")
        elif self.sqlite_path is not None or self.postgres_dsn is None:
            raise ValueError("postgres storage requires only postgres_dsn")
        return self


class RuntimeNatsManifestV1(RuntimeManifestModel):
    servers: tuple[str, ...] = Field(min_length=1)
    client_name: str | None = None
    jetstream_enabled: bool
    subject_prefix: str
    storage: str
    replicas: int = Field(ge=1)
    ack_wait_seconds: float = Field(gt=0)
    max_deliver: int = Field(ge=1)
    duplicate_window_seconds: float = Field(gt=0)
    work_max_age_seconds: float | None = Field(default=None, gt=0)
    presence_max_age_seconds: float | None = Field(default=None, gt=0)
    process_consumer_inactive_threshold_seconds: float = Field(gt=0)
    user: RuntimeSecretReferenceV1
    password: RuntimeSecretReferenceV1
    credentials_file_env_var: str = "NATS_CREDENTIALS_FILE"


class GatewayUserRuntimeContextV1(RuntimeManifestModel):
    display_name: str
    canonical_user_id: str
    timezone: str
    default_session_key: str
    users_enabled: bool


class GatewayAssistantRuntimeContextV1(RuntimeManifestModel):
    display_name: str
    manager_transition_notices_enabled: bool
    unavailable_message_template: str
    available_message_template: str


class GatewayRuntimeManifestV1(HashedRuntimeManifestModel):
    schema_version: Literal[1] = RUNTIME_MANIFEST_SCHEMA_VERSION
    service: Literal["gateway"] = "gateway"
    source_config_schema_version: int = Field(ge=1)
    source_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance_name: str
    instance_root_path: str
    workspace_path: str
    team_id: str
    manager_named_agent_type: str
    manager_agent_id: str | None = None
    response_route: str
    manager_request_timeout_seconds: float = Field(gt=0)
    user: GatewayUserRuntimeContextV1
    assistant: GatewayAssistantRuntimeContextV1
    state_storage: RuntimeStorageManifestV1
    transport: RuntimeNatsManifestV1
    authorization_enabled: bool
    secure_broker_internal_base_url: str | None = None
    secure_broker_token: RuntimeSecretReferenceV1
    activity_collector_base_url: str | None = None
    activity_collector_ingest_token: RuntimeSecretReferenceV1
    enabled_interactive_channels: tuple[str, ...]

    @model_validator(mode="after")
    def _verify_content_hash(self) -> Self:
        return _verify_manifest_hash(self)


class ActivityCollectorRuntimeManifestV1(HashedRuntimeManifestModel):
    schema_version: Literal[1] = RUNTIME_MANIFEST_SCHEMA_VERSION
    service: Literal["activity_collector"] = "activity_collector"
    source_config_schema_version: int = Field(ge=1)
    source_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    team_id: str
    bind_host: str
    bind_port: int = Field(ge=1, le=65_535)
    user_url_path: str
    admin_url_path: str
    otel_trace_base_url: str | None = None
    batch_max_events: int = Field(ge=1, le=1_000)
    storage: RuntimeStorageManifestV1
    user_token: RuntimeSecretReferenceV1
    admin_token: RuntimeSecretReferenceV1
    ingest_token: RuntimeSecretReferenceV1

    @model_validator(mode="after")
    def _verify_content_hash(self) -> Self:
        return _verify_manifest_hash(self)


class SecureBrokerRuntimeManifestV1(HashedRuntimeManifestModel):
    schema_version: Literal[1] = RUNTIME_MANIFEST_SCHEMA_VERSION
    service: Literal["secure_broker"] = "secure_broker"
    source_config_schema_version: int = Field(ge=1)
    source_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bind_host: str
    bind_port: int = Field(ge=1, le=65_535)
    authorization_enabled: bool
    token: RuntimeSecretReferenceV1
    mcp: BrokerMCPConfig

    @model_validator(mode="after")
    def _verify_content_hash(self) -> Self:
        return _verify_manifest_hash(self)


class ExecutionControllerSettingsV1(RuntimeManifestModel):
    enabled: bool
    tick_interval_seconds: float = Field(gt=0)
    max_concurrent_jobs: int = Field(ge=1)
    lease_ttl_seconds: int = Field(ge=1)
    service_reconcile_interval_seconds: float = Field(gt=0)


class ExecutionControllerRuntimeManifestV1(HashedRuntimeManifestModel):
    schema_version: Literal[1] = RUNTIME_MANIFEST_SCHEMA_VERSION
    service: Literal["execution_controller"] = "execution_controller"
    source_config_schema_version: int = Field(ge=1)
    source_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance_name: str
    team_id: str
    workspace_path: str
    storage: RuntimeStorageManifestV1
    transport: RuntimeNatsManifestV1
    artifact_store_path: str
    run_path: str
    maximum_artifact_bytes: int = Field(ge=1)
    profiles: tuple[ExecutionProfileV1, ...]
    kubectl_executable: str
    kubernetes_network_policy_enforced: bool
    ssh_enabled: bool
    ssh_executable: str
    ssh_lease_ttl_seconds: int = Field(ge=1)
    ssh_transcript_limit_bytes: int = Field(ge=1)
    ssh_targets: tuple[ExecutionSSHTargetSettings, ...]
    controller: ExecutionControllerSettingsV1

    @model_validator(mode="after")
    def _verify_content_hash(self) -> Self:
        return _verify_manifest_hash(self)


class AdapterHostRuntimeManifestV1(HashedRuntimeManifestModel):
    """Service-scoped identity and transport projection for the plugin host."""

    schema_version: Literal[1] = RUNTIME_MANIFEST_SCHEMA_VERSION
    service: Literal["adapter_host"] = "adapter_host"
    source_config_schema_version: int = Field(ge=1)
    source_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance_name: str
    instance_root_path: str
    enabled: bool
    host_id: str
    transport: Literal["unix", "mtls"]
    unix_socket_path: str
    bind_host: str
    bind_port: int = Field(ge=1, le=65_535)
    connect_host: str
    client_state_path: str
    token: RuntimeSecretReferenceV1
    plugin_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _verify_content_hash(self) -> Self:
        return _verify_manifest_hash(self)


RuntimeManifestV1 = (
    GatewayRuntimeManifestV1
    | ActivityCollectorRuntimeManifestV1
    | SecureBrokerRuntimeManifestV1
    | ExecutionControllerRuntimeManifestV1
    | AdapterHostRuntimeManifestV1
)


def _verify_manifest_hash(manifest: _ManifestT) -> _ManifestT:
    payload = manifest.model_dump(mode="json", exclude={"content_sha256"})
    if manifest.content_sha256 != _sha256(payload):
        raise ValueError("runtime manifest content_sha256 does not match its payload")
    return manifest
