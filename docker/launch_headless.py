#!/usr/bin/env python3
"""Start manager, gateway, and activity collector for containerized headless runtime."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from enum import StrEnum
import functools
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_messages.contracts.runtime_v1 import (
    AUTHORITY_MODE_AUTHENTICATED_GATEWAY_OPERATOR,
    AUTHORITY_MODE_AUTHORIZATION_DISABLED,
    AUTHORITY_MODE_PERSON_APPROVAL,
    RUNTIME_RESTART_REQUEST_SCHEMA_VERSION,
)
from custom_agents.governance.authorized_operations import atomic_write_json
from custom_messages.contracts.common_v1 import utc_now
from id_generation import new_id
from runtime_settings.instance_layout import InstanceLayout
from access_control.caller_tokens import (
    CALLER_GATEWAY,
    CALLER_MANAGER,
    agent_caller_label,
    derive_caller_token,
)
from access_control.identity import (
    ENV_GATEWAY_SIGNING_KEY,
    ENV_SCHEDULER_SIGNING_KEY,
    ENV_VERIFY_KEYS,
    ISSUER_GATEWAY,
    ISSUER_SCHEDULER,
    public_entry,
    verify_keys_value,
)
from assistant_gateway.config import GatewayInstanceConfig, StartupAgentSettings
from assistant_gateway.instance_secrets import (
    ENVIRONMENT_SECRETS_DOCS,
    missing_secrets,
    missing_secrets_text,
)
from runtime_settings.network import compose_public_base_url
from runtime_settings.access_cutover import obsolete_binding_data, obsolete_binding_message
from runtime_settings.approval_cutover import old_approval_message, old_approval_records
from runtime_settings.legacy_layout import legacy_layout_message, legacy_layout_moves
from assistant_gateway.config import ensure_required_startup_agents
from assistant_gateway.config import (
    inspect_instance_config_version,
    migrate_instance_config_file,
)
from runtime_settings.governance import reject_removed_file_access_env_vars
from runtime_settings.network import reject_removed_public_url_env_vars
from runtime_settings.security import (
    reject_removed_credential_catalog_env_vars,
    reject_removed_sealbox_env_vars,
    reject_removed_secure_cli_env_var,
    reject_removed_tool_credential_env_vars,
)
from runtime_settings.services import reject_removed_web_service_env_vars
from assistant_gateway.config import resolve_activity_collector_runtime_base_url
from assistant_gateway.nats_manager_runtime import NatsGatewayManagerRuntime
from custom_transport.nats_topology import NatsTopologyMismatch
from nats.errors import Error as NatsError
from default_values import (
    EMBEDDING_DEFAULTS,
    STORAGE_DEFAULTS,
)
from custom_agents.external_skills import (
    ExternalSkillsSyncError,
    ExternalSkillsSyncResult,
    sync_external_skill_repositories,
)
from custom_agents.team_composition import (
    RemovedRosterAttachments,
    load_team_composition,
)
from custom_agents.memory_graph_embedding_profile_runtime import (
    build_embedding_profile_spec,
    ensure_memory_graph_embedding_profile,
)
from env_secrets import (
    PROCESS_ACTIVITY_COLLECTOR,
    PROCESS_ADAPTER_HOST,
    PROCESS_AGENT,
    PROCESS_CONSUMER_CHECK,
    PROCESS_EXECUTION_CONTROLLER,
    PROCESS_GATEWAY,
    PROCESS_MANAGER,
    PROCESS_OPENSHELL_CLI,
    PROCESS_OPENSHELL_GATEWAY,
    PROCESS_SEALBOX,
    PROCESS_SECURE_BROKER,
    environment_for,
    materialize_secret_file_envs,
    withheld_names,
)
from runtime_settings.secret_names import configured_secret_names
from secure_tool_broker.credential_state import resolve_broker_credentials_dir
from secure_tool_broker.defaults import resolve_secure_tool_broker_internal_base_url
from secure_tool_broker.sealbox_runtime import (
    SealboxRuntimeConfig,
    apply_sealbox_cli_env,
    ensure_sealbox_token_file,
    resolve_sealbox_runtime_config,
    sealbox_server_env,
)
from storage.memory_graph_repository import create_memory_graph_repository
from storage.sqlite_connection import prepare_sqlite_database
from runtime_hosting.manifest_rendering import write_runtime_manifests
from runtime_hosting.process_run import ProcessRunBusy, acquire_process_run
from custom_tools import openshell_sandboxes
from runtime_settings.code_sandbox_contract import (
    ENV_CODE_SANDBOX_EXTRA_READ_PATHS,
    ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS,
    ENV_CODE_SANDBOX_MODE,
)
from docker.nats_service import (
    apply_local_nats_env,
    local_nats_enabled_from_env,
    start_local_nats,
)
from docker.settings_environment import SettingsEnvironment, environment_overrides, write_environment_overrides
from docker.python_entrypoints import resolve_python_entrypoint

try:
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover - dependency is expected in normal runtime
    dotenv_values = None


LOGGER = logging.getLogger("docker.launch_headless")
HEADLESS_STARTUP_NOTICE_ENV = "TEAM_HEADLESS_STARTUP_NOTICE"
# openshell-gateway reads its SSH handshake secret from this variable (the
# environment form of its --ssh-handshake-secret flag), which keeps the secret
# off the command line that every process can read.
OPENSHELL_HANDSHAKE_SECRET_ENV = "OPENSHELL_SSH_HANDSHAKE_SECRET"
OPENSHELL_HANDSHAKE_SECRET_FILENAME = "ssh_handshake_secret"


@dataclass(frozen=True)
class ManagedOpenShellSandbox:
    """Per-person OpenShell sandboxes: their name prefix, and whether to delete them at shutdown."""

    prefix: str
    delete_on_shutdown: bool


@dataclass(frozen=True)
class ManagedOpenShellGateway:
    name: str
    endpoint: str
    process: subprocess.Popen[bytes]


@dataclass(frozen=True)
class ManagedSealboxService:
    runtime: SealboxRuntimeConfig
    process: subprocess.Popen[bytes]


@dataclass(frozen=True)
class ServiceProcessEnvironments:
    """Service-scoped child environments with explicit manifest ownership."""

    gateway: dict[str, str]
    activity_collector: dict[str, str]
    secure_broker: dict[str, str]
    execution_controller: dict[str, str]
    adapter_host: dict[str, str]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch the TeamArchitect and gateway for an onboarded instance.",
    )
    parser.add_argument(
        "--config", required=True, help="Path to the onboarded instance_config.yaml"
    )
    parser.add_argument(
        "--env",
        default=None,
        help="Optional env file to merge before launch. Overrides config.launch.env_file_path.",
    )
    parser.add_argument(
        "--refresh-external-skills",
        action="store_true",
        help="Refresh configured external skill repositories before any agents start.",
    )
    parser.add_argument(
        "--skills-repository",
        action="append",
        default=None,
        help=(
            "Public GitHub repository containing a skills/ directory at any depth. Repeat to override "
            "workspace.external_skill_repositories for this launch."
        ),
    )
    parser.add_argument(
        "gateway_args",
        nargs=argparse.REMAINDER,
        help="Additional arguments forwarded to assistant_cli.py run-gateway",
    )
    return parser


def _render_instance_config_migration_notice(
    config_path: Path,
    *,
    original_version: int,
    migrated_version: int,
    backup_path: Path | None,
) -> str:
    backup_suffix = (
        f" Backup written to {backup_path}." if backup_path is not None else ""
    )
    return (
        f"Headless startup migrated {config_path.name} from schema_version {original_version} "
        f"to {migrated_version}.{backup_suffix}"
    )


def ensure_instance_config_ready_for_headless_launch(config_path: Path) -> str | None:
    status = inspect_instance_config_version(config_path)
    if not status.needs_migration:
        return None
    result = migrate_instance_config_file(status.path)
    LOGGER.warning(
        "Headless launch migrated stale instance config %s from schema_version %s to %s; backup=%s",
        result.path,
        result.original_version,
        result.migrated_version,
        result.backup_path or "-",
    )
    return _render_instance_config_migration_notice(
        result.path,
        original_version=result.original_version,
        migrated_version=result.migrated_version,
        backup_path=result.backup_path,
    )


def _parse_startup_notices(raw: str | None) -> list[str]:
    payload = str(raw or "").strip()
    if not payload:
        return []
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        decoded = payload
    if isinstance(decoded, list):
        notices: list[str] = []
        for item in decoded:
            rendered = str(item or "").strip()
            if rendered:
                notices.append(rendered)
        return notices
    rendered = str(decoded or "").strip()
    return [rendered] if rendered else []


def append_headless_startup_notice(env: dict[str, str], notice: str | None) -> None:
    rendered = str(notice or "").strip()
    if not rendered:
        return
    notices = _parse_startup_notices(env.get(HEADLESS_STARTUP_NOTICE_ENV))
    notices.append(rendered)
    env[HEADLESS_STARTUP_NOTICE_ENV] = json.dumps(notices)


def resolve_env_file_path(
    config: GatewayInstanceConfig,
    explicit_env_path: str | None,
    *,
    report_ignored_env_file: bool = True,
) -> Path | None:
    """The env file to read, or None.

    With launch.secrets_source: environment no env file is read: secrets come
    only from the process environment and Docker secrets. An explicit --env
    (TEAM_ASSISTANT_ENV_FILE) contradicts that and is refused; a team.env left
    in the instance folder is ignored with one warning. The bundle supervisor
    passes ``report_ignored_env_file=False`` because the launcher it starts
    reports it.
    """

    if config.launch.secrets_from_environment:
        if explicit_env_path:
            raise ValueError(
                f"An env file was given ({explicit_env_path}), but launch.secrets_source is "
                "environment, so secrets come only from environment variables and Docker secrets. "
                "Remove --env (or TEAM_ASSISTANT_ENV_FILE), or set launch.secrets_source to env_file."
            )
        if report_ignored_env_file:
            _warn_ignored_team_env(config)
        return None
    raw_path = explicit_env_path or config.launch.env_file_path
    if not raw_path:
        return None
    env_path = Path(raw_path).expanduser().resolve()
    if not env_path.exists():
        if explicit_env_path:
            raise FileNotFoundError(f"Explicit env file does not exist: {env_path}")
        LOGGER.warning(
            "Configured env file does not exist at %s; continuing without it.", env_path
        )
        return None
    if not env_path.is_file():
        raise ValueError(f"Env path is not a file: {env_path}")
    return env_path


def warn_missing_environment_secrets(
    config: GatewayInstanceConfig, env: Mapping[str, str]
) -> None:
    """With secrets from the environment, name required secrets it lacks (names only).

    The signing keys refuse the launch on their own (task_context_keys_missing);
    the rest would fail later in the service that needs them, so say it now.
    """

    if not config.launch.secrets_from_environment:
        return
    missing = [
        item
        for item in missing_secrets(config, env=env)
        if item.name not in TASK_CONTEXT_SIGNING_KEYS
    ]
    if missing:
        LOGGER.warning(
            "Secrets this configuration needs are missing from the environment. "
            "event=launch_secrets_missing names=%s\n%s",
            ",".join(item.name for item in missing),
            missing_secrets_text(missing),
        )


def _warn_ignored_team_env(config: GatewayInstanceConfig) -> None:
    team_env = config.layout.team_env_path
    if team_env.exists():
        LOGGER.warning(
            "%s exists but is ignored: launch.secrets_source is environment, so secrets come only "
            "from environment variables and Docker secrets. Delete the file once its values are "
            "Docker secrets. event=launch_team_env_ignored path=%s",
            team_env,
            team_env,
        )


def resolve_manager_db_path(config: GatewayInstanceConfig) -> Path:
    if config.state.storage.backend != STORAGE_DEFAULTS.backend.value:
        return config.layout.task_database_path
    settings = config.manager_storage_settings
    if not settings.sqlite_path:
        return config.layout.task_database_path
    return Path(settings.sqlite_path).expanduser().resolve()


def prepare_manager_database(config: GatewayInstanceConfig) -> Path | None:
    """Prime the manager database before any service opens it on each launch.

    The manager, gateway, secure tool broker, execution controller and
    startup agents all open this file as they start. On the first start its
    WAL switch and first write held SQLite's locks across several disk
    flushes, and on a slow disk the other services' single lock wait expired
    and every service stopped (``storage.sqlite_connection``). The launcher
    holds the run lock here, so no process of an earlier run uses the file.
    Returns the prepared SQLite path, or None when the manager store is not a
    SQLite file.
    """

    if config.state.storage.backend != STORAGE_DEFAULTS.backend.value:
        return None
    path = resolve_manager_db_path(config)
    prepare_sqlite_database(str(path), logger=LOGGER)
    return path


# What setting up the message streams can fail with: NATS client and JetStream
# errors, connection errors, and settings the broker did not take.
MESSAGE_STREAM_PREPARE_ERRORS = (NatsError, OSError, NatsTopologyMismatch)


def prepare_message_streams(config_path: Path, gateway_env: Mapping[str, str]) -> tuple[str, ...]:
    """Create or update the team's message streams before any service starts.

    The gateway owns the team's JetStream streams and creates or updates them
    when it connects. The manager, the startup agents and every worker only
    check, once, that the streams exist with the expected settings
    (``custom_transport.nats_topology``), and the launcher starts them all
    at the same moment. Whenever the streams were missing or out of date (a
    new team, an upgrade that adds or changes a stream, and with
    ``transport.storage: memory`` every start of the container's own NATS
    server), a manager whose check came before the gateway's setup stopped
    with "Stream ... is missing" and every service stopped with it; slow
    storage delays the gateway enough to make that order likely. So the
    launcher runs the gateway's own setup, with the gateway's configuration
    and environment, before it starts any service; the gateway repeats it
    when it connects and finds nothing to change. Nothing waits or polls.
    Returns the stream names, or an empty tuple when JetStream is off.
    """

    gateway_config = GatewayInstanceConfig.load_for_process(config_path, env=gateway_env)
    runtime = NatsGatewayManagerRuntime.from_gateway_config(gateway_config)
    started = time.monotonic()
    names = asyncio.run(runtime.prepare_topology())
    if not names:
        return names
    LOGGER.info(
        "Set up the team's message streams before starting the services. "
        "event=nats_streams_prepared streams=%s seconds=%.2f",
        ",".join(names),
        time.monotonic() - started,
    )
    return names


def build_instance_env_defaults(
    config: GatewayInstanceConfig,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    manager_storage = config.resolve_manager_storage_settings(env=env)
    broker_internal_base_url = resolve_secure_tool_broker_internal_base_url(
        config.security_layer.broker_internal_base_url
    )
    internal_website = config.interactive_apps.internal_website
    env_defaults = {
        "TEAM_INSTANCE_CONFIG": str(config.config_path),
        "TEAM_INSTANCE_ROOT": str(config.instance_root_path),
        "RUNTIME_RESTART_REQUEST_DIR": str(
            config.instance_root_path / "runtime" / "restart_requests"
        ),
        # The agents' area (workspaces/); every other location is derived
        # from TEAM_INSTANCE_ROOT by runtime_settings.instance_layout.
        "AGENT_WORKSPACE_DIR": str(config.workspace_path),
        "TRANSPORT_TYPE": config.transport.type,
        "NATS_URL": config.transport.nats_url,
        "NATS_SERVERS": ",".join(config.transport.nats_servers or []),
        "NATS_NAME": config.transport.nats_name or "",
        "NATS_SUBJECT_PREFIX": config.transport.subject_prefix,
        "NATS_JETSTREAM_ENABLED": str(bool(config.transport.jetstream_enabled)).lower(),
        "NATS_ACK_WAIT_SECONDS": str(config.transport.ack_wait_seconds),
        "NATS_MAX_DELIVER": str(config.transport.max_deliver),
        "NATS_PROCESS_CONSUMER_INACTIVE_THRESHOLD_SECONDS": str(
            config.transport.process_consumer_inactive_threshold_seconds
        ),
        "NATS_USER": config.transport.nats_user or "",
        "NATS_PASSWORD": config.transport.nats_password or "",
        "NATS_CREDENTIALS_FILE": config.transport.credentials_file or "",
        "DEFAULT_TEAM_ID": config.manager.team_id,
        "DEFAULT_REQUEST_TIMEZONE": config.user.timezone,
        # Local time for every process (log times, local clocks). An ambient
        # TZ overrides it, like every default here (build_process_env).
        "TZ": config.user.timezone,
        "MEMORY_GRAPH_ENABLED": str(bool(config.agent.memory_graph_enabled)).lower(),
        "STORAGE_BACKEND": manager_storage.backend,
        "NETWORK_PUBLIC_ORIGIN": config.network.public_origin,
        "SECURITY_LAYER_MODE": config.security_layer.mode,
        ENV_CODE_SANDBOX_MODE: config.security_layer.code_execution.sandbox,
        ENV_CODE_SANDBOX_EXTRA_READ_PATHS: json.dumps(
            config.security_layer.code_execution.extra_read_paths
        ),
        ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS: json.dumps(
            config.security_layer.code_execution.extra_write_paths
        ),
        "SECURE_TOOL_BROKER_PUBLIC_PORT": ""
        if config.security_layer.broker_public_port is None
        else str(config.security_layer.broker_public_port),
        "INTERNAL_WEBSITE_ENABLED": str(bool(internal_website.enabled)).lower(),
        "INTERNAL_WEBSITE_BIND_HOST": internal_website.bind_host,
        "INTERNAL_WEBSITE_BIND_PORT": str(internal_website.bind_port),
        "INTERNAL_WEBSITE_PUBLIC_PORT": ""
        if internal_website.public_port is None
        else str(internal_website.public_port),
        "INTERNAL_WEBSITE_BASE_URL": config.internal_website_base_url,
    }
    if internal_website.docs_root:
        env_defaults["INTERNAL_WEBSITE_DOCS_ROOT"] = internal_website.docs_root
    if internal_website.skills_dir:
        env_defaults["INTERNAL_WEBSITE_SKILLS_DIR"] = internal_website.skills_dir
    env_defaults["SECURE_TOOL_BROKER_INTERNAL_BASE_URL"] = broker_internal_base_url
    explicit_security_fields = getattr(config.security_layer, "model_fields_set", set())
    if (
        "broker_bind_host" in explicit_security_fields
        and config.security_layer.broker_bind_host
    ):
        env_defaults["SECURE_TOOL_BROKER_HOST"] = config.security_layer.broker_bind_host
    if (
        "broker_bind_port" in explicit_security_fields
        and config.security_layer.broker_bind_port
    ):
        env_defaults["SECURE_TOOL_BROKER_PORT"] = str(
            config.security_layer.broker_bind_port
        )
    if config.security_layer.mode == "openshell":
        env_defaults["OPENSHELL_WORKSPACE_MOUNT"] = (
            config.security_layer.openshell.workspace_mount
        )
        env_defaults["OPENSHELL_MANAGE_GATEWAY"] = str(
            bool(config.security_layer.openshell.manage_gateway)
        ).lower()
        env_defaults["OPENSHELL_GATEWAY_BIND_HOST"] = (
            config.security_layer.openshell.gateway_bind_host
        )
        env_defaults["OPENSHELL_GATEWAY_BIND_PORT"] = str(
            config.security_layer.openshell.gateway_bind_port
        )
        if config.security_layer.openshell.sandbox_name:
            env_defaults["OPENSHELL_SANDBOX_NAME"] = (
                config.security_layer.openshell.sandbox_name
            )
        if config.security_layer.openshell.gateway_name:
            env_defaults["OPENSHELL_GATEWAY"] = (
                config.security_layer.openshell.gateway_name
            )
        if config.security_layer.openshell.gateway_endpoint:
            env_defaults["OPENSHELL_GATEWAY_ENDPOINT"] = (
                config.security_layer.openshell.gateway_endpoint
            )
        if config.security_layer.openshell.gateway_binary:
            env_defaults["OPENSHELL_GATEWAY_BIN"] = (
                config.security_layer.openshell.gateway_binary
            )
    if config.security_layer.bitwarden_server_url:
        env_defaults["BITWARDEN_SERVER_URL"] = (
            config.security_layer.bitwarden_server_url
        )
    if config.security_layer.credential_catalog.enabled:
        env_defaults["SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH"] = str(
            config.credential_catalog_database_path
        )
        env_defaults["SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH"] = str(
            config.credential_template_overrides_path
        )
        env_defaults["SECURE_TOOL_BROKER_DEFAULT_CREDENTIAL_WRITE_SOURCE"] = (
            config.security_layer.credential_catalog.default_write_source
        )
        env_defaults["SECURE_TOOL_BROKER_OPENSHELL_BINDING_TTL_SECONDS"] = str(
            config.security_layer.credential_catalog.openshell_binding_ttl_seconds
        )
        env_defaults["SECURE_TOOL_BROKER_COLLECTION_SESSION_TTL_SECONDS"] = str(
            config.security_layer.credential_catalog.collection_session_ttl_seconds
        )
        env_defaults["SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS"] = str(
            config.security_layer.credential_catalog.secret_cache_ttl_seconds
        )
    if config.llm.provider:
        env_defaults["LLM_PROVIDER"] = config.llm.provider
    if config.llm.model_id:
        env_defaults["LLM_MODEL_ID"] = config.llm.model_id
    if config.llm.temperature is not None:
        env_defaults["LLM_TEMPERATURE"] = str(config.llm.temperature)
    if config.llm.stream is not None:
        env_defaults["LLM_STREAM"] = str(config.llm.stream).lower()
    if config.llm.request_timeout_seconds is not None:
        env_defaults["LLM_REQUEST_TIMEOUT_SECONDS"] = str(
            config.llm.request_timeout_seconds
        )
    if config.llm.reasoning_trace_retention_messages is not None:
        env_defaults["LLM_REASONING_TRACE_RETENTION_MESSAGES"] = str(
            config.llm.reasoning_trace_retention_messages
        )
    if config.llm.compaction_model_provider:
        env_defaults["LLM_COMPACTION_MODEL_PROVIDER"] = (
            config.llm.compaction_model_provider
        )
    if config.llm.compaction_model_id:
        env_defaults["LLM_COMPACTION_MODEL_ID"] = config.llm.compaction_model_id
    if config.llm.litellm_api_base:
        env_defaults["LITELLM_API_BASE"] = config.llm.litellm_api_base
    if config.llm.openai_codex.base_url:
        env_defaults["OPENAI_CODEX_BASE_URL"] = config.llm.openai_codex.base_url
    if config.llm.openai_codex.auth_store_path:
        env_defaults["OPENAI_CODEX_AUTH_STORE_PATH"] = (
            config.llm.openai_codex.auth_store_path
        )
    if config.llm.openai_codex.shared_codex_home:
        env_defaults["OPENAI_CODEX_SHARED_HOME"] = (
            config.llm.openai_codex.shared_codex_home
        )
    env_defaults["OPENAI_CODEX_ALLOW_SHARED_AUTH_READ"] = str(
        config.llm.openai_codex.allow_shared_codex_auth_read
    ).lower()
    env_defaults["OPENAI_CODEX_REFRESH_SKEW_SECONDS"] = str(
        config.llm.openai_codex.refresh_skew_seconds
    )
    env_defaults["OPENAI_CODEX_REFRESH_TIMEOUT_SECONDS"] = str(
        config.llm.openai_codex.refresh_timeout_seconds
    )
    if config.llm.xai_oauth.base_url:
        env_defaults["XAI_OAUTH_BASE_URL"] = config.llm.xai_oauth.base_url
    if config.llm.xai_oauth.auth_store_path:
        env_defaults["XAI_OAUTH_AUTH_STORE_PATH"] = config.llm.xai_oauth.auth_store_path
    env_defaults["XAI_OAUTH_REFRESH_SKEW_SECONDS"] = str(
        config.llm.xai_oauth.refresh_skew_seconds
    )
    env_defaults["XAI_OAUTH_REFRESH_TIMEOUT_SECONDS"] = str(
        config.llm.xai_oauth.refresh_timeout_seconds
    )
    if manager_storage.backend == "sqlite" and manager_storage.sqlite_path:
        env_defaults["SQLITE_DB_PATH"] = manager_storage.sqlite_path
        env_defaults["TASK_HISTORY_DB_PATH"] = manager_storage.sqlite_path
    elif manager_storage.backend == "postgres" and manager_storage.postgres_dsn:
        env_defaults["POSTGRES_DSN"] = manager_storage.postgres_dsn
    if config.embedding.provider:
        env_defaults["EMBEDDING_PROVIDER"] = config.embedding.provider
    if config.embedding.model_id:
        env_defaults["EMBEDDING_MODEL_ID"] = config.embedding.model_id
    if config.embedding.api_base:
        env_defaults["EMBEDDING_API_BASE"] = config.embedding.api_base
    if config.embedding.dimensions is not None:
        env_defaults["EMBEDDING_DIMENSIONS"] = str(config.embedding.dimensions)
    if config.embedding.timeout_seconds is not None:
        env_defaults["EMBEDDING_TIMEOUT_SECONDS"] = str(
            config.embedding.timeout_seconds
        )
    if config.embedding.organization:
        env_defaults["EMBEDDING_ORGANIZATION"] = config.embedding.organization
    activity_base_url = resolve_activity_collector_runtime_base_url(config)
    if activity_base_url:
        env_defaults["ACTIVITY_COLLECTOR_BASE_URL"] = activity_base_url
    broker_credentials_dir = resolve_broker_credentials_dir(
        (env or {}).get("SECURE_TOOL_BROKER_CREDENTIALS_DIR")
        or config.security_layer.broker_credentials_dir,
        instance_root=config.instance_root_path,
    )
    env_defaults["SECURE_TOOL_BROKER_CREDENTIALS_DIR"] = str(broker_credentials_dir)
    state_backend = config.security_layer.credential_state_backend
    env_defaults["SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE"] = (
        state_backend.mode
    )
    if state_backend.source:
        env_defaults["SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE"] = (
            state_backend.source
        )
    env_defaults["SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_NAMESPACE"] = (
        state_backend.namespace
    )
    env_defaults["SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES"] = str(
        state_backend.fallback_to_files
    ).lower()
    if config.security_layer.sealbox.enabled:
        sealbox_runtime = resolve_sealbox_runtime_config(
            config.security_layer.sealbox,
            broker_credentials_dir=broker_credentials_dir,
            env=env,
        )
        apply_sealbox_cli_env(sealbox_runtime, env=env_defaults)
    return env_defaults


def load_env_file_values(env_path: Path | None) -> dict[str, str]:
    if env_path is None:
        return {}
    if dotenv_values is None:
        raise RuntimeError(
            "python-dotenv is required to load env files for container startup."
        )
    raw_values = dotenv_values(env_path)
    values: dict[str, str] = {}
    for key, value in raw_values.items():
        if value is None:
            continue
        values[str(key)] = str(value)
    materialize_secret_file_envs(values)
    return values


def use_process_timezone(env: Mapping[str, str]) -> None:
    """Run this supervisor in the time zone its children get (``TZ``), so its log times match theirs."""

    zone = str(env.get("TZ") or "").strip()
    if not zone:
        return
    os.environ["TZ"] = zone
    time.tzset()
    LOGGER.info("Processes run in the time zone %s. event=process_timezone timezone=%s", zone, zone)


def build_process_env(
    config: GatewayInstanceConfig,
    *,
    env_path: Path | None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env_file_values = load_env_file_values(env_path)
    ambient_env = os.environ if base_env is None else base_env
    runtime_env = dict(env_file_values)
    runtime_env.update(dict(ambient_env))
    reject_removed_public_url_env_vars(runtime_env)
    reject_removed_file_access_env_vars(runtime_env)
    reject_removed_sealbox_env_vars(runtime_env)
    reject_removed_credential_catalog_env_vars(runtime_env)
    reject_removed_tool_credential_env_vars(runtime_env)
    reject_removed_secure_cli_env_var(runtime_env)
    reject_removed_web_service_env_vars(runtime_env)
    materialize_secret_file_envs(runtime_env)
    process_env = dict(build_instance_env_defaults(config, env=runtime_env))
    process_env.update(env_file_values)
    process_env.update(dict(ambient_env))
    materialize_secret_file_envs(process_env)
    # This convenience value must reflect the final env precedence, not the instance defaults.
    explicit_website_base_url = str(
        runtime_env.get("INTERNAL_WEBSITE_BASE_URL") or ""
    ).strip()
    if explicit_website_base_url:
        process_env["INTERNAL_WEBSITE_BASE_URL"] = explicit_website_base_url.rstrip("/")
    else:
        raw_public_port = str(
            process_env.get("INTERNAL_WEBSITE_PUBLIC_PORT") or ""
        ).strip()
        public_port = int(raw_public_port) if raw_public_port else None
        process_env["INTERNAL_WEBSITE_BASE_URL"] = compose_public_base_url(
            str(
                process_env.get("NETWORK_PUBLIC_ORIGIN") or config.network.public_origin
            ),
            public_port,
        )
    broker_credentials_dir = resolve_broker_credentials_dir(
        process_env.get("SECURE_TOOL_BROKER_CREDENTIALS_DIR")
        or config.security_layer.broker_credentials_dir,
        instance_root=config.instance_root_path,
    )
    process_env["SECURE_TOOL_BROKER_CREDENTIALS_DIR"] = str(broker_credentials_dir)
    process_env.setdefault("PYTHONUNBUFFERED", "1")
    return process_env


def with_website_base_url(env: Mapping[str, str], *, serves_docs: bool) -> dict[str, str]:
    """The team's own processes see INTERNAL_WEBSITE_BASE_URL only when the website serves docs."""

    result = dict(env)
    website_base_url = str(env.get("INTERNAL_WEBSITE_BASE_URL") or "").strip()
    if serves_docs and website_base_url:
        result["INTERNAL_WEBSITE_BASE_URL"] = website_base_url.rstrip("/")
    elif not serves_docs:
        result.pop("INTERNAL_WEBSITE_BASE_URL", None)
    return result


CONSUMER_CHECK_TIMEOUT_SECONDS = 30


class ConsumerCheckNotRunReason(StrEnum):
    """Why the consumer check gave no result (``event=nats_consumer_preflight_not_run``)."""

    PROGRAM_MISSING = "program_missing"
    START_FAILED = "start_failed"
    TIMED_OUT = "timed_out"
    EXITED_WITH_ERROR = "exited_with_error"


def run_nats_consumer_preflight(
    *,
    config_path: Path,
    env: Mapping[str, str],
) -> None:
    """Run the read-only consumer check before any agent creates a consumer.

    The check (``scripts/reconcile_nats_consumers.py``) exits 0 whenever it
    finishes its scan. What it finds, such as consumer usage near the account's
    limit, it logs itself, and start-up continues. Any other result means the
    check could not run: that is logged once at error level with its reason,
    separately from the check's own findings. Start-up continues then too,
    because the check only reports; it never changes or protects anything.

    Images keep only compiled files, so the program is found through
    ``resolve_python_entrypoint`` like every other program the launcher starts.
    The check needs only the NATS and storage credentials
    (docker/secret_env_vars.yaml, process ``consumer_check``).
    """

    try:
        program = resolve_python_entrypoint(
            REPO_ROOT / "scripts" / "reconcile_nats_consumers.py"
        )
    except FileNotFoundError as exc:
        _log_consumer_check_not_run(ConsumerCheckNotRunReason.PROGRAM_MISSING, str(exc))
        return
    try:
        result = subprocess.run(
            [sys.executable, str(program), "--config", str(config_path)],
            cwd=REPO_ROOT,
            env=environment_for(PROCESS_CONSUMER_CHECK, env),
            check=False,
            timeout=CONSUMER_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        _log_consumer_check_not_run(
            ConsumerCheckNotRunReason.TIMED_OUT,
            f"no result after {CONSUMER_CHECK_TIMEOUT_SECONDS} seconds",
        )
        return
    except OSError as exc:
        _log_consumer_check_not_run(ConsumerCheckNotRunReason.START_FAILED, str(exc))
        return
    if result.returncode != 0:
        _log_consumer_check_not_run(
            ConsumerCheckNotRunReason.EXITED_WITH_ERROR,
            f"the check stopped with status {result.returncode}; its log above gives the cause",
        )


def _log_consumer_check_not_run(reason: ConsumerCheckNotRunReason, detail: str) -> None:
    LOGGER.error(
        "The JetStream consumer check could not run; agents start without it. "
        "event=nats_consumer_preflight_not_run reason=%s detail=%s",
        reason,
        detail,
    )


BROKER_TOKEN_ENV = "SECURE_TOOL_BROKER_TOKEN"
TASK_CONTEXT_SIGNING_KEYS = frozenset({ENV_GATEWAY_SIGNING_KEY, ENV_SCHEDULER_SIGNING_KEY})


def scoped_environment(
    process: str,
    env: Mapping[str, str],
    *,
    renamed: Mapping[str, str] | None = None,
    broker_root: str | None = None,
    caller_label: str | None = None,
) -> dict[str, str]:
    """One process's environment: only the listed variables it uses, plus its broker token.

    docker/secret_env_vars.yaml decides which secrets each process
    receives (``env_secrets.environment_for``). Only the broker holds the
    broker's root token; a process that calls the broker gets a token
    derived for its label (``access_control.caller_tokens``), and one that
    never calls it (``caller_label=None``) gets none.
    """

    scoped = environment_for(process, env, renamed=renamed)
    if broker_root and caller_label:
        scoped[BROKER_TOKEN_ENV] = derive_caller_token(broker_root, caller_label)
    withheld = withheld_names(process, env, renamed=renamed)
    LOGGER.info(
        "Scoped a process environment. event=process_environment_scoped process=%s withheld=%s",
        process,
        ",".join(sorted(withheld)) or "-",
    )
    return scoped


def missing_task_context_keys(env: Mapping[str, str]) -> list[str]:
    """Private signing keys the env lacks (after Docker secret files are read).

    The public verification list is derived from them (``with_verify_keys``),
    so only the two private keys are required.
    """

    return [
        name
        for name in (ENV_GATEWAY_SIGNING_KEY, ENV_SCHEDULER_SIGNING_KEY)
        if not str(env.get(name) or "").strip()
    ]


def with_verify_keys(env: Mapping[str, str]) -> dict[str, str]:
    """The env with ACCESS_CONTEXT_VERIFY_KEYS derived from the private keys.

    Verification keys are by definition the public halves of the two signing
    keys (``internal_credentials`` writes them the same way), so the
    supervisor derives them for every child. A different value in team.env or
    a secret file is replaced and logged.
    """

    derived = verify_keys_value(
        [
            public_entry(ISSUER_GATEWAY, str(env[ENV_GATEWAY_SIGNING_KEY])),
            public_entry(ISSUER_SCHEDULER, str(env[ENV_SCHEDULER_SIGNING_KEY])),
        ]
    )
    configured = str(env.get(ENV_VERIFY_KEYS) or "").strip()
    if configured and configured != derived:
        LOGGER.warning(
            "ACCESS_CONTEXT_VERIFY_KEYS does not match the signing keys; using the keys derived from them. "
            "event=task_context_verify_keys_mismatch"
        )
    result = {key: value for key, value in env.items() if key != f"{ENV_VERIFY_KEYS}_FILE"}
    result[ENV_VERIFY_KEYS] = derived
    return result


def task_context_keys_message(
    config: GatewayInstanceConfig, env_path: Path | None, missing: list[str]
) -> str:
    """Without these keys no task says who it works for, so the broker would
    refuse every agent tool call; starting would only hide that."""

    reason = (
        f"No {', '.join(missing)} is set (reason_code=task_context_keys_missing). The gateway "
        "and the scheduler sign who each task works for with these keys, and the broker refuses "
        "agent tool calls without them."
    )
    if config.launch.secrets_from_environment:
        # No env file is read in this mode, so the fix is a Docker secret.
        files = " and ".join(f"{name}_FILE" for name in missing)
        return (
            f"{reason} This instance's secrets come from the environment "
            "(launch.secrets_source: environment). Add each missing key as a Docker secret and set "
            f"{files} to the mounted file, then start again. Print new values with:\n"
            f"python3 -m scripts.generate_internal_credentials --instance-name "
            f"{shlex.quote(config.instance_name)} --print\n"
            f"The docs ({ENVIRONMENT_SECRETS_DOCS}) also show a one-line command for each key."
        )
    env_file = shlex.quote(str(env_path)) if env_path else "<path to team.env>"
    return (
        f"{reason} Add the missing keys to team.env (existing values are "
        "kept), then start again:\n"
        f"python3 -m scripts.generate_internal_credentials --instance-name "
        f"{shlex.quote(config.instance_name)} --env-file {env_file} --write\n"
        "To use Docker secrets instead, set ACCESS_GATEWAY_SIGNING_KEY_FILE and "
        "ACCESS_SCHEDULER_SIGNING_KEY_FILE to files holding the values that "
        "`python3 -m scripts.generate_internal_credentials --instance-name <name> --print` shows."
    )


def broker_token_missing(env: Mapping[str, str]) -> bool:
    """The env holds no broker root token; the broker always starts and needs it.

    ``env`` has Docker secret files (``*_FILE``) read in already.
    """

    return not str(env.get(BROKER_TOKEN_ENV) or "").strip()


def broker_token_message(config: GatewayInstanceConfig, env_path: Path | None) -> str:
    """Without the token the broker refuses to start and no process gets a
    token for calling it, so every agent tool call through it would fail."""

    reason = (
        f"No {BROKER_TOKEN_ENV} is set (reason_code=broker_token_missing). The secure tool broker "
        "always starts, because every credentialed tool call goes through it, and it does not "
        "start without this token."
    )
    instance = shlex.quote(config.instance_name)
    if config.launch.secrets_from_environment:
        return (
            f"{reason} This instance's secrets come from the environment "
            "(launch.secrets_source: environment). Add the token as a Docker secret and set "
            f"{BROKER_TOKEN_ENV}_FILE to the mounted file, then start again. Print a new value with:\n"
            f"python3 -m scripts.generate_internal_credentials --instance-name {instance} --print"
        )
    env_file = shlex.quote(str(env_path)) if env_path else "<path to team.env>"
    return (
        f"{reason} Add it to team.env (existing values are kept), then start again:\n"
        f"python3 -m scripts.generate_internal_credentials --instance-name {instance} "
        f"--env-file {env_file} --write"
    )


def missing_secret_refusal(
    config: GatewayInstanceConfig, env_path: Path | None, env: Mapping[str, str]
) -> str | None:
    """Why the launch must stop for a secret it cannot run without, or None.

    Checked before anything starts, so the person sees one plain message
    instead of a service failing later.
    """

    missing_keys = missing_task_context_keys(env)
    if missing_keys:
        return task_context_keys_message(config, env_path, missing_keys)
    if broker_token_missing(env):
        return broker_token_message(config, env_path)
    return None


def build_service_process_environments(
    *,
    runtime_env: Mapping[str, str],
    inherited_env: Mapping[str, str],
    manifest_paths_by_service: Mapping[str, Path],
    renamed: Mapping[str, str] | None = None,
) -> ServiceProcessEnvironments:
    """Give each replaceable runtime process its scoped environment and its own manifest.

    ``runtime_env`` is what the team's own processes start from (it carries
    the website address the manager and gateway should use); the broker
    starts from ``inherited_env``.
    """

    root = str(inherited_env.get(BROKER_TOKEN_ENV) or "").strip() or None
    environments = {
        PROCESS_GATEWAY: scoped_environment(
            PROCESS_GATEWAY, runtime_env, renamed=renamed, broker_root=root, caller_label=CALLER_GATEWAY
        ),
        # These services never call the broker, so they hold no broker token.
        PROCESS_ACTIVITY_COLLECTOR: scoped_environment(PROCESS_ACTIVITY_COLLECTOR, runtime_env, renamed=renamed),
        PROCESS_EXECUTION_CONTROLLER: scoped_environment(PROCESS_EXECUTION_CONTROLLER, runtime_env, renamed=renamed),
        PROCESS_ADAPTER_HOST: scoped_environment(PROCESS_ADAPTER_HOST, runtime_env, renamed=renamed),
        PROCESS_SECURE_BROKER: scoped_environment(PROCESS_SECURE_BROKER, inherited_env, renamed=renamed),
    }
    for service, environment in environments.items():
        environment["UA_RUNTIME_MANIFEST"] = str(manifest_paths_by_service[service])
    return ServiceProcessEnvironments(
        gateway=environments[PROCESS_GATEWAY],
        activity_collector=environments[PROCESS_ACTIVITY_COLLECTOR],
        secure_broker=environments[PROCESS_SECURE_BROKER],
        execution_controller=environments[PROCESS_EXECUTION_CONTROLLER],
        adapter_host=environments[PROCESS_ADAPTER_HOST],
    )


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = str(env.get(key, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = str(env.get(key, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("Ignoring invalid integer %s=%r; using %s.", key, raw, default)
        return default


def _instance_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-._")
    return slug or "instance"


def should_manage_openshell_sandbox(config: GatewayInstanceConfig) -> bool:
    return config.security_layer.mode == "openshell"


def should_manage_openshell_gateway(
    config: GatewayInstanceConfig, env: Mapping[str, str]
) -> bool:
    if config.security_layer.mode != "openshell":
        return False
    return _env_bool(
        env, "OPENSHELL_MANAGE_GATEWAY", config.security_layer.openshell.manage_gateway
    )


def resolve_openshell_sandbox_name(
    config: GatewayInstanceConfig, env: Mapping[str, str]
) -> str:
    configured = (
        env.get("OPENSHELL_SANDBOX_NAME")
        or config.security_layer.openshell.sandbox_name
        or f"team-{_instance_slug(config.instance_name)}"
    )
    return str(configured).strip()


def resolve_openshell_gateway_name(
    config: GatewayInstanceConfig, env: Mapping[str, str]
) -> str:
    configured = (
        env.get("OPENSHELL_GATEWAY")
        or config.security_layer.openshell.gateway_name
        or f"team-{_instance_slug(config.instance_name)}"
    )
    return str(configured).strip()


def resolve_openshell_gateway_endpoint(
    config: GatewayInstanceConfig, env: Mapping[str, str]
) -> str:
    settings = config.security_layer.openshell
    configured = env.get("OPENSHELL_GATEWAY_ENDPOINT") or settings.gateway_endpoint
    if configured:
        return str(configured).strip()
    host = (
        str(
            env.get("OPENSHELL_GATEWAY_BIND_HOST") or settings.gateway_bind_host
        ).strip()
        or "127.0.0.1"
    )
    port = _env_int(env, "OPENSHELL_GATEWAY_BIND_PORT", settings.gateway_bind_port)
    endpoint_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{endpoint_host}:{port}"


def _openshell_command(env: Mapping[str, str]) -> str:
    configured = str(env.get("OPENSHELL_BIN") or "").strip()
    if configured:
        return configured
    resolved = shutil.which("openshell", path=env.get("PATH"))
    if not resolved:
        raise RuntimeError(
            "security_layer.mode=openshell requires the openshell CLI on PATH."
        )
    return resolved


def _run_openshell(
    args: list[str],
    *,
    env: Mapping[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_openshell_command(env), *args],
        cwd=REPO_ROOT,
        env=environment_for(PROCESS_OPENSHELL_CLI, env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )


def _openshell_gateway_command(env: Mapping[str, str]) -> str:
    configured = str(env.get("OPENSHELL_GATEWAY_BIN") or "").strip()
    if configured:
        return configured
    resolved = shutil.which("openshell-gateway", path=env.get("PATH"))
    if not resolved:
        raise RuntimeError(
            "security_layer.mode=openshell with manage_gateway=true requires openshell-gateway on PATH. "
            "Build the image with INSTALL_OPENSHELL=true or set OPENSHELL_MANAGE_GATEWAY=false for an external gateway."
        )
    return resolved


def _run_openshell_best_effort(
    args: list[str], *, env: Mapping[str, str], timeout_seconds: int
) -> subprocess.CompletedProcess[str] | None:
    try:
        return _run_openshell(args, env=env, timeout_seconds=timeout_seconds)
    except Exception as exc:
        LOGGER.debug(
            "Best-effort OpenShell command failed: openshell %s: %s",
            " ".join(args),
            exc,
        )
        return None


def _ensure_instance_scoped_openshell_env(
    config: GatewayInstanceConfig, env: dict[str, str]
) -> Path:
    base_dir = config.instance_root_path / "runtime" / "openshell"
    config_home = (
        Path(env.get("XDG_CONFIG_HOME") or base_dir / "config").expanduser().resolve()
    )
    state_home = (
        Path(env.get("XDG_STATE_HOME") or base_dir / "state").expanduser().resolve()
    )
    data_home = (
        Path(env.get("XDG_DATA_HOME") or base_dir / "data").expanduser().resolve()
    )
    for path in (config_home, state_home, data_home):
        path.mkdir(parents=True, exist_ok=True)
    env.setdefault("XDG_CONFIG_HOME", str(config_home))
    env.setdefault("XDG_STATE_HOME", str(state_home))
    env.setdefault("XDG_DATA_HOME", str(data_home))
    return state_home


def _managed_openshell_secret(
    config: GatewayInstanceConfig, env: Mapping[str, str], key: str, filename: str
) -> str:
    existing = str(env.get(key) or "").strip()
    if existing:
        return existing
    secret_file = config.instance_root_path / "runtime" / "openshell" / filename
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()
    value = secrets.token_hex(32)
    secret_file.write_text(f"{value}\n", encoding="utf-8")
    try:
        secret_file.chmod(0o600)
    except OSError:
        LOGGER.debug("Could not chmod OpenShell secret file %s.", secret_file)
    return value


def build_openshell_gateway_command(
    config: GatewayInstanceConfig, *, env: Mapping[str, str]
) -> list[str]:
    settings = config.security_layer.openshell
    state_home = (
        Path(
            env.get("XDG_STATE_HOME")
            or config.instance_root_path / "runtime" / "openshell" / "state"
        )
        .expanduser()
        .resolve()
    )
    gateway_state_dir = (
        Path(
            env.get("OPENSHELL_GATEWAY_STATE_DIR")
            or settings.gateway_state_dir
            or state_home / "openshell"
        )
        .expanduser()
        .resolve()
    )
    gateway_state_dir.mkdir(parents=True, exist_ok=True)
    db_url = (
        env.get("OPENSHELL_DB_URL")
        or settings.gateway_db_url
        or f"sqlite:{gateway_state_dir / 'gateway.db'}?mode=rwc"
    )
    bind_host = (
        str(
            env.get("OPENSHELL_GATEWAY_BIND_HOST") or settings.gateway_bind_host
        ).strip()
        or "127.0.0.1"
    )
    bind_port = _env_int(env, "OPENSHELL_GATEWAY_BIND_PORT", settings.gateway_bind_port)
    driver = str(
        env.get("OPENSHELL_DRIVERS") or settings.gateway_driver or "docker"
    ).strip()
    command = [
        _openshell_gateway_command(env),
        "--bind-address",
        bind_host,
        "--port",
        str(bind_port),
        "--drivers",
        driver,
        "--db-url",
        db_url,
        "--disable-tls",
    ]
    sandbox_image = env.get("OPENSHELL_SANDBOX_IMAGE") or settings.sandbox_image
    if sandbox_image:
        command.extend(["--sandbox-image", sandbox_image])
    extra_args = list(settings.gateway_extra_args)
    env_extra_args = str(env.get("OPENSHELL_GATEWAY_EXTRA_ARGS") or "").strip()
    if env_extra_args:
        extra_args.extend(shlex.split(env_extra_args))
    command.extend(extra_args)
    return command


def build_openshell_gateway_env(
    config: GatewayInstanceConfig, *, env: Mapping[str, str]
) -> dict[str, str]:
    """The managed openshell-gateway's environment, which carries its handshake secret.

    The secret goes in OPENSHELL_SSH_HANDSHAKE_SECRET, which the gateway
    reads the same as its --ssh-handshake-secret flag, because every process
    can read another process's command line. It is never logged.
    """

    gateway_env = environment_for(PROCESS_OPENSHELL_GATEWAY, env)
    gateway_env[OPENSHELL_HANDSHAKE_SECRET_ENV] = _managed_openshell_secret(
        config, env, OPENSHELL_HANDSHAKE_SECRET_ENV, OPENSHELL_HANDSHAKE_SECRET_FILENAME
    )
    return gateway_env


def register_openshell_gateway(
    name: str, endpoint: str, *, env: Mapping[str, str]
) -> None:
    _run_openshell_best_effort(["gateway", "remove", name], env=env, timeout_seconds=30)
    completed = _run_openshell(
        ["gateway", "add", endpoint, "--local", "--name", name],
        env=env,
        timeout_seconds=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Failed to register OpenShell gateway {name!r}: {(completed.stdout or '').strip()}"
        )


def wait_for_openshell_gateway_ready(
    gateway: ManagedOpenShellGateway,
    *,
    env: Mapping[str, str],
    timeout_seconds: int,
) -> None:
    deadline = time.time() + max(1, timeout_seconds)
    last_output = ""
    while time.time() < deadline:
        if gateway.process.poll() is not None:
            raise RuntimeError(
                f"OpenShell gateway {gateway.name!r} exited before becoming ready with code {gateway.process.returncode}."
            )
        completed = _run_openshell(["status"], env=env, timeout_seconds=10)
        if completed.returncode == 0:
            return
        last_output = completed.stdout or ""
        time.sleep(1)
    raise RuntimeError(
        f"OpenShell gateway {gateway.name!r} did not become ready: {last_output.strip()}"
    )


def ensure_openshell_gateway(
    config: GatewayInstanceConfig,
    *,
    env: dict[str, str],
) -> ManagedOpenShellGateway | None:
    if config.security_layer.mode != "openshell":
        return None

    _ensure_instance_scoped_openshell_env(config, env)
    name = resolve_openshell_gateway_name(config, env)
    endpoint = resolve_openshell_gateway_endpoint(config, env)
    if not name:
        raise RuntimeError("OpenShell gateway name resolved to an empty value.")
    env["OPENSHELL_GATEWAY"] = name
    env["OPENSHELL_GATEWAY_ENDPOINT"] = endpoint

    if not should_manage_openshell_gateway(config, env):
        LOGGER.info(
            "Using externally managed OpenShell gateway %s at %s.", name, endpoint
        )
        return None

    command = build_openshell_gateway_command(config, env=env)
    LOGGER.info(
        "Starting OpenShell gateway %s at %s: %s", name, endpoint, " ".join(command)
    )
    process = subprocess.Popen(
        command, cwd=REPO_ROOT, env=build_openshell_gateway_env(config, env=env)
    )
    gateway = ManagedOpenShellGateway(name=name, endpoint=endpoint, process=process)
    register_openshell_gateway(name, endpoint, env=env)
    wait_for_openshell_gateway_ready(
        gateway,
        env=env,
        timeout_seconds=_env_int(
            env,
            "OPENSHELL_GATEWAY_READY_TIMEOUT_SECONDS",
            config.security_layer.openshell.gateway_ready_timeout_seconds,
        ),
    )
    return gateway


def _sealbox_server_executable() -> str | None:
    configured = str(os.getenv("SEALBOX_SERVER_PATH") or "").strip()
    if configured:
        return configured
    return shutil.which("sealbox-server")


def should_start_managed_sealbox(
    config: GatewayInstanceConfig, *, env: Mapping[str, str] | None = None
) -> bool:
    broker_credentials_dir = resolve_broker_credentials_dir(
        (env or {}).get("SECURE_TOOL_BROKER_CREDENTIALS_DIR")
        or config.security_layer.broker_credentials_dir,
        instance_root=config.instance_root_path,
    )
    runtime = resolve_sealbox_runtime_config(
        config.security_layer.sealbox,
        broker_credentials_dir=broker_credentials_dir,
        env=env,
    )
    return runtime.enabled and runtime.manage_service


def wait_for_sealbox_tcp(
    runtime: SealboxRuntimeConfig,
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: int,
) -> None:
    deadline = time.time() + max(1, timeout_seconds)
    last_error = ""
    while time.time() < deadline:
        code = process.poll()
        if code is not None:
            raise RuntimeError(
                f"sealbox-server exited before readiness with code {code}."
            )
        try:
            with socket.create_connection(
                (runtime.bind_host, runtime.bind_port), timeout=0.25
            ):
                return
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.2)
    raise RuntimeError(
        f"sealbox-server did not open {runtime.listen_addr}: {last_error}"
    )


def ensure_managed_sealbox_service(
    config: GatewayInstanceConfig, *, env: dict[str, str]
) -> ManagedSealboxService | None:
    if not should_start_managed_sealbox(config, env=env):
        return None
    broker_credentials_dir = resolve_broker_credentials_dir(
        env.get("SECURE_TOOL_BROKER_CREDENTIALS_DIR")
        or config.security_layer.broker_credentials_dir,
        instance_root=config.instance_root_path,
    )
    runtime = resolve_sealbox_runtime_config(
        config.security_layer.sealbox,
        broker_credentials_dir=broker_credentials_dir,
        env=env,
    )
    apply_sealbox_cli_env(runtime, env=env)
    if runtime.auto_initialize:
        ensure_sealbox_token_file(runtime.root_token_file)
    elif not runtime.root_token_file.is_file():
        raise RuntimeError(
            "Sealbox root token file is missing and auto_initialize is false."
        )
    runtime.store_path.parent.mkdir(parents=True, exist_ok=True)
    executable = _sealbox_server_executable()
    if not executable:
        raise RuntimeError(
            "security_layer.sealbox.manage_service is true, but sealbox-server is not on PATH."
        )
    server_env = sealbox_server_env(runtime, env=environment_for(PROCESS_SEALBOX, env))
    LOGGER.info("Starting managed Sealbox service at %s: %s", runtime.url, executable)
    process = subprocess.Popen([executable], cwd=REPO_ROOT, env=server_env)
    try:
        wait_for_sealbox_tcp(
            runtime, process, timeout_seconds=runtime.ready_timeout_seconds
        )
    except Exception:
        terminate_process(process, "Sealbox service")
        raise
    return ManagedSealboxService(runtime=runtime, process=process)


def ensure_openshell_sandbox(
    config: GatewayInstanceConfig,
    *,
    env: dict[str, str],
) -> ManagedOpenShellSandbox | None:
    """Pass the settings for per-person OpenShell sandboxes to the agent processes.

    No sandbox is created here: each person gets their own sandbox, holding
    copies of only their folder and the shared team/ and agents/ folders,
    created the first time their code runs (custom_tools.openshell_sandboxes;
    decision D6). The configured sandbox name is the prefix of those names.
    """

    if not should_manage_openshell_sandbox(config):
        return None
    settings = config.security_layer.openshell
    prefix = resolve_openshell_sandbox_name(config, env)
    if not prefix:
        raise RuntimeError("OpenShell sandbox name resolved to an empty value.")
    env[openshell_sandboxes.ENV_SANDBOX_NAME] = prefix
    env[openshell_sandboxes.ENV_WORKSPACE_MOUNT] = str(
        env.get(openshell_sandboxes.ENV_WORKSPACE_MOUNT) or settings.workspace_mount
    )
    env[openshell_sandboxes.ENV_INSTANCE_LABEL] = _instance_slug(config.instance_name)
    for key, value in (
        (openshell_sandboxes.ENV_SANDBOX_IMAGE, settings.sandbox_image),
        (openshell_sandboxes.ENV_POLICY_FILE, settings.policy_file),
    ):
        if value and not env.get(key):
            env[key] = str(value)
    for key, default in (
        (openshell_sandboxes.ENV_UPLOAD_WORKSPACE, settings.upload_workspace),
        (openshell_sandboxes.ENV_NO_GIT_IGNORE, settings.no_git_ignore),
        (openshell_sandboxes.ENV_CREATE_IF_MISSING, settings.create_if_missing),
    ):
        env[key] = str(_env_bool(env, key, default)).lower()
    for key, default in (
        (openshell_sandboxes.ENV_PROVISION_TIMEOUT, settings.provision_timeout_seconds),
        (openshell_sandboxes.ENV_READINESS_TIMEOUT, settings.readiness_timeout_seconds),
    ):
        env[key] = str(_env_int(env, key, default))
    LOGGER.info(
        "OpenShell sandboxes are created per person on first use. event=openshell_per_person prefix=%s",
        prefix,
    )
    return ManagedOpenShellSandbox(
        prefix=prefix,
        delete_on_shutdown=_env_bool(env, "OPENSHELL_DELETE_ON_SHUTDOWN", settings.delete_on_shutdown),
    )


def cleanup_openshell_sandbox(
    sandbox: ManagedOpenShellSandbox | None, *, env: Mapping[str, str]
) -> None:
    """Delete this instance's per-person sandboxes when configured to."""

    if sandbox is None or not sandbox.delete_on_shutdown:
        return
    try:
        names = openshell_sandboxes.instance_sandboxes(
            _openshell_command(env), environment_for(PROCESS_OPENSHELL_CLI, env)
        )
    except Exception as exc:  # noqa: BLE001 - shutdown continues; the sandboxes stay listed
        LOGGER.warning(
            "Could not list OpenShell sandboxes to delete them. event=openshell_cleanup_failed prefix=%s error=%s",
            sandbox.prefix,
            exc,
        )
        return
    for name in names:
        LOGGER.info("Deleting OpenShell sandbox %s.", name)
        try:
            completed = _run_openshell(["sandbox", "delete", name], env=env, timeout_seconds=60)
        except Exception as exc:  # noqa: BLE001 - try the others
            LOGGER.warning("Failed to delete OpenShell sandbox %s: %s", name, exc)
            continue
        if completed.returncode != 0:
            LOGGER.warning(
                "OpenShell sandbox delete failed for %s: %s",
                name,
                (completed.stdout or "").strip(),
            )


def validate_memory_graph_embedding_preflight(
    config: GatewayInstanceConfig,
    *,
    env: Mapping[str, str],
    config_path: Path,
) -> None:
    if not bool(config.agent.memory_graph_enabled):
        return
    storage_settings = config.resolve_manager_storage_settings(env=env)
    repository = create_memory_graph_repository(storage_settings, logger=LOGGER)
    dimensions_raw = env.get("EMBEDDING_DIMENSIONS")
    dimensions = config.embedding.dimensions
    if dimensions_raw not in (None, ""):
        try:
            dimensions = int(dimensions_raw)
        except (TypeError, ValueError):
            dimensions = config.embedding.dimensions
    spec = build_embedding_profile_spec(
        {
            "provider": env.get(
                "EMBEDDING_PROVIDER",
                config.embedding.provider or EMBEDDING_DEFAULTS.provider.value,
            ),
            "model_id": env.get("EMBEDDING_MODEL_ID", config.embedding.model_id),
            "api_base": env.get("EMBEDDING_API_BASE", config.embedding.api_base),
            "dimensions": dimensions,
        }
    )
    ensure_memory_graph_embedding_profile(
        repository=repository,
        profile_spec=spec,
        logger=LOGGER,
        config_path=str(config_path),
    )


def build_agent_command(
    config: GatewayInstanceConfig,
    *,
    agent_type: str,
    is_lead: bool,
    python_executable: str | None = None,
) -> list[str]:
    command = [
        python_executable or sys.executable,
        "-u",
        str(resolve_python_entrypoint(REPO_ROOT / "strands_main.py")),
        "--agent-type",
        agent_type,
        "--team-id",
        config.manager.team_id,
        "--config",
        str(config.config_path),
    ]
    if is_lead:
        command.append("--is-lead")
    if config.state.storage.backend == STORAGE_DEFAULTS.backend.value:
        command.extend(
            [
                "--task-history-db-path",
                str(resolve_manager_db_path(config)),
            ]
        )
    return command


def build_manager_command(
    config: GatewayInstanceConfig,
    *,
    python_executable: str | None = None,
) -> list[str]:
    return build_agent_command(
        config,
        agent_type=config.manager.named_agent_type,
        is_lead=True,
        python_executable=python_executable,
    )


def resolve_team_composition_startup_agents(
    config: GatewayInstanceConfig,
) -> list[StartupAgentSettings]:
    base_agents = ensure_required_startup_agents(config.gateway.startup_agents)
    try:
        composition_path = config.layout.team_composition_path
        composition = load_team_composition(composition_path)
    except Exception as exc:
        LOGGER.warning(
            "Failed to load team composition file for startup overrides; using config.yaml startup_agents. "
            "event=team_composition_startup_load_failed team_composition_path=%s error=%s",
            config.layout.team_composition_path,
            exc,
        )
        return base_agents

    composition_agents = [
        StartupAgentSettings(
            named_agent_type=agent.named_agent_type,
            count=agent.count,
            enabled=agent.enabled,
        )
        for agent in composition.startup_agents.agents
    ]
    if not composition_agents:
        return base_agents

    if composition.startup_agents.mode == "replace":
        return ensure_required_startup_agents(composition_agents)

    merged: dict[str, StartupAgentSettings] = {}
    for agent in composition_agents:
        merged[agent.named_agent_type] = agent
    for agent in base_agents:
        if agent.named_agent_type not in merged:
            merged[agent.named_agent_type] = agent
    return ensure_required_startup_agents(list(merged.values()))


def startup_agent_type(label: str) -> str:
    """The agent type of a startup agent label (``Type`` or ``Type#<n>``)."""

    return label.split("#", 1)[0]


def build_startup_agent_environments(
    startup_agent_commands: tuple[tuple[str, list[str]], ...],
    *,
    base_env: Mapping[str, str],
    broker_root: str | None,
    renamed: Mapping[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """Each startup agent process gets the agent secrets and may call the broker only as its own agent type.

    The shared list keeps the scheduler signing key from startup agents: only
    the manager process holds it. A startup agent that claims one of its
    scheduled runs gives the run the schedule's manager-signed permit
    (``custom_agents.schedule_permits``); only when the schedule has none does
    it ask the manager to sign the run, showing its claim, and it does not
    start the run without one or the other
    (``custom_agents.scheduled_run_contexts``).
    """

    return {
        label: scoped_environment(
            PROCESS_AGENT,
            base_env,
            renamed=renamed,
            broker_root=broker_root,
            caller_label=agent_caller_label(startup_agent_type(label)),
        )
        for label, _command in startup_agent_commands
    }


def build_startup_agent_commands(
    config: GatewayInstanceConfig,
    *,
    python_executable: str | None = None,
) -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    for startup_agent in resolve_team_composition_startup_agents(config):
        if not startup_agent.enabled:
            continue
        agent_type = startup_agent.named_agent_type
        if agent_type == config.manager.named_agent_type:
            LOGGER.warning(
                "Ignoring startup agent %s because it is the configured team lead.",
                agent_type,
            )
            continue
        for index in range(1, int(startup_agent.count) + 1):
            label = f"{agent_type}#{index}" if startup_agent.count > 1 else agent_type
            commands.append(
                (
                    label,
                    build_agent_command(
                        config,
                        agent_type=agent_type,
                        is_lead=False,
                        python_executable=python_executable,
                    ),
                )
            )
    return commands


def build_gateway_command(
    config_path: Path,
    gateway_args: list[str],
    *,
    python_executable: str | None = None,
) -> list[str]:
    command = [
        python_executable or sys.executable,
        "-u",
        str(resolve_python_entrypoint(REPO_ROOT / "assistant_cli.py")),
        "run-gateway",
        "--config",
        str(config_path),
    ]
    command.extend(gateway_args)
    return command


def build_activity_collector_command(
    config_path: Path,
    *,
    python_executable: str | None = None,
) -> list[str]:
    return [
        python_executable or sys.executable,
        "-u",
        str(resolve_python_entrypoint(REPO_ROOT / "assistant_cli.py")),
        "run-activity-collector",
        "--config",
        str(config_path),
    ]


def build_adapter_host_command(
    config_path: Path,
    *,
    python_executable: str | None = None,
) -> list[str]:
    return [
        python_executable or sys.executable,
        "-u",
        str(resolve_python_entrypoint(REPO_ROOT / "assistant_cli.py")),
        "run-adapter-host",
        "--config",
        str(config_path),
    ]


def build_execution_controller_command(
    config_path: Path,
    *,
    python_executable: str | None = None,
) -> list[str]:
    return [
        python_executable or sys.executable,
        "-u",
        str(resolve_python_entrypoint(REPO_ROOT / "assistant_cli.py")),
        "run-execution-controller",
        "--config",
        str(config_path),
    ]


def should_start_execution_controller(config: GatewayInstanceConfig) -> bool:
    return bool(
        config.execution_control.enabled and config.execution_control.runner.enabled
    )


def should_start_adapter_host(
    config: GatewayInstanceConfig,
    *,
    gateway_args: list[str] | tuple[str, ...] = (),
) -> bool:
    return bool(
        config.interactive_apps.adapter_host.enabled
        and "--no-apps" not in gateway_args
    )


def internal_website_will_serve_docs(
    config: GatewayInstanceConfig,
    *,
    env: Mapping[str, str],
    gateway_args: list[str],
) -> bool:
    """Return whether the supervised gateway internal website app will own docs serving."""
    website_enabled = _env_bool(
        env,
        "INTERNAL_WEBSITE_ENABLED",
        bool(config.interactive_apps.internal_website.enabled),
    )
    gateway_apps_disabled = "--no-apps" in gateway_args
    return website_enabled and not gateway_apps_disabled


def build_finish_import_task(
    config: GatewayInstanceConfig,
    config_path: Path,
    env: Mapping[str, str],
    *,
    python_executable: str | None = None,
) -> tuple[str, list[str], Mapping[str, str]] | None:
    """Store the credentials an onboarding import left, once the broker runs.

    Only when ``InstanceLayout.team_import_credentials_path`` exists
    (``team_transfer.credential_handover``). The step reaches the broker's
    transfer routes as the ``operator`` caller, which it derives from the
    broker's root token, so it starts with the environment the broker starts
    from: trusted supervisor code, never an agent process.
    """

    if not config.layout.team_import_credentials_path.is_file():
        return None
    return (
        "first-start credential import",
        [
            python_executable or sys.executable,
            "-u",
            str(resolve_python_entrypoint(REPO_ROOT / "assistant_cli.py")),
            "team",
            "finish-import",
            "--config",
            str(config_path),
        ],
        env,
    )


def build_secure_tool_broker_command(
    config_path: Path,
    *,
    python_executable: str | None = None,
) -> list[str]:
    return [
        python_executable or sys.executable,
        "-u",
        str(resolve_python_entrypoint(REPO_ROOT / "assistant_cli.py")),
        "run-secure-tool-broker",
        "--config",
        str(config_path),
    ]


class RuntimeLayoutError(RuntimeError):
    """The instance cannot start until the operator fixes its files."""


def validate_runtime_layout(config: GatewayInstanceConfig) -> None:
    """Refuse to start an unmoved old-layout or un-onboarded instance.

    Moving files is the operator's step (no automatic migration); the refusal
    prints the exact commands (runtime_settings.legacy_layout).
    """

    moves = legacy_layout_moves(config.instance_root_path)
    if moves:
        raise RuntimeLayoutError(legacy_layout_message(config.instance_root_path, moves))
    obsolete = obsolete_binding_data(config.instance_root_path)
    if obsolete.present:
        raise RuntimeLayoutError(obsolete_binding_message(obsolete))
    old_approvals = old_approval_records(
        config.instance_root_path,
        storage_settings=config.manager_storage_settings,
    )
    if old_approvals.present:
        raise RuntimeLayoutError(old_approval_message(old_approvals, config_path=config.config_path))
    try:
        # A roster that still has the removed capability-attachment section is
        # the operator's to fix; the message says how (custom_agents.team_composition).
        load_team_composition(config.layout.team_composition_path)
    except RemovedRosterAttachments as exc:
        raise RuntimeLayoutError(str(exc)) from exc
    if not config.layout.control_dir.is_dir():
        raise RuntimeLayoutError(
            f"Team control folder is missing: {config.layout.control_dir}\n"
            "Run `onboard` for this mounted instance before starting `run`."
        )
    config.layout.ensure_directories()


def resolve_external_skills_destination(config: GatewayInstanceConfig) -> Path:
    return config.layout.skills_dir


def refresh_external_skills_before_launch(
    config: GatewayInstanceConfig,
    *,
    env: Mapping[str, str],
    cli_refresh_requested: bool = False,
    cli_repositories: list[str] | None = None,
) -> ExternalSkillsSyncResult | None:
    refresh_requested = cli_refresh_requested or _env_bool(
        env,
        "TEAM_REFRESH_EXTERNAL_SKILLS",
        config.workspace.refresh_external_skills_on_start,
    )
    if not refresh_requested:
        return None
    repositories = (
        list(cli_repositories)
        if cli_repositories is not None
        else list(config.workspace.external_skill_repositories)
    )
    result = sync_external_skill_repositories(
        repositories,
        resolve_external_skills_destination(config),
    )
    LOGGER.info(
        "Refreshed %s externally managed skills into %s; removed=%s repositories=%s",
        result.refreshed_count,
        result.destination,
        ",".join(result.removed_skills) or "-",
        len(result.repositories),
    )
    return result


def terminate_process(
    process: subprocess.Popen[bytes], name: str, *, timeout_seconds: float = 10.0
) -> None:
    if process.poll() is not None:
        return
    LOGGER.info("Stopping %s (pid=%s)", name, process.pid)
    process.terminate()
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.2)
    LOGGER.warning("%s did not stop after %.1fs; killing it.", name, timeout_seconds)
    process.kill()
    process.wait(timeout=5.0)


def _runtime_restart_authority_error(payload: object) -> str | None:
    """Return why a restart request is untrusted, or ``None`` when accepted.

    Only trusted code writes restart requests (agents cannot reach the runtime
    folder); this re-checks that each one records how it was approved: the
    gateway configuration editor's operator session, or an agent restart
    applied through ``apply_change`` after a person approved it (or, with
    authorization off, after the person agreed in conversation).
    """

    if not isinstance(payload, dict):
        return "request payload must be an object"
    if payload.get("schema_version") != RUNTIME_RESTART_REQUEST_SCHEMA_VERSION:
        return f"schema_version {RUNTIME_RESTART_REQUEST_SCHEMA_VERSION} is required"
    scope = str(payload.get("scope") or "").strip().lower()
    if scope not in {"manager", "gateway", "all"}:
        return "scope must be manager, gateway, or all"
    authority = payload.get("authority")
    if not isinstance(authority, dict):
        return "structured authority provenance is required"
    mode = str(authority.get("mode") or "").strip()
    requested_by = str(payload.get("requested_by") or "").strip()
    if requested_by == "gateway_config_editor":
        if mode != AUTHORITY_MODE_AUTHENTICATED_GATEWAY_OPERATOR:
            return "gateway config-editor restart requires authenticated gateway authority"
        if str(authority.get("source") or "").strip() != "bootstrap_config_editor_session":
            return "gateway config-editor authority source is invalid"
        return None
    if mode not in {AUTHORITY_MODE_PERSON_APPROVAL, AUTHORITY_MODE_AUTHORIZATION_DISABLED}:
        return "agent restart requires a person's approval or, with authorization off, agreement"
    if not str(authority.get("change_id") or "").strip() or not all(
        re.fullmatch(r"[0-9a-f]{64}", str(authority.get(field) or "").strip())
        for field in ("plan_digest", "runtime_state_digest")
    ):
        return "agent restart plan provenance is incomplete"
    approval = authority.get("approval")
    if not isinstance(approval, dict) or not str(approval.get("approved_by") or "").strip():
        return "agent restart approval is missing"
    expected = "agreement" if mode == AUTHORITY_MODE_AUTHORIZATION_DISABLED else "person_approval"
    if str(approval.get("mode") or "") != expected:
        return "agent restart approval does not match its authority mode"
    if expected == "person_approval" and not str(approval.get("access_request_id") or "").strip():
        return "agent restart approval names no approved access request"
    if not re.fullmatch(r"runtime_restart_[0-9a-f]{12}", str(authority.get("change_id") or "")):
        return "agent restart change id is invalid"
    if not re.fullmatch(r"restart_[0-9a-f]{12}", str(payload.get("request_id") or "")):
        return "agent restart request id is invalid"
    if not isinstance(payload.get("source_task_id"), str) or not payload["source_task_id"].strip():
        return "agent restart source task is missing"
    if not isinstance(payload.get("approval_note"), str):
        return "agent restart approval note must be text"
    if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
        return "agent restart reason is missing"
    return None


def _record_runtime_restart_dispatch(request_path: Path, payload: dict[str, Any]) -> bool:
    """Record accepted agent work before any process can be stopped.

    A request and its recovery metadata are published together. A receipt
    is evidence that the supervisor accepted it, never that an agent was
    merely preparing to send it. A retained request whose receipt already
    exists was consumed before a supervisor interruption and is not replayed.
    """
    if payload.get("requested_by") == "gateway_config_editor":
        return True
    authority = payload["authority"]
    change_id = authority["change_id"]
    layout = InstanceLayout.at(request_path.parent.parent.parent)
    receipt_path = layout.runtime_restart_authorization_dir / "receipts" / f"{change_id}.json"
    if receipt_path.is_file():
        LOGGER.info("Skipping an already dispatched restart. event=runtime_restart_already_dispatched change_id=%s", change_id)
        return False
    atomic_write_json(receipt_path, {
        "schema_version": RUNTIME_RESTART_REQUEST_SCHEMA_VERSION,
        "receipt_id": new_id("restart_rcpt"),
        "change_id": change_id,
        "plan_digest": authority["plan_digest"],
        "runtime_state_digest": authority["runtime_state_digest"],
        "dispatched_at_utc": utc_now().isoformat(),
        "request_id": payload["request_id"],
        "request_path": str(request_path),
        "scope": payload["scope"],
        "reason": payload["reason"],
        "authority": authority,
        "source_task_id": payload["source_task_id"],
        "approval_note": payload["approval_note"],
    }, immutable=True)
    LOGGER.info("Accepted an approved restart. event=runtime_restart_dispatched change_id=%s task_id=%s scope=%s",
                change_id, payload["source_task_id"], payload["scope"])
    return True


def pop_runtime_restart_request(
    restart_request_dir: Path | None,
) -> dict[str, object] | None:
    if restart_request_dir is None or not restart_request_dir.exists():
        return None
    for request_path in sorted(restart_request_dir.glob("restart_*.json")):
        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning(
                "Ignoring invalid runtime restart request %s: %s", request_path, exc
            )
            request_path.unlink(missing_ok=True)
            continue
        authority_error = _runtime_restart_authority_error(payload)
        if authority_error:
            LOGGER.warning(
                "Ignoring unauthorized runtime restart request %s: %s",
                request_path,
                authority_error,
            )
            request_path.unlink(missing_ok=True)
            continue
        try:
            dispatch = _record_runtime_restart_dispatch(request_path, payload)
        except OSError:
            LOGGER.exception("Could not record the restart dispatch; the request is retained. event=runtime_restart_dispatch_failed request_path=%s", request_path)
            return None
        request_path.unlink(missing_ok=True)
        if not dispatch:
            continue
        payload["request_path"] = str(request_path)
        return payload
    return None


def _launch_environment(env: Mapping[str, str], _process: str) -> dict[str, str]:
    return dict(env)


def _environment_for_restart(
    settings_environment: SettingsEnvironment | None, env: Mapping[str, str], process: str
) -> dict[str, str]:
    """A restarted process's environment, with its copies of saved settings recomputed."""

    if settings_environment is None:
        return dict(env)
    return settings_environment.refresh(env, process=process)


def _wait_for_first_start_task(
    name: str,
    process: subprocess.Popen[bytes],
    *,
    current_processes: Callable[[], tuple[tuple[str, subprocess.Popen[bytes]], ...]],
    stop_requested: Callable[[], bool],
) -> int | None:
    """Wait for a successful step, watching its dependencies and shutdown signals."""

    while not stop_requested():
        for service_name, service in current_processes():
            code = service.poll()
            if code is not None:
                LOGGER.error(
                    "Runtime startup stopped because %s exited with code %s. "
                    "event=first_start_service_exited step=%s",
                    service_name,
                    code,
                    name,
                )
                return int(code) or 1
        code = process.poll()
        if code is None:
            time.sleep(0.2)
            continue
        if code == 0:
            LOGGER.info("%s finished.", name)
            return None
        LOGGER.error(
            "Runtime startup stopped because %s ended with code %s; see its log above. "
            "Restart the runtime to retry. event=first_start_step_failed step=%s",
            name,
            code,
            name,
        )
        return int(code)
    return 0


def _run_first_start_tasks(
    tasks: tuple[tuple[str, list[str], Mapping[str, str]], ...],
    *,
    current_processes: Callable[[], tuple[tuple[str, subprocess.Popen[bytes]], ...]],
    stop_requested: Callable[[], bool],
) -> int | None:
    """Complete credential restoration before any model-dependent process starts.

    None permits startup. An exit code stops it, including a requested stop.
    The broker and external services remain supervised while each step waits.
    A failed import retains its handover for the next start rather than leaving
    agents initialized with missing keys for the whole run.
    """

    for name, command, env in tasks:
        if stop_requested():
            return 0
        LOGGER.info("Starting %s: %s", name, " ".join(command))
        try:
            process = subprocess.Popen(command, cwd=REPO_ROOT, env=dict(env))
        except OSError as exc:
            LOGGER.error(
                "Runtime startup stopped because a first-start step could not start. "
                "Restart the runtime to retry. event=first_start_step_failed step=%s error_type=%s",
                name,
                type(exc).__name__,
            )
            return 1
        try:
            code = _wait_for_first_start_task(
                name,
                process,
                current_processes=current_processes,
                stop_requested=stop_requested,
            )
            if code is not None:
                return code
        finally:
            terminate_process(process, name)
    return None


def run_supervised(
    *,
    manager_command: list[str],
    startup_agent_commands: tuple[tuple[str, list[str]], ...] = (),
    gateway_command: list[str],
    activity_collector_command: list[str],
    secure_tool_broker_command: list[str] | None = None,
    execution_controller_command: list[str] | None = None,
    external_processes: tuple[tuple[str, subprocess.Popen[bytes]], ...] = (),
    before_external_shutdown: Callable[[], None] | None = None,
    manager_env: Mapping[str, str],
    gateway_env: Mapping[str, str],
    activity_collector_env: Mapping[str, str],
    secure_tool_broker_env: Mapping[str, str] | None = None,
    execution_controller_env: Mapping[str, str] | None = None,
    restart_request_dir: Path | None = None,
    adapter_host_command: list[str] | None = None,
    adapter_host_env: Mapping[str, str] | None = None,
    startup_agent_envs: Mapping[str, Mapping[str, str]] | None = None,
    settings_environment: SettingsEnvironment | None = None,
    agent_pass_fds: tuple[int, ...] = (),
    first_start_tasks: tuple[tuple[str, list[str], Mapping[str, str]], ...] = (),
) -> int:
    """Start and watch every runtime process; restarts reuse the environments given here.

    Each environment was scoped once at startup (``scoped_environment``), so
    a restart requested at run time cannot hand a process more secrets than
    it started with. On a restart, ``settings_environment`` recomputes the
    variables that copy saved settings, so a setting saved while the runtime
    ran takes effect in the restarted process (``docker.settings_environment``).
    Every startup agent label needs its entry in ``startup_agent_envs``.
    ``agent_pass_fds`` holds the run lock (``runtime_hosting.process_run``);
    the manager and startup agents, the processes that can start workers,
    inherit it on every start and restart so the run stays locked while any
    of them lives.

    The broker starts first. ``first_start_tasks`` must then finish successfully
    before the manager, startup agents or gateway initialize, so their model
    and embedding keys are already stored (``build_finish_import_task``).
    Failure stops startup; the retained handover retries at the next start.
    """
    stop_requested = False
    restart_env = functools.partial(_environment_for_restart, settings_environment)

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        LOGGER.info("Received signal %s; shutting down child processes.", signum)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    manager: subprocess.Popen[bytes] | None = None
    activity_collector: subprocess.Popen[bytes] | None = None
    gateway: subprocess.Popen[bytes] | None = None
    secure_tool_broker: subprocess.Popen[bytes] | None = None
    execution_controller: subprocess.Popen[bytes] | None = None
    adapter_host: subprocess.Popen[bytes] | None = None
    startup_agents: list[tuple[str, subprocess.Popen[bytes]]] = []

    def start_startup_agents(
        environment: Callable[[Mapping[str, str], str], dict[str, str]] = _launch_environment,
    ) -> None:
        startup_agents.clear()
        for label, command in startup_agent_commands:
            process_name = f"startup agent {label}"
            LOGGER.info("Starting %s: %s", process_name, " ".join(command))
            env = environment((startup_agent_envs or {})[label], process_name)
            startup_agents.append(
                (
                    process_name,
                    subprocess.Popen(
                        command, cwd=REPO_ROOT, env=env, pass_fds=agent_pass_fds
                    ),
                )
            )

    def terminate_startup_agents() -> None:
        for name, process in reversed(startup_agents):
            terminate_process(process, name)
        startup_agents.clear()

    exit_name: str | None = None
    exit_code = 0

    def current_processes() -> tuple[tuple[str, subprocess.Popen[bytes]], ...]:
        items = [
            ("manager", manager),
            *startup_agents,
            ("activity collector", activity_collector),
            ("gateway", gateway),
        ]
        if secure_tool_broker is not None:
            items.append(("secure tool broker", secure_tool_broker))
        if execution_controller is not None:
            items.append(("execution controller", execution_controller))
        if adapter_host is not None:
            items.append(("adapter host", adapter_host))
        items.extend(external_processes)
        return tuple((name, process) for name, process in items if process is not None)

    def restart_manager() -> None:
        nonlocal manager
        if manager is not None:
            terminate_process(manager, "manager")
        LOGGER.info(
            "Restarting manager after runtime restart request: %s",
            " ".join(manager_command),
        )
        manager = subprocess.Popen(
            manager_command,
            cwd=REPO_ROOT,
            env=restart_env(manager_env, "manager"),
            pass_fds=agent_pass_fds,
        )

    def restart_gateway() -> None:
        nonlocal gateway
        if gateway is not None:
            terminate_process(gateway, "gateway")
        LOGGER.info(
            "Restarting gateway after runtime restart request: %s",
            " ".join(gateway_command),
        )
        gateway = subprocess.Popen(
            gateway_command, cwd=REPO_ROOT, env=restart_env(gateway_env, "gateway")
        )

    def restart_core() -> None:
        nonlocal manager, activity_collector, gateway, execution_controller, adapter_host
        if adapter_host is not None:
            terminate_process(adapter_host, "adapter host")
        if execution_controller is not None:
            terminate_process(execution_controller, "execution controller")
        if gateway is not None:
            terminate_process(gateway, "gateway")
        if activity_collector is not None:
            terminate_process(activity_collector, "activity collector")
        terminate_startup_agents()
        if manager is not None:
            terminate_process(manager, "manager")
        LOGGER.info(
            "Restarting manager after runtime restart request: %s",
            " ".join(manager_command),
        )
        manager = subprocess.Popen(
            manager_command,
            cwd=REPO_ROOT,
            env=restart_env(manager_env, "manager"),
            pass_fds=agent_pass_fds,
        )
        start_startup_agents(restart_env)
        LOGGER.info(
            "Restarting activity collector after runtime restart request: %s",
            " ".join(activity_collector_command),
        )
        activity_collector = subprocess.Popen(
            activity_collector_command,
            cwd=REPO_ROOT,
            env=restart_env(activity_collector_env, "activity collector"),
        )
        LOGGER.info(
            "Restarting gateway after runtime restart request: %s",
            " ".join(gateway_command),
        )
        gateway = subprocess.Popen(
            gateway_command, cwd=REPO_ROOT, env=restart_env(gateway_env, "gateway")
        )
        if execution_controller_command is not None:
            LOGGER.info(
                "Restarting execution controller after runtime restart request: %s",
                " ".join(execution_controller_command),
            )
            execution_controller = subprocess.Popen(
                execution_controller_command,
                cwd=REPO_ROOT,
                env=restart_env(execution_controller_env or manager_env, "execution controller"),
            )
        if adapter_host_command is not None:
            LOGGER.info(
                "Restarting adapter host after runtime restart request: %s",
                " ".join(adapter_host_command),
            )
            adapter_host = subprocess.Popen(
                adapter_host_command,
                cwd=REPO_ROOT,
                env=restart_env(adapter_host_env or manager_env, "adapter host"),
            )

    def process_restart_request() -> None:
        request = pop_runtime_restart_request(restart_request_dir)
        if not request:
            return
        scope = str(request.get("scope") or "").strip().lower()
        reason = str(request.get("reason") or "").strip()
        LOGGER.warning(
            "Processing runtime restart request scope=%s reason=%r path=%s",
            scope,
            reason,
            request.get("request_path"),
        )
        if scope == "manager":
            restart_manager()
        elif scope == "gateway":
            restart_gateway()
        elif scope == "all":
            restart_core()
        else:
            LOGGER.warning(
                "Ignoring runtime restart request with invalid scope=%r.", scope
            )

    try:
        if secure_tool_broker_command is not None:
            LOGGER.info(
                "Starting secure tool broker: %s", " ".join(secure_tool_broker_command)
            )
            secure_tool_broker = subprocess.Popen(
                secure_tool_broker_command,
                cwd=REPO_ROOT,
                env=dict(secure_tool_broker_env or manager_env),
            )
        first_start_code = _run_first_start_tasks(
            first_start_tasks,
            current_processes=current_processes,
            stop_requested=lambda: stop_requested,
        )
        if first_start_code is not None:
            return first_start_code
        if stop_requested:
            return 0
        LOGGER.info("Starting manager: %s", " ".join(manager_command))
        manager = subprocess.Popen(
            manager_command, cwd=REPO_ROOT, env=dict(manager_env), pass_fds=agent_pass_fds
        )
        start_startup_agents()
        LOGGER.info(
            "Starting activity collector: %s", " ".join(activity_collector_command)
        )
        activity_collector = subprocess.Popen(
            activity_collector_command, cwd=REPO_ROOT, env=dict(activity_collector_env)
        )
        LOGGER.info("Starting gateway: %s", " ".join(gateway_command))
        gateway = subprocess.Popen(
            gateway_command, cwd=REPO_ROOT, env=dict(gateway_env)
        )
        if execution_controller_command is not None:
            LOGGER.info(
                "Starting execution controller: %s", " ".join(execution_controller_command)
            )
            execution_controller = subprocess.Popen(
                execution_controller_command,
                cwd=REPO_ROOT,
                env=dict(execution_controller_env or manager_env),
            )
        if adapter_host_command is not None:
            LOGGER.info("Starting adapter host: %s", " ".join(adapter_host_command))
            adapter_host = subprocess.Popen(
                adapter_host_command, cwd=REPO_ROOT, env=dict(adapter_host_env or manager_env)
            )
        while True:
            if stop_requested:
                break
            process_restart_request()
            for name, process in current_processes():
                code = process.poll()
                if code is not None:
                    exit_name = name
                    exit_code = int(code)
                    LOGGER.warning(
                        "%s exited with code %s; stopping the remaining process.",
                        name,
                        code,
                    )
                    stop_requested = True
                    break
            if stop_requested:
                break
            time.sleep(1.0)
    finally:
        if adapter_host is not None:
            terminate_process(adapter_host, "adapter host")
        if execution_controller is not None:
            terminate_process(execution_controller, "execution controller")
        if secure_tool_broker is not None:
            terminate_process(secure_tool_broker, "secure tool broker")
        if gateway is not None:
            terminate_process(gateway, "gateway")
        if activity_collector is not None:
            terminate_process(activity_collector, "activity collector")
        terminate_startup_agents()
        if manager is not None:
            terminate_process(manager, "manager")
        stop_external_processes(external_processes, before_external_shutdown)

    return exit_code if exit_name is not None else 0


def stop_external_processes(
    external_processes: tuple[tuple[str, subprocess.Popen[bytes]], ...],
    before_external_shutdown: Callable[[], None] | None,
) -> None:
    """Stop NATS, Sealbox and the OpenShell gateway last, after their cleanup step."""

    if before_external_shutdown is not None:
        before_external_shutdown()
    for name, process in reversed(external_processes):
        terminate_process(process, name)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    materialize_secret_file_envs()
    args = build_parser().parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"No such instance config: {config_path}")

    startup_notice = ensure_instance_config_ready_for_headless_launch(config_path)
    # The saved file plus this deployment's details (for the Docker bundle, its
    # own NATS and PostgreSQL), applied in memory and never saved.
    # A half-imported team must never run (team_transfer.service). Checked
    # first, so the operator sees this cause rather than a missing folder.
    from team_transfer.service import TransferError, require_finished_import

    try:
        config = GatewayInstanceConfig.load_for_process(config_path)
        require_finished_import(config.layout)
    except TransferError as exc:
        LOGGER.error("Headless launch refused. event=team_import_unfinished %s", exc)
        return 2
    try:
        validate_runtime_layout(config)
    except RuntimeLayoutError as exc:
        LOGGER.error("Headless launch refused. %s", exc)
        return 2
    # Before anything starts: holding the run lock proves that no agent process
    # of an earlier run is alive, so their unfinished worker starts can be
    # cleared (runtime_hosting.process_run).
    try:
        process_run = acquire_process_run(config.layout.process_run_lock_path)
    except ProcessRunBusy as exc:
        LOGGER.error("Headless launch refused. %s", exc)
        return 2
    try:
        prepare_manager_database(config)
    except (sqlite3.Error, OSError) as exc:
        LOGGER.error(
            "Headless launch refused: the manager database could not be set up before the services start. "
            "event=manager_database_prepare_failed database=%s error=%s",
            resolve_manager_db_path(config),
            exc,
        )
        return 2
    manifest_paths = write_runtime_manifests(
        config,
        config.instance_root_path / "runtime" / "service-manifests",
    )
    manifest_paths_by_service = {
        json.loads(path.read_text(encoding="utf-8"))["service"]: path
        for path in manifest_paths
    }
    try:
        env_path = resolve_env_file_path(config, args.env)
    except ValueError as exc:
        LOGGER.error("Headless launch refused. %s", exc)
        return 2
    gateway_args = list(args.gateway_args)
    if gateway_args and gateway_args[0] == "--":
        gateway_args = gateway_args[1:]

    inherited_env = build_process_env(config, env_path=env_path)
    use_process_timezone(inherited_env)
    refusal = missing_secret_refusal(config, env_path, inherited_env)
    if refusal:
        LOGGER.error("Headless launch refused. %s", refusal)
        return 2
    warn_missing_environment_secrets(config, inherited_env)
    inherited_env = with_verify_keys(inherited_env)
    try:
        refresh_external_skills_before_launch(
            config,
            env=inherited_env,
            cli_refresh_requested=bool(args.refresh_external_skills),
            cli_repositories=args.skills_repository,
        )
    except (ExternalSkillsSyncError, ValueError) as exc:
        LOGGER.error(
            "Headless launch aborted because external skills could not be refreshed: %s",
            exc,
        )
        return 2
    local_nats_process: subprocess.Popen[bytes] | None = None
    if local_nats_enabled_from_env(inherited_env, default=True):
        apply_local_nats_env(inherited_env)
        local_nats_process = start_local_nats(
            env=inherited_env, logger=LOGGER, cwd=REPO_ROOT
        )
    else:
        LOGGER.warning(
            "Local NATS startup disabled by environment; using configured NATS endpoint(s): %s",
            inherited_env.get("NATS_SERVERS") or inherited_env.get("NATS_URL"),
        )
    run_nats_consumer_preflight(config_path=config_path, env=inherited_env)
    managed_sealbox_service: ManagedSealboxService | None = None
    managed_openshell_gateway: ManagedOpenShellGateway | None = None
    managed_openshell_sandbox: ManagedOpenShellSandbox | None = None
    try:
        managed_sealbox_service = ensure_managed_sealbox_service(
            config, env=inherited_env
        )
        managed_openshell_gateway = ensure_openshell_gateway(config, env=inherited_env)
        managed_openshell_sandbox = ensure_openshell_sandbox(config, env=inherited_env)
    except Exception:
        if managed_openshell_gateway is not None:
            terminate_process(managed_openshell_gateway.process, "OpenShell gateway")
        if managed_sealbox_service is not None:
            terminate_process(managed_sealbox_service.process, "Sealbox service")
        if local_nats_process is not None:
            terminate_process(local_nats_process, "nats")
        raise
    broker_root = str(inherited_env.get(BROKER_TOKEN_ENV) or "").strip() or None
    renamed_secrets = configured_secret_names(config)
    runtime_env = with_website_base_url(
        inherited_env,
        serves_docs=internal_website_will_serve_docs(config, env=inherited_env, gateway_args=gateway_args),
    )
    env_file_values = load_env_file_values(env_path)
    settings_environment = SettingsEnvironment.at_launch(
        config,
        defaults=build_instance_env_defaults,
        launch_env=runtime_env,
        operator_keys=frozenset(env_file_values) | frozenset(os.environ),
        secret_names=frozenset(renamed_secrets),
    )
    # Where each override came from, so the Status page names the file or environment to change.
    write_environment_overrides(
        config.layout.environment_overrides_path,
        environment_overrides(
            settings_environment,
            config=config,
            launch_env=runtime_env,
            env_file=env_path,
            env_file_values=env_file_values,
            container_env=os.environ,
        ),
    )
    manager_env = {
        **scoped_environment(
            PROCESS_MANAGER,
            runtime_env,
            renamed=renamed_secrets,
            broker_root=broker_root,
            caller_label=CALLER_MANAGER,
        ),
        **process_run.environment(),
    }
    service_envs = build_service_process_environments(
        runtime_env=runtime_env,
        inherited_env=inherited_env,
        manifest_paths_by_service=manifest_paths_by_service,
        renamed=renamed_secrets,
    )
    gateway_env = service_envs.gateway
    activity_collector_env = service_envs.activity_collector
    secure_tool_broker_env = service_envs.secure_broker
    execution_controller_env = service_envs.execution_controller
    adapter_host_env = service_envs.adapter_host
    if startup_notice:
        append_headless_startup_notice(gateway_env, startup_notice)
    try:
        validate_memory_graph_embedding_preflight(
            config, env=manager_env, config_path=config_path
        )
    except Exception as exc:
        if exc.__class__.__name__ == "EmbeddingProfileMismatchError":
            LOGGER.error(
                "Headless launch aborted because memory-graph embeddings are inconsistent with the configured provider."
            )
            return 1
        raise
    manager_command = build_manager_command(config)
    startup_agent_commands = tuple(build_startup_agent_commands(config))
    startup_agent_envs = {
        label: {**env, **process_run.environment()}
        for label, env in build_startup_agent_environments(
            startup_agent_commands,
            base_env=runtime_env,
            broker_root=broker_root,
            renamed=renamed_secrets,
        ).items()
    }
    activity_collector_command = build_activity_collector_command(config_path)
    gateway_command = build_gateway_command(config_path, gateway_args)
    secure_tool_broker_command = build_secure_tool_broker_command(config_path)
    execution_controller_command = (
        build_execution_controller_command(config_path)
        if should_start_execution_controller(config)
        else None
    )
    adapter_host_command = (
        build_adapter_host_command(config_path)
        if should_start_adapter_host(config, gateway_args=gateway_args)
        else None
    )
    external_process_list: list[tuple[str, subprocess.Popen[bytes]]] = []
    if local_nats_process is not None:
        external_process_list.append(("nats", local_nats_process))
    if managed_openshell_gateway is not None:
        external_process_list.append(
            ("OpenShell gateway", managed_openshell_gateway.process)
        )
    if managed_sealbox_service is not None:
        external_process_list.append(
            ("Sealbox service", managed_sealbox_service.process)
        )
    external_processes = tuple(external_process_list)

    def cleanup_managed_openshell() -> None:
        cleanup_openshell_sandbox(managed_openshell_sandbox, env=inherited_env)

    finish_import = build_finish_import_task(config, config_path, inherited_env)
    # Last step before any service starts, so every check finds the streams.
    try:
        prepare_message_streams(config_path, gateway_env)
    except MESSAGE_STREAM_PREPARE_ERRORS as exc:
        LOGGER.error(
            "Headless launch refused: the team's message streams could not be set up before the services start. "
            "event=nats_streams_prepare_failed error_type=%s error=%s",
            type(exc).__name__,
            exc,
        )
        stop_external_processes(external_processes, cleanup_managed_openshell)
        return 2

    return run_supervised(
        manager_command=manager_command,
        startup_agent_commands=startup_agent_commands,
        gateway_command=gateway_command,
        activity_collector_command=activity_collector_command,
        secure_tool_broker_command=secure_tool_broker_command,
        execution_controller_command=execution_controller_command,
        adapter_host_command=adapter_host_command,
        external_processes=external_processes,
        before_external_shutdown=cleanup_managed_openshell,
        manager_env=manager_env,
        gateway_env=gateway_env,
        activity_collector_env=activity_collector_env,
        secure_tool_broker_env=secure_tool_broker_env,
        execution_controller_env=execution_controller_env,
        adapter_host_env=adapter_host_env,
        restart_request_dir=config.instance_root_path / "runtime" / "restart_requests",
        startup_agent_envs=startup_agent_envs,
        settings_environment=settings_environment,
        agent_pass_fds=process_run.pass_fds(),
        first_start_tasks=(finish_import,) if finish_import else (),
    )


if __name__ == "__main__":
    raise SystemExit(main())
