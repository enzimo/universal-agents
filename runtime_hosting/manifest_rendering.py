"""Manifest rendering configuration and contracts."""

from __future__ import annotations
import json
import os
from pathlib import Path
from runtime_settings.telemetry import (
    ACTIVITY_COLLECTOR_ADMIN_TOKEN_ENV,
    ACTIVITY_COLLECTOR_USER_TOKEN_ENV,
)
from assistant_gateway.config import (
    GatewayInstanceConfig,
    resolve_activity_collector_runtime_base_url,
)
from secure_tool_broker.mcp_config import BrokerMCPConfig
from storage.backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from storage.config import StorageSettings
from secure_tool_broker.defaults import resolve_secure_tool_broker_internal_base_url
from runtime_hosting.manifests import (
    ActivityCollectorRuntimeManifestV1,
    AdapterHostRuntimeManifestV1,
    ExecutionControllerRuntimeManifestV1,
    ExecutionControllerSettingsV1,
    GatewayAssistantRuntimeContextV1,
    GatewayRuntimeManifestV1,
    GatewayUserRuntimeContextV1,
    RUNTIME_MANIFEST_FILENAMES,
    RUNTIME_MANIFEST_SCHEMA_VERSION,
    RuntimeManifestV1,
    RuntimeNatsManifestV1,
    RuntimeSecretReferenceV1,
    RuntimeStorageManifestV1,
    SecureBrokerRuntimeManifestV1,
    _ManifestT,
    _sha256,
)


def _with_hash(model: type[_ManifestT], payload: dict[str, object]) -> _ManifestT:
    content_sha256 = _sha256(payload)
    return model.model_validate({**payload, "content_sha256": content_sha256})


def _storage_manifest(settings: StorageSettings) -> RuntimeStorageManifestV1:
    if settings.backend == STORAGE_BACKEND_SQLITE:
        return RuntimeStorageManifestV1(
            backend=STORAGE_BACKEND_SQLITE,
            sqlite_path=settings.sqlite_path,
        )
    return RuntimeStorageManifestV1(
        backend=STORAGE_BACKEND_POSTGRES,
        postgres_dsn=RuntimeSecretReferenceV1.for_env("POSTGRES_DSN"),
    )


def _transport_manifest(config: GatewayInstanceConfig) -> RuntimeNatsManifestV1:
    transport = config.transport
    servers = tuple(transport.nats_servers or ()) or (transport.nats_url,)
    return RuntimeNatsManifestV1(
        servers=servers,
        client_name=transport.nats_name,
        jetstream_enabled=transport.jetstream_enabled,
        subject_prefix=transport.subject_prefix,
        storage=transport.storage,
        replicas=transport.replicas,
        ack_wait_seconds=transport.ack_wait_seconds,
        max_deliver=transport.max_deliver,
        duplicate_window_seconds=transport.duplicate_window_seconds,
        work_max_age_seconds=transport.work_max_age_seconds,
        presence_max_age_seconds=transport.presence_max_age_seconds,
        process_consumer_inactive_threshold_seconds=(
            transport.process_consumer_inactive_threshold_seconds
        ),
        user=RuntimeSecretReferenceV1.for_env("NATS_USER"),
        password=RuntimeSecretReferenceV1.for_env("NATS_PASSWORD"),
    )


def _enabled_interactive_channels(config: GatewayInstanceConfig) -> tuple[str, ...]:
    return tuple(
        name
        for name, enabled in (
            ("discord", config.interactive_apps.discord.enabled),
            ("telegram", config.interactive_apps.telegram.enabled),
            ("whatsapp", config.interactive_apps.whatsapp.enabled),
            ("signal", config.interactive_apps.signal.enabled),
            ("photon", config.interactive_apps.photon.enabled),
            ("website", config.interactive_apps.internal_website.enabled),
        )
        if enabled
    )


def render_gateway_runtime_manifest(
    config: GatewayInstanceConfig,
) -> GatewayRuntimeManifestV1:
    source_hash = _source_config_hash(config)
    payload: dict[str, object] = {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "service": "gateway",
        "source_config_schema_version": config.schema_version,
        "source_config_sha256": source_hash,
        "instance_name": config.instance_name,
        "instance_root_path": str(config.instance_root_path),
        "workspace_path": str(config.workspace_path),
        "team_id": config.manager.team_id,
        "manager_named_agent_type": config.manager.named_agent_type,
        "manager_agent_id": config.manager.manager_agent_id,
        "response_route": config.manager.response_queue_name,
        "manager_request_timeout_seconds": config.manager.request_timeout_seconds,
        "user": GatewayUserRuntimeContextV1(
            display_name=config.user.display_name,
            canonical_user_id=config.user.canonical_user_id,
            timezone=config.user.timezone,
            default_session_key=config.user.default_session_key,
            users_enabled=config.gateway.users.enabled,
        ),
        "assistant": GatewayAssistantRuntimeContextV1(
            display_name=config.assistant.display_name,
            manager_transition_notices_enabled=(
                config.assistant.manager_transition_notices_enabled
            ),
            unavailable_message_template=config.assistant.unavailable_message_template,
            available_message_template=config.assistant.available_message_template,
        ),
        "state_storage": _storage_manifest(config.state_storage_settings),
        "transport": _transport_manifest(config),
        "authorization_enabled": config.authorization.enabled,
        "secure_broker_internal_base_url": resolve_secure_tool_broker_internal_base_url(
            config.security_layer.broker_internal_base_url
        ),
        "secure_broker_token": RuntimeSecretReferenceV1.for_env(
            "SECURE_TOOL_BROKER_TOKEN"
        ),
        "activity_collector_base_url": resolve_activity_collector_runtime_base_url(
            config
        ),
        "activity_collector_ingest_token": RuntimeSecretReferenceV1.for_env(
            config.activity_collector.ingest_token_env_var
        ),
        "enabled_interactive_channels": _enabled_interactive_channels(config),
    }
    return _with_hash(GatewayRuntimeManifestV1, payload)


def render_activity_collector_runtime_manifest(
    config: GatewayInstanceConfig,
) -> ActivityCollectorRuntimeManifestV1:
    settings = config.activity_collector
    payload: dict[str, object] = {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "service": "activity_collector",
        "source_config_schema_version": config.schema_version,
        "source_config_sha256": _source_config_hash(config),
        "team_id": config.manager.team_id,
        "bind_host": settings.bind_host,
        "bind_port": settings.bind_port,
        "user_url_path": settings.user_url_path,
        "admin_url_path": settings.admin_url_path,
        "otel_trace_base_url": settings.otel_trace_base_url,
        "batch_max_events": settings.batch_max_events,
        "storage": _storage_manifest(config.activity_collector_storage_settings),
        "user_token": RuntimeSecretReferenceV1.for_env(
            ACTIVITY_COLLECTOR_USER_TOKEN_ENV
        ),
        "admin_token": RuntimeSecretReferenceV1.for_env(
            ACTIVITY_COLLECTOR_ADMIN_TOKEN_ENV
        ),
        "ingest_token": RuntimeSecretReferenceV1.for_env(settings.ingest_token_env_var),
    }
    return _with_hash(ActivityCollectorRuntimeManifestV1, payload)


def render_secure_broker_runtime_manifest(
    config: GatewayInstanceConfig,
) -> SecureBrokerRuntimeManifestV1:
    security = config.security_layer
    payload: dict[str, object] = {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "service": "secure_broker",
        "source_config_schema_version": config.schema_version,
        "source_config_sha256": _source_config_hash(config),
        "bind_host": security.broker_bind_host or "0.0.0.0",
        "bind_port": security.broker_bind_port or 18_600,
        "authorization_enabled": config.authorization.enabled,
        "token": RuntimeSecretReferenceV1.for_env("SECURE_TOOL_BROKER_TOKEN"),
        "mcp": BrokerMCPConfig.from_runtime_settings(
            instance_root_path=str(config.instance_root_path),
            agent=config.agent,
            agent_types=config.agent_types,
            mcp_profiles=config.mcp_profiles,
            mcp=config.mcp,
        ),
    }
    return _with_hash(SecureBrokerRuntimeManifestV1, payload)


def render_execution_controller_runtime_manifest(
    config: GatewayInstanceConfig,
) -> ExecutionControllerRuntimeManifestV1:
    settings = config.execution_control
    runner = settings.runner
    payload: dict[str, object] = {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "service": "execution_controller",
        "source_config_schema_version": config.schema_version,
        "source_config_sha256": _source_config_hash(config),
        "instance_name": config.instance_name,
        "team_id": config.manager.team_id,
        "workspace_path": str(config.workspace_path),
        "storage": _storage_manifest(config.manager_storage_settings),
        "transport": _transport_manifest(config),
        "artifact_store_path": str(config.layout.execution_artifacts_dir),
        "run_path": str(config.layout.execution_runs_dir),
        "maximum_artifact_bytes": settings.artifact_store.maximum_artifact_bytes,
        "profiles": settings.profiles,
        "kubectl_executable": settings.kubernetes.kubectl_executable,
        "kubernetes_network_policy_enforced": settings.kubernetes.network_policy_enforced,
        "ssh_enabled": settings.ssh.enabled,
        "ssh_executable": settings.ssh.ssh_executable,
        "ssh_lease_ttl_seconds": settings.ssh.lease_ttl_seconds,
        "ssh_transcript_limit_bytes": settings.ssh.transcript_limit_bytes,
        "ssh_targets": settings.ssh.targets,
        "controller": ExecutionControllerSettingsV1(
            enabled=settings.enabled and runner.enabled,
            tick_interval_seconds=runner.tick_interval_seconds,
            max_concurrent_jobs=runner.max_concurrent_jobs,
            lease_ttl_seconds=runner.lease_ttl_seconds,
            service_reconcile_interval_seconds=runner.service_reconcile_interval_seconds,
        ),
    }
    return _with_hash(ExecutionControllerRuntimeManifestV1, payload)


def render_adapter_host_runtime_manifest(
    config: GatewayInstanceConfig,
) -> AdapterHostRuntimeManifestV1:
    settings = config.interactive_apps.adapter_host
    plugin_ids = tuple(
        plugin_id
        for plugin_id, plugin in config.interactive_apps.plugins.items()
        if plugin.enabled and plugin.execution == "adapter_host"
    )
    payload: dict[str, object] = {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "service": "adapter_host",
        "source_config_schema_version": config.schema_version,
        "source_config_sha256": _source_config_hash(config),
        "instance_name": config.instance_name,
        "instance_root_path": str(config.instance_root_path),
        "enabled": settings.enabled,
        "host_id": settings.host_id,
        "transport": settings.transport,
        "unix_socket_path": settings.unix_socket_path,
        "bind_host": settings.bind_host,
        "bind_port": settings.bind_port,
        "connect_host": settings.connect_host,
        "client_state_path": settings.client_state_path,
        "token": RuntimeSecretReferenceV1.for_env(settings.token_env_var),
        "plugin_ids": plugin_ids,
    }
    return _with_hash(AdapterHostRuntimeManifestV1, payload)


def render_runtime_manifests(
    config: GatewayInstanceConfig,
) -> tuple[RuntimeManifestV1, ...]:
    """Render every service projection from one validated authoring config."""

    return (
        render_gateway_runtime_manifest(config),
        render_activity_collector_runtime_manifest(config),
        render_secure_broker_runtime_manifest(config),
        render_execution_controller_runtime_manifest(config),
        render_adapter_host_runtime_manifest(config),
    )


def write_runtime_manifests(
    config: GatewayInstanceConfig,
    output_dir: str | Path,
) -> tuple[Path, ...]:
    """Atomically write deterministic service manifests for a launcher."""

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for manifest in render_runtime_manifests(config):
        path = destination / RUNTIME_MANIFEST_FILENAMES[manifest.service]
        temporary = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
        temporary.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        written.append(path)
    return tuple(written)


def _source_config_hash(config: GatewayInstanceConfig) -> str:
    return _sha256(config.to_yaml_dict())
